"""End-to-end HTTPS tests for the provider transport, against a trusted local server.

These tests exist because the transport once read a real HTTPS response as raw
TLS record bytes: ``http.client`` parses the status line through the buffered
reader the socket hands it, and a reader built over a *second plain socket* over
the same descriptor reads ciphertext. Every test here therefore goes through the
real production transport - :func:`rfq_agent.providers.groq._http_transport`,
with no mocked transport at the seam - against a loopback TLS server.

The certificate is generated per test session into a temporary directory: it is
issued for ``localhost`` only, it is short-lived, and only the test client is made
to trust it. Nothing durable is stored in the repository, so no key material can
be committed. ``openssl`` is the only external tool needed, and it is present on
the CI runner; without it these tests skip with a reason rather than pretending
to pass.

The socket-bound tests bind ``127.0.0.1``: no DNS, no egress, no provider.
"""

from __future__ import annotations

import gc
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
from rfq_agent.providers import GroqProvider
from rfq_agent.providers import groq as groq_module
from rfq_agent.providers.groq import _connect, _http_transport, _now

_API_KEY = "gsk_test_key_do_not_leak_0000000000"
_MODEL = "openai/gpt-oss-120b"
_CHAT_PATH = "/openai/v1/chat/completions"

#: Small enough that a stalled phase is obvious, large enough to survive a busy host.
_BUDGET_SECONDS = 0.15
#: How long a fixture keeps its socket open after the phase it is stalling in.
_STALL_SECONDS = 3.0
#: Slack allowed above the budget for scheduling noise.
_SLACK_SECONDS = 1.0
#: One byte every _TRICKLE_SECONDS: progress a per-operation timeout never sees.
_TRICKLE_SECONDS = 0.02


class TinyPayload(BaseModel):
    """Minimal closed schema: binding succeeds unless the metadata is refused."""

    a: str


@dataclass(frozen=True, slots=True)
class _Material:
    """The loopback server certificate and its key."""

    cert: Path
    key: Path


@pytest.fixture(scope="session")
def tls_material(tmp_path_factory: pytest.TempPathFactory) -> _Material:
    """Generate a short-lived certificate for ``localhost``, trusted by nobody yet."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required to generate the loopback TLS fixture")
    directory = tmp_path_factory.mktemp("tls")
    cert = directory / "localhost.pem"
    key = directory / "localhost.key"
    subprocess.run(  # noqa: S603 - fixed argv, local tool, paths under tmp_path
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    return _Material(cert=cert, key=key)


@pytest.fixture
def trusting_context(tls_material: _Material, monkeypatch: pytest.MonkeyPatch) -> ssl.SSLContext:
    """The client context the transport uses, trusting only the fixture CA.

    The seam is a module-level factory so the suite can add one trust anchor for a
    loopback fixture without touching process-wide SSL state. It keeps the
    hardened defaults, which the first test below asserts.
    """
    context = ssl.create_default_context(cafile=str(tls_material.cert))
    monkeypatch.setattr(groq_module, "_ssl_context", lambda: context)
    return context


def _request() -> ParseRequest:
    """A structurally valid extraction request, as the pipeline would build it."""
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


def _completion_response() -> bytes:
    """A well-formed chat-completion body whose content satisfies ``TinyPayload``."""
    return json.dumps(
        {
            "model": _MODEL,
            "choices": [{"message": {"role": "assistant", "content": '{"a": "x"}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()


def _provider(base_url: str) -> GroqProvider:
    """A provider pointed at ``base_url``, using the real transport."""
    return GroqProvider(
        api_key=_API_KEY,
        model=_MODEL,
        base_url=base_url,
        timeout_seconds=5.0,
        max_attempts=1,
        sleep=lambda _delay: None,
    )


class _QuietHandler(BaseHTTPRequestHandler):
    """Answer the POST the provider sends, recording it and the TLS version."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
        """Silence the default access log."""

    def do_POST(self) -> None:
        """Record the request and answer with a chat completion."""
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.seen.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "headers": dict(self.headers),
                "tls": self.connection.version(),
            }
        )
        body = _completion_response()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _QuietServer(ThreadingHTTPServer):
    """A server that swallows client-abort tracebacks instead of printing them."""

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        """An aborted TLS handshake is the client's answer, not a server fault."""
        del request, client_address


@contextmanager
def _serve_tls(
    handler: type[BaseHTTPRequestHandler],
    material: _Material,
    *,
    seen: list[dict[str, object]],
) -> Iterator[ThreadingHTTPServer]:
    """Serve ``handler`` over TLS on an ephemeral loopback port for one test."""
    server = _QuietServer(("127.0.0.1", 0), handler)
    server.seen = seen  # type: ignore[attr-defined]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(material.cert), str(material.key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
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


def _https_base(server: ThreadingHTTPServer, host: str = "localhost") -> str:
    """The provider base URL of ``server``, by default under the certified name."""
    port = server.server_address[1]
    return f"https://{host}:{port}/openai/v1"


# ---------------------------------------------------------------------------
# P1 - an HTTPS response is decrypted, parsed, verified
# ---------------------------------------------------------------------------


def test_the_installed_context_is_the_hardened_default(tls_material: _Material) -> None:
    """The transport verifies by default, and the fixture only adds a trust anchor.

    This is the negative control for every other test in the module: a permissive
    context would make the certificate and hostname tests vacuous. It reads the
    *unpatched* factory, so it describes production rather than the fixture.
    """
    production = groq_module._ssl_context()
    assert production.verify_mode is ssl.CERT_REQUIRED
    assert production.check_hostname is True
    assert len(production.get_ca_certs()) > 1, "the system trust store is in use"

    fixture = ssl.create_default_context(cafile=str(tls_material.cert))
    assert fixture.verify_mode is ssl.CERT_REQUIRED
    assert fixture.check_hostname is True
    assert len(fixture.get_ca_certs()) == 1, "only the fixture CA is trusted"


@pytest.mark.usefixtures("trusting_context")
def test_a_trusted_https_response_parses_through_the_production_transport(
    tls_material: _Material,
) -> None:
    """P1: a real TLS response reaches the pipeline as plaintext JSON.

    Before the fix the body was read through a plain socket over the same
    descriptor, so the adapter parsed TLS record bytes and the call failed with
    ``BadStatusLine``. This asserts the success path end to end through
    ``GroqProvider.parse`` with the default transport.
    """
    seen: list[dict[str, object]] = []
    with _serve_tls(_QuietHandler, tls_material, seen=seen) as server:
        result, instance = _provider(_https_base(server)).parse(_request(), TinyPayload)

    assert isinstance(result, ParseSuccess)
    assert instance == TinyPayload(a="x")
    assert result.model == _MODEL
    assert len(seen) == 1
    # The server saw TLS ciphertext; the client saw plaintext.
    assert str(seen[0]["tls"]).startswith("TLSv1")
    assert seen[0]["path"] == _CHAT_PATH


@pytest.mark.usefixtures("trusting_context")
def test_the_transport_returns_decrypted_plaintext_not_tls_records(
    tls_material: _Material,
) -> None:
    """The transport's response body is JSON, not a TLS record stream.

    A regression here is recognisable by its first byte: a TLS application-data
    record starts ``0x17 0x03 0x03``.
    """
    seen: list[dict[str, object]] = []
    with _serve_tls(_QuietHandler, tls_material, seen=seen) as server:
        port = server.server_address[1]
        response = _http_transport(
            groq_module.HttpRequest(
                url=f"https://localhost:{port}{_CHAT_PATH}",
                headers=(("Authorization", f"Bearer {_API_KEY}"),),
                body=b"{}",
                timeout_seconds=5.0,
                max_response_bytes=8_192,
            )
        )

    assert response.status == 200
    assert response.body == _completion_response()
    assert not response.body.startswith(b"\x17\x03\x03")


@pytest.mark.usefixtures("trusting_context")
def test_the_response_reader_reads_through_the_tls_socket(tls_material: _Material) -> None:
    """P1, mechanically: the reader is bound to the TLS descriptor, not a copy of it.

    ``http.client`` parses the status line through whatever ``makefile`` returns.
    A second plain socket over ``os.dup`` of the descriptor has a *different*
    descriptor number and no access to the TLS session, which is precisely how the
    ciphertext reached the parser; the invariant below cannot hold in that design.
    """
    seen: list[dict[str, object]] = []
    with _serve_tls(_QuietHandler, tls_material, seen=seen) as server:
        port = server.server_address[1]
        connection = _connect(
            groq_module.urlsplit(f"https://localhost:{port}{_CHAT_PATH}"), _now() + 5.0
        )
        try:
            connection.connect()
            tls_socket = connection.sock._sock
            assert isinstance(tls_socket, ssl.SSLSocket)

            connection.request(
                "POST",
                _CHAT_PATH,
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            try:
                assert response.fp is not None
                assert response.fp.fileno() == tls_socket.fileno()
                assert response.read() == _completion_response()
            finally:
                response.close()
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# TLS verification: untrusted certificate, wrong hostname
# ---------------------------------------------------------------------------


def test_an_untrusted_certificate_is_refused_before_the_request_is_sent(
    tls_material: _Material,
) -> None:
    """No ``_ssl_context`` patch: the system trust store does not know the fixture.

    The handshake fails, so the request - and the credential in it - is never
    written. TLS rejecting the endpoint is deterministic, so it is not retryable.
    """
    seen: list[dict[str, object]] = []
    with _serve_tls(_QuietHandler, tls_material, seen=seen) as server:
        result, instance = _provider(_https_base(server)).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.INVALID_REQUEST.value
    assert result.retryable is False
    assert seen == []


def test_a_certificate_issued_for_another_host_is_refused(
    tls_material: _Material, trusting_context: ssl.SSLContext
) -> None:
    """Trusting the CA is not enough: the name in the certificate must match.

    The fixture certificate is issued for ``localhost``; connecting by IP address
    must fail hostname verification, and the request must never be sent.
    """
    del trusting_context
    seen: list[dict[str, object]] = []
    with _serve_tls(_QuietHandler, tls_material, seen=seen) as server:
        result, instance = _provider(_https_base(server, host="127.0.0.1")).parse(
            _request(), TinyPayload
        )

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.INVALID_REQUEST.value
    assert result.retryable is False
    assert seen == []


def test_a_certificate_failure_names_neither_the_host_nor_the_credential(
    tls_material: _Material,
) -> None:
    """A verification failure is reported in the closed vocabulary, not verbatim."""
    seen: list[dict[str, object]] = []
    with _serve_tls(_QuietHandler, tls_material, seen=seen) as server:
        result, _instance = _provider(_https_base(server)).parse(_request(), TinyPayload)

    rendered = f"{result!r} {result.model_dump_json()}"
    assert _API_KEY not in rendered
    assert "CERTIFICATE_VERIFY_FAILED" not in rendered
    assert "localhost" not in rendered
    assert "self-signed" not in rendered


# ---------------------------------------------------------------------------
# P2 - TLS resource cleanup
# ---------------------------------------------------------------------------


class _SilentTCP:
    """Accept a TCP connection and never speak TLS.

    ``hold_seconds`` is how long the accepted socket is kept open. The cleanup
    tests set it to zero: they measure the *client's* descriptor count in a process
    that also hosts the server, so a held connection would count against the client.
    """

    def __init__(self, *, hold_seconds: float = 0.0) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(5.0)
        self.host, self.port = self._listener.getsockname()[:2]
        self._hold_seconds = hold_seconds
        self._done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._done.is_set():
            try:
                client, _address = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._hold, args=(client,), daemon=True).start()

    def _hold(self, client: socket.socket) -> None:
        with client:
            self._done.wait(self._hold_seconds)

    def close(self) -> None:
        self._done.set()
        self._listener.close()


def test_a_stalled_tls_handshake_is_a_typed_retryable_timeout() -> None:
    """A peer that accepts the connection and says nothing cannot outlast the budget.

    This is the real handshake, not a stubbed one: no certificate is involved
    because the peer never sends a ``ServerHello``, so the attempt ends at the
    deadline and is reported in the closed vocabulary rather than hanging.
    """
    server = _SilentTCP(hold_seconds=_STALL_SECONDS)
    try:
        provider = GroqProvider(
            api_key=_API_KEY,
            model=_MODEL,
            base_url=f"https://localhost:{server.port}/openai/v1",
            timeout_seconds=_BUDGET_SECONDS,
            max_attempts=2,
            base_delay_seconds=0.01,
            max_delay_seconds=0.02,
            sleep=lambda _delay: None,
        )
        started = time.monotonic()
        result, instance = provider.parse(_request(), TinyPayload)
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.TIMEOUT.value
    assert result.retryable is True
    # Two bounded attempts, not one unbounded wait.
    assert elapsed < 2 * _BUDGET_SECONDS + _SLACK_SECONDS + 0.5
    assert elapsed < _STALL_SECONDS


def test_a_failed_handshake_closes_the_tls_socket_without_waiting_for_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: the object that owns the descriptor is closed before the error propagates.

    ``wrap_socket`` detaches the raw socket, so the TLS object owns the descriptor
    from then on. Closing the detached raw socket instead leaves the live
    ``SSLSocket`` - and its descriptor - to whatever references the traceback,
    which is exactly the leak this pins: it asserts the descriptor is gone while
    the exception is still in hand.
    """
    captured: list[ssl.SSLSocket] = []

    def expire(sock: ssl.SSLSocket, deadline: float) -> None:
        del deadline
        captured.append(sock)
        msg = "the provider TLS handshake exceeded the request deadline"
        raise TimeoutError(msg)

    monkeypatch.setattr(groq_module, "_complete_handshake", expire)
    server = _SilentTCP()
    try:
        connection = _connect(
            groq_module.urlsplit(f"https://localhost:{server.port}/openai/v1"),
            _now() + 5.0,
        )
        with pytest.raises(TimeoutError):
            connection.connect()
    finally:
        server.close()

    (tls_socket,) = captured
    assert isinstance(tls_socket, ssl.SSLSocket)
    assert tls_socket.fileno() == -1, "the TLS socket must be closed, not left to GC"
    assert connection.sock is None, "a half-open socket must not be recorded"


def test_repeated_handshake_timeouts_do_not_accumulate_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: repeated TLS timeouts release every descriptor they open."""
    proc_fd = Path("/proc/self/fd")
    if not proc_fd.is_dir():
        pytest.skip("descriptor accounting needs /proc/self/fd")

    def expire(sock: ssl.SSLSocket, deadline: float) -> None:
        del sock, deadline
        msg = "the provider TLS handshake exceeded the request deadline"
        raise TimeoutError(msg)

    monkeypatch.setattr(groq_module, "_complete_handshake", expire)
    server = _SilentTCP()
    try:
        url = groq_module.urlsplit(f"https://localhost:{server.port}/openai/v1")
        # One attempt first, so the listener and the fixture's own descriptors are
        # already counted; what the loop must not do is grow the count.
        attempt = _connect(url, _now() + 5.0)
        with pytest.raises(TimeoutError):
            attempt.connect()
        gc.collect()
        baseline = len(list(proc_fd.iterdir()))

        for _ in range(25):
            failed = _connect(url, _now() + 5.0)
            with pytest.raises(TimeoutError):
                failed.connect()
        gc.collect()

        assert len(list(proc_fd.iterdir())) - baseline <= 2
    finally:
        server.close()


def test_repeated_https_requests_release_every_descriptor(
    tls_material: _Material, trusting_context: ssl.SSLContext
) -> None:
    """A successful HTTPS exchange must not leave the reader or the socket behind."""
    del trusting_context
    proc_fd = Path("/proc/self/fd")
    if not proc_fd.is_dir():
        pytest.skip("descriptor accounting needs /proc/self/fd")

    with _serve_tls(_QuietHandler, tls_material, seen=[]) as server:
        provider = _provider(_https_base(server))
        provider.parse(_request(), TinyPayload)
        gc.collect()
        baseline = len(list(proc_fd.iterdir()))

        for _ in range(25):
            assert isinstance(provider.parse(_request(), TinyPayload)[0], ParseSuccess)
        gc.collect()

        assert len(list(proc_fd.iterdir())) - baseline <= 2


# ---------------------------------------------------------------------------
# Deadline through TLS, and the redirect/metadata protections under TLS
# ---------------------------------------------------------------------------


def _trickle(client: ssl.SSLSocket, prefix: bytes) -> None:
    """Send ``prefix``, then one byte at a time for longer than the budget."""
    client.sendall(prefix)
    for _ in range(int(_STALL_SECONDS / _TRICKLE_SECONDS)):
        client.sendall(b"X")
        time.sleep(_TRICKLE_SECONDS)


_CHUNKED_HEAD = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
)


def _slow_status_line(client: ssl.SSLSocket) -> None:
    _trickle(client, b"HTTP/1.1 200 OK")


def _slow_headers(client: ssl.SSLSocket) -> None:
    _trickle(client, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Pad: ")


def _slow_chunk_framing(client: ssl.SSLSocket) -> None:
    _trickle(client, _CHUNKED_HEAD + b"0000000")


def _slow_body(client: ssl.SSLSocket) -> None:
    _trickle(client, b"HTTP/1.1 200 OK\r\nContent-Length: 400\r\n\r\n")


def _slow_trailers(client: ssl.SSLSocket) -> None:
    _trickle(client, _CHUNKED_HEAD + b"5\r\nhello\r\n0\r\nX-Trailer: ")


@contextmanager
def _raw_tls_server(write: Callable[[ssl.SSLSocket], None], material: _Material) -> Iterator[str]:
    """Serve one hand-written response inside TLS, then hold the socket open.

    ``http.server`` cannot emit a partial status line, header block or chunk
    framing; a raw socket can. Going through TLS here is the point: it proves the
    deadline still applies to reads performed by ``SSLSocket`` after decryption.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(material.cert), str(material.key))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5.0)
    port = listener.getsockname()[1]
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
                    tls = context.wrap_socket(client, server_side=True)
                except (ssl.SSLError, OSError):
                    return
                with tls:
                    try:
                        tls.recv(65_536)
                    except OSError:
                        return
                    try:
                        write(tls)
                    except OSError:
                        return
                    finished.wait(_STALL_SECONDS)
        finally:
            finished.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{port}"
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
def test_every_https_phase_is_bounded_by_the_same_deadline(
    phase: Callable[[ssl.SSLSocket], None],
    tls_material: _Material,
    trusting_context: ssl.SSLContext,
) -> None:
    """The deadline applies after decryption, not only to plain TCP."""
    del trusting_context
    with _raw_tls_server(phase, tls_material) as base_url:
        provider = GroqProvider(
            api_key=_API_KEY,
            model=_MODEL,
            base_url=base_url,
            timeout_seconds=_BUDGET_SECONDS,
            max_attempts=1,
            sleep=lambda _delay: None,
        )
        started = time.monotonic()
        result, instance = provider.parse(_request(), TinyPayload)
        elapsed = time.monotonic() - started

    assert instance is None, phase.__name__
    assert isinstance(result, ParseFailure), phase.__name__
    assert result.provider_error == ProviderErrorCode.TIMEOUT.value, phase.__name__
    assert result.retryable is True, phase.__name__
    assert elapsed < _BUDGET_SECONDS + _SLACK_SECONDS, phase.__name__
    assert elapsed < _STALL_SECONDS, phase.__name__


def _stalled_redirect(client: ssl.SSLSocket) -> None:
    """Answer 302 with a body that never finishes arriving."""
    client.sendall(
        b"HTTP/1.1 302 Found\r\nLocation: https://collector.invalid/steal\r\n"
        b"Content-Type: text/plain\r\nContent-Length: 4096\r\n\r\npartial"
    )
    for _ in range(int(_STALL_SECONDS / _TRICKLE_SECONDS)):
        time.sleep(_TRICKLE_SECONDS)


def test_a_stalled_https_redirect_is_classified_without_reading_its_body(
    tls_material: _Material, trusting_context: ssl.SSLContext
) -> None:
    """A 3xx over TLS is decided from status and headers, and never retried."""
    del trusting_context
    with _raw_tls_server(_stalled_redirect, tls_material) as base_url:
        provider = GroqProvider(
            api_key=_API_KEY,
            model=_MODEL,
            base_url=base_url,
            timeout_seconds=5.0,
            max_attempts=2,
            base_delay_seconds=0.01,
            max_delay_seconds=0.02,
            sleep=lambda _delay: None,
        )
        started = time.monotonic()
        result, instance = provider.parse(_request(), TinyPayload)
        elapsed = time.monotonic() - started

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.INVALID_REQUEST.value
    assert result.retryable is False
    # Reading the stalled body would take _STALL_SECONDS; not reading it is instant.
    assert elapsed < _STALL_SECONDS / 2
    assert "collector.invalid" not in f"{result!r} {result.model_dump_json()}"


def test_an_https_redirect_never_replays_the_credential_at_the_destination(
    tls_material: _Material, trusting_context: ssl.SSLContext
) -> None:
    """The redirect target of an HTTPS response is never contacted."""
    del trusting_context
    seen: list[dict[str, object]] = []

    class _Redirects(_QuietHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", f"{destination_base}{_CHAT_PATH}")
            self.send_header("Content-Length", "0")
            self.end_headers()

    with (
        _serve_tls(_QuietHandler, tls_material, seen=seen) as destination,
        _serve_tls(_Redirects, tls_material, seen=[]) as origin,
    ):
        destination_base = _https_base(destination)
        result, instance = _provider(_https_base(origin)).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert result.provider_error == ProviderErrorCode.INVALID_REQUEST.value
    assert seen == [], "the destination must never receive the credential"


def test_https_metadata_is_still_sanitised(
    tls_material: _Material, trusting_context: ssl.SSLContext
) -> None:
    """The metadata protections hold on the TLS path too."""
    del trusting_context

    class _Surrogate(_QuietHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps(
                {
                    "model": chr(0xD800),
                    "choices": [{"message": {"role": "assistant", "content": '{"a": "x"}'}}],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with _serve_tls(_Surrogate, tls_material, seen=[]) as server:
        result, instance = _provider(_https_base(server)).parse(_request(), TinyPayload)

    assert instance is None
    assert isinstance(result, ParseFailure)
    assert [issue.error_type for issue in result.issues] == ["provider_metadata_invalid"]
    assert result.model == _MODEL
    assert "ud800" not in result.model_dump_json().casefold()
