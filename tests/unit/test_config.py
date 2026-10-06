"""Tests for the configuration loader (``rfq_agent.config``).

These matter because configuration is where the V1 decisions live: the default
model (D1), replay-by-default evaluation (D3), free-tier budgets (D4) and the
human-approval policy.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from rfq_agent.config import (
    Environment,
    GroqSettings,
    HttpSettings,
    LLMMode,
    RetryPolicy,
    Settings,
    WorkflowSettings,
    get_settings,
)


def _clear_rfq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``RFQ_*`` variable so a test starts from a clean slate."""
    for key in list(os.environ):
        if key.startswith("RFQ_"):
            monkeypatch.delenv(key, raising=False)


class TestDefaults:
    def test_defaults_match_locked_decisions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        settings = Settings.load()

        # D1: default model is gpt-oss-120b, configurable.
        assert settings.groq.agent_model == "openai/gpt-oss-120b"
        assert settings.groq.triage_model == "openai/gpt-oss-120b"
        # D3: replay is the default, so nothing reaches the network by default.
        assert settings.llm_mode is LLMMode.REPLAY
        # D4: budgets sit under the free-tier ceiling of 30 RPM / 8K TPM.
        assert settings.groq.requests_per_minute_budget < 30
        assert settings.groq.tokens_per_minute_budget < 8_000
        # Human approval is on and there is no auto-approve path.
        assert settings.workflow.require_human_approval is True
        assert settings.workflow.auto_approve_enabled is False
        assert settings.redact_logs is True
        assert settings.env is Environment.DEVELOPMENT

    def test_no_credentials_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        settings = Settings.load()
        assert settings.groq.has_credentials is False
        with pytest.raises(RuntimeError, match="RFQ_GROQ__API_KEY"):
            settings.groq.require_credentials()


class TestEnvironmentOverrides:
    def test_scalar_and_nested_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_ENV", "development")
        monkeypatch.setenv("RFQ_GROQ__AGENT_MODEL", "qwen/qwen3.8-27b")
        monkeypatch.setenv("RFQ_AGENT__MAX_STEPS", "5")
        monkeypatch.setenv("RFQ_AGENT__TEMPERATURE", "0.25")
        monkeypatch.setenv("RFQ_BUSINESS__CURRENCY", "PLN")

        settings = Settings.load()

        assert settings.groq.agent_model == "qwen/qwen3.8-27b"
        assert settings.agent.max_steps == 5
        assert settings.agent.temperature == Decimal("0.25")
        assert settings.business.currency == "PLN"

    def test_boolean_parsing_accepts_common_spellings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_rfq_env(monkeypatch)
        for raw, expected in (("false", False), ("0", False), ("no", False), ("off", False)):
            monkeypatch.setenv("RFQ_REDACT_LOGS", raw)
            assert Settings.load().redact_logs is expected, raw

        for raw in ("true", "1", "YES", "On"):
            monkeypatch.setenv("RFQ_REDACT_LOGS", raw)
            assert Settings.load().redact_logs is True, raw

    def test_invalid_boolean_is_a_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_REDACT_LOGS", "perhaps")
        with pytest.raises(ValidationError, match="RFQ_REDACT_LOGS"):
            Settings.load()

    def test_out_of_range_budget_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_AGENT__MAX_STEPS", "0")
        with pytest.raises(ValidationError):
            Settings.load()

    def test_bad_currency_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_BUSINESS__CURRENCY", "EURO")
        with pytest.raises(ValidationError):
            Settings.load()

    def test_env_file_is_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "RFQ_GROQ__AGENT_MODEL=openai/gpt-oss-20b\n"
            "RFQ_AGENT__MAX_STEPS=6\n"
            "RFQ_LLM_MODE=replay\n",
            encoding="utf-8",
        )

        settings = Settings.load(env_file=env_file)

        assert settings.groq.agent_model == "openai/gpt-oss-20b"
        assert settings.agent.max_steps == 6

    def test_missing_env_file_is_ignored(self, tmp_path: Path) -> None:
        settings = Settings.load(env_file=tmp_path / "does-not-exist.env")
        assert settings.groq.agent_model == "openai/gpt-oss-120b"


class TestInvariants:
    def test_production_requires_redaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_ENV", "production")
        monkeypatch.setenv("RFQ_REDACT_LOGS", "false")
        with pytest.raises(ValidationError, match="redact_logs"):
            Settings.load()

    def test_live_mode_requires_an_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_LLM_MODE", "live")
        with pytest.raises(ValidationError, match="RFQ_GROQ__API_KEY"):
            Settings.load()

    def test_live_mode_with_key_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_LLM_MODE", "live")
        monkeypatch.setenv("RFQ_GROQ__API_KEY", "gsk_test_key")
        settings = Settings.load()
        assert settings.llm_mode is LLMMode.LIVE
        assert settings.groq.require_credentials() == "gsk_test_key"

    def test_autonomous_send_configuration_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="autonomous send"):
            WorkflowSettings(require_human_approval=False, auto_approve_enabled=True)

    def test_retry_ceiling_must_not_be_below_base(self) -> None:
        with pytest.raises(ValidationError, match="max_delay_seconds"):
            RetryPolicy(base_delay_seconds=Decimal("10"), max_delay_seconds=Decimal("5"))


class TestSecretHandling:
    def test_dump_never_contains_the_api_key(self) -> None:
        settings = Settings(groq=GroqSettings(api_key="gsk_super_secret_value"))

        dumped = str(settings.model_dump())
        json_dumped = settings.model_dump_json()

        assert "gsk_super_secret_value" not in dumped
        assert "gsk_super_secret_value" not in json_dumped
        assert "***redacted***" in dumped
        # The real value is still reachable in-process, for the adapter only.
        assert settings.groq.require_credentials() == "gsk_super_secret_value"

    def test_operator_token_is_never_dumped(self) -> None:
        settings = Settings(http=HttpSettings(operator_token=SecretStr("tok_secret_123")))
        assert "tok_secret_123" not in str(settings.model_dump())
        assert "tok_secret_123" not in settings.model_dump_json()


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
