"""Tests for the live provider adapter, with no socket and no API key.

Every test drives :class:`GroqProvider` through its injected transport, so the
suite is deterministic, offline and free. What is being verified is the
adapter's contract: what it sends, what it accepts, how it classifies each
failure, and that no failure can carry a credential or provider text.
"""

from __future__ import annotations

import json
import urllib.error
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict

from rfq_agent.contracts.errors import ProviderErrorCode, ProviderNotConfiguredError
from rfq_agent.contracts.llm import (
    ChatMessage,
    ChatRole,
    LLMProvider,
    ModelPurpose,
    ParseFailure,
    ParseRequest,
    ParseSuccess,
)
from rfq_agent.providers import GroqProvider, HttpRequest, HttpResponse

if TYPE_CHECKING:
    from collections.abc import Sequence

_API_KEY = "gsk_test_key_do_not_leak_0000000000"
_MODEL = "openai/gpt-oss-120b"
_BODY = "please quote 25 pcs of 100-ABC for our standard replenishment."


class TinyClaims(BaseModel):
    """The smallest structured output a test can bind to (closed, like the domain)."""

    model_config = ConfigDict(extra="forbid")

    a: str


def _request(*, seed: int | None = 1337, max_output_tokens: int | None = 256) -> ParseRequest:
    """Build a valid structured-output request."""
    return ParseRequest(
        messages=(
            ChatMessage(role=ChatRole.SYSTEM, content="system rules"),
            ChatMessage(role=ChatRole.USER, content=_BODY),
        ),
        schema_name="extraction_claims",
        json_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        model=_MODEL,
        purpose=ModelPurpose.EXTRACT,
        temperature=Decimal("0"),
        seed=seed,
        max_output_tokens=max_output_tokens,
    )


def _envelope(
    content: str, *, status: int = 200, usage: dict[str, int] | None = None
) -> HttpResponse:
    """Build a Groq-shaped chat-completion response carrying ``content``."""
    payload = {
        "id": "req_123",
        "model": _MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


class _Scripted:
    """A transport that replays responses in order and records every request."""

    def __init__(self, *responses: HttpResponse | BaseException) -> None:
        self._responses: Sequence[HttpResponse | BaseException] = responses
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        response = self._responses[len(self.requests) - 1]
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def call_count(self) -> int:
        return len(self.requests)


def _provider(
    transport: _Scripted,
    *,
    sleeps: list[float] | None = None,
    **overrides: object,
) -> GroqProvider:
    """Build a provider over ``transport`` with fast, recorded backoff."""
    settings: dict[str, object] = {
        "api_key": _API_KEY,
        "model": _MODEL,
        "max_attempts": 1,
        "base_delay_seconds": 0.01,
        "max_delay_seconds": 0.05,
        "jitter_ratio": 0.0,
    }
    settings.update(overrides)
    return GroqProvider(
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else (lambda _delay: None)),
        **settings,  # type: ignore[arg-type]
    )


def test_the_adapter_satisfies_the_provider_port() -> None:
    provider = _provider(_Scripted(_envelope('{"a": "x"}')))

    assert isinstance(provider, LLMProvider)
    assert provider.capabilities.provider.value == "groq"
    assert provider.capabilities.model == _MODEL
    assert provider.capabilities.supports_strict_json_schema is False
    assert provider.capabilities.supports_tools is False


def test_parse_sends_one_bounded_json_mode_request() -> None:
    transport = _Scripted(_envelope('{"a": "x"}'))
    provider = _provider(transport, max_output_tokens=512)

    result, instance = provider.parse(_request(max_output_tokens=None), TinyClaims)

    assert isinstance(result, ParseSuccess)
    assert instance == TinyClaims(a="x")
    assert result.strict_mode_used is False
    assert result.raw == ""
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 18

    request = transport.requests[0]
    assert request.url == "https://api.groq.com/openai/v1/chat/completions"
    assert request.timeout_seconds == 30.0
    assert dict(request.headers)["Authorization"] == f"Bearer {_API_KEY}"
    body = json.loads(request.body)
    assert body["model"] == _MODEL
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 512
    assert body["temperature"] == 0.0
    assert body["seed"] == 1337
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    # The credential travels in the header only, never in the body.
    assert _API_KEY not in request.body.decode()


def test_parse_reports_invalid_json_as_a_typed_failure() -> None:
    transport = _Scripted(_envelope("I think the customer wants pumps."))

    result, instance = _provider(transport).parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.retryable is False
    assert result.raw is None
    assert [issue.error_type for issue in result.issues] == ["json_invalid"]
    assert transport.call_count == 1


def test_parse_refuses_markdown_fenced_json_rather_than_repairing_it() -> None:
    transport = _Scripted(_envelope('```json\n{"a": "x"}\n```'))

    result, instance = _provider(transport).parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert [issue.error_type for issue in result.issues] == ["json_invalid"]


def test_parse_reports_a_schema_violation_without_echoing_the_value() -> None:
    transport = _Scripted(_envelope('{"a": 17, "extra": "leaked-marker"}'))

    result, instance = _provider(transport).parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error is None
    assert [issue.error_type for issue in result.issues] == ["schema_invalid"]
    assert result.issues[0].field == "<response>"
    assert "2 error(s)" in result.issues[0].message
    rendered = json.dumps(result.model_dump(mode="json"))
    # Neither the offending value, the unknown field's value nor the payload is
    # echoed: field paths and messages are withheld at this boundary.
    assert "leaked-marker" not in rendered
    assert "17" not in rendered
    assert '"raw": null' in rendered


def test_parse_reports_a_response_with_no_content() -> None:
    response = HttpResponse(
        status=200,
        body=json.dumps({"choices": [{"message": {"content": None}}]}).encode(),
        headers={},
    )

    result, instance = _provider(_Scripted(response)).parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert [issue.error_type for issue in result.issues] == ["missing_content"]


def test_parse_refuses_a_response_larger_than_the_configured_ceiling() -> None:
    oversized = HttpResponse(status=200, body=b"x" * 2_048, headers={})
    transport = _Scripted(oversized)
    provider = _provider(transport, max_response_bytes=1_024)

    result, instance = provider.parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert [issue.error_type for issue in result.issues] == ["response_too_large"]
    assert transport.call_count == 1


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, ProviderErrorCode.AUTHENTICATION, False),
        (403, ProviderErrorCode.AUTHENTICATION, False),
        (400, ProviderErrorCode.INVALID_REQUEST, False),
        (404, ProviderErrorCode.INVALID_REQUEST, False),
        (413, ProviderErrorCode.INVALID_REQUEST, False),
        (500, ProviderErrorCode.SERVER_ERROR, True),
        (502, ProviderErrorCode.SERVER_ERROR, True),
        (503, ProviderErrorCode.OVERLOADED, True),
        (504, ProviderErrorCode.TIMEOUT, True),
        (418, ProviderErrorCode.UNKNOWN, False),
    ],
)
def test_http_status_is_classified_in_the_accepted_vocabulary(
    status: int, code: ProviderErrorCode, retryable: bool
) -> None:
    transport = _Scripted(HttpResponse(status=status, body=b"{}", headers={}))

    result, instance = _provider(transport).parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == code.value
    assert result.retryable is retryable
    assert transport.call_count == 1


def test_a_retryable_failure_is_retried_within_the_attempt_ceiling() -> None:
    sleeps: list[float] = []
    transport = _Scripted(
        HttpResponse(status=429, body=b"{}", headers={}),
        HttpResponse(status=500, body=b"{}", headers={}),
        _envelope('{"a": "x"}'),
    )
    provider = _provider(transport, max_attempts=3, sleeps=sleeps)  # type: ignore[arg-type]

    result, instance = provider.parse(_request(), TinyClaims)

    assert isinstance(result, ParseSuccess)
    assert instance == TinyClaims(a="x")
    assert transport.call_count == 3
    assert len(sleeps) == 2
    assert all(0 < delay <= 0.05 for delay in sleeps)


def test_exhausted_retries_report_the_last_failure_and_stop_calling() -> None:
    sleeps: list[float] = []
    transport = _Scripted(*[HttpResponse(status=503, body=b"{}", headers={})] * 5)
    provider = _provider(transport, max_attempts=3, sleeps=sleeps)  # type: ignore[arg-type]

    result, instance = provider.parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.OVERLOADED.value
    assert result.retryable is True
    assert transport.call_count == 3
    assert len(sleeps) == 2


def test_a_non_retryable_failure_is_never_retried() -> None:
    transport = _Scripted(
        HttpResponse(status=401, body=b"{}", headers={}),
        _envelope('{"a": "x"}'),
    )

    result, instance = _provider(transport, max_attempts=3).parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert transport.call_count == 1


def test_retry_after_is_honoured_within_the_configured_cap() -> None:
    sleeps: list[float] = []
    transport = _Scripted(
        HttpResponse(status=429, body=b"{}", headers={"Retry-After": "60"}),
        _envelope('{"a": "x"}'),
    )
    provider = _provider(
        transport,
        max_attempts=2,
        max_delay_seconds=5.0,
        sleeps=sleeps,  # type: ignore[arg-type]
    )

    result, _instance = provider.parse(_request(), TinyClaims)

    assert isinstance(result, ParseSuccess)
    assert sleeps == [5.0]


def test_a_timeout_is_typed_and_bounded() -> None:
    transport = _Scripted(TimeoutError(), TimeoutError())

    result, instance = _provider(transport, max_attempts=2).parse(_request(), TinyClaims)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.TIMEOUT.value
    assert result.retryable is True
    assert transport.call_count == 2


def test_a_network_failure_is_typed() -> None:
    transport = _Scripted(
        urllib.error.URLError(TimeoutError()),
        urllib.error.URLError(OSError("connection reset")),
    )
    provider = _provider(transport, max_attempts=1)

    first, _ = provider.parse(_request(), TinyClaims)
    second, _ = provider.parse(_request(), TinyClaims)

    assert isinstance(first, ParseFailure)
    assert first.provider_error == ProviderErrorCode.TIMEOUT.value
    assert isinstance(second, ParseFailure)
    assert second.provider_error == ProviderErrorCode.SERVER_ERROR.value


def test_a_failure_never_carries_the_credential_the_prompt_or_the_response() -> None:
    transport = _Scripted(_envelope("sensitive-marker-DO-NOT-LEAK"))

    result, _ = _provider(transport).parse(_request(), TinyClaims)

    rendered = f"{result!r} {result.model_dump_json()}"
    assert _API_KEY not in rendered
    assert "sensitive-marker-DO-NOT-LEAK" not in rendered
    assert _BODY not in rendered


def test_tool_messages_are_refused_rather_than_dropped() -> None:
    tool_request = ParseRequest(
        messages=(
            ChatMessage(role=ChatRole.SYSTEM, content="system rules"),
            ChatMessage(role=ChatRole.TOOL, content="result", tool_call_id="call_1"),
        ),
        schema_name="extraction_claims",
        json_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        model=_MODEL,
        purpose=ModelPurpose.EXTRACT,
    )
    transport = _Scripted(_envelope('{"a": "x"}'))

    with pytest.raises(Exception, match="text messages only"):
        _provider(transport).parse(tool_request, TinyClaims)

    assert transport.call_count == 0


def test_complete_reports_that_the_slice_does_not_implement_it() -> None:
    provider = _provider(_Scripted(_envelope('{"a": "x"}')))

    with pytest.raises(Exception, match="structured extraction only"):
        provider.complete(object())  # type: ignore[arg-type]


def test_no_credential_is_a_typed_configuration_failure() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        GroqProvider(api_key="   ", model=_MODEL)


def test_a_non_https_base_url_is_refused() -> None:
    with pytest.raises(ProviderNotConfiguredError, match="HTTPS"):
        GroqProvider(api_key=_API_KEY, model=_MODEL, base_url="http://api.groq.com/openai/v1")


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 301},
        {"max_response_bytes": 10},
        {"max_output_tokens": 1},
        {"max_attempts": 0},
        {"jitter_ratio": 2},
        {"base_delay_seconds": 2.0, "max_delay_seconds": 1.0},
        {"model": ""},
    ],
)
def test_out_of_range_bounds_are_refused(overrides: dict[str, object]) -> None:
    payload: dict[str, object] = {"api_key": _API_KEY, "model": _MODEL, **overrides}

    with pytest.raises(ValueError, match="must be"):
        GroqProvider(**payload)  # type: ignore[arg-type]
