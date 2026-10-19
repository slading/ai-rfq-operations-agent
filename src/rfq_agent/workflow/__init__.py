"""Workflow persistence primitives — V1-01a1.

This package holds the durable run-transition primitive. It is internal
and does not expose a general unguarded API. Future guarded business services
must verify workflow guards before invoking the internal writer.
"""

from __future__ import annotations
