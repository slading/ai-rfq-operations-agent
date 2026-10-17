"""Extended verification beyond the 89-test QA gate (F3-A1 revisions 2-4).

Unchanged across revisions 3 and 4: the 101 assertions are re-run verbatim.
"""
from __future__ import annotations

import json
import random
import time
from decimal import Decimal

from pydantic import BaseModel, ValidationError

from rfq_agent.observability.redaction import (
    REDACTED,
    redact_mapping,
    redact_payload,
    redact_text,
    summarize_payload,
)

QA_SECRET = "gsk_FAKEsupersecret0123456789"
S = QA_SECRET
SHORT = "hunter2"
EMAIL = "jan.kowalski@example.com"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def leaks(text: object, needle: str = S) -> bool:
    return needle in str(text)


# ---------------------------------------------------------------- C-1 perf
for label, text in {
    "key-run": "key" * 16_000 + "=",
    "dash-key": "a-" * 16_000 + "key",
    "email-dots": "a.b" * 16_000,
    "email-digits": "a1." * 16_000 + "@b.",
    "at-run": "@" * 48_000,
    "dot-run": "." * 48_000,
    "quote-run": '"' * 48_000,
    "brace-run": "{" * 48_000,
    "backslash-run": "\\u005f" * 8_000,
    "eq-run": "a=" * 24_000,
}.items():
    t0 = time.perf_counter()
    redact_text(text)
    dt = time.perf_counter() - t0
    check(f"perf:{label}", dt < 0.2, f"{dt*1000:.1f} ms")

t0 = time.perf_counter(); redact_text("The quick brown fox. " * 50_000)
check("perf:1MB prose", time.perf_counter() - t0 < 0.5, f"{(time.perf_counter()-t0)*1000:.0f} ms")

# ------------------------------------------------------------ C-2 short values
for text in ["password=hunter2", "password: hunter2", "passwd=secret99",
             "api_key=abc12345", "token=x", "secret=1", "aws_secret_access_key=Zq7Xk2",
             "Authorization: Basic dXNlcjpwYXNz", "private_key=abc"]:
    value = text.split("=", 1)[-1].split(":", 1)[-1].strip()
    check(f"short:{text[:34]}", value not in redact_text(text), repr(redact_text(text)))

# ------------------------------------------------------------ C-3 unicode / NFKC
for text in ["api_key\uff1d" + S, "api_key\uff1a" + S, "\uff41\uff50\uff49_\uff4b\uff45\uff59=" + S]:
    check(f"nfkc:{text[:22]!r}", not leaks(redact_text(text)))
for addr in ["kontakt@przyk\u0142ad.pl", "\u017c\u00f3\u0142w@example.com", '"john doe"@example.com']:
    check(f"idn:{addr}", addr not in redact_text(f"from {addr} today"))
for suffix in ["_", "\u017c", "0", "-x"]:
    check(f"email-suffix:{suffix}", EMAIL not in redact_text(f"{EMAIL}{suffix}"))
BENIGN_UNICODE = [
    "Za\u017c\u00f3\u0142\u0107 g\u0119\u015bl\u0105 ja\u017a\u0144",
    "Warszawa, ul. \u017b\u00f3rawia 15",
    "40 units of X-120 to Warsaw by Friday",
    "The quick brown fox jumps over the lazy dog.",
]
for text in BENIGN_UNICODE:
    check(f"nfkc-benign:{text[:24]}", redact_text(text) == text, repr(redact_text(text)))

# ------------------------------------------------- C-4 aws key + name/value
check("struct:aws", redact_mapping({"aws_secret_access_key": SHORT})["aws_secret_access_key"] == REDACTED)
check("struct:name-value", not leaks(redact_mapping({"headers": [{"name": "api_key", "value": SHORT}]}), SHORT))
check("struct:name-value-nested", not leaks(redact_mapping({"h": {"name": "authorization", "value": "Basic abc"}}), "abc"))
check("struct:key-value-benign", redact_mapping({"name": "model", "value": "gpt"}) == {"name": "model", "value": "gpt"})

# --------------------------------------------------------- C-5 bearer / header
check("bearer:business", redact_text("Bearer Instrument-2026-A") == "Bearer Instrument-2026-A")
check("bearer:secret", SHORT not in redact_text(f"Bearer {SHORT}"))
check("bearer:gsk", S not in redact_text(f"used bearer {S} for the call"))
check("header:bounded", "PO-2026-00123" in redact_text("token: 40 units of X-120 to Warsaw by Friday, PO-2026-00123"))
check("header:eol", redact_text(f"Authorization: Basic {S}") == f"Authorization: {REDACTED}")
check("header:both-secrets", S not in redact_text(f"api_key={S} and also token={S}"))

# ------------------------------------------------------- C-6 validation errors
class _IntOnly(BaseModel):
    requests_per_minute_budget: int

for secret in (QA_SECRET, SHORT, "correct-horse-battery"):
    try:
        _IntOnly(requests_per_minute_budget=secret)
    except ValidationError as exc:
        check(f"verr:text:{secret[:14]}", not leaks(redact_text(str(exc)), secret))
        check(f"verr:map:{secret[:14]}", not leaks(redact_mapping({"errors": exc.errors()}), secret))

# --------------------------------------------------------------- C-8 escaped keys
check("escaped:underscore", not leaks(redact_text('{"api\\u005fkey": "%s"}' % S)))
check("escaped:password", not leaks(redact_text('{"pass\\u0077ord": "%s"}' % S)))
check("escaped:structural", not leaks(redact_mapping({"raw": '{"api\\u005fkey": "%s"}' % S})))
check("escaped:benign", redact_text('{"model": "x"}') == '{"model": "x"}')

# --------------------------------------------------------------- max_bytes
for bad in (0, -1, True, 1.5, "128", None):
    try:
        summarize_payload({"a": 1}, max_bytes=bad)
        check(f"max_bytes:reject:{bad!r}", False)
    except ValueError:
        check(f"max_bytes:reject:{bad!r}", True)
check("max_bytes:one", summarize_payload({"a": "x" * 100}, max_bytes=1)["payload_status"] == "TRUNCATED")

# --------------------------------------------------------------- structural
checks = [
    ("struct:json-pair", redact_mapping({"h": [["api_key", SHORT]]})["h"][0][1] == REDACTED),
    ("struct:tuple-pair", redact_payload(("api_key", S)) == [REDACTED, REDACTED]),
    ("struct:set", not leaks(redact_mapping({"s": {f"token={S}"}}))),
    ("struct:bytes", not leaks(redact_mapping({"b": f"token={S}".encode()}))),
    ("struct:bytearray", not leaks(redact_mapping({"b": bytearray(f"token={S}".encode())}))),
    ("struct:int-key", not leaks(redact_mapping({1: f"api_key={S}"}))),
    ("struct:tuple-key", not leaks(redact_mapping({("a",): f"api_key={S}"}))),
    ("struct:secret-as-key", not leaks(redact_mapping({f"api_key={S}": 1}))),
    ("struct:email-as-key", not leaks(redact_mapping({EMAIL: 2}), EMAIL)),
    ("struct:benign", redact_mapping({"count": 3, "ok": True, "nothing": None}) == {"count": 3, "ok": True, "nothing": None}),
    ("struct:scalar-roundtrip", redact_payload(Decimal("12.34")) == Decimal("12.34")),
]
for name, cond in checks:
    check(name, cond)

cyc = {"api_key": S}; cyc["self"] = cyc
check("struct:cycle", not leaks(redact_payload(cyc)))
check("struct:cycle-summary", summarize_payload(cyc, max_bytes=64)["payload_status"] == "WITHHELD")
deep = cur = {}
for _ in range(100):
    cur["n"] = {}; cur = cur["n"]
check("struct:depth", not leaks(redact_payload({"api_key": S, "d": deep})))

class _Hostile:
    def __str__(self) -> str:
        raise RuntimeError("boom")

check("struct:hostile-str", redact_mapping({"x": _Hostile()})["x"] == REDACTED)

# --------------------------------------------------------------- summaries
check("summary:prefix", S[:8] not in str(summarize_payload({"a_token": S, "zz": "x"*4000}, max_bytes=len('{"a_token":"')+16)))
check("summary:list", not leaks(summarize_payload([{"api_key": S}], max_bytes=4096)))
check("summary:bytes", len(summarize_payload({"body": "\U0001f600"*500}, max_bytes=64)["payload"].encode()) <= 64)
check("summary:digest", summarize_payload({"q": "X-120"}, max_bytes=1024)["payload_sha256"] ==
      __import__("hashlib").sha256(json.dumps({"q": "X-120"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
check("summary:surrogate", not leaks(summarize_payload({"body": "ab\udcffcd", "token": S}, max_bytes=100)))
check("summary:inline-shape", isinstance(summarize_payload({"q": 1}, max_bytes=99)["payload"], dict))

# --------------------------------------------------------------- false positives
FP = [
    "idempotency_key=ORD-2026-000123", "primary_key=RFQ-2026-000042",
    "Keyboard=KB-2026-BLACK-US", "Turkey: ANKARA-WAREHOUSE-01",
    "token_count: 1234567890123", "Bearer Instrument-2026-A",
    "the key: Warsaw", "monkey: bananas!", "max_tokens: 7000",
    "tokenizer: abcdefghijkl", "secretary: abcdefghijkl",
    "public_key: abcdefghijkl", "hockey: abcdefghijkl",
    "https://api.groq.com/openai/v1", "sqlite:///var/rfq_agent.db",
    "openai/gpt-oss-120b", "RUN-0001", "TRACE-" + "a" * 32,
]
for text in FP:
    check(f"fp:{text[:30]}", redact_text(text) == text, repr(redact_text(text)))

# --------------------------------------------------------------- idempotence
corpus = FP + [f"password={S}", f"Authorization: Basic {S}", f"Bearer {S}",
               '{"api_key": "%s"}' % S, f"postgresql://u:{S}@h/db", EMAIL,
               f"api_key\uff1d{S}", f"token,'{S}'", f"private_key=-----BEGIN X-----\n{S}\n-----END X-----"]
rng = random.Random(7)
_KEYS = ["api_key", "token", "password", "key", "secret"]
_SEPS = ["=", ": ", ",", "='", '": "']
_TAILS = ["", "'", '"', ">"]
for _ in range(300):
    corpus.append(rng.choice(_KEYS) + rng.choice(_SEPS) + S + rng.choice(_TAILS))
bad = [t for t in corpus if redact_text(redact_text(t)) != redact_text(t)]
check("idem:corpus", not bad, repr(bad[:3]))

# --------------------------------------------------------------- report
passed = sum(1 for _, ok, _ in results if ok)
failed = [r for r in results if not r[1]]
print(f"EXTENDED MATRIX: {passed}/{len(results)} passed")
for name, _, detail in failed:
    print("  FAIL", name, detail)
