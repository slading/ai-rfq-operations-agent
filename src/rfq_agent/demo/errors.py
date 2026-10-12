"""The demo command's failure contract: actionable messages, never tracebacks."""

from __future__ import annotations

__all__ = ["DemoError"]


class DemoError(Exception):
    """A demonstration failure the operator can act on.

    The command reports it on stderr and exits 2, mirroring the seed command's
    ``SeedError``. Contract violations raised by the accepted components
    themselves (``ValueError``) are reported separately as data defects.
    """
