#!/usr/bin/env python3
"""Content-level lint for marketplace .md files.

Catches Lion-internal leakage: khive paths, LION identity symbols,
deprecated verb syntax, nonexistent CLI commands, and stale model names.

Usage:
    uv run python marketplace/scripts/lint_skills.py [path ...]

Exit 0 if clean, exit 1 if any findings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Rule sets
# ---------------------------------------------------------------------------

# (pattern, description)
FORBIDDEN_PATHS: list[tuple[str, str]] = [
    (r"\.khive/", "khive workspace path (.khive/)"),
    (r"khive-work/", "Lion-internal show directory (khive-work/)"),
    (r"\bfirm/", "private firm repo reference (firm/)"),
    (r"/Users/\w+/", "hardcoded home directory path (/Users/<name>/)"),
]

FORBIDDEN_SYMBOLS: list[tuple[str, str]] = [
    (r"∵α\[", "LION agent identity prefix (∵α[)"),
    (r"→LION\.", "LION affiliation marker (→LION.)"),
    (r"\bkpp\s+format", "internal .kpp format reference"),
    (r"\bplan\.kpp\b", "internal plan.kpp reference"),
]

DEPRECATED_PATTERNS: list[tuple[str, str]] = [
    (r"mcp__khive__\w+\s*\(action=", "deprecated service.action() dispatch (action= kwarg)"),
    (r"\bmemory\.recall\(", "bare Python method syntax (memory.recall())"),
    (r"\bwork\.tasks\(", "bare Python method syntax (work.tasks())"),
    (r"\bforget_batch\b", "nonexistent verb (forget_batch)"),
    (r"\bmcp__khive__graph\b", "nonexistent verb (mcp__khive__graph)"),
    (r"\bmcp__khive__waves\b", "nonexistent verb (mcp__khive__waves)"),
    (r"\bmcp__khive__work\b", "nonexistent verb (mcp__khive__work)"),
    (r"\bmcp__khive__communication\b", "nonexistent verb (mcp__khive__communication)"),
]

# Only matched in .md files (not plugin.json where Ocean is an author name)
OCEAN_PATTERNS: list[tuple[str, str]] = [
    (r"\bOcean\b", "Lion-internal person name (Ocean) in skill body"),
]

# Author attribution lines to skip for the Ocean check
_AUTHOR_SKIP_RE = re.compile(
    r"""(?xi)
    (
        author\s*:          |   # YAML author: Ocean
        "author"\s*:        |   # JSON "author": ...
        by\s+Ocean\b        |   # "by Ocean" attribution
        Ocean\s*\(he/       |   # bio pronoun context
        Ocean\s+Li\b        |   # full name
        created\s+by\s+Ocean |
        maintainer.*Ocean   |
        Ocean.*maintainer
    )
""",
    re.IGNORECASE,
)

NONEXISTENT_COMMANDS: list[tuple[str, str]] = [
    (r"li\s+o\s+flow\s+validate", "nonexistent subcommand (li o flow validate)"),
    (r"li\s+o\s+flow\s+run", "nonexistent subcommand (li o flow run)"),
    (r"\bnohup\s+li\s", "nohup li ... (use --background flag instead)"),
]

STALE_MODELS: list[tuple[str, str]] = [
    (r"\bcodex/gpt-5\.5\b", "nonexistent model (codex/gpt-5.5)"),
    (r"\bcodex/gpt-5\.3-codex\b", "stale model name (codex/gpt-5.3-codex)"),
    (r"\bopus-4-7\b", "banned model name variant (opus-4-7)"),
    (r"\bopus-4\.7\b", "banned model name variant (opus-4.7)"),
]

# ---------------------------------------------------------------------------
# Compiled rule sets
# ---------------------------------------------------------------------------

_RULE_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("FORBIDDEN_PATH", FORBIDDEN_PATHS),
    ("LION_SYMBOL", FORBIDDEN_SYMBOLS),
    ("DEPRECATED_VERB", DEPRECATED_PATTERNS),
    ("INTERNAL_NAME", OCEAN_PATTERNS),
    ("NONEXISTENT_CMD", NONEXISTENT_COMMANDS),
    ("STALE_MODEL", STALE_MODELS),
]

_COMPILED: list[tuple[str, re.Pattern[str], str]] = []
for _category, _rules in _RULE_GROUPS:
    for _pat, _desc in _rules:
        _COMPILED.append((_category, re.compile(_pat), _desc))

# --yolo without --bypass: window size in lines
_YOLO_WINDOW = 3
_YOLO_RE = re.compile(r"li\s+\S*\s+.*--yolo|li\s+agent\b.*--yolo|li\s+play\b.*--yolo")
_BYPASS_RE = re.compile(r"--bypass")


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------


def _check_ocean_line(line: str) -> bool:
    """Return True if the line should be flagged for the Ocean check."""
    if not re.search(r"\bOcean\b", line):
        return False
    # Skip attribution contexts
    if _AUTHOR_SKIP_RE.search(line):
        return False
    return True


def scan_file(path: Path) -> list[str]:
    """Return a list of finding strings for one file."""
    findings: list[str] = []
    is_json = path.suffix == ".json"
    is_md = path.suffix == ".md"

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"[ERROR] {path} — cannot read: {exc}"]

    lines = text.splitlines()

    for lineno, line in enumerate(lines, start=1):
        for category, pattern, desc in _COMPILED:
            # Ocean check: skip in JSON files (author attribution in plugin.json)
            if category == "INTERNAL_NAME" and is_json:
                continue
            if category == "INTERNAL_NAME" and is_md:
                if not _check_ocean_line(line):
                    continue

            if pattern.search(line):
                findings.append(
                    f"[{category}] {path}:{lineno} — {desc}"
                )

    # --yolo without --bypass check: only in .md files
    if is_md:
        for lineno, line in enumerate(lines, start=1):
            if not _YOLO_RE.search(line):
                continue
            if _BYPASS_RE.search(line):
                continue
            # Check the window: current line + next _YOLO_WINDOW lines
            window = lines[lineno - 1 : lineno - 1 + _YOLO_WINDOW + 1]
            window_text = "\n".join(window)
            if not _BYPASS_RE.search(window_text):
                findings.append(
                    f"[YOLO_NO_BYPASS] {path}:{lineno} — --yolo without --bypass"
                    f" in next {_YOLO_WINDOW} lines"
                )

    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def collect_md_files(roots: list[Path]) -> list[Path]:
    """Recursively collect .md files from the given root directories/files."""
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            if root.suffix in (".md", ".json"):
                files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.md")))
            # plugin.json for INTERNAL_NAME check (but Ocean check skips JSON)
            files.extend(sorted(root.rglob("plugin.json")))
    return sorted(set(files))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    repo_root = Path(__file__).parent.parent.parent

    if argv:
        scan_roots = [Path(a) for a in argv]
    else:
        scan_roots = [repo_root / "marketplace"]

    files = collect_md_files(scan_roots)

    if not files:
        print("lint_skills: no .md files found to scan")
        return 0

    all_findings: list[str] = []
    for f in files:
        # Make paths relative to repo root for cleaner output
        try:
            display_path = f.relative_to(repo_root)
        except ValueError:
            display_path = f
        file_findings = scan_file(f)
        # Replace absolute path with relative in finding strings
        file_findings = [
            finding.replace(str(f), str(display_path))
            for finding in file_findings
        ]
        all_findings.extend(file_findings)

    if all_findings:
        for finding in all_findings:
            print(finding)
        print(f"\nlint_skills: {len(all_findings)} finding(s) in {len(files)} file(s)")
        return 1

    print(f"lint_skills: PASS — {len(files)} file(s) scanned, no issues found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
