"""The ledger's write path: one writer, one transaction, one identity per write.

Phase 1J' gives the projected ledger a table of its own and rules that it is
written by nothing else, that a retry is the same write rather than a second one,
and that a write which fails leaves no half-record behind. ``test_append_only``
covers the database's own refusal to rewrite the ledger and
``test_seeded_writers`` covers the behaviour end to end; this module covers the
structural half of the same contract - *which* module is allowed to write it, and
what the write path is allowed to know.

The structural checks parse the sources rather than trusting a convention: a
second insert somewhere in the package would be a real regression, and no
behavioural test calls the code path that was never meant to exist.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rfq_agent.domain.gating import project_blocked_ledger
from rfq_agent.domain.policy import QuoteBlockedLedger
from rfq_agent.domain.pricing import select_price
from rfq_agent.domain.quote import QuoteCalculation, QuoteLineInput, calculate_quote
from rfq_agent.domain.stock import evaluate_stock
from rfq_agent.persistence import Database, models, writers
from rfq_agent.persistence.models import QuoteBlockedReasonRow
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.persistence.writers import QuoteWriter
from rfq_agent.seed import reset_and_seed
from tests.persistence.factories import rfq_row, run_row

#: Where the package lives, for the structural checks.
PACKAGE = Path(writers.__file__).resolve().parent

#: The table the projection is stored in, and the model that declares it.
LEDGER_TABLE = "quote_blocked_reasons"
LEDGER_MODEL = "QuoteBlockedReasonRow"

#: The name at the head of the write path, and the module that holds it.
WRITER_MODULE = "persistence.writers"

#: The decisions the write path must not be able to make for itself.
DECISIONS = frozenset(
    {
        "calculate_quote",
        "evaluate_delivery",
        "evaluate_policy",
        "evaluate_stock",
        "project_blocked_ledger",
        "select_discount",
        "select_price",
    }
)

#: The same facts ``test_seeded_writers`` uses, so the behaviour here is seeded.
AS_OF = date(2026, 10, 6)
STOCK_STAMP = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
RUN_ID = "RUN_0001"


@pytest.fixture
def seeded(session: Session) -> Session:
    """A committed, freshly seeded database."""
    reset_and_seed(session)
    session.commit()
    return session


@pytest.fixture
def runs(seeded: Session) -> Session:
    """The seeded database plus the run a quote in these tests belongs to."""
    seeded.add(rfq_row("RFQ_0001"))
    seeded.flush()
    seeded.add(run_row(RUN_ID, "RFQ_0001"))
    seeded.commit()
    return seeded


@pytest.fixture
def writer(db: Database) -> QuoteWriter:
    """The write path under test."""
    return QuoteWriter(db)


@pytest.fixture
def reader(seeded: Session, db: Database) -> Iterator[BusinessReader]:
    """The read boundary, over a second session that only ever reads."""
    del seeded  # dependency only: the database must be seeded before reading
    with db.session_factory() as active:
        yield BusinessReader.for_session(active)


def _sources() -> dict[Path, str]:
    """Every Python module in the package, by path."""
    return {path: path.read_text() for path in sorted(PACKAGE.rglob("*.py"))}


def _module_name(path: Path) -> str:
    """The dotted name of a module file, the way the package imports it."""
    parts = list(path.relative_to(PACKAGE).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("persistence", *parts))


def _names_imported_from(source: str, prefix: str) -> set[str]:
    """Every name a module imports from submodules whose name starts with ``prefix``."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(prefix):
            names.update(alias.name for alias in node.names)
    return names


def _functions_mentioning(source: str, name: str) -> set[str | None]:
    """The enclosing function of every node that mentions ``name`` (``None`` at module level)."""
    tree = ast.parse(source)
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    enclosing: set[str | None] = set()
    for node in ast.walk(tree):
        if not any(isinstance(inner, ast.Name) and inner.id == name for inner in ast.walk(node)):
            continue
        lineno = getattr(node, "lineno", None)
        holder = next(
            (
                function.name
                for function in functions
                if lineno is not None
                and function.lineno <= lineno <= (function.end_lineno or function.lineno)
            ),
            None,
        )
        enclosing.add(holder)
    return enclosing


def _called_on(source: str, name: str) -> set[str]:
    """The text of the first argument of every call to ``name`` in the module."""
    return {
        ast.unparse(node.args[0])
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        and node.args
    }


# ---------------------------------------------------------------------------
# One table, one writer
# ---------------------------------------------------------------------------


def test_the_ledger_model_is_declared_exactly_once() -> None:
    """One declaration, one table: a second model would be a second writer's excuse."""
    declarations = [
        path.name
        for path, source in _sources().items()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and node.name == LEDGER_MODEL
    ]
    assert declarations == ["quote.py"]
    assert models.QuoteBlockedReasonRow.__tablename__ == LEDGER_TABLE


def test_only_the_write_path_imports_the_ledger_model() -> None:
    """Outside the write path the ledger is a name nobody needs - and nobody has."""
    importers = {
        _module_name(path)
        for path, source in _sources().items()
        if LEDGER_MODEL in _names_imported_from(source, "rfq_agent.persistence.models")
    }
    # The models package re-exports its own declarations; that is a convenience,
    # not a second writer.
    importers -= {"persistence", "persistence.models", "persistence.models.quote"}
    assert importers == {WRITER_MODULE}


def test_the_write_path_only_reads_and_inserts_the_ledger() -> None:
    """``_stored_result`` reads it, ``_ledger_rows`` builds it - nothing else touches it."""
    source = (PACKAGE / "writers.py").read_text()
    assert _functions_mentioning(source, LEDGER_MODEL) == {
        None,  # the import
        "_stored_result",
        "_ledger_rows",
    }


def test_the_write_path_deletes_claims_and_never_evidence() -> None:
    """The one row the write path deletes is an idempotency claim, and only that."""
    source = (PACKAGE / "writers.py").read_text()
    assert _called_on(source, "delete") == {"IdempotencyClaimRow"}
    assert _called_on(source, "update") == set()
    assert _called_on(source, "sqlite_insert") == {"IdempotencyClaimRow"}


# ---------------------------------------------------------------------------
# What the write path is allowed to know
# ---------------------------------------------------------------------------


def test_the_write_path_imports_contracts_and_no_decisions() -> None:
    """Types and constants, never a function that decides something."""
    source = (PACKAGE / "writers.py").read_text()
    imported = _names_imported_from(source, "rfq_agent.domain")
    assert imported == {
        "MISSING_PRICE_ENTRY_ID",
        "PriceLookupStatus",
        "Quote",
        "QuoteBlockedLedger",
        "QuoteCalculation",
        "QuoteLine",
        "canonical_json",
        "sha256_text",
    }
    assert imported.isdisjoint(DECISIONS)


def test_the_write_path_takes_no_decision_inputs() -> None:
    """Nothing to decide with: no rfq id, no revision, no status, no policy verdict."""
    parameters = inspect.signature(QuoteWriter.persist).parameters
    assert list(parameters) == ["self", "calculation", "ledger", "claim_ttl_seconds"]
    assert list(inspect.signature(QuoteWriter.__init__).parameters) == [
        "self",
        "database",
        "clock",
    ]


def test_the_claims_are_filed_under_this_layer_s_own_scope() -> None:
    """A claim key only means one thing inside one scope."""
    assert writers.CLAIM_SCOPE == "quote_persist"


# ---------------------------------------------------------------------------
# What identity means for a write
# ---------------------------------------------------------------------------


def _a_quote_with_a_refusal(reader: BusinessReader) -> QuoteCalculation:
    """One line whose price lookup failed, and the stock it cannot be shipped from."""
    price = select_price(
        reader.pricing.entries_for_products(["PRD_0006"]),
        product_id="PRD_0006",
        quantity=1,
        as_of=AS_OF,
        customer_id="CUS_0001",
        currency="EUR",
    )
    stock = evaluate_stock(
        reader.stock.levels_for_products(["PRD_0006"]),
        product_id="PRD_0006",
        requested_qty=1,
        as_of=STOCK_STAMP,
    )
    lines: Sequence[QuoteLineInput] = (
        QuoteLineInput(
            product_id=price.product_id,
            sku="PMP-D-300",
            description="Seeded catalogue item",
            quantity=1,
            price=price,
            stock_status=stock.status,
        ),
    )
    return calculate_quote(
        lines,
        quote_id="QTE_0001",
        quote_number="Q-2026-0001",
        run_id=RUN_ID,
        customer_id="CUS_0001",
        currency="EUR",
        pricing_as_of=AS_OF,
    )


def _ledger(calculation: QuoteCalculation) -> QuoteBlockedLedger:
    """The projection for that quote."""
    return project_blocked_ledger(calculation, run_id=RUN_ID)


def test_a_second_writer_instance_recognises_the_first_write(
    reader: BusinessReader, runs: Session, db: Database
) -> None:
    """Identity is the content of the write, not the object that performed it."""
    del runs  # dependency only: a quote cannot be stored without its run
    calculation = _a_quote_with_a_refusal(reader)
    ledger = _ledger(calculation)

    first = QuoteWriter(db).persist(calculation, ledger)
    second = QuoteWriter(db).persist(calculation, ledger)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.line_ids == first.line_ids


def test_the_evidence_order_is_part_of_the_write_s_identity(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Reordered evidence is a different record - and a second record is refused.

    The ledger is stored in the order it was projected, so two orders are two
    different claims. The second one finds the run already carrying revision 1 and
    is refused rather than quietly becoming a contradicting store of the same
    quote.
    """
    calculation = _a_quote_with_a_refusal(reader)
    ledger = _ledger(calculation)
    writer.persist(calculation, ledger)

    reversed_ledger = ledger.model_copy(update={"reasons": tuple(reversed(ledger.reasons))})
    assert reversed_ledger.reasons != ledger.reasons

    with pytest.raises(IntegrityError, match=r"quotes\.run_id"):
        writer.persist(calculation, reversed_ledger)

    runs.rollback()
    stored = list(
        runs.scalars(
            select(QuoteBlockedReasonRow.code)
            .where(QuoteBlockedReasonRow.quote_id == "QTE_0001")
            .order_by(QuoteBlockedReasonRow.seq)
        )
    )
    assert stored == [reason.code.value for reason in ledger.reasons]
