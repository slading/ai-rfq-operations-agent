"""Tests for the configuration loader (``rfq_agent.config``).

These matter because configuration is where the V1 decisions live: the default
model (D1), replay-by-default evaluation (D3), free-tier budgets (D4) and the
human-approval policy.
"""

from __future__ import annotations

import os
import subprocess
import sys
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

#: Repository root: ``alembic.ini`` and ``migrations/`` live there.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Synthetic credentials for the F3-B disclosure tests. Never real secrets.
_API_KEY = "gsk_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
_OPERATOR_TOKEN = "OPTOKEN_A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # noqa: S105 - synthetic
_DATABASE_PASSWORD = "S3cretDbPassw0rd0123456789"  # noqa: S105 - synthetic
_DATABASE_URL = f"postgresql://rfquser:{_DATABASE_PASSWORD}@db.internal:5432/rfq"
#: Shorter and longer than the ~52-character window Pydantic renders.
_SHORT_KEY = "gsk_short_Ab12"
_LONG_KEY = "gsk_" + "A1b2C3d4E5f6" * 6


def _clear_rfq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``RFQ_*`` variable so a test starts from a clean slate."""
    for key in list(os.environ):
        if key.startswith("RFQ_"):
            monkeypatch.delenv(key, raising=False)


def _leaked_fragments(text: str, secret: str, *, min_length: int = 8) -> list[str]:
    """Meaningful substrings of ``secret`` that still appear in ``text``.

    Pydantic truncates the values it renders, keeping a leading and a trailing
    window, so "the whole secret is absent" proves very little on its own. This
    tests every window of at least ``min_length`` characters - prefixes,
    suffixes and interior slices alike - and returns the ones that survived.
    """
    return sorted(
        {
            secret[start : start + length]
            for length in range(min_length, len(secret) + 1)
            for start in range(len(secret) - length + 1)
            if secret[start : start + length] in text
        },
        key=len,
        reverse=True,
    )


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


class TestValidationErrorDisclosure:
    """F3-B: nothing a validator reads may be rendered back to a caller.

    A model-level validator failure makes Pydantic echo the raw input mapping,
    and an over-long value keeps its head and its tail; either can print an API
    key, the operator token or a password to stderr.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run from an empty directory so no developer ``.env`` participates.

        ``Settings`` reads ``.env`` from the working directory; without this a
        real credential could enter the mapping under test - and a failure
        message could end up quoting it.
        """
        monkeypatch.chdir(tmp_path)

    @staticmethod
    def _production_error(monkeypatch: pytest.MonkeyPatch, **env: str) -> ValidationError:
        """Return the error raised by the production redaction invariant."""
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_ENV", "production")
        monkeypatch.setenv("RFQ_REDACT_LOGS", "false")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError) as raised:
            Settings.load()
        return raised.value

    @pytest.mark.parametrize(
        "secret", [_SHORT_KEY, _API_KEY, _LONG_KEY], ids=["short", "typical", "long"]
    )
    def test_api_key_never_reaches_the_message(
        self, monkeypatch: pytest.MonkeyPatch, secret: str
    ) -> None:
        rendered = str(self._production_error(monkeypatch, RFQ_GROQ__API_KEY=secret))

        assert "redact_logs must be true when env=production" in rendered
        assert "input_value" not in rendered
        assert _leaked_fragments(rendered, secret) == []

    def test_operator_token_never_reaches_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rendered = str(
            self._production_error(monkeypatch, RFQ_HTTP__OPERATOR_TOKEN=_OPERATOR_TOKEN)
        )

        assert "redact_logs must be true when env=production" in rendered
        assert "input_value" not in rendered
        assert _leaked_fragments(rendered, _OPERATOR_TOKEN) == []

    def test_database_credentials_never_reach_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rendered = str(self._production_error(monkeypatch, RFQ_DATABASE__URL=_DATABASE_URL))

        assert "input_value" not in rendered
        assert _leaked_fragments(rendered, _DATABASE_PASSWORD) == []
        assert _leaked_fragments(rendered, _DATABASE_URL) == []

    def test_malformed_section_value_stays_out_of_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A section supplied at the wrong nesting level renders differently."""
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_GROQ", f'["{_API_KEY}"]')

        with pytest.raises(ValidationError) as raised:
            Settings.load()

        rendered = str(raised.value)
        assert "input_value" not in rendered
        assert _leaked_fragments(rendered, _API_KEY) == []

    def test_boolean_field_value_never_reaches_the_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A value in a boolean slot is not quoted back verbatim.

        ``_as_bool`` runs before the field is coerced, so its message text is the
        place a credential pasted into the wrong variable would be disclosed -
        ``hide_input_in_errors`` never covers message text.
        """
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_REDACT_LOGS", _API_KEY)

        with pytest.raises(ValidationError) as raised:
            Settings.load()

        rendered = str(raised.value) + repr(raised.value)
        assert "RFQ_REDACT_LOGS must be one of true/false/1/0/yes/no/on/off" in rendered
        assert _leaked_fragments(rendered, _API_KEY) == []

        # The same failure through the documented CLI entry point.
        completed = subprocess.run(
            [sys.executable, "-m", "rfq_agent.demo"],
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(_REPO_ROOT / "src"),
                "RFQ_REDACT_LOGS": _API_KEY,
            },
            cwd=tmp_path,
            check=False,
        )

        assert completed.returncode == 2
        assert "invalid configuration or stored data" in completed.stderr
        assert _leaked_fragments(completed.stderr, _API_KEY) == []

    def test_every_secret_stays_out_when_several_are_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rendered = str(
            self._production_error(
                monkeypatch,
                RFQ_GROQ__API_KEY=_API_KEY,
                RFQ_HTTP__OPERATOR_TOKEN=_OPERATOR_TOKEN,
                RFQ_DATABASE__URL=_DATABASE_URL,
                RFQ_GROQ__BASE_URL=f"https://user:{_DATABASE_PASSWORD}@proxy.internal/v1",
            )
        )

        assert "redact_logs must be true when env=production" in rendered
        assert "input_value" not in rendered
        for secret in (_API_KEY, _OPERATOR_TOKEN, _DATABASE_PASSWORD, _DATABASE_URL):
            assert _leaked_fragments(rendered, secret) == []

    def test_env_file_sourced_secrets_never_reach_the_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_rfq_env(monkeypatch)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "RFQ_ENV=production\n"
            "RFQ_REDACT_LOGS=false\n"
            f"RFQ_GROQ__API_KEY={_API_KEY}\n"
            f"RFQ_HTTP__OPERATOR_TOKEN={_OPERATOR_TOKEN}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValidationError) as raised:
            Settings.load(env_file=env_file)

        rendered = str(raised.value)
        assert "redact_logs must be true when env=production" in rendered
        assert "input_value" not in rendered
        for secret in (_API_KEY, _OPERATOR_TOKEN):
            assert _leaked_fragments(rendered, secret) == []

    def test_demo_cli_stderr_carries_no_secret(self, tmp_path: Path) -> None:
        """The demo command's configuration failure path must stay clean."""
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(_REPO_ROOT / "src"),
            "RFQ_ENV": "production",
            "RFQ_REDACT_LOGS": "false",
            "RFQ_GROQ__API_KEY": _API_KEY,
            "RFQ_HTTP__OPERATOR_TOKEN": _OPERATOR_TOKEN,
        }
        completed = subprocess.run(
            [sys.executable, "-m", "rfq_agent.demo"],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        )

        assert completed.returncode == 2
        assert "invalid configuration or stored data" in completed.stderr
        assert "input_value" not in completed.stderr
        for secret in (_API_KEY, _OPERATOR_TOKEN):
            assert _leaked_fragments(completed.stderr, secret) == []

    def test_alembic_configuration_failure_carries_no_secret(self, tmp_path: Path) -> None:
        """``alembic upgrade head`` loads settings too, and prints a traceback.

        Run from an empty directory against a copy of ``alembic.ini`` whose
        script location is absolute, so the command is isolated: it cannot read
        a developer ``.env``, and the database URL points at a file that must
        never be created because configuration loading fails first.
        """
        marker = "F3bAlembicMarker0123456789"
        database = tmp_path / f"{marker}.db"
        ini_text = (_REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")
        (tmp_path / "alembic.ini").write_text(
            ini_text.replace(
                "script_location = migrations", f"script_location = {_REPO_ROOT / 'migrations'}"
            ).replace("prepend_sys_path = . src", f"prepend_sys_path = {_REPO_ROOT / 'src'}"),
            encoding="utf-8",
        )
        env = {
            "PATH": os.environ["PATH"],
            "RFQ_ENV": "production",
            "RFQ_REDACT_LOGS": "false",
            "RFQ_GROQ__API_KEY": _API_KEY,
            "RFQ_HTTP__OPERATOR_TOKEN": _OPERATOR_TOKEN,
            "RFQ_DATABASE__URL": f"sqlite:///{database}",
        }
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(tmp_path / "alembic.ini"),
                "upgrade",
                "head",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        )

        combined = completed.stdout + completed.stderr
        assert completed.returncode != 0
        assert "redact_logs must be true when env=production" in combined
        assert "input_value" not in combined
        # ``database.url`` is the last field, so it is the value Pydantic's
        # render window ends on: the marker proves that tail is gone too.
        for secret in (_API_KEY, _OPERATOR_TOKEN, marker):
            assert _leaked_fragments(combined, secret) == []
        # Nothing destructive happened: configuration loading failed first.
        assert not database.exists()


class TestDisclosureFixKeepsDiagnostics:
    """The F3-B correction must not cost the operator anything they need."""

    @pytest.fixture(autouse=True)
    def _isolated_settings_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither the working directory nor the ambient environment decides.

        ``Settings`` reads ``.env`` from the working directory and ``RFQ_*`` from
        the process environment, so a developer machine with a production-ish
        ``.env`` or exported variables would otherwise fail these guards for a
        reason that has nothing to do with the behaviour under test.
        """
        monkeypatch.chdir(tmp_path)
        _clear_rfq_env(monkeypatch)

    def test_model_level_failure_still_identifies_the_rule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_ENV", "production")
        monkeypatch.setenv("RFQ_REDACT_LOGS", "false")
        with pytest.raises(ValidationError) as raised:
            Settings.load()

        (issue,) = raised.value.errors()
        assert issue["type"] == "value_error"
        assert issue["msg"] == "Value error, redact_logs must be true when env=production"

    def test_field_level_failure_still_names_the_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_LOG_LEVEL", "LOUD")
        with pytest.raises(ValidationError) as raised:
            Settings.load()

        (issue,) = raised.value.errors()
        assert issue["loc"] == ("log_level",)
        assert issue["type"] == "string_pattern_mismatch"
        assert "LOUD" not in str(raised.value)

    def test_input_hiding_is_configured(self) -> None:
        """The control itself is pinned, so it cannot be dropped silently."""
        assert Settings.model_config.get("hide_input_in_errors") is True

    def test_valid_development_configuration_still_loads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_rfq_env(monkeypatch)
        settings = Settings.load()

        assert settings.env is Environment.DEVELOPMENT
        assert settings.redact_logs is True
        assert settings.groq.has_credentials is False

    def test_valid_production_configuration_still_loads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_rfq_env(monkeypatch)
        monkeypatch.setenv("RFQ_ENV", "production")
        monkeypatch.setenv("RFQ_REDACT_LOGS", "true")
        monkeypatch.setenv("RFQ_GROQ__API_KEY", _API_KEY)
        settings = Settings.load()

        assert settings.is_production is True
        assert settings.groq.has_credentials is True
        assert settings.groq.require_credentials() == _API_KEY

    def test_serialisation_stays_safe(self) -> None:
        settings = Settings(groq=GroqSettings(api_key=_API_KEY))

        dumped = str(settings.model_dump())
        assert _leaked_fragments(dumped, _API_KEY) == []
        assert _leaked_fragments(settings.model_dump_json(), _API_KEY) == []
        # The value is still reachable in-process, for the provider adapter only.
        assert settings.groq.require_credentials() == _API_KEY
