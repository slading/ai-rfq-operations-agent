"""Tests for the extraction stage: what the model is asked, and what is accepted.

The provider here is the shipped, scriptable
:class:`~rfq_agent.contracts.testing.FakeLLMProvider`, so every case is offline
and free. What is being verified is the boundary: the prompt is assembled from
trusted constants plus the body only, the claim is validated against the accepted
schema, and every failure is a closed, printable code.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rfq_agent.contracts.llm import (
    ChatRole,
    ModelPurpose,
    ParseFailure,
    ParseIssue,
    ParseRequest,
    ProviderCapabilities,
    ProviderName,
)
from rfq_agent.contracts.testing import FakeLLMProvider, parse_success
from rfq_agent.domain.extraction import ExtractionResult
from rfq_agent.domain.trust import UntrustedText
from rfq_agent.domain.values import canonical_json
from rfq_agent.llm_extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    ExtractionRefusal,
    ExtractionRefusalKind,
    LlmExtraction,
    build_parse_request,
    extract_claims,
)

_STATEMENT = "please quote 25 pcs of 100-ABC for our standard replenishment."
_BODY = (
    f"Hello,\n{_STATEMENT}\nNo specific delivery date is required.\nBest regards,\nNordwind Einkauf"
)
_SENDER = "einkauf@nordwind-industrie.de"
_MODEL = "fake/scripted"


def _content(body: str = _BODY) -> UntrustedText:
    """The untrusted body as the rest of the system carries it."""
    return UntrustedText.from_text(body)


def _claims(**overrides: object) -> dict[str, object]:
    """A schema-valid extraction payload for the documented grammar."""
    payload: dict[str, object] = {
        "lines": [
            {
                "ordinal": 1,
                "raw_text": _STATEMENT,
                "requested_sku": "100-ABC",
                "quantity": 25,
                "uom": "pcs",
                "evidence": "25 pcs of 100-ABC",
                "confidence": "1",
            }
        ],
        "requested_delivery": {"resolution": "ABSENT"},
    }
    payload.update(overrides)
    return payload


def _failure(*, provider_error: str | None = None, **overrides: object) -> ParseFailure:
    """A scripted provider failure."""
    payload: dict[str, object] = {"model": _MODEL, **overrides}
    if provider_error is not None:
        payload["provider_error"] = provider_error
    if "issues" not in payload:
        payload["issues"] = ()
    if provider_error is None and not payload["issues"]:
        payload["issues"] = (ParseIssue(field="<response>", message="bad", error_type="bad"),)
    return ParseFailure.model_validate(payload)


def test_a_successful_extraction_returns_the_accepted_claim_object() -> None:
    provider = FakeLLMProvider([parse_success(_claims())])

    outcome = extract_claims(_content(), provider, model=_MODEL, max_output_tokens=1_024)

    assert isinstance(outcome, LlmExtraction)
    assert isinstance(outcome.claims, ExtractionResult)
    assert outcome.claims.lines[0].quantity == 25
    assert outcome.claims.lines[0].requested_sku == "100-ABC"
    assert outcome.summary.model == _MODEL
    assert outcome.summary.line_count == 1
    assert outcome.summary.open_questions == 0


def test_exactly_one_provider_call_is_made() -> None:
    provider = FakeLLMProvider([parse_success(_claims()), parse_success(_claims())])

    extract_claims(_content(), provider, model=_MODEL)

    assert provider.call_count == 1


def test_the_request_is_bound_to_the_accepted_schema() -> None:
    provider = FakeLLMProvider([parse_success(_claims())])

    extract_claims(
        _content(),
        provider,
        model=_MODEL,
        temperature=Decimal("0.25"),
        seed=99,
        max_output_tokens=2_048,
    )

    request = provider.requests[0]
    assert isinstance(request, ParseRequest)
    assert request.purpose is ModelPurpose.EXTRACT
    assert request.model == _MODEL
    assert request.schema_name == "extraction_claims"
    assert request.json_schema == ExtractionResult.model_json_schema()
    assert request.temperature == Decimal("0.25")
    assert request.seed == 99
    assert request.max_output_tokens == 2_048


def test_the_prompt_carries_the_body_and_nothing_else_from_the_request() -> None:
    request = build_parse_request(_content(), model=_MODEL)

    system, user = request.messages
    assert system.role is ChatRole.SYSTEM
    assert user.role is ChatRole.USER
    assert system.content == EXTRACTION_SYSTEM_PROMPT
    assert _BODY in user.content
    # The sender is trusted metadata and is not part of the model's input at all.
    assert _SENDER not in system.content + user.content
    assert "Nordwind Einkauf" in user.content  # part of the body's own signature
    # The body is framed by a nonce-delimited envelope whose nonce it cannot contain.
    assert user.content.startswith("<<<UNTRUSTED-CUSTOMER-CONTENT:")
    nonce = user.content.split(":", 1)[1].split(">>>", 1)[0]
    assert len(nonce) == 32
    assert nonce not in _BODY


def test_the_system_prompt_states_the_rules_and_the_schema() -> None:
    for rule in ("Never invent", "raw_text", "evidence", "open_questions", "PENDING"):
        assert rule in EXTRACTION_SYSTEM_PROMPT
    assert "JSON schema:" in EXTRACTION_SYSTEM_PROMPT
    # The schema is taken from the domain contract itself, not restated by hand.
    assert canonical_json(ExtractionResult.model_json_schema()) in EXTRACTION_SYSTEM_PROMPT


def test_a_fresh_envelope_is_used_for_every_call() -> None:
    first = build_parse_request(_content(), model=_MODEL)
    second = build_parse_request(_content(), model=_MODEL)

    assert first.messages[1].content != second.messages[1].content
    assert _BODY in first.messages[1].content
    assert _BODY in second.messages[1].content


@pytest.mark.parametrize(
    ("provider_error", "kind", "retryable"),
    [
        ("TIMEOUT", ExtractionRefusalKind.PROVIDER_TIMEOUT, True),
        ("RATE_LIMITED", ExtractionRefusalKind.PROVIDER_FAILURE, True),
        ("SERVER_ERROR", ExtractionRefusalKind.PROVIDER_FAILURE, True),
        ("AUTHENTICATION", ExtractionRefusalKind.PROVIDER_FAILURE, False),
        ("INVALID_REQUEST", ExtractionRefusalKind.PROVIDER_FAILURE, False),
        ("NOT_CONFIGURED", ExtractionRefusalKind.PROVIDER_NOT_CONFIGURED, False),
    ],
)
def test_provider_failures_map_onto_the_closed_refusal_vocabulary(
    provider_error: str, kind: ExtractionRefusalKind, retryable: bool
) -> None:
    model = "fake/scripted"
    failure = ParseFailure(
        provider_error=provider_error,
        retryable=provider_error in {"TIMEOUT", "RATE_LIMITED", "SERVER_ERROR"},
        model=model,
    )
    provider = FakeLLMProvider([failure])

    outcome = extract_claims(_content(), provider, model=_MODEL)

    assert isinstance(outcome, ExtractionRefusal)
    assert outcome.kind is kind
    assert outcome.detail == provider_error
    assert outcome.retryable is retryable


@pytest.mark.parametrize(
    ("error_type", "kind"),
    [
        ("json_invalid", ExtractionRefusalKind.RESPONSE_NOT_JSON),
        ("response_too_large", ExtractionRefusalKind.RESPONSE_TOO_LARGE),
        ("missing", ExtractionRefusalKind.RESPONSE_SCHEMA_INVALID),
        ("extra_forbidden", ExtractionRefusalKind.RESPONSE_SCHEMA_INVALID),
    ],
)
def test_content_failures_map_onto_the_closed_refusal_vocabulary(
    error_type: str, kind: ExtractionRefusalKind
) -> None:
    failure = ParseFailure(
        issues=(ParseIssue(field="<response>", message="bad", error_type=error_type),),
        model="fake/scripted",
    )

    outcome = extract_claims(_content(), FakeLLMProvider([failure]), model=_MODEL)

    assert isinstance(outcome, ExtractionRefusal)
    assert outcome.kind is kind
    assert outcome.detail == error_type
    assert outcome.retryable is False


def test_a_schema_invalid_payload_never_becomes_a_claim() -> None:
    payload = _claims()
    payload["lines"] = [{"ordinal": 1, "raw_text": _STATEMENT}]  # no sku, no evidence
    provider = FakeLLMProvider([parse_success(payload)])

    outcome = extract_claims(_content(), provider, model=_MODEL)

    assert isinstance(outcome, ExtractionRefusal)
    assert outcome.kind is ExtractionRefusalKind.RESPONSE_SCHEMA_INVALID


def test_a_response_carrying_an_unknown_field_is_refused() -> None:
    payload = _claims(unit_price="0.01", total="0.25")
    provider = FakeLLMProvider([parse_success(payload)])

    outcome = extract_claims(_content(), provider, model=_MODEL)

    assert isinstance(outcome, ExtractionRefusal)
    assert outcome.kind is ExtractionRefusalKind.RESPONSE_SCHEMA_INVALID


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("status", "RESOLVED", "status_set"),
        ("rejection_reason", "customer is vague", "rejection_reason_set"),
    ],
)
def test_a_response_that_sets_a_system_owned_field_is_refused(
    field: str, value: str, detail: str
) -> None:
    payload = _claims()
    lines = [dict(line) for line in payload["lines"]]  # type: ignore[arg-type]
    lines[0][field] = value
    payload["lines"] = lines
    provider = FakeLLMProvider([parse_success(payload)])

    outcome = extract_claims(_content(), provider, model=_MODEL)

    assert isinstance(outcome, ExtractionRefusal)
    assert outcome.kind is ExtractionRefusalKind.RESPONSE_SCHEMA_INVALID
    assert outcome.detail == detail


def test_an_empty_extraction_is_refused_rather_than_passed_on() -> None:
    provider = FakeLLMProvider([parse_success({"lines": [], "requested_delivery": {}})])

    outcome = extract_claims(_content(), provider, model=_MODEL)

    assert isinstance(outcome, ExtractionRefusal)
    assert outcome.kind is ExtractionRefusalKind.NO_CLAIMED_LINES
    assert outcome.retryable is False


def test_open_questions_and_missing_quantities_are_passed_through_unchanged() -> None:
    payload = {
        "lines": [
            {
                "ordinal": 1,
                "raw_text": _STATEMENT,
                "requested_sku": "100-ABC",
                "quantity": None,
                "missing_reason": "AMBIGUOUS_REFERENCE",
                "uom": "pcs",
                "evidence": "of 100-ABC",
                "confidence": "0.4",
            }
        ],
        "requested_delivery": {"resolution": "ABSENT"},
        "open_questions": ["Which quantity does the customer mean?"],
    }
    provider = FakeLLMProvider([parse_success(payload)])

    outcome = extract_claims(_content(), provider, model=_MODEL)

    assert isinstance(outcome, LlmExtraction)
    assert outcome.claims.open_questions == ("Which quantity does the customer mean?",)
    assert outcome.claims.lines[0].quantity is None
    assert outcome.summary.open_questions == 1


def test_the_fake_provider_reports_its_own_capabilities_not_the_adapters() -> None:
    provider = FakeLLMProvider(
        [parse_success(_claims())],
        capabilities=ProviderCapabilities(
            provider=ProviderName.REPLAY, model="fake/scripted", supports_strict_json_schema=True
        ),
    )

    outcome = extract_claims(_content(), provider, model=_MODEL)

    assert isinstance(outcome, LlmExtraction)
    assert outcome.summary.strict_mode_used is True
