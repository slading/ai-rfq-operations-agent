"""Resolution schemas and the grounding gate (architecture §4.5).

The resolve agent emits *proposals*: which candidate product/customer it thinks
matches, each with a score and a reason. It never assigns a final status, and
it never invents an identifier.

:func:`ground_resolve_draft` is the deterministic gate that decides whether a
draft is admissible at all. It answers exactly two questions:

* did every identifier in the draft actually appear in a tool result shown to
  the model during this run?
* is every evidence span literally present in the untrusted input?

Anything else - the final match status, prices, totals - is computed later by
the deterministic core.

The module also owns :func:`normalize_alias`, the single rule that turns a
human-written string into the form stored in ``customer_aliases.normalized`` and
``product_aliases.normalized``. It lives here because two very different places
depend on it agreeing with itself: the seed that writes those columns and the
readers that look values up in them.
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.extraction import Confidence, ExtractedLine
from rfq_agent.domain.ids import CustomerId, LineItemId, ProductId
from rfq_agent.domain.trust import UntrustedEnvelope
from rfq_agent.domain.values import DomainModel

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from collections.abc import Set as AbstractSet

__all__ = [
    "CustomerCandidate",
    "CustomerMatchStatus",
    "GroundedResolveDraft",
    "GroundingErrorCode",
    "GroundingIssue",
    "GroundingReport",
    "ProductCandidate",
    "ResolutionMatchStatus",
    "ResolutionSource",
    "ResolveDraft",
    "ResolvedCustomer",
    "ResolvedLine",
    "ground_resolve_draft",
    "normalize_alias",
]

#: How many candidates the agent may attach to one line.
_MAX_CANDIDATES = 5
_MAX_LINE_ITEMS = 50
_MAX_OPEN_QUESTIONS = 10
_MAX_AMBIGUITIES = 20


class CustomerMatchStatus(StrEnum):
    """Outcome of customer resolution."""

    #: Exactly one candidate above the acceptance band.
    EXACT = "EXACT"
    #: One plausible candidate, below the auto-bind threshold -> human confirms.
    SINGLE_CANDIDATE = "SINGLE_CANDIDATE"
    #: Several plausible candidates -> human selects.
    AMBIGUOUS = "AMBIGUOUS"
    #: No candidate at all -> human binds a customer.
    NO_MATCH = "NO_MATCH"
    #: The agent made no proposal.
    UNBOUND = "UNBOUND"


#: Statuses that require a human decision before pricing can proceed.
HUMAN_REQUIRED_CUSTOMER_STATUSES: frozenset[CustomerMatchStatus] = frozenset(
    {
        CustomerMatchStatus.SINGLE_CANDIDATE,
        CustomerMatchStatus.AMBIGUOUS,
        CustomerMatchStatus.NO_MATCH,
        CustomerMatchStatus.UNBOUND,
    }
)


class ResolutionMatchStatus(StrEnum):
    """Final per-line resolution status, assigned by the deterministic core."""

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNMATCHED = "UNMATCHED"
    MISSING_QTY = "MISSING_QTY"
    DISCONTINUED = "DISCONTINUED"
    REJECTED = "REJECTED"


#: Line statuses that block an automatic quote and force human input.
BLOCKING_LINE_STATUSES: frozenset[ResolutionMatchStatus] = frozenset(
    {
        ResolutionMatchStatus.AMBIGUOUS,
        ResolutionMatchStatus.UNMATCHED,
        ResolutionMatchStatus.MISSING_QTY,
        ResolutionMatchStatus.REJECTED,
    }
)


class ResolutionSource(StrEnum):
    """Who made the resolution decision. Recorded on every resolved line."""

    #: Deterministic exact match on SKU; no human needed.
    SYSTEM = "SYSTEM"
    #: The agent proposed it and the grounding gate accepted it.
    AGENT = "AGENT"
    #: A human selected it in the operator console.
    HUMAN = "HUMAN"


class GroundingErrorCode(StrEnum):
    """Why the grounding gate rejected part of a draft."""

    UNGROUNDED_PRODUCT_ID = "UNGROUNDED_PRODUCT_ID"
    UNGROUNDED_CUSTOMER_ID = "UNGROUNDED_CUSTOMER_ID"
    EVIDENCE_NOT_VERBATIM = "EVIDENCE_NOT_VERBATIM"
    RESOLVED_WITHOUT_PRODUCT = "RESOLVED_WITHOUT_PRODUCT"
    UNKNOWN_LINE_ORDINAL = "UNKNOWN_LINE_ORDINAL"
    DUPLICATE_LINE_ORDINAL = "DUPLICATE_LINE_ORDINAL"
    STATUS_CLAIMED_BY_MODEL = "STATUS_CLAIMED_BY_MODEL"
    CANDIDATE_LIMIT_EXCEEDED = "CANDIDATE_LIMIT_EXCEEDED"


class CustomerCandidate(DomainModel):
    """One customer candidate, as returned by ``search_customer``."""

    customer_id: CustomerId
    rank: Annotated[int, Field(ge=1)]
    match_score: Confidence = Decimal("0")
    match_reason: Annotated[str, StringConstraints(min_length=1, max_length=200)] = "unspecified"
    #: Set only by a human selection in the operator console.
    chosen: bool = False
    chosen_by: ResolutionSource | None = None

    @model_validator(mode="after")
    def _check_chosen_by(self) -> Self:
        """``chosen`` and ``chosen_by`` must agree."""
        if self.chosen and self.chosen_by is None:
            msg = "chosen_by is required when chosen is true"
            raise ValueError(msg)
        if not self.chosen and self.chosen_by is not None:
            msg = "chosen_by must be None when chosen is false"
            raise ValueError(msg)
        return self


class ProductCandidate(DomainModel):
    """One catalog candidate for a line, as returned by ``search_catalog``."""

    product_id: ProductId
    rank: Annotated[int, Field(ge=1)]
    match_score: Confidence = Decimal("0")
    match_reason: Annotated[str, StringConstraints(min_length=1, max_length=200)] = "unspecified"
    #: Set only by a human selection or by the deterministic core.
    chosen: bool = False
    chosen_by: ResolutionSource | None = None

    @model_validator(mode="after")
    def _check_chosen_by(self) -> Self:
        """``chosen`` and ``chosen_by`` must agree."""
        if self.chosen and self.chosen_by is None:
            msg = "chosen_by is required when chosen is true"
            raise ValueError(msg)
        if not self.chosen and self.chosen_by is not None:
            msg = "chosen_by must be None when chosen is false"
            raise ValueError(msg)
        return self


class ProposedLine(DomainModel):
    """The agent's proposal for one extracted line.

    Note what is *absent*: no price, no stock, no final status. The model
    proposes identifiers; the deterministic core decides everything else.
    """

    line_item_id: LineItemId | None = None
    #: Must match an ordinal in the extraction result this draft responds to.
    ordinal: Annotated[int, Field(ge=1)]
    #: Proposed unique match. Mutually exclusive with ``candidates`` in
    #: practice; both present means "ambiguous", which is validated below.
    proposed_product_id: ProductId | None = None
    candidates: Annotated[tuple[ProductCandidate, ...], Field(max_length=_MAX_CANDIDATES)] = ()
    #: Free-text explanation shown to the operator. Never used as a fact.
    note: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None

    @model_validator(mode="after")
    def _check_candidate_ranks(self) -> Self:
        """Candidate ranks must be unique."""
        ranks = [c.rank for c in self.candidates]
        if len(set(ranks)) != len(ranks):
            msg = "candidate ranks must be unique"
            raise ValueError(msg)
        return self


class ResolveDraft(DomainModel):
    """Structured output of the resolve stage (trust tier T3, unvalidated)."""

    proposed_customer_id: CustomerId | None = None
    customer_match_status: CustomerMatchStatus = CustomerMatchStatus.UNBOUND
    customer_candidates: Annotated[
        tuple[CustomerCandidate, ...], Field(max_length=_MAX_CANDIDATES)
    ] = ()
    lines: Annotated[tuple[ProposedLine, ...], Field(min_length=1, max_length=_MAX_LINE_ITEMS)]
    ambiguities: Annotated[
        tuple[Annotated[str, StringConstraints(min_length=1, max_length=300)], ...],
        Field(max_length=_MAX_AMBIGUITIES),
    ] = ()
    open_questions: Annotated[
        tuple[Annotated[str, StringConstraints(min_length=1, max_length=300)], ...],
        Field(max_length=_MAX_OPEN_QUESTIONS),
    ] = ()

    @model_validator(mode="after")
    def _check_customer_contract(self) -> Self:
        """A proposed customer id requires a non-UNBOUND status and vice versa."""
        unbound = CustomerMatchStatus.UNBOUND
        if self.proposed_customer_id is None and self.customer_match_status is not unbound:
            msg = "customer_match_status must be UNBOUND when no customer is proposed"
            raise ValueError(msg)
        if self.proposed_customer_id is not None and self.customer_match_status is unbound:
            msg = "customer_match_status must not be UNBOUND when a customer is proposed"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_ordinals(self) -> Self:
        """Line ordinals must be unique within a draft."""
        ordinals = [line.ordinal for line in self.lines]
        if len(set(ordinals)) != len(ordinals):
            msg = "draft line ordinals must be unique"
            raise ValueError(msg)
        return self


class GroundingContext(DomainModel):
    """Everything the grounding gate needs to know about one run.

    Built by the orchestrator (Phase 4) from the recorded ``tool_calls`` rows:
    these are the identifiers the model was *actually shown*.
    """

    envelope: UntrustedEnvelope
    presented_product_ids: frozenset[ProductId] = frozenset()
    presented_customer_ids: frozenset[CustomerId] = frozenset()
    #: Ordinals present in the extraction result this draft responds to.
    valid_ordinals: frozenset[int] = frozenset()


class GroundingIssue(DomainModel):
    """One rejection produced by the grounding gate."""

    code: GroundingErrorCode
    field: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    #: Offending ordinal, when the issue concerns a specific line.
    ordinal: int | None = None


class GroundingReport(DomainModel):
    """Result of :func:`ground_resolve_draft`.

    ``draft`` is present if and only if ``issues`` is empty: a partially
    grounded draft is never half-trusted (§7 F20).
    """

    issues: tuple[GroundingIssue, ...] = ()
    draft: GroundedResolveDraft | None = None

    @property
    def ok(self) -> bool:
        """Whether the draft passed every grounding check."""
        return not self.issues and self.draft is not None

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        """A draft and issues are mutually exclusive."""
        if self.issues and self.draft is not None:
            msg = "draft must be None when issues are present"
            raise ValueError(msg)
        if not self.issues and self.draft is None:
            msg = "draft is required when there are no issues"
            raise ValueError(msg)
        return self


class GroundedResolveDraft(DomainModel):
    """A :class:`ResolveDraft` that has passed the grounding gate.

    Obtaining one of these is the only way downstream stages can see agent
    output, which is what makes "the model cannot invent an identifier" a
    structural property rather than a prompt instruction.
    """

    proposed_customer_id: CustomerId | None = None
    customer_match_status: CustomerMatchStatus = CustomerMatchStatus.UNBOUND
    customer_candidates: tuple[CustomerCandidate, ...] = ()
    lines: tuple[ProposedLine, ...]
    ambiguities: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


def ground_resolve_draft(draft: ResolveDraft, context: GroundingContext) -> GroundingReport:
    """Validate a resolve draft against the run's grounding context.

    Pure function, no I/O. Returns a report whose ``issues`` list is safe to
    feed back to the model as a repair prompt (Phase 4) or to show an operator.
    """
    issues: list[GroundingIssue] = []

    if draft.proposed_customer_id is not None and (
        draft.proposed_customer_id not in context.presented_customer_ids
    ):
        issues.append(
            GroundingIssue(
                code=GroundingErrorCode.UNGROUNDED_CUSTOMER_ID,
                field="proposed_customer_id",
                detail=(f"{draft.proposed_customer_id} was never returned by a tool in this run"),
            )
        )
    for index, candidate in enumerate(draft.customer_candidates):
        if candidate.customer_id not in context.presented_customer_ids:
            issues.append(
                GroundingIssue(
                    code=GroundingErrorCode.UNGROUNDED_CUSTOMER_ID,
                    field=f"customer_candidates[{index}]",
                    detail=f"{candidate.customer_id} was never returned by a tool in this run",
                )
            )

    seen_ordinals: set[int] = set()
    for line in draft.lines:
        location = f"lines[ordinal={line.ordinal}]"
        if line.ordinal in seen_ordinals:
            issues.append(
                GroundingIssue(
                    code=GroundingErrorCode.DUPLICATE_LINE_ORDINAL,
                    field=location,
                    detail="ordinal appears more than once in the draft",
                    ordinal=line.ordinal,
                )
            )
        seen_ordinals.add(line.ordinal)

        if context.valid_ordinals and line.ordinal not in context.valid_ordinals:
            issues.append(
                GroundingIssue(
                    code=GroundingErrorCode.UNKNOWN_LINE_ORDINAL,
                    field=location,
                    detail="ordinal does not exist in the extraction result",
                    ordinal=line.ordinal,
                )
            )

        if line.proposed_product_id is not None and (
            line.proposed_product_id not in context.presented_product_ids
        ):
            issues.append(
                GroundingIssue(
                    code=GroundingErrorCode.UNGROUNDED_PRODUCT_ID,
                    field=f"{location}.proposed_product_id",
                    detail=(f"{line.proposed_product_id} was never returned by a tool in this run"),
                    ordinal=line.ordinal,
                )
            )

        for index, candidate in enumerate(line.candidates):
            if candidate.product_id not in context.presented_product_ids:
                issues.append(
                    GroundingIssue(
                        code=GroundingErrorCode.UNGROUNDED_PRODUCT_ID,
                        field=f"{location}.candidates[{index}]",
                        detail=(f"{candidate.product_id} was never returned by a tool in this run"),
                        ordinal=line.ordinal,
                    )
                )

    return GroundingReport(issues=tuple(issues)) if issues else _accept(draft)


def _accept(draft: ResolveDraft) -> GroundingReport:
    """Promote a draft that passed every check."""
    return GroundingReport(
        issues=(),
        draft=GroundedResolveDraft(
            proposed_customer_id=draft.proposed_customer_id,
            customer_match_status=draft.customer_match_status,
            customer_candidates=draft.customer_candidates,
            lines=draft.lines,
            ambiguities=draft.ambiguities,
            open_questions=draft.open_questions,
        ),
    )


def ground_evidence(
    lines: Iterable[ExtractedLine], envelope: UntrustedEnvelope
) -> tuple[GroundingIssue, ...]:
    """Verify that every extraction evidence span is verbatim in the input.

    Kept separate from :func:`ground_resolve_draft` because extraction and
    resolution are different model calls with different repair loops.
    """
    return tuple(
        GroundingIssue(
            code=GroundingErrorCode.EVIDENCE_NOT_VERBATIM,
            field=f"lines[ordinal={line.ordinal}].evidence",
            detail="evidence span does not occur verbatim in the customer content",
            ordinal=line.ordinal,
        )
        for line in lines
        if not envelope.contains_evidence(line.evidence)
    )


def presented_ids(pairs: Iterable[tuple[str, str]]) -> Mapping[str, AbstractSet[str]]:
    """Group identifiers by kind, for building a :class:`GroundingContext`."""
    grouped: dict[str, set[str]] = {}
    for kind, identifier in pairs:
        grouped.setdefault(kind, set()).add(identifier)
    return {kind: frozenset(values) for kind, values in grouped.items()}


class ResolvedCustomer(DomainModel):
    """Customer binding after the deterministic core has ruled on the proposal."""

    customer_id: CustomerId | None = None
    match_status: CustomerMatchStatus = CustomerMatchStatus.UNBOUND
    source: ResolutionSource | None = None

    @model_validator(mode="after")
    def _check_binding(self) -> Self:
        """A bound customer needs an id and a source; an unbound one needs neither."""
        if (self.customer_id is None) != (self.match_status is CustomerMatchStatus.UNBOUND):
            msg = "customer_id and match_status=UNBOUND must be consistent"
            raise ValueError(msg)
        if (self.customer_id is None) != (self.source is None):
            msg = "source is required exactly when customer_id is set"
            raise ValueError(msg)
        return self


class ResolvedLine(DomainModel):
    """A line after deterministic resolution: this is a fact, not a claim."""

    line_item_id: LineItemId
    ordinal: Annotated[int, Field(ge=1)]
    extracted: ExtractedLine
    product_id: ProductId | None = None
    sku: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    status: ResolutionMatchStatus = ResolutionMatchStatus.UNMATCHED
    source: ResolutionSource | None = None
    #: Deterministic explanation shown to the operator, e.g. ``"exact SKU match"``.
    resolution_reason: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _check_resolution_contract(self) -> Self:
        """Resolution status, product and source must agree."""
        resolved_states = (ResolutionMatchStatus.RESOLVED, ResolutionMatchStatus.DISCONTINUED)
        if self.status in resolved_states:
            if self.product_id is None or self.sku is None:
                msg = "product_id and sku are required for a resolved line"
                raise ValueError(msg)
            if self.source is None:
                msg = "source is required for a resolved line"
                raise ValueError(msg)
        if self.status in BLOCKING_LINE_STATUSES and self.source is not None:
            msg = "a blocking line has not been resolved, so it must have no source"
            raise ValueError(msg)
        return self


def normalize_alias(value: str) -> str:
    """Return the stored lookup form of ``value``.

    This is the one normalisation rule in the project: the seed writes the
    ``normalized`` columns with it, the catalogue and customer readers look
    values up with it, and a second implementation - however similar - would make
    those columns meaningless while every test still passed.

    What it does: Unicode NFC first, so two byte-different spellings of the same
    character collapse; whitespace runs collapse to a single space and the ends
    are stripped, because e-mail clients are generous with trailing newlines; and
    ``casefold()`` rather than ``lower()``, because German and Nordic text is in
    scope and ``Straße``/``STRASSE`` must agree.

    What it deliberately does **not** do: strip punctuation, drop legal suffixes
    (``GmbH``) or expand abbreviations. Those are *matching* decisions with
    business consequences - ``Nordwind`` and ``Nordwind GmbH`` may or may not be
    the same customer - and they belong to the resolver, which has to justify a
    match, rather than to a column that silently makes two different strings
    equal.

    Args:
        value: Human-written text: a customer name, trading name, e-mail address
            or part number exactly as it appeared in a message.

    Returns:
        The casefolded, whitespace-collapsed form stored in a ``normalized``
        column. Idempotent: normalising the result again returns the same string,
        so a stored value is always already normalised.

    Raises:
        TypeError: If ``value`` is not a string. Passing ``None`` here would
            otherwise store the text ``"none"`` as an alias.
    """
    if not isinstance(value, str):
        msg = f"expected str, got {type(value).__name__}"
        raise TypeError(msg)
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()
