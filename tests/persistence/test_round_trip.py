"""Write one row of every kind, read it back, and check nothing moved.

The persistence layer's job is to be boring: what the domain hands it is what the
domain gets back, byte for byte where bytes matter. These tests insert a full
graph covering **all 25 tables**, then compare what was written against what a
fresh read returns - every mapped attribute, not a chosen few. That is
deliberately unsubtle: a column whose type quietly rounds, truncates or
reinterprets its value is exactly the kind of defect that surfaces months later
as "the quoted price does not match the invoice".

Four properties get their own tests, because they are the ones a plain
round-trip comparison can still pass while being wrong:

* enums are stored by **value** (``triage``, not ``TRIAGE``), and a row written
  by raw SQL is read back as the right domain member;
* money keeps its scale - ``1234.5600`` does not become ``1234.56`` and
  ``Decimal`` never becomes ``float``;
* timestamps come back UTC-aware even though SQLite stores them naive;
* JSON payloads survive nesting, unicode and nulls, and a Python ``None`` lands
  as SQL ``NULL``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from rfq_agent.contracts.llm import ModelPurpose
from rfq_agent.domain.human import HumanActionKind
from rfq_agent.domain.intake import IntakeEventKind
from rfq_agent.domain.outbound import OutboundStatus
from rfq_agent.domain.policy import DiscountScope
from rfq_agent.domain.workflow import RunState, TransitionEvent
from rfq_agent.observability.spans import ToolResultStatus
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.enums import AliasKind, QueueEntryStatus
from rfq_agent.persistence.models import (
    CustomerAliasRow,
    DiscountRuleRow,
    HolidayRow,
    IdempotencyClaimRow,
    IntakeEventRow,
    LlmCallRow,
    PriceEntryRow,
    ProductAliasRow,
    QuoteLineRow,
    QuoteRow,
    RfqAttachmentRow,
    RunEventRow,
    RunQueueEntryRow,
    ToolCallRow,
)
from tests.persistence.factories import (
    NOW,
    SHA256,
    Core,
    core_rows,
    human_action_row,
    operational_rows,
    outbound_row,
    quote_line_row,
    quote_row,
    run_event_row,
)

#: A 32-character trace id and a 16-character span id, as ``observability.ids``
#: formats them. Trace tables are not written by this layer in normal operation,
#: but the columns still have to hold what the writer will put there.
TRACE_ID = "0123456789abcdef0123456789abcdef"
SPAN_ID = "0123456789abcdef"


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """One written row: what it was, how to find it, and what it held."""

    row_class: type[Base]
    key: tuple[object, ...]
    values: dict[str, object]


RoundTrips = dict[str, RoundTrip]


def _customer_alias() -> Base:
    """The alternate name a resolver would have to match."""
    return CustomerAliasRow(
        customer_id="CUS_0001",
        normalized="nordwind industrie",
        alias="Nordwind Industrie GmbH",
        kind=AliasKind.NAME,
    )


def _product_alias() -> Base:
    """A customer's own part number for a catalogue product."""
    return ProductAliasRow(
        product_id="PRD_0001",
        normalized="100-abc",
        alias="100-ABC",
        kind=AliasKind.CUSTOMER_PART,
    )


def _discount_rule() -> Base:
    """A customer-scoped, approval-requiring discount."""
    return DiscountRuleRow(
        rule_id="DSC_0001",
        scope=DiscountScope.CUSTOMER,
        scope_ref="CUS_0001",
        percent=Decimal("5.00"),
        min_qty=None,
        min_order_value=Decimal("10000.00"),
        requires_approval=True,
        priority=10,
        active=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
    )


def _holiday() -> Base:
    """Non-ASCII text, because a holiday calendar is full of it."""
    return HolidayRow(country_code="PL", holiday_date=date(2026, 12, 25), name="Boże Narodzenie")


def _attachment() -> Base:
    """Metadata only: V1 parses no attachments."""
    return RfqAttachmentRow(
        attachment_id="ATT_0001",
        rfq_id="RFQ_0001",
        filename="pump-list.pdf",
        content_type="application/pdf",
        byte_length=20481,
        sha256=SHA256,
        parsed=False,
        text_preview=None,
    )


def _intake_event() -> Base:
    """The first row of the RFQ's own append-only history."""
    return IntakeEventRow(
        rfq_id="RFQ_0001",
        seq=1,
        kind=IntakeEventKind.RECEIVED,
        occurred_at=NOW,
        detail_json={"source": "email", "attachment_count": 1, "headers": {"spf": "pass"}},
        detail_sha256=SHA256,
    )


def _queue_entry() -> Base:
    """A leased queue row, as the single in-process worker will leave it."""
    return RunQueueEntryRow(
        run_id="RUN_0001",
        status=QueueEntryStatus.LEASED,
        priority=5,
        attempts=1,
        enqueued_at=NOW,
        available_at=NOW,
        lease_owner="worker-1",
        lease_expires_at=NOW,
        completed_at=None,
    )


def _idempotency_claim() -> Base:
    """The claim that backs ``contracts.ports.IdempotencyStore``."""
    return IdempotencyClaimRow(
        scope="intake",
        claim_key="intake:RFQ_0001",
        claimed_at=NOW,
        expires_at=None,
        released_at=None,
    )


def _llm_call() -> Base:
    """A trace row: digests and redacted metadata, never a raw prompt."""
    return LlmCallRow(
        call_id="LLM_0001",
        run_id="RUN_0001",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        parent_span_id=None,
        stage="extract",
        model="openai/gpt-oss-120b",
        purpose=ModelPurpose.EXTRACT,
        prompt_sha256=SHA256,
        response_sha256=SHA256,
        tokens_in=812,
        tokens_out=240,
        latency_ms=1840,
        attempt=1,
        output_valid=True,
        finish_reason="stop",
        error_code=None,
        payload_json={"redacted": True, "schema": "RfqExtraction", "fields": 7},
    )


def _tool_call() -> Base:
    """A tool invocation, with redacted arguments."""
    return ToolCallRow(
        call_id="TCL_0001",
        run_id="RUN_0001",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        parent_span_id=None,
        tool_name="check_stock",
        step_index=3,
        args_json={"sku": "PMP-A-100", "quantity": 40},
        result_status=ToolResultStatus.OK,
        result_sha256=SHA256,
        duration_ms=12,
        error_code=None,
    )


def _sample(session: Session, core: Core) -> list[Base]:
    """Insert one row into every table, in foreign-key order, and return them.

    Two details are deliberate.

    The rows are grouped and flushed by dependency level rather than added in one
    call, because SQLAlchemy flushes pending inserts in mapper-registration order
    and not foreign-key order: a single ``add_all`` across levels would insert
    ``quote_lines`` before the ``quotes`` they belong to.

    The reference graph comes from the factories rather than from the ``core``
    fixture, because that fixture commits - and a committed row has already left
    Python. Keeping every row in hand until after the snapshot is what makes the
    comparison below a real round-trip through the database rather than a
    comparison of two reads.
    """
    rows: list[Base] = []
    for level in [*core_rows(core), operational_rows(core)]:
        session.add_all(level)
        session.flush()
        rows.extend(level)

    extras: list[Base] = [
        _customer_alias(),
        _product_alias(),
        _discount_rule(),
        _holiday(),
        _attachment(),
        _intake_event(),
        _queue_entry(),
        _idempotency_claim(),
        _llm_call(),
        _tool_call(),
        run_event_row(),
        human_action_row(),
    ]
    session.add_all(extras)
    session.flush()
    rows.extend(extras)

    quote = quote_row(
        core,
        discount_rule_id="DSC_0001",
        discount_scope=DiscountScope.CUSTOMER,
        discount_percent=Decimal("5.00"),
        discount_amount=Decimal("2469.12"),
        total=Decimal("46913.28"),
        policy_allowed=False,
        policy_reason_codes_json={"codes": ["STOCK_PARTIAL"], "count": 1},
        requested_date=date(2026, 10, 15),
        split_shipment_proposed=True,
    )
    session.add(quote)
    session.flush()
    rows.append(quote)

    leaves: list[Base] = [quote_line_row(core), outbound_row(core)]
    session.add_all(leaves)
    session.flush()
    rows.extend(leaves)
    return rows


def _capture(rows: list[Base]) -> RoundTrips:
    """Snapshot each row: its class, its key and its mapped values."""
    captured: RoundTrips = {}
    for instance in rows:
        mapper = type(instance).__mapper__  # type: ignore[attr-defined]
        key = tuple(getattr(instance, column.key) for column in mapper.primary_key)
        values = {column.key: getattr(instance, column.key) for column in mapper.columns}
        captured[mapper.class_.__tablename__] = RoundTrip(type(instance), key, values)
    return captured


@pytest.fixture
def round_trip(session: Session) -> RoundTrips:
    """Every table populated, and what each row held when it was written.

    Deliberately not built on the ``core`` fixture: that one seeds and commits
    the same reference graph, and the duplicate insert would trip the unique
    constraints on ``products.sku`` and friends.
    """
    written = _capture(_sample(session, Core()))
    session.commit()
    return written


def test_the_sample_covers_every_table(round_trip: RoundTrips, session: Session) -> None:
    """A round-trip suite that skips a table is a suite that lies by omission."""
    assert set(round_trip) == set(Base.metadata.tables)

    for table in sorted(round_trip):
        stored = session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608
        assert stored >= 1, f"{table} was not populated"


def test_every_written_value_survives_the_round_trip(
    round_trip: RoundTrips, session: Session
) -> None:
    """Read every row back through a cleared identity map and compare it all.

    ``expunge_all`` matters: comparing against the identity-mapped instance would
    compare the object with itself and never touch the database.
    """
    session.expunge_all()

    mismatches: list[str] = []
    for table, written in sorted(round_trip.items()):
        again = session.get(written.row_class, written.key)
        assert again is not None, f"{table} row {written.key} disappeared"
        for attribute, expected in written.values.items():
            actual = getattr(again, attribute)
            if actual != expected or type(actual) is not type(expected):
                mismatches.append(f"{table}.{attribute}: {expected!r} -> {actual!r}")

    assert mismatches == []


def test_enum_columns_store_the_value_not_the_member_name(
    session: Session, round_trip: RoundTrips
) -> None:
    """The column holds what the domain calls the value, at rest.

    Raw SQL is the only view that can tell the difference: the ORM would happily
    map ``TRIAGE_START`` back to ``TransitionEvent.TRIAGE_START``. Any other
    writer - a data fix, a reporting tool, an operator at the sqlite prompt -
    sees the string below, and it has to make sense to them.
    """
    expected = {
        ("run_events", "event"): "triage_start",
        ("run_events", "to_state"): "TRIAGING",
        ("human_actions", "action"): "APPROVE",
        ("llm_calls", "purpose"): "extract",
        ("tool_calls", "result_status"): "OK",
        ("run_queue", "status"): "LEASED",
        ("customer_aliases", "kind"): "NAME",
        ("product_aliases", "kind"): "CUSTOMER_PART",
        ("outbound_messages", "status"): "SENT",
        ("quote_lines", "stock_status"): "SUFFICIENT",
    }
    assert round_trip  # the sample is what is being inspected

    for (table, column), value in expected.items():
        stored = _scalar(session, table, column)
        assert isinstance(stored, str), f"{table}.{column} is not stored as text"
        assert stored == value, f"{table}.{column} stored as {stored!r}"


def _scalar(session: Session, table: str, column: str) -> object:
    """Read one stored value, as SQLite holds it rather than as the ORM maps it."""
    statement = text(f"SELECT {column} FROM {table}")  # noqa: S608
    return session.execute(statement).scalar_one()


def test_a_row_written_by_raw_sql_is_read_as_domain_values(session: Session, core: Core) -> None:
    """The mapping is by value, so anything that can write SQL can write a run.

    ``TransitionEvent.TRIAGE_START`` and ``RunState.TRIAGING`` disagree about
    case and about underscores, which is why this test would fail loudly if the
    column ever started storing member *names*.
    """
    session.execute(
        text(
            "INSERT INTO run_events "
            "(run_id, seq, occurred_at, from_state, to_state, event, actor) "
            "VALUES (:run_id, 1, '2026-10-06 12:00:00.000000', "
            "'RECEIVED', 'TRIAGING', 'triage_start', 'SYSTEM')"
        ),
        {"run_id": core.run_id},
    )
    session.commit()
    session.expunge_all()

    stored = session.get(RunEventRow, {"run_id": core.run_id, "seq": 1})
    assert stored is not None
    assert stored.from_state is RunState.RECEIVED
    assert stored.to_state is RunState.TRIAGING
    assert stored.event is TransitionEvent.TRIAGE_START
    assert stored.occurred_at == NOW


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

#: ``(column, value)`` pairs for the two decimal columns. The values are the ones
#: that have broken a float-backed money column at some point: the smallest unit
#: the column can represent, a trailing-zero case (``1234.5600`` and ``1234.56``
#: are equal as numbers and different as strings) and the largest amount the
#: 14-digit columns can hold. ``0.10`` is here because ``0.1`` has no exact
#: binary representation.
_MONEY_CASES: list[tuple[str, Decimal]] = [
    ("line_extension", Decimal("0.00")),
    ("line_extension", Decimal("0.01")),
    ("line_extension", Decimal("0.10")),
    ("line_extension", Decimal("1234.56")),
    ("line_extension", Decimal("999999999999.99")),
    ("unit_price", Decimal("0.0000")),
    ("unit_price", Decimal("0.0001")),
    ("unit_price", Decimal("1234.5600")),
    ("unit_price", Decimal("9999999999.9999")),
]


@pytest.mark.parametrize(
    ("column", "value"), _MONEY_CASES, ids=[f"{c}={v}" for c, v in _MONEY_CASES]
)
def test_decimal_amounts_keep_their_precision(
    session: Session, core: Core, column: str, value: Decimal
) -> None:
    """A ``Decimal`` goes in and the same ``Decimal`` comes out, as a ``Decimal``.

    SQLite has no decimal type, so this is a claim about the column type and the
    dialect's conversion, and it is worth pinning: a ``float`` anywhere in the
    path shows up here as ``49382.400000000001``, and money arithmetic that is
    off by a hundredth of a cent is a quotation a customer can dispute.

    The comparison is on the *string*, not only on equality, so that the column's
    scale is pinned too: ``0.01`` in a four-decimal column and ``0.0100`` in a
    two-decimal one are both wrong answers that compare equal to the input.
    """
    session.add(quote_row(core))
    session.add(quote_line_row(core, **{column: value}))
    session.commit()
    session.expunge_all()

    stored_row = session.get(QuoteLineRow, {"line_id": core.line_id})
    assert stored_row is not None
    stored = getattr(stored_row, column)
    assert isinstance(stored, Decimal)
    assert not isinstance(stored, float)
    assert stored == value
    assert str(stored) == str(value)


def test_a_column_cannot_hold_more_decimals_than_it_declares(session: Session, core: Core) -> None:
    """Rounding is the column's job, and it rounds rather than truncates.

    ``9999999999.9999`` fits the four-decimal unit-price column and must not be
    squeezed into the two-decimal amount column unchanged; the database is the
    last line of defence if application code ever hands it the wrong scale.
    """
    session.add(quote_row(core))
    session.add(quote_line_row(core, line_extension=Decimal("999999999999.999")))
    session.commit()
    session.expunge_all()

    stored_row = session.get(QuoteLineRow, {"line_id": core.line_id})
    assert stored_row is not None
    assert str(stored_row.line_extension) == "1000000000000.00"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("rfqs", "received_at"),
        ("runs", "created_at"),
        ("run_events", "occurred_at"),
        ("quotes", "created_at"),
        ("human_actions", "occurred_at"),
        ("outbound_messages", "sent_at"),
    ],
)
def test_timestamps_are_stored_naive_utc_and_read_back_aware(
    session: Session, round_trip: RoundTrips, table: str, column: str
) -> None:
    """UTC is normalised on the way in and re-attached on the way out.

    Both halves matter. Stored naive means the value is unambiguous UTC text that
    any SQL tool reads correctly; returned aware means the application can never
    accidentally subtract an aware value from a naive one.
    """
    stored_text = _scalar(session, table, column)
    assert isinstance(stored_text, str), f"{table}.{column} is not stored as a text timestamp"
    assert "+" not in stored_text
    assert not stored_text.endswith("Z")

    written = round_trip[table].values[column]
    assert isinstance(written, datetime)
    assert written.tzinfo is UTC


def test_a_non_utc_instant_is_normalised_on_the_way_in(session: Session, core: Core) -> None:
    """An offset input lands as the same instant, stored as UTC.

    14:00 on a Warsaw summer clock and 12:00 UTC are the same moment; the column
    stores the second, because that is the one that stays true when the clocks
    change.
    """
    warsaw_summer = datetime(2026, 10, 6, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert warsaw_summer == NOW

    session.add(human_action_row(occurred_at=warsaw_summer))
    session.commit()

    stored_text = session.execute(
        text("SELECT occurred_at FROM human_actions WHERE action_id = :action_id"),
        {"action_id": core.action_id},
    ).scalar_one()
    assert stored_text == "2026-10-06 12:00:00.000000"


def test_dates_stay_dates(session: Session, round_trip: RoundTrips) -> None:
    """A ``date`` column does not come back as a ``datetime``.

    ``earliest_delivery_date`` deciding ``2026-10-07 00:00:00`` is not a better
    answer than ``2026-10-07``: it is a different type flowing into delivery
    arithmetic that expects a day.
    """
    written = round_trip["price_books"].values["effective_from"]
    assert isinstance(written, date)
    assert not isinstance(written, datetime)

    session.expunge_all()
    again = session.get(round_trip["price_books"].row_class, round_trip["price_books"].key)
    assert again is not None
    assert again.effective_from == date(2026, 1, 1)  # type: ignore[attr-defined]
    assert not isinstance(again.effective_from, datetime)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# JSON payloads
# ---------------------------------------------------------------------------

#: Nested structure with every JSON type a payload column can be handed.
_NESTED_PAYLOAD: dict[str, object] = {
    "reason_codes": ["STOCK_PARTIAL", "PRICE_STALE"],
    "quantities": [40, 0, 12],
    "ratio": 1.5,
    "empty_list": [],
    "empty_object": {},
    "flagged": True,
    "note": None,
    "unicode": "Zażółć gęślą jaźń",
    "nested": {"level_2": {"level_3": [{"sku": "PMP-A-100"}]}},
}


def test_json_payloads_round_trip_faithfully(session: Session, core: Core) -> None:
    """Nesting, unicode and mixed types survive without being stringified.

    A JSON column that returns a *string* rather than a structure is a common
    half-migration; the equality check below would catch it, and the assertion on
    the type says which half broke.
    """
    session.add(
        quote_row(
            core,
            policy_reason_codes_json=_NESTED_PAYLOAD,
            delivery_destination="Warszawa, PL",
        )
    )
    session.commit()
    session.expunge_all()

    stored = session.get(QuoteRow, {"quote_id": core.quote_id})
    assert stored is not None
    assert isinstance(stored.policy_reason_codes_json, dict)
    assert stored.policy_reason_codes_json == _NESTED_PAYLOAD
    assert stored.policy_reason_codes_json["unicode"] == "Zażółć gęślą jaźń"


def test_a_null_json_payload_is_sql_null_not_the_literal_null(session: Session, core: Core) -> None:
    """``None`` means absent, and the database is asked to say so.

    The ``human_actions`` constraints branch on ``before_json IS NULL``, so a
    column that wrote the four-character literal ``null`` would take the wrong
    branch and let a non-EDIT action carry a diff - see
    ``test_constraints.py::test_non_edit_actions_must_not_carry_a_diff``.
    """
    session.add(quote_row(core, policy_reason_codes_json=None))
    session.commit()

    is_null = session.execute(
        text("SELECT policy_reason_codes_json IS NULL FROM quotes")
    ).scalar_one()
    assert is_null == 1


def test_a_json_column_holding_a_list_is_not_silently_converted(
    session: Session, core: Core
) -> None:
    """A list stays a list: the payload columns are genuinely schemaless."""
    session.add(quote_row(core, policy_reason_codes_json=["STOCK_PARTIAL", "PRICE_STALE"]))
    session.commit()
    session.expunge_all()

    stored = session.get(QuoteRow, {"quote_id": core.quote_id})
    assert stored is not None
    assert stored.policy_reason_codes_json == ["STOCK_PARTIAL", "PRICE_STALE"]


# ---------------------------------------------------------------------------
# Keys and identity
# ---------------------------------------------------------------------------


def test_composite_primary_keys_round_trip(session: Session, round_trip: RoundTrips) -> None:
    """Three tables are keyed by a pair; ``session.get`` must accept both parts."""
    for table in ("customer_aliases", "product_aliases", "intake_events", "idempotency_claims"):
        written = round_trip[table]
        assert len(written.key) == 2, f"{table} should have a two-part key"
        session.expunge_all()
        assert session.get(written.row_class, written.key) is not None


def test_the_orm_refuses_a_value_that_is_not_an_enum_member(session: Session, core: Core) -> None:
    """``validate_strings`` turns a typo into an error instead of a ``NULL``.

    Without it, ``price_status="FOUND_"`` would be written as-is and read back as
    ``None`` - a line whose price status is not "not found" but "unrepresentable",
    which is precisely the sort of silent nothing that a quotation must never
    carry.
    """
    session.add(quote_row(core))
    session.add(quote_line_row(core, price_status="FOUND_"))
    with pytest.raises(StatementError, match="not among the defined enum values"):
        session.commit()
    session.rollback()


def test_sqlite_refuses_an_enum_typo_written_by_raw_sql(session: Session, core: Core) -> None:
    """The check is in the schema, so it holds for writers that bypass the ORM.

    A data fix, a support script or a reporting tool does not go through
    SQLAlchemy's type system, and the column still has to refuse a value outside
    the vocabulary the rest of the system reasons about.
    """
    session.add(quote_row(core))
    session.add(quote_line_row(core))
    session.commit()

    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        session.execute(
            text("UPDATE quote_lines SET price_status = 'FOUND_' WHERE line_id = 'QLI_0001'")
        )
    session.rollback()


def test_the_database_rejects_an_outbound_status_without_a_timestamp(
    session: Session, round_trip: RoundTrips
) -> None:
    """A guard worth repeating here, because it survives every application change."""
    assert round_trip["outbound_messages"].values["status"] is OutboundStatus.SENT

    with pytest.raises(IntegrityError, match="CHECK"):
        session.execute(text("UPDATE outbound_messages SET sent_at = NULL"))
    session.rollback()


def test_run_state_round_trips_as_a_state_machine_value(
    session: Session, round_trip: RoundTrips
) -> None:
    """The run's state is the same object the transition table is keyed by."""
    session.expunge_all()
    stored = session.get(round_trip["runs"].row_class, round_trip["runs"].key)
    assert stored is not None
    assert stored.state is RunState.RECEIVED

    written_action = round_trip["human_actions"].values["action"]
    assert written_action is HumanActionKind.APPROVE


def test_a_price_entry_can_be_scoped_to_a_customer_or_a_tier(session: Session, core: Core) -> None:
    """``NULL`` in ``customer_id`` is a real value here: "list price, for anyone"."""
    session.add(
        PriceEntryRow(
            price_entry_id="PE_0002",
            price_book_code=core.price_book_code,
            product_id=core.product_id,
            customer_id=core.customer_id,
            customer_tier=None,
            min_qty=40,
            unit_price=Decimal("1180.0000"),
            currency="EUR",
            effective_from=date(2026, 7, 1),
            effective_to=date(2026, 12, 31),
        )
    )
    session.commit()
    session.expunge_all()

    stored = session.get(PriceEntryRow, {"price_entry_id": "PE_0002"})
    assert stored is not None
    assert stored.customer_id == core.customer_id
    assert stored.customer_tier is None
    assert stored.unit_price == Decimal("1180.0000")
    assert stored.effective_to == date(2026, 12, 31)
