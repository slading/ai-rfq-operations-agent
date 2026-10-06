"""Tests for the grounding gate (architecture §4.5).

This is the anti-hallucination control, so the negative cases matter more than
the positive ones: an identifier the model was never shown must be rejected, not
repaired, and a partially grounded draft must never be half-accepted.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from rfq_agent.domain.extraction import ExtractedLine
from rfq_agent.domain.resolution import (
    BLOCKING_LINE_STATUSES,
    HUMAN_REQUIRED_CUSTOMER_STATUSES,
    CustomerCandidate,
    CustomerMatchStatus,
    GroundedResolveDraft,
    GroundingContext,
    GroundingErrorCode,
    GroundingIssue,
    GroundingReport,
    ProductCandidate,
    ProposedLine,
    ResolutionMatchStatus,
    ResolutionSource,
    ResolvedLine,
    ResolveDraft,
    ground_evidence,
    ground_resolve_draft,
)
from tests.conftest import CUSTOMER_ID, FOREIGN_CUSTOMER, PRODUCT_X, PRODUCT_Y, make_envelope


def make_context(**overrides: object) -> GroundingContext:
    payload: dict[str, object] = {
        "envelope": make_envelope(),
        "presented_product_ids": frozenset({PRODUCT_X, PRODUCT_Y}),
        "presented_customer_ids": frozenset({CUSTOMER_ID}),
        "valid_ordinals": frozenset({1, 2}),
    }
    payload.update(overrides)
    return GroundingContext.model_validate(payload)


def make_draft(**overrides: object) -> ResolveDraft:
    payload: dict[str, object] = {
        "proposed_customer_id": CUSTOMER_ID,
        "customer_match_status": CustomerMatchStatus.EXACT,
        "lines": (
            ProposedLine(ordinal=1, proposed_product_id=PRODUCT_X),
            ProposedLine(
                ordinal=2,
                candidates=(
                    ProductCandidate(
                        product_id=PRODUCT_Y,
                        rank=1,
                        match_score=Decimal("0.62"),
                        match_reason="fuzzy match on 'Y-500'",
                    ),
                ),
            ),
        ),
    }
    payload.update(overrides)
    return ResolveDraft.model_validate(payload)


class TestGroundingAccepts:
    def test_grounded_draft_is_promoted(self) -> None:
        report = ground_resolve_draft(make_draft(), make_context())
        assert report.ok is True
        assert report.issues == ()
        assert isinstance(report.draft, GroundedResolveDraft)
        assert report.draft is not None
        assert report.draft.proposed_customer_id == CUSTOMER_ID
        assert len(report.draft.lines) == 2

    def test_unbound_customer_with_no_proposal_is_accepted(self) -> None:
        draft = make_draft(
            proposed_customer_id=None,
            customer_match_status=CustomerMatchStatus.UNBOUND,
        )
        assert ground_resolve_draft(draft, make_context()).ok is True


class TestGroundingRejects:
    def test_invented_product_id_is_rejected(self) -> None:
        draft = make_draft(lines=(ProposedLine(ordinal=1, proposed_product_id="PROD-INVENTED"),))
        report = ground_resolve_draft(draft, make_context())
        assert report.ok is False
        assert report.draft is None
        assert report.issues[0].code is GroundingErrorCode.UNGROUNDED_PRODUCT_ID
        assert report.issues[0].ordinal == 1

    def test_invented_customer_id_is_rejected(self) -> None:
        draft = make_draft(proposed_customer_id=FOREIGN_CUSTOMER)
        report = ground_resolve_draft(draft, make_context())
        assert [issue.code for issue in report.issues] == [
            GroundingErrorCode.UNGROUNDED_CUSTOMER_ID
        ]

    def test_ungrounded_candidate_is_rejected(self) -> None:
        draft = make_draft(
            lines=(
                ProposedLine(
                    ordinal=1,
                    candidates=(ProductCandidate(product_id="PROD-NEVER-SHOWN", rank=1),),
                ),
            )
        )
        report = ground_resolve_draft(draft, make_context())
        assert report.issues[0].code is GroundingErrorCode.UNGROUNDED_PRODUCT_ID
        assert "candidates[0]" in report.issues[0].field

    def test_ungrounded_customer_candidate_is_rejected(self) -> None:
        draft = make_draft(
            customer_match_status=CustomerMatchStatus.AMBIGUOUS,
            customer_candidates=(CustomerCandidate(customer_id=FOREIGN_CUSTOMER, rank=1),),
        )
        report = ground_resolve_draft(draft, make_context())
        assert report.issues[0].code is GroundingErrorCode.UNGROUNDED_CUSTOMER_ID

    def test_unknown_ordinal_is_rejected(self) -> None:
        draft = make_draft(lines=(ProposedLine(ordinal=9, proposed_product_id=PRODUCT_X),))
        report = ground_resolve_draft(draft, make_context())
        assert GroundingErrorCode.UNKNOWN_LINE_ORDINAL in {issue.code for issue in report.issues}

    def test_all_problems_are_reported_at_once(self) -> None:
        draft = make_draft(
            proposed_customer_id=FOREIGN_CUSTOMER,
            lines=(
                ProposedLine(ordinal=1, proposed_product_id="PROD-INVENTED"),
                ProposedLine(ordinal=2, proposed_product_id="PROD-ALSO-INVENTED"),
            ),
        )
        report = ground_resolve_draft(draft, make_context())
        assert len(report.issues) == 3
        assert report.draft is None


class TestGroundingReportInvariants:
    def test_issues_and_draft_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="must be None when issues"):
            GroundingReport(
                issues=(
                    GroundingIssue(
                        code=GroundingErrorCode.UNKNOWN_LINE_ORDINAL,
                        field="lines",
                        detail="bad",
                    ),
                ),
                draft=GroundedResolveDraft(lines=(ProposedLine(ordinal=1),)),
            )

    def test_empty_report_requires_a_draft(self) -> None:
        with pytest.raises(ValidationError, match="draft is required"):
            GroundingReport()


class TestEvidenceGrounding:
    def test_verbatim_evidence_passes(self) -> None:
        lines = (
            ExtractedLine.model_validate(
                {
                    "ordinal": 1,
                    "raw_text": "40 units of X-120",
                    "requested_sku": "X-120",
                    "quantity": 40,
                    "evidence": "40 units of X-120",
                }
            ),
        )
        assert ground_evidence(lines, make_envelope()) == ()

    def test_invented_evidence_is_reported(self) -> None:
        lines = (
            ExtractedLine.model_validate(
                {
                    "ordinal": 1,
                    "raw_text": "40 units of X-120",
                    "requested_sku": "X-120",
                    "quantity": 40,
                    "evidence": "400 units of X-1200",
                }
            ),
        )
        issues = ground_evidence(lines, make_envelope())
        assert len(issues) == 1
        assert issues[0].code is GroundingErrorCode.EVIDENCE_NOT_VERBATIM


class TestDraftSchemaContract:
    def test_customer_status_must_agree_with_the_proposal(self) -> None:
        with pytest.raises(ValidationError, match="must be UNBOUND"):
            make_draft(
                proposed_customer_id=None,
                customer_match_status=CustomerMatchStatus.EXACT,
            )
        with pytest.raises(ValidationError, match="must not be UNBOUND"):
            make_draft(
                proposed_customer_id=CUSTOMER_ID,
                customer_match_status=CustomerMatchStatus.UNBOUND,
            )

    def test_duplicate_ordinals_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be unique"):
            make_draft(
                lines=(
                    ProposedLine(ordinal=1, proposed_product_id=PRODUCT_X),
                    ProposedLine(ordinal=1, proposed_product_id=PRODUCT_Y),
                )
            )

    def test_a_draft_must_have_at_least_one_line(self) -> None:
        with pytest.raises(ValidationError):
            make_draft(lines=())

    def test_candidate_ranks_must_be_unique(self) -> None:
        with pytest.raises(ValidationError, match="ranks must be unique"):
            ProposedLine(
                ordinal=1,
                candidates=(
                    ProductCandidate(product_id=PRODUCT_X, rank=1),
                    ProductCandidate(product_id=PRODUCT_Y, rank=1),
                ),
            )

    def test_chosen_requires_chosen_by(self) -> None:
        with pytest.raises(ValidationError, match="chosen_by is required"):
            ProductCandidate(product_id=PRODUCT_X, rank=1, chosen=True)


class TestResolvedLine:
    def test_resolved_line_requires_product_and_source(self) -> None:
        with pytest.raises(ValidationError, match="product_id and sku are required"):
            ResolvedLine(
                line_item_id="LI-0001",
                ordinal=1,
                extracted=ExtractedLine.model_validate(
                    {
                        "ordinal": 1,
                        "raw_text": "40 units of X-120",
                        "requested_sku": "X-120",
                        "quantity": 40,
                        "evidence": "40 units of X-120",
                    }
                ),
                status=ResolutionMatchStatus.RESOLVED,
            )

    def test_blocking_line_must_not_claim_a_source(self) -> None:
        with pytest.raises(ValidationError, match="must have no source"):
            ResolvedLine(
                line_item_id="LI-0001",
                ordinal=1,
                extracted=ExtractedLine.model_validate(
                    {
                        "ordinal": 1,
                        "raw_text": "15 units of Y-500",
                        "requested_sku": "Y-500",
                        "evidence": "15 units of Y-500",
                        "quantity": None,
                        "missing_reason": "NOT_STATED",
                    }
                ),
                status=ResolutionMatchStatus.MISSING_QTY,
                source=ResolutionSource.AGENT,
            )


class TestStatusSets:
    def test_blocking_statuses(self) -> None:
        assert (
            frozenset(
                {
                    ResolutionMatchStatus.AMBIGUOUS,
                    ResolutionMatchStatus.UNMATCHED,
                    ResolutionMatchStatus.MISSING_QTY,
                    ResolutionMatchStatus.REJECTED,
                }
            )
            == BLOCKING_LINE_STATUSES
        )
        assert ResolutionMatchStatus.RESOLVED not in BLOCKING_LINE_STATUSES

    def test_customer_statuses_requiring_a_human(self) -> None:
        assert CustomerMatchStatus.EXACT not in HUMAN_REQUIRED_CUSTOMER_STATUSES
        assert (
            frozenset(
                {
                    CustomerMatchStatus.SINGLE_CANDIDATE,
                    CustomerMatchStatus.AMBIGUOUS,
                    CustomerMatchStatus.NO_MATCH,
                    CustomerMatchStatus.UNBOUND,
                }
            )
            == HUMAN_REQUIRED_CUSTOMER_STATUSES
        )
