"""Regression tests for the provider transport's redirect and deadline behaviour.

Two findings from independent QA are pinned here, both offline:

* **redirects must never be followed.** ``urllib`` replays a redirected request
  with the original headers minus only the content headers, so an
  ``Authorization`` header would travel to whatever host the response named, and
  an ``https`` URL would be allowed to become ``http``. The handler refuses every
  3xx, and a 3xx status is a non-retryable typed failure.
* **the timeout must be a wall-clock deadline.** A per-socket timeout lets a
  response that trickles one byte at a time run far past the intended budget. The
  reader re-checks a monotonic deadline before every chunk and re-arms the
  socket's timeout with the remaining budget.

The socket tests here bind ``127.0.0.1`` only: no DNS, no egress, no provider.
The deadline itself is additionally tested against a fake clock, so the core
regression is deterministic and its timing does not depend on the machine.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.request
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from rfq_agent.contracts.errors import ProviderErrorCode
from rfq_agent.contracts.llm import (
    ChatMessage,
    ChatRole,
    ModelPurpose,
    ParseFailure,
    ParseRequest,
    ParseSuccess,
)
from rfq_agent.providers import GroqProvider, HttpRequest, HttpResponse
from rfq_agent.providers import groq as groq_module
from rfq_agent.providers.groq import _OPENER, _read_bounded, _RefuseRedirects, _urllib_transport

if TYPE_CHECKING:
    from collections.abc import Iterator

_API_KEY = "gsk_test_key_do_not_leak_0000000000"
_MODEL = "openai/gpt-oss-120b"
_LOCATION = "https://collector.invalid/steal"


# ---------------------------------------------------------------------------
# Offline HTTP helpers: loopback only
# ---------------------------------------------------------------------------


class _QuietHandler(BaseHTTPRequestHandler):
    """A handler that answers deterministically and writes nothing to stderr."""

    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
        """Silence the default access log."""

    def do_POST(self) -> None:
        """Answer the POST a provider request makes."""
        self.handle_request()

    def do_GET(self) -> None:
        """Answer a GET, which is what a *followed* redirect would become."""
        self.handle_request()

    def handle_request(self) -> None:
        """Subclasses override this."""
        raise NotImplementedError


class _Redirect(_QuietHandler):
    """Answer every request with a redirect to ``server.location``."""

    def handle_request(self) -> None:
        body = b"redirected"
        self.send_response(302)
        self.send_header("Location", self.server.location)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Capture(_QuietHandler):
    """Record every request it receives, including all headers."""

    def handle_request(self) -> None:
        self.server.seen.append(  # type: ignore[attr-defined]
            {"path": self.path, "headers": dict(self.headers)}
        )
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Trickle(_QuietHandler):
    """Send headers, then one byte at a time, far past any sane deadline."""

    total = 400
    interval = 0.02

    def handle_request(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(self.total))
        self.end_headers()
        for _ in range(self.total):
            try:
                self.wfile.write(b" ")
                self.wfile.flush()
            except OSError:
                return
            time.sleep(self.interval)


class _Truncated(_QuietHandler):
    """Promise 100 bytes, send five and hang up."""

    def handle_request(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b'{"a":')
        self.wfile.flush()
        self.close_connection = True


@contextmanager
def _serve(
    handler: type[BaseHTTPRequestHandler], **attributes: object
) -> Iterator[ThreadingHTTPServer]:
    """Serve ``handler`` on an ephemeral loopback port for one test."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    for name, value in attributes.items():
        setattr(server, name, value)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _url(server: ThreadingHTTPServer, path: str = "/v1/chat/completions") -> str:
    """The loopback URL of ``server``."""
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{path}"


def _http_request(url: str, *, timeout_seconds: float = 2.0) -> HttpRequest:
    """One request carrying the credential, as the adapter would build it."""
    return HttpRequest(
        url=url,
        headers=(
            ("Authorization", f"Bearer {_API_KEY}"),
            ("Content-Type", "application/json"),
        ),
        body=b'{"model": "x"}',
        timeout_seconds=timeout_seconds,
        max_response_bytes=8_192,
    )


# ---------------------------------------------------------------------------
# P1 — redirects are never followed
# ---------------------------------------------------------------------------


def test_the_default_urllib_handler_would_have_forwarded_the_credential() -> None:
    """Negative control: the leak this regression exists to prevent is real.

    Stdlib behaviour, asserted so the fix cannot be mistaken for belt-and-braces:
    the default handler builds a follow-up request for another host, downgrades
    ``https`` to ``http``, and carries ``Authorization`` onto it.
    """
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=b"{}",
        headers={"Authorization": f"Bearer {_API_KEY}"},
        method="POST",
    )

    followed = urllib.request.HTTPRedirectHandler().redirect_request(
        request, None, 302, "Found", {}, "http://collector.invalid/steal"
    )

    assert followed is not None
    assert followed.get_full_url() == "http://collector.invalid/steal"
    assert followed.get_header("Authorization") == f"Bearer {_API_KEY}"


@pytest.mark.parametrize(
    "location",
    [
        "https://collector.invalid/steal",  # cross-host
        "http://collector.invalid/steal",  # https -> http downgrade
        "https://api.groq.com/openai/v1/elsewhere",  # same host, still refused
    ],
)
def test_the_installed_handler_refuses_every_redirect(location: str) -> None:
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=b"{}",
        headers={"Authorization": f"Bearer {_API_KEY}"},
        method="POST",
    )

    assert _RefuseRedirects().redirect_request(request, None, 302, "Found", {}, location) is None


def test_the_shared_opener_has_no_redirect_handler_that_would_follow() -> None:
    handlers = [
        item for item in _OPENER.handlers if isinstance(item, urllib.request.HTTPRedirectHandler)
    ]

    assert handlers, "the refusing handler must be installed on the provider opener"
    assert all(isinstance(item, _RefuseRedirects) for item in handlers)


def test_a_redirected_request_is_never_replayed_at_the_destination() -> None:
    """The credential must not reach the host a redirect names."""
    with _serve(_Capture, seen=[]) as destination, _serve(_Redirect) as origin:
        origin.location = _url(destination)  # type: ignore[attr-defined]

        response = _urllib_transport(_http_request(_url(origin)))

        # A 302 is the answer. Had the redirect been followed, this would be the
        # destination's status and ``seen`` would hold the replayed request.
        assert response.status == 302
        assert destination.seen == []  # type: ignore[attr-defined]
        assert _API_KEY not in response.body.decode("utf-8", errors="replace")
        assert _API_KEY not in "".join(f"{k}:{v}" for k, v in response.headers.items())


def test_a_redirect_status_is_a_typed_non_retryable_failure() -> None:
    calls: list[HttpRequest] = []

    def transport(request: HttpRequest) -> HttpResponse:
        calls.append(request)
        return HttpResponse(
            status=302,
            body=b"",
            headers={"Location": _LOCATION, "Retry-After": "5"},
        )

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.INVALID_REQUEST.value
    assert result.retryable is False
    assert len(calls) == 1  # never retried, whatever the Retry-After says


def test_a_redirect_failure_echoes_neither_the_location_nor_the_credential() -> None:
    def transport(request: HttpRequest) -> HttpResponse:
        del request
        return HttpResponse(status=301, body=b"", headers={"Location": _LOCATION})

    result, _ = _provider(transport).parse(_request(), TinyPayload)

    rendered = f"{result!r} {result.model_dump_json()}"
    assert "collector.invalid" not in rendered
    assert _API_KEY not in rendered
    assert "https://" not in rendered


# ---------------------------------------------------------------------------
# P2 — one wall-clock deadline per attempt
# ---------------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock that only moves when the reader performs a read."""

    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _SlowBody:
    """A body source that costs ``per_read`` seconds per chunk."""

    def __init__(
        self,
        clock: _FakeClock,
        *,
        per_read: float = 0.05,
        total: int | None = None,
        byte: bytes = b"x",
    ) -> None:
        self.clock = clock
        self.per_read = per_read
        self.total = total
        self.byte = byte
        self.reads = 0

    def read1(self, amount: int) -> bytes:
        del amount
        self.clock.advance(self.per_read)
        self.reads += 1
        if self.total is not None and self.reads > self.total:
            return b""
        return self.byte


def test_the_body_reader_stops_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trickling body cannot outlive the budget, and the stop is deterministic."""
    clock = _FakeClock()
    monkeypatch.setattr(groq_module, "_now", clock.now)
    body = _SlowBody(clock, per_read=0.05)

    with pytest.raises(TimeoutError):
        _read_bounded(body, 1_024, deadline=clock.value + 0.2)

    # 0.20 s of budget at 0.05 s per read: four reads, then the next check fails.
    assert body.reads == 4
    assert clock.value == pytest.approx(0.2)


def test_the_body_reader_returns_a_complete_body(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(groq_module, "_now", clock.now)

    body = _read_bounded(_SlowBody(clock, per_read=0.0, total=5), 1_024, deadline=clock.value + 1)

    assert body == b"xxxxx"
    assert clock.value == 0.0


def test_the_body_reader_keeps_the_response_size_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline must not replace the byte ceiling: both bounds hold."""
    clock = _FakeClock()
    monkeypatch.setattr(groq_module, "_now", clock.now)

    body = _read_bounded(_SlowBody(clock, per_read=0.0), 1_024, deadline=clock.value + 5)

    assert len(body) == 1_025  # one byte past the ceiling, never the whole stream


class _TruncatedBody:
    """A body source whose declared length is never satisfied."""

    length = 95

    def __init__(self) -> None:
        self._chunks = [b'{"a":']

    def read1(self, amount: int) -> bytes:
        del amount
        return self._chunks.pop(0) if self._chunks else b""


def test_the_body_reader_reports_a_body_shorter_than_its_content_length() -> None:
    with pytest.raises(http.client.IncompleteRead) as raised:
        _read_bounded(_TruncatedBody(), 1_024, deadline=time.monotonic() + 5)

    assert raised.value.partial == b'{"a":'
    assert raised.value.expected == 95


def test_a_trickling_response_hits_the_deadline() -> None:
    """Real sockets, real clock: the trickle is cut off near the budget."""
    with _serve(_Trickle) as server:
        started = time.monotonic()

        with pytest.raises(TimeoutError):
            _urllib_transport(_http_request(_url(server), timeout_seconds=0.25))

        elapsed = time.monotonic() - started

    # The server would trickle for ~8 s; the whole attempt must be nowhere near it.
    assert elapsed < 3.0


def test_a_truncated_http_response_raises_a_protocol_error() -> None:
    with _serve(_Truncated) as server, pytest.raises(http.client.IncompleteRead):
        _urllib_transport(_http_request(_url(server), timeout_seconds=2.0))


def test_an_incomplete_read_from_a_transport_is_a_typed_retryable_failure() -> None:
    def transport(request: HttpRequest) -> HttpResponse:
        del request
        raise http.client.IncompleteRead(b'{"a":', 95)

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.SERVER_ERROR.value
    assert result.retryable is True
    assert "IncompleteRead" not in f"{result!r} {result.model_dump_json()}"
    assert '{"a":' not in f"{result!r} {result.model_dump_json()}"


def test_a_deadline_timeout_from_the_transport_is_a_typed_timeout() -> None:
    def transport(request: HttpRequest) -> HttpResponse:
        del request
        raise TimeoutError("the provider request exceeded its wall-clock deadline")

    result, instance = _provider(transport, max_attempts=2).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.TIMEOUT.value
    assert result.retryable is True


# ---------------------------------------------------------------------------
# P2 — malformed successful responses become typed failures, not exceptions
# ---------------------------------------------------------------------------


class TinyPayload(BaseModel):
    """Minimal closed schema: binding succeeds unless the metadata is refused."""

    a: str


class _Scripted:
    """A transport that replays ``response`` once and counts calls."""

    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = 0

    def __call__(self, request: HttpRequest) -> HttpResponse:
        del request
        self.calls += 1
        return self.response


def _request() -> ParseRequest:
    return ParseRequest(
        messages=(
            ChatMessage(role=ChatRole.SYSTEM, content="system rules"),
            ChatMessage(role=ChatRole.USER, content="please quote 25 pcs of 100-ABC"),
        ),
        schema_name="extraction_claims",
        json_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        model=_MODEL,
        purpose=ModelPurpose.EXTRACT,
        temperature=Decimal("0"),
    )


def _provider(transport: object, *, max_attempts: int = 1) -> GroqProvider:
    return GroqProvider(
        api_key=_API_KEY,
        model=_MODEL,
        max_attempts=max_attempts,
        transport=transport,  # type: ignore[arg-type]
        sleep=lambda _delay: None,
    )


def _completion(
    content: str, *, model: object = _MODEL, include_model: bool = True
) -> HttpResponse:
    """A 200 chat-completion response whose ``model`` field is under test."""
    envelope: dict[str, object] = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    if include_model:
        envelope["model"] = model
    return HttpResponse(status=200, body=json.dumps(envelope).encode(), headers={})


def test_oversized_model_metadata_is_a_typed_sanitized_failure() -> None:
    oversized = "m" * 5_000
    transport = _Scripted(_completion('{"a": "x"}', model=oversized))

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error is None
    assert [issue.error_type for issue in result.issues] == ["provider_metadata_invalid"]
    assert result.retryable is False
    rendered = f"{result!r} {result.model_dump_json()}"
    assert oversized not in rendered
    assert "mmmmmmmmmm" not in rendered


def test_non_string_model_metadata_is_a_typed_sanitized_failure() -> None:
    transport = _Scripted(_completion('{"a": "x"}', model={"vendor": "x"}))

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert [issue.error_type for issue in result.issues] == ["provider_metadata_invalid"]
    assert "vendor" not in result.model_dump_json()


def test_blank_model_metadata_is_a_typed_sanitized_failure() -> None:
    transport = _Scripted(_completion('{"a": "x"}', model="   "))

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert [issue.error_type for issue in result.issues] == ["provider_metadata_invalid"]


def test_absent_model_metadata_falls_back_to_the_configured_model() -> None:
    transport = _Scripted(_completion('{"a": "x"}', include_model=False))

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert isinstance(result, ParseSuccess)
    assert instance == TinyPayload(a="x")
    assert result.model == _MODEL


def test_reported_model_metadata_is_used_when_it_is_valid() -> None:
    transport = _Scripted(_completion('{"a": "x"}', model=f"  {_MODEL}  "))

    result, _instance = _provider(transport).parse(_request(), TinyPayload)

    assert isinstance(result, ParseSuccess)
    assert result.model == _MODEL
