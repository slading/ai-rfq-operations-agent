"""F3-A1 Revision 5 fuzz harness: wrapper cross-product and credential records.

Usage:  PYTHONPATH=src:<prototype dir> python3 f3a1-fuzz-r5.py [label]

Prints one line per grid with the leak count, the number of idempotence
violations and up to two sample leaks. Synthetic dummy secret only: the grid
must never contain a real credential.
"""

from __future__ import annotations

import sys

import f3a1_prototype as m

SECRET = "hunter2024"

#: 64 wrapper shapes: four openers x four closers x four inner shapes.
OPENERS = ["{", "<", "(", "["]
CLOSERS = ["}", ">", ")", "]"]
INNER = [
    '{"value": "%s"}' % SECRET,
    "'value': '%s'" % SECRET,
    "value: %s" % SECRET,
    "value=%s" % SECRET,
]
#: 640 record shapes: 4 names x 5 values x 2 quotes x 2 separators x 8 spacings.
NAMES = ["api_key", "password", "secret", "token"]
VALUES = [SECRET, "abc", "", "12345", "x-y"]
QUOTES = ['"', "'"]
SEPS = [":", "="]
SPACES = ["", " ", "  ", "\t", "\n", "\n  ", "   ", " \t "]


def wrapper_grid() -> list[str]:
    """Every wrapper pair around every inner value shape."""
    return [f"credentials={o}{inner}{c}" for o in OPENERS for c in CLOSERS for inner in INNER]


def record_grid() -> list[str]:
    """Every quoted record shape, both field orders, every spacing."""
    texts: list[str] = []
    for name in NAMES:
        for value in VALUES:
            for q in QUOTES:
                for sep in SEPS:
                    for sp in SPACES:
                        texts.append(
                            "{"
                            + q + "name" + q + sp + sep + sp + q + name + q
                            + ", " + q + "value" + q + sp + sep + sp
                            + q + value + q
                            + "}"
                        )
    return texts


def audit(texts: list[str]) -> tuple[int, int, list[str]]:
    """Return (leaks, idempotence violations, samples) for one grid."""
    leaks = 0
    nonidem = 0
    samples: list[str] = []
    for text in texts:
        try:
            out = m.redact_text(text)
        except BaseException as exc:  # noqa: BLE001 - a crash is a failure too
            print(f"  CRASH {text!r}: {type(exc).__name__}: {exc}")
            leaks += 1
            continue
        if SECRET in out:
            leaks += 1
            if len(samples) < 2:
                samples.append(f"{text!r} -> {out!r}")
        try:
            if m.redact_text(out) != out:
                nonidem += 1
        except BaseException as exc:  # noqa: BLE001
            nonidem += 1
            print(f"  CRASH on re-run {out!r}: {type(exc).__name__}: {exc}")
    return leaks, nonidem, samples


def run(label: str, name: str, texts: list[str]) -> int:
    leaks, nonidem, samples = audit(texts)
    print(
        f"{label}: {name}: {len(texts)} shapes, {leaks} leaks, "
        f"{nonidem} idempotence violations"
    )
    for sample in samples:
        print(f"   {sample}")
    return leaks


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "unlabelled"
    total = run(label, "wrapper cross-product", wrapper_grid())
    total += run(label, "credential-record grid", record_grid())
    sys.exit(0 if total == 0 else 1)
