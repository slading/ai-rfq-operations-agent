"""The database-level guarantees: append-only audit rows, human-gated sending.

Every test here uses raw SQL rather than the ORM. That is the point: the claims
being tested are "no update or delete path exists anywhere in the codebase" and
"only an approval can unlock a send". A test that went through the ORM would only
show what the ORM happens to do; issuing ``UPDATE`` and ``DELETE`` directly shows
what the *database* permits - which is what protects the system from the code
nobody has written yet.

Note that SQLite triggers fire per row, so each test writes the row it then tries
to rewrite. A trigger that never fires on an empty table proves nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rfq_agent.domain.human import HumanActionKind
from rfq_agent.domain.intake import IntakeEventKind
from rfq_agent.domain.outbound import OutboundStatus
from rfq_agent.domain.policy import BlockedReasonCode
from rfq_agent.domain.workflow import ReasonCode
from rfq_agent.persistence.models import IntakeEventRow, QuoteBlockedReasonRow, RunEventRow
from tests.persistence.factories import (
    NOW,
    Core,
    human_action_row,
    outbound_row,
    quote_row,
    rfq_row,
    run_event_row,
    run_row,
)


def _refuse(session: Session, statement: str, match: str) -> None:
    """Execute ``statement`` and assert the database refuses it.

    DML triggers and cascading deletes fire during execution, so no commit is
    needed for the refusal to surface - and the rollback below leaves the
    session usable for the assertions that follow.
    """
    with pytest.raises(IntegrityError, match=match):
        session.execute(text(statement))
    session.rollback()


def _seed_audit_rows(session: Session) -> None:
    """One row in each append-only table, so the triggers have something to bite."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.add(run_event_row())
    session.add(
        IntakeEventRow(
            rfq_id="RFQ_0001",
            seq=1,
            kind=IntakeEventKind.RECEIVED,
            occurred_at=NOW,
            detail_json=None,
            detail_sha256=None,
        )
    )
    session.add(human_action_row())
    session.commit()


#: ``(table, statement)`` pairs - one update and one delete per append-only table.
_APPEND_ONLY_STATEMENTS = [
    ("run_events", "UPDATE run_events SET to_state = 'SENT' WHERE seq = 1"),
    ("run_events", "DELETE FROM run_events"),
    ("intake_events", "UPDATE intake_events SET kind = 'ATTACHMENT_REJECTED' WHERE seq = 1"),
    ("intake_events", "DELETE FROM intake_events"),
    ("human_actions", "UPDATE human_actions SET actor = 'someone-else'"),
    ("human_actions", "DELETE FROM human_actions"),
]


def _seed_a_ledger_row(session: Session, core: Core) -> None:
    """A quote and one ledger reason, so that table's triggers have something to bite.

    It needs the ``core`` fixture rather than the audit rows above, because the
    ledger hangs off a quotation - and a quotation needs the reference graph.
    """
    session.add(quote_row(core))
    session.commit()

    session.add(
        QuoteBlockedReasonRow(
            quote_id=core.quote_id,
            seq=1,
            run_id=core.run_id,
            code=BlockedReasonCode.PRICE_MISSING,
            message="line 1 has no usable price",
            line_ordinal=1,
            resolvable_by_human=True,
            flags_json=[],
            created_at=NOW,
        )
    )
    session.commit()


#: The blocking ledger (Phase 1J') is append-only for the same reason: it is the
#: evidence a human acts on, so it may be added to but never rewritten.
_LEDGER_STATEMENTS = [
    (
        "quote_blocked_reasons",
        "UPDATE quote_blocked_reasons SET message = 'rewritten' WHERE seq = 1",
    ),
    ("quote_blocked_reasons", "DELETE FROM quote_blocked_reasons"),
]


@pytest.mark.parametrize(
    ("table", "statement"),
    _LEDGER_STATEMENTS,
    ids=[f"{table}:{statement.split()[0]}" for table, statement in _LEDGER_STATEMENTS],
)
def test_the_blocking_ledger_refuses_updates_and_deletes(
    session: Session, core: Core, table: str, statement: str
) -> None:
    """A reason a reviewer is acting on must not be quietly editable."""
    _seed_a_ledger_row(session, core)

    _refuse(session, statement, f"{table} is append-only")


@pytest.mark.parametrize(
    ("table", "statement"),
    _APPEND_ONLY_STATEMENTS,
    ids=[f"{table}:{statement.split()[0]}" for table, statement in _APPEND_ONLY_STATEMENTS],
)
def test_append_only_tables_refuse_updates_and_deletes(
    session: Session, table: str, statement: str
) -> None:
    """History is rewritten by nobody, including a well-meaning operator."""
    _seed_audit_rows(session)

    _refuse(session, statement, f"{table} is append-only")


def test_the_refusal_names_the_operation_and_the_table(session: Session) -> None:
    """The error is written for the human who hits it at a SQL prompt."""
    _seed_audit_rows(session)

    _refuse(
        session,
        "UPDATE run_events SET to_state = 'SENT' WHERE seq = 1",
        "run_events is append-only: UPDATE is not permitted",
    )

    stored = session.execute(text("SELECT to_state FROM run_events")).scalar_one()
    assert stored == "TRIAGING"


def test_a_run_with_history_cannot_be_deleted(session: Session) -> None:
    """``ON DELETE CASCADE`` meets the append-only trigger, and the trigger wins.

    Deleting the run would cascadially delete its audit rows; the trigger aborts
    the cascade, so erasing a run's history is impossible by any route.
    """
    _seed_audit_rows(session)

    _refuse(session, "DELETE FROM runs WHERE run_id = 'RUN_0001'", "append-only")

    assert session.execute(text("SELECT COUNT(*) FROM runs")).scalar_one() == 1


def test_a_run_without_history_can_be_deleted(session: Session) -> None:
    """The guard is precise: a run that produced no evidence is disposable."""
    session.add(rfq_row())
    session.add(run_row())
    session.commit()

    session.execute(text("DELETE FROM runs WHERE run_id = 'RUN_0001'"))
    session.commit()

    assert session.execute(text("SELECT COUNT(*) FROM runs")).scalar_one() == 0


def test_audit_rows_survive_a_new_connection(session: Session, db: object) -> None:
    """Durability, not just in-transaction visibility."""
    _seed_audit_rows(session)

    factory = db.session_factory  # type: ignore[attr-defined]
    with factory() as fresh:
        stored = fresh.get(RunEventRow, {"run_id": "RUN_0001", "seq": 1})
    assert stored is not None
    assert stored.event.value == "triage_start"


# ---------------------------------------------------------------------------
# Human-gated outbound
# ---------------------------------------------------------------------------


def _quote_and_action(session: Session, core: Core, **action_overrides: object) -> None:
    """Create the quotation and one human action for an outbound test."""
    session.add(quote_row(core))
    session.add(human_action_row(**action_overrides))
    session.commit()


def test_a_message_cannot_be_written_without_a_human_action(session: Session, core: Core) -> None:
    """ "A human approved this" is structural: a foreign key *and* a trigger.

    Both guards apply here. The trigger fires first, so the message the operator
    sees names the actual rule rather than reporting a constraint violation.
    """
    session.add(quote_row(core))
    session.commit()

    session.add(outbound_row(core, approval_action_id="HAC_MISSING"))
    with pytest.raises(IntegrityError, match="requires a recorded APPROVE"):
        session.commit()
    session.rollback()

    assert session.execute(text("SELECT COUNT(*) FROM outbound_messages")).scalar_one() == 0


def test_a_rejection_cannot_unlock_a_send(session: Session, core: Core) -> None:
    """A REJECT row satisfies the foreign key but must not satisfy the rule."""
    _quote_and_action(
        session, core, action=HumanActionKind.REJECT, reason_code=ReasonCode.DUPLICATE
    )

    session.add(outbound_row(core))
    with pytest.raises(IntegrityError, match="requires a recorded APPROVE"):
        session.commit()
    session.rollback()


def test_an_edit_cannot_unlock_a_send(session: Session, core: Core) -> None:
    """An edit is not consent to send."""
    _quote_and_action(
        session,
        core,
        action=HumanActionKind.EDIT,
        before_json={"quantity": 40},
        after_json={"quantity": 30},
    )

    session.add(outbound_row(core))
    with pytest.raises(IntegrityError, match="requires a recorded APPROVE"):
        session.commit()
    session.rollback()


def test_an_approval_unlocks_a_send(session: Session, core: Core) -> None:
    """The happy path the trigger exists to protect."""
    _quote_and_action(session, core)

    session.add(outbound_row(core))
    session.commit()

    status = session.execute(text("SELECT status FROM outbound_messages")).scalar_one()
    assert status == OutboundStatus.SENT.value


def test_an_approved_message_cannot_be_repointed_at_a_rejection(
    session: Session, core: Core
) -> None:
    """The guard covers ``UPDATE`` too, so the approval cannot be swapped later."""
    _quote_and_action(session, core)
    session.add(outbound_row(core))
    session.commit()

    session.add(
        human_action_row(
            action_id="HAC_0002",
            action=HumanActionKind.REJECT,
            reason_code=ReasonCode.DUPLICATE,
            idempotency_key="action:reject:0002",
        )
    )
    session.commit()

    _refuse(
        session,
        "UPDATE outbound_messages SET approval_action_id = 'HAC_0002' "
        "WHERE message_id = 'MSG_0001'",
        "requires a recorded APPROVE",
    )

    still_approved = session.execute(
        text("SELECT approval_action_id FROM outbound_messages")
    ).scalar_one()
    assert still_approved == "HAC_0001"


def test_a_draft_still_requires_an_approval_reference(session: Session, core: Core) -> None:
    """The gate is on the approval reference, not on the message status.

    Recording an outbound message *is* the act of sending it in V1, so a
    ``DRAFTED`` row is held to the same rule as a ``SENT`` one. Choosing
    otherwise would mean a draft could carry a forged approval and be flipped to
    ``SENT`` with a single ``UPDATE`` - which the trigger would then not catch,
    because the reference would already be there.
    """
    session.add(quote_row(core))
    session.commit()

    session.add(outbound_row(core, status=OutboundStatus.DRAFTED, sent_at=None))
    with pytest.raises(IntegrityError, match="requires a recorded APPROVE"):
        session.commit()
    session.rollback()

    assert session.execute(text("SELECT COUNT(*) FROM outbound_messages")).scalar_one() == 0
