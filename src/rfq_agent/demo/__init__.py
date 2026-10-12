"""The fixture-driven RFQ-to-DRAFT demonstration (Phase 1M-C).

The smallest human-runnable walk through the accepted chain, outside pytest:
one typed, versioned fixture per scenario stands in for the untrusted intake
and the claims an LLM would extract from it, and the accepted components do
every rule-bound step - customer binding and line resolution (Phase 1M-B), the
quote request adapter (Phase 1M-A), the deterministic quote run (Phase 1L) and
the atomic write path (Phase 1J').

Typical use (against a migrated and seeded database)::

    python -m rfq_agent.demo                     # run every scenario
    python -m rfq_agent.demo --scenario SC-01    # run one

Or from the repository root: ``make migrate && make seed && make demo``.

The demonstration persists DRAFT quotations and their blocking evidence, and
nothing else: no approval, no outbound message, no run-state transition, and
no stored gate decision - the gate's verdict is printed, never persisted.
"""

from __future__ import annotations

from rfq_agent.demo.errors import DemoError
from rfq_agent.demo.fixtures import SCENARIOS, DemoOutcome, DemoScenario, scenario_by_id
from rfq_agent.demo.intake import provision_run, require_prerequisites
from rfq_agent.demo.runner import ScenarioReport, run_scenario

__all__ = [
    "SCENARIOS",
    "DemoError",
    "DemoOutcome",
    "DemoScenario",
    "ScenarioReport",
    "provision_run",
    "require_prerequisites",
    "run_scenario",
    "scenario_by_id",
]
