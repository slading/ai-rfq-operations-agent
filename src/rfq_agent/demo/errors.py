"""Controlled error types for demo CLI failures."""

from __future__ import annotations

__all__ = ["DemoError", "DemoPrerequisiteError", "UnknownScenarioError"]


class DemoError(Exception):
    """Base class for demo failures; messages must not be rendered by the CLI."""


class DemoPrerequisiteError(DemoError):
    """Safe category for missing schema or demo dataset prerequisites."""


class UnknownScenarioError(DemoError):
    """Safe category for an unrecognized scenario selector."""
