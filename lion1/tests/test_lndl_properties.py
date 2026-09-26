import keyword
import math
import re

from hypothesis import assume, given
from hypothesis import strategies as st

from lionagi.lndl import Aliases, LNDLError, Pointer, Program, parse, parse_call_noting, plain, spans

COMMA = "a comma between arguments was missing and was supplied"
CLOSERS = "the call's closers were missing at its end and were supplied"

# Fragments of the notation and of what surrounds it, for text that is almost LNDL.
FRAGMENTS = [
    "<lvar ", "<lact ", "</lvar>", "</lact>", ">", "<", "a", "b.c", "note.k", "OUT{", "OUT {", "{", "}",
    "[", "]", "(", ")", ",", ":", "=", "*", "**", '"', "'", '"""', "'''", "\\", "=<<EOF\n", "<<'EOF'\n",
    "EOF", "EOF)", "\n", "\r\n", " ", "\t", "`", "```", "f(", "x=", "1", "-", "true", "#", "True", "\x00",
]  # fmt: skip


def _word(pattern: str) -> st.SearchStrategy[str]:
    return st.from_regex(pattern, fullmatch=True).filter(
        lambda s: not any(keyword.iskeyword(part) for part in s.split("."))
    )


ALIAS = _word(r"[a-z][a-z0-9_]{0,5}")
KEY = _word(r"[a-z][a-z0-9_]{0,5}")
NAME = _word(r"[a-z][a-z0-9_]{0,5}(\.[a-z][a-z0-9_]{0,4})?")
TAG = st.sampled_from(["EOF", "PY", "SH", "END", "A"])


def _chars(exclude: str) -> st.SearchStrategy[str]:
    return st.characters(blacklist_characters=exclude, blacklist_categories=("Cs",))


def _no_marker(s: str) -> bool:
    # a string spelled like the reader's internal placeholders is a known defect, tested on its own
    return not s.startswith("\x00")


# Prose never opens a construct: no `<`, no backtick (markdown code quotes what it holds), no `OUT`.
PROSE = st.text(_chars("<`")).filter(lambda s: "OUT" not in s)


def _literals(strings: st.SearchStrategy[str], dicts: bool = True) -> st.SearchStrategy:
    scalars = (
        st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | strings
    )

    def containers(inner):
        found = st.lists(inner, max_size=3) | st.lists(inner, max_size=3).map(tuple)
        return found | st.dictionaries(strings, inner, max_size=3) if dicts else found

    return st.recursive(scalars, containers, max_leaves=6)


# Backticks in strings and values are left out: see the known defects on backticks.
STRINGS = st.text(_chars("`")).filter(_no_marker)
LITERALS = _literals(STRINGS)


def _same(a, b) -> bool:
    """Equal and of the same types all the way down: `True == 1` and `[1] == (1,)` are not the same."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float):
        return a == b and math.copysign(1, a) == math.copysign(1, b)
    return a == b


@st.composite
def heredoc_bodies(draw, tag: str, exclude: str = ""):
    """Lines with none that could be the closing line (the tag at a line's start), and none ending in
    a carriage return, so the text is the same body written with LF or with CRLF."""
    line = st.text(_chars("\n" + exclude)).filter(lambda s: not s.endswith("\r"))
    body = "\n".join(draw(st.lists(line, max_size=5)))
    assume(not re.search(rf"^[ \t]*{tag}(?!\w)", body, re.MULTILINE))
    return body


@st.composite
def arguments(draw, strings, exclude):
    """(source, value) for one argument: a literal, a pointer, or a heredoc."""
    kind = draw(st.sampled_from(["literal", "pointer", "heredoc"]))
    if kind == "literal":
        value = draw(_literals(strings))
        return repr(value), value
    if kind == "pointer":
        name = draw(NAME)
        return f"*{name}", Pointer(name)
    tag = draw(TAG)
    body = draw(heredoc_bodies(tag, exclude))
    return f"<<{tag}\n{body}\n{tag}", body


@st.composite
def calls(draw, strings=STRINGS, exclude=""):
    """(text, name, args) for a well-formed call; a heredoc's closing line is followed by `,` or `)`."""
    name = draw(NAME)
    args, parts = {}, []
    for key in draw(st.lists(KEY, max_size=4, unique=True)):
        source, value = draw(arguments(strings, exclude))
        args[key] = value
        parts.append(f"{key}={source}")
    return f"{name}({', '.join(parts)})", name, args


@st.composite
def turns(draw, plain_safe: bool = False):
    """A well-formed turn, prose around value tags, calls and at most one OUT{}, with what it holds.
    `plain_safe` keeps closing tags and braces out of the constructs' insides."""
    strings = STRINGS.filter(lambda s: not ({"<", "{", "}"} & set(s))) if plain_safe else STRINGS
    exclude = "<" if plain_safe else ""
    aliases = draw(st.lists(ALIAS, max_size=4, unique=True))
    constructs, lvars, lacts = [], [], []
    for alias in aliases:
        if draw(st.booleans()):
            value = draw(st.text(_chars("<`")))  # a value's body holds no tag
            constructs.append(f"<lvar {alias}>{value}</lvar>")
            lvars.append((alias, value.strip()))
        else:
            text, name, args = draw(calls(strings, exclude))
            constructs.append(f"<lact {alias}>{text}</lact>")
            lacts.append((alias, name, args))
    out = None
    if draw(st.booleans()):
        fields, parts = {}, []
        for key in draw(st.lists(KEY, min_size=1, max_size=3, unique=True)):
            if aliases and draw(st.booleans()):
                named = draw(st.lists(st.sampled_from(aliases), min_size=1, max_size=3))
                fields[key] = Aliases(named)
                bare = len(named) == 1 and draw(st.booleans())
                parts.append(f"{key}: {named[0]}" if bare else f"{key}: [{', '.join(named)}]")
            else:
                value = draw(_literals(strings, dicts=not plain_safe))
                fields[key] = value
                parts.append(f"{key}: {value!r}")
        out = fields
        constructs.insert(draw(st.integers(0, len(constructs))), "OUT{" + ", ".join(parts) + "}")
    prose = draw(st.lists(PROSE, min_size=len(constructs) + 1, max_size=len(constructs) + 1))
    text = prose[0] + "".join(f"\n{c}\n{p}" for c, p in zip(constructs, prose[1:], strict=True))
    return text, lvars, lacts, out, prose


def _same_out(got: dict, expected: dict) -> bool:
    if got.keys() != expected.keys():
        return False
    for key, value in expected.items():
        if isinstance(value, Aliases):
            if not (isinstance(got[key], Aliases) and list(got[key]) == list(value)):
                return False
        elif isinstance(got[key], Aliases) or not _same(got[key], value):
            return False
    return True


@given(st.one_of(st.text(), st.lists(st.sampled_from(FRAGMENTS), max_size=40).map("".join)))
def test_any_text_is_a_program_or_an_lndl_error(text):
    try:
        program = parse(text)
    except LNDLError:
        pass
    else:
        assert isinstance(program, Program)
        assert all(isinstance(p, str) for p in program.problems)
    # the prose view and the construct scan never fail either
    assert isinstance(plain(text), str)
    assert all(0 <= a < b <= len(text) for a, b in spans(text))


@given(turns())
def test_a_well_formed_turn_reads_back_exactly_what_was_written(turn):
    text, lvars, lacts, out, _ = turn
    p = parse(text)
    assert p.problems == []
    assert [(v.alias, v.value) for v in p.lvars] == lvars
    assert [(a.alias, a.name) for a in p.lacts] == [(alias, name) for alias, name, _ in lacts]
    for got, (_, _, args) in zip(p.lacts, lacts, strict=True):
        assert _same(got.args, args), (got.args, args)
    assert (p.out is None) if out is None else (p.out is not None and _same_out(p.out.fields, out))


@given(TAG.flatmap(lambda tag: st.tuples(st.just(tag), heredoc_bodies(tag))), st.sampled_from(["\n", "\r\n"]))
def test_a_heredoc_body_is_verbatim_and_crlf_reads_as_its_lf_lines(tagged, newline):
    # ADR-0004/C2
    tag, body = tagged
    written = body.replace("\n", newline)
    p = parse(f"<lact r>run(cmd=<<{tag}{newline}{written}{newline}{tag}, n=1)</lact>")
    assert p.lacts[0].args == {"cmd": body, "n": 1} and p.problems == []


# text a string literal may hold that would be a construct anywhere else
INSIDE = st.lists(
    st.sampled_from(
        [
            "<lact b>g(y=1)</lact>",
            "<lvar c>v</lvar>",
            "</lact>",
            "</lvar>",
            "OUT{x: 1}",
            "OUT{",
            "}",
            "{",
            "=<<EOF\n",
            "EOF)",
            "*a",
            ",",
            ")",
            "(",
            '"',
            "'",
            "\\",
            "\n",
            " ",
            "x",
        ]
    ),  # fmt: skip
    max_size=12,
).map("".join)


@given(INSIDE, PROSE)
def test_a_string_literals_contents_are_never_read_for_tags_or_out(inside, prose):
    # ADR-0004/C1
    p = parse(f"{prose}\n<lact a>f(x={inside!r}, y=1)</lact>\n{prose}")
    assert [(a.alias, a.args) for a in p.lacts] == [("a", {"x": inside, "y": 1})]
    assert p.lvars == [] and p.out is None and p.problems == []
    p = parse(f"{prose}\nOUT{{answer: {inside!r}, other: {{'k': {inside!r}}}}}\n{prose}")
    assert p.lacts == [] and p.lvars == [] and p.problems == []
    assert p.out.fields == {"answer": inside, "other": {"k": inside}}


@given(INSIDE.filter(lambda s: '"""' not in s and "\\" not in s and not s.endswith('"')))
def test_a_triple_quoted_literal_holding_line_breaks_is_never_read_for_tags_or_out(inside):
    p = parse(f'<lact a>f(x="""{inside}""", y=1)</lact>')
    assert [(a.alias, a.args) for a in p.lacts] == [("a", {"x": inside, "y": 1})] and p.problems == []


@given(turns(plain_safe=True))
def test_plain_keeps_the_prose_lines_and_holds_no_construct(turn):
    text, _, _, _, prose = turn
    view = plain(text)
    expected = [" ".join(line.split()) for piece in prose for line in piece.splitlines() if line.split()]
    assert [line for line in view.splitlines() if line] == expected
    assert "\n\n\n" not in view and view == view.strip()
    again = parse(view)
    assert again.lvars == [] and again.lacts == [] and again.out is None


@st.composite
def slipped_calls(draw):
    """A call with one slip a model makes, the call it stands for, and the repair that reads it."""
    name = draw(NAME)
    keys = draw(st.lists(KEY, min_size=2, max_size=4, unique=True))
    values = [draw(LITERALS) for _ in keys]
    args = dict(zip(keys, values, strict=True))
    parts = [f"{k}={v!r}" for k, v in args.items()]
    slip = draw(st.sampled_from(["comma", "closers", "string"]))
    if slip == "comma":
        # the comma is supplied after a value that ends in a string, a number or a closing bracket
        spots = [i for i, v in enumerate(values[:-1]) if v is not None and not isinstance(v, bool)]
        assume(spots)
        i = draw(st.sampled_from(spots))
        return f"{name}({', '.join(parts[: i + 1])} {', '.join(parts[i + 1 :])})", name, args, COMMA
    if slip == "closers":
        return f"{name}({', '.join(parts)}", name, args, CLOSERS
    # a string argument's closing quote left off along with the call's closer; whitespace at the end
    # of such a string is a known defect, tested on its own
    last = draw(STRINGS.filter(lambda s: s == s.rstrip()))
    args[keys[-1]] = last
    parts[-1] = f"{keys[-1]}={last!r}"
    return f"{name}({', '.join(parts)}"[:-1], name, args, CLOSERS


@given(slipped_calls())
def test_every_repair_the_reader_makes_is_named(slipped):
    # ADR-0004/C3: a repaired call runs, and the turn is told what was supplied
    text, name, args, note = slipped
    got_name, got_args, got_note = parse_call_noting(text)
    assert got_name == name and _same(got_args, args) and got_note == note
    p = parse(f"<lact a>{text}</lact>")
    assert p.problems == [f"<lact a>: {note}; if that is not the call you meant, write it again"]
