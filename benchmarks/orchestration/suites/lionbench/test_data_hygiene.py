"""Publication-hygiene gate over every committed instance/rejection JSON.

A round-1 review of the first three harvested instances found local worktree
paths, an internal actor identifier, an internal issue-tracker comment, and a
test-generated session UUID leaking into fields outside ``task_text`` —
``provenance.nominated_by``, ``validation.gold_output``/``null_output``, and
``oracle.test_patch``. ``save_instance`` now redacts the first two at write
time (see ``schema._redact_for_publication``), but this repo is public and a
future harvest run (or a hand-edited fixture) could reintroduce any of these —
so every scalar field of every committed JSON under ``data/`` is walked here
and checked against the concrete leak patterns from that review.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_HERE = Path(__file__).resolve().parent
DATA_DIR = _HERE / "data"

_BANNED_SUBSTRINGS = (
    "/Users/",
    "/private/var",
    ".claude",
    "swebench-work",
    "_lionbench_wt_",
    "private repo",
)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Internal actor identifiers look like "lambda:leo"/"lambda:khive" — no space after
# the colon. A bare "lambda:" substring also matches Python's own zero-arg lambda
# syntax ("lambda: mock_db"), which is unavoidable in harvested test_patch content
# (any repo with mocked callables uses it) and is not a leak. ruff-formatted Python
# always inserts a space after the colon in that syntax, so "not followed by
# whitespace" cleanly separates the two shapes.
_LAMBDA_ACTOR_RE = re.compile(r"lambda:(?!\s)")


def _iter_scalars(obj, path: str = "$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_scalars(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_scalars(v, f"{path}[{i}]")
    else:
        yield path, obj


def _committed_json_files() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    return sorted(DATA_DIR.rglob("*.json"))


def _violations_in(path: Path, doc: dict) -> list[str]:
    violations = []
    for field_path, value in _iter_scalars(doc):
        if not isinstance(value, str):
            continue
        for bad in _BANNED_SUBSTRINGS:
            if bad in value:
                violations.append(f"{path}:{field_path} contains {bad!r}")
        if _LAMBDA_ACTOR_RE.search(value):
            violations.append(f"{path}:{field_path} contains an actor-identifier-shaped 'lambda:'")
        m = _UUID_RE.search(value)
        if m:
            violations.append(f"{path}:{field_path} contains a UUID-shaped token ({m.group(0)})")
    return violations


def test_no_committed_data_json_leaks_local_or_internal_identifiers():
    files = _committed_json_files()
    assert files, "expected at least one committed instance/manifest JSON under data/"
    violations: list[str] = []
    for path in files:
        doc = json.loads(path.read_text())
        violations.extend(_violations_in(path, doc))
    assert not violations, "publication-hygiene leak(s):\n" + "\n".join(violations)


def test_hygiene_check_catches_a_known_leak_shape():
    """Sanity-check the checker itself against the exact leak shapes it exists
    to catch — a silently-broken checker is worse than no checker."""
    assert _violations_in(Path("fixture.json"), {"provenance": {"nominated_by": "lambda:x"}})
    assert _violations_in(Path("fixture.json"), {"validation": {"gold_output": "/Users/x/y"}})
    assert _violations_in(
        Path("fixture.json"), {"oracle": {"test_patch": "id dd2cf083-d265-4c14-98a0-186007304bc1"}}
    )
    assert _violations_in(Path("fixture.json"), {"provenance": {"why": "issue in private repo"}})
    assert not _violations_in(Path("fixture.json"), {"task_text": "totally clean text"})
