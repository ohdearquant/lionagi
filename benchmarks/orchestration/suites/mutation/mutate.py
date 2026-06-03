"""Mutation suite — manufacture ground truth by planting known defects.

The benchmark insight: you don't hand-label every run, you MANUFACTURE the
key. Take a clean source file, plant exactly one known defect → a task whose
correct answer is known. Keep a clean copy whose only flag-bait is *intended*
behavior → a task where any Medium+ finding is a measurable false positive.

This module builds two tasks over ``lionagi/hooks/bus.py``:

- ``bus_break_to_return`` (DEFECT): in ``blocking_emit`` the StopHook handler
  uses ``break`` so ``await self._record(...)`` still runs after the loop.
  Mutating ``break`` → ``return`` makes the short-circuit skip ``_record`` —
  a real regression (the HookSignal is silently dropped on short-circuit). A
  good reviewer catches it; auditor-2 already did, so the answer is known.

- ``bus_clean_intended`` (NO DEFECT): the unmodified file. The flag-bait is
  that ``_record`` lets ``BaseException`` (cancellation) propagate — which is
  the INTENDED totality contract, not a leak. Rating it Medium+ is the
  false-positive failure we observed the default chain commit.
"""

from __future__ import annotations

from pathlib import Path

# Repo root = .../lionagi (four parents up from this file)
ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "lionagi" / "hooks" / "bus.py"
TASKS_DIR = Path(__file__).resolve().parent / "tasks"

# The exact mutation: the StopHook break in blocking_emit → return.
# Anchored on the unique comment so we hit blocking_emit, not emit.
#
# CRITICAL (audit F1): the mutated comment must NOT announce the defect. A
# comment like "# BUG: skips _record" turns defect DETECTION into comment
# READING — every agent trivially echoes it and recall saturates at 1.0. The
# replacement reads like plausible intended code; the regression (the post-loop
# `await self._record(...)` is skipped on short-circuit) is detectable ONLY by
# reasoning about control flow, which is what we want to measure.
_ANCHOR = "            except StopHook:\n                break  # stop remaining handlers, but still record below"
_MUTATED = (
    "            except StopHook:\n                return  # short-circuit: stop remaining handlers"
)


def build_tasks() -> list[dict]:
    """Materialize mutant + clean files under tasks/ and return task specs."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    clean_src = SOURCE.read_text(encoding="utf-8")

    # The anchor appears in BOTH blocking_emit and emit. We mutate only the
    # FIRST occurrence (blocking_emit, which comes first in the file).
    if _ANCHOR not in clean_src:
        raise RuntimeError(
            "mutation anchor not found in bus.py — the source changed; "
            "update mutate.py's _ANCHOR to match the current StopHook handler."
        )
    mutant_src = clean_src.replace(_ANCHOR, _MUTATED, 1)
    assert mutant_src != clean_src, "mutation produced no change"

    clean_path = TASKS_DIR / "bus_clean.py"
    mutant_path = TASKS_DIR / "bus_mutant_break_to_return.py"
    clean_path.write_text(clean_src, encoding="utf-8")
    mutant_path.write_text(mutant_src, encoding="utf-8")

    return [
        {
            "id": "bus_break_to_return",
            "file": str(mutant_path),
            "labels": [
                {
                    "kind": "defect",
                    "location": "blocking_emit StopHook handler",
                    "summary": (
                        "StopHook short-circuit uses `return` instead of `break`, so "
                        "`await self._record(...)` after the loop is skipped — the "
                        "HookSignal is silently dropped when a handler short-circuits "
                        "AND an observer is bound (the intended production path; with "
                        "no observer `_record` is a no-op so behaviour is unchanged)."
                    ),
                    "true_severity": "medium",
                },
            ],
        },
        {
            "id": "bus_clean_intended",
            "file": str(clean_path),
            "labels": [
                {
                    "kind": "intended",
                    "location": "_record / emit / blocking_emit BaseException boundary",
                    "summary": (
                        "_record catches `except Exception` and lets BaseException "
                        "(asyncio cancellation) propagate. This is the INTENDED totality "
                        "contract, not a leak. Flagging it as a Medium+ defect is a false positive."
                    ),
                    "true_severity": "none",
                },
            ],
        },
    ]


if __name__ == "__main__":
    import json

    tasks = build_tasks()
    print(json.dumps(tasks, indent=2))
    print(f"\nWrote tasks to {TASKS_DIR}")
