"""Demo-specific failures (Phase 1M-C).

One error type, one contract: a :class:`DemoError` message is written to
stderr and ends the command with exit code 2, because the next step is a
command the operator can run, not a traceback a developer must read. This
mirrors :class:`~rfq_agent.seed.loader.SeedError` in the seed command.
"""

from __future__ import annotations

__all__ = ["DemoError"]


class DemoError(Exception):
    """A demonstration failure the operator can act on.

    Raised for a missing schema or dataset, a fixture whose stated identifiers
    collide with different stored rows, a scenario that produced an outcome
    its fixture did not declare, and an unknown scenario name on the command
    line. Contract violations raised by the accepted components themselves
    (``ValueError``) are reported by the command as data defects instead.
    """
