"""The production intake path: stored rows, replayed identities and refusals.

These tests check the database, not the report: what was written, what was
reused, and what the refusal left untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update

from rfq_agent.domain.intake import IntakeEventKind, RfqStatus, SourceChannel
from rfq_agent.domain.values import sha256_text
from rfq_agent.domain.workflow import RunState
from rfq_agent.intake import (
    IntakeConflictError,
    IntakeConflictKind,
    IntakeDeclaration,
    IntakeIntegrityError,
    IntakePrerequisiteError,
    load_source,
    provision_intake,
    require_prerequisites,
)
from rfq_agent.persistence.models import (
    IntakeEventRow,
    RfqAttachmentRow,
    RfqRow,
    RunRow,
)
from tests.acceptance.conftest import make_declaration


def _events(session, rfq_id: str) -> list[IntakeEventRow]:
    return list(
        session.scalars(
            select(IntakeEventRow)
            .where(IntakeEventRow.rfq_id == rfq_id)
            .order_by(IntakeEventRow.seq)
        )
    )


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_provision_stores_the_rfq_its_run_and_one_received_event(seeded, session) -> None:
    declaration = make_declaration().intake

    with seeded.session() as work:
        stored = provision_intake(work, declaration)

    assert stored.created is True
    assert (stored.rfq_id, stored.run_id) == ("RFQ_TEST_0001", "RUN_TEST_0001")
    session.expire_all()

    rfq = session.get(RfqRow, "RFQ_TEST_0001")
    assert rfq is not None
    assert rfq.source_channel is SourceChannel.EMAIL
    assert rfq.status is RfqStatus.OPEN
    assert rfq.body_text == declaration.body
    assert rfq.body_sha256 == sha256_text(declaration.body)
    assert rfq.sender_email == declaration.sender_email
    assert rfq.sender_email_hash == sha256_text(declaration.sender_email)
    assert rfq.idempotency_key == declaration.idempotency_key
    assert rfq.thread_key is None
    assert rfq.superseded_by_rfq_id is None

    run = session.get(RunRow, "RUN_TEST_0001")
    assert run is not None
    assert run.rfq_id == "RFQ_TEST_0001"
    assert run.attempt_no == 1
    assert run.state is RunState.RECEIVED
    assert run.outcome is None
    assert run.row_version == 0

    events = _events(session, "RFQ_TEST_0001")
    assert [event.kind for event in events] == [IntakeEventKind.RECEIVED]
    assert events[0].seq == 1
    assert events[0].occurred_at == declaration.received_at


def test_provision_reuses_identical_rows_and_writes_nothing_on_a_replay(seeded, session) -> None:
    declaration = make_declaration().intake

    with seeded.session() as work:
        first = provision_intake(work, declaration)
    with seeded.session() as work:
        second = provision_intake(work, declaration)

    assert first.created is True
    assert second.created is False
    session.expire_all()
    assert _count(session, RfqRow) == 1
    assert _count(session, RunRow) == 1
    assert _count(session, IntakeEventRow) == 1


def test_different_content_under_a_stored_identity_is_refused(seeded, session) -> None:
    declaration = make_declaration().intake
    with seeded.session() as work:
        provision_intake(work, declaration)

    changed = make_declaration(body=declaration.body + "\nOne more line.").intake
    with pytest.raises(IntakeConflictError) as raised, seeded.session() as work:
        provision_intake(work, changed)

    assert raised.value.kind is IntakeConflictKind.CONTENT_DIFFERS
    assert "body_text" in str(raised.value)
    session.expire_all()
    assert session.get(RfqRow, "RFQ_TEST_0001").body_text == declaration.body
    assert _count(session, IntakeEventRow) == 1


def test_an_idempotency_key_cannot_name_a_second_rfq(seeded, session) -> None:
    first = make_declaration(number=1).intake
    with seeded.session() as work:
        provision_intake(work, first)

    second = make_declaration(
        number=2,
        body=first.body.replace("25 pcs", "30 pcs").replace("RFQ: 25", "RFQ: 30"),
        idempotency_key=first.idempotency_key,
    ).intake
    with pytest.raises(IntakeConflictError) as raised, seeded.session() as work:
        provision_intake(work, second)

    assert raised.value.kind is IntakeConflictKind.KEY_REUSED
    session.expire_all()
    assert session.get(RfqRow, "RFQ_TEST_0002") is None
    assert _count(session, RfqRow) == 1


def test_a_run_that_has_moved_on_is_not_rejoined(seeded, session) -> None:
    declaration = make_declaration().intake
    with seeded.session() as work:
        provision_intake(work, declaration)
    with seeded.session() as work:
        work.execute(
            update(RunRow).where(RunRow.run_id == "RUN_TEST_0001").values(state=RunState.TRIAGING)
        )

    with pytest.raises(IntakeConflictError) as raised, seeded.session() as work:
        provision_intake(work, declaration)

    assert raised.value.kind is IntakeConflictKind.LIFECYCLE_MOVED
    session.expire_all()
    assert session.get(RunRow, "RUN_TEST_0001").state is RunState.TRIAGING


def test_a_run_identifier_cannot_belong_to_another_rfq(seeded, session) -> None:
    with seeded.session() as work:
        provision_intake(work, make_declaration(number=1).intake)

    clash = make_declaration(number=2).intake.model_copy(update={"run_id": "RUN_TEST_0001"})
    with pytest.raises(IntakeConflictError) as raised, seeded.session() as work:
        provision_intake(work, clash)

    assert raised.value.kind is IntakeConflictKind.LIFECYCLE_MOVED
    session.expire_all()
    assert session.get(RfqRow, "RFQ_TEST_0002") is None


def test_the_fixture_channel_is_refused() -> None:
    with pytest.raises(ValidationError):
        IntakeDeclaration(
            rfq_id="RFQ_TEST_0001",
            run_id="RUN_TEST_0001",
            idempotency_key="acceptance:test:0001",
            source_channel=SourceChannel.FIXTURE,
            received_at=datetime(2026, 10, 6, 9, 0, tzinfo=UTC),
            body="Hello.",
            sender_email="einkauf@nordwind-industrie.de",
        )


def test_a_naive_arrival_instant_is_refused() -> None:
    with pytest.raises(ValidationError):
        IntakeDeclaration(
            rfq_id="RFQ_TEST_0001",
            run_id="RUN_TEST_0001",
            idempotency_key="acceptance:test:0001",
            source_channel=SourceChannel.EMAIL,
            # A naive instant is exactly what this test is about.
            received_at=datetime(2026, 10, 6, 9, 0),  # noqa: DTZ001
            body="Hello.",
            sender_email="einkauf@nordwind-industrie.de",
        )


def test_load_source_rebuilds_the_stored_snapshot(seeded) -> None:
    declaration = make_declaration().intake
    with seeded.session() as work:
        provision_intake(work, declaration)

    with seeded.session() as work:
        source = load_source(work, "RFQ_TEST_0001")

    assert source.body.text == declaration.body
    assert source.body.sha256 == sha256_text(declaration.body)
    assert source.body.truncated is False
    assert source.sender_email == declaration.sender_email
    assert source.sender_email_hash == sha256_text(declaration.sender_email)
    assert source.sender_name == declaration.sender_name
    assert source.subject == declaration.subject
    assert source.received_at == declaration.received_at
    assert source.source_channel is SourceChannel.EMAIL
    assert source.idempotency_key == declaration.idempotency_key


def test_load_source_refuses_a_body_whose_digest_no_longer_matches(seeded) -> None:
    declaration = make_declaration().intake
    with seeded.session() as work:
        provision_intake(work, declaration)
    with seeded.session() as work:
        work.execute(
            update(RfqRow)
            .where(RfqRow.rfq_id == "RFQ_TEST_0001")
            .values(body_text="please quote 999 pcs of 100-ABC.")
        )

    with seeded.session() as work, pytest.raises(IntakeIntegrityError):
        load_source(work, "RFQ_TEST_0001")


def test_load_source_refuses_an_rfq_it_cannot_reconstruct_completely(seeded) -> None:
    declaration = make_declaration().intake
    with seeded.session() as work:
        provision_intake(work, declaration)
    with seeded.session() as work:
        work.add(
            RfqAttachmentRow(
                attachment_id="ATT_TEST_0001",
                rfq_id="RFQ_TEST_0001",
                filename="specification.pdf",
            )
        )

    with seeded.session() as work, pytest.raises(IntakeIntegrityError):
        load_source(work, "RFQ_TEST_0001")


def test_load_source_refuses_an_unknown_identity(seeded) -> None:
    with seeded.session() as work, pytest.raises(IntakeIntegrityError):
        load_source(work, "RFQ_NOT_STORED")


def test_prerequisites_name_the_missing_dataset(session) -> None:
    with pytest.raises(IntakePrerequisiteError) as raised:
        require_prerequisites(session)

    assert "make seed" in str(raised.value)
