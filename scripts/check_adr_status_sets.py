"""Check that enumerated status sets in docs/adr/ match the lifecycle policy registry.

Scope is deliberately narrow: only status vocabularies and terminal-status sets,
not general ADR prose. Two shapes are checked:

- Python code-block assignments like ``SCHEDULE_RUN_TERMINAL_STATUSES = frozenset({...})``
  or ``VALID_SESSION_STATUSES = frozenset({...})``.
- Wait-surface-style markdown tables with a "Terminal statuses" column and rows
  like ``| `schedule_run` | `completed`, `failed`, ... | ... |``.

A symbol/row is compared against ``lionagi.state.lifecycle.policy.DEFAULT_REGISTRY``
only when its name/kind resolves to a known entity_type; unrecognized symbols are
skipped rather than treated as errors. Usage: ``uv run scripts/check_adr_status_sets.py``.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from lionagi.state.lifecycle.policy import DEFAULT_REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ADR_DIR = REPO_ROOT / "docs" / "adr"

_CODE_BLOCK_RE = re.compile(r"```(?:python|py)\n(.*?)```", re.S)
_ASSIGNMENT_RE = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*frozenset\(\{(.*?)\}\)", re.S)
_QUOTED_STATUS_RE = re.compile(r'"([A-Za-z0-9_]+)"')
_BACKTICK_STATUS_RE = re.compile(r"`([a-z0-9_]+)`")
_BACKTICK_LIST_RE = re.compile(r"`[a-z0-9_]+`(?:,\s*`[a-z0-9_]+`)*")


@dataclass(frozen=True)
class Finding:
    file: Path
    label: str
    line: int
    kind: str  # "terminal" or "vocab"
    entity_type: str
    doc_statuses: frozenset[str]


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _resolve_symbol(name: str) -> tuple[str, str] | None:
    """Map a Python symbol name to (category, entity_type), or None if unrecognized."""
    if name.endswith("_TERMINAL_STATUSES"):
        core = name[: -len("_TERMINAL_STATUSES")]
        return "terminal", core.lower()
    if name.endswith("_STATUSES"):
        core = name[: -len("_STATUSES")]
        if core.startswith("VALID_"):
            core = core[len("VALID_") :]
        return "vocab", core.lower()
    return None


def _iter_python_findings(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for block in _CODE_BLOCK_RE.finditer(text):
        block_text = block.group(1)
        block_offset = block.start(1)
        for match in _ASSIGNMENT_RE.finditer(block_text):
            name, body = match.group(1), match.group(2)
            resolved = _resolve_symbol(name)
            if resolved is None:
                continue
            kind, entity_type = resolved
            if entity_type not in DEFAULT_REGISTRY:
                continue
            statuses = frozenset(_QUOTED_STATUS_RE.findall(body))
            if not statuses:
                continue
            line = _line_of(text, block_offset + match.start(1))
            findings.append(Finding(path, name, line, kind, entity_type, statuses))
    return findings


def _iter_table_findings(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.split("\n")
    terminal_col: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            terminal_col = None
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if terminal_col is None:
            # Candidate header row: look for a column mentioning "terminal".
            header_col = next((i for i, c in enumerate(cells) if "terminal" in c.lower()), None)
            if header_col is None:
                continue
            # Next non-blank line must be a markdown table separator.
            if idx + 1 >= len(lines) or not re.fullmatch(r"\|?[\s:|-]+\|?", lines[idx + 1].strip()):
                continue
            terminal_col = header_col
            continue
        if terminal_col >= len(cells):
            continue
        kind_match = re.fullmatch(r"`([a-z0-9_]+)`", cells[0])
        if kind_match is None:
            continue
        entity_type = kind_match.group(1)
        if entity_type not in DEFAULT_REGISTRY:
            continue
        cell = cells[terminal_col]
        # Only treat the cell as an enumerated status list when it is nothing
        # but a comma-separated run of backtick-quoted statuses; prose like
        # "all except `running`" or "same as Session" is not an enumeration
        # of the set and is intentionally skipped, not flagged.
        if _BACKTICK_LIST_RE.fullmatch(cell) is None:
            continue
        statuses = frozenset(_BACKTICK_STATUS_RE.findall(cell))
        if not statuses:
            continue
        findings.append(
            Finding(path, f"table row `{entity_type}`", idx + 1, "terminal", entity_type, statuses)
        )
    return findings


def check_file(path: Path) -> list[str]:
    text = path.read_text()
    errors: list[str] = []
    for finding in _iter_python_findings(path, text) + _iter_table_findings(path, text):
        policy = DEFAULT_REGISTRY.get(finding.entity_type)
        registry_statuses = (
            policy.terminal_statuses if finding.kind == "terminal" else policy.statuses
        )
        if finding.doc_statuses == registry_statuses:
            continue
        missing = sorted(registry_statuses - finding.doc_statuses)
        extra = sorted(finding.doc_statuses - registry_statuses)
        errors.append(
            f"{finding.file}:{finding.line}: {finding.label} ({finding.kind} statuses for "
            f"entity_type={finding.entity_type!r}) does not match "
            f"lionagi.state.lifecycle.policy.DEFAULT_REGISTRY\n"
            f"  missing from doc: {missing}\n"
            f"  extra in doc:     {extra}"
        )
    return errors


def check_paths(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            files = sorted(path.rglob("*.md"))
        else:
            files = [path]
        for file in files:
            errors.extend(check_file(file))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[DEFAULT_ADR_DIR],
        help="Markdown files or directories to check (default: docs/adr/)",
    )
    args = parser.parse_args(argv)

    errors = check_paths(args.paths)
    if errors:
        print("ADR status-set check FAILED:\n", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
            print(file=sys.stderr)
        return 1

    print("ADR status-set check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
