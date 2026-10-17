"""Reproduce F3-A1 security evidence against production, without a test shim.

Usage: python tools/verify_f3a1.py --review-root /path/to/pinned/review/checkout
Optional: --benchmark --full. Only synthetic secrets are printed.
"""

# CLI output, fixed subprocess argv, and numeric security budgets are intentional.
# ruff: noqa: T201, S603, S607, PLR2004

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median
from time import perf_counter
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rfq_agent.observability import redaction as production  # noqa: E402

REVIEW_SHA = "66de32c55994144c21df4a9af8b3d1edae1635ad"
BASELINE = "d5611eefab3b326e9ad5caa9789d892e69ce3875"
GATE_SHA256 = "766c9b7dbae521ded06d167e43db914eb198bd2fa84d7c4f4f3fe5ce8692b44f"
REVIEW_BLOBS = {
    "revision-5/f3a1-design-revision-r5.md": "46b7dc6b6061478d703aa1540ee04757c8a381b4",
    "revision-5/f3a1-prototype-r5.py": "c297875731e8eb9d3d8ad929a22c57c82b120d6a",
    "revision-5/f3a1-regression-r5.py": "0bb619839b489ac6431c0cef9d9cd99710d37f58",
    "revision-5/f3a1-fuzz-r5.py": "44a1b17f9f3859743c1da7f45a2054727636ee56",
    "revision-5/f3a1-qa-coverage-matrix-r5.md": "f2ebcff2aa78e6c44e7590c8a74eef64b0bd6dac",
    "revision-5/f3a1-r5-evidence.txt": "1a4da3ac97708bd6c88512d7be42a00d97dd2075",
    "revision-4/f3a1-prototype-r4.py": "109b21fd81f5c0f53041d6ba866ef218328da5a4",
    "revision-4/f3a1-regression-r4.py": "9ba7c84b915861ec6923ca8a92b3aa60796fb4db",
    "revision-4/f3a1-extended-matrix.py": "f69e7dbe84979ea081fdf936973288309ab16054",
    "revision-3/f3a1-regression-r3.py": "4d59f93fbc430ff1b04540271ef8556cbac7123f",
}


def load_module(path: Path, name: str) -> ModuleType:
    """Load a pinned local reference for comparisons, never install a shim."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_provenance(review: Path) -> None:
    """Fail if any supplied review artifact differs from the pinned blob."""
    for path, expected in REVIEW_BLOBS.items():
        data = (review / path).read_bytes()
        actual = hashlib.sha1(  # noqa: S324 - Git object identity, not authentication
            b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        assert actual == expected, (path, actual, expected)
    gate = (ROOT / "tests/unit/test_redaction_adversarial.py").read_bytes()
    assert hashlib.sha256(gate).hexdigest() == GATE_SHA256
    assert (ROOT / "tests/unit/test_redaction_regression.py").read_bytes() == (
        review / "revision-5/f3a1-regression-r5.py"
    ).read_bytes()
    print(f"PROVENANCE: {len(REVIEW_BLOBS)} pinned review blobs + verbatim gate/regression")
    print(f"SOURCE: production module {production.__file__}; review {REVIEW_SHA}", flush=True)


def reproduce(review: Path) -> None:
    """Compare baseline, revision 4, and production with dummy credentials."""
    baseline_source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:src/rfq_agent/observability/redaction.py"],
        cwd=ROOT,
        text=True,
    )
    baseline = ModuleType("f3a1_baseline")
    exec(compile(baseline_source, BASELINE, "exec"), baseline.__dict__)  # noqa: S102
    r4 = load_module(review / "revision-4/f3a1-prototype-r4.py", "f3a1_r4")
    for label, module in [("baseline", baseline), ("revision4", r4), ("production", production)]:
        for text in [
            'credentials=<{"value": "hunter2024"}>',
            '{"name": "api_key", "value": "hunter2024"}',
            '{"value": "hunter2024", "name": "api_key"}',
        ]:
            print(f"REPRO {label}: {text!r} -> {module.redact_text(text)!r}")


def run_review_checks(review: Path) -> None:
    """Run the original matrix and strengthen the fuzz harness's assertions."""
    matrix = runpy.run_path(str(review / "revision-4/f3a1-extended-matrix.py"))
    assert len(matrix["results"]) == 101
    assert all(ok for _, ok, _ in matrix["results"])
    # The harness imports an old prototype name. Alias that name to the real
    # production module in this runner only; no production names are replaced.
    sys.modules["f3a1_prototype"] = production
    fuzz = runpy.run_path(str(review / "revision-5/f3a1-fuzz-r5.py"))
    for name, texts in [("wrapper", fuzz["wrapper_grid"]()), ("record", fuzz["record_grid"]())]:
        leaks, nonidem, samples = fuzz["audit"](texts)
        print(f"FUZZ {name}: {len(texts)} shapes; leaks={leaks}; nonidem={nonidem}")
        assert leaks == 0, samples
        assert nonidem == 0
    # The published harness checks only hunter2024. Check every nonempty
    # generated secret as well, including both orders, with unique markers.
    count = 0
    for name in fuzz["NAMES"]:
        for shape in ["text", "object", "array"]:
            for reverse in [False, True]:
                for spacing in fuzz["SPACES"]:
                    secret = f"F3aDummy{count:06d}"
                    value: Any = secret
                    if shape == "object":
                        value = {"leaf": secret}
                    elif shape == "array":
                        value = ["decoy", secret]
                    items = [("name", name), ("value", value)]
                    if reverse:
                        items.reverse()
                    text = json.dumps(dict(items), separators=("," + spacing, ":" + spacing))
                    out = production.redact_text(text)
                    assert secret not in out
                    assert production.redact_text(out) == out
                    count += 1
    print(f"FUZZ expanded records: {count} shapes; 0 leaks; 0 idempotence failures", flush=True)


def benchmark(review: Path) -> None:
    """Measure five interleaved samples per variant, with explicit budgets."""
    variants = {
        "r4": load_module(review / "revision-4/f3a1-prototype-r4.py", "f3a1_bench_r4"),
        "r5": load_module(review / "revision-5/f3a1-prototype-r5.py", "f3a1_bench_r5"),
        "production": production,
    }
    cases = {
        "48KB key-run": ("key" * 16000 + "=", 0.2),
        "32KB dash-key": ("a-" * 16000 + "key", 0.2),
        "48KB email-dots": ("a.b" * 16000, 0.2),
        "48KB backslash-run": ("\\u005f" * 8000, 0.2),
        "1MB prose": ("The quick brown fox. " * 50000, 0.5),
        "1MB a=": ("a=" * 500000, 0.5),
        "password x5000": ("password=hunter2024 " * 5000, 0.2),
        "48KB BEGIN": ("-----BEGIN A-----" * (48000 // 17), 0.2),
        "1MB BEGIN": ("-----BEGIN A-----" * (1000000 // 17), 0.5),
        "4MB BEGIN": ("-----BEGIN A-----" * (4000000 // 17), 2.0),
        "5000 containers": ('credentials={"v":"hunter2024"} ' * 5000, 0.2),
        "5000 unterminated": ('credentials={"v":"hunter2024" ' * 5000, 0.2),
        "5000 YAML blocks": ("credentials:\n  value: hunter2024\nnext: ok\n" * 5000, 0.2),
        "90KB records": ('{"name":"api_key","value":"hunter2024"} ' * 2000, 0.2),
        "1MB name/value prose": ("an ordinary name and value. " * 40000, 0.5),
    }
    print("PERFORMANCE milliseconds (min / median of 5 interleaved samples)")
    print("case | r4 | r5 | production | budget")
    for label, (text, budget) in cases.items():
        samples: dict[str, list[float]] = {name: [] for name in variants}
        for _ in range(5):
            for name, module in variants.items():
                start = perf_counter()
                module.redact_text(text)
                samples[name].append(perf_counter() - start)
        cells = [f"{min(t) * 1000:.2f} / {median(t) * 1000:.2f}" for t in samples.values()]
        print(f"{label} | {' | '.join(cells)} | {budget * 1000:.0f}", flush=True)
        assert median(samples["production"]) < budget, label
    for size in [48000, 1000000]:
        payload = {"body": "-----BEGIN A-----" * (size // 17)}
        samples = []
        for _ in range(5):
            start = perf_counter()
            production.summarize_payload(payload, max_bytes=512)
            samples.append(perf_counter() - start)
        print(
            f"summary {size}: min={min(samples) * 1000:.2f}, median={median(samples) * 1000:.2f} ms"
        )
        assert median(samples) < 1.0


def falsifiability(review: Path) -> None:
    """Diagnose the old reference in a child process with its published shim.

    This intentionally failing run is separate from production verification.
    No production function is replaced in this process or written to disk.
    """
    with tempfile.TemporaryDirectory(prefix="f3a1-reference-") as directory:
        shim = Path(directory)
        shutil.copy2(review / "revision-4/f3a1-prototype-r4.py", shim / "f3a1_prototype.py")
        shutil.copy2(review / "revision-4/f3a1-test-shim.py", shim / "sitecustomize.py")
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(shim),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                str(ROOT / "pyproject.toml"),
                str(review / "revision-5/f3a1-regression-r5.py"),
                "-q",
                "--tb=no",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    print("REFERENCE DIAGNOSTIC ONLY: published revision-4 shim; expected failures")
    print(result.stdout)
    assert result.returncode == 1
    assert "40 failed, 238 passed" in result.stdout


def main() -> None:
    """Run pinned security checks; optional full suite and benchmarks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--falsifiability", action="store_true")
    args = parser.parse_args()
    review = args.review_root.resolve() / "docs/reviews/f3a"
    check_provenance(review)
    reproduce(review)
    run_review_checks(review)
    command = [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"), "-q"]
    command += [str(review / f"revision-{n}/f3a1-regression-r{n}.py") for n in [3, 4, 5]]
    subprocess.run(command, cwd=ROOT, check=True)
    paths = (
        ["tests"]
        if args.full
        else [
            "tests/unit/test_redaction_adversarial.py",
            "tests/unit/test_redaction_regression.py",
            "tests/unit/test_redaction_production.py",
            "tests/unit/test_observability.py",
        ]
    )
    subprocess.run([sys.executable, "-m", "pytest", "-q", *paths], cwd=ROOT, check=True)
    if args.benchmark:
        benchmark(review)
    if args.falsifiability:
        falsifiability(review)


if __name__ == "__main__":
    main()
