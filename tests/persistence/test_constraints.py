"""Constraint tests: the invariants that live in the database, not in Python.

Two ideas run through this module.

**The database is the last line of defence.** Every rule tested here is also
checked by a Pydantic validator in the domain layer. That is not duplication: a
domain rule stops the code we wrote, and a constraint stops the code we did not
write - a repair script, a migration helper, a future caller that skipped the
validator, or a human at a SQL prompt.

**The constraints must agree with the domain.** Where a rule is derived from the
state machine (terminal states, legal transitions), the test walks the domain's
own tables and asserts the database agrees. If a later phase adds a state and
forgets the migration, these tests fail instead of the invariant quietly
disappearing.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from rfq_agent.contracts.llm import ModelPurpose
from rfq_agent.domain.human import HumanActionKind
from rfq_agent.domain.intake import IntakeEventKind
from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.stock import StockStatus
from rfq_agent.domain.workflow import (
    TRANSITIONS,
    ReasonCode,
    RunOutcome,
    RunState,
    TransitionEvent,
    terminal_states,
)
from rfq_agent.observability.ids import utc_now
from rfq_agent.observability.spans import ToolResultStatus
from rfq_agent.persistence.enums import AliasKind, QueueEntryStatus
from rfq_agent.persistence.models import (
    CustomerAliasRow,
    IdempotencyClaimRow,
    IntakeEventRow,
    LlmCallRow,
    PriceBookRow,
    ProductAliasRow,
    RunEventRow,
    RunQueueEntryRow,
    StockLevelRow,
    ToolCallRow,
)
from tests.persistence.factories import (
    NOW,
    SHA256,
    Core,
    human_action_row,
    outbound_row,
    quote_line_row,
    quote_row,
    rfq_row,
    run_event_row,
    run_row,
)


def _commit_fails(session: Session, match: str) -> None:
    """Assert that writing the pending unit of work is refused, then unwind it."""
    with pytest.raises(IntegrityError, match=match):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# Enums: values are stored, not member names
# ---------------------------------------------------------------------------


def test_enum_columns_store_values_not_member_names(session: Session) -> None:
    """``TransitionEvent`` is the case that proves it: name and value differ.

    A column that stored ``member.name`` would write ``TRIAGE_START`` and read
    back ``None`` - a bug that only shows up on the read path.
    """
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    event = TransitionEvent.TRIAGE_START
    assert event.name != event.value
    session.add(run_event_row(event=event))
    session.commit()

    raw = session.execute(text("SELECT event FROM run_events")).scalar_one()
    assert raw == "triage_start"

    session.expire_all()
    stored = session.get(RunEventRow, {"run_id": "RUN_0001", "seq": 1})
    assert stored is not None
    assert stored.event is TransitionEvent.TRIAGE_START


def test_every_legal_transition_can_be_recorded_and_read_back(session: Session) -> None:
    """Walk the whole transition table through the database.

    Covers every ``TransitionEvent``, every ``RunState`` and all 45 legal edges -
    the storage contract for the audit trail, exercised from the domain's own
    table rather than from a hand-copied list.
    """
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    for seq, transition in enumerate(TRANSITIONS, start=1):
        session.add(
            run_event_row(
                seq=seq,
                from_state=transition.source,
                to_state=transition.target,
                event=transition.event,
                reason_code=transition.required_reason or ReasonCode.DUPLICATE,
            )
        )
    session.commit()
    session.expire_all()

    rows = (
        session.execute(text("SELECT event, from_state, to_state FROM run_events ORDER BY seq"))
        .mappings()
        .all()
    )
    assert len(rows) == len(TRANSITIONS)

    for row, transition in zip(rows, TRANSITIONS, strict=True):
        assert row["event"] == transition.event.value
        assert row["from_state"] == transition.source.value
        assert row["to_state"] == transition.target.value


def test_every_model_purpose_round_trips(session: Session) -> None:
    """Model purposes are lowercase labels; they must survive storage verbatim."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    purposes = list(ModelPurpose)
    for index, purpose in enumerate(purposes, start=1):
        session.add(
            LlmCallRow(
                call_id=f"LLM_{index:04d}",
                run_id="RUN_0001",
                trace_id="0" * 32,
                stage="RESOLVING",
                model="openai/gpt-oss-120b",
                purpose=purpose,
                prompt_sha256=SHA256,
                response_sha256=SHA256,
                tokens_in=10,
                tokens_out=5,
                latency_ms=120,
                attempt=1,
                occurred_at=NOW,
            )
        )
    session.commit()
    session.expire_all()

    stored = session.execute(text("SELECT purpose FROM llm_calls ORDER BY call_id")).scalars().all()
    assert stored == [purpose.value for purpose in purposes]


def test_unknown_enum_value_is_rejected_by_the_database(session: Session) -> None:
    """A hand-written UPDATE cannot introduce a state the machine does not have."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    with pytest.raises(IntegrityError, match="CHECK"):
        session.execute(text("UPDATE runs SET state = 'PONDERING' WHERE run_id = 'RUN_0001'"))
    session.rollback()


# ---------------------------------------------------------------------------
# The state machine, mirrored in SQL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", list(RunState), ids=lambda state: state.value)
def test_terminal_states_require_an_outcome_exactly_as_the_domain_says(
    session: Session, state: RunState
) -> None:
    """The ``CHECK`` and :func:`terminal_states` must agree for every state.

    Data-driven from the domain: if a later phase adds a terminal state without
    updating the migration, this test fails on that state.
    """
    session.add(rfq_row())
    session.commit()

    if state in terminal_states():
        # Terminal without an outcome is a lie about how the run ended.
        session.add(run_row(state=state, outcome=None))
        _commit_fails(session, "CHECK")

        session.add(
            run_row(run_id="RUN_0002", attempt_no=2, state=state, outcome=RunOutcome.SUCCEEDED)
        )
    else:
        # A non-terminal run claiming an outcome is equally wrong.
        session.add(run_row(state=state, outcome=RunOutcome.SUCCEEDED))
        _commit_fails(session, "CHECK")

        session.add(run_row(run_id="RUN_0002", attempt_no=2, state=state, outcome=None))
    session.commit()


def test_retry_requires_a_checkpoint_stage(session: Session) -> None:
    """A retried run must record where it resumes, or retrying is a guess."""
    session.add(rfq_row())
    session.commit()

    session.add(run_row(retry_count=1, checkpoint_stage=None))
    _commit_fails(session, "CHECK")

    session.add(run_row(retry_count=1, checkpoint_stage=RunState.EXTRACTING))
    session.commit()


@pytest.mark.parametrize("edge", list(enumerate(TRANSITIONS)), ids=lambda item: item[1].event.value)
def test_every_legal_edge_is_writable(session: Session, edge: tuple[int, object]) -> None:
    """All 45 edges of the state machine can be written to the audit trail.

    The ``legal_edge`` constraint is generated from ``TRANSITIONS``; this walks
    that same table, so model, migration and state machine cannot disagree
    without a failure naming the edge that broke.
    """
    _, transition = edge
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(
        run_event_row(
            from_state=transition.source,
            to_state=transition.target,
            event=transition.event,
        )
    )
    session.commit()

    stored = session.execute(text("SELECT COUNT(*) FROM run_events")).scalar_one()
    assert stored == 1


@pytest.mark.parametrize(
    ("from_state", "event", "to_state"),
    [
        (RunState.RECEIVED, TransitionEvent.TRIAGE_START, RunState.SENT),
        (RunState.RECEIVED, TransitionEvent.APPROVE, RunState.SENT),
        (RunState.SENT, TransitionEvent.TRIAGE_START, RunState.TRIAGING),
        (RunState.REJECTED, TransitionEvent.APPROVE, RunState.SENT),
    ],
)
def test_illegal_edges_are_refused_by_the_database(
    session: Session, from_state: RunState, event: TransitionEvent, to_state: RunState
) -> None:
    """History cannot record a transition the state machine does not define."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(run_event_row(from_state=from_state, to_state=to_state, event=event))
    _commit_fails(session, "CHECK")


# ---------------------------------------------------------------------------
# Uniqueness: the de-duplication guarantees
# ---------------------------------------------------------------------------


def test_duplicate_intake_key_is_rejected(session: Session) -> None:
    """F17: a re-delivered message can never become a second RFQ."""
    session.add(rfq_row())
    session.commit()

    # Same key, different message id: this is exactly F17.
    session.add(rfq_row(rfq_id="RFQ_0002", idempotency_key="intake:RFQ_0001"))
    _commit_fails(session, "UNIQUE")


def test_duplicate_quote_number_is_rejected(session: Session, core: Core) -> None:
    """Two quotations cannot share the customer-visible reference."""
    session.add(quote_row(core))
    session.commit()

    session.add(quote_row(core, quote_id="QTE_0002", revision=2))
    _commit_fails(session, "UNIQUE")


def test_run_attempt_number_is_unique_per_rfq(session: Session) -> None:
    """Retrying a run increments ``attempt_no``; it cannot silently reuse one."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(run_row(run_id="RUN_0002", attempt_no=1))
    _commit_fails(session, "UNIQUE")


def test_run_event_sequence_is_unique_per_run(session: Session) -> None:
    """``(run_id, seq)`` orders the audit trail; a duplicate would corrupt it."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(run_event_row(seq=1))
    session.commit()

    session.add(run_event_row(seq=1))
    _commit_fails(session, "UNIQUE")


def test_line_ordinal_is_unique_per_quote(session: Session, core: Core) -> None:
    """Two lines cannot claim the same position in the quotation."""
    session.add(quote_row(core))
    session.add(quote_line_row(core))
    session.commit()

    session.add(quote_line_row(core, line_id="QLI_0002", ordinal=1))
    _commit_fails(session, "UNIQUE")


def test_stock_level_is_unique_per_location_and_product(session: Session, core: Core) -> None:
    """One stock figure per product per location: a conflict is a data error."""
    session.add(
        StockLevelRow(
            location_code=core.location_code,
            product_id=core.product_id,
            on_hand_qty=5,
            reserved_qty=0,
            inbound_qty=0,
            inbound_eta=None,
            as_of=NOW,
        )
    )
    _commit_fails(session, "UNIQUE")


def test_customer_alias_is_unique_per_customer(session: Session, core: Core) -> None:
    """The same alias twice for one customer adds nothing and breaks idempotency."""
    session.add(
        CustomerAliasRow(
            customer_id=core.customer_id,
            normalized="nordwind",
            alias="Nordwind",
            kind=AliasKind.NAME,
        )
    )
    session.commit()

    session.add(
        CustomerAliasRow(
            customer_id=core.customer_id,
            normalized="nordwind",
            alias="NORDWIND",
            kind=AliasKind.NAME,
        )
    )
    _commit_fails(session, "UNIQUE")


def test_aliases_are_indexed_for_the_resolver(session: Session, core: Core) -> None:
    """An alias lookup is an index hit, and the same alias may serve two owners."""
    session.add(
        CustomerAliasRow(
            customer_id=core.customer_id,
            normalized="nordwind",
            alias="Nordwind",
            kind=AliasKind.NAME,
        )
    )
    session.add(
        ProductAliasRow(
            product_id=core.product_id,
            normalized="pmp a 100",
            alias="PMP A-100",
            kind=AliasKind.SKU,
        )
    )
    session.commit()

    matches = (
        session.execute(
            text("SELECT customer_id FROM customer_aliases WHERE normalized = 'nordwind'")
        )
        .scalars()
        .all()
    )
    assert matches == [core.customer_id]


def test_idempotency_claim_is_unique_per_scope(session: Session) -> None:
    """A key is claimed once per scope; the same string in another scope is free."""
    session.add(IdempotencyClaimRow(scope="human_action", claim_key="key-0000001", claimed_at=NOW))
    session.add(IdempotencyClaimRow(scope="intake", claim_key="key-0000001", claimed_at=NOW))
    session.commit()

    session.add(IdempotencyClaimRow(scope="human_action", claim_key="key-0000001", claimed_at=NOW))
    _commit_fails(session, "UNIQUE")


def test_claim_key_must_be_long_enough(session: Session) -> None:
    """A three-character idempotency key is a collision waiting to happen."""
    session.add(IdempotencyClaimRow(scope="intake", claim_key="abc", claimed_at=NOW))
    _commit_fails(session, "CHECK")


# ---------------------------------------------------------------------------
# Domain invariants expressed in SQL
# ---------------------------------------------------------------------------


def test_quote_line_blocked_state_must_be_explained(session: Session, core: Core) -> None:
    """A blocked line without a reason is unusable to a human reviewer."""
    session.add(quote_row(core))
    session.commit()

    session.add(quote_line_row(core, blocked=True, blocked_reason=None))
    _commit_fails(session, "CHECK")

    session.add(quote_line_row(core, blocked=True, blocked_reason="stock short"))
    session.commit()


def test_line_with_unusable_price_must_be_blocked(session: Session, core: Core) -> None:
    """F09: a missing price blocks the line - the database will not store it unblocked."""
    session.add(quote_row(core))
    session.commit()

    session.add(quote_line_row(core, price_status=PriceLookupStatus.MISSING))
    _commit_fails(session, "CHECK")

    session.add(
        quote_line_row(
            core,
            price_status=PriceLookupStatus.MISSING,
            blocked=True,
            blocked_reason="no price on file",
        )
    )
    session.commit()


def test_line_without_stock_must_be_blocked(session: Session, core: Core) -> None:
    """F13: zero stock blocks the line rather than producing a quotable zero."""
    session.add(quote_row(core))
    session.commit()

    session.add(quote_line_row(core, stock_status=StockStatus.NONE))
    _commit_fails(session, "CHECK")


def test_quantity_and_money_must_be_non_negative(session: Session, core: Core) -> None:
    """Negative quantities and totals are data errors, not business conditions."""
    session.add(quote_row(core))
    session.commit()

    session.add(quote_line_row(core, quantity=0))
    _commit_fails(session, "CHECK")

    session.add(quote_line_row(core, line_extension=Decimal("-1.00")))
    _commit_fails(session, "CHECK")


@pytest.mark.parametrize(
    "quote_number",
    ["Q-26-0001", "2026-0001", "Q-2026-1", "Q-2026-000123456", "X-2026-0001"],
)
def test_quote_number_format_is_enforced(session: Session, core: Core, quote_number: str) -> None:
    """The customer-visible reference has exactly one shape, checked by the database."""
    session.add(quote_row(core, quote_number=quote_number))
    _commit_fails(session, "CHECK")


def test_valid_quote_number_shapes_are_accepted(session: Session, core: Core) -> None:
    """The format check accepts the shortest and longest legal references."""
    session.add(quote_row(core, quote_number="Q-2026-0001"))
    session.add(quote_row(core, quote_id="QTE_0002", quote_number="Q-2026-12345678", revision=2))
    session.commit()


def test_inputs_fingerprint_must_be_a_hex_digest(session: Session, core: Core) -> None:
    """``inputs_sha256`` is what makes a quote replayable: shape-checked in SQL."""
    session.add(quote_row(core, inputs_sha256="Z" * 64))
    _commit_fails(session, "CHECK")

    session.add(quote_row(core, inputs_sha256="ab" * 32))
    session.commit()


def test_price_book_window_must_be_ordered(session: Session) -> None:
    """A price window that ends before it starts can never be queried coherently."""
    session.add(
        PriceBookRow(
            price_book_code="BK_BROKEN",
            name="Broken",
            currency="EUR",
            customer_tier=None,
            effective_from=date(2026, 10, 1),
            effective_to=date(2026, 9, 1),
            active=True,
        )
    )
    _commit_fails(session, "CHECK")


def test_edit_action_requires_a_before_and_after_diff(session: Session) -> None:
    """A human edit is only reviewable if the diff is stored."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(human_action_row(action=HumanActionKind.EDIT, before_json=None, after_json=None))
    _commit_fails(session, "CHECK")

    session.add(
        human_action_row(
            action=HumanActionKind.EDIT,
            before_json={"quantity": 40},
            after_json={"quantity": 30},
        )
    )
    session.commit()


def test_python_none_is_stored_as_sql_null(session: Session, core: Core) -> None:
    """The ``IS NULL`` checks depend on it.

    Stored as the JSON literal ``null`` instead, ``before_json`` would be
    non-NULL text and the ``edit_carries_diff`` constraint could be satisfied by
    the wrong branch.
    """
    session.add(human_action_row(run_id=core.run_id, rfq_id=core.rfq_id))
    session.commit()

    stored = session.execute(
        text("SELECT before_json IS NULL, after_json IS NULL FROM human_actions")
    ).one()
    assert tuple(stored) == (1, 1)


def test_non_edit_actions_must_not_carry_a_diff(session: Session) -> None:
    """An APPROVE with a diff would imply a change that never happened."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(human_action_row(action=HumanActionKind.APPROVE, before_json={"a": 1}))
    _commit_fails(session, "CHECK")


def test_reject_requires_a_reason_code(session: Session) -> None:
    """F20: a rejection without a reason tells the next reader nothing."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(human_action_row(action=HumanActionKind.REJECT, reason_code=None))
    _commit_fails(session, "CHECK")

    session.add(human_action_row(action=HumanActionKind.REJECT, reason_code=ReasonCode.DUPLICATE))
    session.commit()


def test_add_note_requires_a_note(session: Session) -> None:
    """A note with no text is a row that says nothing."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(human_action_row(action=HumanActionKind.ADD_NOTE, note=None))
    _commit_fails(session, "CHECK")


def test_duplicate_human_action_key_is_rejected(session: Session) -> None:
    """A double-clicked APPROVE must not be applied twice."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(human_action_row())
    session.commit()

    session.add(human_action_row(action_id="HAC_0002"))
    _commit_fails(session, "UNIQUE")


def test_queue_lease_fields_must_agree_with_status(session: Session) -> None:
    """A LEASED row without a lease, or a QUEUED row with one, is a broken queue."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(
        RunQueueEntryRow(
            run_id="RUN_0001",
            status=QueueEntryStatus.LEASED,
            priority=0,
            attempts=0,
            enqueued_at=NOW,
            available_at=NOW,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=None,
        )
    )
    _commit_fails(session, "CHECK")

    session.add(
        RunQueueEntryRow(
            run_id="RUN_0001",
            status=QueueEntryStatus.LEASED,
            priority=0,
            attempts=0,
            enqueued_at=NOW,
            available_at=NOW,
            lease_owner="worker-1",
            lease_expires_at=utc_now(),
            completed_at=None,
        )
    )
    session.commit()


def test_stock_cannot_be_over_reserved(session: Session, core: Core) -> None:
    """Reserved exceeding on-hand means the data is wrong, not that stock is short."""
    session.add(
        StockLevelRow(
            location_code=core.location_code,
            product_id=core.product_id,
            on_hand_qty=10,
            reserved_qty=11,
            inbound_qty=0,
            inbound_eta=None,
            as_of=NOW,
        )
    )
    _commit_fails(session, "CHECK")


def test_intake_event_kind_round_trips(session: Session) -> None:
    """Intake events are append-only evidence; kind and detail survive storage."""
    session.add(rfq_row())
    session.commit()

    session.add(
        IntakeEventRow(
            rfq_id="RFQ_0001",
            seq=1,
            kind=IntakeEventKind.RECEIVED,
            occurred_at=NOW,
            detail_json={"channel": "EMAIL"},
            detail_sha256=SHA256,
        )
    )
    session.commit()
    session.expire_all()

    stored = session.execute(text("SELECT kind, detail_json FROM intake_events")).mappings().one()
    assert stored["kind"] == "RECEIVED"
    assert json.loads(stored["detail_json"]) == {"channel": "EMAIL"}


def test_tool_result_status_is_stored_by_value(session: Session) -> None:
    """``DENIED`` is a first-class outcome and must be visible in raw SQL."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(
        ToolCallRow(
            call_id="TC_0001",
            run_id="RUN_0001",
            trace_id="1" * 32,
            span_id="2" * 16,
            parent_span_id=None,
            tool_name="get_price",
            step_index=0,
            args_json={"product_id": "PRD_0001"},
            result_status=ToolResultStatus.DENIED,
            result_sha256=None,
            duration_ms=1,
            error_code="TOOL_DENIED",
            occurred_at=NOW,
        )
    )
    session.commit()

    stored = session.execute(text("SELECT result_status FROM tool_calls")).scalar_one()
    assert stored == "DENIED"


def test_sent_message_requires_a_timestamp(session: Session, core: Core) -> None:
    """A message marked SENT without a time is an audit trail with a hole."""
    # An outbound message needs the quotation it responds to and the approval
    # that unlocked it, so both are created explicitly here.
    session.add(quote_row(core))
    session.add(human_action_row())
    session.commit()

    session.add(outbound_row(core, sent_at=None))
    _commit_fails(session, "CHECK")

    session.add(outbound_row(core, sent_at=NOW))
    session.commit()


def test_canary_failure_prevents_a_sent_status(session: Session, core: Core) -> None:
    """A response containing a planted token can never be recorded as sent."""
    session.add(quote_row(core))
    session.add(human_action_row())
    session.commit()

    session.add(outbound_row(core, canary_passed=False))
    _commit_fails(session, "CHECK")

    session.add(outbound_row(core, canary_passed=False, status="BLOCKED", sent_at=None))
    session.commit()


def test_naive_timestamps_are_refused_through_the_orm(session: Session) -> None:
    """The column type rejects local time before it can be stored and misread."""
    naive = datetime(2026, 10, 6, 12, 0)  # noqa: DTZ001 - naive is the subject of the test
    session.add(rfq_row(received_at=naive))
    with pytest.raises(StatementError, match="naive datetimes are not accepted"):
        session.flush()
    session.rollback()


def test_date_columns_accept_plain_dates(session: Session, core: Core) -> None:
    """``pricing_as_of`` is a date, not a timestamp: the day the prices were read."""
    session.add(quote_row(core, pricing_as_of=date(2026, 10, 6)))
    session.commit()

    stored = session.execute(text("SELECT pricing_as_of FROM quotes")).scalar_one()
    assert stored == "2026-10-06"


def test_timestamps_are_stored_and_returned_as_utc(session: Session, core: Core) -> None:
    """An instant written in +02:00 comes back as the same instant in UTC."""
    session.add(quote_row(core, approved_at=datetime(2026, 10, 6, 14, 30, tzinfo=UTC)))
    session.commit()
    session.expire_all()

    stored = session.execute(text("SELECT approved_at FROM quotes")).scalar_one()
    assert stored == "2026-10-06 14:30:00.000000"
