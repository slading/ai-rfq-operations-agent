"""The one live LLM adapter: Groq's OpenAI-compatible chat completions.

This module is the only place in the system that knows a vendor exists. It
implements :class:`~rfq_agent.contracts.llm.LLMProvider` and nothing else: no
domain object, no prompt, no schema and no decision. It sends the
:class:`~rfq_agent.contracts.llm.ParseRequest` it is given, and returns either a
schema-validated :class:`~rfq_agent.contracts.llm.ParseSuccess` or a
:class:`~rfq_agent.contracts.llm.ParseFailure` describing, in a closed
vocabulary, why not.

Four properties are deliberate.

**Failures are data, not exceptions.** A timeout, a 429, a 500, a 401, a
response larger than the configured ceiling and a response that is not the
schema all come back as ``ParseFailure``: the pipeline decides what that means,
and the adapter never raises a vendor exception into it. Every message is a
fixed sentence and every code is a closed enum value, so a failure can be
printed without echoing a credential, a prompt or a model's output. The raw
response is deliberately not retained.

**Retries are bounded and only for availability.** Only the provider codes the
accepted taxonomy already calls retryable are retried
(:data:`~rfq_agent.contracts.errors.RETRYABLE_PROVIDER_CODES`), the attempt
count is capped, the backoff is capped, and a ``Retry-After`` hint is honoured
within that cap. Bad output is never retried here - a repair is a pipeline
policy, and this slice has none. Exhausting the attempts is a reported failure,
never a best-effort guess.

**Every call is bounded, in wall-clock terms - with one honest exception.** Each
attempt gets one monotonic deadline, fixed before connecting. The TCP connect,
the TLS handshake (driven non-blocking under ``select``, so a peer that trickles
the handshake cannot outlast it), the request write, the status line, the
response headers, chunk framing, the body and the trailers all run under it: the
socket is wrapped so the remaining budget is re-armed before every read and write,
which is the one point every one of those phases passes through. Alongside the
deadline there is a ceiling on generated tokens, a maximum number of attempts,
and a maximum response size in bytes - the reader stops one byte past the ceiling
and refuses the body rather than buffering whatever the provider sends.

The exception is name resolution. ``socket.getaddrinfo`` is a blocking libc call
that no socket timeout, ``select`` or Python-level deadline can interrupt, so a
slow resolver can overrun the budget; every stdlib HTTP client shares this. The
deadline is re-checked as soon as the connection is established, so an overrun is
reported as ``TIMEOUT`` instead of being spent silently on the TLS handshake and
the request. No other phase is unbounded.

**TLS is never stepped around.** Every byte of the response is read through the
:class:`ssl.SSLSocket` the handshake produced, through the buffered reader
``http.client`` parses status lines, headers and bodies with. The transport never
reads the descriptor directly and never substitutes a plain socket for the TLS
one, so decryption, certificate verification and hostname checking apply to the
same bytes that are parsed.

**Redirects are never followed and never read.** A 3xx is decided from the
status line and headers alone: the response object is closed immediately, its
body is left unread, and ``Location`` is never parsed. A client that read the
body first would block on the provider's schedule, and one that followed the
redirect would forward ``Authorization`` to whatever host the response names -
including an ``https`` to ``http`` downgrade. A 3xx status is a non-retryable
typed failure, so no second request is ever made.

**Structured output is validated locally, not trusted.** The adapter asks for
JSON mode and validates the payload against the domain schema itself over
:class:`pydantic.BaseModel`; ``strict_mode_used`` is therefore ``False`` and the
capabilities never claim a strict-schema guarantee this call does not use. A
model that answers with prose, Markdown fences or extra fields is a
``ParseFailure``, not a repaired value.

The API key lives in an ``Authorization`` header on an HTTPS URL and nowhere
else: it is not in the request body, not in a failure, not in ``repr`` and not in
any message this module builds.
"""

from __future__ import annotations

import http.client
import io
import json
import secrets
import select
import socket
import ssl
import time
import urllib.error
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import SplitResult, urlsplit

from pydantic import ValidationError

from rfq_agent.contracts.errors import (
    RETRYABLE_PROVIDER_CODES,
    ProviderErrorCode,
    ProviderNotConfiguredError,
    ProviderUnsupportedError,
)
from rfq_agent.contracts.llm import (
    ChatRole,
    ParseFailure,
    ParseIssue,
    ParseSuccess,
    ProviderCapabilities,
    ProviderName,
    TokenUsage,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel as PydanticModel

    from rfq_agent.contracts.llm import (
        CompletionRequest,
        CompletionResult,
        ParseRequest,
        ParseResult,
    )
    from rfq_agent.domain.values import Json

__all__ = [
    "GroqProvider",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
]

#: The endpoint, relative to the configured base URL.
_CHAT_COMPLETIONS_PATH = "/chat/completions"

#: Smallest/largest accepted bounds. They mirror the contract's own limits:
#: ``ParseRequest.max_output_tokens`` is capped at 32 768, and a response larger
#: than a few hundred kilobytes cannot be a useful extraction claim.
_MIN_RESPONSE_BYTES = 1_024
_MAX_RESPONSE_BYTES = 4_194_304
_MIN_OUTPUT_TOKENS = 64
_MAX_OUTPUT_TOKENS = 32_768
_MAX_ATTEMPTS = 10

#: HTTP status to the accepted provider-neutral classification. Anything else is
#: ``UNKNOWN`` - reported, never guessed into a retryable bucket.
_STATUS_CODES: dict[int, ProviderErrorCode] = {
    400: ProviderErrorCode.INVALID_REQUEST,
    401: ProviderErrorCode.AUTHENTICATION,
    403: ProviderErrorCode.AUTHENTICATION,
    404: ProviderErrorCode.INVALID_REQUEST,
    413: ProviderErrorCode.INVALID_REQUEST,
    422: ProviderErrorCode.INVALID_REQUEST,
    429: ProviderErrorCode.RATE_LIMITED,
    500: ProviderErrorCode.SERVER_ERROR,
    502: ProviderErrorCode.SERVER_ERROR,
    503: ProviderErrorCode.OVERLOADED,
    504: ProviderErrorCode.TIMEOUT,
}

#: Longest model identifier and provider-side request id the contract accepts.
_MAX_IDENTIFIER_CHARS = 120

#: Largest per-request timeout this adapter permits, in seconds.
_MAX_TIMEOUT_SECONDS = 300

#: The only status that carries a usable body.
_HTTP_OK = 200

#: A redirect is never followed and its body is never read.
_REDIRECT_MIN = 300
_REDIRECT_MAX = 400

#: Smallest budget handed to a blocking socket call, so a nearly expired deadline
#: is still a positive timeout rather than a non-blocking socket.
_MIN_BUDGET_SECONDS = 0.001

#: How much of a response body one underlying read may return. Small enough that
#: the remaining wall-clock budget is re-checked often, large enough that a normal
#: extraction response is read in a handful of calls.
_READ_CHUNK_BYTES = 8_192


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One outbound HTTP request, as a transport receives it.

    A plain value object rather than a live connection: it is what makes the
    adapter testable without a socket, and it keeps the credential in exactly one
    field (``headers``) that no failure path copies.
    """

    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    timeout_seconds: float
    max_response_bytes: int


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One inbound HTTP response, as a transport returns it."""

    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    """The seam that performs one request.

    The default implementation uses :mod:`http.client`; tests pass a scripted
    callable. A transport raises ``TimeoutError`` for a timeout,
    ``OSError``/``urllib.error.URLError`` for a network failure and
    ``http.client.HTTPException`` for a protocol failure - it never retries, and
    it never classifies. The default transport treats ``timeout_seconds`` as a
    wall-clock deadline for the whole attempt; a custom transport is expected to
    honour the same contract.
    """

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Perform ``request`` and return the response."""
        ...


class _DeadlineSocket:
    """A socket proxy on which every read and write observes one deadline.

    This is the load-bearing piece of the wall-clock budget. A socket timeout is
    *per operation*: a response that trickles one byte at a time never trips it,
    however small it is. ``http.client`` also performs reads this module cannot
    see - the status line, the response headers, chunk framing and the trailers
    are all parsed inside library calls - so checking the clock between
    application-level reads cannot interrupt them either.

    Every one of those library reads ultimately calls ``recv_into`` (or
    ``recv``) on the socket, so wrapping the socket is what makes the deadline
    apply to *all* of them: before each call the remaining budget is recomputed
    from the monotonic clock and armed as that call's own timeout. No single read
    can wait past the deadline, and a trickle that keeps making progress still
    stops at it. ``sendall`` is bounded the same way, and ``makefile`` hands back
    a buffered reader whose underlying reads go through this proxy rather than
    around it - and therefore through the TLS object beneath it, never around
    that.
    """

    __slots__ = ("_deadline", "_sock")

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def recv(self, *args: object) -> bytes:
        """Read at most one chunk, waiting no later than the deadline."""
        self._sock.settimeout(_remaining(self._deadline))
        return self._sock.recv(*args)  # type: ignore[arg-type]

    def recv_into(self, *args: object) -> int:
        """Read into a buffer, waiting no later than the deadline."""
        self._sock.settimeout(_remaining(self._deadline))
        return self._sock.recv_into(*args)  # type: ignore[arg-type]

    def sendall(self, *args: object) -> None:
        """Write the request, waiting no later than the deadline."""
        self._sock.settimeout(_remaining(self._deadline))
        self._sock.sendall(*args)  # type: ignore[arg-type]

    def makefile(self, mode: str = "rb", *args: object, **kwargs: object) -> io.BufferedReader:
        """Return a buffered reader that reads *through* this proxy.

        The reader is :class:`socket.SocketIO` over this proxy, which is exactly
        how ``socket.socket.makefile`` builds one over a socket: every read lands
        in :meth:`recv_into` and is therefore given the remaining budget. What it
        must *not* be is a second plain socket over the same descriptor - that
        would read raw TLS records and hand back ciphertext (see the module
        docstring), so the decryption, and with it the certificate and hostname
        verification already performed by the handshake, would be bypassed.

        The reference count is the stdlib's, for the stdlib's reason:
        ``http.client`` closes the connection as soon as the response headers say
        the connection will not persist, *before* the body has been read. Counting
        the reader keeps the descriptor alive until the reader itself closes.
        """
        del args, kwargs
        if "r" not in mode:
            msg = "the provider transport only reads through a buffered file"
            raise ValueError(msg)
        # ``_io_refs`` is the documented-by-source counter ``socket.makefile``
        # uses for exactly this purpose; there is no public equivalent.
        self._sock._io_refs += 1  # noqa: SLF001
        return io.BufferedReader(socket.SocketIO(self, "rb"))  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        """Delegate everything else (``close``, ``fileno``, ``shutdown``) unchanged."""
        return getattr(self._sock, name)


def _now() -> float:
    """Return the monotonic clock, indirected so the deadline is testable."""
    return time.monotonic()


def _remaining(deadline: float) -> float:
    """Return the budget left, or raise the deadline ``TimeoutError``."""
    remaining = deadline - _now()
    if remaining <= 0:
        msg = "the provider request exceeded its wall-clock deadline"
        raise TimeoutError(msg)
    return remaining


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection whose socket enforces the attempt's deadline."""

    def __init__(self, host: str, port: int, deadline: float) -> None:
        super().__init__(host, port, timeout=max(deadline - _now(), _MIN_BUDGET_SECONDS))
        self._deadline = deadline

    def connect(self) -> None:
        """Connect inside the budget, then install the deadline proxy."""
        self.timeout = _remaining(self._deadline)
        super().connect()
        self.sock = _DeadlineSocket(self.sock, self._deadline)


def _ssl_context() -> ssl.SSLContext:
    """The context used for provider traffic: verification on, as the stdlib has it.

    ``ssl.create_default_context`` is the hardened default - ``CERT_REQUIRED``
    against the system trust store, and hostname checking, which ``SSLContext``
    applies to the ``server_hostname`` passed to ``wrap_socket``. The ALPN and
    post-handshake-auth settings repeat what ``http.client`` would have applied to
    its own default context. Indirected through this function so the suite can
    trust a loopback fixture CA without touching global SSL state; production
    always gets the verifying default.
    """
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    if context.post_handshake_auth is not None:
        context.post_handshake_auth = True
    return context


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection whose connect, handshake and socket share one deadline.

    The TLS handshake is driven explicitly against a non-blocking socket with a
    ``select`` wait bounded by the remaining budget. A blocking handshake would
    honour only a per-read timeout, so a trickling peer could exceed the deadline.

    The handshake also *is* the security boundary: certificate verification and
    hostname checking happen here, under the deadline, and every later read goes
    through the resulting :class:`ssl.SSLSocket`. Nothing in this module ever
    reads the underlying descriptor directly.
    """

    def __init__(self, host: str, port: int, deadline: float) -> None:
        super().__init__(
            host,
            port,
            timeout=max(deadline - _now(), _MIN_BUDGET_SECONDS),
            context=_ssl_context(),
        )
        self._deadline = deadline

    def connect(self) -> None:
        """Connect, complete the handshake inside the budget, then proxy.

        Cleanup follows ownership, not construction order: ``wrap_socket`` detaches
        the raw socket and returns an :class:`ssl.SSLSocket` that now owns the
        descriptor, so a failed handshake must close *that* object. Closing the
        detached raw socket instead - as this did before - silently leaked the
        TLS socket and its descriptor for as long as the exception traceback that
        referenced it survived.
        """
        raw = socket.create_connection(
            (self.host, self.port), _remaining(self._deadline), self.source_address
        )
        try:
            tls = self._context.wrap_socket(
                raw, server_hostname=self.host, do_handshake_on_connect=False
            )
        except BaseException:
            raw.close()
            raise
        try:
            # DNS has already happened inside ``create_connection``; re-checking
            # here is the only point at which an unresolvable overrun, or a
            # multi-address connect, can be noticed. See the module docstring.
            _remaining(self._deadline)
            _complete_handshake(tls, self._deadline)
            tls.settimeout(_remaining(self._deadline))
        except BaseException:
            tls.close()
            raise
        self.sock = _DeadlineSocket(tls, self._deadline)


def _complete_handshake(sock: ssl.SSLSocket, deadline: float) -> None:
    """Drive a TLS handshake, never waiting past the remaining budget."""
    sock.setblocking(False)
    while True:
        try:
            sock.do_handshake()
            return
        except ssl.SSLWantReadError:
            _await_ready(sock, deadline, writing=False)
        except ssl.SSLWantWriteError:
            _await_ready(sock, deadline, writing=True)


def _await_ready(sock: ssl.SSLSocket, deadline: float, *, writing: bool) -> None:
    """Wait for the socket to become ready, or raise the deadline timeout."""
    remaining = _remaining(deadline)
    if writing:
        ready = select.select([], [sock], [], remaining)[1]
    else:
        ready = select.select([sock], [], [], remaining)[0]
    if not ready:
        msg = "the provider TLS handshake exceeded the request deadline"
        raise TimeoutError(msg)


def _connect(parts: SplitResult, deadline: float) -> http.client.HTTPConnection:
    """Open a connection for ``parts`` bounded by this attempt's deadline."""
    secure = parts.scheme == "https"
    host = parts.hostname or ""
    port = parts.port or (443 if secure else 80)
    if secure:
        return _DeadlineHTTPSConnection(host, port, deadline)
    return _DeadlineHTTPConnection(host, port, deadline)


def _http_transport(request: HttpRequest) -> HttpResponse:
    """Perform one HTTP(S) request under one enforced wall-clock deadline.

    The budget is fixed before connecting and applies to every phase:
    ``connect`` (TCP and the TLS handshake), the request write, the status line,
    the response headers, chunk framing, the body and the trailers - all of them
    read or write through :class:`_DeadlineSocket`, which re-arms the remaining
    budget on the socket before each underlying operation.

    No redirect is followed and no redirect body is read: a 3xx is returned to
    the caller as a status with an empty body, so a stalled redirect cannot
    consume the budget, and a ``Location`` is never contacted. A 2xx body is read
    at most one byte past the configured ceiling.
    """
    deadline = _now() + request.timeout_seconds
    parts = urlsplit(request.url)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    connection = _connect(parts, deadline)
    try:
        connection.request("POST", target, body=request.body, headers=dict(request.headers))
        response = connection.getresponse()
        status = int(response.status)
        headers = dict(response.getheaders())
        if _REDIRECT_MIN <= status < _REDIRECT_MAX:
            # The caller classifies it. Closing without reading keeps a stalled
            # redirect body from becoming a timeout - and therefore a retry.
            response.close()
            return HttpResponse(status=status, body=b"", headers=headers)
        try:
            body = _read_bounded(response, request.max_response_bytes, deadline)
        finally:
            response.close()
        return HttpResponse(status=status, body=body, headers=headers)
    finally:
        connection.close()


def _read_bounded(response: object, max_bytes: int, deadline: float) -> bytes:
    """Read a body up to ``max_bytes + 1`` bytes, or raise ``TimeoutError``.

    The socket proxy already bounds every underlying read; this loop bounds the
    *total* bytes and re-checks the clock between chunks, so an opaque response
    object handed to the adapter is bounded too. ``read1`` is used where
    available because it performs at most one underlying read.
    """
    read = getattr(response, "read1", None) or response.read  # type: ignore[attr-defined]
    chunks: list[bytes] = []
    budget = max_bytes + 1
    reached_eof = False
    while budget > 0:
        _remaining(deadline)
        chunk = read(min(_READ_CHUNK_BYTES, budget))
        if not chunk:
            reached_eof = True
            break
        chunks.append(chunk)
        budget -= len(chunk)
    body = b"".join(chunks)
    if reached_eof:
        _require_complete_body(response, body)
    return body


def _require_complete_body(response: object, body: bytes) -> None:
    """Report a body that ended before its declared ``Content-Length``.

    ``read1`` returns short counts and cannot raise on its own, so a connection
    that closes mid-body would otherwise look like a complete response and be
    misreported as unparsable JSON rather than as the retryable protocol failure
    it is. ``http.client`` keeps the unread byte count in ``length``; a positive
    remainder after EOF is an :class:`http.client.IncompleteRead`, which the
    adapter converts into a typed server failure.
    """
    remaining = getattr(response, "length", None)
    if isinstance(remaining, int) and remaining > 0:
        raise http.client.IncompleteRead(body, remaining)


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One attempt's outcome, plus the provider's own backoff hint."""

    result: ParseSuccess | ParseFailure
    retry_after_seconds: float | None = None


class GroqProvider:
    """A live :class:`~rfq_agent.contracts.llm.LLMProvider` over Groq.

    Structured extraction only: :meth:`parse` is implemented and
    :meth:`complete` reports that tool-calling completions are out of scope for
    this slice rather than pretending to support them.

    Args:
        api_key: The provider credential. Read from the environment by the
            caller; never logged, echoed or included in a failure.
        model: The model identifier, from configuration.
        base_url: HTTPS base URL, from configuration.
        timeout_seconds: Wall-clock budget for one attempt, in seconds: a
            connect/header/body deadline, not a per-socket-operation timeout.
        max_response_bytes: Hard ceiling on a response body.
        max_output_tokens: Default completion-token ceiling when the request
            does not state one.
        max_attempts: Total attempts, including the first.
        base_delay_seconds: First backoff delay.
        max_delay_seconds: Backoff ceiling, and the cap on ``Retry-After``.
        jitter_ratio: Fraction of the delay added as jitter, in ``[0, 1]``.
        transport: The HTTP seam; defaults to :func:`_http_transport`.
        sleep: Injected so retry timing is testable; defaults to ``time.sleep``.

    Raises:
        ProviderNotConfiguredError: When no API key is configured or the base
            URL is not HTTPS - a credential must never travel in the clear.
        ValueError: When a numeric bound is outside its documented range.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 262_144,
        max_output_tokens: int = 1_024,
        max_attempts: int = 3,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        jitter_ratio: float = 0.2,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            msg = "no provider credential is configured: live calls are unavailable"
            raise ProviderNotConfiguredError(msg)
        if not model.strip() or len(model) > _MAX_IDENTIFIER_CHARS:
            msg = "a provider model identifier must be 1-120 characters"
            raise ValueError(msg)
        scheme = urlsplit(base_url).scheme.casefold()
        if scheme != "https":
            msg = "the provider base URL must be HTTPS: a credential is never sent in the clear"
            raise ProviderNotConfiguredError(msg)
        if not (0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS):
            msg = "timeout_seconds must be in (0, 300]"
            raise ValueError(msg)
        if not (_MIN_RESPONSE_BYTES <= max_response_bytes <= _MAX_RESPONSE_BYTES):
            msg = f"max_response_bytes must be in [{_MIN_RESPONSE_BYTES}, {_MAX_RESPONSE_BYTES}]"
            raise ValueError(msg)
        if not (_MIN_OUTPUT_TOKENS <= max_output_tokens <= _MAX_OUTPUT_TOKENS):
            msg = f"max_output_tokens must be in [{_MIN_OUTPUT_TOKENS}, {_MAX_OUTPUT_TOKENS}]"
            raise ValueError(msg)
        if not (1 <= max_attempts <= _MAX_ATTEMPTS):
            msg = f"max_attempts must be in [1, {_MAX_ATTEMPTS}]"
            raise ValueError(msg)
        if not (0 <= jitter_ratio <= 1):
            msg = "jitter_ratio must be in [0, 1]"
            raise ValueError(msg)
        if base_delay_seconds <= 0 or max_delay_seconds < base_delay_seconds:
            msg = "retry delays must be positive, with max_delay_seconds >= base_delay_seconds"
            raise ValueError(msg)

        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._jitter_ratio = jitter_ratio
        self._transport: HttpTransport = transport or _http_transport
        self._sleep = sleep

    @property
    def capabilities(self) -> ProviderCapabilities:
        """What this adapter, configured with this model, actually offers.

        ``supports_strict_json_schema`` is ``False``: the adapter asks for JSON
        mode and validates locally, so no strict-decoding guarantee is claimed.
        ``supports_tools`` is ``False``: this slice makes one structured call.
        """
        return ProviderCapabilities(
            provider=ProviderName.GROQ,
            model=self._model,
            supports_tools=False,
            supports_parallel_tool_calls=False,
            supports_strict_json_schema=False,
            supports_reasoning_effort=False,
        )

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """Report that tool-calling completions are not part of this slice.

        Project 04 authorises one structured extraction call. A half-implemented
        agent loop would be worse than none, so this is an explicit typed
        refusal rather than a silent empty answer.
        """
        del request
        msg = (
            "this adapter implements structured extraction only: tool-calling "
            "completions are outside the Project 04 slice"
        )
        raise ProviderUnsupportedError(msg)

    def parse(
        self, request: ParseRequest, model: type[PydanticModel]
    ) -> tuple[ParseResult, PydanticModel | None]:
        """Send one structured-output request and validate the answer.

        The payload is sent as a JSON-mode chat completion and the returned
        object is validated against ``model`` with the same pydantic schema the
        prompt describes. Nothing is repaired and nothing is retried for bad
        output; only availability failures are retried, a bounded number of
        times.

        Returns:
            ``(ParseSuccess, instance)`` when the response parsed and validated,
            ``(ParseFailure, None)`` otherwise. The failure never carries raw
            provider text.
        """
        self._require_structured_messages(request)
        payload = self._encode(request)
        attempt = 1
        while True:
            outcome = self._attempt(payload, request.model)
            result = outcome.result
            if isinstance(result, ParseFailure):
                if result.retryable and attempt < self._max_attempts:
                    self._sleep(self._delay(attempt, outcome.retry_after_seconds))
                    attempt += 1
                    continue
                return result, None
            instance = self._bind(result, model)
            if isinstance(instance, ParseFailure):
                return instance, None
            return result, instance

    def _require_structured_messages(self, request: ParseRequest) -> None:
        """Refuse a request this adapter cannot send faithfully.

        Only system/user/assistant text messages are representable in a JSON-mode
        call. A tool message would silently lose its meaning, so it is refused
        rather than dropped.
        """
        for message in request.messages:
            if message.role is ChatRole.TOOL or message.tool_calls:
                msg = "structured-output requests carry text messages only"
                raise ProviderUnsupportedError(msg)

    def _encode(self, request: ParseRequest) -> bytes:
        """Serialise one request body. The credential is not part of it."""
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "response_format": {"type": "json_object"},
            "temperature": float(request.temperature),
            "max_completion_tokens": request.max_output_tokens or self._max_output_tokens,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _attempt(self, payload: bytes, model: str) -> _Attempt:
        """Perform one HTTP attempt and classify its outcome."""
        request = HttpRequest(
            url=f"{self._base_url}{_CHAT_COMPLETIONS_PATH}",
            headers=(
                ("Authorization", f"Bearer {self._api_key}"),
                ("Content-Type", "application/json"),
                ("Accept", "application/json"),
            ),
            body=payload,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
        )
        started = _now()
        try:
            response = self._transport(request)
        except TimeoutError:
            return _Attempt(self._failure(ProviderErrorCode.TIMEOUT, started))
        except ssl.SSLCertVerificationError:
            # The endpoint could not prove it is the configured provider: the
            # certificate is not trusted, or it is issued for another host. TLS
            # refused before anything was sent, and no retry can change that, so
            # this is not a transient server error.
            return _Attempt(self._failure(ProviderErrorCode.INVALID_REQUEST, started))
        except urllib.error.URLError as error:
            code = (
                ProviderErrorCode.TIMEOUT
                if isinstance(error.reason, TimeoutError)
                else ProviderErrorCode.SERVER_ERROR
            )
            return _Attempt(self._failure(code, started))
        except http.client.HTTPException:
            # Malformed status lines, premature disconnects and truncated bodies
            # (``IncompleteRead``) are protocol failures of a transport, not
            # exceptions the pipeline should ever see. They are reported as the
            # retryable server errors they are.
            return _Attempt(self._failure(ProviderErrorCode.SERVER_ERROR, started))
        except OSError:
            return _Attempt(self._failure(ProviderErrorCode.SERVER_ERROR, started))

        if _REDIRECT_MIN <= response.status < _REDIRECT_MAX:
            # The transport never follows a redirect and never reads its body,
            # so this is the first and only decision about it. Replaying a
            # credentialed request at a host the response names would be a
            # credential leak, and an https->http hop would send it in the clear.
            # A redirect therefore means the configured endpoint is not the API:
            # a non-retryable failure, reported without naming the destination.
            return _Attempt(self._failure(ProviderErrorCode.INVALID_REQUEST, started))
        if response.status != _HTTP_OK:
            code = _STATUS_CODES.get(response.status, ProviderErrorCode.UNKNOWN)
            return _Attempt(self._failure(code, started), _retry_after(response.headers))
        return _Attempt(self._decode(response, model, started))

    def _decode(
        self, response: HttpResponse, model: str, started: float
    ) -> ParseSuccess | ParseFailure:
        """Turn a 200 response into validated data or a typed failure."""
        latency_ms = _elapsed_ms(started)
        if len(response.body) > self._max_response_bytes:
            return self._content_failure(
                "response_too_large",
                "provider response exceeded the configured size limit",
                latency_ms,
                model,
            )
        try:
            envelope = json.loads(response.body.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._content_failure(
                "json_invalid", "provider response is not valid JSON", latency_ms, model
            )
        if not isinstance(envelope, dict):
            return self._content_failure(
                "json_invalid", "provider response is not a JSON object", latency_ms, model
            )
        usage = _usage(envelope.get("usage"))
        content = _message_content(envelope)
        if content is None:
            return self._content_failure(
                "missing_content",
                "provider response carries no message content",
                latency_ms,
                model,
            )
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return self._content_failure(
                "json_invalid",
                "the model's structured output is not valid JSON",
                latency_ms,
                model,
            )
        if not isinstance(data, dict):
            return self._content_failure(
                "json_invalid",
                "the model's structured output is not a JSON object",
                latency_ms,
                model,
            )
        if not _metadata_is_usable(envelope):
            return self._content_failure(
                "provider_metadata_invalid",
                "provider response metadata is not usable",
                latency_ms,
                model,
            )
        success = _validated_success(data=data, usage=usage, latency_ms=latency_ms, model=model)
        if success is None:
            # The response's own metadata cannot satisfy the contract's ``model``
            # field, so building ParseSuccess would raise instead of reporting a
            # failure. The whole response is refused as sanitized content.
            return self._content_failure(
                "provider_metadata_invalid",
                "provider response metadata is not usable",
                latency_ms,
                model,
            )
        return success

    def _bind(
        self, result: ParseSuccess, model: type[PydanticModel]
    ) -> PydanticModel | ParseFailure:
        """Validate a parsed payload against the domain schema.

        The failure is reported through :func:`_schema_issue`, which reads only
        pydantic's count-only native API. The rest of the public error surface -
        ``errors()``, ``str()``, ``json()`` - is deliberately not used at this
        trust boundary, for the reason
        :mod:`rfq_agent.observability.validation_errors` documents: it formats
        messages and can interpolate the untrusted input.
        """
        try:
            return model.model_validate(result.data)
        except ValidationError as error:
            return ParseFailure(
                issues=(_schema_issue(error),),
                usage=result.usage,
                latency_ms=result.latency_ms,
                # The configured identifier, never ``result.model``: a failure is
                # a place a provider-controlled string must not reach.
                model=self._model,
            )

    def _failure(self, code: ProviderErrorCode, started: float) -> ParseFailure:
        """Build a transport-level failure in the accepted closed vocabulary."""
        return ParseFailure(
            provider_error=code.value,
            retryable=code in RETRYABLE_PROVIDER_CODES,
            latency_ms=_elapsed_ms(started),
            model=self._model,
        )

    def _content_failure(
        self, error_type: str, message: str, latency_ms: int, model: str
    ) -> ParseFailure:
        """Build a content-level failure: the provider answered, badly."""
        return ParseFailure(
            issues=(ParseIssue(field="<response>", message=message, error_type=error_type),),
            retryable=False,
            latency_ms=latency_ms,
            model=model,
        )

    def _delay(self, attempt: int, retry_after_seconds: float | None) -> float:
        """Compute the bounded backoff before the next attempt.

        A provider's ``Retry-After`` is honoured only inside the configured
        ceiling; otherwise exponential backoff plus a fractional jitter drawn
        from the OS CSPRNG (no seeded pseudo-random state).
        """
        if retry_after_seconds is not None:
            return max(0.0, min(retry_after_seconds, self._max_delay_seconds))
        delay = min(self._base_delay_seconds * (2 ** (attempt - 1)), self._max_delay_seconds)
        jitter = delay * self._jitter_ratio * (secrets.randbelow(1_000) / 1_000)
        return delay + jitter


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since ``started`` (never negative)."""
    return max(0, int((_now() - started) * 1_000))


def _validated_success(
    *, data: dict[str, Json], usage: TokenUsage, latency_ms: int, model: str
) -> ParseSuccess | None:
    """Build the success object, or ``None`` when its metadata is unusable.

    The object is validated against the *actual* downstream contract by
    constructing it: the contract's own constraints (``model`` a 1-120 character
    string, a coherent token total) decide, so this cannot drift from what the
    rest of the system accepts. Provider-controlled text is never carried into
    it - ``model`` is the configured identifier, and the vendor's own report is
    only checked for usability - so neither a success nor a later failure can
    echo a string the provider chose.
    """
    try:
        return ParseSuccess(
            data=data,
            strict_mode_used=False,
            usage=usage,
            latency_ms=latency_ms,
            model=model,
            raw="",
        )
    except ValidationError:
        return None


def _metadata_is_usable(envelope: Mapping[str, object]) -> bool:
    """Whether the response's own model metadata can be believed at all.

    An *absent* identifier is a provider reporting habit and is ignored. A
    *present* one must satisfy every constraint of the bytes it will travel as:
    a string, non-blank after stripping, within the contract's length bound,
    encodable as UTF-8 and printable. The last two are what reject a lone
    surrogate such as U+D800: the schema accepts it in a ``str`` field, so only an
    encoding check catches it before it reaches a serialiser.
    """
    if "model" not in envelope:
        return True
    value = envelope["model"]
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > _MAX_IDENTIFIER_CHARS:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return text.isprintable()


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse a ``Retry-After`` header as seconds, when it is a plain integer."""
    for name, value in headers.items():
        if name.casefold() != "retry-after":
            continue
        try:
            return float(int(value.strip()))
        except (TypeError, ValueError):
            return None
    return None


def _usage(value: object) -> TokenUsage:
    """Read the provider's token accounting defensively.

    A reported total that disagrees with its parts is not accepted verbatim -
    the contract refuses it - so the total is derived from the parts instead of
    inventing an adjustment.
    """
    if not isinstance(value, dict):
        return TokenUsage()
    prompt = _count(value.get("prompt_tokens"))
    completion = _count(value.get("completion_tokens"))
    reported_total = _count(value.get("total_tokens"))
    total = reported_total if reported_total == prompt + completion else prompt + completion
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=0,
    )


def _count(value: object) -> int:
    """Read one non-negative token count, defaulting to zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _message_content(envelope: Mapping[str, object]) -> str | None:
    """Extract ``choices[0].message.content`` when it is a non-empty string."""
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _schema_issue(error: ValidationError) -> ParseIssue:
    """Describe a schema violation with a count and no untrusted text.

    Only pydantic's native ``error_count()`` is read, exactly as
    :func:`rfq_agent.observability.validation_errors.render_validation_error`
    does. Field paths and messages are withheld because obtaining them requires
    formatting input-derived messages; a closed code plus the count is enough for
    an operator to know the model produced something the schema refuses.
    """
    try:
        count = ValidationError.error_count(error)
    except Exception:  # noqa: BLE001 - fail closed without reading exception text
        count = 0
    message = "model output failed schema validation"
    if type(count) is int and count > 0:
        message = f"{message}: {min(count, 32)} error(s)"
    return ParseIssue(field="<response>", message=message, error_type="schema_invalid")
