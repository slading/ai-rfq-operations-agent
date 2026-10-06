"""Environment / configuration loading.

All tunables live here and nowhere else. Model identifiers, budgets, retry
policy and the human-approval policy are configuration, so swapping provider,
model or policy is a config-only change (architecture decision D1).

Conventions
-----------
* Every variable is prefixed ``RFQ_``.
* Nested sections use a double underscore: ``RFQ_GROQ__AGENT_MODEL``.
* Values are read from the process environment first, then ``.env``.
* ``Settings.load()`` never raises on a missing file; it raises a readable
  :class:`pydantic.ValidationError` on an invalid value.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "AgentSettings",
    "BusinessSettings",
    "CurrencyCode",
    "DatabaseSettings",
    "Environment",
    "GroqSettings",
    "HttpSettings",
    "LLMMode",
    "RetryPolicy",
    "Settings",
    "TrustSettings",
    "WorkflowSettings",
    "get_settings",
]

#: ISO-4217 alpha code. V1 is single-currency (architecture §12).
CurrencyCode = str
_CURRENCY_PATTERN = r"^[A-Z]{3}$"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class Environment(StrEnum):
    """Deployment environment."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LLMMode(StrEnum):
    """How the provider port obtains completions (architecture decision D3).

    ``REPLAY`` is the default and the only mode used in CI: it never touches
    the network, which makes the evaluation suite deterministic and free.
    """

    RECORD = "record"
    REPLAY = "replay"
    LIVE = "live"


def _as_bool(value: Any, field: str) -> bool:
    """Parse a boolean-ish environment value with an explicit error message."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    msg = f"{field} must be one of true/false/1/0/yes/no/on/off, got {value!r}"
    raise ValueError(msg)


class _ConfiguredModel(BaseModel):
    """Base for configuration sections: immutable, no unknown keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RetryPolicy(_ConfiguredModel):
    """Bounded retry policy for transient provider failures.

    Retries exist for availability, never for correctness: exhausting them
    produces ``FAILED_RETRYABLE``, not a best-effort guess.
    """

    max_attempts: int = Field(default=4, ge=1, le=10)
    base_delay_seconds: Decimal = Field(default=Decimal("1.0"), gt=0, le=60)
    max_delay_seconds: Decimal = Field(default=Decimal("30.0"), gt=0, le=600)
    jitter_ratio: Decimal = Field(default=Decimal("0.2"), ge=0, le=1)

    @model_validator(mode="after")
    def _check_delay_ordering(self) -> Self:
        """Ensure the ceiling is not below the base delay."""
        if self.max_delay_seconds < self.base_delay_seconds:
            msg = "max_delay_seconds must be >= base_delay_seconds"
            raise ValueError(msg)
        return self


class GroqSettings(_ConfiguredModel):
    """Groq provider configuration (V1 provider; not referenced by domain code)."""

    api_key: SecretStr | None = None
    base_url: str = "https://api.groq.com/openai/v1"

    #: Decision D1 - default model for the resolve agent.
    agent_model: str = Field(default="openai/gpt-oss-120b", min_length=1)
    #: Model used for intake triage; may be smaller/cheaper than the agent model.
    triage_model: str = Field(default="openai/gpt-oss-120b", min_length=1)

    #: Decision D4 - stay *under* the free-tier ceiling (30 RPM / 8K TPM).
    requests_per_minute_budget: int = Field(default=24, ge=1, le=10_000)
    tokens_per_minute_budget: int = Field(default=7_000, ge=1, le=10_000_000)
    request_timeout_seconds: Decimal = Field(default=Decimal("30"), gt=0, le=300)

    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @field_serializer("api_key")
    def _serialize_api_key(self, value: SecretStr | None, _info: Any) -> str:
        """Never emit the key. ``model_dump()`` on Settings must stay log-safe."""
        return "***redacted***" if value is not None else ""

    @property
    def has_credentials(self) -> bool:
        """Whether an API key is configured (required only for live mode)."""
        return self.api_key is not None and self.api_key.get_secret_value() != ""

    def require_credentials(self) -> str:
        """Return the API key or raise a clear configuration error."""
        if self.api_key is None:
            msg = "RFQ_GROQ__API_KEY is not set; live provider calls are unavailable"
            raise RuntimeError(msg)
        return self.api_key.get_secret_value()


class AgentSettings(_ConfiguredModel):
    """Hard ceilings for the agent loop (architecture §4.3)."""

    max_steps: int = Field(default=8, ge=1, le=32)
    max_tool_attempts_per_tool: int = Field(default=3, ge=1, le=10)
    max_tool_result_bytes: int = Field(default=8_192, ge=256, le=1_048_576)
    run_budget_seconds: Decimal = Field(default=Decimal("60"), gt=0, le=3_600)
    #: Structured-output repair attempts before escalating to a human (§4.6).
    max_repair_attempts: int = Field(default=2, ge=0, le=5)
    temperature: Decimal = Field(default=Decimal("0"), ge=0, le=2)
    random_seed: int | None = Field(default=1_337, ge=0)


class TrustSettings(_ConfiguredModel):
    """Trust-boundary configuration (architecture §8)."""

    always_classify: bool = False
    classifier_model: str = Field(default="openai/gpt-oss-safeguard-20b", min_length=1)
    canary_scan_strict: bool = True
    max_untrusted_bytes: int = Field(default=65_536, ge=1_024, le=10_485_760)


class WorkflowSettings(_ConfiguredModel):
    """Workflow policy knobs. The human gate is policy, not an accident."""

    require_human_approval: bool = True
    #: V1 has no auto-approve path; the flag exists so the absence is explicit.
    auto_approve_enabled: bool = False
    max_run_attempts: int = Field(default=3, ge=1, le=10)
    run_queue_poll_seconds: Decimal = Field(default=Decimal("1.0"), gt=0, le=60)

    @model_validator(mode="after")
    def _check_no_autonomous_send(self) -> Self:
        """Refuse a configuration that would allow sending without a human."""
        if self.auto_approve_enabled and not self.require_human_approval:
            msg = (
                "autonomous send is out of scope for V1: auto_approve_enabled=true "
                "requires require_human_approval=true"
            )
            raise ValueError(msg)
        return self


class BusinessSettings(_ConfiguredModel):
    """Business defaults. The demo policy ships as data: see ``rfq_agent.seed``."""

    currency: CurrencyCode = Field(default="EUR", pattern=_CURRENCY_PATTERN)
    tax_display_only: bool = True
    price_staleness_days: int = Field(default=180, ge=0, le=3_650)
    max_quantity_per_line: int = Field(default=10_000, ge=1, le=1_000_000)


class HttpSettings(_ConfiguredModel):
    """HTTP / operator console configuration (Phase 6)."""

    host: str = "0.0.0.0"  # noqa: S104 - bound deliberately for the container preview
    port: int = Field(default=8_000, ge=1, le=65_535)
    #: Decision D2 - server-rendered templates, no JS build step.
    ui_template_dir: Path = Path("src/rfq_agent/ui/templates")
    operator_token: SecretStr | None = None

    @field_serializer("operator_token")
    def _serialize_operator_token(self, value: SecretStr | None, _info: Any) -> str:
        """Never emit the operator token."""
        return "***redacted***" if value is not None else ""


class DatabaseSettings(_ConfiguredModel):
    """Persistence configuration (architecture §5.1).

    Every field is load-bearing: :mod:`rfq_agent.persistence.engine` derives the
    connection PRAGMAs from this section, so ``RFQ_DATABASE__*`` genuinely
    controls how the database is opened.
    """

    url: str = Field(default="sqlite:///var/rfq_agent.db", min_length=1)
    echo: bool = False
    #: How long a writer waits for a lock before failing (§5.1, WAL + busy_timeout).
    busy_timeout_ms: int = Field(default=5_000, ge=0, le=120_000)
    #: ``WAL`` lets the run worker write while the UI reads traces.
    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"] = "WAL"
    #: ``NORMAL`` is the standard durability/throughput point under WAL.
    synchronous: Literal["FULL", "NORMAL", "OFF"] = "NORMAL"
    #: Foreign-key enforcement is *not* optional: SQLite disables it per
    #: connection by default, and the schema relies on it to make references to
    #: customers, products and price entries real rather than decorative. The
    #: type is ``Literal[True]`` so ``RFQ_DATABASE__FOREIGN_KEYS=false`` fails at
    #: startup instead of silently weakening integrity.
    foreign_keys: Literal[True] = True


class Settings(BaseSettings):
    """Root application configuration."""

    model_config = SettingsConfigDict(
        env_prefix="RFQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        validate_default=True,
        frozen=True,
    )

    env: Environment = Environment.DEVELOPMENT
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    #: When true, logs and trace rows are redacted and prompts are stored as
    #: hashes only. Forced on in production by :meth:`_check_production`.
    redact_logs: bool = True
    log_file: str = "-"

    #: ``REPLAY`` by default so that no test or CI run can reach the network.
    llm_mode: LLMMode = LLMMode.REPLAY
    recordings_dir: Path = Path("recordings")

    groq: GroqSettings = Field(default_factory=GroqSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    trust: TrustSettings = Field(default_factory=TrustSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    business: BusinessSettings = Field(default_factory=BusinessSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    @field_validator("redact_logs", mode="before")
    @classmethod
    def _parse_redact_logs(cls, value: Any) -> bool:
        """Accept boolean-ish strings for ``RFQ_REDACT_LOGS``."""
        return _as_bool(value, "RFQ_REDACT_LOGS")

    @model_validator(mode="after")
    def _check_production(self) -> Self:
        """Enforce the non-negotiable production invariants."""
        if self.env is Environment.PRODUCTION and not self.redact_logs:
            msg = "redact_logs must be true when env=production"
            raise ValueError(msg)
        if self.llm_mode is LLMMode.LIVE and not self.groq.has_credentials:
            msg = "llm_mode=live requires RFQ_GROQ__API_KEY to be set"
            raise ValueError(msg)
        return self

    @property
    def is_production(self) -> bool:
        """Whether the process is configured for production."""
        return self.env is Environment.PRODUCTION

    @classmethod
    def load(cls, env_file: str | Path | None = None, **overrides: Any) -> Settings:
        """Load settings from the environment and an optional ``.env`` file.

        Missing files are ignored; invalid values raise ``ValidationError``.
        """
        if env_file is not None and Path(env_file).is_file():
            return cls(_env_file=str(env_file), **overrides)
        return cls(**overrides)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings.load()
