"""Tests for the Northwind Components dataset.

Fixtures are imported from the persistence test package rather than redefined:
"a database built by the real migrations" is one idea in this suite and should
have one definition. Importing the fixture functions into this module's namespace
is what makes pytest see them for the tests in this directory.
"""

from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401  (imported as fixtures)
    connection,
    database_path,
    db,
    inspector,
    migrated_template,
    session,
)
