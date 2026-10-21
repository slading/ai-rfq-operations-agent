"""Regression tests for the Codex review findings on PR #14.

Three guarantees, each reproduced and then pinned:

* **P1** - a run another worker advances between intake and the quote write
  cannot receive a quote. The guard runs inside ``QuoteWriter``'s transaction,
  so the refusal leaves no quote, no line and no idempotency claim behind.
* **P2a** - a replay rejoins only a run whose *complete* lifecycle projection is
  the untouched initial attempt this path wrote; every other lifecycle column
  is a ``CONFLICT``.
* **P2b** - concurrent first deliveries serialise instead of racing a deferred
  read into a constraint failure, and a lock that outlives the bounded budget
  is a typed error rather than a raw ``OperationalError``.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError

from rfq_agent import acceptance
from rfq_agent.acceptance import (
    AcceptanceContentionError,
    AcceptanceOutcome,
    AcceptanceReason,
    accept_rfq,
)
from rfq_agent.config import DatabaseSettings
from rfq_agent.domain.workflow import (
    ReasonCode,
    RunActor,
    RunOutcome,
    RunState,
    RunTrigger,
    TransitionEvent,
)
from rfq_agent.intake import provision_intake
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import (
    IdempotencyClaimRow,
    IntakeEventRow,
    QuoteLineRow,
    QuoteRow,
    RfqRow,
    RunRow,
)
from rfq_agent.persistence.writers import QuoteWriter, persist_quote
from rfq_agent.seed import reset_and_seed
from rfq_agent.workflow.transitions import _transition_run
from tests.acceptance.conftest import make_declaration

#: A fixed instant for transitions this file makes on "another worker's" behalf.
TRANSITIONED_AT = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _quotes(session) -> list[str]:
    session.expire_all()
    return list(session.scalars(select(QuoteRow.quote_id)))


def _rows(url: str) -> dict[str, int]:
    database = Database.create(url)
    try:
        with database.session_factory() as session:
            return {
                model.__tablename__: _count(session, model)
                for model in (
                    RfqRow,
                    RunRow,
                    IntakeEventRow,
                    QuoteRow,
                    QuoteLineRow,
                    IdempotencyClaimRow,
                )
            }
    finally:
        database.dispose()


# ---------------------------------------------------------------------------
# P1 - the run moves between intake and persistence
# ---------------------------------------------------------------------------


def _advance_run_on_another_connection(database: Database, run_id: str) -> None:
    """Move ``run_id`` out of ``RECEIVED`` the way a worker would: its own txn."""
    with database.session_factory() as other:
        other.execute(text("BEGIN IMMEDIATE"))
        _transition_run(
            other,
            run_id=run_id,
            event=TransitionEvent.TRIAGE_START,
            actor=RunActor.SYSTEM,
            expected_version=0,
            occurred_at=TRANSITIONED_AT,
        )
        other.commit()


def test_a_run_advanced_before_persistence_is_refused_without_a_quote(
    seeded, session, monkeypatch
) -> None:
    """The finding's reproduction: pause between intake and write, advance, resume."""
    original = acceptance.run_quote

    def run_quote_then_advance(request, reader):
        result = original(request, reader)
        _advance_run_on_another_connection(seeded, request.run_id)
        return result

    monkeypatch.setattr(acceptance, "run_quote", run_quote_then_advance)

    report = accept_rfq(make_declaration(), seeded)

    assert report.outcome is AcceptanceOutcome.CONFLICT
    assert report.reason is AcceptanceReason.STORED_STATE_CONFLICT
    assert report.quote_id is None
    assert report.total is None

    session.expire_all()
    assert _quotes(session) == []
    assert _count(session, QuoteLineRow) == 0
    # The claim was taken before the guard refused and rolled back with it, so a
    # later retry of the same write is free rather than "already claimed".
    assert _count(session, IdempotencyClaimRow) == 0

    run = session.get(RunRow, "RUN_TEST_0001")
    assert run is not None
    assert run.state is RunState.TRIAGING
    assert run.row_version == 1


def test_a_run_terminated_before_persistence_is_refused(seeded, session, monkeypatch) -> None:
    """A terminal run must not collect a DRAFT either."""
    original = acceptance.run_quote

    def run_quote_then_reject(request, reader):
        result = original(request, reader)
        _advance_run_on_another_connection(seeded, request.run_id)
        with seeded.session_factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            _transition_run(
                other,
                run_id=request.run_id,
                event=TransitionEvent.CLASSIFIED_NOT_RFQ,
                actor=RunActor.AGENT,
                expected_version=1,
                reason_code=ReasonCode.NOT_AN_RFQ,
                outcome=RunOutcome.NOT_AN_RFQ,
                occurred_at=TRANSITIONED_AT,
            )
            other.commit()
        return result

    monkeypatch.setattr(acceptance, "run_quote", run_quote_then_reject)

    report = accept_rfq(make_declaration(), seeded)

    assert report.outcome is AcceptanceOutcome.CONFLICT
    session.expire_all()
    assert _quotes(session) == []
    run = session.get(RunRow, "RUN_TEST_0001")
    assert run is not None
    assert run.state is RunState.NOT_AN_RFQ
    assert run.outcome is RunOutcome.NOT_AN_RFQ


def test_a_rolled_back_write_leaves_no_claim_behind(
    seeded, session, migrated_template: Path, tmp_path: Path, monkeypatch
) -> None:
    """The write is one unit of work: a claim staged by a failed attempt is gone.

    The claim is taken inside the caller's transaction, so a rollback must take
    it too. If it survived, the retry that follows would look already done and
    the quotation would never be written - a silent, permanent loss.
    """
    captured: dict[str, object] = {}
    original = acceptance.persist_quote

    def capture(work_session, calculation, ledger, **kwargs):
        captured["calculation"] = calculation
        captured["ledger"] = ledger
        return original(work_session, calculation, ledger, **kwargs)

    monkeypatch.setattr(acceptance, "persist_quote", capture)
    first = accept_rfq(make_declaration(), seeded)
    monkeypatch.undo()
    assert first.outcome is AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW
    assert {"calculation", "ledger"} <= set(captured)

    # A second, independent database holding the same intake rows.
    second_path = tmp_path / "second.db"
    shutil.copyfile(migrated_template, second_path)
    second = Database.create(f"sqlite:///{second_path}")
    try:
        with second.session() as work:
            reset_and_seed(work)
        with second.session() as work:
            provision_intake(work, make_declaration().intake)

        def write_then_fail() -> None:
            with second.session() as work:
                persist_quote(work, captured["calculation"], captured["ledger"])
                raise RuntimeError("simulated failure before commit")

        with pytest.raises(RuntimeError, match="simulated failure"):
            write_then_fail()

        with second.session_factory() as check:
            assert _count(check, QuoteRow) == 0
            assert _count(check, QuoteLineRow) == 0
            assert _count(check, IdempotencyClaimRow) == 0

        write = QuoteWriter(second).persist(captured["calculation"], captured["ledger"])
        assert write.duplicate is False
        with second.session_factory() as check:
            assert _count(check, QuoteRow) == 1
    finally:
        second.dispose()

    # The first database still holds exactly its one quote.
    session.expire_all()
    assert _quotes(session) == ["QTE_TEST_0001"]


# ---------------------------------------------------------------------------
# P2a - a replay rejoins only an untouched run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_no", 2),
        ("trigger", RunTrigger.RETRY),
        ("prior_run_id", "__created__"),
        ("failure_code", ReasonCode.MISSING_QTY),
        ("retry_count", 1),
        ("checkpoint_stage", RunState.EXTRACTING),
        ("row_version", 1),
        ("started_at", datetime(2026, 10, 7, 9, 0, tzinfo=UTC)),
        ("finished_at", datetime(2026, 10, 6, 12, 0, tzinfo=UTC)),
    ],
)
def test_a_replay_refuses_a_run_whose_lifecycle_columns_moved(
    seeded, session, field: str, value: object
) -> None:
    """Every lifecycle column is part of "untouched", not just state and outcome."""
    declaration = make_declaration(number=3, claim_quantity=26)
    first = accept_rfq(declaration, seeded)
    assert first.outcome is AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE

    changes: dict[str, object] = {field: value}
    if field == "retry_count":
        # The schema ties a retry count to a checkpoint: a retry necessarily has one.
        changes["checkpoint_stage"] = RunState.EXTRACTING
    if field == "prior_run_id":
        # A prior run is a real row: the foreign key is enforced.
        with seeded.session() as work:
            work.add(
                RunRow(
                    run_id="RUN_PRIOR_0003",
                    rfq_id="RFQ_TEST_0003",
                    attempt_no=2,
                    trigger=RunTrigger.HUMAN_RERUN,
                    state=RunState.RECEIVED,
                    row_version=0,
                    started_at=TRANSITIONED_AT,
                )
            )
        changes["prior_run_id"] = "RUN_PRIOR_0003"

    with seeded.session_factory() as work:
        work.execute(update(RunRow).where(RunRow.run_id == "RUN_TEST_0003").values(**changes))
        work.commit()

    report = accept_rfq(declaration, seeded)

    assert report.outcome is AcceptanceOutcome.CONFLICT
    assert report.reason is AcceptanceReason.STORED_STATE_CONFLICT
    assert report.quote_id is None
    session.expire_all()
    assert _count(session, QuoteRow) == 0


def test_a_replay_refuses_a_missing_run(seeded) -> None:
    """A declaration cannot rejoin history that is not there."""
    declaration = make_declaration()
    with seeded.session() as work:
        provision_intake(work, declaration.intake)
    with seeded.session_factory() as work:
        # The run is removed the only way a foreign key lets it be: with its RFQ.
        work.execute(text("PRAGMA foreign_keys=OFF"))
        work.execute(text("DELETE FROM runs WHERE run_id = 'RUN_TEST_0001'"))
        work.commit()

    report = accept_rfq(declaration, seeded)

    assert report.outcome is AcceptanceOutcome.CONFLICT
    assert report.reason is AcceptanceReason.STORED_STATE_CONFLICT


def test_an_untouched_replay_is_still_accepted(seeded) -> None:
    """The control: nothing changed, so the replay is still idempotent."""
    declaration = make_declaration()
    first = accept_rfq(declaration, seeded)
    second = accept_rfq(declaration, seeded)

    assert first.outcome is AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW
    assert second.outcome is AcceptanceOutcome.DUPLICATE
    assert second.total == Decimal("28750.00")


# ---------------------------------------------------------------------------
# P2b - concurrent first deliveries
# ---------------------------------------------------------------------------


def _deliver_once(
    url: str,
    declaration,
    box: dict[str, object],
    failures: list[BaseException],
    ready: threading.Event | None = None,
) -> None:
    """Deliver ``declaration`` on its own connection, recording the outcome or error."""
    contender = Database.create(url)
    try:
        if ready is not None:
            ready.set()
        box["report"] = accept_rfq(declaration, contender)
    except BaseException as error:  # noqa: BLE001 - asserted by the calling test
        failures.append(error)
    finally:
        contender.dispose()


def test_an_identical_delivery_waits_for_the_writer_then_reports_a_duplicate(
    seeded_url: str, monkeypatch
) -> None:
    """The finding's reproduction: a first delivery that meets a held write lock.

    Worker A is paused *inside* its intake transaction, holding SQLite's write
    lock with its rows flushed but uncommitted. Worker B's first delivery of the
    same message starts in that window. Before the fix, B read no rows (A's were
    uncommitted), inserted, and lost the race with a constraint failure - a
    generic ``CONFLICT``. It must instead wait for A's commit, read A's rows and
    report the idempotent ``DUPLICATE``.
    """
    url = seeded_url
    declaration = make_declaration()
    original = acceptance.provision_intake
    paused = threading.Event()
    release = threading.Event()
    first_call = {"pending": True}

    def provision_then_pause(session, declared):
        stored = original(session, declared)
        if first_call["pending"]:
            first_call["pending"] = False
            paused.set()
            release.wait(30)
        return stored

    monkeypatch.setattr(acceptance, "provision_intake", provision_then_pause)

    worker_a: dict[str, object] = {}
    worker_b: dict[str, object] = {}
    failures: list[BaseException] = []
    ready = threading.Event()
    thread_a = threading.Thread(target=_deliver_once, args=(url, declaration, worker_a, failures))
    thread_a.start()
    assert paused.wait(30), "worker A never reached its intake transaction"

    thread_b = threading.Thread(
        target=_deliver_once, args=(url, declaration, worker_b, failures, ready)
    )
    thread_b.start()
    try:
        assert ready.wait(30)
        # Give B time to reach the lock, then let A commit. The assertions below
        # hold in either order; this pause only makes the race reproducible.
        time.sleep(0.2)
    finally:
        release.set()
        thread_a.join(30)
        thread_b.join(30)

    assert failures == []
    assert worker_a["report"].outcome is AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW
    assert worker_b["report"].outcome is AcceptanceOutcome.DUPLICATE
    assert worker_b["report"].created is False
    counts = _rows(url)
    assert counts["rfqs"] == 1
    assert counts["runs"] == 1
    assert counts["intake_events"] == 1
    assert counts["quotes"] == 1
    assert counts["quote_lines"] == 1


def test_simultaneous_identical_deliveries_produce_one_request(seeded_url: str) -> None:
    """N threads, one declaration: one set of rows, every caller a clean outcome."""
    url = seeded_url
    declaration = make_declaration()
    start = threading.Barrier(3)
    results: list[object] = []
    errors: list[BaseException] = []

    def deliver() -> None:
        contender = Database.create(url)
        try:
            start.wait(30)
            results.append(accept_rfq(declaration, contender))
        except BaseException as error:  # noqa: BLE001 - collected for the assertion
            errors.append(error)
        finally:
            contender.dispose()

    threads = [threading.Thread(target=deliver) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    assert len(results) == 3
    assert {report.outcome for report in results} <= {
        AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW,
        AcceptanceOutcome.DUPLICATE,
    }
    assert sum(1 for report in results if report.created) == 1
    counts = _rows(url)
    assert counts["rfqs"] == 1
    assert counts["runs"] == 1
    assert counts["intake_events"] == 1
    assert counts["quotes"] == 1
    assert counts["quote_lines"] == 1


def test_simultaneous_conflicting_deliveries_stay_a_conflict(seeded_url: str) -> None:
    """Two RFQs sharing one idempotency key: exactly one wins, the other conflicts."""
    url = seeded_url
    key = "acceptance:test:shared"
    start = threading.Barrier(2)
    outcomes: list[object] = []

    def deliver(number: int) -> None:
        contender = Database.create(url)
        try:
            start.wait(30)
            declaration = make_declaration(
                number=number,
                quantity=25 if number == 1 else 30,
                idempotency_key=key,
            )
            outcomes.append(accept_rfq(declaration, contender))
        finally:
            contender.dispose()

    threads = [threading.Thread(target=deliver, args=(number,)) for number in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert len(outcomes) == 2
    assert sorted(report.outcome.value for report in outcomes) == [
        AcceptanceOutcome.CONFLICT.value,
        AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW.value,
    ]
    counts = _rows(url)
    assert counts["rfqs"] == 1
    assert counts["quotes"] == 1


def test_contention_beyond_the_budget_is_a_typed_error(seeded_url: str) -> None:
    """A lock nothing releases is reported, bounded, instead of raising raw SQLite errors."""
    url = seeded_url
    holder = Database.create(url)
    contender = Database.create(
        url, database_settings=DatabaseSettings(url=url, busy_timeout_ms=10)
    )
    locked = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with holder.session_factory() as work:
            work.execute(text("BEGIN IMMEDIATE"))
            work.execute(update(RunRow).values(row_version=RunRow.row_version))
            locked.set()
            release.wait(30)
            work.rollback()

    thread = threading.Thread(target=hold)
    thread.start()
    assert locked.wait(30)
    started = datetime.now(tz=UTC)
    try:
        with pytest.raises(AcceptanceContentionError):
            accept_rfq(make_declaration(), contender)
    finally:
        elapsed = (datetime.now(tz=UTC) - started).total_seconds()
        release.set()
        thread.join(30)
        contender.dispose()
        holder.dispose()

    # Bounded: the retry budget is a fixed handful of short backoffs, not a wait.
    assert elapsed < 5
    assert _rows(url)["rfqs"] == 0


def test_a_non_contention_operational_error_is_not_retried() -> None:
    """Only the busy/locked family is retried; a real database defect surfaces at once."""
    attempts = {"count": 0}

    def fail():
        attempts["count"] += 1
        raise OperationalError("SELECT 1", {}, sqlite3.OperationalError("no such table: rfqs"))

    with pytest.raises(OperationalError):
        acceptance._with_busy_retry(fail, on_exhausted=AcceptanceContentionError("busy"))

    assert attempts["count"] == 1


def test_the_retry_budget_is_bounded() -> None:
    """Contention is retried a fixed number of times, never indefinitely."""
    attempts = {"count": 0}

    def always_busy():
        attempts["count"] += 1
        raise OperationalError(
            "INSERT INTO rfqs", {}, sqlite3.OperationalError("database is locked")
        )

    with pytest.raises(AcceptanceContentionError):
        acceptance._with_busy_retry(always_busy, on_exhausted=AcceptanceContentionError("busy"))

    assert attempts["count"] == acceptance._WRITE_ATTEMPTS
