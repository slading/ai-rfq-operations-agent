"""The seam the writer sits on: reads hand back values, and one module writes.

Phase 1I established that the blocking ledger is a projection and nothing more.
This module checks the other half of the 1J' boundary, from the outside in: the
read side still returns value objects and never touches the session, the write
side is a single module - the only one in the package that performs DML - and the
idempotency store that makes a retry safe matches the port the contracts declare.

The structural checks parse the modules rather than trusting a convention. A
write helper added to a read repository would be a real regression, and a test
that only reads behaviour would miss the one that is never called.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rfq_agent.contracts.ports import IdempotencyStore
from rfq_agent.persistence import Database, repositories, writers
from rfq_agent.persistence import read_models as read_models_module
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.persistence.writers import SqlIdempotencyStore
from rfq_agent.seed import reset_and_seed

#: Where the package lives, for the structural checks.
PACKAGE = Path(repositories.__file__).resolve().parent

#: The calls that change data. ``begin``/``commit``/``rollback`` are not here:
#: deciding *when* a unit of work ends is the engine's job, and it does exactly
#: that in ``session_scope``.
_DML_HELPERS = frozenset({"add", "add_all", "delete", "flush", "insert", "merge", "update"})

#: The only module in the package allowed to perform them.
_WRITER_MODULE = "writers.py"


def _sources() -> dict[str, str]:
    """Every Python module in the persistence package, by file name."""
    return {path.name: path.read_text() for path in sorted(PACKAGE.rglob("*.py"))}


def _imported_from_sqlalchemy(source: str) -> set[str]:
    """Every name the module imports from ``sqlalchemy``, type-checking block included."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.startswith("sqlalchemy"))
    return names


def _called_attributes(source: str) -> set[str]:
    """Every attribute the module calls, e.g. ``{"add", "commit", "scalar"}``."""
    called: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    return called


def _leaves(value: object) -> Iterator[object]:
    """Every value inside a result - tuples and value objects - where rows would hide."""
    if isinstance(value, tuple):
        for item in value:
            yield from _leaves(item)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _leaves(getattr(value, field.name))
    else:
        yield value


@pytest.fixture
def reader_session(db: Database) -> Iterator[Session]:
    """A session the reader is allowed to use, fresh and seeded."""
    with db.session_factory() as active:
        reset_and_seed(active)
        active.commit()
        active.rollback()  # end the write transaction; the reads below are the point
        yield active


@pytest.fixture
def reader(reader_session: Session) -> BusinessReader:
    """The read boundary, over that session."""
    return BusinessReader.for_session(reader_session)


#: One call per read the boundary offers, so the sweep cannot miss a method.
READS: dict[str, Callable[[BusinessReader], object]] = {
    "customers.get": lambda reader: reader.customers.get("CUS_0001"),
    "customers.search": lambda reader: reader.customers.search("Nordwind"),
    "catalog.get": lambda reader: reader.catalog.get("PRD_0001"),
    "catalog.search": lambda reader: reader.catalog.search("pump"),
    "catalog.family": lambda reader: reader.catalog.family("FAM_PUMPS"),
    "catalog.families": lambda reader: reader.catalog.families(),
    "pricing.book": lambda reader: reader.pricing.book("BK-EU-2026"),
    "pricing.books": lambda reader: reader.pricing.books(),
    "pricing.entry": lambda reader: reader.pricing.entry("PE_0001"),
    "pricing.entries_for_products": lambda reader: reader.pricing.entries_for_products(
        ["PRD_0001"]
    ),
    "stock.warehouse": lambda reader: reader.stock.warehouse("WAW"),
    "stock.warehouses": lambda reader: reader.stock.warehouses(),
    "stock.level": lambda reader: reader.stock.level("WAW", "PRD_0001"),
    "stock.levels_for_products": lambda reader: reader.stock.levels_for_products(["PRD_0001"]),
    "delivery.service": lambda reader: reader.delivery.service("DHL-EXP"),
    "delivery.services": lambda reader: reader.delivery.services(),
    "delivery.services_from": lambda reader: reader.delivery.services_from("WAW"),
    "delivery.holidays": lambda reader: reader.delivery.holidays(["DE"]),
    "discounts.rule": lambda reader: reader.discounts.rule("DSC_0001"),
    "discounts.rules": lambda reader: reader.discounts.rules(),
}


# ---------------------------------------------------------------------------
# Structural: the package's shape
# ---------------------------------------------------------------------------


def test_the_read_models_import_nothing_from_sqlalchemy() -> None:
    """A read model that could hold a row is a read model that one day will."""
    source = PACKAGE.joinpath("read_models.py").read_text()
    assert _imported_from_sqlalchemy(source) == set()
    assert read_models_module.__name__.endswith("read_models")


def test_the_repositories_import_only_the_read_side_of_sqlalchemy() -> None:
    """``select`` to ask, ``Session`` to ask with - nothing that can change a row."""
    assert _imported_from_sqlalchemy(PACKAGE.joinpath("repositories.py").read_text()) <= {
        "select",
        "Session",
    }


def test_the_repositories_call_no_write_helper() -> None:
    """The read boundary is read-only by construction, not by comment."""
    called = _called_attributes(PACKAGE.joinpath("repositories.py").read_text())
    assert called & _DML_HELPERS == set()


def test_one_module_in_the_package_performs_data_changes() -> None:
    """``writers.py`` is the write path: everywhere else, DML would be a defect.

    The scan covers the whole package, including the models and the engine, so a
    second writer cannot appear quietly. The engine is deliberately in scope and
    deliberately clean: it owns *when* a unit of work ends, not what goes in it.
    """
    writing = {
        name for name, source in _sources().items() if _called_attributes(source) & _DML_HELPERS
    }
    assert writing == {_WRITER_MODULE}


def test_every_read_model_is_a_frozen_slotted_value_object() -> None:
    """Values, not entities: immutable, slotted, and nobody's identity map."""
    value_objects = [
        member
        for _, member in inspect.getmembers(read_models_module, inspect.isclass)
        if dataclasses.is_dataclass(member) and member.__module__ == read_models_module.__name__
    ]
    assert len(value_objects) >= 12
    for value_object in value_objects:
        assert value_object.__dataclass_params__.frozen, value_object.__name__
        assert value_object.__slots__, value_object.__name__


# ---------------------------------------------------------------------------
# Behavioural: reads stay reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(READS), ids=list(READS))
def test_every_read_hands_back_value_objects(name: str, reader: BusinessReader) -> None:
    """Nothing that comes out of the boundary is an ORM row, at any depth."""
    value = READS[name](reader)
    assert not isinstance(value, Base)
    for leaf in _leaves(value):
        assert not isinstance(leaf, Base), f"{name} returned {type(leaf).__name__}"


def test_every_read_returns_something(reader: BusinessReader) -> None:
    """A spot check that the sweep above is not asserting over empty tuples."""
    assert reader.customers.get("CUS_0001") is not None
    assert reader.catalog.families()
    assert reader.pricing.entries_for_products(["PRD_0001"])
    assert reader.stock.levels_for_products(["PRD_0001"])
    assert reader.delivery.services()
    assert reader.delivery.holidays(["DE"])
    assert reader.discounts.rules()


def test_a_read_installs_nothing_in_the_session(
    reader: BusinessReader, reader_session: Session
) -> None:
    """Reading is not writing, in the sense the session itself can see."""
    for read in READS.values():
        read(reader)

    assert list(reader_session.new) == []
    assert list(reader_session.dirty) == []
    assert list(reader_session.deleted) == []


def test_the_boundary_reads_the_session_it_was_given(session: Session) -> None:
    """``for_session`` binds a boundary to one session; it does not open its own.

    The session here is the migrated-but-unseeded one, so the two boundaries can
    be told apart by what they can see: the seeded reader finds the customer, the
    fresh one genuinely finds nothing.
    """
    assert BusinessReader.for_session(session).customers.get("CUS_0001") is None


# ---------------------------------------------------------------------------
# The idempotency store matches the port
# ---------------------------------------------------------------------------


def test_the_idempotency_store_implements_the_accepted_port(session: Session) -> None:
    """The port is the contract; this is the implementation the writer uses."""
    store = SqlIdempotencyStore(session)
    assert isinstance(store, IdempotencyStore)
    for method in ("claim", "release", "is_claimed"):
        expected = inspect.signature(getattr(IdempotencyStore, method))
        actual = inspect.signature(getattr(SqlIdempotencyStore, method))
        assert [parameter.name for parameter in actual.parameters.values()] == [
            parameter.name for parameter in expected.parameters.values()
        ], method


def test_the_writer_module_declares_the_scope_its_claims_are_filed_under() -> None:
    """A claim key means different things in different scopes, so the scope is named."""
    assert writers.CLAIM_SCOPE == "quote_persist"


def test_a_claim_key_shorter_than_the_schema_allows_is_refused(session: Session) -> None:
    """``length(claim_key) >= 8`` is the schema's own floor, and it is still enforced."""
    store = SqlIdempotencyStore(session)
    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        store.claim("short", scope=writers.CLAIM_SCOPE)
