"""Conservative source semantics for the EA-only quotation boundary.

This is a recognizer for a documented subset, not a natural-language parser.
Every nonempty source line must be accounted for. Finding a plausible tuple
inside otherwise unsupported text is never enough to authorize a quotation.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import NamedTuple

from rfq_agent.domain.extraction import DateResolution, ExtractionResult, RequestedDelivery
from rfq_agent.domain.intake import InboundRfq
from rfq_agent.domain.resolution import GroundingErrorCode, GroundingIssue, normalize_alias

__all__ = ["verify_extraction_claims"]

# Only spelling equivalence: the numeric quantity never changes.
_EA_LABELS = frozenset({"ea", "each", "pc", "pcs", "piece", "pieces"})
_IDENTIFIER = r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,62}[A-Za-z0-9])?"
_ASSOCIATION = r"(?P<qty>[1-9][0-9]{0,6}) (?P<unit>[A-Za-z]+) of (?P<sku>" + _IDENTIFIER + r")"
_REQUEST = re.compile(
    r"(?:please quote|we need) (?P<association>" + _ASSOCIATION + r")"
    r"(?: for our standard replenishment)?\.?",
    re.IGNORECASE | re.ASCII,
)
_SUBJECT = re.compile(
    r"RFQ: (?:(?P<qty>[1-9][0-9]{0,6}) (?P<unit>[A-Za-z]+) )?"
    r"(?P<sku>" + _IDENTIFIER + r")",
    re.IGNORECASE | re.ASCII,
)
_DELIVERY = re.compile(
    r"Deliver to (?P<destination>[A-Za-z]+)"
    r"(?: (?P<when>on [0-9]{4}-[0-9]{2}-[0-9]{2}|"
    r"by (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)))?\.?",
    re.IGNORECASE | re.ASCII,
)
_GREETINGS = frozenset({"hello,", "hej,"})
_CLOSINGS = frozenset({"best regards,", "regards,"})
_NO_DATE = "no specific delivery date is required."
# A supported signature is a company-style name plus an explicit purchasing
# role, not any string repeated from the untrusted sender header.
_SIGNATURE = re.compile(r"[A-Z][a-z]+(?: [A-Z][a-z]+)?,? (?:Einkauf|inköp|Procurement|Purchasing)")


class _Line(NamedTuple):
    raw: str
    quantity: int
    unit: str
    sku: str
    association: str


def _normalize_statement(text: str) -> str:
    """Preserve numeric meaning: compatibility folding is not quantity parsing.

    The older substring evidence gate uses NFKC. Here superscripts, full-width
    digits and compatibility letters must not turn an unsupported expression
    into a different, supported business request. NFC/spacing alone is allowed.
    """
    return " ".join(unicodedata.normalize("NFC", text).split())


def _issue(
    code: GroundingErrorCode, field: str, detail: str, ordinal: int | None = None
) -> GroundingIssue:
    return GroundingIssue(code=code, field=field, detail=detail, ordinal=ordinal)


def _delivery_claim(match: re.Match[str]) -> RequestedDelivery | None:
    """Map recognized delivery syntax without inferring a date."""
    when = match["when"]
    resolution = DateResolution.ABSENT
    requested_date = None
    if when:
        resolution = DateResolution.INFERRED
        if when.casefold().startswith("on "):
            resolution = DateResolution.EXPLICIT
            try:
                requested_date = date.fromisoformat(when[3:])
            except ValueError:
                return None
    return RequestedDelivery(
        raw=when,
        resolution=resolution,
        requested_delivery_date=requested_date,
        destination=match["destination"],
    )


def _source_lines(
    source: InboundRfq,
) -> tuple[list[_Line], RequestedDelivery, list[GroundingIssue]]:
    """Consume the complete body using full-line matches, including framing."""
    lines: list[_Line] = []
    issues: list[GroundingIssue] = []
    delivery = RequestedDelivery()
    closing = False
    signature = False
    no_date = False
    for index, text in enumerate(source.body.text.splitlines(), start=1):
        raw = _normalize_statement(text)
        if not raw:
            continue
        field = f"source.body.line[{index}]"
        lower = raw.casefold()
        if closing:
            # Both signature syntax and the intake sender name must agree.
            # Header equality alone cannot turn arbitrary prose into framing.
            name = normalize_alias(raw.replace(",", " "))
            sender = normalize_alias((source.sender_name or "").replace(",", " "))
            if not signature and sender and name == sender and _SIGNATURE.fullmatch(raw):
                signature = True
                continue
        elif lower in _GREETINGS and not lines and index == 1:
            continue
        elif lower in _CLOSINGS and lines:
            closing = True
            continue
        elif lower in {_NO_DATE, "please send pricing."} and lines:
            no_date = no_date or lower == _NO_DATE
            continue
        elif match := _REQUEST.fullmatch(raw):
            lines.append(
                _Line(raw, int(match["qty"]), match["unit"], match["sku"], match["association"])
            )
            continue
        elif match := _DELIVERY.fullmatch(raw):
            if delivery != RequestedDelivery():
                issues.append(
                    _issue(
                        GroundingErrorCode.SOURCE_CONTRADICTORY,
                        field,
                        "multiple delivery statements require clarification",
                    )
                )
                continue
            parsed = _delivery_claim(match)
            if parsed is None:
                issues.append(
                    _issue(GroundingErrorCode.SOURCE_UNSUPPORTED, field, "invalid delivery date")
                )
            else:
                delivery = parsed
            continue
        issues.append(
            _issue(
                GroundingErrorCode.SOURCE_UNSUPPORTED,
                field,
                "unsupported source statement or specification; clarification required",
            )
        )
    issues.extend(_source_consistency(lines, delivery, no_date))
    return lines, delivery, issues


def _source_consistency(
    lines: list[_Line], delivery: RequestedDelivery, no_date: bool
) -> tuple[GroundingIssue, ...]:
    """Reject repeated products and date contradictions, never choose a version."""
    issues: list[GroundingIssue] = []
    if no_date and delivery.resolution is not DateResolution.ABSENT:
        issues.append(
            _issue(
                GroundingErrorCode.SOURCE_CONTRADICTORY,
                "source.body",
                "delivery date conflicts with the no-date statement",
            )
        )
    keys = [normalize_alias(line.sku) for line in lines]
    if len(keys) != len(set(keys)):
        issues.append(
            _issue(
                GroundingErrorCode.SOURCE_CONTRADICTORY,
                "source.body",
                "repeated product requests require clarification",
            )
        )
    return tuple(issues)


def _check_subject(subject: str, lines: list[_Line]) -> tuple[GroundingIssue, ...]:
    """A subject is empty, RFQ, or a corroborating single-product summary."""
    text = _normalize_statement(subject)
    if not text or text.casefold() == "rfq":
        return ()
    match = _SUBJECT.fullmatch(text)
    if match is None:
        return (
            _issue(
                GroundingErrorCode.SOURCE_UNSUPPORTED,
                "source.subject",
                "unsupported subject; clarification required",
            ),
        )
    if len(lines) != 1 or normalize_alias(match["sku"]) != normalize_alias(lines[0].sku):
        return (
            _issue(
                GroundingErrorCode.SOURCE_CONTRADICTORY,
                "source.subject",
                "subject product differs from the body",
            ),
        )
    if match["qty"] and (
        int(match["qty"]) != lines[0].quantity
        or match["unit"].casefold() not in _EA_LABELS
        or lines[0].unit.casefold() not in _EA_LABELS
    ):
        return (
            _issue(
                GroundingErrorCode.SOURCE_CONTRADICTORY,
                "source.subject",
                "subject quantity or unit differs from the body",
            ),
        )
    return ()


def verify_extraction_claims(
    source: InboundRfq, extraction: ExtractionResult
) -> tuple[GroundingIssue, ...]:
    """Check complete-source meaning, associations and evidence, without I/O.

    Success only establishes the documented source subset. Catalog identity and
    canonical-unit reconciliation remain the resolver/adapter's responsibility.
    Model descriptions are intentionally not product facts; source specifications
    are rejected by the full-statement grammar regardless of what a model omits.
    """
    if source.body.truncated or source.attachments or source.thread_key is not None:
        return (
            _issue(
                GroundingErrorCode.SOURCE_UNSUPPORTED,
                "source",
                "truncated, attached or threaded source requires clarification",
            ),
        )
    lines, delivery, issues = _source_lines(source)
    issues.extend(_check_subject(source.subject, lines))
    if extraction.open_questions:
        issues.append(
            _issue(
                GroundingErrorCode.EXTRACTION_INCONSISTENT,
                "open_questions",
                "unanswered questions require clarification",
            )
        )
    if extraction.requested_delivery != delivery:
        issues.append(
            _issue(
                GroundingErrorCode.EXTRACTION_INCONSISTENT,
                "requested_delivery",
                "delivery claim differs from the source",
            )
        )
    if not lines or len(lines) != len(extraction.lines):
        issues.append(
            _issue(
                GroundingErrorCode.EXTRACTION_INCONSISTENT,
                "lines",
                "source and extraction must contain exactly the same request lines",
            )
        )
    for ordinal, (fact, claim) in enumerate(zip(lines, extraction.lines, strict=False), start=1):
        field = f"lines[ordinal={ordinal}]"
        if claim.ordinal != ordinal or normalize_alias(
            claim.requested_sku or ""
        ) != normalize_alias(fact.sku):
            issues.append(
                _issue(
                    GroundingErrorCode.EXTRACTION_INCONSISTENT,
                    field,
                    "ordinal or product reference differs from its source statement",
                    ordinal,
                )
            )
        if _normalize_statement(claim.raw_text) != fact.raw:
            issues.append(
                _issue(
                    GroundingErrorCode.EVIDENCE_NOT_VERBATIM,
                    field + ".raw_text",
                    "raw text must be the complete source request statement",
                    ordinal,
                )
            )
        evidence = _normalize_statement(claim.evidence)
        # Membership alone is insufficient: the evidence must cover this row's
        # whole independently parsed association (or its complete statement).
        if evidence not in fact.raw or evidence not in (fact.raw, fact.association):
            issues.append(
                _issue(
                    GroundingErrorCode.EVIDENCE_NOT_VERBATIM,
                    field + ".evidence",
                    "evidence must cover this statement's quantity, unit and product",
                    ordinal,
                )
            )
        if claim.quantity != fact.quantity:
            issues.append(
                _issue(
                    GroundingErrorCode.QUANTITY_UNSUPPORTED,
                    field + ".quantity",
                    "quantity differs from the independently parsed request",
                    ordinal,
                )
            )
        if fact.unit.casefold() not in _EA_LABELS:
            issues.append(
                _issue(
                    GroundingErrorCode.UNIT_UNSUPPORTED,
                    field + ".uom",
                    "source unit is not an approved EA spelling; no conversion is permitted",
                    ordinal,
                )
            )
        if claim.uom is not None and claim.uom.casefold() not in _EA_LABELS:
            issues.append(
                _issue(
                    GroundingErrorCode.UNIT_MISMATCH,
                    field + ".uom",
                    "extracted unit contradicts the source or approved EA policy",
                    ordinal,
                )
            )
    return tuple(issues)
