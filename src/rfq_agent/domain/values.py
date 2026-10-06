"""Small shared value objects and helpers used across the domain layer."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Json",
    "canonical_json",
    "fingerprint",
    "money_field",
    "sha256_bytes",
    "sha256_text",
]

#: Any JSON-serialisable value. Used for provider-neutral payloads.
Json = Any


class DomainModel(BaseModel):
    """Base class for all domain schemas.

    Strict-by-default choices shared by every schema in the project:

    * ``extra="forbid"`` - an unexpected field from a model or a client is an
      error, never silently dropped. This is the first line of defence against
      a model "adding" information that no tool ever returned.
    * ``frozen=True`` - domain objects are immutable value objects; state
      changes are expressed as new objects plus audit events, never mutation.
    * ``validate_assignment=True`` - reassignment cannot smuggle in a bad value.

    Whitespace stripping is deliberately *not* enabled globally: it would mutate
    customer content before its digest is taken, breaking the hash invariant on
    :class:`~rfq_agent.domain.trust.UntrustedText` and silently altering the
    evidence spans the grounding gate verifies. Normalisation happens explicitly,
    in named places, in Phase 1.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
    )


def sha256_text(value: str) -> str:
    """Return the lowercase hex SHA-256 digest of ``value`` (UTF-8)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of raw ``value``."""
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Json) -> str:
    """Serialise ``value`` to deterministic JSON.

    Keys are sorted and separators are fixed so that two structurally equal
    payloads always produce the same bytes. This is what makes
    :func:`fingerprint` stable enough to assert "same inputs, same quote".
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def fingerprint(value: Json) -> str:
    """Return a stable SHA-256 fingerprint of a JSON-serialisable payload.

    Used for ``quotes.inputs_sha256``, prompt hashes and tool-result hashes:
    verifiable without storing (or logging) the underlying content.
    """
    return sha256_text(canonical_json(value))


def money_field(
    *,
    ge: Decimal = Decimal("0"),
    max_digits: int = 14,
    decimal_places: int = 2,
    allow_float: bool = False,
) -> Any:
    """Build an ``Annotated`` money type that refuses binary floats.

    Monetary values must arrive as ``Decimal``, ``int`` or a numeric ``str``.
    Accepting a float would import binary representation error into a customer
    quotation, so floats are rejected unless a caller explicitly opts in.
    """

    def _reject_float(value: object) -> object:
        if isinstance(value, float) and not allow_float:
            msg = "monetary values must be Decimal, int or str - never float"
            raise ValueError(msg)
        return value

    constraints: list[Any] = [
        Field(ge=ge, max_digits=max_digits, decimal_places=decimal_places),
        BeforeValidator(_reject_float),
    ]
    return Annotated[Decimal, *constraints]


def as_mapping(value: Json) -> Mapping[str, Json]:
    """Coerce a decoded JSON value into a read-only mapping, or raise ``TypeError``."""
    if not isinstance(value, dict):
        msg = f"expected a JSON object, got {type(value).__name__}"
        raise TypeError(msg)
    return value
