"""Fixtures for the demo tests.

Imported from the persistence test package rather than redefined: "a database
built by the real migrations" is one idea in this suite and has one definition.
"""

from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401  (imported as fixtures)
    database_path,
    db,
    migrated_template,
    session,
)
