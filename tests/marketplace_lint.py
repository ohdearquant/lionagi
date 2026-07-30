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
        # Shapes where the description entry is fine and the block is not. The product
        # cannot express these, because it only ever varies one entry.
        "---\ndescription: text\n\tname: a\n---\nbody\n",
        "---\ndescription: text\n  name: a\n---\nbody\n",
        "---\ndescription: text\nbarewords\n---\nbody\n",
        "---\ndescription: text\nmeta:\n  k: v\n---\nbody\n",
        "---\nname: a: b\ndescription: text\n---\nbody\n",
        "---\nname: *x\ndescription: text\n---\nbody\n",
        '---\nname: "unterminated\ndescription: text\n---\nbody\n',
        "---\nname: {a: b\ndescription: text\n---\nbody\n",
        "---\ndescription: text\ndescription: other\n---\nbody\n",
        "﻿---\ndescription: text\n---\nbody\n",
        "---\r\ndescription: text\r\n---\r\nbody\r\n",
        # Lone CR and a mix. Present so the wrapper/function agreement test covers the
        # terminator axis: read_text translates these, the text function has to as well, and
        # nothing else in this corpus would notice if they drifted apart.
        "---\rdescription: text\r---\rbody\r",
        "---\rdescription: text\n---\rbody\n",
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
        "needs-a-parser-to-resolve-nested-structure",
        "needs-a-parser-to-resolve-a-flow-collection",
        "frontmatter-must-be-the-first-line",
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
    # A value spelled entirely with zero-width characters. A parser calls it a non-empty
    # string and it is, technically; a reader sees nothing. Refused deliberately.
    ("---\ndescription: ﻿\n---\nb\n", "outside-the-provable-whitelist"),
    # Sibling entries. Every one of these leaves the description entry well formed and a
    # parser reads the file, so each is a price paid for classifying sibling values at
    # all. Classifying them is what closed seven false passes, so the price is named
    # rather than argued away.
    (
        "---\ndescription: text\nmeta:\n  k: v\n---\nb\n",
        "needs-a-parser-to-resolve-nested-structure",
    ),
    ("---\nnotes: |\n  x\ndescription: text\n---\nb\n", "needs-a-parser-to-fold"),
    ('---\nname: "a: b"\ndescription: text\n---\nb\n', "needs-a-parser-to-unquote"),
    (
        "---\nname: {a: b}\ndescription: text\n---\nb\n",
        "needs-a-parser-to-resolve-a-flow-collection",
    ),
    (
        "---\ntools: [a, b]\ndescription: text\n---\nb\n",
        "needs-a-parser-to-resolve-a-flow-collection",
    ),
    ("---\nname: &a x\ndescription: text\n---\nb\n", "outside-the-provable-whitelist"),
    ("---\nname: !!str x\ndescription: text\n---\nb\n", "outside-the-provable-whitelist"),
    ("---\nname: # c\ndescription: text\n---\nb\n", "outside-the-provable-whitelist"),
    # A blank line before the opening fence. A parser is happy to find the mapping on the
    # second line; this treats frontmatter as something that opens the file, which is what
    # every host that reads it does. Recorded for each line-break kind because the fix for
    # CR-only files made these three reachable by different routes to the same answer.
    ("\n---\ndescription: text\n---\nb\n", "frontmatter-must-be-the-first-line"),
    ("\r---\ndescription: text\n---\nb\n", "frontmatter-must-be-the-first-line"),
    ("\r\n---\ndescription: text\n---\nb\n", "frontmatter-must-be-the-first-line"),
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
    # Control characters other than TAB. All four were accepted before, and they reached
    # the value check by a second route as well: str.splitlines() breaks on them, so
    # splitting with it moved the character out of the value entirely.
    ("vertical tab in the value", "---\ndescription: text\vhere\n---\nb\n", False),
    ("form feed in the value", "---\ndescription: text\fhere\n---\nb\n", False),
    ("NUL in the value", "---\ndescription: text\0here\n---\nb\n", False),
    ("escape in the value", "---\ndescription: text\x1bhere\n---\nb\n", False),
    ("C1 next-line in the value", "---\ndescription: text\x85here\n---\nb\n", False),
    ("delete in the value", "---\ndescription: text\x7fhere\n---\nb\n", False),
    ("unicode line separator", "---\ndescription: text here\n---\nb\n", False),
    ("unicode paragraph separator", "---\ndescription: text here\n---\nb\n", False),
    # The other direction, and the reason str.isprintable() is not usable here: these all
    # look unprintable and a parser accepts every one of them.
    ("non-breaking space stays fine", "---\ndescription: text\xa0here\n---\nb\n", True),
    ("zero-width space stays fine", "---\ndescription: text​here\n---\nb\n", True),
    ("byte-order mark inside the value", "---\ndescription: text﻿here\n---\nb\n", True),
    ("emoji stays fine", "---\ndescription: ships \U0001f680 fast\n---\nb\n", True),
    ("CJK stays fine", "---\ndescription: an agent 代理 here\n---\nb\n", True),
    ("accented letters stay fine", "---\ndescription: rôle de révision\n---\nb\n", True),
    # Line endings and the leading mark an editor adds. Both are files a host reads.
    ("CRLF line endings", "---\r\ndescription: text\r\n---\r\nb\r\n", True),
    ("lone CR line endings", "---\rdescription: text\r---\rb\r", True),
    ("mixed CR and LF endings", "---\rdescription: text\n---\rb\n", True),
    ("CR inside the value", "---\ndescription: a\rb\n---\nb\n", False),
    ("leading byte-order mark", "﻿---\ndescription: text\n---\nb\n", True),
    # A parser tolerates one mark at the stream start and no more. Stripping every leading
    # mark accepted a file the parser refuses, so the count is what these pin.
    ("two leading byte-order marks", "﻿﻿---\ndescription: text\n---\nb\n", False),
    ("three leading byte-order marks", "﻿﻿﻿---\ndescription: text\n---\nb\n", False),
    ("byte-order mark then space", "﻿ ---\ndescription: text\n---\nb\n", False),
    ("space then byte-order mark", " ﻿---\ndescription: text\n---\nb\n", False),
    ("byte-order mark alone", "﻿", False),
    # Multiplicity matters for the mark and not for a trailing space, so both are pinned:
    # a fence may carry any number of trailing spaces and stays valid.
    ("fence with three trailing spaces", "---   \ndescription: text\n---\nb\n", True),
    ("closing fence with trailing spaces", "---\ndescription: text\n---   \nb\n", True),
    # Sibling lines. The description entry is well formed in every one of these; the file
    # is broken by the line next to it. Classifying only the description vouched for all
    # of them.
    ("tab-indented sibling", "---\ndescription: text\n\tname: a\n---\nb\n", False),
    ("over-indented sibling", "---\ndescription: text\n  name: a\n---\nb\n", False),
    ("bare scalar sibling", "---\ndescription: text\nbarewords\n---\nb\n", False),
    ("sibling colon-space nests", "---\nname: a: b\ndescription: text\n---\nb\n", False),
    ("sibling colon then two spaces", "---\nname: a:  b\ndescription: text\n---\nb\n", False),
    ("sibling trailing colon", "---\nname: text:\ndescription: text\n---\nb\n", False),
    ("sibling alias with no anchor", "---\nname: *x\ndescription: text\n---\nb\n", False),
    ("sibling unterminated quote", '---\nname: "unterminated\ndescription: text\n---\nb\n', False),
    ("sibling unclosed flow mapping", "---\nname: {a: b\ndescription: text\n---\nb\n", False),
    ("sibling unclosed flow sequence", "---\nname: [a\ndescription: text\n---\nb\n", False),
    ("sibling colon without a space stays fine", "---\nname: a:b\ndescription: t\n---\nb\n", True),
    ("sibling empty value stays fine", "---\nname:\ndescription: text\n---\nb\n", True),
    ("sibling number stays fine", "---\nversion: 1.5\ndescription: text\n---\nb\n", True),
    ("sibling bool stays fine", "---\nenabled: true\ndescription: text\n---\nb\n", True),
    ("comment line in the block stays fine", "---\n# note\ndescription: text\n---\nb\n", True),
    ("blank line in the block stays fine", "---\n\ndescription: text\n---\nb\n", True),
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
# false pass in this lane is present, and each addition is here because a review round
# or a sweep found something with it, never because it looked dangerous:
#
# * a letter, an uppercase letter, a digit, a space, a dot — ordinary text.
# * a colon, a TAB — found only by sweeping. A colon followed by whitespace nests a
#   mapping; a tab anywhere makes the document invalid outright.
# * a hash, a quote, a bracket, a brace, a dash, a pipe, a tilde — indicator characters.
# * a vertical tab, a form feed, NUL, ESC and 0x85 — a later round found all four of the
#   first ones accepted while a parser rejects them. They are not interchangeable with
#   TAB: they also split str.splitlines(), which is how they escaped the value and got
#   past a character check that ran on the value alone.
# * an apostrophe, a star, an ampersand, a comma — the alias, anchor and flow-collection
#   openers. These found nothing in the value position and seven false passes in the
#   sibling position, which is the argument for sweeping both positions rather than
#   assuming a rule proven in one place holds in the other.
_VALUE_ATOMS = [
    "a",
    "Z",
    "1",
    " ",
    ":",
    "#",
    '"',
    "[",
    "}",
    "-",
    "|",
    "~",
    "\t",
    ".",
    "\v",
    "\f",
    "\0",
    "\x1b",
    "'",
    "*",
    "&",
    "!",
    ",",
    "\x85",
]


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
    """Whether a real parser finds a top-level non-empty string description.

    The whole document is handed to the parser, body included, and that is deliberate:
    it means YAML's own ``---`` document splitting decides where the frontmatter ends,
    so the fence rules are checked by something other than the code being tested. An
    oracle that split on fences using the grammar's own logic could not catch a fence
    defect at all, and two of this lane's findings were fence defects.

    The price is a precondition: it is only a valid oracle for a document whose body is
    inert as YAML. Every generated corpus here uses the body ``body``, and
    ``_sweep_for_false_passes`` asserts that rather than trusting it, because a real
    markdown body routinely is not valid YAML — an asterisk in prose reads as an alias
    and the parser refuses the file. Use ``_parser_says_of_block`` for real files.
    """
    import yaml

    try:
        docs = list(yaml.safe_load_all(document))
    except yaml.YAMLError:
        return False
    if not docs or not isinstance(docs[0], dict):
        return False
    value = docs[0].get("description")
    return isinstance(value, str) and bool(value.strip())


def _parser_says_of_block(text: str) -> bool:
    """The same question about a real file, parsing only the frontmatter block.

    This is what a host actually does: split the fences, parse the block, ignore the
    body. It is the right oracle for files whose body is prose, and the wrong one for
    the generated corpora, since the split here is not independent of the grammar.
    Both oracles exist because neither is correct for both jobs.
    """
    import yaml

    lines = text.lstrip("﻿").split("\n")
    if not lines or lines[0].rstrip(" ") != "---":
        return False
    close = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip(" ") == "---"), None)
    if close is None:
        return False
    try:
        block = yaml.safe_load("\n".join(lines[1:close]))
    except yaml.YAMLError:
        return False
    if not isinstance(block, dict):
        return False
    value = block.get("description")
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
    # The oracle parses whole documents, so it only answers correctly for a corpus whose
    # bodies are inert YAML. Stated as an assertion rather than left as a habit: a future
    # corpus with a realistic body would make every answer here quietly meaningless.
    # Line breaks are normalised the same way the code under test normalises them, rather
    # than deleted. Deleting carriage returns leaves a CR-only document with no "---\n" in
    # it at all, so the split returns the whole file as its "body" and this check fails as a
    # corpus complaint pointing at nothing. CRLF collapses before a lone CR, or one break
    # becomes two.
    bodies = {
        d.replace("\r\n", "\n").replace("\r", "\n").split("---\n")[-1]
        for d in documents
        if d.count("---") >= 2
    }
    assert bodies <= {"body\n", "b\n", ""}, f"corpus has a non-inert body: {sorted(bodies)[:5]}"
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


def test_sibling_value_sweep_finds_no_false_pass() -> None:
    """The same alphabet in the position next to the description.

    A rule proven in one position is not proven in the other, and this is where that
    stopped being a principle and became a measurement: the value rules held while seven
    spellings of a *sibling* value passed. Each of them leaves the description entry
    perfectly well formed and makes the file unreadable anyway, so a check that reads only
    the description reports a usable description for a file no host can load.
    """
    documents = [
        f"---\nname: {value}\ndescription: real text\n---\nbody\n" for value in _swept_values()
    ]
    _sweep_for_false_passes(documents, minimum=2500)


_LINE_BREAKS = ["\n", "\r\n", "\r"]


def test_every_yaml_line_break_is_read_the_same_way() -> None:
    """The three sequences a parser treats as a line break, in every combination.

    This is the axis the corpora held constant. Everything else here varies *content* while
    terminating every line with ``\\n``, so a file that uses a different terminator was
    outside every sweep, and a CR-only file is valid YAML whose frontmatter loads.

    What that cost is worth stating precisely, because it is NOT "a real file was rejected".
    ``Path.read_text`` applies universal newlines, so the **file** path never sees a lone CR:
    it arrives already translated and the wrapper answered correctly throughout. The defect
    was in the text function, which is the surface every one of these sweeps measures. So a
    whole class of input was being measured against something *stricter than the product*, and
    the two entry points disagreed about it. That is the same hazard as a rule measured apart
    from its live route, arriving from the other side: here the route was right and the
    measured function was wrong.

    Both directions are asserted for each mix, since the risk runs both ways: missing a
    terminator rejects a good file, and normalising too eagerly would accept a bad one. A CR
    *inside* a value must stay rejected, which is the case the last row covers.
    """
    grammar = _grammar_check()
    disagreements = []
    for opening in _LINE_BREAKS:
        for middle in _LINE_BREAKS:
            for closing in _LINE_BREAKS:
                document = f"---{opening}description: real text{middle}---{closing}body{closing}"
                ours, parser = grammar(document), _parser_says(document)
                if ours != parser:
                    disagreements.append(f"{opening!r}/{middle!r}/{closing!r} ours={ours}")
    assert not disagreements, f"line-break mixes read differently: {disagreements}"

    # A CR that terminates a line is a break; a CR sitting inside a value is not, and a
    # parser refuses the document. Normalising line breaks must not blur the two.
    for value_break in ("\r", "\r\n", "\n"):
        document = f"---\ndescription: a{value_break}b\n---\nbody\n"
        assert not grammar(document), f"accepted a value containing {value_break!r}"
        assert not _parser_says(document), f"parser now accepts {value_break!r} in a value"


def test_stream_prefix_sweep_finds_no_false_pass() -> None:
    """Everything that can sit between the start of the file and the opening fence.

    Generated over the characters an editor or a merge can actually leave there, at every
    count from none to three, because **the count is the thing that decides validity** and
    a corpus that varies content while holding position fixed cannot see it. A parser
    tolerates one byte-order mark at the stream start and treats a second as content
    before the document marker; stripping every leading mark accepted such a file. The
    per-code-point sweep could not catch it, since that one places its character inside a
    value where multiplicity is irrelevant.
    """
    prefix_atoms = ["﻿", " ", "\t", "\n", "\r"]
    prefixes = [""] + [
        "".join(combo) for n in (1, 2, 3) for combo in itertools.product(prefix_atoms, repeat=n)
    ]
    documents = [f"{prefix}---\ndescription: real text\n---\nbody\n" for prefix in prefixes]
    _sweep_for_false_passes(documents, minimum=150)


def test_line_safety_boundary_matches_a_parser() -> None:
    """Every code point up to U+02FF, plus the ones above it that matter.

    A per-code-point sweep rather than a list of suspicious characters, because the
    finding that produced this test was a boundary question: the answer turned out to be
    two disjoint ranges, and the C1 half of it is not one anybody proposed. Both
    directions are asserted, since the tempting shortcut here (``str.isprintable()``) is
    wrong in both — it would accept nothing dangerous and reject emoji and NBSP.
    """
    grammar = _grammar_check()
    named_above_range = [0x2028, 0x2029, 0x3000, 0xFEFF, 0x1F680, 0xE000, 0xFFFD]
    points = [c for c in range(0x300) if c != 0x0A] + named_above_range
    false_passes, false_fails, rejected = [], [], 0
    for code in points:
        document = f"---\ndescription: a{chr(code)}b\n---\nbody\n"
        ours, parser = grammar(document), _parser_says(document)
        rejected += not parser
        if ours and not parser:
            false_passes.append(hex(code))
        if parser and not ours:
            false_fails.append(hex(code))

    assert rejected, "the parser rejected no code point at all — the sweep is uninformative"
    assert rejected < len(points), "the parser rejected every code point — corpus is trivial"
    assert not false_passes, f"grammar accepts code points a parser rejects: {false_passes}"
    assert not false_fails, f"grammar rejects code points a parser accepts: {false_fails}"


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


def test_shipped_agents_agree_with_a_parser() -> None:
    """The files the validator actually reads get both answers, not just the grammar's.

    The test above asserts the grammar accepts the shipped agents, which would also pass
    if the grammar accepted a broken file. Asking a parser the same question is what makes
    that accept mean something.

    It has to be the block oracle. Two of these files contain an asterisk in prose, so a
    whole-document parse refuses the body and reports a disagreement that is really the
    oracle reading the wrong thing.
    """
    agents = sorted((_MARKETPLACE_ROOT / "orchestrate" / "agents").glob("*.md"))
    assert len(agents) >= 2, f"only {len(agents)} agent files found — comparison is vacuous"
    check = _description_check()
    disagreements = [
        f"{_rel(path)}: grammar={check(path)} parser={_parser_says_of_block(_read(path))}"
        for path in agents
        if check(path) != _parser_says_of_block(_read(path))
    ]
    assert not disagreements, f"grammar and parser disagree on shipped agents: {disagreements}"


def test_shipped_skills_are_outside_the_grammar_and_outside_its_subject() -> None:
    """A measured limit, pinned so that widening the grammar has to face it.

    The validator reads descriptions under ``agents/`` only; skills are checked for
    existence and never parsed. So this is not a live failure, and it is worth pinning
    anyway, because the number is the argument: the grammar rejects **every** shipped
    skill file, not an unusual one. They all write ``description: >`` folded over several
    lines and carry an ``allowed-tools: [...]`` flow sequence, and both of those are
    recorded narrowings.

    That makes the folded-scalar narrowing much more expensive than the narrowing table
    alone suggests, and anyone pointing this check at skills has to widen the grammar
    first. This test fails the moment either of those things changes, which is the point:
    it turns a cost that is currently invisible into one that has to be dealt with
    deliberately.
    """
    skills = sorted((_MARKETPLACE_ROOT / "orchestrate" / "skills").glob("*/SKILL.md"))
    assert len(skills) >= 5, f"only {len(skills)} skill files found — the count below is the claim"
    check, problems = _description_check(), []
    for path in skills:
        parser_reads_it = _parser_says_of_block(_read(path))
        if check(path) or not parser_reads_it:
            problems.append(f"{_rel(path)}: grammar={check(path)} parser={parser_reads_it}")
    assert not problems, (
        "the shipped skills no longer sit exactly outside the grammar and inside a "
        f"parser. Update the narrowing record and this test together: {problems}"
    )


def test_validator_runs_on_the_interpreter_ci_actually_uses() -> None:
    """The script is executed the way ci.sh executes it, not the way this suite imports it.

    ci.sh calls ``python3 validate_manifests.py`` on the bare system interpreter with no uv
    arm, and this suite runs under uv. Those are different interpreters — measured here as
    3.9.6 against 3.10.15 — so every other test in this file exercises a Python the lint
    never uses. Anything 3.10-only in that script would pass this whole suite and break the
    actual lint, which is why the dependency-free constraint has to be checked by running
    it rather than by remembering it.
    """
    import shutil
    import subprocess

    interpreter = shutil.which("python3") or shutil.which("python")
    assert interpreter, "no python3 on PATH — ci.sh would skip the validator entirely"
    script = _MARKETPLACE_ROOT / "scripts" / "validate_manifests.py"
    completed = subprocess.run(
        [interpreter, str(script)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"{interpreter} failed on the validator (rc {completed.returncode}):\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    # A run that printed nothing would satisfy the rc check without having validated
    # anything, so the output is asserted to name what it checked.
    assert "plugin(s)" in completed.stdout, f"validator produced no verdict: {completed.stdout!r}"


def test_unreadable_file_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """A file the validator cannot decode fails the check without ending the run.

    ``Path.read_text`` raises UnicodeDecodeError, which is a ValueError and so is not
    caught by an ``except OSError``. The same crash class as an earlier round's, on a
    different path, so both arms are pinned: the answer is False and the reason names the
    problem rather than claiming the description is missing.
    """
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from validate_manifests import _frontmatter_problem

    undecodable = tmp_path / "agent.md"
    undecodable.write_bytes(b"---\ndescription: \xff\xfe not utf-8\n---\nbody\n")
    assert _description_check()(undecodable) is False
    assert "UTF-8" in _frontmatter_problem(undecodable)

    missing = tmp_path / "absent.md"
    assert _description_check()(missing) is False
    assert "could not be read" in _frontmatter_problem(missing)

    # The reason string is load-bearing, so a correct description must produce no reason.
    good = tmp_path / "good.md"
    good.write_text("---\ndescription: a real description\n---\nbody\n")
    assert _frontmatter_problem(good) == ""
