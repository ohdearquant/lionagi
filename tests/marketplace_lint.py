"""Marketplace skill content validation — parameterized over every .md file under marketplace/."""

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


# ---------------------------------------------------------------------------
# Agent frontmatter grammar
#
# validate_manifests.py hand-rolls its frontmatter grammar because ci.sh runs it
# on the bare system interpreter, where no YAML parser is available. This suite
# does run under uv, so it is where that grammar gets pinned against a real
# parser. Each case records both answers; where they differ, the reason is part
# of the case. A change that makes one of them agree has to edit this table, so
# the divergence set stays closed and stays explained.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(_MARKETPLACE_ROOT / "scripts")

# (label, document, our answer, a real parser's answer, why they differ)
_FRONTMATTER_CASES: list[tuple[str, str, bool, bool, str]] = [
    ("exact fence", "---\nname: x\ndescription: d\n---\nb\n", True, True, ""),
    ("trailing space on fences", "--- \nname: x\ndescription: d\n--- \nb\n", True, True, ""),
    ("trailing tab on fence", "---\t\nname: x\ndescription: d\n---\nb\n", False, False, ""),
    ("leading space on fence", "  ---\nname: x\ndescription: d\n---\nb\n", False, False, ""),
    ("no space after colon", "---\ndescription:valid\n---\nb\n", False, False, ""),
    ("tab after colon", "---\ndescription:\tvalid\n---\nb\n", False, False, ""),
    ("nested under another key", "---\nmeta:\n  description: n\n---\nb\n", False, False, ""),
    ("empty block scalar", "---\ndescription: |\n---\nb\n", False, False, ""),
    ("value is empty", "---\ndescription:\n---\nb\n", False, False, ""),
    ("no closing fence", "---\ndescription: d\nb\n", False, False, ""),
    ("description-prefixed key", "---\ndescription_of: d\n---\nb\n", False, False, ""),
    (
        "space before colon",
        "---\ndescription : valid\n---\nb\n",
        False,
        True,
        "legal YAML, but resolving a key spelled with padding needs a parser",
    ),
    (
        "quoted key",
        '---\n"description": valid\n---\nb\n',
        False,
        True,
        "legal YAML, but unquoting a key needs a parser",
    ),
    (
        "block scalar with content",
        "---\ndescription: |\n  real text\n---\nb\n",
        False,
        True,
        "legal YAML, but folding a block scalar needs a parser",
    ),
]


def _description_check():
    """Import the validator's grammar helper, which lives outside any package."""
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from validate_manifests import _has_frontmatter_description

    return _has_frontmatter_description


def _parser_says(document: str) -> bool:
    """Whether a real parser finds a top-level non-empty string description."""
    import yaml

    try:
        docs = list(yaml.safe_load_all(document))
    except yaml.YAMLError:
        return False
    if not docs or not isinstance(docs[0], dict):
        return False
    value = docs[0].get("description")
    return isinstance(value, str) and bool(value.strip())


@pytest.mark.parametrize(
    ("document", "expected"),
    [(c[1], c[2]) for c in _FRONTMATTER_CASES],
    ids=[c[0] for c in _FRONTMATTER_CASES],
)
def test_frontmatter_grammar(tmp_path: Path, document: str, expected: bool) -> None:
    """The hand-rolled grammar answers each recorded case the recorded way."""
    subject = tmp_path / "agent.md"
    subject.write_text(document)
    assert _description_check()(subject) is expected


@pytest.mark.parametrize(
    ("document", "parser_answer"),
    [(c[1], c[3]) for c in _FRONTMATTER_CASES],
    ids=[c[0] for c in _FRONTMATTER_CASES],
)
def test_frontmatter_grammar_never_accepts_what_a_parser_rejects(
    tmp_path: Path, document: str, parser_answer: bool
) -> None:
    """The dangerous direction stays closed: no malformed document may pass.

    Erring toward rejection is tolerable and recorded per case. Erring toward
    acceptance ships an agent the host cannot read, so it is an invariant.
    """
    assert _parser_says(document) is parser_answer, "the recorded parser answer went stale"
    if not parser_answer:
        subject = tmp_path / "agent.md"
        subject.write_text(document)
        assert _description_check()(subject) is False


def test_frontmatter_divergences_are_all_explained() -> None:
    """Every case where the two disagree carries its reason, and no other case does."""
    for label, _document, ours, parser_answer, reason in _FRONTMATTER_CASES:
        if ours != parser_answer:
            assert reason, f"{label}: diverges from a parser with no reason recorded"
        else:
            assert not reason, f"{label}: agrees with a parser but records a divergence reason"


def test_shipped_agents_satisfy_the_grammar() -> None:
    """The agents this bundle actually ships pass, so the grammar is not vacuous."""
    agents = sorted((_MARKETPLACE_ROOT / "orchestrate" / "agents").glob("*.md"))
    assert agents, "no direct agent files found — the check below would be vacuous"
    check = _description_check()
    assert [a.name for a in agents if not check(a)] == []
