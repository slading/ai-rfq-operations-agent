"""Trust-boundary value objects (architecture §8).

The single most important structural rule of the project lives here:

    Untrusted customer content has no path into the trusted instruction
    channel, and model output has no path into a business fact without
    passing a verification gate.

Phase 0 defines the contract. The concrete renderer (prompt assembly) and the
injection classifier arrive in Phase 5; nothing in this module calls a model.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from rfq_agent.domain.values import DomainModel, sha256_bytes, sha256_text

__all__ = [
    "InjectionSignalSource",
    "InjectionSuspicion",
    "TrustBoundaryRenderer",
    "TrustTier",
    "UntrustedEnvelope",
    "UntrustedText",
    "normalize_untrusted",
]

#: Prefix of the framing delimiter; the full delimiter includes a random nonce.
_DELIMITER_PREFIX = "UNTRUSTED-CUSTOMER-CONTENT"
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9]{16,64}$")
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_SNIPPETS = 5


class TrustTier(StrEnum):
    """Trust classification of every piece of content in the system (§8.1)."""

    #: Code, config, versioned system prompts, tool schemas, business policy.
    SYSTEM = "T0_SYSTEM"
    #: Seeded business data (catalog, stock, price books, customers).
    BUSINESS_DATA = "T1_BUSINESS_DATA"
    #: Customer email body/subject/sender strings and attachment text.
    CUSTOMER_CONTENT = "T2_UNTRUSTED"
    #: Raw model output: a *claim*, not a fact.
    MODEL_OUTPUT = "T3_MODEL_CLAIM"
    #: Operator input: semi-trusted, bounded by form types and policy.
    OPERATOR_INPUT = "T4_OPERATOR"


class InjectionSignalSource(StrEnum):
    """Where an injection suspicion came from."""

    HEURISTIC = "heuristic"
    CLASSIFIER_MODEL = "classifier_model"
    OPERATOR = "operator"
    CANARY_SCAN = "canary_scan"


def normalize_untrusted(text: str) -> str:
    """Normalise untrusted text for *matching* purposes only.

    Applies NFKC and collapses whitespace runs to single spaces. The raw text
    is always stored and displayed unchanged; this normalised form exists so
    that evidence-span verification is not defeated by typographic variants
    (non-breaking spaces, full-width digits, curly quotes).
    """
    return " ".join(unicodedata.normalize("NFKC", text).split())


class UntrustedText(DomainModel):
    """Immutable snapshot of customer-supplied content.

    Instances are the *only* carriers of T2 content. They are hash-addressed so
    that logs, prompts and trace rows can reference the content without
    reproducing it.
    """

    text: str
    sha256: Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
    byte_length: Annotated[int, Field(ge=0)]
    truncated: bool = False

    @model_validator(mode="after")
    def _check_digest(self) -> Self:
        """Guarantee the digest and byte length match the stored text.

        A truncated snapshot hashes the stored (truncated) text, so the
        invariant holds in both cases.
        """
        encoded = self.text.encode("utf-8")
        if self.sha256 != sha256_text(self.text):
            msg = "sha256 does not match text"
            raise ValueError(msg)
        if self.byte_length != len(encoded):
            msg = "byte_length does not match text"
            raise ValueError(msg)
        return self

    @classmethod
    def from_text(cls, text: str, *, max_bytes: int | None = None) -> UntrustedText:
        """Build a snapshot, optionally truncating to ``max_bytes`` of UTF-8."""
        raw = text.encode("utf-8")
        truncated = False
        if max_bytes is not None and len(raw) > max_bytes:
            raw = raw[:max_bytes]
            truncated = True
        # errors="ignore" avoids emitting a split multi-byte character.
        stored = raw.decode("utf-8", errors="ignore")
        encoded = stored.encode("utf-8")
        return cls(
            text=stored,
            sha256=sha256_bytes(encoded),
            byte_length=len(encoded),
            truncated=truncated,
        )

    @property
    def normalized(self) -> str:
        """Whitespace/Unicode-normalised form used for evidence matching."""
        return normalize_untrusted(self.text)


class InjectionSuspicion(DomainModel):
    """A recorded suspicion that customer content attempted to steer the system.

    Detection is defence-in-depth only. The actual guarantee comes from
    capability minimisation, data-layer scoping and grounding checks: a missed
    detection must never become a compromise.
    """

    source: InjectionSignalSource
    reason: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    #: Verbatim snippets from the untrusted content that triggered the signal.
    snippets: tuple[str, ...] = ()
    #: Classifier confidence in [0, 1]; absent for deterministic heuristics.
    score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None

    @field_validator("snippets")
    @classmethod
    def _check_snippets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require at least one non-empty snippet; keep at most five."""
        kept = tuple(s for s in value if s.strip())[:_MAX_SNIPPETS]
        if not kept:
            msg = "at least one non-empty snippet is required"
            raise ValueError(msg)
        return kept


class UntrustedEnvelope(DomainModel):
    """Untrusted content wrapped for presentation to a model.

    The nonce-delimited framing exists so the boundary is *explicit in the
    prompt* rather than implied, and so a customer cannot smuggle a closing
    delimiter: the nonce is random per envelope and is rejected if it already
    occurs in the content.

    Phase 0 owns the invariants. Phase 5 owns the prompt assembly that consumes
    :attr:`rendered`.
    """

    content: UntrustedText
    nonce: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{16,64}$")]
    rendered: str
    injection_suspicion: InjectionSuspicion | None = None

    @field_validator("rendered")
    @classmethod
    def _check_rendered_not_empty(cls, value: str) -> str:
        """An envelope must carry something."""
        if not value.strip():
            msg = "rendered envelope must not be empty"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_framing(self) -> Self:
        """Verify the delimiter/nonce framing invariants."""
        if not _NONCE_PATTERN.fullmatch(self.nonce):
            msg = "nonce must be 16-64 alphanumeric characters"
            raise ValueError(msg)
        if self.nonce in self.content.text:
            msg = "nonce collides with untrusted content; refusing to render"
            raise ValueError(msg)
        opening, closing = self.opening_delimiter, self.closing_delimiter
        if opening not in self.rendered or closing not in self.rendered:
            msg = "rendered envelope is missing its delimiters"
            raise ValueError(msg)
        if self.content.text not in self.rendered:
            msg = "rendered envelope does not contain the untrusted content verbatim"
            raise ValueError(msg)
        return self

    @property
    def opening_delimiter(self) -> str:
        """Delimiter that opens the untrusted block."""
        return f"<<<{_DELIMITER_PREFIX}:{self.nonce}>>>"

    @property
    def closing_delimiter(self) -> str:
        """Delimiter that closes the untrusted block."""
        return f"<<<END-{_DELIMITER_PREFIX}:{self.nonce}>>>"

    def contains_evidence(self, evidence: str) -> bool:
        """Return whether ``evidence`` occurs verbatim in the normalised content.

        This is the check behind the "no invented evidence" rule (§4.5): an
        extracted field whose evidence span is not literally present in the
        customer's message is rejected rather than repaired.
        """
        needle = normalize_untrusted(evidence)
        if not needle:
            return False
        return needle in self.content.normalized


class TrustBoundaryRenderer(Protocol):
    """Renders untrusted content into the prompt-facing envelope.

    Implemented in Phase 5. Declared here so the agent and triage stages depend
    on the boundary abstraction rather than on string formatting.
    """

    def render(self, content: UntrustedText, nonce: str) -> UntrustedEnvelope:
        """Wrap ``content`` in a nonce-delimited, clearly labelled block."""
        ...
