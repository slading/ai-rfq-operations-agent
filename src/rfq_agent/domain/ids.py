"""Typed identifier value objects.

Identifiers are ``str`` subclasses constrained by pattern, so a ``ProductId``
can never be silently assigned where a ``CustomerId`` is expected and an
arbitrary model-generated string can never enter the domain as an identifier.

The concrete formats used by the seed data are established in Phase 1; the
patterns below are the validation contract that Phase 1 must satisfy.
"""

from typing import Annotated

from pydantic import StringConstraints

__all__ = [
    "CustomerId",
    "IdempotencyKey",
    "LineItemId",
    "PriceEntryId",
    "ProductId",
    "QuoteId",
    "QuoteNumber",
    "RfqId",
    "RunId",
    "TraceId",
]

_IDENTIFIER = StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_\-]{2,63}$")
_OPAQUE_KEY = StringConstraints(min_length=16, max_length=256)
_HEX32 = StringConstraints(pattern=r"^[0-9a-f]{32}$")

#: Primary key of an inbound request for quotation.
RfqId = Annotated[str, _IDENTIFIER]
#: Primary key of one execution attempt against an RFQ.
RunId = Annotated[str, _IDENTIFIER]
#: Customer master-data record identifier (never invented by a model).
CustomerId = Annotated[str, _IDENTIFIER]
#: Catalog product identifier (never invented by a model).
ProductId = Annotated[str, _IDENTIFIER]
#: Specific price-book entry a unit price was read from.
PriceEntryId = Annotated[str, _IDENTIFIER]
#: Extracted line item within a run.
LineItemId = Annotated[str, _IDENTIFIER]
#: Computed quote identifier.
QuoteId = Annotated[str, _IDENTIFIER]
#: Human-visible quote reference, e.g. ``Q-2026-000123``.
QuoteNumber = Annotated[str, StringConstraints(pattern=r"^Q-\d{4}-\d{4,8}$")]
#: Idempotency key for intake de-duplication and for human actions.
IdempotencyKey = Annotated[str, _OPAQUE_KEY]
#: W3C-compatible trace identifier: 16 bytes, 32 lowercase hex characters.
TraceId = Annotated[str, _HEX32]
