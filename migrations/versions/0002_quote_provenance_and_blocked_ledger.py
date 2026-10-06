"""Quote price provenance and the projected blocking ledger (Phase 1J', D-1/D-2).

Two accepted decisions, one migration.

**D-1 - price provenance may be absent.** Phase 1H gives a line whose price
lookup did not return ``FOUND`` the sentinel ``price_entry_id = "PRICE_MISSING"``:
there is no price row, and the id says so rather than pointing at one that was
never used. ``0001`` required a real ``price_entries`` row for *every* line, so a
refused line was unwritable. This revision makes ``quote_lines.price_entry_id``
nullable and adds the invariant that pairs it with the status:

    (price_status = 'FOUND') = (price_entry_id IS NOT NULL)

The foreign key stays: a value that *is* present must still be a price entry that
exists. No sentinel row is created, and nothing about the ``FOUND`` shape changed.

**D-2 - the projected ledger needs a home.** ``quote_blocked_reasons`` is one
append-only row per projected reason, in the ledger's order, with the ledger's
flags. It is deliberately none of the things that already exist: not a gate
outcome (``quotes.policy_*``), not a workflow transition (``run_events``, every
row of which must be a legal edge of the state machine), not a human action, and
not an observability record. Its ``UPDATE``/``DELETE`` triggers follow the same
pattern, and use the same wording, as the three append-only tables in ``0001``.

SQLite cannot drop ``NOT NULL``, so the D-1 change is a table rebuild, written
out by hand rather than through ``batch_alter_table``. Two reasons, both
concrete: SQLAlchemy does not reflect ``CHECK`` constraints on SQLite, so a
reflected rebuild would silently drop every guard on this table; and the offline
(``--sql``) path cannot reflect at all.

The rebuild copies into a *new* table and only drops the live one once the copy
has succeeded. That ordering matters because Alembic runs this database with
non-transactional DDL - a mid-migration failure is not rolled back - so the one
statement that can legitimately fail (the copy, which is where ``0001``'s
``NOT NULL`` or this revision's pairing ``CHECK`` rejects a row) must fail while
``quote_lines`` is still intact. The copy is preceded by an idempotent
``DROP TABLE IF EXISTS`` of the scratch name, so an attempt that refused - which
leaves the empty scratch table behind, for the reason above - is repaired by the
next attempt rather than by a claim that the database rolled itself back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The append-only table this revision adds: projected blocking evidence only.
LEDGER_TABLE = "quote_blocked_reasons"

#: ``BlockedReasonCode``'s values, frozen as SQL text. A migration must not
#: import the domain - it records what was true when it ran - and the tuple is
#: used for both the column type and its ``CHECK``, so the two cannot disagree.
_LEDGER_CODES: tuple[str, ...] = (
    "UNKNOWN_SKU",
    "AMBIGUOUS_MATCH",
    "MISSING_QTY",
    "CUSTOMER_UNRESOLVED",
    "CUSTOMER_AMBIGUOUS",
    "PRICE_MISSING",
    "STOCK_INSUFFICIENT",
    "DELIVERY_INFEASIBLE",
    "CURRENCY_MISMATCH",
    "DISCOUNT_OVER_POLICY",
    "MALFORMED_MODEL_OUTPUT",
    "INJECTION_SUSPECTED",
    "CREDIT_HOLD",
)

#: ``code IN (...)`` as SQL text, built from the tuple above so the column type
#: and its ``CHECK`` cannot drift apart.
_LEDGER_CODES_SQL = ", ".join(f"'{code}'" for code in _LEDGER_CODES)

#: What the upgrade says when a stored line contradicts D-1's pairing rule.
_UPGRADE_REFUSAL = (
    "cannot upgrade to revision 0002: at least one quote line names a price entry "
    "while its price status is not FOUND, which D-1 forbids. Clear the provenance of "
    "those rows (or correct their price status) and try again."
)

#: What the downgrade says when the database holds lines ''0001'' cannot represent.
_DOWNGRADE_REFUSAL = (
    "cannot downgrade to revision 0001: this database holds refused quote lines with no "
    "price entry id, which 0001 cannot represent. Keep revision 0002 applied, or remove "
    "those lines first."
)

#: The scratch name the rebuild copies into. The live table is only touched
#: after the copy has succeeded - see :func:`_rebuild_quote_lines`.
_REBUILD_TABLE = "quote_lines_rebuild"

#: Every column of ``quote_lines``, in one place: the rebuild copies all of them.
_QUOTE_LINE_COLUMNS: tuple[str, ...] = (
    "line_id",
    "quote_id",
    "ordinal",
    "product_id",
    "sku",
    "description",
    "quantity",
    "unit_price",
    "price_entry_id",
    "line_extension",
    "currency",
    "stock_status",
    "price_status",
    "blocked",
    "blocked_reason",
    "notes",
)


def _quote_lines_definition(*, nullable_price_entry: bool) -> tuple[sa.Column, ...]:
    """Return ``quote_lines``' columns, with the D-1 change applied or reverted."""
    return (
        sa.Column("line_id", sa.String(length=64), nullable=False),
        sa.Column("quote_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("price_entry_id", sa.String(length=64), nullable=nullable_price_entry),
        sa.Column("line_extension", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "stock_status",
            sa.Enum(
                "SUFFICIENT",
                "PARTIAL",
                "NONE",
                "UNKNOWN",
                name="stock_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "price_status",
            sa.Enum(
                "FOUND",
                "MISSING",
                "AMBIGUOUS",
                "EXPIRED",
                name="price_lookup_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        sa.Column("blocked_reason", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.String(length=300), nullable=True),
    )


def _quote_lines_constraints(*, nullable_price_entry: bool) -> tuple[sa.schema.SchemaItem, ...]:
    """Return ``quote_lines``' constraints, with the D-1 pairing CHECK or without it."""
    constraints: list[sa.schema.SchemaItem] = [
        sa.CheckConstraint(
            "price_status = 'FOUND' OR blocked = 1",
            name=op.f("ck_quote_lines_unusable_price_blocks_line"),
        ),
        sa.CheckConstraint(
            "price_status IN ('FOUND', 'MISSING', 'AMBIGUOUS', 'EXPIRED')",
            name=op.f("ck_quote_lines_price_lookup_status"),
        ),
        sa.CheckConstraint(
            "stock_status <> 'NONE' OR blocked = 1",
            name=op.f("ck_quote_lines_no_stock_blocks_line"),
        ),
        sa.CheckConstraint(
            "stock_status IN ('SUFFICIENT', 'PARTIAL', 'NONE', 'UNKNOWN')",
            name=op.f("ck_quote_lines_stock_status"),
        ),
        sa.CheckConstraint(
            "(blocked = 1 AND blocked_reason IS NOT NULL) "
            "OR (blocked = 0 AND blocked_reason IS NULL)",
            name=op.f("ck_quote_lines_blocked_requires_reason"),
        ),
    ]
    if nullable_price_entry:
        constraints.append(
            sa.CheckConstraint(
                "(price_status = 'FOUND') = (price_entry_id IS NOT NULL)",
                name=op.f("ck_quote_lines_price_provenance_pairing"),
            )
        )
    constraints.extend(
        [
            sa.CheckConstraint("length(currency) = 3", name=op.f("ck_quote_lines_currency_len")),
            sa.CheckConstraint(
                "line_extension >= 0",
                name=op.f("ck_quote_lines_line_extension_non_negative"),
            ),
            sa.CheckConstraint("ordinal >= 1", name=op.f("ck_quote_lines_ordinal_positive")),
            sa.CheckConstraint("quantity >= 1", name=op.f("ck_quote_lines_quantity_positive")),
            sa.CheckConstraint(
                "unit_price >= 0", name=op.f("ck_quote_lines_unit_price_non_negative")
            ),
            sa.ForeignKeyConstraint(
                ["price_entry_id"],
                ["price_entries.price_entry_id"],
                name=op.f("fk_quote_lines_price_entry_id_price_entries"),
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["product_id"],
                ["products.product_id"],
                name=op.f("fk_quote_lines_product_id_products"),
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["quote_id"],
                ["quotes.quote_id"],
                name=op.f("fk_quote_lines_quote_id_quotes"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("line_id", name=op.f("pk_quote_lines")),
            sa.UniqueConstraint("quote_id", "ordinal", name="uq_quote_lines_quote_id_ordinal"),
        ]
    )
    return tuple(constraints)


def _refuse_if_the_copy_could_not_succeed(*, nullable_price_entry: bool) -> None:
    """Refuse *before* any DDL when no row could be copied anyway.

    The refusal has to come first because this database runs with
    non-transactional DDL: a rebuild that starts and then fails leaves a scratch
    table behind, and a migration that has to be finished by hand is a worse
    outcome than one that declines cleanly. Asking first costs one query and
    changes nothing - it checks representability at the target revision, which is
    the same invariant the copy enforces one row later, not a business rule.
    """
    context = op.get_context()
    if context.as_sql:
        # Offline mode emits DDL and never looks at a database, so there is
        # nothing to ask. The statement below would have no rows to read.
        return
    if nullable_price_entry:
        statement = (
            "SELECT COUNT(*) FROM quote_lines "
            "WHERE (price_status = 'FOUND') <> (price_entry_id IS NOT NULL)"
        )
        reason = _UPGRADE_REFUSAL
    else:
        statement = "SELECT COUNT(*) FROM quote_lines WHERE price_entry_id IS NULL"
        reason = _DOWNGRADE_REFUSAL
    if op.get_bind().execute(sa.text(statement)).scalar_one():
        raise RuntimeError(reason)


def _rebuild_quote_lines(*, nullable_price_entry: bool) -> None:
    """Recreate ``quote_lines`` with (or without) a nullable price reference.

    Rows are copied, never reinterpreted: a refused line keeps its ``NULL``
    provenance going up. The copy is guarded twice over - the pre-check refuses
    when *no* row could be represented, and the copy itself still fails if the
    database disagrees - and both fail while the live table is untouched.
    """
    reason = _UPGRADE_REFUSAL if nullable_price_entry else _DOWNGRADE_REFUSAL
    _refuse_if_the_copy_could_not_succeed(nullable_price_entry=nullable_price_entry)

    columns = ", ".join(_QUOTE_LINE_COLUMNS)
    # A scratch table from an interrupted run is dropped first, idempotently:
    # statements issued after a failing copy are not reliably committed on this
    # connection, so a retry has to start clean rather than assume it will.
    op.execute(f"DROP TABLE IF EXISTS {_REBUILD_TABLE}")
    op.create_table(
        _REBUILD_TABLE,
        *_quote_lines_definition(nullable_price_entry=nullable_price_entry),
        *_quote_lines_constraints(nullable_price_entry=nullable_price_entry),
    )
    try:
        op.execute(
            f"INSERT INTO {_REBUILD_TABLE} ({columns}) "  # noqa: S608 - frozen names above
            f"SELECT {columns} FROM quote_lines"
        )
    except sa.exc.IntegrityError as error:
        raise RuntimeError(reason) from error

    op.drop_table("quote_lines")
    op.rename_table(_REBUILD_TABLE, "quote_lines")


def _create_ledger_triggers() -> None:
    """Install the ``UPDATE``/``DELETE`` guards, in ``0001``'s wording.

    An append-only table enforced only by convention is a claim; a trigger is
    what makes it true for every writer, including a human at a ``sqlite3``
    prompt.
    """
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{LEDGER_TABLE}_no_{operation.lower()} "
            f"BEFORE {operation} ON {LEDGER_TABLE} "
            f"BEGIN SELECT RAISE(ABORT, "
            f"'{LEDGER_TABLE} is append-only: {operation} is not permitted'); END"
        )


def _drop_ledger_triggers() -> None:
    """Drop the guards this revision installed, so the downgrade is honest."""
    for operation in ("UPDATE", "DELETE"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{LEDGER_TABLE}_no_{operation.lower()}")


def upgrade() -> None:
    """Make price provenance optional (D-1) and add the ledger table (D-2)."""
    _rebuild_quote_lines(nullable_price_entry=True)

    op.create_table(
        LEDGER_TABLE,
        sa.Column("quote_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column(
            "code",
            sa.Enum(
                *_LEDGER_CODES,
                name="blocked_reason_code",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("line_ordinal", sa.Integer(), nullable=True),
        sa.Column("resolvable_by_human", sa.Boolean(), nullable=False),
        sa.Column("flags_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"code IN ({_LEDGER_CODES_SQL})",
            name=op.f("ck_quote_blocked_reasons_blocked_reason_code"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_quote_blocked_reasons_seq_positive")),
        sa.CheckConstraint(
            "length(message) BETWEEN 1 AND 300",
            name=op.f("ck_quote_blocked_reasons_message_len"),
        ),
        sa.CheckConstraint(
            "line_ordinal IS NULL OR line_ordinal >= 1",
            name=op.f("ck_quote_blocked_reasons_line_ordinal_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["quotes.quote_id"],
            name=op.f("fk_quote_blocked_reasons_quote_id_quotes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.run_id"],
            name=op.f("fk_quote_blocked_reasons_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("quote_id", "seq", name=op.f("pk_quote_blocked_reasons")),
        sa.UniqueConstraint("quote_id", "code", name="uq_quote_blocked_reasons_quote_id_code"),
    )
    op.create_index("ix_quote_blocked_reasons_run_id", LEDGER_TABLE, ["run_id"], unique=False)
    _create_ledger_triggers()


def downgrade() -> None:
    """Restore ``0001``'s not-null price reference, then remove the ledger table.

    The order is the point: the rebuild is the one step that can legitimately
    refuse, so it runs while nothing has been removed yet, and a refusal leaves
    the ledger - and the quote lines - exactly as they were. The drops that
    follow cannot fail, and are written ``IF EXISTS`` so that a database left in
    a half-applied state by an interrupted run can still be brought down.
    """
    _rebuild_quote_lines(nullable_price_entry=False)

    _drop_ledger_triggers()
    op.execute(f"DROP INDEX IF EXISTS ix_{LEDGER_TABLE}_run_id")
    op.execute(f"DROP TABLE IF EXISTS {LEDGER_TABLE}")
