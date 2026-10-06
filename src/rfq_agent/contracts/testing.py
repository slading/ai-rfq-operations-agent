"""Test doubles for the provider-neutral contracts.

Shipped with the package (not hidden in ``tests/``) because Phase 3's
``RecordingProvider`` and Phase 7's eval harness both need a scriptable
provider, and because its existence proves :class:`LLMProvider` is implementable
without touching a vendor SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from rfq_agent.contracts.llm import (
    CompletionRequest,
    CompletionResult,
    FinishReason,
    ParseFailure,
    ParseIssue,
    ParseRequest,
    ParseSuccess,
    ProviderCapabilities,
    ProviderName,
    TokenUsage,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel as PydanticModel

    from rfq_agent.contracts.llm import ParseResult
    from rfq_agent.domain.values import Json

__all__ = ["FakeLLMProvider", "ScriptedStep", "completion_text", "parse_success"]


class ScriptedStep:
    """One canned provider response, paired with the request that produced it."""

    def __init__(self, result: CompletionResult | ParseSuccess | ParseFailure) -> None:
        self.result = result
        #: The request this step answered; set by the fake when consumed.
        self.request: CompletionRequest | ParseRequest | None = None


class FakeLLMProvider:
    """A scriptable, offline :class:`~rfq_agent.contracts.llm.LLMProvider`.

    Behaviour is exactly as scripted, which is what makes it useful: tests can
    assert on the requests the application built, and can inject a malformed
    output or a 429-shaped failure without any network access.
    """

    def __init__(
        self,
        steps: Sequence[CompletionResult | ParseSuccess | ParseFailure],
        *,
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self._steps: list[ScriptedStep] = [ScriptedStep(step) for step in steps]
        self._cursor = 0
        self._capabilities = capabilities or ProviderCapabilities(
            provider=ProviderName.REPLAY,
            model="fake/scripted",
            supports_tools=True,
            supports_strict_json_schema=True,
            max_context_tokens=131_072,
        )
        #: Every request received, in order. Assertions read this.
        self.requests: list[CompletionRequest | ParseRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return the scripted capabilities."""
        return self._capabilities

    @property
    def call_count(self) -> int:
        """How many requests have been served."""
        return self._cursor

    @property
    def exhausted(self) -> bool:
        """Whether every scripted step has been consumed."""
        return self._cursor >= len(self._steps)

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """Return the next scripted completion result."""
        step = self._next(request)
        result = step.result
        if not isinstance(result, CompletionResult):
            msg = f"scripted step {self._cursor} is not a CompletionResult"
            raise TypeError(msg)
        return result

    def parse(
        self, request: ParseRequest, model: type[PydanticModel]
    ) -> tuple[ParseResult, PydanticModel | None]:
        """Return the next scripted parse outcome, validating any success payload."""
        step = self._next(request)
        result = step.result
        if isinstance(result, ParseFailure):
            return result, None
        if not isinstance(result, ParseSuccess):
            msg = f"scripted step {self._cursor} is not a parse outcome"
            raise TypeError(msg)
        try:
            instance = model.model_validate(result.data)
        except ValidationError as exc:
            issues = tuple(
                ParseIssue(
                    field=".".join(str(part) for part in error["loc"]) or "<root>",
                    message=error["msg"],
                    error_type=error["type"],
                )
                for error in exc.errors()
            )
            return (
                ParseFailure(
                    issues=issues,
                    raw=str(result.data),
                    retryable=False,
                    usage=result.usage,
                    latency_ms=result.latency_ms,
                    model=result.model,
                ),
                None,
            )
        return result, instance

    def _next(self, request: CompletionRequest | ParseRequest) -> ScriptedStep:
        """Consume the next scripted step, recording the request."""
        self.requests.append(request)
        if self._cursor >= len(self._steps):
            msg = "FakeLLMProvider has no more scripted steps"
            raise AssertionError(msg)
        step = self._steps[self._cursor]
        step.request = request
        self._cursor += 1
        return step


def completion_text(
    text: str,
    *,
    model: str = "fake/scripted",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> CompletionResult:
    """Build a minimal successful completion result for scripting."""
    return CompletionResult(
        text=text,
        finish_reason=FinishReason.STOP,
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        latency_ms=12,
        model=model,
    )


def parse_success(
    data: dict[str, Json],
    *,
    model: str = "fake/scripted",
    strict: bool = True,
) -> ParseSuccess:
    """Build a successful structured-output step for scripting."""
    return ParseSuccess(
        data=data,
        strict_mode_used=strict,
        usage=TokenUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
        latency_ms=30,
        model=model,
        raw=str(data),
    )
