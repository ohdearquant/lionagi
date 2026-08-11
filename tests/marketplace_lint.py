"""Marketplace skill content validation — parameterized over every .md file under marketplace/."""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import re
import sys
from pathlib import Path

import pytest

# File discovery

_REPO_ROOT = Path(__file__).parent.parent
_MARKETPLACE_ROOT = _REPO_ROOT / "marketplace"


def get_skill_files() -> list[Path]:
    """Return all .md files under marketplace/."""
    if not _MARKETPLACE_ROOT.is_dir():
        return []
    return sorted(_MARKETPLACE_ROOT.rglob("*.md"))


_SKILL_FILES = get_skill_files()

# Regexes

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

# Allowed sets

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


# Top-level `li` subcommands. The registry half is READ FROM THE CLI, not
# hand-listed here: a hand-maintained copy previously named eleven while the
# CLI registered twenty-three, falsely rejecting skills that correctly
# documented `li monitor` or `li runs`.
#
# The shims below can't be derived the same way -- each is dispatched by an
# `argv[0] == "..."` branch in main() ahead of argparse, so all three are
# absent from `li --help` and the typed CLI seed registry despite being real
# commands. This list is still hand-maintained (it was once short by `wait`,
# the same false-rejection failure mode), so it does not stand alone:
# test_pre_parse_shims_are_all_declared re-derives the branches from main()'s
# source and fails when a new one appears undeclared.
_PRE_PARSE_SHIMS: frozenset[str] = frozenset({"play", "skill", "wait"})


def _known_li_subcommands() -> frozenset[str]:
    from lionagi._auto import iter_cli_seeds

    names: set[str] = set()
    for seed in iter_cli_seeds():
        names.add(seed.name)
        names.update(seed.aliases)
    return frozenset(names) | _PRE_PARSE_SHIMS


_KNOWN_LI_SUBCOMMANDS: frozenset[str] = _known_li_subcommands()

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

# Helpers


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


# Tests


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


# Names compared against argv[0] in main(), which is how a pre-parse shim is
# dispatched. Both operators matter: the `play` branch tests `argv[0] != "play"`
# to bail out early, while `skill` and `wait` test `== `.
_ARGV0_EQ_RE = re.compile(r"_?argv\[0\]\s*(?:==|!=)\s*\"([a-z][a-z_-]*)\"")
_ARGV0_IN_RE = re.compile(r"_?argv\[0\]\s+in\s+\(([^)]*)\)")


def _shim_candidates_in(source: str) -> set[str]:
    """Every name main() dispatches on by comparing argv[0], from source text.

    Takes the text rather than reading the file so the extractor itself can be
    tested against a synthetic input. An extractor exercised only on the real
    source cannot be shown to fail.
    """
    names = {m.group(1) for m in _ARGV0_EQ_RE.finditer(source)}
    for m in _ARGV0_IN_RE.finditer(source):
        names.update(re.findall(r"\"([a-z][a-z_-]*)\"", m.group(1)))
    return names


def test_shim_extractor_finds_both_comparison_forms() -> None:
    """The guard below is only as good as this extractor, so prove it works.

    Without this, a regex that silently matched nothing would make the drift
    guard pass forever while detecting nothing.
    """
    synthetic = """
    if _argv and _argv[0] == "alpha":
        return run_alpha(_argv[1:])
    if not argv or argv[0] != "beta":
        return argv
    if _argv and _argv[0] in ("gamma", "g"):
        return run_gamma(_argv[1:])
    """
    assert _shim_candidates_in(synthetic) == {"alpha", "beta", "gamma", "g"}
    assert _shim_candidates_in("nothing to see here") == set()


def test_pre_parse_shims_are_all_declared() -> None:
    """Fail when main() gains a pre-parse shim that _PRE_PARSE_SHIMS does not name.

    `_KNOWN_LI_SUBCOMMANDS` derives its registry half from the CLI, but a shim
    is dispatched before argparse and appears in no registry, so that half
    cannot see one. The hand-written half was short by `wait` on its first
    outing, which made this check reject a skill that correctly documented
    `li wait` — the same false rejection the derived half had just been
    introduced to stop producing. This is the drift detector for the part that
    still has to be written by hand.
    """
    from importlib import import_module

    from lionagi._auto import iter_cli_seeds

    # `import_module` reaches the module unambiguously; `from lionagi.cli
    # import main` can resolve to the lazily-exported callable instead,
    # depending on which spelling a prior import in this process used first
    # (see tests/mcp/test_projection.py's attribute-vs-dotted-import probe).
    cli_main = import_module("lionagi.cli.main")

    source = Path(cli_main.__file__).read_text(encoding="utf-8")
    candidates = _shim_candidates_in(source)
    assert candidates, "extractor found no argv[0] comparisons in main.py at all"

    known_names: set[str] = set()
    for seed in iter_cli_seeds():
        known_names.add(seed.name)
        known_names.update(seed.aliases)

    # Names already in the registry are dispatched normally; an argv[0] check on
    # one of those is an interception of a SUBcommand (`li agent status`,
    # `li monitor run`), not a top-level shim.
    undeclared = candidates - frozenset(known_names) - _PRE_PARSE_SHIMS
    assert not undeclared, (
        "main() dispatches these on argv[0] but they are in neither the CLI "
        f"registry nor _PRE_PARSE_SHIMS: {sorted(undeclared)}. A skill "
        "documenting one would be reported as an unknown subcommand. Add them "
        "to _PRE_PARSE_SHIMS."
    )


# The verb name in a documented `{"op": "...", "args": {...}}` example. Written to
# take source text so the extractor can be tested against a synthetic input; an
# extractor exercised only on the real files cannot be shown to fail, and one that
# silently matched nothing would make the check below pass while reading nothing.
_OP_NAME_RE = re.compile(r"\"op\"\s*:\s*\"([a-z][a-z0-9_.]*)\"")


def _op_names_in(source: str) -> set[str]:
    return set(_OP_NAME_RE.findall(source))


def test_op_name_extractor_finds_quoted_ops() -> None:
    synthetic = """
    {"ops": [{"op": "play.submit", "args": {"playbook": "x"}}]}
    {"ops": [{ "op" : "job.wait", "args": {"run_ids": ["a"]}}]}
    the word op in prose, and "operation": "not.a.verb"
    """
    assert _op_names_in(synthetic) == {"play.submit", "job.wait"}
    assert _op_names_in("nothing to see here") == set()


def test_documented_mcp_verbs_are_runnable_on_the_published_server() -> None:
    """Every verb the bundle shows in an `op` position must be one the server runs.

    Two failure modes, both of which read as correct documentation: a verb
    that doesn't exist at all, and -- the one a plain catalog membership
    check would miss -- a verb the catalog names only to decline, with a
    reason (`team.send`, `invoke.start`: present in `help=true` output,
    refused when called). So membership is checked against the runnable
    registry and declined names are rejected explicitly, not by omission.

    The registry is read from the installed lionagi rather than listed here
    -- a hand-kept copy of someone else's catalog goes stale in whichever
    direction nobody is watching, which is how this file's own `li`
    subcommand list went wrong before.
    """
    from lionagi.mcp.verbs import ABSENT, VERBS

    runnable = frozenset(VERBS)
    declined = frozenset(a.name for a in ABSENT)
    assert runnable, "lionagi.mcp.verbs.VERBS is empty — the check would pass vacuously"
    assert declined, "lionagi.mcp.verbs.ABSENT is empty — the declined arm would never fire"

    documented: dict[str, list[str]] = {}
    for path in _SKILL_FILES:
        for verb in _op_names_in(_read(path)):
            documented.setdefault(verb, []).append(_rel(path))
    assert documented, 'no `"op": "..."` examples found under marketplace/ at all'

    for verb, where in sorted(documented.items()):
        assert verb not in declined, (
            f"{verb} is documented in {sorted(where)} but the published server names it "
            "as a verb it declines to run; a reader following the example gets a refusal"
        )
        assert verb in runnable, (
            f"{verb} is documented in {sorted(where)} but is not a verb the published "
            f"server runs. Runnable verbs: {sorted(runnable)}"
        )


def _op_objects_in(source: str) -> list[tuple[str, str]]:
    """Each documented op as (verb, the text of the object it appears in).

    The window runs from the `"op"` key to whichever comes first: the next `"op"`
    key, or the end of the enclosing fenced block. That is enough to tell a
    sibling key from a key belonging to the next op, without parsing markdown
    around JSON that is deliberately not valid JSON — the examples carry
    `<from help>` placeholders where a real value would go.
    """
    found: list[tuple[str, str]] = []
    matches = list(_OP_NAME_RE.finditer(source))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        window = source[m.start() : end]
        fence = window.find("\n```")
        if fence != -1:
            window = window[:fence]
        found.append((m.group(1), window))
    return found


def test_op_object_extractor_separates_siblings_from_the_next_op() -> None:
    """Prove the window logic before trusting the check below.

    The failure it has to avoid is reading the *next* op's fingerprint as this
    op's, which would pass a batch where only the last entry carried one.
    """
    synthetic = '```json\n{"ops": [\n'
    synthetic += '  {"op": "a.submit", "args": {}},\n'
    synthetic += '  {"op": "b.submit", "args": {}, "schema_fingerprint": "f"}\n'
    synthetic += "]}\n```\n"
    objects = _op_objects_in(synthetic)
    assert [verb for verb, _ in objects] == ["a.submit", "b.submit"]
    assert "schema_fingerprint" not in objects[0][1], (
        "the first op's window bled into the second's — the check would pass a "
        "batch where only the last op carried a fingerprint"
    )
    assert "schema_fingerprint" in objects[1][1]
    # A fingerprint after the fence belongs to no op inside it.
    leaked = '```json\n{"op": "c.submit", "args": {}}\n```\nschema_fingerprint in prose\n'
    assert "schema_fingerprint" not in _op_objects_in(leaked)[0][1]


# A quoted value in either spelling the bundle uses: `"x"` in JSON, `'x'` in the
# tool-call form. Backslashes are stripped before matching, so `\"x\"` inside a
# JSON string literal reads the same as `"x"`.
_QUOTED = r"[\"']([^\"']+)[\"']"
_KEYED = r"[\"']?{key}[\"']?\s*[:=]\s*" + _QUOTED


def _balanced_object(text: str, *, one_line: bool) -> str | None:
    """The brace-delimited object starting at *text*, or None if it does not close.

    Quote-aware: a `{` or `}` inside a value would otherwise unbalance the count
    and make a complete object read as unterminated, or an incomplete one read as
    closed. Returning None for anything that does not balance keeps every caller's
    failure loud rather than silently scoped to the wrong span.
    """
    body = text.split("\n", 1)[0] if one_line else text
    depth = 0
    quote: str | None = None
    for i, char in enumerate(body):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[: i + 1]
    return None


def _playbook_in_args(window: str) -> str | None:
    """The playbook an op names in its own `args`, or None.

    Read from the `args` object alone. Taking the first `"playbook"` anywhere in
    the window would instead read the one inside a `schema_fingerprint`
    placeholder whenever that key is written first, which turns a mismatch into a
    self-satisfying match. An `args` object that does not close inside the window
    yields None rather than a span reaching to the end of it, for the same reason;
    the rule below reports that case separately instead of skipping the example.
    """
    args_at = window.find('"args"')
    if args_at == -1:
        return None
    open_at = window.find("{", args_at)
    if open_at == -1:
        return None
    obj = _balanced_object(window[open_at:], one_line=False)
    if obj is None:
        return None
    m = re.search(_KEYED.format(key="playbook"), obj)
    return m.group(1) if m else None


# The two spellings the bundle actually uses for a help call whose argument
# is an object: `help={...}` as a parameter, and `"help": {...}` as a JSON
# key. Written as an enumeration of supported forms rather than "the letters
# `help` minus whatever I remembered to exclude" -- a subtractive pattern is
# unbounded (`nothelp`, `not-help` are fabricated sources wearing a real
# one's shape). Requiring the brace matters too: a `help` *string* field,
# which every playbook declares per argument, is followed by a quote and
# matches neither form.
#
# The parameter form's left boundary is "not a character a name is made of"
# (a closed set) rather than a list of separators to reject (not closed --
# enumerating allowed separators would have to cover every character the
# bundle's prose and code fences put in front of a call, and missing one
# rejects a real source).
_NAME_CHAR = r"A-Za-z0-9_.\-"
_HELP_OBJECT = re.compile(
    rf"""(?:
          [\"']help[\"']\s*:\s*\{{           # JSON key: "help": {{...}}
        | (?<![{_NAME_CHAR}])help\s*=\s*\{{  # parameter: help={{...}}
        )""",
    re.VERBOSE,
)


def _qualified_help_sources(source: str) -> set[tuple[str, str]]:
    """Every (verb, playbook) pair named together inside one `help` call.

    A call has to name both to be a usable source, so the pair is the unit,
    and both names must come from the *same* object -- reading them from
    anywhere on the line would let an unrelated `help` field sit next to
    sibling `verb`/`playbook` keys and fabricate a pair no call ever named,
    passing the rule below on an example the server then refuses.

    A call spanning lines is read as naming nothing (deliberately, rather
    than binding a playbook from the next line): the bundle writes these on
    one line, and a false negative is a visible failure while a false
    positive ships a fingerprint that doesn't resolve.
    """
    plain = source.replace("\\", "")
    pairs: set[tuple[str, str]] = set()
    for m in _HELP_OBJECT.finditer(plain):
        obj = _balanced_object(plain[m.end() - 1 :], one_line=True)
        if obj is None:
            continue
        verb = re.search(_KEYED.format(key="verb"), obj)
        playbook = re.search(_KEYED.format(key="playbook"), obj)
        if verb and playbook:
            pairs.add((verb.group(1), playbook.group(1)))
    return pairs


def test_args_playbook_extractor_ignores_a_playbook_named_elsewhere_in_the_op() -> None:
    """The op's own playbook must come from `args`, not from a placeholder.

    The failure this avoids: reading the playbook out of the fingerprint's own
    `<from help=...>` text, which would make every example agree with itself.
    """
    window = (
        '{"op": "play.submit", "schema_fingerprint": "<from help={\\"verb\\": '
        '\\"play.submit\\", \\"playbook\\": \\"other\\"}>", "args": {"playbook": "mine"}}'
    )
    assert _playbook_in_args(window) == "mine"
    assert _playbook_in_args('{"op": "play.submit", "args": {"prompt": "x"}}') is None


def test_qualified_help_extractor_reads_both_quote_spellings() -> None:
    """Both spellings appear in the shipped bundle, so both must be read.

    Reading only JSON double quotes misses the tool-call spelling, which the
    bundle uses for exactly this call — and a source it cannot see reads as a
    source that is not there.
    """
    assert _qualified_help_sources("help={'verb': 'play.submit', 'playbook': 'a'}") == {
        ("play.submit", "a")
    }
    assert _qualified_help_sources('help={"verb": "flow.submit", "playbook": "b"}') == {
        ("flow.submit", "b")
    }
    # Escaped, inside a JSON string literal.
    assert _qualified_help_sources(
        '"<from help={\\"verb\\": \\"play.submit\\", \\"playbook\\": \\"c\\"}>"'
    ) == {("play.submit", "c")}
    # A call naming only the verb is not a qualified source.
    assert _qualified_help_sources('help="play.submit"') == set()
    # A playbook named on the next line does not bind.
    assert _qualified_help_sources('help={"verb": "play.submit",\n "playbook": "d"}') == set()


def test_qualified_help_extractor_ignores_a_help_field_that_is_not_a_call() -> None:
    """`help` is also an ordinary field name, so a mention must not read as a call.

    Every playbook declares a `help` string per argument, and the bundle prints
    those blocks. Reading `verb` and `playbook` from anywhere on the line lets
    such a field stand in for a source that was never named — the pair is
    fabricated, the rule below passes, and the server refuses the example. Both
    names have to come from inside the call's own object.
    """
    # A JSON `help` string beside sibling keys: no call, so no pair.
    assert (
        _qualified_help_sources(
            '{"op": "play.submit", "args": {"help": "mode: dry | security", '
            '"verb": "play.submit", "playbook": "target"}}'
        )
        == set()
    )
    # The YAML form a playbook's own args block uses.
    assert _qualified_help_sources('  mode:\n    help: "audit mode"\n    playbook: x\n') == set()
    # An unterminated object names nothing rather than running past its line.
    assert _qualified_help_sources('help={"verb": "play.submit", "playbook": "a"') == set()
    # Two calls on one line are read separately, not merged into a third pair.
    assert _qualified_help_sources(
        'help={"verb": "play.submit", "playbook": "a"} or help={"verb": "flow.submit", '
        '"playbook": "b"}'
    ) == {("play.submit", "a"), ("flow.submit", "b")}
    # A name that merely ends in the letters is not the `help` parameter. The
    # separator cases matter as much as the run-together ones: a subtractive
    # pattern closes whichever of these it was shown and leaves the rest, so both
    # kinds are pinned here.
    for name in ("nothelp", "xhelp", "somehelp", "self_help", "not-help", "auto.help"):
        assert (
            _qualified_help_sources(
                f'{{"{name}": {{"verb": "play.submit", "playbook": "target"}}}}'
            )
            == set()
        ), name
        assert (
            _qualified_help_sources(f'{name}={{"verb": "play.submit", "playbook": "target"}}')
            == set()
        ), name
    # And the separators the bundle really puts in front of a call still read. A
    # boundary tight enough to reject the names above must not reject these.
    for lead in ("", " ", "`", "(", "from ", "- "):
        assert _qualified_help_sources(
            f'{lead}help={{"verb": "play.submit", "playbook": "target"}}'
        ) == {("play.submit", "target")}, repr(lead)


def test_the_object_reader_is_quote_aware_in_both_directions() -> None:
    """A brace inside a value must not decide where an object ends.

    Counting braces blind fails both ways: a `}` in a value closes a complete
    object early, and a `{` in one leaves a complete object reading as
    unterminated — which reports a documented source as no source at all.
    """
    assert _qualified_help_sources('help={"verb": "play.submit", "playbook": "a } b"}') == {
        ("play.submit", "a } b")
    }
    assert _qualified_help_sources('help={"verb": "play.submit", "playbook": "a { b"}') == {
        ("play.submit", "a { b")
    }
    # And on the args side, where the object legitimately spans lines.
    assert (
        _playbook_in_args('{"op": "play.submit", "args": {\n  "playbook": "a } b"\n}}') == "a } b"
    )
    # An args object that never closes yields nothing rather than a span that
    # reaches past it into a fingerprint placeholder.
    assert (
        _playbook_in_args(
            '{"op": "play.submit", "args": {"playbook": "mine", '
            '"schema_fingerprint": "<from help={\\"playbook\\": \\"other\\"}>"'
        )
        is None
    )


def test_documented_submit_ops_carry_a_schema_fingerprint() -> None:
    """Every documented `*.submit` op must show the fingerprint it has to carry.

    The server requires it as a **sibling of `args`** on every spawn verb and
    refuses an op without one. Nesting it inside `args` is worse: the key
    isn't read there, the identical refusal repeats, and the failure reads
    as idempotent rather than as a misplaced key -- so nesting is rejected
    here too. Which verbs need one is read from the server's own registry
    rather than listed, since "the spawn verbs" is a fact about the release,
    not about this file.
    """
    from lionagi.mcp.verbs import VERBS

    needs = frozenset(name for name, verb in VERBS.items() if verb.executor == "spawn")
    assert needs, "no spawn verbs found in the registry — the check would pass vacuously"

    missing: list[str] = []
    nested: list[str] = []
    checked = 0
    for path in _SKILL_FILES:
        for verb, window in _op_objects_in(_read(path)):
            if verb not in needs:
                continue
            checked += 1
            if "schema_fingerprint" not in window:
                missing.append(f"{_rel(path)}: {verb}")
                continue
            # A sibling sits at the object's own level. Inside `args` it is
            # preceded by the opening of `args` and not closed before it.
            args_at = window.find('"args"')
            fp_at = window.find("schema_fingerprint")
            if args_at != -1 and fp_at > args_at:
                between = window[args_at:fp_at]
                if between.count("{") > between.count("}"):
                    nested.append(f"{_rel(path)}: {verb}")

    assert checked, "no spawn-verb examples found under marketplace/ at all"
    assert not missing, (
        "these documented spawn ops omit the schema_fingerprint the server "
        f"requires, so a reader copying them gets a refusal and no run: {sorted(missing)}"
    )
    assert not nested, (
        "these documented spawn ops put schema_fingerprint inside `args`, where "
        f"it is not read; it must be a sibling of `args`: {sorted(nested)}"
    )


def test_a_playbook_qualified_schema_has_its_own_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the premise the check below rests on, rather than assuming it.

    A playbook-aware verb's schema resolves that playbook's own declared
    arguments, so naming one changes the schema and therefore the fingerprint.
    If that stopped being true the next check would still pass while guarding
    nothing, so the difference is asserted against a fixture written here.

    The fixture goes in a project-local `.lionagi/playbooks/` under a temporary
    cwd, which is the first place playbook resolution looks. Writing it into the
    real global directory instead would leave a stray playbook behind on any run
    that died between the write and the cleanup, and would collide with a
    genuine playbook of the same name.
    """
    import yaml

    from lionagi.mcp.dispatch import schema_fingerprint, verb_schema
    from lionagi.mcp.verbs import VERBS

    verb = VERBS["play.submit"]
    base = schema_fingerprint(verb_schema(verb))

    books = tmp_path / ".lionagi" / "playbooks"
    books.mkdir(parents=True)
    (books / "lint-fixture.playbook.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "lint-fixture",
                "description": "fixture for the fingerprint check",
                "prompt": "do the thing with {depth} and {target}",
                "args": {
                    "target": {"type": "str", "default": ".", "help": "what to act on"},
                    "depth": {"type": "int", "default": 1, "help": "how many passes"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    qualified = schema_fingerprint(verb_schema(verb, playbook="lint-fixture"))

    assert qualified != base, (
        "a playbook-qualified play.submit schema no longer differs from the base "
        "schema, so the qualified-source check below guards nothing. Either the "
        "playbook arguments stopped being resolved into the schema, or the "
        "fingerprint stopped covering them."
    )


def test_a_playbook_bearing_example_names_a_playbook_qualified_help_source() -> None:
    """A fingerprint from the wrong schema is refused as surely as a missing one.

    `play.submit`/`flow.submit` resolve the named playbook's own arguments
    into their schema, so the fingerprint differs per playbook. An example
    that names a playbook in `args` while pointing at an unqualified
    `help='play.submit'` yields `stale_schema`: a real fingerprint, from a
    real help call, for a different schema -- worse than a missing one, since
    the reader has no reason to suspect the value they copied. The check
    above can't see this (a fingerprint is present and correctly positioned),
    so both rules are needed.

    The source must be bound to the op by **both** names: requiring only that
    some playbook-qualified call appear nearby would pass an example whose
    source names a *different* playbook -- exactly the failure being
    guarded. So the (verb, playbook) pair from the help call must equal the
    pair the op itself names, checked in order: (1) the op's own
    `schema_fingerprint` value self-binds and needs nothing else; (2)
    otherwise the enclosing section, since a placeholder reading "from the
    help call above" is correct exactly when the call above it is the right
    one.

    Two residual limits, stated rather than closed: within one section this
    can't tell which of two correct-for-something calls a prose placeholder
    points at (closing that needs a structured per-example annotation, not a
    tighter pattern), and a help call written across lines names nothing, so
    an example relying on one is reported as having no source.
    """
    from lionagi.mcp.verbs import VERBS

    aware = frozenset(name for name, verb in VERBS.items() if verb.playbook_aware)
    assert aware, "no playbook-aware verbs in the registry — the check would pass vacuously"

    offenders: list[str] = []
    unreadable: list[str] = []
    checked = 0
    for path in _SKILL_FILES:
        text = _read(path)
        for verb, window in _op_objects_in(text):
            if verb not in aware:
                continue
            playbook = _playbook_in_args(window)
            start = text.find(window)
            lineno = text[:start].count("\n") + 1
            if playbook is None:
                # A playbook present in the window that the `args` object did not
                # yield is an example this rule cannot read, not one it has
                # cleared. Skipping it silently is how an uncovered example reads
                # as a covered one. The trigger accepts the same quote spellings
                # the reader does, so a form the reader would have understood
                # cannot slip through the gap between them.
                if re.search(r"[\"']playbook[\"']\s*[:=]", window):
                    unreadable.append(f"{_rel(path)}:{lineno} {verb}")
                continue
            checked += 1
            section = text.rfind("\n#", 0, start)
            scope = text[section if section != -1 else 0 : start + len(window)]
            if (verb, playbook) in _qualified_help_sources(window) or (
                verb,
                playbook,
            ) in _qualified_help_sources(scope):
                continue
            offenders.append(f"{_rel(path)}:{lineno} {verb} playbook={playbook!r}")

    assert checked, "no playbook-bearing spawn examples found under marketplace/ at all"
    assert not unreadable, (
        "these examples mention a playbook but this rule could not read it out of "
        "their `args` object, so they are unchecked rather than clear — rewrite the "
        "example or widen the reader: " + repr(sorted(unreadable))
    )
    assert not offenders, (
        "these examples name a playbook in `args` but no help call naming that same "
        "verb and playbook appears in their section, so a reader takes the "
        "fingerprint from a schema that is not the one the op resolves, and the op "
        "is refused with stale_schema: " + repr(sorted(offenders))
    )


# A prompt that names a workspace path or a worktree is asking the spawned run to
# read or write in a specific directory. The server resolves an omitted `cwd` to
# its own directory, not the caller's, so such an example starts the run where the
# files are not.
#
# The underscore-prefixed workspace convention is matched with or without a
# directory component, because the two spellings are the same reference: a rule
# that accepted `_intent.md` and not `_context/diff.txt` passed three examples that
# were wrong in exactly the way it existed to catch.
_PROMPT_NEEDS_CWD = re.compile(
    r"""(?:
          (?<![\w:/.])_[a-z_]+/[\w./-]+          # _context/diff.txt
        | (?<![\w:/.])_[a-z_]+\.(?:md|json|txt|ya?ml)   # _intent.md, _verdict.json
        | \bartifacts\b
        | \bworktree\b
        | <(?:play|show)_dir>
        )""",
    re.VERBOSE | re.IGNORECASE,
)


def test_a_spawn_example_whose_prompt_names_a_path_passes_cwd() -> None:
    """A run that must read the caller's files has to be told where they are.

    An omitted `cwd` resolves to the server's own directory, so an example
    whose prompt says to read `_intent.md` starts the run somewhere that
    file doesn't exist -- the run reports on evidence it never saw, a
    verdict formed from absence rather than an error.

    **This is a net, not a proof.** Prompts are prose, so there is no closed
    set of ways to say "read my files," unlike a help call, which has
    exactly two spellings and can be enumerated. This rule stops a *known*
    phrasing from regressing; it does not certify every example needing a
    `cwd` has one -- that check is the author's.

    Two known sites are of a different shape this rule doesn't see: a
    `prompt` whose whole value was `src/auth/`, and a playbook's typed
    `target` argument. Both are fixed in the bundle with an explicit `cwd`.
    A second rule was written for that shape and then removed: an argument
    whose *entire* value is a relative path is decidable in a way a path
    inside prose is not, but deciding which argument values an example
    passes means reading quoted strings out of JSON-ish documentation
    samples, and a regex cannot do that reliably -- successive attempts each
    missed a different edge case (a hand-written extension list's gaps, an
    array element, a filename containing `]`, an escaped quote). A real
    parser would close the class but would only cover one of the two known
    examples, since the other isn't valid JSON. A rule that advertises
    closure it doesn't have is worse than one that admits it's a net,
    because a reader stops checking -- so what guards this class now is this
    heuristic, the two fixed examples as documented pattern, and author
    judgment. If it regresses, write a parser or accept the limit; don't add
    a third regex.

    Most spawn examples legitimately omit `cwd` (a minimal quick-start
    doesn't need one), so requiring it everywhere would put a placeholder
    path in every teaching example -- hence a pattern over prompts, not a
    blanket requirement.
    """
    from lionagi.mcp.verbs import VERBS

    spawns = frozenset(name for name, verb in VERBS.items() if verb.executor == "spawn")
    assert spawns, "no spawn verbs in the registry — the check would pass vacuously"

    offenders: list[str] = []
    checked = 0
    for path in _SKILL_FILES:
        text = _read(path)
        for verb, window in _op_objects_in(text):
            if verb not in spawns or '"prompt"' not in window:
                continue
            if not _PROMPT_NEEDS_CWD.search(window):
                continue
            checked += 1
            if '"cwd"' in window:
                continue
            start = text.find(window)
            lineno = text[:start].count("\n") + 1
            offenders.append(f"{_rel(path)}:{lineno} {verb}")

    # Counting, not just non-emptiness: the pattern matching *one* example while
    # missing five is what happened when it accepted only bare filenames, and a
    # bare truthiness check cannot see that. The number is asserted low-bound so it
    # falls when a phrasing stops matching, which is the failure this pattern has
    # actually had.
    assert checked >= 8, (
        f"only {checked} spawn examples matched the path-bearing prompt pattern; the "
        "bundle had 8 when this bound was set, so the pattern has stopped seeing "
        "phrasings it used to catch"
    )
    assert not offenders, (
        "these examples tell the spawned run to read or write specific files but pass "
        "no `cwd`, so the run starts in the server's directory and reports on evidence "
        "it never saw: " + repr(sorted(offenders))
    )


def test_the_cwd_pattern_reads_both_workspace_path_spellings() -> None:
    """`_context/diff.txt` and `_intent.md` are the same reference, differently written.

    The first version of this pattern accepted the second and not the first, which
    passed three examples that told their workers to read a diff the run would not
    find. Both spellings are pinned, and so is a case that must not fire, since a
    pattern that matches everything demands a `cwd` on every teaching example.
    """
    assert _PROMPT_NEEDS_CWD.search("Diff is at _context/diff.txt.")
    assert _PROMPT_NEEDS_CWD.search("Acceptance criteria from _intent.md:")
    assert _PROMPT_NEEDS_CWD.search("artifacts saved to <play_dir>")
    assert not _PROMPT_NEEDS_CWD.search("what is a monad?")
    assert not _PROMPT_NEEDS_CWD.search("Review PR #123 for security only.")
    # An absolute path is already unambiguous, and a URL is not a workspace path.
    assert not _PROMPT_NEEDS_CWD.search("read /etc/hosts")
    assert not _PROMPT_NEEDS_CWD.search("see https://example.com/_context/diff.txt")


def test_the_qualified_source_check_rejects_a_source_for_a_different_playbook() -> None:
    """A qualified call for one playbook, an op naming another.

    Both names are present in the section, a fingerprint is present and
    correctly positioned, and the documented source resolves a different schema
    — so the reader gets `stale_schema` from a value that looked verified. A
    proximity check passes this; only name equality catches it.
    """
    subject = (
        "## A\n"
        '{"help": {"verb": "play.submit", "playbook": "other"}}\n'
        '{"ops": [{"op": "play.submit", "args": {"playbook": "target"}, '
        '"schema_fingerprint": "<from help=play.submit>"}]}\n'
    )
    verb, window = _op_objects_in(subject)[0]
    playbook = _playbook_in_args(window)
    assert (verb, playbook) == ("play.submit", "target")
    assert (verb, playbook) not in _qualified_help_sources(window)
    assert (verb, playbook) not in _qualified_help_sources(subject), (
        "a help call for a different playbook still satisfies the check, so the "
        "rule admits the failure it exists to catch"
    )
    # The same subject with the source corrected passes.
    fixed = subject.replace('"playbook": "other"', '"playbook": "target"')
    assert (verb, playbook) in _qualified_help_sources(fixed)


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


# Agent frontmatter grammar.
#
# validate_manifests.py hand-rolls its frontmatter grammar because ci.sh runs
# it on the bare system interpreter, where no YAML parser is available. This
# suite runs under uv, so it is where that grammar is pinned against a real
# parser. The grammar is narrower than YAML on purpose: the one invariant is
# that it must never accept a document a parser would reject; rejecting a
# form that is legal YAML is a judgement call, recorded below with a
# category from a closed set.
#
# The corpus below is a CROSS PRODUCT of fence x key x value forms, not a
# hand-picked list -- a hand-picked table reads as complete while omitting
# combinations nobody thought to write down.

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

    The whole document is handed to the parser, body included, deliberately:
    YAML's own ``---`` document splitting decides where the frontmatter ends,
    so the fence rules are checked by something other than the code being
    tested -- an oracle that split on fences using the grammar's own logic
    couldn't catch a fence defect at all, and this lane has found real ones.

    The price is a precondition: this is only a valid oracle for a document
    whose body is inert as YAML. Every generated corpus here uses the body
    ``body``, and ``_sweep_for_false_passes`` asserts that rather than
    trusting it, since a real markdown body routinely is not valid YAML (an
    asterisk in prose reads as an alias and the parser refuses the file).
    Use ``_parser_says_of_block`` for real files.
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

    This is the axis the corpora held constant elsewhere: everything else
    here varies *content* while terminating every line with ``\\n``, so a
    file using a different terminator was outside every other sweep, and a
    CR-only file is valid YAML whose frontmatter loads. This is not "a real
    file was rejected": ``Path.read_text`` applies universal newlines, so
    the **file** path never sees a lone CR, it arrives already translated.
    The defect was in the text function that every one of these sweeps
    measures, which was being held to something *stricter than the
    product*: the file path and the text-function path disagreed on CR-only
    input, and here the file path was right.

    Both directions are asserted for each mix, since the risk runs both
    ways: missing a terminator rejects a good file, and normalising too
    eagerly would accept a bad one. A CR *inside* a value must stay
    rejected, which is the case the last row covers.
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

    The validator reads descriptions under ``agents/`` only; skills are
    checked for existence and never parsed, so this is not a live failure --
    it's worth pinning anyway because the number is the argument: the
    grammar rejects **every** shipped skill file, not an unusual one. They
    all write ``description: >`` folded over several lines and carry an
    ``allowed-tools: [...]`` flow sequence, both recorded narrowings, which
    makes the folded-scalar narrowing much more expensive than the
    narrowing table alone suggests. This test fails the moment either of
    those things changes, turning a cost that is currently invisible into
    one that has to be dealt with deliberately.
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


# mcpServers gate.
#
# validate_manifests.py checks a plugin.json's mcpServers block is a dict and
# each entry inside it is too, before ever calling a dict method on either --
# a non-object value (a string, a list) is reported as FAIL rather than
# raising AttributeError from `.get(...)` on something that is not a dict.
# Both the per-plugin and standalone-scan branches share this gate function
# and are exercised end to end here, broken on purpose and then restored,
# since a clean run can't tell a check that passed from one that never
# looked.


def _mcp_gate():
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from validate_manifests import _mcp_servers_gate

    return _mcp_servers_gate


def _validator_main():
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from validate_manifests import main

    return main


def test_mcp_servers_gate_accepts_absent_and_empty() -> None:
    """No block and an empty block both mean 'nothing to check', not a failure."""
    gate = _mcp_gate()
    assert gate(None) == ({}, [])
    assert gate({}) == ({}, [])


@pytest.mark.parametrize("bad", [5, True], ids=type)
def test_mcp_servers_gate_rejects_non_object_top_level(bad: object) -> None:
    """A number or bool where mcpServers should be a string/array/object is
    reported by name and type, and nothing is left for the caller to iterate."""
    gate = _mcp_gate()
    usable, problems = gate(bad)
    assert usable == {}
    assert len(problems) == 1
    assert "'mcpServers' must be a string, an array, or an object" in problems[0]
    assert type(bad).__name__ in problems[0]


def test_mcp_servers_gate_drops_non_object_entries_and_keeps_the_rest() -> None:
    """A malformed entry is reported and excluded; a well-formed sibling survives
    so the stub check downstream still runs on it."""
    gate = _mcp_gate()
    usable, problems = gate({"lion": {"command": "uvx"}, "bad": "not-an-object", "worse": [1, 2]})
    assert usable == {"lion": {"command": "uvx"}}
    assert len(problems) == 2
    assert any("mcpServers['bad']" in p and p.endswith("got str") for p in problems)
    assert any("mcpServers['worse']" in p and p.endswith("got list") for p in problems)


def test_mcp_servers_gate_accepts_a_string_naming_an_external_config() -> None:
    """Claude Code documents a bare string as a valid form: a path to an
    external MCP config file. This validator used to reject it outright."""
    gate = _mcp_gate()
    assert gate("./mcp-config.json") == ({}, [])
    assert gate("stub-server") == ({}, [])


def test_mcp_servers_gate_accepts_an_array_of_config_paths() -> None:
    """Claude Code also documents an array of those strings as valid."""
    gate = _mcp_gate()
    assert gate(["./mcp-config.json"]) == ({}, [])
    assert gate(["a.json", "b.json"]) == ({}, [])


def test_mcp_servers_gate_rejects_a_non_string_array_element() -> None:
    """The array form is an array of config-path strings; anything else inside
    it is reported by index and type, the same shape as a bad object entry."""
    gate = _mcp_gate()
    usable, problems = gate(["ok.json", 5, {"nope": True}])
    assert usable == {}
    assert len(problems) == 2
    assert any(p == "mcpServers[1] must be a string, got int" for p in problems)
    assert any(p == "mcpServers[2] must be a string, got dict" for p in problems)


def test_mcp_servers_gate_rejects_an_empty_inline_entry() -> None:
    """`claude plugin validate` (2.1.220) refuses an inline entry with no
    configuration in it; this validator used to accept it silently."""
    gate = _mcp_gate()
    usable, problems = gate({"empty": {}})
    assert usable == {}
    assert problems == ["mcpServers['empty'] must not be empty"]


def test_mcp_servers_gate_keeps_a_well_formed_sibling_beside_an_empty_one() -> None:
    """Rejecting the empty entry does not cost the entries beside it."""
    gate = _mcp_gate()
    usable, problems = gate({"lion": {"command": "uvx"}, "empty": {}})
    assert usable == {"lion": {"command": "uvx"}}
    assert problems == ["mcpServers['empty'] must not be empty"]


def _write_marketplace_json(root: Path, plugins: list[dict]) -> None:
    manifest_dir = root / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "marketplace.json").write_text(
        json.dumps({"name": "x", "version": "1.0.0", "description": "d", "plugins": plugins})
    )


def _run_validator(main, repo_root: Path) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = main(repo_root=repo_root)
    return rc, stdout.getvalue()


def test_per_plugin_malformed_mcp_server_entry_fails_without_raising(tmp_path: Path) -> None:
    """The defect this pins: a string-valued mcpServers entry used to raise
    AttributeError from `.get("type")` inside the per-plugin branch of main(),
    because the entry's type was never checked before that call. Broken on
    purpose here with a non-object entry — the run must answer FAIL, never a
    traceback — then restored to a well-formed entry to prove the failure was
    about the malformed value and not something else in the fixture.
    """
    plugin_dir = tmp_path / "marketplace" / "p1"
    skill_dir = plugin_dir / "skills" / "hello"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: d\n---\nbody\n")
    plugin_json_dir = plugin_dir / ".claude-plugin"
    plugin_json_dir.mkdir(parents=True)
    plugin_json = plugin_json_dir / "plugin.json"
    plugin_json.write_text(
        json.dumps(
            {
                "name": "p1",
                "version": "1.0.0",
                "description": "d",
                "mcpServers": {"lion": "not-an-object"},
            }
        )
    )
    _write_marketplace_json(
        tmp_path, [{"name": "p1", "source": "./marketplace/p1", "description": "d"}]
    )

    main = _validator_main()
    rc, output = _run_validator(main, tmp_path)
    assert rc == 1
    assert "FAIL [p1]: plugin.json mcpServers['lion'] must be an object, got str" in output, output
    assert "Traceback" not in output

    plugin_json.write_text(
        json.dumps(
            {
                "name": "p1",
                "version": "1.0.0",
                "description": "d",
                "mcpServers": {"lion": {"command": "uvx"}},
            }
        )
    )
    rc, output = _run_validator(main, tmp_path)
    assert rc == 0
    assert "PASS [p1]" in output


def test_standalone_malformed_mcp_server_entry_fails_without_raising(tmp_path: Path) -> None:
    """The same defect class on the standalone-scan branch: a plugin.json not
    referenced by marketplace.json still has to answer FAIL rather than raise
    when one of its mcpServers entries is not an object.
    """
    _write_marketplace_json(tmp_path, [])
    plugin_dir = tmp_path / "marketplace" / "p2"
    plugin_json_dir = plugin_dir / ".claude-plugin"
    plugin_json_dir.mkdir(parents=True)
    plugin_json = plugin_json_dir / "plugin.json"
    plugin_json.write_text(
        json.dumps(
            {
                "name": "p2",
                "version": "1.0.0",
                "description": "d",
                "mcpServers": {"lion": ["not", "an", "object"]},
            }
        )
    )

    main = _validator_main()
    rc, output = _run_validator(main, tmp_path)
    assert rc == 1
    assert (
        "FAIL [standalone:p2]: plugin.json mcpServers['lion'] must be an object, got list" in output
    ), output
    assert "Traceback" not in output

    plugin_json.write_text(
        json.dumps(
            {
                "name": "p2",
                "version": "1.0.0",
                "description": "d",
                "mcpServers": {"lion": {"command": "uvx"}},
            }
        )
    )
    rc, output = _run_validator(main, tmp_path)
    assert rc == 0
    assert "PASS [standalone:p2]" in output
