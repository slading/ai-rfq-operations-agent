"""The fixture-driven RFQ-to-DRAFT demonstration (Phase 1M-C).

``python -m rfq_agent.demo`` (or ``make demo``) walks typed, versioned fixture
scenarios through the accepted RFQ-to-DRAFT chain against a migrated and
seeded database (see README for the walkthrough).

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
