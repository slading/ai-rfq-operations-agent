"""Domain error taxonomy.

Every failure in the system has a *named* code, a decision about whether it is
retryable, and a safe outcome (§7). Exceptions carry the code so that the
workflow engine can route on it without string matching, and so that traces can
be aggregated by failure type.

These are deliberately provider-neutral: ``GroqAdapter`` (Phase 3) maps HTTP
429 / 400 ``json_validate_failed`` / socket timeouts onto these types.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

__all__ = [
    "RETRYABLE_PROVIDER_CODES",
    "DomainError",
    "IllegalTransitionError",
    "LLMError",
    "MalformedStructuredOutputError",
    "ProviderErrorCode",
    "ProviderNotConfiguredError",
    "ProviderRequestError",
    "ProviderUnsupportedError",
    "RateLimitedError",
    "ToolContractError",
    "ToolDeniedError",
    "ToolError",
    "ToolNotFoundError",
    "ToolTimeoutError",
    "WorkflowError",
    "WorkflowTimeoutError",
]


class ProviderErrorCode(StrEnum):
    """Provider-neutral error classification."""

    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTHENTICATION = "AUTHENTICATION"
    SERVER_ERROR = "SERVER_ERROR"
    OVERLOADED = "OVERLOADED"
    CONTENT_FILTERED = "CONTENT_FILTERED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNKNOWN = "UNKNOWN"


#: Codes that a bounded retry may legitimately resolve.
RETRYABLE_PROVIDER_CODES: frozenset[ProviderErrorCode] = frozenset(
    {
        ProviderErrorCode.RATE_LIMITED,
        ProviderErrorCode.TIMEOUT,
        ProviderErrorCode.SERVER_ERROR,
        ProviderErrorCode.OVERLOADED,
    }
)


class DomainError(Exception):
    """Base class for every error raised by this system."""

    #: Stable machine-readable code, safe to log and to aggregate on.
    code: str = "DOMAIN_ERROR"
    #: Whether a bounded retry could succeed.
    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        #: Free-form context. Must never contain secrets or raw customer text;
        #: the redaction tests in Phase 5 enforce this at the logging boundary.
        self.detail = detail

    def __str__(self) -> str:
        """Render as ``[CODE] message (detail)`` for logs and traces."""
        if self.detail:
            return f"[{self.code}] {self.message} ({self.detail})"
        return f"[{self.code}] {self.message}"


class WorkflowError(DomainError):
    """A workflow-level failure: illegal transition, budget exhausted, etc."""

    code = "WORKFLOW_ERROR"


class IllegalTransitionError(WorkflowError):
    """A state transition was attempted that the state machine does not allow."""

    code = "ILLEGAL_TRANSITION"


class WorkflowTimeoutError(WorkflowError):
    """The per-run time budget was exhausted."""

    code = "WORKFLOW_TIMEOUT"
    retryable = True


class LLMError(DomainError):
    """Base class for provider failures."""

    code = "LLM_ERROR"

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        provider_code: ProviderErrorCode = ProviderErrorCode.UNKNOWN,
    ) -> None:
        super().__init__(message, detail=detail)
        self.provider_code = provider_code
        self.retryable = provider_code in RETRYABLE_PROVIDER_CODES


class RateLimitedError(LLMError):
    """The provider returned 429 (Groq free tier: 30 RPM / 8K TPM)."""

    code = "RATE_LIMITED"

    def __init__(
        self,
        message: str = "provider rate limit reached",
        *,
        retry_after_seconds: Decimal | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, provider_code=ProviderErrorCode.RATE_LIMITED)
        #: Honour the provider's own hint when it gives one.
        self.retry_after_seconds = retry_after_seconds


class MalformedStructuredOutputError(LLMError):
    """The model returned output that does not satisfy the schema.

    Retrying here means a *repair* attempt with validation errors fed back, not
    a blind re-roll; the pipeline caps those at ``agent.max_repair_attempts``
    and then escalates to a human (§7 F20).
    """

    code = "MALFORMED_OUTPUT"

    def __init__(
        self,
        message: str = "model output failed schema validation",
        *,
        issues: tuple[str, ...] = (),
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, provider_code=ProviderErrorCode.MALFORMED_OUTPUT)
        self.issues = issues
        # Repair is worthwhile; a blind retry usually is not.
        self.retryable = False


class ProviderRequestError(LLMError):
    """A non-retryable request failure (invalid request, auth, unsupported)."""

    code = "PROVIDER_REQUEST"


class ProviderUnsupportedError(ProviderRequestError):
    """The configured model does not support a requested capability."""

    code = "PROVIDER_UNSUPPORTED"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message, detail=detail, provider_code=ProviderErrorCode.UNSUPPORTED)


class ProviderNotConfiguredError(ProviderRequestError):
    """No credentials are configured but a live call was requested."""

    code = "PROVIDER_NOT_CONFIGURED"

    def __init__(
        self, message: str = "provider is not configured", *, detail: str | None = None
    ) -> None:
        super().__init__(message, detail=detail, provider_code=ProviderErrorCode.NOT_CONFIGURED)


class ToolError(DomainError):
    """Base class for tool failures.

    Tool errors are returned *to the agent* as structured results so it can
    adapt; only repeated failure of an essential tool fails the run (§7 F22).
    """

    code = "TOOL_ERROR"


class ToolNotFoundError(ToolError):
    """The agent called a tool that is not in its registry for this stage."""

    code = "TOOL_NOT_FOUND"


class ToolContractError(ToolError):
    """Tool arguments did not satisfy the tool's schema."""

    code = "TOOL_CONTRACT"


class ToolDeniedError(ToolError):
    """The tool exists but this call is not permitted.

    This is the structural answer to prompt injection: a request for data the
    agent is not scoped to see is refused by the registry and the data layer,
    not by the model's judgement (§8.3 layers 3-4).
    """

    code = "TOOL_DENIED"
    retryable = False


class ToolTimeoutError(ToolError):
    """A tool call exceeded its time budget."""

    code = "TOOL_TIMEOUT"
    retryable = True
