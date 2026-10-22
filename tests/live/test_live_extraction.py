"""Opt-in smoke test against the real provider.

Never runs in CI or in ``make test``: it is marked ``live`` and skipped unless
``RFQ_LLM_MODE=live`` and ``RFQ_GROQ__API_KEY`` are set. It costs one small
completion and proves only one thing - that the configured model, reached
through the real adapter, returns claims that satisfy the accepted schema and
are grounded in the body it was given. The deterministic engine's behaviour is
covered offline; this test asserts nothing about it.
"""

from __future__ import annotations

import os

import pytest

from rfq_agent.config import LLMMode, Settings
from rfq_agent.domain.trust import UntrustedText
from rfq_agent.llm_extraction import LlmExtraction, extract_claims
from rfq_agent.providers import GroqProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RFQ_LLM_MODE") != LLMMode.LIVE.value
        or not os.environ.get("RFQ_GROQ__API_KEY"),
        reason="live provider calls need RFQ_LLM_MODE=live and RFQ_GROQ__API_KEY",
    ),
]

_BODY = (
    "Hello,\n"
    "please quote 25 pcs of 100-ABC for our standard replenishment.\n"
    "No specific delivery date is required.\n"
    "Best regards,\n"
    "Nordwind Einkauf"
)


def test_the_configured_model_returns_grounded_claims() -> None:
    settings = Settings.load()
    assert settings.llm_mode is LLMMode.LIVE
    provider = GroqProvider(
        api_key=settings.groq.require_credentials(),
        model=settings.groq.agent_model,
        base_url=settings.groq.base_url,
        timeout_seconds=float(settings.groq.request_timeout_seconds),
        max_response_bytes=settings.groq.max_response_bytes,
        max_output_tokens=settings.groq.max_output_tokens,
    )

    outcome = extract_claims(
        UntrustedText.from_text(_BODY),
        provider,
        model=settings.groq.agent_model,
        temperature=settings.agent.temperature,
        seed=settings.agent.random_seed,
        max_output_tokens=settings.groq.max_output_tokens,
    )

    assert isinstance(outcome, LlmExtraction), getattr(outcome, "kind", outcome)
    assert outcome.claims.lines, "a supported body must yield at least one claim"
    for line in outcome.claims.lines:
        # Grounding, checked the same way the deterministic verifier checks it.
        assert " ".join(line.evidence.split()) in " ".join(_BODY.split())
        assert line.quantity == 25
        assert line.requested_sku == "100-ABC"
        assert line.status.value == "PENDING"
        assert line.rejection_reason is None
