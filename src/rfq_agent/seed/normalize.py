"""The alias normaliser, re-exported from the domain.

The implementation moved to :func:`rfq_agent.domain.resolution.normalize_alias`
in Phase 1C, when the read repositories became its second consumer. A rule about
how aliases are *stored* is not the seed's property, and one implementation is
the only way the ``normalized`` columns and the lookups against them can agree.

This module stays so that :mod:`rfq_agent.seed` keeps the surface it documented
in Phase 1B (``from rfq_agent.seed import normalize_alias``), and so that the
dataset's own code does not have to import the domain module it happens to share
a function with. It intentionally contains no logic.
"""

from __future__ import annotations

from rfq_agent.domain.resolution import normalize_alias

__all__ = ["normalize_alias"]
