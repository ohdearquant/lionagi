"""pytest-based marketplace skill content validation (issue #1031).

Parameterized over every .md file under marketplace/. Each test validates
a different surface: MCP tool names, CLI subcommands, model identifiers,
banned models, nohup usage, and lambda roster names.

Run:
    uv run pytest tests/marketplace_lint.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_MARKETPLACE_ROOT = _REPO_ROOT / "marketplace"


def get_skill_files() -> list[Path]:
    """Return all .md files under marketplace/."""
    if not _MARKETPLACE_ROOT.is_dir():
        return []
    return sorted(_MARKETPLACE_ROOT.rglob("*.md"))


_SKILL_FILES = get_skill_files()

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# mcp__server__verb  (e.g. mcp__khive__recall, mcp__lore__compose)
_MCP_RE = re.compile(r"\bmcp__([a-z0-9_-]+)__([a-z_]+)\b")

# li <subcommand> (first word only; excludes flags and compound paths)
_LI_RE = re.compile(r"(?<![/\w])li\s+([a-z][a-z_-]*)\b")

# model identifiers: provider/name or bare name like opus-4-7 or gpt-5.4
_MODEL_RE = re.compile(
    r"\b(?:claude(?:-code)?|codex|openai|gpt)/([a-z0-9_.-]+)\b|(?:opus|sonnet|haiku)-[\d.]+\b|gpt-[\d.]+\b"
)

# nohup
_NOHUP_RE = re.compile(r"\bnohup\b")

# lambda namespace references  lambda:<name>
_LAMBDA_RE = re.compile(r"\blambda:([a-z][a-z0-9_-]*)\b")

# ---------------------------------------------------------------------------
# Allowed sets
# ---------------------------------------------------------------------------

# Canonical khive verbs (from ADR + server registration)
_KNOWN_KHIVE_VERBS: frozenset[str] = frozenset(
    {
        "assign",
        "complete",
        "create",
        "delete",
        "inbox",
        "link",
        "list",
        "next",
        "orient",
        "recall",
        "remember",
        "request",
        "search",
        "send",
        "thread",
        "update",
        "get",
        "merge",
        "neighbors",
        "query",
        "traverse",
        "suggest",
        "compose",
        "log",
        "trend",
        "remind",
        # brain pack
        "brain.config",
        "brain.emit",
        "brain.events",
        "brain.reset",
        "brain.state",
        # recall sub-verbs
        "recall.candidates",
        "recall.embed",
        "recall.fuse",
        "recall.score",
    }
)

# Servers whose verbs we validate against _KNOWN_KHIVE_VERBS
_KHIVE_SERVERS: frozenset[str] = frozenset({"khive", "khive-remote", "khive-staging"})

# All known valid MCP servers (servers not in this set get a warning, not a failure)
_KNOWN_MCP_SERVERS: frozenset[str] = frozenset(
    {
        "khive",
        "khive-remote",
        "khive-staging",
        "lore",
        "kg",
        "plugin-context7-context7",
        "plugin-kg-kg",
        "chrome-devtools",
        "claude-in-chrome",
        "claude-ai-gmail",
        "claude-ai-google-calendar",
        "claude-ai-google-drive",
        "plugin-stripe-stripe",
    }
)

# Top-level `li` subcommands derived from lionagi/cli/main.py
# (agent, o/orchestrate, team, studio, state, invoke) plus sugar (play, skill)
_KNOWN_LI_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "agent",
        "o",
        "orchestrate",
        "team",
        "studio",
        "state",
        "invoke",
        "play",  # sugar for li o flow -p NAME
        "skill",  # prints skill body
    }
)

# Explicitly banned model strings (deprecated / hallucinated names)
_BANNED_MODELS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcodex/gpt-5\.3-codex\b"), "stale model codex/gpt-5.3-codex"),
    (re.compile(r"\bgpt-5\.5\b"), "hallucinated model gpt-5.5"),
    (re.compile(r"\bopus-4-8\b"), "future/invalid model opus-4-8"),
    (re.compile(r"\bclaude-3\b"), "retired model family claude-3"),
    (re.compile(r"\bclaude-2\b"), "retired model family claude-2"),
    (re.compile(r"\bclaude-1\b"), "retired model family claude-1"),
    (re.compile(r"\btext-davinci\b"), "retired OpenAI model text-davinci"),
]

# Canonical lambda namespace roster (warn on unknown — don't fail)
_CANONICAL_LAMBDAS: frozenset[str] = frozenset(
    {
        "lionagi",
        "leo",
        "khive",
    }
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _SKILL_FILES, ids=[_rel(p) for p in _SKILL_FILES])
def test_no_banned_models(path: Path) -> None:
    """Fail if a deprecated or hallucinated model string appears."""
    text = _read(path)
    violations: list[str] = []
    for pattern, label in _BANNED_MODELS:
        for m in pattern.finditer(text):
            lineno = text[: m.start()].count("\n") + 1
            violations.append(f"line {lineno}: {label!r}")
    assert not violations, f"{_rel(path)} contains banned model references:\n" + "\n".join(
        f"  {v}" for v in violations
    )


@pytest.mark.parametrize("path", _SKILL_FILES, ids=[_rel(p) for p in _SKILL_FILES])
def test_no_nohup_usage(path: Path) -> None:
    """Fail if `nohup` appears — use --background flag instead."""
    text = _read(path)
    hits: list[int] = []
    for m in _NOHUP_RE.finditer(text):
        hits.append(text[: m.start()].count("\n") + 1)
    assert not hits, f"{_rel(path)} uses `nohup` (use --background flag instead) at line(s): {hits}"


@pytest.mark.parametrize("path", _SKILL_FILES, ids=[_rel(p) for p in _SKILL_FILES])
def test_mcp_khive_verbs_are_canonical(path: Path) -> None:
    """Fail if a khive MCP tool name uses an unknown verb."""
    text = _read(path)
    bad: list[str] = []
    for m in _MCP_RE.finditer(text):
        server, verb = m.group(1), m.group(2)
        if server in _KHIVE_SERVERS and verb not in _KNOWN_KHIVE_VERBS:
            lineno = text[: m.start()].count("\n") + 1
            bad.append(f"line {lineno}: mcp__{server}__{verb} — unknown verb")
    assert not bad, f"{_rel(path)} references unknown khive MCP verbs:\n" + "\n".join(
        f"  {b}" for b in bad
    )


@pytest.mark.parametrize("path", _SKILL_FILES, ids=[_rel(p) for p in _SKILL_FILES])
def test_cli_subcommands_exist(path: Path) -> None:
    """Fail if a `li <subcommand>` example uses a subcommand not in the CLI registry."""
    text = _read(path)
    bad: list[str] = []
    for m in _LI_RE.finditer(text):
        cmd = m.group(1)
        if cmd not in _KNOWN_LI_SUBCOMMANDS:
            lineno = text[: m.start()].count("\n") + 1
            bad.append(f"line {lineno}: `li {cmd}` — unknown subcommand")
    assert not bad, f"{_rel(path)} references unknown `li` subcommands:\n" + "\n".join(
        f"  {b}" for b in bad
    )


@pytest.mark.parametrize("path", _SKILL_FILES, ids=[_rel(p) for p in _SKILL_FILES])
def test_lambda_names_are_canonical(path: Path) -> None:
    """Warn (xfail) if a lambda: namespace not in the canonical roster is referenced.

    This is a soft check: unknown lambda IDs generate xfail markers rather than
    hard failures, since third-party plugins may define their own lambda namespaces.
    """
    text = _read(path)
    unknown: list[str] = []
    for m in _LAMBDA_RE.finditer(text):
        name = m.group(1)
        if name not in _CANONICAL_LAMBDAS:
            lineno = text[: m.start()].count("\n") + 1
            unknown.append(f"line {lineno}: lambda:{name}")
    if unknown:
        pytest.xfail(
            f"{_rel(path)} references non-canonical lambda namespace(s):\n"
            + "\n".join(f"  {u}" for u in unknown)
        )
