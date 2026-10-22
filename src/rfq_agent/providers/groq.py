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

**Every call is bounded, in wall-clock terms.** Each attempt gets one monotonic
deadline: the connect and header read are bounded by it, and the body is read in
chunks with the remaining budget re-checked and re-armed on the response socket
before each chunk, so a response that trickles one byte at a time still times out
instead of running indefinitely. Alongside the deadline there is a ceiling on
generated tokens, a maximum number of attempts, and a maximum response size in
bytes - the reader stops one byte past the ceiling and refuses the body rather
than buffering whatever the provider sends.

**Redirects are never followed.** ``urllib`` replays a redirected request with
the original headers minus only the content headers, which would forward the
``Authorization`` header to whatever host the response names, and it permits an
``https`` to ``http`` downgrade. The transport therefore installs a redirect
handler that declines every 3xx, and a 3xx status is a non-retryable typed
failure - no second request is ever made.

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
import json
import secrets
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

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

#: A redirect is never followed. See :class:`_RefuseRedirects`.
_REDIRECT_MIN = 300
_REDIRECT_MAX = 400

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

    The default implementation uses :mod:`urllib.request`; tests pass a
    scripted callable. A transport raises ``TimeoutError`` for a timeout,
    ``OSError``/``urllib.error.URLError`` for a network failure and
    ``http.client.HTTPException`` for a protocol failure - it never retries, and
    it never classifies. The default transport treats ``timeout_seconds`` as a
    wall-clock deadline for the whole attempt; a custom transport is expected to
    honour the same contract.
    """

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Perform ``request`` and return the response."""
        ...


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect handler that never redirects.

    ``urllib`` follows 3xx by default, and its redirect request is built from the
    original request's headers minus only ``Content-Length`` and ``Content-Type``:
    the ``Authorization`` header - this adapter's provider credential - would be
    replayed at whatever host the response named, and an ``https`` URL would be
    allowed to become ``http``. Neither is acceptable for a call carrying a
    secret, so ``redirect_request`` always declines. Declining (rather than
    rewriting the URL) means no second transmission is attempted at all: the
    opener falls back to ``http_error_default``, which raises
    :class:`urllib.error.HTTPError` for the 3xx response, and the adapter turns
    that status into a non-retryable typed failure.
    """

    def redirect_request(  # noqa: PLR0917 - urllib's signature, called positionally
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        """Never build a follow-up request, for any status, host or scheme."""
        del req, fp, code, msg, headers, newurl


#: The one opener this adapter uses. ``build_opener`` substitutes this handler for
#: its own default redirect handler (same class hierarchy), so no other code path
#: can re-enable redirect following for provider traffic.
_OPENER = urllib.request.build_opener(_RefuseRedirects())


def _now() -> float:
    """Return the monotonic clock, indirected so the deadline is testable."""
    return time.monotonic()


def _urllib_transport(request: HttpRequest) -> HttpResponse:
    """Perform one HTTPS request under one wall-clock deadline.

    The budget is a *monotonic deadline*, not a per-socket-operation timeout: it
    is fixed before connecting, the connect and header read are bounded by it,
    and the body is read in chunks with the remaining budget re-checked and
    re-armed before every chunk. A response that trickles one byte at a time
    therefore cannot outlive the deadline - the reader notices between chunks
    and raises :class:`TimeoutError`, which the adapter reports as ``TIMEOUT``.

    Redirects are never followed (:class:`_RefuseRedirects`), and the body is read
    at most one byte past the configured ceiling so an oversized response is
    detectable without being buffered whole.
    """
    deadline = _now() + request.timeout_seconds
    http_request = urllib.request.Request(  # noqa: S310 - scheme enforced by GroqProvider
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method="POST",
    )
    try:
        with _OPENER.open(http_request, timeout=request.timeout_seconds) as response:
            body = _read_bounded(response, request.max_response_bytes, deadline)
            return HttpResponse(
                status=int(response.status),
                body=body,
                headers=dict(response.headers.items()),
            )
    except urllib.error.HTTPError as error:
        # A 4xx/5xx - and any 3xx, which is deliberately never followed - is a
        # provider answer, not a transport failure: hand its status to the
        # classifier. The body is read bounded and never parsed.
        try:
            body = _read_bounded(error, request.max_response_bytes, deadline)
        finally:
            error.close()
        return HttpResponse(
            status=int(error.code),
            body=body,
            headers=dict(error.headers.items()) if error.headers is not None else {},
        )


def _read_bounded(response: object, max_bytes: int, deadline: float) -> bytes:
    """Read a body up to ``max_bytes + 1`` bytes, or raise ``TimeoutError``.

    Before every chunk the remaining budget is recomputed from the monotonic
    clock and armed on the response's socket, so the *next* underlying read can
    never wait past the deadline, and total time is bounded by the deadline plus
    at most one short read. ``read1`` is used where available because it performs
    at most one underlying read; a plain ``read`` (all that an
    :class:`urllib.error.HTTPError` offers) is still bounded by the same budget.
    """
    read = getattr(response, "read1", None) or response.read  # type: ignore[attr-defined]
    chunks: list[bytes] = []
    budget = max_bytes + 1
    reached_eof = False
    while budget > 0:
        remaining = deadline - _now()
        if remaining <= 0:
            msg = "the provider request exceeded its wall-clock deadline"
            raise TimeoutError(msg)
        _arm_read_timeout(response, remaining)
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


def _arm_read_timeout(response: object, remaining: float) -> None:
    """Bound the response socket's next read by the remaining budget.

    The socket is reached through the documented ``fp.raw._sock`` shape of
    :class:`http.client.HTTPResponse`. When it cannot be found - an opaque
    response object - the deadline is still enforced between chunks, and the
    socket keeps the timeout it was opened with.
    """
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if isinstance(sock, socket.socket):
        sock.settimeout(remaining)


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
        transport: The HTTP seam; defaults to :func:`_urllib_transport`.
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
        self._transport: HttpTransport = transport or _urllib_transport
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
            # Never followed (see ``_RefuseRedirects``): replaying a credentialed
            # request at a host the response names is a credential leak, and an
            # https->http hop would send it in the clear. A redirect therefore
            # means the configured endpoint is not the API - a non-retryable
            # request failure, reported without naming the destination.
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
        reported = _response_model(envelope, fallback=model)
        if reported is None:
            # The response's own metadata cannot satisfy the contract's ``model``
            # field. Constructing ParseSuccess would raise instead of reporting a
            # failure, so the whole response is refused as sanitized content.
            return self._content_failure(
                "provider_metadata_invalid",
                "provider response metadata is not usable",
                latency_ms,
                model,
            )
        return ParseSuccess(
            data=data,
            strict_mode_used=False,
            usage=usage,
            latency_ms=latency_ms,
            model=reported,
            raw="",
        )

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
                model=result.model,
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


def _response_model(envelope: Mapping[str, object], *, fallback: str) -> str | None:
    """Return a contract-valid model identifier, or ``None`` if the metadata is bad.

    An *absent* identifier falls back to the configured model: that is a provider
    reporting habit, and the configured value is trusted. A *present* identifier
    that cannot be the contract's ``model`` field - not a string, blank, or longer
    than 120 characters - makes the response unusable, because building
    :class:`~rfq_agent.contracts.llm.ParseSuccess` from it would raise a
    validation error rather than report a failure.
    """
    if "model" not in envelope:
        return fallback
    value = envelope["model"]
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_IDENTIFIER_CHARS:
        return None
    return text


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
