"""The deterministic resolution core: claims in, resolved facts out (Phase 1M-B).

Extraction claims name what the customer wrote; the quote engine asks its
questions of resolved facts. This module is the deterministic seam between
them: it rules on which catalogue item and which customer the claims refer to,
using nothing but stored business data read through
:class:`~rfq_agent.persistence.repositories.BusinessReader`. The model
proposes nothing here and the human decides nothing here - every output is
either a fact this core derived from data or a status that hands the decision
to a human, which :func:`rfq_agent.quote_adapter.to_quote_request` already
refuses to quote.

Where each responsibility lives, so a reviewer can hold this module to its
boundary:

* **no new domain models.** ``ExtractedLine`` and ``LineItemId`` in,
  ``ResolvedCustomer`` and ``ResolvedLine`` out, with ``CustomerSearch``
  travelling back as the evidence a human needs - all accepted contracts;
* **identity is proven, never assumed.** A customer is bound ``EXACT`` only
  when exact normalized identity equality is established the one way the data
  can establish it: a stored ``customer_aliases`` entry of kind ``EMAIL`` -
  "a full e-mail address belonging to the customer" - whose normalised form
  equals the sender's address under the shared :func:`normalize_alias` rule,
  naming exactly one active customer. One *candidate* is not one *identity*:
  names, trading names and e-mail domains are evidence for a human, so every
  other outcome is ``SINGLE_CANDIDATE`` (human confirms) or ``UNBOUND``
  (human binds), never ``EXACT``;
* **claims are not facts.** The claim's own ``status``, ``confidence`` and
  ``rejection_reason`` are never consulted - final statuses belong to the
  deterministic core (grounding rule ``STATUS_CLAIMED_BY_MODEL``). What the
  claim states (the catalogue number, the quantity) is asked about; what the
  claim *concludes* is ignored;
* **nothing is invented, nothing is guessed.** No clock, no minted identifier
  - ``LineItemId``s are stated by the caller and must be unique. Ambiguous,
  unknown, discontinued and quantity-less outcomes are recorded as the
  statuses the contracts already define, with a deterministic
  ``resolution_reason`` for the operator. ``matches[0]`` is never taken;
* **the quote engine is untouched.** Nothing here prices, gates, approves,
  persists or transitions; the human gate lives downstream in 1M-A's adapter,
  and this core only decides whether the facts are there at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rfq_agent.domain.extraction import ExtractedLine
from rfq_agent.domain.ids import LineItemId
from rfq_agent.domain.resolution import (
    CustomerMatchStatus,
    ResolutionMatchStatus,
    ResolutionSource,
    ResolvedCustomer,
    ResolvedLine,
)
from rfq_agent.persistence.enums import AliasKind
from rfq_agent.persistence.read_models import CustomerSearch, MatchSource
from rfq_agent.persistence.repositories import BusinessReader

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["bind_customer", "resolve_lines"]


def bind_customer(text: str, *, reader: BusinessReader) -> tuple[ResolvedCustomer, CustomerSearch]:
    """Rule on which customer a sender's identity belongs to, or decline to.

    ``text`` is the sender's identity string as written - the orchestrator
    passes ``InboundRfq.sender_email``, the only identity intake holds. It is
    used as a lookup key through the read boundary's own
    :func:`normalize_alias` matching; it is never itself a business fact.

    The ruling, in full: exact normalized identity equality - the query
    matching a stored ``EMAIL`` alias, which the data records as a full
    address *belonging to the customer* - against exactly one active customer
    is the only auto-bind, and yields ``EXACT``/``SYSTEM``. One matched
    customer without that proof (a trading name, a display name, an e-mail
    domain) is ``SINGLE_CANDIDATE``: identified, but a human confirms. More
    than one matched customer - including the same address recorded twice -
    is ``UNBOUND``. The search evidence returns alongside the ruling so a
    human sees every candidate; nothing here picks one.

    Returns:
        The ruling and the ``CustomerSearch`` evidence it was made over.

    """
    search = reader.customers.search(text)
    identity = [
        match
        for match in search.matches
        if match.source is MatchSource.ALIAS and match.alias_kind is AliasKind.EMAIL
    ]
    if len(search.matched_ids) != 1:
        unbound = ResolvedCustomer(
            customer_id=None,
            match_status=CustomerMatchStatus.UNBOUND,
            source=None,
        )
        return unbound, search
    customer_id = search.matched_ids[0]
    # Every match names the same customer's record; the first is as good as any.
    customer = search.matches[0].customer
    if not identity or not customer.active:
        candidate = ResolvedCustomer(
            customer_id=customer_id,
            match_status=CustomerMatchStatus.SINGLE_CANDIDATE,
            source=ResolutionSource.SYSTEM,
        )
        return candidate, search
    exact = ResolvedCustomer(
        customer_id=customer_id,
        match_status=CustomerMatchStatus.EXACT,
        source=ResolutionSource.SYSTEM,
    )
    return exact, search


def resolve_lines(
    lines: Sequence[ExtractedLine],
    *,
    line_item_ids: Sequence[LineItemId],
    reader: BusinessReader,
) -> tuple[ResolvedLine, ...]:
    """Rule on every extracted line against the catalogue, deterministically.

    ``line_item_ids`` are stated by the caller - one per line, in order, unique
    - so this core mints no identifier. Each line is matched on the catalogue
    number the customer wrote, against the product's own SKU and its stored
    aliases (the read boundary's normalisation, and its ``PMP-A-100``
    collision, are exactly the accepted rules). No name, description or fuzzy
    matching: a claim with no catalogue number cannot be resolved here.

    Final statuses, in the precedence the contracts fix: ``AMBIGUOUS`` (the
    number names more than one product) over ``UNMATCHED`` (nothing matches,
    or none was stated) over ``DISCONTINUED`` (the product is inactive) over
    ``MISSING_QTY`` (no quantity was stated) over ``RESOLVED``. A resolved or
    discontinued line carries the product's canonical SKU and
    ``ResolutionSource.SYSTEM``; a blocking line carries no source, and keeps
    the matched product as evidence when there is exactly one.

    Returns:
        One ``ResolvedLine`` per input line, in input order.

    Raises:
        ValueError: When the caller's identifiers do not pair one-to-one with
            the lines, or repeat - identity is stated, never generated.

    """
    if len(line_item_ids) != len(lines):
        msg = (
            "line_item_ids must pair one-to-one with the lines: "
            f"{len(line_item_ids)} identifiers for {len(lines)} lines"
        )
        raise ValueError(msg)
    if len(set(line_item_ids)) != len(line_item_ids):
        msg = "line_item_ids must be unique: identity is stated, never generated"
        raise ValueError(msg)
    return tuple(
        _resolve_line(extracted, line_item_id, reader)
        for extracted, line_item_id in zip(lines, line_item_ids, strict=True)
    )


def _resolve_line(
    extracted: ExtractedLine,
    line_item_id: LineItemId,
    reader: BusinessReader,
) -> ResolvedLine:
    """Rule on one claim; see :func:`resolve_lines` for the rules."""
    if extracted.requested_sku is None:
        return ResolvedLine(
            line_item_id=line_item_id,
            ordinal=extracted.ordinal,
            extracted=extracted,
            status=ResolutionMatchStatus.UNMATCHED,
            resolution_reason="no catalogue number was stated",
        )
    found = reader.catalog.search(
        extracted.requested_sku,
        sources={MatchSource.SKU, MatchSource.ALIAS},
    )
    if len(found.matched_ids) > 1:
        reason = f"catalogue number matches {', '.join(found.matched_ids)}"
        return ResolvedLine(
            line_item_id=line_item_id,
            ordinal=extracted.ordinal,
            extracted=extracted,
            status=ResolutionMatchStatus.AMBIGUOUS,
            resolution_reason=reason,
        )
    if not found.matched_ids:
        return ResolvedLine(
            line_item_id=line_item_id,
            ordinal=extracted.ordinal,
            extracted=extracted,
            status=ResolutionMatchStatus.UNMATCHED,
            resolution_reason="no catalogue number matches",
        )
    product_id = found.matched_ids[0]
    product = next(
        match.product for match in found.matches if match.product.product_id == product_id
    )
    if not product.active:
        return ResolvedLine(
            line_item_id=line_item_id,
            ordinal=extracted.ordinal,
            extracted=extracted,
            product_id=product_id,
            sku=product.sku,
            status=ResolutionMatchStatus.DISCONTINUED,
            source=ResolutionSource.SYSTEM,
            resolution_reason="catalogue item is inactive",
        )
    if extracted.quantity is None:
        return ResolvedLine(
            line_item_id=line_item_id,
            ordinal=extracted.ordinal,
            extracted=extracted,
            product_id=product_id,
            sku=product.sku,
            status=ResolutionMatchStatus.MISSING_QTY,
            resolution_reason="quantity was not stated",
        )
    labels = {MatchSource.SKU: "exact SKU match", MatchSource.ALIAS: "stored alias match"}
    evidence = sorted(
        {labels[match.source] for match in found.matches if match.product.product_id == product_id}
    )
    return ResolvedLine(
        line_item_id=line_item_id,
        ordinal=extracted.ordinal,
        extracted=extracted,
        product_id=product_id,
        sku=product.sku,
        status=ResolutionMatchStatus.RESOLVED,
        source=ResolutionSource.SYSTEM,
        resolution_reason=", ".join(evidence),
    )
