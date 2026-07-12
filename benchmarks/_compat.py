from __future__ import annotations

import importlib
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
