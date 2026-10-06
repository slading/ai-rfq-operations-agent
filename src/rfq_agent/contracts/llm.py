"""Provider-neutral LLM contracts (architecture §4.7).

The whole application depends on this module and on nothing vendor-specific.
``GroqAdapter`` (Phase 3) is the only place that knows about ``response_format``,
Groq error codes, or which Groq model supports constrained decoding.

Two design points worth defending:

* **Two call shapes, not one.** ``complete()`` drives a tool-calling loop and
  returns tool calls; ``parse()`` binds output to a JSON schema and returns
  either validated data or a structured failure. Groq's strict structured
  output mode is model-restricted and its ``gpt-oss`` models do not do parallel
  tool calls, so combining both in one call would be fragile. Splitting them is
  both more robust and more evaluable (§4.4).
* **``parse()`` does not retry.** Retry/repair policy is a pipeline concern, so
  a failure comes back as data (:class:`ParseFailure`) rather than as an
  exception the caller has to catch and interpret.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from rfq_agent.domain.values import Json

if TYPE_CHECKING:
    from pydantic import BaseModel as _PydanticModel

__all__ = [
    "ChatMessage",
    "ChatRole",
    "CompletionRequest",
    "CompletionResult",
    "FinishReason",
    "LLMProvider",
    "ModelPurpose",
    "ParseFailure",
    "ParseIssue",
    "ParseRequest",
    "ParseResult",
    "ParseSuccess",
    "ProviderCapabilities",
    "ProviderName",
    "TokenUsage",
    "ToolCall",
    "ToolChoice",
    "ToolParameterSchema",
    "ToolSpec",
]

_TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
_SCHEMA_NAME_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
_MAX_TOOLS = 16
_MAX_MESSAGES = 64


class ProviderName(StrEnum):
    """Known providers. V1 ships one; the enum exists so the port stays honest."""

    GROQ = "groq"
    #: Record/replay provider used by tests and CI (decision D3).
    REPLAY = "replay"


class ChatRole(StrEnum):
    """Chat message roles."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ModelPurpose(StrEnum):
    """Why a model call is happening.

    A stable label used as the observability dimension for cost, latency and
    failure analysis: it lets you answer "where do our tokens go?" without
    parsing prompts.
    """

    TRIAGE = "triage"
    EXTRACT = "extract"
    RESOLVE = "resolve"
    #: A repair attempt after a schema-validation failure.
    REPAIR = "repair"
    #: Trust-boundary classifier (§8.3 layer 6).
    CLASSIFY_TRUST = "classify_trust"


class FinishReason(StrEnum):
    """Why the provider stopped generating."""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class ToolChoice(StrEnum):
    """How the model may use tools."""

    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"


class _ContractModel(BaseModel):
    """Base for wire-level contracts: immutable and closed.

    Whitespace stripping is safe here because nothing in this layer hashes its
    own content; the untrusted-content snapshot in the domain layer deliberately
    does not strip, so its digest stays exact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ToolParameterSchema(_ContractModel):
    """JSON Schema for a tool's arguments.

    Validated just enough to be useful: it must be an object schema with a
    ``properties`` mapping, which is what every provider we target requires.
    """

    schema_: Annotated[dict[str, Json], Field(alias="schema", min_length=1)]

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        """Require an object schema with declared properties."""
        if self.schema_.get("type") != "object":
            msg = "tool parameter schema must declare type=object"
            raise ValueError(msg)
        properties = self.schema_.get("properties")
        if not isinstance(properties, dict):
            msg = "tool parameter schema must declare a properties object"
            raise ValueError(msg)
        return self


class ToolSpec(_ContractModel):
    """A tool the model may call.

    ``read_only`` is load-bearing: the resolve agent's registry contains only
    read-only tools, which is what makes "the agent cannot take a consequential
    action" a structural property rather than a prompt instruction (§4.1).
    """

    name: Annotated[str, StringConstraints(pattern=_TOOL_NAME_PATTERN)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    parameters: ToolParameterSchema
    read_only: bool = True
    #: Stage(s) in which this tool is registered. Empty means "everywhere".
    stages: tuple[str, ...] = ()


class ToolCall(_ContractModel):
    """A tool invocation requested by the model."""

    id: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    name: Annotated[str, StringConstraints(pattern=_TOOL_NAME_PATTERN)]
    #: Decoded arguments. Never executed directly: the registry re-validates
    #: them against the tool's schema before dispatch.
    arguments: dict[str, Json] = Field(default_factory=dict)


class ChatMessage(_ContractModel):
    """One message in a completion request."""

    role: ChatRole
    content: str = ""
    #: Present on assistant messages that requested tools.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Present on ``tool`` role messages: the id being answered.
    tool_call_id: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    #: Optional display name for the tool that produced a ``tool`` message.
    name: Annotated[str, StringConstraints(pattern=_TOOL_NAME_PATTERN)] | None = None

    @model_validator(mode="after")
    def _check_role_contract(self) -> Self:
        """Role-specific fields must be present exactly when required."""
        if self.role is ChatRole.TOOL and self.tool_call_id is None:
            msg = "tool_call_id is required for role=tool"
            raise ValueError(msg)
        if self.role is not ChatRole.TOOL and self.tool_call_id is not None:
            msg = "tool_call_id is only valid for role=tool"
            raise ValueError(msg)
        if self.role is not ChatRole.ASSISTANT and self.tool_calls:
            msg = "tool_calls are only valid for role=assistant"
            raise ValueError(msg)
        if not self.content.strip() and not self.tool_calls:
            msg = "a message must carry content or tool calls"
            raise ValueError(msg)
        return self


class TokenUsage(_ContractModel):
    """Token accounting for one call."""

    prompt_tokens: Annotated[int, Field(ge=0)] = 0
    completion_tokens: Annotated[int, Field(ge=0)] = 0
    total_tokens: Annotated[int, Field(ge=0)] = 0
    #: Prompt-cache hits, where the provider reports them.
    cached_tokens: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _check_total(self) -> Self:
        """A reported total must agree with its parts."""
        if self.total_tokens and self.total_tokens != self.prompt_tokens + self.completion_tokens:
            msg = "total_tokens must equal prompt_tokens + completion_tokens"
            raise ValueError(msg)
        return self


class ProviderCapabilities(_ContractModel):
    """What the configured provider/model can do.

    Checked before a call, not discovered by a 400 response: Groq's strict
    JSON-schema mode is only available on some models, and silently falling
    back to best-effort JSON would quietly drop a correctness guarantee.
    """

    provider: ProviderName
    model: str
    supports_tools: bool = False
    supports_parallel_tool_calls: bool = False
    supports_strict_json_schema: bool = False
    supports_reasoning_effort: bool = False
    max_context_tokens: Annotated[int, Field(ge=1)] | None = None


class CompletionRequest(_ContractModel):
    """A tool-calling completion request."""

    messages: Annotated[tuple[ChatMessage, ...], Field(min_length=1, max_length=_MAX_MESSAGES)]
    tools: Annotated[tuple[ToolSpec, ...], Field(max_length=_MAX_TOOLS)] = ()
    tool_choice: ToolChoice = ToolChoice.AUTO
    model: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    purpose: ModelPurpose
    temperature: Decimal = Decimal("0")
    seed: int | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=32_768)] | None = None

    @model_validator(mode="after")
    def _check_tool_choice(self) -> Self:
        """``tool_choice`` must be meaningful given the tool list."""
        if self.tool_choice is not ToolChoice.NONE and not self.tools:
            msg = "tool_choice requires at least one tool"
            raise ValueError(msg)
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            msg = "tool names must be unique"
            raise ValueError(msg)
        return self


class CompletionResult(_ContractModel):
    """The outcome of one :class:`CompletionRequest`."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = FinishReason.STOP
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: Annotated[int, Field(ge=0)] = 0
    model: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    #: Provider-side request id, for correlating with provider dashboards.
    provider_request_id: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = (
        None
    )

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        """Tool calls only with ``finish_reason=tool_calls``; never both empty."""
        if self.tool_calls and self.finish_reason is not FinishReason.TOOL_CALLS:
            msg = "tool_calls require finish_reason=tool_calls"
            raise ValueError(msg)
        if not self.text.strip() and not self.tool_calls:
            msg = "a completion must return text or tool calls"
            raise ValueError(msg)
        return self


class ParseRequest(_ContractModel):
    """A structured-output request bound to a JSON schema."""

    messages: Annotated[tuple[ChatMessage, ...], Field(min_length=1, max_length=_MAX_MESSAGES)]
    schema_name: Annotated[str, StringConstraints(pattern=_SCHEMA_NAME_PATTERN)]
    schema_description: Annotated[str, StringConstraints(min_length=1, max_length=500)] = ""
    #: JSON Schema the provider should constrain decoding with, when supported.
    json_schema: Annotated[dict[str, Json], Field(min_length=1)]
    model: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    purpose: ModelPurpose
    temperature: Decimal = Decimal("0")
    seed: int | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=32_768)] | None = None


class ParseIssue(_ContractModel):
    """One validation problem, safe to feed back to the model as a repair hint."""

    field: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    message: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    error_type: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "value_error"


class ParseSuccess(_ContractModel):
    """Structured output that parsed and validated."""

    kind: Literal["success"] = "success"
    data: dict[str, Json]
    #: True when the provider constrained decoding to the schema.
    strict_mode_used: bool = False
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: Annotated[int, Field(ge=0)] = 0
    model: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    raw: str = ""


class ParseFailure(_ContractModel):
    """Structured output that did not parse or did not validate.

    Returned as data so the pipeline can decide between a repair attempt and
    escalation. ``raw`` is retained for debugging and is persisted behind the
    redaction policy, never logged verbatim in production.
    """

    kind: Literal["failure"] = "failure"
    issues: tuple[ParseIssue, ...] = ()
    raw: str | None = None
    #: Set when the provider itself errored rather than returning bad output.
    provider_error: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    #: True when the same request might succeed unchanged (e.g. a 429).
    retryable: bool = False
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: Annotated[int, Field(ge=0)] = 0
    model: Annotated[str, StringConstraints(min_length=1, max_length=120)]

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        """A failure must explain itself."""
        if not self.issues and self.provider_error is None:
            msg = "a ParseFailure needs issues or a provider_error"
            raise ValueError(msg)
        return self


#: Discriminated union returned by :meth:`LLMProvider.parse`.
ParseResult = ParseSuccess | ParseFailure


@runtime_checkable
class LLMProvider(Protocol):
    """The only interface the application has to a language model.

    Synchronous by design: V1 runs one worker thread, and a sync port keeps the
    retry/backoff code ordinary and testable.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        """What this provider/model combination supports."""
        ...

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one tool-calling completion step."""
        ...

    def parse(
        self, request: ParseRequest, model: type[_PydanticModel]
    ) -> tuple[ParseResult, _PydanticModel | None]:
        """Bind output to ``model`` and return the outcome plus any instance.

        The second element is the validated instance on success and ``None`` on
        failure, so callers never have to re-validate.
        """
        ...


class RecordingLLMProvider(Protocol):
    """A provider that can record and replay interactions (decision D3).

    Recording makes the evaluation suite deterministic, offline and free; it is
    also the only way to show a reviewer exactly what the model was sent.
    """

    def start_recording(self, path: str) -> None:
        """Begin writing interactions to ``path``."""
        ...

    def stop_recording(self) -> None:
        """Flush and close the current recording."""
        ...

    @property
    def is_recording(self) -> bool:
        """Whether interactions are currently being recorded."""
        ...
