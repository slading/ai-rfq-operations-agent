"""Column types shared by every persistence model (architecture §5.1).

Three concerns are handled here once, so that no table can get them subtly
wrong:

* **Time** - :class:`UtcDateTime` refuses a naive ``datetime`` on the way in and
  always returns a timezone-aware UTC value on the way out. SQLite has no native
  timestamp type, so without this decorator a naive value would be silently
  stored and silently read back as if it were local time. Timezone bugs are
  exactly the class of defect that makes an audit trail untrustworthy.
* **Money** - money is never a float in the domain (:func:`money_field`), so the
  storage types are declared centrally with the same precision the domain uses.
* **Enums** - every enum column stores the enum *value* (not the member name)
  and carries a ``CHECK`` constraint, so an invalid value cannot be written even
  by a hand-written ``UPDATE``. ``TransitionEvent`` is the reason this matters:
  its member names and values differ (``TRIAGE_START`` / ``"triage_start"``), so
  a column that silently stored names would look correct until the first read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Enum, Integer, Numeric
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import Dialect

__all__ = [
    "JSON_PAYLOAD",
    "MONEY",
    "PERCENT",
    "QUANTITY",
    "UNIT_PRICE",
    "UtcDateTime",
    "enum_type",
]

#: Monetary amounts: 12 integer digits, 2 decimals - matches ``money_field()``.
#:
#: SQLite has no native decimal type: ``NUMERIC`` columns are stored as IEEE-754
#: doubles. That is acceptable here *because* every value is a 2-decimal decimal
#: below 10^13, where a float64 round-trips through ``"%.2f"`` exactly.
#: ``tests/persistence/test_round_trip.py`` pins that claim at the boundaries.
#: A derived-money ``CHECK`` (``total = subtotal - discount``) is deliberately
#: *not* used: exact equality between SQLite float arithmetic and the domain's
#: ``Decimal`` arithmetic is not guaranteed at every magnitude, and a constraint
#: that rejects a correct quotation is worse than no constraint. Money
#: arithmetic is enforced by :class:`~rfq_agent.domain.quote.Quote` in Python.
MONEY = Numeric(14, 2)

#: Unit prices carry four decimals (per-unit economics); extensions do not.
UNIT_PRICE = Numeric(14, 4)

#: Percentages, e.g. a discount rate.
PERCENT = Numeric(5, 2)

#: Quantities and counts. Whole units only.
QUANTITY = Integer

#: Structured payload columns (diffs, findings, redacted snapshots).
#:
#: ``none_as_null=True`` stores a Python ``None`` as SQL ``NULL`` rather than as
#: the four characters ``null``. That distinction is load-bearing: the
#: ``human_actions`` constraints ask ``before_json IS NULL``, and a column that
#: wrote the JSON literal would satisfy the wrong branch and let a non-EDIT
#: action masquerade as a diff-carrying one. A literal JSON null is never
#: needed here - "absent" and "null" mean the same thing to this schema.
JSON_PAYLOAD = JSON(none_as_null=True)


class UtcDateTime(TypeDecorator[datetime]):
    """A ``datetime`` column that is always timezone-aware UTC.

    Bind: an aware value in any timezone is converted to UTC and stored naive
    (SQLite has no tz-aware storage). A naive value raises, because silently
    guessing a timezone is how "the quote expired an hour early" bugs happen.

    Result: the stored UTC value is returned with ``tzinfo=UTC`` attached.
    """

    # The ``_dialect`` parameters are part of the ``TypeDecorator`` contract.
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        """Normalise an aware datetime to naive UTC, refusing naive input."""
        if value is None:
            return None
        if not isinstance(value, datetime):
            msg = f"expected datetime, got {type(value).__name__}"
            raise TypeError(msg)
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            msg = "naive datetimes are not accepted; attach tzinfo (use utc_now())"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        """Re-attach UTC to a value read back from the database."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _values_callable(enum_cls: type[StrEnum]) -> list[str]:
    """Return the stored strings for :class:`~sqlalchemy.Enum`.

    Passing this to ``values_callable`` keeps the Python-side enum binding (so
    reads return members) while persisting ``member.value`` rather than
    ``member.name``. Without it, ``TransitionEvent.TRIAGE_START`` would be
    written as ``"TRIAGE_START"`` and read back as ``None``.
    """
    return [member.value for member in enum_cls]


def enum_type(enum_cls: type[StrEnum], *, name: str) -> Enum:
    """Build a ``VARCHAR`` + ``CHECK`` column type for ``enum_cls``.

    ``native_enum=False`` selects a portable ``VARCHAR``; ``create_constraint``
    adds the ``CHECK (col IN (...))`` guard, which is the part that actually
    protects the data. The ``name`` must be unique per enum and is used to build
    a deterministic constraint name via the metadata naming convention.
    """
    values = _values_callable(enum_cls)
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=max(len(value) for value in values),
        values_callable=_values_callable,
    )
