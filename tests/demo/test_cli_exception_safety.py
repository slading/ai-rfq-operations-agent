"""CLI exception boundaries must not render exception payloads or tracebacks."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from rfq_agent.demo import __main__ as demo_cli
from rfq_agent.demo import runner as demo_runner
from rfq_agent.demo.fixtures import DemoOutcome
from rfq_agent.domain.resolution import CustomerMatchStatus
from rfq_agent.seed import __main__ as seed_cli
from rfq_agent.seed.loader import SeedError

_MARKER = "sensitive-marker-DO-NOT-PRINT"


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
def test_demo_unexpected_and_data_errors_are_sanitized(monkeypatch, capsys, error) -> None:
    """Unexpected and chained failures stay off both output streams."""

    def fail(_url):
        try:
            raise RuntimeError(_MARKER)
        except RuntimeError as cause:
            raise error("outer failure") from cause

    monkeypatch.setattr(demo_cli.Database, "create", fail)
    code = demo_cli.main([])
    captured = capsys.readouterr()

    assert code == (2 if error is ValueError else 1)
    assert captured.out == ""
    assert _MARKER not in captured.err
    assert "Traceback" not in captured.err


def test_demo_validation_error_does_not_render_input(monkeypatch, capsys) -> None:
    """Pydantic's formatted errors can include raw input values."""

    class Model(BaseModel):
        count: int

    with pytest.raises(ValidationError) as raised:
        Model(count=_MARKER)

    monkeypatch.setattr(
        demo_cli.Database, "create", lambda _url: (_ for _ in ()).throw(raised.value)
    )
    assert demo_cli.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _MARKER not in captured.err
    assert "Traceback" not in captured.err


def test_demo_unknown_scenario_does_not_echo_input(monkeypatch, capsys) -> None:
    """Scenario lookup diagnostics do not repeat the supplied identifier."""
    monkeypatch.setattr(demo_cli, "SCENARIOS", ())
    assert demo_cli.main(["--scenario", _MARKER]) == 2
    captured = capsys.readouterr()
    assert _MARKER not in captured.out + captured.err
    assert "use `--list`" in captured.err


@pytest.mark.parametrize("kind", ["value", "validation"])
def test_runner_stopped_reports_do_not_render_exception_data(monkeypatch, kind) -> None:
    """Adapter ValueError and ValidationError are converted to safe stop text."""

    class Model(BaseModel):
        count: int

    if kind == "validation":
        with pytest.raises(ValidationError) as raised:
            Model(count=_MARKER)
        failure = raised.value
    else:
        try:
            raise RuntimeError(_MARKER)
        except RuntimeError as cause:
            failure = ValueError("adapter failure")
            failure.__cause__ = cause

    customer = SimpleNamespace(match_status=CustomerMatchStatus.EXACT)
    scenario = SimpleNamespace(
        scenario_id="SC-TEST",
        expected=DemoOutcome.HUMAN_REQUIRED_BEFORE_QUOTE,
        lines=(),
        line_item_ids=(),
        requested_delivery=None,
        run_id="RUN-SAFE",
        quote_id="QUOTE-SAFE",
        quote_number="NUMBER-SAFE",
        pricing_as_of=None,
        stock_as_of=None,
        delivery_as_of=None,
    )
    monkeypatch.setattr(
        demo_runner, "provision_run", lambda *_args: SimpleNamespace(sender_email="x")
    )
    monkeypatch.setattr(demo_runner.BusinessReader, "for_session", lambda _session: object())
    monkeypatch.setattr(
        demo_runner, "bind_customer", lambda *_args, **_kwargs: (customer, object())
    )
    monkeypatch.setattr(
        demo_runner, "_customer_for_quote", lambda *_args: (customer, "safe customer")
    )
    monkeypatch.setattr(demo_runner, "resolve_lines", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        demo_runner, "to_quote_request", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure)
    )

    report = demo_runner.run_scenario(
        scenario,
        SimpleNamespace(
            session=lambda: nullcontext(object()), session_factory=lambda: nullcontext(object())
        ),
    )
    assert "review" in report.stop_reason
    assert _MARKER not in report.stop_reason


def test_seed_unexpected_failure_is_sanitized(monkeypatch, capsys) -> None:
    """Seed startup failures return a distinct non-zero status without details."""
    monkeypatch.setattr(
        seed_cli.Database, "create", lambda _url: (_ for _ in ()).throw(RuntimeError(_MARKER))
    )
    assert seed_cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _MARKER not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("operation_fails", [False, True])
def test_seed_disposal_failure_is_nonzero_without_masking_operation(
    monkeypatch, capsys, operation_fails
) -> None:
    """Cleanup runs after success and handled operation failure, safely."""

    class FakeDatabase:
        disposed = False

        def session(self):
            return nullcontext(object())

        def dispose(self):
            self.disposed = True
            try:
                raise RuntimeError(_MARKER)
            except RuntimeError as cause:
                raise RuntimeError("outer cleanup") from cause

    database = FakeDatabase()
    monkeypatch.setattr(seed_cli.Database, "create", lambda _url: database)
    if operation_fails:
        monkeypatch.setattr(
            seed_cli, "seed", lambda _session: (_ for _ in ()).throw(SeedError(_MARKER))
        )
    else:
        monkeypatch.setattr(
            seed_cli, "seed", lambda _session: SimpleNamespace(format=lambda: "done")
        )

    assert seed_cli.main([]) == (2 if operation_fails else 1)
    captured = capsys.readouterr()
    assert database.disposed
    assert _MARKER not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_seed_report_format_failure_is_sanitized(monkeypatch, capsys) -> None:
    """The final report renderer remains inside the failure boundary."""

    class FakeDatabase:
        def session(self):
            return nullcontext(object())

        def dispose(self):
            pass

    def render():
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(seed_cli.Database, "create", lambda _url: FakeDatabase())
    monkeypatch.setattr(seed_cli, "seed", lambda _session: SimpleNamespace(format=render))
    assert seed_cli.main([]) == 1
    captured = capsys.readouterr()
    assert _MARKER not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_cli_parsers_do_not_echo_unknown_arguments(capsys) -> None:
    """Both parsers retain argparse status 2 without echoing argv values."""
    for cli in (demo_cli, seed_cli):
        with pytest.raises(SystemExit) as raised:
            cli.main([f"--unknown-{_MARKER}"])
        assert raised.value.code == 2
        captured = capsys.readouterr()
        assert _MARKER not in captured.out + captured.err
        assert "Traceback" not in captured.err


def test_help_remains_available_for_both_commands(capsys) -> None:
    """The safe parser keeps standard help successful and descriptive."""
    for cli in (demo_cli, seed_cli):
        with pytest.raises(SystemExit) as raised:
            cli.main(["--help"])
        assert raised.value.code == 0
        assert "usage:" in capsys.readouterr().out


def test_demo_success_does_not_print_database_query_credentials(monkeypatch, capsys) -> None:
    """A successful run reports a static backend label, never the configured URL."""

    class FakeDatabase:
        url = SimpleNamespace(render_as_string=lambda **_kwargs: _MARKER)

        def session(self):
            return nullcontext(object())

        def dispose(self):
            pass

    scenario = SimpleNamespace(scenario_id="SC-TEST", expected=SimpleNamespace(value="OK"))
    report = SimpleNamespace(outcome=SimpleNamespace(value="OK"))
    monkeypatch.setattr(demo_cli, "SCENARIOS", (scenario,))
    monkeypatch.setattr(demo_cli.Database, "create", lambda _url: FakeDatabase())
    monkeypatch.setattr(demo_cli, "require_prerequisites", lambda _session: None)
    monkeypatch.setattr(demo_cli, "run_scenario", lambda _scenario, _database: report)
    monkeypatch.setattr(demo_cli, "_format_report", lambda _report: "safe report")
    assert demo_cli.main(["--url", f"sqlite:///safe.db?access_token={_MARKER}"]) == 0
    captured = capsys.readouterr()
    assert _MARKER not in captured.out + captured.err
    assert "database: configured database" in captured.out


def test_demo_list_success_contract_remains_intact(monkeypatch, capsys) -> None:
    """The list-only success path still prints scenarios and returns zero."""
    monkeypatch.setattr(
        demo_cli,
        "SCENARIOS",
        (
            SimpleNamespace(
                scenario_id="SC-TEST", expected=SimpleNamespace(value="OK"), title="safe"
            ),
        ),
    )
    assert demo_cli.main(["--list"]) == 0
    captured = capsys.readouterr()
    assert "SC-TEST  OK" in captured.out
    assert captured.err == ""
