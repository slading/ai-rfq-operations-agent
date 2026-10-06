"""Tests for the provider-neutral LLM contracts (architecture §4.7).

Two things are being verified:

1. the request/result schemas reject nonsense (so a malformed provider response
   cannot silently become a "successful" call);
2. :class:`LLMProvider` is genuinely implementable without a vendor SDK, which
   is the claim behind "not tightly coupled to Groq".
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from rfq_agent.contracts.errors import (
    RETRYABLE_PROVIDER_CODES,
    LLMError,
    MalformedStructuredOutputError,
    ProviderErrorCode,
    ProviderNotConfiguredError,
    RateLimitedError,
    ToolDeniedError,
    ToolTimeoutError,
)
from rfq_agent.contracts.llm import (
    ChatMessage,
    ChatRole,
    CompletionRequest,
    CompletionResult,
    FinishReason,
    LLMProvider,
    ModelPurpose,
    ParseFailure,
    ParseIssue,
    ParseRequest,
    ParseSuccess,
    ProviderCapabilities,
    ProviderName,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolParameterSchema,
    ToolSpec,
)
from rfq_agent.contracts.testing import (
    FakeLLMProvider,
    completion_text,
    parse_success,
)
from tests.conftest import make_capabilities


class TinySchema(BaseModel):
    """Minimal schema used to exercise the parse contract."""

    is_rfq: bool
    reason: str = ""


def tool_spec(**overrides: object) -> ToolSpec:
    payload: dict[str, object] = {
        "name": "search_catalog",
        "description": "Search the product catalog by free text.",
        "parameters": {
            "schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
        },
    }
    payload.update(overrides)
    return ToolSpec.model_validate(payload)


class TestToolSpec:
    def test_read_only_by_default(self) -> None:
        assert tool_spec().read_only is True

    def test_parameter_schema_must_be_an_object(self) -> None:
        with pytest.raises(ValidationError, match="type=object"):
            ToolParameterSchema(schema={"type": "string"})  # type: ignore[typeddict-item]

    def test_parameter_schema_requires_properties(self) -> None:
        with pytest.raises(ValidationError, match="properties"):
            ToolParameterSchema(schema={"type": "object"})  # type: ignore[typeddict-item]

    def test_tool_name_must_be_snake_case(self) -> None:
        with pytest.raises(ValidationError):
            tool_spec(name="SearchCatalog")

    def test_alias_allows_the_schema_key(self) -> None:
        spec = tool_spec()
        assert spec.parameters.schema_["type"] == "object"


class TestChatMessage:
    def test_tool_message_requires_a_call_id(self) -> None:
        with pytest.raises(ValidationError, match="tool_call_id is required"):
            ChatMessage(role=ChatRole.TOOL, content="{}")

    def test_non_tool_message_must_not_carry_a_call_id(self) -> None:
        with pytest.raises(ValidationError, match="only valid for role=tool"):
            ChatMessage(role=ChatRole.USER, content="hi", tool_call_id="call_1")

    def test_tool_calls_only_on_assistant_messages(self) -> None:
        with pytest.raises(ValidationError, match="only valid for role=assistant"):
            ChatMessage(
                role=ChatRole.USER,
                content="hi",
                tool_calls=(ToolCall(id="c1", name="search_catalog"),),
            )

    def test_empty_message_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="content or tool calls"):
            ChatMessage(role=ChatRole.USER, content="   ")

    def test_assistant_tool_call_message_is_valid(self) -> None:
        message = ChatMessage(
            role=ChatRole.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="c1", name="search_catalog", arguments={"query": "X-120"}),),
        )
        assert message.tool_calls[0].arguments["query"] == "X-120"


class TestCompletionRequest:
    def test_tool_choice_requires_tools(self) -> None:
        with pytest.raises(ValidationError, match="requires at least one tool"):
            CompletionRequest(
                messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
                model="openai/gpt-oss-120b",
                purpose=ModelPurpose.RESOLVE,
                tool_choice=ToolChoice.AUTO,
            )

    def test_duplicate_tool_names_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            CompletionRequest(
                messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
                tools=(tool_spec(), tool_spec()),
                model="openai/gpt-oss-120b",
                purpose=ModelPurpose.RESOLVE,
            )

    def test_valid_request(self) -> None:
        request = CompletionRequest(
            messages=(ChatMessage(role=ChatRole.SYSTEM, content="you are a helper"),),
            tools=(tool_spec(),),
            model="openai/gpt-oss-120b",
            purpose=ModelPurpose.RESOLVE,
            temperature=Decimal("0"),
            seed=1337,
        )
        assert request.tool_choice is ToolChoice.AUTO
        assert request.temperature == Decimal("0")

    def test_empty_message_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CompletionRequest(
                messages=(),
                model="openai/gpt-oss-120b",
                purpose=ModelPurpose.TRIAGE,
                tool_choice=ToolChoice.NONE,
            )


class TestCompletionResult:
    def test_tool_calls_require_the_matching_finish_reason(self) -> None:
        with pytest.raises(ValidationError, match="finish_reason=tool_calls"):
            CompletionResult(
                text="",
                tool_calls=(ToolCall(id="c1", name="search_catalog"),),
                finish_reason=FinishReason.STOP,
                model="openai/gpt-oss-120b",
            )

    def test_empty_result_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="text or tool calls"):
            CompletionResult(text="  ", model="openai/gpt-oss-120b")

    def test_valid_text_result(self) -> None:
        result = completion_text("hello")
        assert result.text == "hello"
        assert result.usage.total_tokens == 15


class TestTokenUsage:
    def test_total_must_agree_with_parts(self) -> None:
        with pytest.raises(ValidationError, match="total_tokens"):
            TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=99)

    def test_zero_usage_is_valid(self) -> None:
        assert TokenUsage().total_tokens == 0

    def test_cached_tokens_are_tracked_separately(self) -> None:
        usage = TokenUsage(
            prompt_tokens=100, completion_tokens=20, total_tokens=120, cached_tokens=80
        )
        assert usage.cached_tokens == 80


class TestParseOutcomes:
    def test_failure_needs_an_explanation(self) -> None:
        with pytest.raises(ValidationError, match="issues or a provider_error"):
            ParseFailure(model="openai/gpt-oss-120b")

    def test_failure_with_issues(self) -> None:
        failure = ParseFailure(
            issues=(ParseIssue(field="is_rfq", message="field required"),),
            raw="{not json",
            model="openai/gpt-oss-120b",
        )
        assert failure.retryable is False

    def test_success_records_whether_strict_mode_was_used(self) -> None:
        success = parse_success({"is_rfq": True})
        assert success.strict_mode_used is True
        assert success.kind == "success"


class TestParseRequest:
    def test_requires_a_json_schema(self) -> None:
        with pytest.raises(ValidationError):
            ParseRequest(
                messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
                schema_name="triage",
                json_schema={},
                model="openai/gpt-oss-120b",
                purpose=ModelPurpose.TRIAGE,
            )

    def test_valid_request(self) -> None:
        request = ParseRequest(
            messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
            schema_name="triage_result",
            json_schema={"type": "object", "properties": {"is_rfq": {"type": "boolean"}}},
            model="openai/gpt-oss-120b",
            purpose=ModelPurpose.TRIAGE,
        )
        assert request.temperature == Decimal("0")


class TestProviderIsImplementable:
    def test_fake_provider_satisfies_the_protocol(self) -> None:
        provider = FakeLLMProvider([completion_text("ok")])
        assert isinstance(provider, LLMProvider)

    def test_complete_returns_the_scripted_result(self) -> None:
        provider = FakeLLMProvider([completion_text("first"), completion_text("second")])
        request = CompletionRequest(
            messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
            model="openai/gpt-oss-120b",
            purpose=ModelPurpose.TRIAGE,
            tool_choice=ToolChoice.NONE,
        )

        assert provider.complete(request).text == "first"
        assert provider.complete(request).text == "second"
        assert provider.call_count == 2
        assert provider.exhausted is True
        assert provider.requests[0].purpose is ModelPurpose.TRIAGE

    def test_exhausted_provider_fails_loudly(self) -> None:
        provider = FakeLLMProvider([])
        request = CompletionRequest(
            messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
            model="openai/gpt-oss-120b",
            purpose=ModelPurpose.TRIAGE,
            tool_choice=ToolChoice.NONE,
        )
        with pytest.raises(AssertionError, match="no more scripted steps"):
            provider.complete(request)

    def test_parse_validates_against_the_model(self) -> None:
        provider = FakeLLMProvider([parse_success({"is_rfq": True, "reason": "pricing request"})])
        request = ParseRequest(
            messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
            schema_name="triage_result",
            json_schema={"type": "object"},
            model="openai/gpt-oss-120b",
            purpose=ModelPurpose.TRIAGE,
        )

        result, instance = provider.parse(request, TinySchema)

        assert isinstance(result, ParseSuccess)
        assert isinstance(instance, TinySchema)
        assert instance is not None
        assert instance.is_rfq is True

    def test_parse_failure_is_returned_as_data_not_raised(self) -> None:
        provider = FakeLLMProvider([parse_success({"reason": "missing the boolean"})])
        request = ParseRequest(
            messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
            schema_name="triage_result",
            json_schema={"type": "object"},
            model="openai/gpt-oss-120b",
            purpose=ModelPurpose.TRIAGE,
        )

        result, instance = provider.parse(request, TinySchema)

        assert isinstance(result, ParseFailure)
        assert instance is None
        assert result.issues[0].field == "is_rfq"

    def test_scripted_failure_step_is_passed_through(self) -> None:
        failure = ParseFailure(
            provider_error="rate_limited", retryable=True, model="openai/gpt-oss-120b"
        )
        provider = FakeLLMProvider([failure])
        request = ParseRequest(
            messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
            schema_name="triage_result",
            json_schema={"type": "object"},
            model="openai/gpt-oss-120b",
            purpose=ModelPurpose.TRIAGE,
        )

        result, instance = provider.parse(request, TinySchema)

        assert result is failure
        assert instance is None

    def test_capabilities_default_matches_the_v1_model(self) -> None:
        capabilities = make_capabilities()
        assert capabilities.provider is ProviderName.GROQ
        assert capabilities.model == "openai/gpt-oss-120b"
        assert capabilities.supports_strict_json_schema is True
        # gpt-oss models on Groq do not support parallel tool calls.
        assert capabilities.supports_parallel_tool_calls is False

    def test_capabilities_can_be_scripted(self) -> None:
        provider = FakeLLMProvider(
            [], capabilities=make_capabilities(supports_strict_json_schema=False)
        )
        assert provider.capabilities.supports_strict_json_schema is False


class TestErrorTaxonomy:
    def test_rate_limit_is_retryable_and_carries_a_hint(self) -> None:
        error = RateLimitedError(retry_after_seconds=Decimal("12.5"))
        assert error.retryable is True
        assert error.provider_code is ProviderErrorCode.RATE_LIMITED
        assert error.retry_after_seconds == Decimal("12.5")
        assert "RATE_LIMITED" in str(error)

    def test_malformed_output_is_not_blindly_retryable(self) -> None:
        error = MalformedStructuredOutputError(issues=("quantity: field required",))
        assert error.retryable is False
        assert error.issues == ("quantity: field required",)

    def test_tool_denied_is_never_retryable(self) -> None:
        assert ToolDeniedError("cross-customer pricing is not accessible").retryable is False

    def test_tool_timeout_is_retryable(self) -> None:
        assert ToolTimeoutError("check_stock timed out").retryable is True

    def test_not_configured_error(self) -> None:
        error = ProviderNotConfiguredError()
        assert error.provider_code is ProviderErrorCode.NOT_CONFIGURED
        assert error.retryable is False

    def test_retryable_code_set(self) -> None:
        assert (
            frozenset(
                {
                    ProviderErrorCode.RATE_LIMITED,
                    ProviderErrorCode.TIMEOUT,
                    ProviderErrorCode.SERVER_ERROR,
                    ProviderErrorCode.OVERLOADED,
                }
            )
            == RETRYABLE_PROVIDER_CODES
        )
        assert ProviderErrorCode.MALFORMED_OUTPUT not in RETRYABLE_PROVIDER_CODES

    def test_error_detail_is_attached_to_the_message(self) -> None:
        error = LLMError("boom", detail="stage=resolve")
        assert "stage=resolve" in str(error)


class TestProviderCapabilitiesSchema:
    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProviderCapabilities(
                provider=ProviderName.GROQ,
                model="openai/gpt-oss-120b",
                supports_vision=True,
            )
