from __future__ import annotations

import importlib
import importlib.metadata
import sys


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
    """Which lionagi install actually served this run.

    `python -m benchmarks.X` prepends the current working directory to
    sys.path. If cwd happens to contain a `lionagi/` source directory (e.g.
    the repo checkout root), `import lionagi` silently resolves there
    regardless of what is installed in the invoking interpreter's own
    site-packages -- the interpreter's venv is shadowed. This is exactly the
    failure mode that makes a same-machine A/B comparison worthless: both
    the "baseline" and "current" runs would import identical code.

    Every result JSON records this so the mistake is visible in the data
    itself, and CI can assert baseline/current disagree on lionagi_file.
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
    }
