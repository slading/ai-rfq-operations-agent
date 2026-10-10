"""The deterministic resolution core's contract (Phase 1M-B).

Two kinds of check live here. The first are the ruling's own rules: identity
is proven only by an ``EMAIL`` alias and everything else hands the decision to
a human; lines resolve on catalogue numbers alone, with the fixed precedence
for ambiguous, unknown, discontinued and quantity-less claims; claim verdicts
are ignored; caller identifiers are stated, never generated.

The second re-run the purity claim: this core mints nothing and reads no
clock, so the same data and the same stated inputs produce the same facts -
which is what lets everything downstream stay deterministic.

The reader is faked at the query boundary only: every candidate, match and
record below is the real read model, so these tests pin exactly what evidence
the core is willing to rule on.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from rfq_agent import resolving
from rfq_agent.domain.extraction import ExtractedLine
from rfq_agent.domain.resolution import (
    CustomerMatchStatus,
    ResolutionMatchStatus,
    ResolutionSource,
)
from rfq_agent.persistence.enums import AliasKind
from rfq_agent.persistence.read_models import (
    CustomerMatch,
    CustomerRecord,
    CustomerSearch,
    MatchSource,
    ProductMatch,
    ProductRecord,
    ProductSearch,
)
from rfq_agent.resolving import bind_customer, resolve_lines

MODULE_PATH = Path(resolving.__file__)

#: Modules a pure ruling core has no business importing: clocks and minting.
_BANNED_IMPORTS = frozenset({"uuid", "random", "secrets", "time"})

#: Call attributes that would make the core stateful or non-deterministic.
_BANNED_CALLS = frozenset({"now", "utcnow", "today", "uuid4", "perf_counter", "monotonic"})


def customer_record(
    customer_id: str = "CUS_0001",
    *,
    active: bool = True,
    display_name: str = "Nordwind",
) -> CustomerRecord:
    return CustomerRecord(
        customer_id=customer_id,
        legal_name=f"{display_name} GmbH",
        display_name=display_name,
        country_code="DE",
        default_currency="EUR",
        payment_terms_days=30,
        credit_limit=None,
        credit_hold=False,
        active=active,
        notes=None,
    )


def product_record(
    product_id: str = "PRD_0001",
    *,
    sku: str = "PMP-A-100",
    active: bool = True,
) -> ProductRecord:
    return ProductRecord(
        product_id=product_id,
        sku=sku,
        family_code="FAM_PUMPS",
        name=f"Catalogue item {sku}",
        description=f"Catalogue item {sku}",
        uom="EA",
        active=active,
    )


def email_match(customer: CustomerRecord, address: str) -> CustomerMatch:
    return CustomerMatch(
        customer=customer,
        source=MatchSource.ALIAS,
        matched_text=address,
        alias_kind=AliasKind.EMAIL,
    )


def domain_match(customer: CustomerRecord, domain: str) -> CustomerMatch:
    return CustomerMatch(
        customer=customer,
        source=MatchSource.ALIAS,
        matched_text=domain,
        alias_kind=AliasKind.EMAIL_DOMAIN,
    )


def name_alias_match(customer: CustomerRecord, trading_name: str) -> CustomerMatch:
    return CustomerMatch(
        customer=customer,
        source=MatchSource.ALIAS,
        matched_text=trading_name,
        alias_kind=AliasKind.NAME,
    )


def name_match(customer: CustomerRecord, name: str) -> CustomerMatch:
    return CustomerMatch(customer=customer, source=MatchSource.NAME, matched_text=name)


def product_match(
    product: ProductRecord,
    matched_text: str,
    *,
    source: MatchSource = MatchSource.ALIAS,
    alias_kind: AliasKind | None = None,
) -> ProductMatch:
    return ProductMatch(
        product=product,
        source=source,
        matched_text=matched_text,
        alias_kind=alias_kind,
    )


def product_search(query: str, *matches: ProductMatch) -> ProductSearch:
    return ProductSearch(
        query=query,
        sources=frozenset({MatchSource.SKU, MatchSource.ALIAS}),
        matches=matches,
    )


def extracted(**overrides: object) -> ExtractedLine:
    values: dict[str, object] = {
        "ordinal": 1,
        "raw_text": "40 units of 100-ABC",
        "requested_sku": "100-ABC",
        "description": "Hydraulic pump",
        "quantity": 40,
        "evidence": "40 units of 100-ABC",
    }
    values.update(overrides)
    return ExtractedLine.model_validate(values)


class FakeCustomers:
    """One canned search outcome; the query is recorded, not interpreted."""

    def __init__(self, result: CustomerSearch) -> None:
        self._result = result
        self.queries: list[str] = []

    def search(self, text: str) -> CustomerSearch:
        self.queries.append(text)
        return self._result


class FakeCatalog:
    """Canned search outcomes keyed by the queried catalogue number."""

    def __init__(self, results: dict[str, ProductSearch]) -> None:
        self._results = results
        self.queries: list[tuple[str, object]] = []

    def search(self, text: str, *, sources: object = None) -> ProductSearch:
        self.queries.append((text, sources))
        return self._results.get(text, product_search(text))


class FakeReader:
    def __init__(
        self,
        *,
        customers: CustomerSearch | None = None,
        catalog: dict[str, ProductSearch] | None = None,
    ) -> None:
        self.customers = FakeCustomers(customers or CustomerSearch(query="", matches=()))
        self.catalog = FakeCatalog(catalog or {})


class TestBindCustomer:
    def test_email_identity_alone_auto_binds_exact(self) -> None:
        nordwind = customer_record()
        search = CustomerSearch(
            query="einkauf@nordwind-industrie.de",
            matches=(email_match(nordwind, "einkauf@nordwind-industrie.de"),),
        )
        ruling, evidence = bind_customer(
            "einkauf@nordwind-industrie.de", reader=FakeReader(customers=search)
        )

        assert ruling.customer_id == "CUS_0001"
        assert ruling.match_status is CustomerMatchStatus.EXACT
        assert ruling.source is ResolutionSource.SYSTEM
        assert evidence is search

    def test_extra_supporting_evidence_does_not_disturb_an_exact_bind(self) -> None:
        nordwind = customer_record()
        search = CustomerSearch(
            query="einkauf@nordwind-industrie.de",
            matches=(
                email_match(nordwind, "einkauf@nordwind-industrie.de"),
                domain_match(nordwind, "nordwind-industrie.de"),
                name_match(nordwind, "Nordwind"),
            ),
        )
        ruling, _ = bind_customer(
            "einkauf@nordwind-industrie.de", reader=FakeReader(customers=search)
        )

        assert ruling.match_status is CustomerMatchStatus.EXACT

    def test_a_name_only_match_is_a_candidate_never_an_identity(self) -> None:
        vistula = customer_record("CUS_0002", display_name="Vistula Machinery")
        search = CustomerSearch(
            query="Vistula",
            matches=(name_alias_match(vistula, "Vistula"),),
        )
        ruling, _ = bind_customer("Vistula", reader=FakeReader(customers=search))

        assert ruling.customer_id == "CUS_0002"
        assert ruling.match_status is CustomerMatchStatus.SINGLE_CANDIDATE
        assert ruling.source is ResolutionSource.SYSTEM

    def test_a_domain_match_is_a_candidate_never_an_identity(self) -> None:
        nordwind = customer_record()
        search = CustomerSearch(
            query="nordwind-industrie.de",
            matches=(domain_match(nordwind, "nordwind-industrie.de"),),
        )
        ruling, _ = bind_customer("nordwind-industrie.de", reader=FakeReader(customers=search))

        assert ruling.match_status is CustomerMatchStatus.SINGLE_CANDIDATE

    def test_an_email_identity_to_an_inactive_customer_still_needs_a_human(self) -> None:
        dormant = customer_record(active=False)
        search = CustomerSearch(
            query="einkauf@nordwind-industrie.de",
            matches=(email_match(dormant, "einkauf@nordwind-industrie.de"),),
        )
        ruling, _ = bind_customer(
            "einkauf@nordwind-industrie.de", reader=FakeReader(customers=search)
        )

        assert ruling.customer_id == "CUS_0001"
        assert ruling.match_status is CustomerMatchStatus.SINGLE_CANDIDATE

    def test_the_same_address_on_two_customers_binds_nobody(self) -> None:
        first = customer_record("CUS_0001")
        second = customer_record("CUS_0002", display_name="Vistula Machinery")
        search = CustomerSearch(
            query="shared@example.com",
            matches=(
                email_match(first, "shared@example.com"),
                email_match(second, "shared@example.com"),
            ),
        )
        ruling, evidence = bind_customer("shared@example.com", reader=FakeReader(customers=search))

        assert ruling.customer_id is None
        assert ruling.match_status is CustomerMatchStatus.UNBOUND
        assert ruling.source is None
        assert evidence.matched_ids == ("CUS_0001", "CUS_0002")

    def test_identity_on_one_customer_and_evidence_on_another_binds_nobody(self) -> None:
        first = customer_record("CUS_0001")
        second = customer_record("CUS_0002", display_name="Vistula Machinery")
        search = CustomerSearch(
            query="einkauf@nordwind-industrie.de",
            matches=(
                email_match(first, "einkauf@nordwind-industrie.de"),
                name_alias_match(second, "einkauf@nordwind-industrie.de"),
            ),
        )
        ruling, _ = bind_customer(
            "einkauf@nordwind-industrie.de", reader=FakeReader(customers=search)
        )

        assert ruling.customer_id is None
        assert ruling.match_status is CustomerMatchStatus.UNBOUND

    def test_no_match_binds_nobody(self) -> None:
        search = CustomerSearch(query="nobody@example.com", matches=())
        ruling, evidence = bind_customer("nobody@example.com", reader=FakeReader(customers=search))

        assert ruling.customer_id is None
        assert ruling.match_status is CustomerMatchStatus.UNBOUND
        assert evidence.found is False

    def test_the_same_evidence_produces_the_same_ruling(self) -> None:
        nordwind = customer_record()
        search = CustomerSearch(
            query="einkauf@nordwind-industrie.de",
            matches=(email_match(nordwind, "einkauf@nordwind-industrie.de"),),
        )
        first, _ = bind_customer(
            "einkauf@nordwind-industrie.de", reader=FakeReader(customers=search)
        )
        second, _ = bind_customer(
            "einkauf@nordwind-industrie.de", reader=FakeReader(customers=search)
        )
        assert first == second


class TestResolveLines:
    def test_an_exact_sku_match_resolves_with_the_canonical_sku(self) -> None:
        pump = product_record()
        catalog = {
            "PMP-A-100": product_search(
                "PMP-A-100",
                product_match(pump, "PMP-A-100", source=MatchSource.SKU),
            )
        }
        (line,) = resolve_lines(
            [extracted(requested_sku="PMP-A-100")],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )

        assert line.status is ResolutionMatchStatus.RESOLVED
        assert line.product_id == "PRD_0001"
        assert line.sku == "PMP-A-100"
        assert line.source is ResolutionSource.SYSTEM
        assert line.resolution_reason == "exact SKU match"
        assert line.ordinal == 1
        assert line.extracted.requested_sku == "PMP-A-100"

    def test_a_stored_alias_match_resolves_too(self) -> None:
        pump = product_record()
        catalog = {
            "100-ABC": product_search(
                "100-ABC",
                product_match(pump, "100-ABC", alias_kind=AliasKind.CUSTOMER_PART),
            )
        }
        (line,) = resolve_lines(
            [extracted()],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )

        assert line.status is ResolutionMatchStatus.RESOLVED
        assert line.product_id == "PRD_0001"
        assert line.sku == "PMP-A-100"
        assert line.resolution_reason == "stored alias match"

    def test_a_sku_alias_collision_is_ambiguous_and_picks_nothing(self) -> None:
        pump = product_record()
        twin = product_record("PRD_0002", sku="PMP-A-100-SS")
        catalog = {
            "PMP-A-100": product_search(
                "PMP-A-100",
                product_match(pump, "PMP-A-100", source=MatchSource.SKU),
                product_match(twin, "PMP-A-100", alias_kind=AliasKind.NAME),
            )
        }
        (line,) = resolve_lines(
            [extracted(requested_sku="PMP-A-100")],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )

        assert line.status is ResolutionMatchStatus.AMBIGUOUS
        assert line.product_id is None
        assert line.sku is None
        assert line.source is None
        assert "PRD_0001" in (line.resolution_reason or "")
        assert "PRD_0002" in (line.resolution_reason or "")

    def test_an_unknown_catalogue_number_is_unmatched(self) -> None:
        (line,) = resolve_lines(
            [extracted(requested_sku="NO-SUCH-SKU")],
            line_item_ids=["LI_0001"],
            reader=FakeReader(),
        )

        assert line.status is ResolutionMatchStatus.UNMATCHED
        assert line.product_id is None
        assert line.source is None

    def test_a_line_without_a_catalogue_number_is_unmatched(self) -> None:
        (line,) = resolve_lines(
            [extracted(requested_sku=None)],
            line_item_ids=["LI_0001"],
            reader=FakeReader(),
        )

        assert line.status is ResolutionMatchStatus.UNMATCHED
        assert line.resolution_reason == "no catalogue number was stated"

    def test_an_inactive_product_is_discontinued_not_unknown(self) -> None:
        retired = product_record(active=False)
        catalog = {
            "PMP-A-100": product_search(
                "PMP-A-100",
                product_match(retired, "PMP-A-100", source=MatchSource.SKU),
            )
        }
        (line,) = resolve_lines(
            [extracted(requested_sku="PMP-A-100")],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )

        assert line.status is ResolutionMatchStatus.DISCONTINUED
        assert line.product_id == "PRD_0001"
        assert line.sku == "PMP-A-100"
        assert line.source is ResolutionSource.SYSTEM

    def test_a_line_without_a_quantity_blocks_but_keeps_the_match(self) -> None:
        pump = product_record()
        catalog = {
            "PMP-A-100": product_search(
                "PMP-A-100",
                product_match(pump, "PMP-A-100", source=MatchSource.SKU),
            )
        }
        (line,) = resolve_lines(
            [extracted(requested_sku="PMP-A-100", quantity=None, missing_reason="NOT_STATED")],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )

        assert line.status is ResolutionMatchStatus.MISSING_QTY
        assert line.product_id == "PRD_0001"
        assert line.sku == "PMP-A-100"
        assert line.source is None

    def test_discontinued_beats_a_missing_quantity(self) -> None:
        retired = product_record(active=False)
        catalog = {
            "PMP-A-100": product_search(
                "PMP-A-100",
                product_match(retired, "PMP-A-100", source=MatchSource.SKU),
            )
        }
        (line,) = resolve_lines(
            [extracted(requested_sku="PMP-A-100", quantity=None, missing_reason="NOT_STATED")],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )

        assert line.status is ResolutionMatchStatus.DISCONTINUED

    def test_the_claim_verdict_is_ignored_when_it_says_resolved(self) -> None:
        (line,) = resolve_lines(
            [
                extracted(
                    requested_sku="NO-SUCH-SKU",
                    status="RESOLVED",
                    confidence=Decimal("1.0"),
                )
            ],
            line_item_ids=["LI_0001"],
            reader=FakeReader(),
        )

        assert line.status is ResolutionMatchStatus.UNMATCHED

    def test_the_claim_verdict_is_ignored_when_it_says_rejected(self) -> None:
        pump = product_record()
        catalog = {
            "PMP-A-100": product_search(
                "PMP-A-100",
                product_match(pump, "PMP-A-100", source=MatchSource.SKU),
            )
        }
        (line,) = resolve_lines(
            [
                extracted(
                    requested_sku="PMP-A-100",
                    status="REJECTED",
                    rejection_reason="the model disliked it",
                )
            ],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )

        assert line.status is ResolutionMatchStatus.RESOLVED

    def test_lines_keep_their_order_and_their_stated_ids(self) -> None:
        pump = product_record()
        seal = product_record("PRD_0002", sku="SL-K-50")
        catalog = {
            "PMP-A-100": product_search(
                "PMP-A-100",
                product_match(pump, "PMP-A-100", source=MatchSource.SKU),
            ),
            "SL-K-50": product_search(
                "SL-K-50",
                product_match(seal, "SL-K-50", source=MatchSource.SKU),
            ),
        }
        lines = resolve_lines(
            [
                extracted(requested_sku="PMP-A-100"),
                extracted(ordinal=2, requested_sku="SL-K-50", quantity=5),
            ],
            line_item_ids=["LI_0001", "LI_0002"],
            reader=FakeReader(catalog=catalog),
        )

        assert [line.line_item_id for line in lines] == ["LI_0001", "LI_0002"]
        assert [line.ordinal for line in lines] == [1, 2]
        assert [line.product_id for line in lines] == ["PRD_0001", "PRD_0002"]

    def test_refuses_identifier_count_mismatches(self) -> None:
        with pytest.raises(ValueError, match="pair one-to-one"):
            resolve_lines(
                [extracted(), extracted(ordinal=2)], line_item_ids=["LI_0001"], reader=FakeReader()
            )

    def test_refuses_repeated_identifiers(self) -> None:
        with pytest.raises(ValueError, match="must be unique"):
            resolve_lines(
                [extracted(), extracted(ordinal=2)],
                line_item_ids=["LI_0001", "LI_0001"],
                reader=FakeReader(),
            )

    def test_the_same_claims_produce_the_same_facts(self) -> None:
        pump = product_record()
        catalog = {
            "PMP-A-100": product_search(
                "PMP-A-100",
                product_match(pump, "PMP-A-100", source=MatchSource.SKU),
            )
        }
        first = resolve_lines(
            [extracted(requested_sku="PMP-A-100")],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )
        second = resolve_lines(
            [extracted(requested_sku="PMP-A-100")],
            line_item_ids=["LI_0001"],
            reader=FakeReader(catalog=catalog),
        )
        assert first == second


class TestPurity:
    def test_source_reads_no_clock_and_mints_nothing(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
                assert not names & _BANNED_IMPORTS
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not any(module.startswith(banned) for banned in _BANNED_IMPORTS)
                names = {alias.name for alias in node.names}
                assert not names & _BANNED_IMPORTS
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in _BANNED_CALLS

    def test_the_seam_is_exactly_two_public_functions(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        public = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        ]
        assert public == ["bind_customer", "resolve_lines"]
