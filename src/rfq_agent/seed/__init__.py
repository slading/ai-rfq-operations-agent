"""The Northwind Components demo dataset - versioned business master data.

The dataset is what the deterministic core reads: customers, catalogue, prices,
stock, carriers and calendars. It is deliberately *data*, not generators: every
value is a literal in :mod:`rfq_agent.seed.dataset`, checked into the repository
and versioned by :data:`SEED_VERSION`, so "the demo database" is reproducible and
an evaluation run recorded against one version can be identified by it.

Typical use::

    from rfq_agent.persistence import Database
    from rfq_agent.seed import reset_and_seed

    database = Database.create("sqlite:///var/rfq_agent.db")
    with database.session() as session:
        report = reset_and_seed(session)
    print(report.format())

From a shell, the equivalent is ``make migrate`` followed by ``make seed`` (or
``python -m rfq_agent.seed --reset``).
"""

from __future__ import annotations

from rfq_agent.seed.dataset import (
    CALENDAR_YEAR,
    CURRENCY,
    DATASET_NAME,
    PRICE_BOOK_CONTRACT,
    PRICE_BOOK_LIST,
    SEED_VERSION,
    STOCK_AS_OF,
    TIER_STANDARD,
    row_counts,
    table_names,
    total_rows,
)
from rfq_agent.seed.loader import (
    SeedError,
    SeedReport,
    SeedResetBlockedError,
    SeedSchemaError,
    TableReport,
    require_schema,
    reset,
    reset_and_seed,
    seed,
)
from rfq_agent.seed.normalize import normalize_alias

__all__ = [
    "CALENDAR_YEAR",
    "CURRENCY",
    "DATASET_NAME",
    "PRICE_BOOK_CONTRACT",
    "PRICE_BOOK_LIST",
    "SEED_VERSION",
    "STOCK_AS_OF",
    "TIER_STANDARD",
    "SeedError",
    "SeedReport",
    "SeedResetBlockedError",
    "SeedSchemaError",
    "TableReport",
    "normalize_alias",
    "require_schema",
    "reset",
    "reset_and_seed",
    "row_counts",
    "seed",
    "table_names",
    "total_rows",
]
