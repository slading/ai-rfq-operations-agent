"""The resolution core over the demo dataset, up to the draft quote (Phase 1M-B).

One chain, end to end, on data a reviewer can check by hand: the sender's
address is proven against Nordwind's stored ``EMAIL`` alias, the customer part
number resolves to one catalogue item, and the accepted seams do the rest -
``to_quote_request`` assembles the request and ``run_quote`` decides over the
seeded business data. What is checked here is that the chain *connects* and
where it stops: execution completion is not approval, the stored quote stays a
``DRAFT``, and every outcome that needs a human stops at the 1M-A adapter
rather than reaching the engine.

The verdicts themselves belong to their phase tests - the unit tests pin the
ruling rules, ``test_seeded_quote_run.py`` pins the run - and are not
re-argued here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from rfq_agent.domain.extraction import DateResolution, ExtractedLine, RequestedDelivery
from rfq_agent.domain.quote import QuoteStatus
from rfq_agent.domain.resolution import (
    CustomerMatchStatus,
    ResolutionMatchStatus,
    ResolutionSource,
)
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import QuoteRow
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.persistence.writers import QuoteWriter
from rfq_agent.quote_adapter import to_quote_request
from rfq_agent.quoting import QuoteRunStatus, run_quote
from rfq_agent.resolving import bind_customer, resolve_lines
from rfq_agent.seed import reset_and_seed
from tests.persistence.factories import rfq_row, run_row

#: The date every price in these tests is selected for.
AS_OF = date(2026, 10, 6)
#: The stamp the seeded stock positions carry.
STOCK_STAMP = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
#: When the delivery question is asked - before the DHL express cut-off.
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
#: The run every quote in this module belongs to, and its RFQ.
RUN_ID = "RUN_0001"
RFQ_ID = "RFQ_0001"


@pytest.fixture
def seeded(session: Session) -> Session:
    """A committed, freshly seeded database."""
    reset_and_seed(session)
    session.commit()
    return session


@pytest.fixture
def runs(seeded: Session) -> Session:
    """The seeded database plus the run a quote belongs to (and its RFQ)."""
    seeded.add(rfq_row(RFQ_ID))
    seeded.flush()
    seeded.add(run_row(RUN_ID, RFQ_ID))
    seeded.commit()
    return seeded


@pytest.fixture
def reader(seeded: Session, db: Database) -> Iterator[BusinessReader]:
    """The read boundary, over a second session that only ever reads."""
    del seeded  # dependency only: the database must be seeded before reading
    with db.session_factory() as active:
        yield BusinessReader.for_session(active)


@pytest.fixture
def writer(db: Database) -> QuoteWriter:
    """The accepted write path, for the draft-stays-draft check."""
    return QuoteWriter(db)


def extracted_line(**overrides: object) -> ExtractedLine:
    """The seeded best case as claims: forty pumps ordered by part number."""
    values: dict[str, object] = {
        "ordinal": 1,
        "raw_text": "Please quote 40 pcs of 100-ABC",
        "requested_sku": "100-ABC",
        "description": "Centrifugal pump PMP-A-100",
        "quantity": 40,
        "evidence": "40 pcs of 100-ABC",
    }
    values.update(overrides)
    return ExtractedLine.model_validate(values)


def requested_delivery() -> RequestedDelivery:
    """What the customer asked for: Hamburg, on the eighth, as written."""
    return RequestedDelivery(
        raw="on 2026-10-08",
        resolution=DateResolution.EXPLICIT,
        requested_delivery_date=date(2026, 10, 8),
        destination="Hamburg",
    )


class TestTheSeededChain:
    def test_email_identity_to_draft_quote_and_no_further(
        self, runs: Session, reader: BusinessReader, writer: QuoteWriter, db: Database
    ) -> None:
        """The whole chain resolves, runs, and stops at a draft awaiting a human."""
        del runs  # dependency only: a stored quote needs its run row
        customer, evidence = bind_customer("einkauf@nordwind-industrie.de", reader=reader)
        assert customer.customer_id == "CUS_0001"
        assert customer.match_status is CustomerMatchStatus.EXACT
        assert customer.source is ResolutionSource.SYSTEM
        assert evidence.matched_ids == ("CUS_0001",)

        (line,) = resolve_lines([extracted_line()], line_item_ids=["LI_0001"], reader=reader)
        assert line.status is ResolutionMatchStatus.RESOLVED
        assert line.product_id == "PRD_0001"
        assert line.sku == "PMP-A-100"
        assert line.source is ResolutionSource.SYSTEM

        request = to_quote_request(
            customer,
            [line],
            requested_delivery=requested_delivery(),
            delivery_as_of=NOW,
            run_id=RUN_ID,
            quote_id="QTE_0001",
            quote_number="Q-2026-0001",
            pricing_as_of=AS_OF,
            stock_as_of=STOCK_STAMP,
        )
        assert request.require_human_approval is True

        result = run_quote(request, reader)

        # Execution completed - that is not approval and never becomes one.
        assert result.status is QuoteRunStatus.COMPLETED
        quote = result.quote
        assert quote is not None
        assert quote.status is QuoteStatus.DRAFT
        assert result.decision is not None
        # The gate's verdict is about *eligibility for review*, nothing more.
        assert result.eligible_for_human_review == result.decision.eligible_for_human_review

        written = writer.persist(result.calculation, result.ledger)  # type: ignore[arg-type]
        assert written.duplicate is False
        with db.session() as session:
            row = session.get(QuoteRow, written.quote_id)
        assert row is not None
        assert row.status == QuoteStatus.DRAFT.value
        assert request.require_human_approval is True

    def test_a_trading_name_alone_never_auto_binds(self, reader: BusinessReader) -> None:
        customer, evidence = bind_customer("Vistula", reader=reader)
        assert customer.customer_id == "CUS_0002"
        assert customer.match_status is CustomerMatchStatus.SINGLE_CANDIDATE
        assert customer.source is ResolutionSource.SYSTEM
        assert evidence.matched_ids == ("CUS_0002",)

        (line,) = resolve_lines([extracted_line()], line_item_ids=["LI_0001"], reader=reader)
        with pytest.raises(ValueError, match="requires a human decision"):
            to_quote_request(
                customer,
                [line],
                run_id=RUN_ID,
                quote_id="QTE_0001",
                quote_number="Q-2026-0001",
                pricing_as_of=AS_OF,
                stock_as_of=STOCK_STAMP,
            )

    def test_the_seeded_sku_collision_stays_with_a_human(self, reader: BusinessReader) -> None:
        """``PMP-A-100`` is one product's SKU and another's alias: never resolved."""
        customer, _ = bind_customer("einkauf@nordwind-industrie.de", reader=reader)
        (line,) = resolve_lines(
            [extracted_line(requested_sku="PMP-A-100")],
            line_item_ids=["LI_0001"],
            reader=reader,
        )
        assert line.status is ResolutionMatchStatus.AMBIGUOUS
        assert line.product_id is None

        with pytest.raises(ValueError, match="line 1 is not RESOLVED"):
            to_quote_request(
                customer,
                [line],
                run_id=RUN_ID,
                quote_id="QTE_0001",
                quote_number="Q-2026-0001",
                pricing_as_of=AS_OF,
                stock_as_of=STOCK_STAMP,
            )
