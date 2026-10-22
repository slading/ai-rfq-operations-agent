"""Regression tests for the provider transport's redirect and deadline behaviour.

Two findings from independent QA are pinned here, both offline:

* **redirects are never followed, and their body is never read.** A 3xx is
  classified from the status line and headers alone: the response is closed, its
  body is left unread and ``Location`` is not consulted, so no credential can be
  replayed at a host the provider names.
* **the timeout must be a wall-clock deadline covering the whole attempt.** A
  per-socket timeout bounds one ``recv``, not the attempt: a response that
  trickles its status line, headers, chunk framing or trailers one byte at a time
  runs far past the budget. Every blocked socket operation re-arms the remaining
  budget, so the status line, headers, chunk framing, body and trailers are all
  covered - see :class:`_SlowPhase`.

The socket tests here bind ``127.0.0.1`` only: no DNS, no egress, no provider.
The deadline is additionally tested against a fake clock, so the core regression
is deterministic and its timing does not depend on the machine.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
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
from rfq_agent.providers.groq import _complete_handshake, _http_transport, _now, _read_bounded

if TYPE_CHECKING:
    from collections.abc import Iterator

_API_KEY = "gsk_test_key_do_not_leak_0000000000"
_MODEL = "openai/gpt-oss-120b"
_LOCATION = "https://collector.invalid/steal"
_CHAT_PATH = "/openai/v1/chat/completions"

#: A budget so small that a stalled phase is obvious and the test stays quick.
_BUDGET_SECONDS = 0.15
#: How long a server keeps trickling (and then holds its socket open).
_STALL_SECONDS = 3.0
#: Slack allowed above the budget for scheduling noise on a loaded machine.
_SLACK_SECONDS = 1.0


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
    """Promise 100 bytes, send five and hang up cleanly.

    The request body is drained first. It was not, and that was the intermittent
    failure this fixture used to cause: closing a socket with unread data in its
    receive buffer makes some kernels send RST where others send FIN, so the same
    test sometimes saw ``IncompleteRead`` and sometimes ``ConnectionResetError``.
    Both are legitimate client-visible outcomes of a truncated response; the
    ``FIN`` case is pinned here deterministically, and the abortive case is
    pinned separately in :class:`_Aborted`.
    """

    def handle_request(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b'{"a":')
        self.wfile.flush()
        self.close_connection = True


class _Aborted(_QuietHandler):
    """Promise 100 bytes, send five, then reset the connection instead of closing it."""

    def handle_request(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b'{"a":')
        self.wfile.flush()
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.connection.close()
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


class _RecordingTransport:
    """Record every attempt, delegating to a real transport at the seam.

    The provider refuses a non-HTTPS base URL - correctly, a credential must not
    travel in the clear - so the loopback rewrite happens here. Everything below
    the seam is the production transport.
    """

    def __init__(self, delegate: Callable[[HttpRequest], HttpResponse], *, origin: str) -> None:
        self._delegate = delegate
        self._origin = origin
        self.urls: list[str] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.urls.append(request.url)
        return self._delegate(replace(request, url=f"{self._origin}{_CHAT_PATH}"))


# ---------------------------------------------------------------------------
# P1 — redirects are never followed and their body is never consumed
# ---------------------------------------------------------------------------


class _StalledRedirect(_QuietHandler):
    """Answer 302, then stall without ever finishing the body.

    302 responses routinely carry a body with a ``Content-Length``; a client that
    reads it before classifying the status blocks on the provider's schedule. The
    attempt must be decided from the status line and headers alone.
    """

    requests = 0

    def handle_request(self) -> None:
        type(self).requests += 1
        self.send_response(302)
        self.send_header("Location", "http://collector.invalid/steal")
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "4096")
        self.end_headers()
        self.wfile.write(b"partial")
        self.wfile.flush()
        time.sleep(_STALL_SECONDS)


def test_a_redirect_body_is_never_read_and_the_status_is_returned() -> None:
    """A stalled redirect body must not delay or alter the classification."""
    _StalledRedirect.requests = 0
    started = time.monotonic()

    with _serve(_StalledRedirect) as server:
        response = _http_transport(_http_request(_url(server), timeout_seconds=5.0))

    elapsed = time.monotonic() - started

    # The body would take _STALL_SECONDS to arrive; the status is available now.
    assert response.status == 302
    assert response.body == b""
    assert elapsed < _STALL_SECONDS / 2


def test_a_redirect_is_one_origin_request_however_many_attempts_are_configured() -> None:
    """A 3xx is final: two configured attempts must not mean two requests."""
    _StalledRedirect.requests = 0

    with _serve(_StalledRedirect) as server:
        transport = _RecordingTransport(_http_transport, origin=_url(server, ""))
        result, instance = _provider(transport, max_attempts=2).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.INVALID_REQUEST.value
    assert result.retryable is False
    assert len(transport.urls) == 1
    assert _StalledRedirect.requests == 1


def test_a_redirected_request_is_never_replayed_at_the_destination() -> None:
    """The credential must not reach the host a redirect names."""
    with _serve(_Capture, seen=[]) as destination, _serve(_Redirect) as origin:
        origin.location = _url(destination)  # type: ignore[attr-defined]

        response = _http_transport(_http_request(_url(origin)))

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


_CHUNKED_HEAD = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
)
#: One byte every _TRICKLE_SECONDS: progress that a per-operation timeout never sees.
_TRICKLE_SECONDS = 0.02


def _trickle(client: socket.socket, prefix: bytes) -> None:
    """Send ``prefix``, then one byte at a time past the budget.

    This is the shape a per-socket timeout cannot catch: every single read makes
    progress well inside the timeout, while the attempt as a whole runs on for as
    long as the peer chooses.
    """
    client.sendall(prefix)
    for _ in range(int(_STALL_SECONDS / _TRICKLE_SECONDS)):
        client.sendall(b"X")
        time.sleep(_TRICKLE_SECONDS)


def _slow_status_line(client: socket.socket) -> None:
    """Never end the status line."""
    _trickle(client, b"HTTP/1.1 200 OK")


def _slow_headers(client: socket.socket) -> None:
    """Never send the blank line that ends the headers."""
    _trickle(client, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Pad: ")


def _slow_chunk_framing(client: socket.socket) -> None:
    """Never end the chunk-size line."""
    _trickle(client, _CHUNKED_HEAD + b"0000000")


def _slow_body(client: socket.socket) -> None:
    """Send a declared body one byte at a time."""
    _trickle(client, b"HTTP/1.1 200 OK\r\nContent-Length: 400\r\n\r\n")


def _slow_trailers(client: socket.socket) -> None:
    """Finish the chunked body, then never end the trailer section."""
    _trickle(client, _CHUNKED_HEAD + b"5\r\nhello\r\n0\r\nX-Trailer: ")


@contextmanager
def _raw_server(write: Callable[[socket.socket], None]) -> Iterator[str]:
    """Serve one raw response on loopback, holding the socket open afterwards.

    ``http.server`` cannot emit a partial status line, header block, or chunk
    framing; a raw socket can. These are exactly the phases ``http.client``
    parses internally, which is where a per-operation socket timeout escaped the
    budget.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5.0)
    host, port = listener.getsockname()[:2]
    finished = threading.Event()

    def serve() -> None:
        try:
            client, _address = listener.accept()
        except OSError:
            return
        try:
            with client:
                client.settimeout(1.0)
                try:
                    client.recv(65_536)
                except OSError:
                    return
                try:
                    write(client)
                except OSError:
                    return
                # Hold the connection open: an unbounded client would wait here.
                finished.wait(_STALL_SECONDS)
        finally:
            finished.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        finished.set()
        listener.close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "phase",
    [
        pytest.param(_slow_status_line, id="status-line"),
        pytest.param(_slow_headers, id="headers"),
        pytest.param(_slow_chunk_framing, id="chunk-framing"),
        pytest.param(_slow_body, id="body"),
        pytest.param(_slow_trailers, id="trailers"),
    ],
)
def test_every_phase_of_a_response_is_bounded_by_one_deadline(
    phase: Callable[[socket.socket], None],
) -> None:
    """Each phase ``http.client`` parses itself must observe the same budget.

    Before the socket proxy existed these phases were read inside library calls
    that no clock check could interrupt, and a per-operation socket timeout never
    fired because every byte arrived well inside it: the attempt ran for as long
    as the peer kept going.
    """
    with _raw_server(phase) as url:
        started = time.monotonic()

        with pytest.raises(TimeoutError):
            _http_transport(_http_request(url, timeout_seconds=_BUDGET_SECONDS))

        elapsed = time.monotonic() - started

    # The server holds for _STALL_SECONDS; the attempt must end near the budget.
    assert elapsed < _BUDGET_SECONDS + _SLACK_SECONDS
    assert elapsed < _STALL_SECONDS


def test_a_trickling_response_hits_the_deadline() -> None:
    """Real sockets, real clock: a trickle of a whole body is cut off."""
    with _serve(_Trickle) as server:
        started = time.monotonic()

        with pytest.raises(TimeoutError):
            _http_transport(_http_request(_url(server), timeout_seconds=0.25))

        elapsed = time.monotonic() - started

    # The server would trickle for ~8 s; the whole attempt must be nowhere near it.
    assert elapsed < 3.0


def test_a_timing_out_attempt_never_produces_a_success() -> None:
    """An expired attempt is a typed, retryable timeout - for every phase."""
    for phase in (_slow_status_line, _slow_headers, _slow_chunk_framing, _slow_trailers):
        with _raw_server(phase) as url:
            transport = _RecordingTransport(_http_transport, origin=url)
            result, instance = _provider(
                transport, max_attempts=2, timeout_seconds=_BUDGET_SECONDS
            ).parse(_request(), TinyPayload)

        assert instance is None, phase.__name__
        assert isinstance(result, ParseFailure), phase.__name__
        assert result.provider_error == ProviderErrorCode.TIMEOUT.value, phase.__name__
        assert result.retryable is True, phase.__name__
        # Bounded retries still apply: every attempt timed out, none succeeded.
        assert len(transport.urls) == 2, phase.__name__


def test_a_stalled_tls_handshake_is_bounded_by_the_deadline() -> None:
    """The handshake is part of the attempt, not a separate unbounded wait.

    A server that accepts the TCP connection and then says nothing leaves a
    blocking handshake waiting forever. Certificate verification is not reached
    here - the peer never sends a ``ServerHello`` - so no certificate is needed.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5.0)
    host, port = listener.getsockname()[:2]
    finished = threading.Event()
    held: list[socket.socket] = []

    def accept_and_stall() -> None:
        try:
            client, _address = listener.accept()
        except OSError:
            return
        with client:
            held.append(client)
            finished.wait(_STALL_SECONDS)

    thread = threading.Thread(target=accept_and_stall, daemon=True)
    thread.start()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), 5.0)
    started = time.monotonic()
    try:
        with (
            context.wrap_socket(
                raw, server_hostname="localhost", do_handshake_on_connect=False
            ) as tls,
            pytest.raises(TimeoutError),
        ):
            _complete_handshake(tls, _now() + _BUDGET_SECONDS)
    finally:
        elapsed = time.monotonic() - started
        raw.close()
        finished.set()
        listener.close()
        thread.join(timeout=5)

    assert elapsed < _BUDGET_SECONDS + _SLACK_SECONDS
    assert elapsed < _STALL_SECONDS


def test_a_truncated_http_response_raises_a_protocol_error() -> None:
    """A response shorter than its ``Content-Length`` is never returned as a body."""
    with _serve(_Truncated) as server, pytest.raises(http.client.IncompleteRead) as raised:
        _http_transport(_http_request(_url(server), timeout_seconds=2.0))

    assert raised.value.partial == b'{"a":'
    assert raised.value.expected == 95


def test_an_abortively_closed_response_is_a_typed_retryable_failure() -> None:
    """The other half of the same event: RST instead of FIN.

    A peer that resets mid-body is a transport failure, not a short answer, and it
    must not be mistaken for a truncated-but-acceptable body. Which of the two a
    truncated response produces depends on the peer's teardown, so both are pinned
    - and both end in the same typed, retryable classification.
    """
    with _serve(_Aborted) as server:
        transport = _RecordingTransport(_http_transport, origin=_url(server, ""))
        result, instance = _provider(transport, max_attempts=1).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.SERVER_ERROR.value
    assert result.retryable is True
    assert '{"a":' not in f"{result!r} {result.model_dump_json()}"


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


def _provider(
    transport: object, *, max_attempts: int = 1, timeout_seconds: float = 30.0
) -> GroqProvider:
    return GroqProvider(
        api_key=_API_KEY,
        model=_MODEL,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
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


def test_a_valid_reported_model_is_never_adopted() -> None:
    """The adapter reports the model it was configured with, not the vendor's.

    A provider can name any model it likes - a preview alias, a pinned revision,
    or something else entirely. That string is provider-controlled and is never
    the identifier the system records.
    """
    transport = _Scripted(_completion('{"a": "x"}', model="vendor/pinned-revision-7"))

    result, _instance = _provider(transport).parse(_request(), TinyPayload)

    assert isinstance(result, ParseSuccess)
    assert result.model == _MODEL

    for success in (result,):
        assert "vendor" not in success.model_dump_json()


def test_lone_surrogate_model_metadata_is_a_typed_sanitized_failure() -> None:
    """A surrogate cannot be serialised, and must not escape as an exception.

    ``ParseSuccess`` accepts the value in its ``str`` field but its JSON encoder
    (and every downstream writer) refuses it, so a raw provider string would
    turn a provider quirk into a crash after the call had already "succeeded".
    """
    transport = _Scripted(_completion('{"a": "x"}', model="\ud800"))

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error is None
    assert result.retryable is False
    assert [issue.error_type for issue in result.issues] == ["provider_metadata_invalid"]
    assert result.model == _MODEL
    renderable = result.model_dump_json()  # must not raise
    assert "ud800" not in renderable.casefold()


def test_metadata_that_looks_like_a_credential_never_surfaces_in_a_failure() -> None:
    """A provider string that reaches a failure is a leak with no upside."""
    leak = "gsk_live_do_not_leak_0000000000000000000000000000"
    transport = _Scripted(_completion('{"a": "x"}', model=leak))

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert isinstance(result, ParseSuccess), "valid-looking metadata is accepted"
    assert instance == TinyPayload(a="x")
    assert result.model == _MODEL
    assert leak not in result.model_dump_json()


def test_a_failure_never_carries_the_provider_identifier() -> None:
    """Schema binding fails: the failure records the configured model only."""
    leak = "gsk_live_do_not_leak_0000000000000000000000000000"
    transport = _Scripted(_completion('{"a": 17}', model=leak))

    result, instance = _provider(transport).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert [issue.error_type for issue in result.issues] == ["schema_invalid"]
    rendered = f"{result!r} {result.model_dump_json()}"
    assert leak not in rendered
    assert result.model == _MODEL
