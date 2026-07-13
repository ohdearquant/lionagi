from __future__ import annotations

import importlib
import importlib.metadata
import platform
import sys
import time


def soft_import(module_path: str, names: list[str]) -> dict[str, object | None]:
    """Best-effort import: return {name: obj_or_None} instead of raising.

    The CI benchmark job now runs each script twice in the same job: once
    against the current lionagi and once against an older baseline install
    (same-machine A/B comparison, see benchmarks.yml). A benchmark added for
    a brand-new API will not exist in the baseline yet. A hard ImportError
    at module load would crash every scenario in the file, not just the new
    one, so callers use this to look symbols up individually and skip only
    the scenarios that need a missing one.
    """
    out: dict[str, object | None] = {}
    try:
        mod = importlib.import_module(module_path)
    except ImportError as e:
        print(
            f"[bench] WARNING: could not import {module_path} ({e}); "
            "all scenarios needing it will be skipped",
            file=sys.stderr,
        )
        return dict.fromkeys(names)
    for name in names:
        obj = getattr(mod, name, None)
        if obj is None:
            print(
                f"[bench] WARNING: {module_path}.{name} not found in this "
                "lionagi install; skipping scenarios that need it",
                file=sys.stderr,
            )
        out[name] = obj
    return out


def dep_version(distribution_name: str) -> str:
    """Installed version of a distribution, by package metadata rather than
    `module.__version__`.

    Some packages (anyio, on the release this repo currently locks) use a
    lazy `__getattr__` module loader that raises for any name it doesn't
    recognize, including `__version__` -- `getattr(module, "__version__",
    "unknown")` silently returns "unknown" in that case even though the
    version is perfectly well defined in the package's own metadata. This
    matters here because ci_check_provenance.py compares these values
    across the baseline/current arms to catch dependency-version drift; a
    field that always reads "unknown" would make that check pass trivially
    without checking anything.
    """
    try:
        return importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def lionagi_provenance() -> dict[str, str]:
    """Which lionagi install, and which interpreter, actually served this run.

    `python -m benchmarks.X` prepends the current working directory to
    sys.path. If cwd happens to contain a `lionagi/` source directory (e.g.
    the repo checkout root), `import lionagi` silently resolves there
    regardless of what is installed in the invoking interpreter's own
    site-packages -- the interpreter's venv is shadowed. This is exactly the
    failure mode that makes a same-machine A/B comparison worthless: both
    the "baseline" and "current" runs would import identical code.

    Every result JSON records this so the mistake is visible in the data
    itself, and CI can assert baseline/current disagree on lionagi_file.

    lionagi_file is expected to DIFFER between the two arms (that is the
    point of the comparison); python_full_version/python_build/
    python_compiler are the opposite -- a same-machine A/B is not actually
    comparing "the same machine" if the two arms run under different
    Python builds, and that can happen silently: a bare `uv venv` (no
    --python) can resolve a different interpreter than one pinned
    explicitly (e.g. a committed .python-version overriding a CI matrix
    version), and even two interpreters reporting the identical short
    version string can be materially different builds (an official
    PGO+LTO-optimized toolcache build vs. a generic build measurably
    differ on CPU-bound work). python_executable is a venv-local path and
    is expected to differ even when the underlying binary is identical --
    it is recorded for debugging, not asserted equal.
    """
    try:
        import lionagi as _lionagi

        lionagi_file = str(getattr(_lionagi, "__file__", "unknown"))
        lionagi_version = str(getattr(_lionagi, "__version__", "unknown"))
    except ImportError as e:
        lionagi_file = f"IMPORT FAILED: {e}"
        lionagi_version = "unknown"
    return {
        "lionagi_file": lionagi_file,
        "lionagi_version": lionagi_version,
        "python_executable": sys.executable,
        "python_full_version": sys.version,
        "python_build": " ".join(platform.python_build()),
        "python_compiler": platform.python_compiler(),
    }


def cpu_probe(iterations: int = 500_000) -> float:
    """Wall time for a fixed, code-independent CPU workload -- a canary for
    the runner's *current* effective speed, separate from anything the
    benchmark scenarios themselves measure.

    Same-machine A/B still assumes the machine's own speed is constant
    across the job. Hosted CI runners can violate that: burstable-CPU
    credit decay, thermal throttling, or a noisy neighbor can make the
    runner measurably slower (or faster) later in the job than it was
    earlier, with nothing to do with the code under test. Every result
    JSON records this fixed-workload timing so that kind of drift is
    directly visible in the artifacts (compare probe values across chunks/
    arms) instead of only inferable after a compare gate fails for no
    code-level reason. The iteration count is intentionally simple integer
    arithmetic with no allocation-heavy or I/O-bound behavior, so it
    isolates raw CPU throughput.

    500k iterations costs ~0.05s on the hardware this was calibrated on --
    a first pass used 20_000_000 (~13-20s measured locally) on the
    assumption that a tight integer loop is negligible; it measurably is
    not, and at that cost this probe would have added minutes across the
    ~50 chunk invocations one CI run now makes. This count keeps the added
    overhead low while still running long enough (tens of milliseconds) to
    rise well above typical timer/scheduling jitter.
    """
    t0 = time.perf_counter()
    x = 0
    for _ in range(iterations):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
    return time.perf_counter() - t0
