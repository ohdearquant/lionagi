"""Run all examples that don't require LLM access.

These exercise core primitives (events, hooks, piles, casts, engines)
and complete in seconds. The orchestration examples (orchestration_flow,
orchestration_fanout) require codex and should be run individually.

    uv run python examples/run_local_examples.py
"""

from __future__ import annotations

import subprocess
import sys

SCRIPTS = [
    ("Event lifecycle", "examples/event_lifecycle.py"),
    ("Hook bus", "examples/hook_bus.py"),
    ("Pile and types", "examples/pile_and_types.py"),
    ("Casts composition", "examples/casts_composition.py"),
    ("Engine lifecycle", "examples/engine_lifecycle.py"),
]


def main():
    results: list[tuple[str, bool]] = []

    for label, script in SCRIPTS:
        proc = subprocess.run(  # noqa: S603
            ["uv", "run", "python", script],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=60,
        )
        ok = proc.returncode == 0
        last = (proc.stdout or proc.stderr).strip().rsplit("\n", 1)[-1]
        results.append((label, ok))
        print(f"  {'OK' if ok else 'FAIL'} {label}: {last}")

    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
