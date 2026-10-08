"""The quote run's contract, and the shape that keeps it a composition.

Two kinds of check live here. The first are contract checks: what a request may
ask for, and what a result may carry - a run that decided nothing must be unable
to hand back a decision, and a question only one line can answer cannot be asked
about three.

The second parse ``rfq_agent.quoting`` itself, because the layer is only
trustworthy as a composition if its source is one. The module must import and
call each accepted decision, must contain no arithmetic of its own, must not
write, must not read a clock and must read only through the boundary it is
given. None of those are style rules: each one is a claim the Phase 1L design
makes about where the decisions, the data, the composition and the persistence
live, and a claim a reviewer should be able to re-run.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import rfq_agent.domain as domain_package
from rfq_agent import quoting
from rfq_agent.domain.quote import QuoteCalculation
from rfq_agent.quoting import (
    DeliveryQuestion,
    DiscountQuestion,
    QuoteLineRequest,
    QuoteRequest,
    QuoteRunResult,
    QuoteRunStatus,
)
from tests.conftest import make_clean_quote

MODULE_PATH = Path(quoting.__file__)
DOMAIN_PACKAGE = Path(domain_package.__file__).resolve().parent

#: Where each accepted decision comes from. The composition may import these
#: names, and only these: a second copy of a rule would be the defect Phase 1L
#: must not introduce.
ACCEPTED_DECISIONS: dict[str, str] = {
    "select_price": "rfq_agent.domain.pricing",
    "evaluate_stock": "rfq_agent.domain.stock",
    "evaluate_delivery": "rfq_agent.domain.delivery",
    "select_discount": "rfq_agent.domain.policy",
    "calculate_quote": "rfq_agent.domain.quote",
    "project_blocked_ledger": "rfq_agent.domain.gating",
    "evaluate_quote_gate": "rfq_agent.domain.gating",
}

#: Arithmetic, as opposed to the ``X | None`` annotations ``BinOp`` also parses.
_ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)

#: The calls that change data, from the Phase 1J' purity test's own list.
_DML_HELPERS = frozenset({"add", "add_all", "delete", "flush", "insert", "merge", "update"})

#: Ways to read a clock or mint an identity; a deterministic run needs none.
_CLOCK_NAMES = frozenset({"now", "utcnow", "today", "time", "monotonic", "perf_counter"})
_IDENTITY_NAMES = frozenset(
    {"uuid1", "uuid3", "uuid4", "uuid5", "token_hex", "token_urlsafe", "random"}
)


def source() -> str:
    """The composition module's own source."""
    return MODULE_PATH.read_text()


def tree() -> ast.Module:
    """The composition module, parsed."""
    return ast.parse(source())


def imported_names(module_tree: ast.Module) -> dict[str, str]:
    """Every ``from X import Y`` in the module, as ``{Y: X}``."""
    imported: dict[str, str] = {}
    for node in ast.walk(module_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = node.module
    return imported


def called_names(module_tree: ast.Module) -> set[str]:
    """Every plain name the module calls, e.g. ``{"calculate_quote"}``."""
    return {
        node.func.id
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def called_attributes(module_tree: ast.Module) -> set[str]:
    """Every attribute the module calls, e.g. ``{"get", "append"}``."""
    return {
        node.func.attr
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def reader_attributes(module_tree: ast.Module) -> set[str]:
    """Every attribute read off the boundary named ``reader`` in the module."""
    return {
        node.attr
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "reader"
    }


def request(**overrides: object) -> QuoteRequest:
    """A minimal valid request, so each contract test changes exactly one thing."""
    values: dict[str, object] = {
        "run_id": "RUN_0001",
        "quote_id": "QTE_0001",
        "quote_number": "Q-2026-0001",
        "customer_id": "CUS_0001",
        "lines": (
            QuoteLineRequest(
                product_id="PRD_0001",
                sku="PMP-A-100",
                description="Centrifugal pump PMP-A-100",
                quantity=40,
            ),
        ),
        "pricing_as_of": date(2026, 10, 6),
        "stock_as_of": datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return QuoteRequest(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The contract: what a request may ask and what a result may carry
# ---------------------------------------------------------------------------


class TestTheRequestContract:
    """A request states facts; the run reads the rest."""

    def test_a_request_without_a_line_is_refused(self) -> None:
        """A quote needs something to quote (the calculator's own rule)."""
        with pytest.raises(ValidationError):
            request(lines=())

    @pytest.mark.parametrize("question", ["delivery", "discount"])
    def test_two_lines_may_not_ask_a_one_line_question(self, question: str) -> None:
        """No accepted contract says which line such a question would be about."""
        second = QuoteLineRequest(
            product_id="PRD_0003",
            sku="PMP-B-150",
            description="Centrifugal pump PMP-B-150",
            quantity=1,
        )
        asked: dict[str, object] = {
            "delivery": DeliveryQuestion(
                destination="Hamburg",
                destination_country="DE",
                as_of=datetime(2026, 10, 6, 9, 0, tzinfo=UTC),
            ),
            "discount": DiscountQuestion(order_value="46000.00"),
        }

        with pytest.raises(ValidationError, match="answered for one line"):
            request(lines=(*request().lines, second), **{question: asked[question]})

    def test_a_discount_question_refuses_a_float_order_value(self) -> None:
        """Money is never a binary float - the accepted contract's own refusal."""
        with pytest.raises(ValidationError, match="never float"):
            DiscountQuestion(order_value=46000.0)

    def test_an_unexpected_field_is_refused(self) -> None:
        """``extra="forbid"``: a request cannot smuggle in a fact nobody reads."""
        with pytest.raises(ValidationError):
            request(approved=True)


class TestTheResultContract:
    """A result is the artefacts, or the reason there are none."""

    def test_a_completed_run_must_carry_its_artefacts(self) -> None:
        """A status that says \"completed\" with nothing to show is unrepresentable."""
        with pytest.raises(ValidationError, match="completed run carries"):
            QuoteRunResult(
                request=request(),
                status=QuoteRunStatus.COMPLETED,
                detail="nothing to show",
            )

    def test_a_failed_run_carries_no_artefact(self) -> None:
        """A run that decided nothing may not hand back a decision."""
        calculation = QuoteCalculation(
            quote=make_clean_quote(),
            refusals=(),
            detail="a clean quote",
        )
        with pytest.raises(ValidationError, match="did not complete"):
            QuoteRunResult(
                request=request(),
                status=QuoteRunStatus.CUSTOMER_NOT_FOUND,
                detail="customer missing",
                calculation=calculation,
            )

    def test_a_run_that_decided_nothing_is_never_eligible(self) -> None:
        """The one field a consumer may act on defaults to \"no\", not to \"approved\"."""
        result = QuoteRunResult(
            request=request(),
            status=QuoteRunStatus.CUSTOMER_NOT_FOUND,
            detail="customer CUS_9999 is not in the business data",
        )

        assert result.eligible_for_human_review is False
        assert result.quote is None

    def test_the_status_enum_is_closed(self) -> None:
        """Two outcomes, both named: a run does not half-complete."""
        assert {status.value for status in QuoteRunStatus} == {"COMPLETED", "CUSTOMER_NOT_FOUND"}


# ---------------------------------------------------------------------------
# The shape: the composition is a composition, by construction
# ---------------------------------------------------------------------------


class TestTheCompositionIsStructural:
    """The module's source, held to the boundary the design check states."""

    def test_every_accepted_decision_is_imported_from_where_it_is_defined(self) -> None:
        """Each decision is used as the phase that owns it published it."""
        imported = imported_names(tree())
        for name, module in ACCEPTED_DECISIONS.items():
            assert imported.get(name) == module, name

    def test_every_accepted_decision_is_actually_called(self) -> None:
        """Importing a rule and not asking it would leave the run undecided."""
        assert set(ACCEPTED_DECISIONS) <= called_names(tree())

    def test_the_layer_computes_nothing_of_its_own(self) -> None:
        """No arithmetic, no rounding: the calculator is the only place money moves."""
        offenders = [
            node
            for node in ast.walk(tree())
            if isinstance(node, ast.BinOp) and isinstance(node.op, _ARITHMETIC)
        ]
        assert offenders == []
        primitives = {"round", "quantize", "from_float"}
        assert primitives & (called_names(tree()) | called_attributes(tree())) == set()

    def test_the_layer_performs_no_data_change(self) -> None:
        """A composition that could write is a persistence layer in disguise."""
        module_tree = tree()
        assert called_attributes(module_tree) & _DML_HELPERS == set()
        imported = imported_names(module_tree)
        assert "Decimal" not in imported
        assert not any(module.startswith("sqlalchemy") for module in imported.values())
        assert not any(module.endswith("writers") for module in imported.values())

    def test_the_layer_reads_only_through_the_boundary_it_is_given(self) -> None:
        """The reader's repositories are the whole data surface - and no search."""
        module_tree = tree()
        assert reader_attributes(module_tree) == {
            "customers",
            "discounts",
            "delivery",
            "pricing",
            "stock",
        }
        assert "search" not in called_attributes(module_tree)

    def test_the_layer_reads_no_clock_and_mints_no_identity(self) -> None:
        """Determinism is a property of the source, not a hope about the data."""
        module_tree = tree()
        assert called_attributes(module_tree) & _CLOCK_NAMES == set()
        assert called_names(module_tree) & _IDENTITY_NAMES == set()
        imported = imported_names(module_tree)
        assert not ({"uuid", "random", "secrets", "time"} & set(imported.values()))

    def test_the_only_persistence_types_it_names_are_the_boundary(self) -> None:
        """The boundary and its value object; no session, no row, no writer."""
        imported = imported_names(tree())
        assert {
            name for name, module in imported.items() if module.startswith("rfq_agent.persistence")
        } == {"BusinessReader", "CustomerRecord"}

    def test_no_domain_module_reaches_back_into_the_composition(self) -> None:
        """The domain stays the layer the composition conforms to, never the reverse."""
        leaking = [
            path.name
            for path in sorted(DOMAIN_PACKAGE.rglob("*.py"))
            if "quoting" in path.read_text()
        ]
        assert leaking == []

    def test_the_domain_layer_names_business_data_without_reading_it(self) -> None:
        """A rule may name a read model under ``TYPE_CHECKING``; never at runtime.

        Phase 1F and 1G name the boundary's records in their annotations that
        way, which is the accepted shape. A runtime import would put I/O inside
        the layer whose own docstring says it contains none - and it is the
        boundary that the composition, not the domain, is allowed to touch.
        """
        for path in sorted(DOMAIN_PACKAGE.rglob("*.py")):
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, ast.If) and _is_type_checking(node.test):
                    continue
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom) and (child.module or "").startswith(
                        "rfq_agent.persistence"
                    ):
                        pytest.fail(f"{path.name} imports persistence at runtime: {child.module}")


def _is_type_checking(test: ast.expr) -> bool:
    """Whether an ``if`` is the ``if TYPE_CHECKING:`` guard."""
    return isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"


def module_functions() -> Iterator[str]:
    """Every function the composition defines, so the count can be asserted stable."""
    for node in tree().body:
        if isinstance(node, ast.FunctionDef):
            yield node.name


def test_the_layer_defines_its_entry_point_and_no_decision_function() -> None:
    """Only one public name runs a quote, and it decides nothing itself."""
    defined = set(module_functions())
    selectors = {"select_price", "evaluate_stock", "evaluate_delivery", "select_discount"}
    assert "run_quote" in defined
    assert defined & set(ACCEPTED_DECISIONS) == set()
    assert selectors & defined == set()
