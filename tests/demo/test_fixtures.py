"""The demo fixtures' own contract (Phase 1M-C).

Two kinds of check live here. The first pins the three approved scenarios:
their ids, order and declared outcomes, timezone-aware instants, and the trust
hygiene that makes the fixtures a legitimate stand-in for the LLM - every
claim span is verbatim in the untrusted body.

The second re-runs the reproducibility claim: the fixtures module is literals
with no clock and no minting, so the AST walk that ``test_resolving.py`` and
``test_quote_adapter.py`` apply to the pure cores applies here too.

What is *not* checked here: business verdicts. Whether SC-01's line prices at
1150.00 is the accepted phases' business, pinned in ``tests/persistence``;
these tests only prove the fixture is shaped so the accepted seams can run it.
"""

from __future__ import annotations

import ast
import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from rfq_agent import demo
from rfq_agent.demo.errors import DemoError
from rfq_agent.demo.fixtures import SCENARIOS, DemoOutcome, DemoScenario, scenario_by_id

MODULE_PATH = Path(demo.fixtures.__file__)

#: Modules literal fixture data has no business importing: clocks and minting.
_BANNED_IMPORTS = frozenset({"uuid", "random", "secrets", "time"})

#: Call attributes that would make the fixtures non-reproducible.
_BANNED_CALLS = frozenset({"now", "utcnow", "today", "uuid4", "perf_counter", "monotonic"})


class TestTheDeclaredScenarios:
    def test_three_scenarios_in_stable_order(self) -> None:
        assert [scenario.scenario_id for scenario in SCENARIOS] == ["SC-01", "SC-02", "SC-03"]

    def test_the_three_approved_outcomes_are_covered(self) -> None:
        declared = {scenario.scenario_id: scenario.expected for scenario in SCENARIOS}
        assert declared == {
            "SC-01": DemoOutcome.DRAFT_AWAITING_HUMAN_REVIEW,
            "SC-02": DemoOutcome.HUMAN_REQUIRED_BEFORE_QUOTE,
            "SC-03": DemoOutcome.DRAFT_BLOCKED,
        }

    def test_duplicate_is_never_a_declared_outcome(self) -> None:
        assert all(scenario.expected is not DemoOutcome.DUPLICATE for scenario in SCENARIOS)

    def test_scenario_by_id_finds_each_and_refuses_others(self) -> None:
        for scenario in SCENARIOS:
            assert scenario_by_id(scenario.scenario_id) is scenario
        with pytest.raises(DemoError, match=r"unknown scenario 'SC-99'.*SC-01, SC-02, SC-03"):
            scenario_by_id("SC-99")


class TestTrustHygiene:
    def test_every_claim_span_is_verbatim_in_the_untrusted_body(self) -> None:
        for scenario in SCENARIOS:
            for line in scenario.lines:
                assert line.raw_text in scenario.body, (scenario.scenario_id, line.raw_text)
                assert line.evidence in scenario.body, (scenario.scenario_id, line.evidence)

    def test_the_claim_cartouches_carry_no_verdict(self) -> None:
        """Claims arrive PENDING with zero confidence: the core re-derives everything."""
        for scenario in SCENARIOS:
            for line in scenario.lines:
                assert line.status.value == "PENDING"
                assert line.confidence == 0

    def test_every_instant_is_timezone_aware(self) -> None:
        for scenario in SCENARIOS:
            instants = [scenario.received_at, scenario.stock_as_of, scenario.delivery_as_of]
            assert all(
                instant.utcoffset() is not None for instant in instants if instant is not None
            )

    def test_row_identifiers_are_unique_across_scenarios(self) -> None:
        rfq_ids = [scenario.rfq_id for scenario in SCENARIOS]
        run_ids = [scenario.run_id for scenario in SCENARIOS]
        quote_ids = [scenario.quote_id for scenario in SCENARIOS]
        quote_numbers = [scenario.quote_number for scenario in SCENARIOS]
        for identifiers in (rfq_ids, run_ids, quote_ids, quote_numbers):
            assert len(set(identifiers)) == len(identifiers)


class TestFixtureValidation:
    """A malformed fixture fails at construction, never silently mid-demo."""

    @staticmethod
    def _payload(**overrides: object) -> dict[str, object]:
        payload = SCENARIOS[0].model_dump(mode="python")
        payload.update(overrides)
        return payload

    def test_duplicate_is_refused_as_a_declared_outcome(self) -> None:
        with pytest.raises(ValidationError, match="DUPLICATE"):
            DemoScenario.model_validate(self._payload(expected=DemoOutcome.DUPLICATE))

    def test_line_item_ids_must_pair_with_the_claimed_lines(self) -> None:
        with pytest.raises(ValidationError, match="one-to-one"):
            DemoScenario.model_validate(
                self._payload(line_item_ids=("LI_DEMO_0001", "LI_DEMO_9999"))
            )

    def test_line_item_ids_must_be_unique(self) -> None:
        payload = self._payload(
            lines=(SCENARIOS[0].lines[0], SCENARIOS[0].lines[0]),
            line_item_ids=("LI_DEMO_0001", "LI_DEMO_0001"),
        )
        with pytest.raises(ValidationError, match="unique"):
            DemoScenario.model_validate(payload)

    def test_a_naive_instant_is_refused(self) -> None:
        naive = datetime(2026, 10, 1, 6, 0, tzinfo=UTC).replace(tzinfo=None)
        with pytest.raises(ValidationError, match="timezone-aware"):
            DemoScenario.model_validate(self._payload(stock_as_of=naive))

    def test_a_human_binding_requires_a_persisted_expectation(self) -> None:
        with pytest.raises(ValidationError, match="human binding"):
            DemoScenario.model_validate(
                self._payload(
                    expected=DemoOutcome.HUMAN_REQUIRED_BEFORE_QUOTE,
                    human_binding="CUS_0007",
                )
            )

    def test_a_stated_delivery_destination_needs_its_instant(self) -> None:
        delivery = {
            "raw": "next Friday",
            "resolution": "INFERRED",
            "destination": "Hamburg",
        }
        with pytest.raises(ValidationError, match=r"delivery_as_of|the instant it is asked"):
            DemoScenario.model_validate(
                self._payload(requested_delivery=delivery, delivery_as_of=None)
            )
        fixed = self._payload(
            requested_delivery=delivery,
            delivery_as_of=datetime(2026, 10, 6, 9, 0, tzinfo=UTC),
        )
        DemoScenario.model_validate(fixed)


class TestReproducibility:
    def test_the_module_reads_no_clock_and_mints_nothing(self) -> None:
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

    def test_importing_again_produces_equal_scenarios(self) -> None:
        """Fixture data is literals: a reload recomputes nothing."""
        reloaded = importlib.reload(demo.fixtures)
        assert [s.model_dump(mode="json") for s in reloaded.SCENARIOS] == [
            s.model_dump(mode="json") for s in SCENARIOS
        ]
