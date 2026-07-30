"""Marketplace skill content validation — parameterized over every .md file under marketplace/."""

from __future__ import annotations

import itertools
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
# runs under uv, so it is where that grammar is pinned against a real parser.
#
# The grammar is narrower than YAML on purpose, so exactly one direction is an
# invariant: it must never accept a document a parser would reject. The opposite
# direction is a judgement call per form, so every form rejected despite being
# legal YAML is recorded below with a category drawn from a closed set.
#
# The corpus is a CROSS PRODUCT, not a list of remembered cases. A hand-picked
# table is an enumeration: it reads as complete while being short, and two
# review rounds found forms such a table had omitted. Generating fence x key x
# value combinations covers pairings nobody thought to write down.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(_MARKETPLACE_ROOT / "scripts")

_FENCES = ["---", "--- ", "---\t", "  ---", "----", "--", "---x"]

_KEYS = [
    "description: ",
    "description:",
    "description :",
    '"description":',
    "description_of: ",
    "  description: ",
]

# YAML-significant value spellings. Several resolve to something that is not a
# string, which is the whole point: a parser answers for each one and the
# grammar must never be more permissive than that answer.
_VALUES = [
    "",
    " ",
    '""',
    '"   "',
    "'   '",
    "'x'",
    '"x"',
    "null",
    "Null",
    "NULL",
    "~",
    "true",
    "false",
    "True",
    "FALSE",
    "yes",
    "no",
    "Yes",
    "on",
    "off",
    "y",
    "n",
    "123",
    "-5",
    "1.5",
    "0x1f",
    "0o17",
    "1e3",
    "2026-07-30",
    "12:30:00",
    "[]",
    "[a]",
    "{}",
    "{a: b}",
    "|",
    ">",
    "|-",
    ">-",
    "# comment",
    "#c",
    "text",
    "text # comment",
    "text#notcomment",
    "yes # comment",
    "*anchor",
    "&a x",
    "!!str x",
    "nan",
    ".nan",
    "inf",
    ".inf",
    "-",
    "? x",
    ": x",
    ", x",
    "%x",
    "@x",
    "Yes really",
    "No thanks",
    "On call",
    "null pointer safety",
    "e5",
    "an agent that reviews changes",
]


def _documents() -> list[str]:
    """The cross product, plus structural shapes the product cannot express."""
    docs = [
        f"{fence}\nname: agent\n{key}{value}\n---\nbody\n"
        for fence in _FENCES
        for key in _KEYS
        for value in _VALUES
    ]
    docs += [
        "---\nname: agent\n",
        "---\n---\nbody\n",
        "---\nmeta:\n  description: nested\n---\nbody\n",
        "",
        "body only, no frontmatter\n",
    ]
    return docs


# Categories are closed, so a plausible-sounding free-text explanation cannot
# stand in for one. Each row is also asserted to be a real divergence, which is
# what stops the record drifting into fiction.
_NARROWING_CATEGORIES = frozenset(
    {
        "needs-a-parser-to-unquote",
        "needs-a-parser-to-fold",
        "needs-a-parser-to-resolve-the-key",
        "outside-the-provable-whitelist",
    }
)

# (document a parser accepts and the grammar rejects, category)
_DOCUMENTED_NARROWINGS: list[tuple[str, str]] = [
    ('---\ndescription: "x"\n---\nb\n', "needs-a-parser-to-unquote"),
    ("---\ndescription: 'x'\n---\nb\n", "needs-a-parser-to-unquote"),
    ("---\ndescription: |\n  real text\n---\nb\n", "needs-a-parser-to-fold"),
    ("---\ndescription: >\n  real text\n---\nb\n", "needs-a-parser-to-fold"),
    ("---\ndescription : valid\n---\nb\n", "needs-a-parser-to-resolve-the-key"),
    ('---\n"description": valid\n---\nb\n', "needs-a-parser-to-resolve-the-key"),
    ("---\ndescription: 0o17\n---\nb\n", "outside-the-provable-whitelist"),
    ("---\ndescription: 1e3\n---\nb\n", "outside-the-provable-whitelist"),
    ("---\ndescription: &a x\n---\nb\n", "outside-the-provable-whitelist"),
    ("---\ndescription: !!str x\n---\nb\n", "outside-the-provable-whitelist"),
]

# Forms the review rounds actually surfaced, kept named so a reader can see which
# ones were real defects rather than inferring it from the cross product.
# (label, document, the grammar's required answer)
_NAMED_REGRESSIONS: list[tuple[str, str, bool]] = [
    ("trailing-space fence stays accepted", "--- \ndescription: text\n--- \nb\n", True),
    ("trailing-tab fence", "---\t\ndescription: text\n---\nb\n", False),
    ("no space after colon", "---\ndescription:text\n---\nb\n", False),
    ("tab after colon", "---\ndescription:\ttext\n---\nb\n", False),
    ("comment-only value", "---\ndescription: # c\n---\nb\n", False),
    ("quoted empty value", '---\ndescription: ""\n---\nb\n', False),
    ("quoted whitespace value", '---\ndescription: "   "\n---\nb\n', False),
    ("null value", "---\ndescription: null\n---\nb\n", False),
    ("sequence value", "---\ndescription: []\n---\nb\n", False),
    ("mapping value", "---\ndescription: {}\n---\nb\n", False),
    ("integer value", "---\ndescription: 123\n---\nb\n", False),
    ("boolean value", "---\ndescription: yes\n---\nb\n", False),
    ("boolean with a trailing comment", "---\ndescription: yes # c\n---\nb\n", False),
    ("prose opening with a bool word", "---\ndescription: Yes really\n---\nb\n", True),
    ("prose opening with null", "---\ndescription: null pointer safety\n---\nb\n", True),
    ("y resolves to a string, not a bool", "---\ndescription: y\n---\nb\n", True),
    ("colon-space nests the mapping", "---\ndescription: text: value\n---\nb\n", False),
    ("trailing colon", "---\ndescription: text:\n---\nb\n", False),
    ("colon without a space stays fine", "---\ndescription: ratio 1:2\n---\nb\n", True),
    ("time of day stays fine", "---\ndescription: standup at 12:30 daily\n---\nb\n", True),
    ("tab inside the value", "---\ndescription: text\there\n---\nb\n", False),
    ("trailing tab", "---\ndescription: text\t\n---\nb\n", False),
    ("ordinary description", "---\ndescription: an agent that reviews\n---\nb\n", True),
]


def _description_check():
    """Import the validator's grammar helper, which lives outside any package."""
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from validate_manifests import _has_frontmatter_description

    return _has_frontmatter_description


def _grammar_check():
    """The same grammar over text, so a sweep needs no file per candidate."""
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from validate_manifests import _frontmatter_description_ok

    return _frontmatter_description_ok


# Alphabet for the lexical sweep. Every character class that has actually produced a
# false pass in this lane is present: a letter, an uppercase letter, a digit, a space,
# a colon, a hash, a quote, a bracket, a brace, a dash, a pipe, a tilde, a TAB and a
# dot. Two of these were found only by sweeping — a colon followed by whitespace makes
# the line parse as a nested mapping, and a tab makes the document invalid outright.
_VALUE_ATOMS = ["a", "Z", "1", " ", ":", "#", '"', "[", "}", "-", "|", "~", "\t", "."]


def _swept_values() -> list[str]:
    """Every value of length 1 to 3 over the alphabet above.

    Generated rather than listed. A curated vocabulary inherits whatever its author
    failed to think of, which is how both of the sweep-only defects above survived a
    2651-document cross product built from hand-picked value spellings.
    """
    return [
        "".join(combo) for n in (1, 2, 3) for combo in itertools.product(_VALUE_ATOMS, repeat=n)
    ]


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


def _sweep_for_false_passes(documents: list[str], minimum: int) -> None:
    """Assert the one invariant over a corpus, after asserting the corpus is informative.

    The opposite direction is deliberately not asserted. The grammar is narrower than
    YAML, and each narrowing lives in _DOCUMENTED_NARROWINGS, so a form it rejects is a
    recorded decision rather than a silent gap.
    """
    grammar = _grammar_check()
    accepted_by_us = accepted_by_parser = 0
    false_passes: list[str] = []
    for document in documents:
        ours = grammar(document)
        parser = _parser_says(document)
        accepted_by_us += ours
        accepted_by_parser += parser
        if ours and not parser:
            false_passes.append(repr(document))

    # A corpus that resolved nothing satisfies the invariant vacuously, so the
    # measurement is asserted informative before its result is trusted.
    assert len(documents) >= minimum, f"corpus collapsed to {len(documents)} documents"
    assert accepted_by_us, "the grammar accepted nothing at all — it cannot discriminate"
    assert accepted_by_parser, "the parser accepted nothing at all — the corpus is malformed"
    assert accepted_by_parser < len(documents), "the parser accepted everything — corpus is trivial"

    assert not false_passes, (
        f"the grammar accepts {len(false_passes)} of {len(documents)} documents that a "
        f"parser rejects (listing up to 20):\n"
        + "\n".join(f"  {d}" for d in sorted(false_passes)[:20])
    )


def test_grammar_never_accepts_what_a_parser_rejects() -> None:
    """Structural sweep: fence x key x value spellings."""
    _sweep_for_false_passes(_documents(), minimum=1000)


def test_value_lexical_sweep_finds_no_false_pass() -> None:
    """Lexical sweep: every generated value of length up to three.

    This is the corpus that catches what a curated vocabulary misses. The rule it
    guards was derived from a wider sweep than the one committed here — exhaustive to
    length three over 27 atoms, plus length four over 12 — which found zero false
    passes; this narrower version keeps every character class that mattered while
    staying fast enough to run on every commit.
    """
    documents = [
        f"---\nname: agent\ndescription: {value}\n---\nbody\n" for value in _swept_values()
    ]
    _sweep_for_false_passes(documents, minimum=2500)


def test_grammar_and_file_wrapper_agree(tmp_path: Path) -> None:
    """The Path wrapper and the text function answer identically.

    The sweeps above exercise the text function for speed, so this is what stops the
    shipped entry point drifting away from the thing that is actually swept.
    """
    grammar, check = _grammar_check(), _description_check()
    subject = tmp_path / "agent.md"
    disagreements = []
    for document in _documents():
        subject.write_text(document)
        if check(subject) != grammar(document):
            disagreements.append(repr(document))
    assert not disagreements, f"wrapper and text function disagree on: {disagreements[:10]}"


@pytest.mark.parametrize(
    ("document", "category"),
    _DOCUMENTED_NARROWINGS,
    ids=[f"{i}-{category}" for i, (_d, category) in enumerate(_DOCUMENTED_NARROWINGS)],
)
def test_documented_narrowings_are_real(tmp_path: Path, document: str, category: str) -> None:
    """Each recorded narrowing is a genuine parser-accepts, grammar-rejects pair.

    Asserting both answers is what keeps the record honest: a row whose parser answer
    changed, or that the grammar has since started accepting, fails here instead of
    remaining in the table as a false explanation.
    """
    assert category in _NARROWING_CATEGORIES, f"unknown narrowing category {category!r}"
    subject = tmp_path / "agent.md"
    subject.write_text(document)
    assert _parser_says(document) is True, "a parser no longer accepts this form"
    assert _description_check()(subject) is False, "the grammar now accepts it — drop this row"


@pytest.mark.parametrize(
    ("document", "expected"),
    [(d, e) for _label, d, e in _NAMED_REGRESSIONS],
    ids=[label for label, _d, _e in _NAMED_REGRESSIONS],
)
def test_named_regressions(tmp_path: Path, document: str, expected: bool) -> None:
    """Forms the review rounds surfaced, pinned individually so each one is named."""
    subject = tmp_path / "agent.md"
    subject.write_text(document)
    assert _description_check()(subject) is expected


@pytest.mark.parametrize(
    "document",
    [d for _label, d, e in _NAMED_REGRESSIONS if e],
    ids=[label for label, _d, e in _NAMED_REGRESSIONS if e],
)
def test_named_acceptances_agree_with_a_parser(document: str) -> None:
    """Every form the grammar is required to accept is one a parser accepts too.

    Without this, a regression row could pin an accept that is itself a false pass.
    """
    assert _parser_says(document) is True


def test_shipped_agents_satisfy_the_grammar() -> None:
    """The agents this bundle actually ships pass, so the grammar is not vacuous."""
    agents = sorted((_MARKETPLACE_ROOT / "orchestrate" / "agents").glob("*.md"))
    assert agents, "no direct agent files found — the check below would be vacuous"
    check = _description_check()
    assert [a.name for a in agents if not check(a)] == []
