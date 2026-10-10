"""Tests for the package surface itself.

Cheap, but they catch the two failures that silently rot a project: a module
that no longer imports, and an ``__all__`` that lies about what a module exports.
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
from types import ModuleType

import pytest

import rfq_agent

MODULES = [
    "rfq_agent.config",
    "rfq_agent.contracts",
    "rfq_agent.contracts.errors",
    "rfq_agent.contracts.llm",
    "rfq_agent.contracts.ports",
    "rfq_agent.contracts.testing",
    "rfq_agent.domain",
    "rfq_agent.domain.delivery",
    "rfq_agent.domain.extraction",
    "rfq_agent.domain.gating",
    "rfq_agent.domain.human",
    "rfq_agent.domain.ids",
    "rfq_agent.domain.intake",
    "rfq_agent.domain.outbound",
    "rfq_agent.domain.policy",
    "rfq_agent.domain.pricing",
    "rfq_agent.domain.quote",
    "rfq_agent.domain.resolution",
    "rfq_agent.domain.stock",
    "rfq_agent.domain.trust",
    "rfq_agent.domain.values",
    "rfq_agent.domain.workflow",
    "rfq_agent.observability",
    "rfq_agent.observability.ids",
    "rfq_agent.observability.redaction",
    "rfq_agent.observability.spans",
    "rfq_agent.persistence",
    "rfq_agent.persistence.base",
    "rfq_agent.persistence.engine",
    "rfq_agent.persistence.enums",
    "rfq_agent.persistence.models",
    "rfq_agent.persistence.models.catalog",
    "rfq_agent.persistence.models.customers",
    "rfq_agent.persistence.models.human",
    "rfq_agent.persistence.models.logistics",
    "rfq_agent.persistence.models.outbound",
    "rfq_agent.persistence.models.pricing",
    "rfq_agent.persistence.models.quote",
    "rfq_agent.persistence.models.rfq",
    "rfq_agent.persistence.models.run",
    "rfq_agent.persistence.models.trace",
    "rfq_agent.persistence.read_models",
    "rfq_agent.persistence.repositories",
    "rfq_agent.persistence.types",
    "rfq_agent.persistence.writers",
    "rfq_agent.quote_adapter",
    "rfq_agent.quoting",
    "rfq_agent.seed",
    "rfq_agent.seed.dataset",
    "rfq_agent.seed.loader",
    "rfq_agent.seed.normalize",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


@pytest.mark.parametrize("name", MODULES)
def test_all_is_accurate(name: str) -> None:
    module = importlib.import_module(name)
    exported = getattr(module, "__all__", None)
    if exported is None:
        pytest.skip(f"{name} declares no __all__")
    missing = [symbol for symbol in exported if not hasattr(module, symbol)]
    assert missing == [], f"{name}.__all__ references missing names: {missing}"


def test_no_submodule_is_missing_from_the_list() -> None:
    """Guard against adding a module and forgetting to cover it here."""
    package: ModuleType = rfq_agent
    discovered = {
        info.name
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}.")
        if not info.name.endswith("__main__")
    }
    assert discovered - set(MODULES) == set(), "new modules must be added to MODULES"


def test_version_is_declared() -> None:
    assert rfq_agent.__version__ == "0.1.0"


def test_no_layer_without_a_phase_exists_yet() -> None:
    """Layers that no authorised phase owns yet must not exist.

    This began as the Phase 0 gate: the skeleton must not quietly grow a
    database, a provider client or a UI before the phase that owns them. The
    list shrinks as those phases are authorised and accepted -
    ``repositories.py`` left it in Phase 1C, when the read boundary was exactly
    the authorised work - and what remains is what still has no phase behind it:
    a provider client, a UI, and the workflow/tool layers.
    """
    root = pathlib.Path(rfq_agent.__path__[0])
    tree = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    forbidden = ("db/", "groq", "ui/", "workflow_engine", "tools/")
    present = [entry for entry in tree if any(bad in entry for bad in forbidden)]
    assert present == [], f"No phase owns these yet: {present}"

    # The one module that exists under a name this check used to forbid, so its
    # presence is asserted rather than merely tolerated.
    assert "persistence/repositories.py" in tree
