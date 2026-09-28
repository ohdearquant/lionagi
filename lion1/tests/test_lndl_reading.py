import re
import sys

import pytest

from lionagi.lndl import (
    Aliases,
    LNDLError,
    Lvar,
    Pointer,
    Program,
    parse,
    parse_call,
    parse_call_noting,
    parse_out,
    plain,
    pointers_in,
    spans,
    stray_tags,
)

COMMA = "a comma between arguments was missing and was supplied"
CLOSERS = "the call's closers were missing at its end and were supplied"
REWRITE = "if that is not the call you meant, write it again"
HEREDOC_HINT = (
    ". A multi-line value, or one with quotes in it, is written as a heredoc argument: "
    "cmd=<<EOF, then the lines verbatim, then EOF alone on a line, nothing escaped"
)


def _repaired(alias: str, note: str) -> str:
    return f"<lact {alias}>: {note}; {REWRITE}"


def _calls(text: str) -> list[tuple[str, dict]]:
    return [(la.alias, la.args) for la in parse(text).lacts]


# headers


def test_a_bare_alias_has_no_spec_and_no_field():
    assert parse("<lvar a1>x</lvar>").lvars == [Lvar(alias="a1", value="x", spec=None, field=None)]


def test_a_note_alias_is_one_alias_whatever_its_dots():
    p = parse("<lvar note.k>x</lvar><lvar note.plan.v2>y</lvar>")
    assert [(v.alias, v.spec, v.field) for v in p.lvars] == [
        ("note.k", None, None),
        ("note.plan.v2", None, None),
    ]
    # only the exact `note.` prefix is the note namespace
    p = parse("<lvar notes.k>x</lvar><lvar NOTE.k>y</lvar>")
    assert [(v.alias, v.spec, v.field) for v in p.lvars] == [
        ("notes.k", "notes", "k"),
        ("NOTE.k", "NOTE", "k"),
    ]


def test_a_single_dotted_header_is_the_alias_and_names_its_field():
    p = parse("<lvar report.summary>x</lvar><lact out.body.text>fetch(url='u')</lact>")
    assert p.lvars == [Lvar(alias="report.summary", value="x", spec="report", field="summary")]
    assert [(a.alias, a.spec, a.field) for a in p.lacts] == [("out.body.text", "out", "body.text")]


def test_a_spec_field_alias_header_splits_at_the_first_dot():
    p = parse("<lvar a.b.c d>1</lvar>")
    assert p.lvars == [Lvar(alias="d", value="1", spec="a", field="b.c")]


def test_attribute_style_headers_name_the_alias():
    p = parse("<lvar id=x>1</lvar><lvar name='y'>2</lvar><lvar alias=\"z\">3</lvar>")
    assert [(v.alias, v.value) for v in p.lvars] == [("x", "1"), ("y", "2"), ("z", "3")]


@pytest.mark.parametrize("header", ["a b", "a b c", "a.b c d"])
def test_a_header_that_is_not_alias_or_spec_field_alias_is_prose(header):
    p = parse(f"<lvar {header}>x</lvar> <lvar ok>y</lvar>")
    assert [v.alias for v in p.lvars] == ["ok"]
    assert p.problems == [
        f"<lvar {header}>: the header is `alias` or `spec.field alias`, space separated; it was read as prose"
    ]
    # a header of spaces only is no header either
    assert parse("<lvar   >x</lvar>").problems == [
        "<lvar  >: the header is `alias` or `spec.field alias`, space separated; it was read as prose"
    ]


def test_an_alias_that_is_not_an_identifier_is_prose():
    p = parse("<lvar 1a>x</lvar> <lvar a-b>y</lvar>")
    assert p.lvars == []
    assert p.problems == [
        "<lvar 1a>: '1a' is not a valid alias; it was read as prose",
        "<lvar a-b>: 'a-b' is not a valid alias; it was read as prose",
    ]


def test_a_program_names_every_alias_it_declares():
    p = parse("<lvar a>x</lvar><lact b>f(x=1)</lact><lact c>echo hi</lact><lvar note.n>y</lvar>")
    assert p.aliases == {"a", "b", "c", "note.n"}
    assert parse("no constructs").aliases == set()


# pointers


def test_pointers_are_found_in_every_container_in_order():
    value = {"a": Pointer("x"), "b": [Pointer("y"), (1, Pointer("z"))], "c": {"d": {"e": Pointer("w")}}}
    assert pointers_in(value) == [Pointer("x"), Pointer("y"), Pointer("z"), Pointer("w")]
    p = parse("<lact s>send(body={'to': *who, 'cc': [*a, (*b, 2)]})</lact>")
    assert p.lacts[0].pointers() == [Pointer("who"), Pointer("a"), Pointer("b")]


def test_a_pointer_in_a_tuple_stays_in_a_tuple():
    p = parse("<lact a>f(x=(1, *b, 'c'), y=(*d,))</lact>")
    assert p.lacts[0].args == {"x": (1, Pointer("b"), "c"), "y": (Pointer("d"),)}


def test_a_dotted_pointer_is_one_name():
    assert parse_call("f(x=*note.a.b, y=[*r.c])") == ("f", {"x": Pointer("note.a.b"), "y": [Pointer("r.c")]})


def test_a_pointer_on_a_later_line_of_a_call_is_read():
    p = parse('<lact a>send(\n  to="x",\n  body=*b,\n  cc=[*c, *d])</lact>')
    assert p.lacts[0].args == {"to": "x", "body": Pointer("b"), "cc": [Pointer("c"), Pointer("d")]}
    assert p.problems == []


@pytest.mark.parametrize(
    "body",
    [
        "f(x=3*n)",  # a product, not a pointer
        "f(x=(1)*n)",
        "f(x=[1]*n)",
        "f(x=[*[1, 2]])",  # a star before a bracket
        # a pointer after such a star is still read: unread, `{'k': *a}` would not even parse
        "f(x=[*[1]], y={'k': *a})",
        "f(x=[1]*2, y={'k': *a})",
        "f(x=3*n, y={'k': *a})",
        "f(x=(1)*n, y={'k': *a})",
    ],
)
def test_a_star_that_is_not_a_pointer_leaves_the_argument_not_a_literal(body):
    with pytest.raises(LNDLError) as e:
        parse_call(body)
    assert str(e.value) == "f(...): x= must be a literal (string, number, list, dict) or a *pointer"


def test_a_star_with_a_space_after_it_is_not_a_pointer():
    with pytest.raises(LNDLError, match=r"^cannot read the call 'f\(x=\* a\)'"):
        parse_call("f(x=* a)")


def test_a_literal_that_looks_like_the_heredoc_marker_is_a_string_nested_too():
    p = parse(
        '<lact a>echo(text=["\\x00doc:0"], t=("\\x00doc:1",), '
        'd={"k": "\\x00doc:0", "j": "\\x00doc:x"})</lact>'
    )
    assert p.lacts[0].args == {
        "text": ["\x00doc:0"],
        "t": ("\x00doc:1",),
        "d": {"k": "\x00doc:0", "j": "\x00doc:x"},
    }


# the call


def test_parse_call_is_parse_call_noting_without_the_repairs():
    assert parse_call('run(cmd="ls" timeout=1)') == ("run", {"cmd": "ls", "timeout": 1})
    assert parse_call_noting('run(cmd="ls" timeout=1)') == ("run", {"cmd": "ls", "timeout": 1}, ("comma",))
    assert parse_call_noting('run(cmd="ls")') == ("run", {"cmd": "ls"}, ())


def test_a_dotted_callee_is_the_name():
    assert parse_call("context.hide(refs=['_r1'])") == ("context.hide", {"refs": ["_r1"]})
    assert parse_call("a.b.c()") == ("a.b.c", {})


@pytest.mark.parametrize(
    "body, message",
    [
        ("f(x=1).y", "'f(x=1).y' is not a call: write name(field=value, ...)"),
        ("f(x=1)[0]", "'f(x=1)[0]' is not a call: write name(field=value, ...)"),
        ("f(x=1)(y=2)", "'f(x=1)(y=2)': the callee must be a name"),
        ("f(x=1).g(y=2)", "'f(x=1).g(y=2)': the callee must be a name"),
        ("f(**{'a': 1})", "f(...): ** unpacking is not allowed"),
        ("f(1)", "f(...): arguments are keyword only, write field=value"),
        ("f(x={[]: 1})", "f(...): x= must be a literal (string, number, list, dict) or a *pointer"),
    ],
)
def test_a_call_that_reads_but_is_not_a_request_says_why(body, message):
    with pytest.raises(LNDLError) as e:
        parse_call(body)
    assert str(e.value) == message
    with pytest.raises(LNDLError) as e:
        parse(f"<lact a>{body}</lact>")
    assert str(e.value) == message


def test_a_callee_that_is_not_a_name_is_refused_even_when_the_text_does_not_look_like_a_call():
    with pytest.raises(LNDLError) as e:
        parse_call("a[0].b(x=1)")
    assert str(e.value) == "'a[0].b(x=1)': the callee must be a name"
    # through a turn it is not call-shaped, so it is a value
    assert parse("<lact a>a[0].b(x=1)</lact>").lvars == [Lvar(alias="a", value="a[0].b(x=1)")]


@pytest.mark.parametrize("body", ["[1", '"abc'])
def test_text_that_is_not_a_call_gets_its_closers_and_is_then_refused(body):
    with pytest.raises(LNDLError, match="is not a call"):
        parse_call(body)


@pytest.mark.parametrize(
    "body, hinted",
    [
        ("run(x=)", False),  # a plain syntax error: no hint
        ("run(x=1, y=)", False),
        ("run(x=(1 2", True),  # a bracket never closed
        ('run(cmd="ls", y=1 2, z="a', True),  # a string never closed, on one line
        ("run(cmd=python -c\nimport x)", True),  # a line break in the call
    ],
)
def test_an_unreadable_call_names_its_syntax_error_and_the_heredoc_route_when_it_helps(body, hinted):
    with pytest.raises(LNDLError) as e:
        parse_call(body)
    cause = e.value.__cause__
    assert isinstance(cause, SyntaxError)
    assert str(e.value) == f"cannot read the call {body!r}: {cause.msg}{HEREDOC_HINT if hinted else ''}"


def test_an_unreadable_call_quotes_at_most_its_first_160_characters():
    body = "run(cmd=" + "x" * 200 + " y)"
    with pytest.raises(LNDLError) as e:
        parse_call(body)
    assert str(e.value).startswith(f"cannot read the call {body[:160]!r}: ")


# repairs, each named once and exactly


def test_a_missing_comma_is_supplied_after_a_string_a_number_or_a_bracket():
    p = parse("<lact r>run(a=\"x\" b=1 c=[2] d={'e': 3} f=(4,) g=5)</lact>")
    assert p.lacts[0].args == {"a": "x", "b": 1, "c": [2], "d": {"e": 3}, "f": (4,), "g": 5}
    assert p.problems == [_repaired("r", COMMA)]


def test_a_missing_comma_is_found_across_a_line_break_and_a_comment():
    p = parse('<lact r>run(cmd="ls"  # list\n    timeout=1)</lact>')
    assert p.lacts[0].args == {"cmd": "ls", "timeout": 1}
    assert p.problems == [_repaired("r", COMMA)]


def test_missing_closers_are_supplied_in_order():
    p = parse('<lact a>f(x=[{"a": (1, "b</lact>')
    assert p.lacts[0].args == {"x": [{"a": (1, "b")}]}
    assert p.problems == [_repaired("a", CLOSERS)]


def test_a_string_left_open_is_closed_with_its_own_quote():
    for text, value in (
        ('<lact e>edit(new="say \\"hi\\" to</lact>', 'say "hi" to'),
        ("<lact e>edit(new='it\\'s</lact>", "it's"),
        ('<lact e>edit(old="C:\\\\", new="x</lact>', None),
        ('<lact e>edit(new="""abc\ndef</lact>', "abc\ndef"),
        ("<lact e>edit(new='''abc\ndef</lact>", "abc\ndef"),
        ('<lact e>edit(old="""a""", new="b</lact>', None),
        ('<lact e>edit(old="""""", new="b</lact>', None),
        ('<lact e>edit(old="""""", new=\'b</lact>', None),
    ):
        p = parse(text)
        assert p.problems == [_repaired("e", CLOSERS)], text
        if value is not None:
            assert p.lacts[0].args == {"new": value}, text
    assert _calls('<lact e>edit(old="C:\\\\", new="x</lact>') == [("e", {"old": "C:\\", "new": "x"})]
    assert _calls('<lact e>edit(old="""a""", new="b</lact>') == [("e", {"old": "a", "new": "b"})]
    assert _calls('<lact e>edit(old="""""", new="b</lact>') == [("e", {"old": "", "new": "b"})]
    # an empty triple-quoted string is closed right where it opens, so the quote left open is the `'`
    assert _calls('<lact e>edit(old="""""", new=\'b</lact>') == [("e", {"old": "", "new": "b"})]


def test_a_complete_triple_quoted_string_then_an_open_paren_is_closed_after_it():
    p = parse('<lact e>edit(new="""a"""</lact>')
    assert p.lacts[0].args == {"new": "a"} and p.problems == [_repaired("e", CLOSERS)]


def test_a_triple_quoted_string_left_open_takes_the_rest_of_the_call():
    # the `)` written before the closing tag sits inside the open string, so it is text
    p = parse('<lact a>f(x="""abc\ndef)</lact>')
    assert p.lacts[0].args == {"x": "abc\ndef)"} and p.problems == [_repaired("a", CLOSERS)]


def test_line_breaks_in_strings_are_escaped_and_the_call_keeps_its_own_lines():
    p = parse('<lact e>edit(new="a\nb",\n     old="c")</lact>')
    assert p.lacts[0].name == "edit"
    assert p.lacts[0].args == {"new": "a\nb", "old": "c"}
    assert p.problems == []  # the value is what was written, so nothing is named (ADR-0004/C3)
    p = parse("<lact e>edit(new='x\ny', path='p')</lact>")
    assert p.lacts[0].args == {"new": "x\ny", "path": "p"}
    assert p.problems == []


def test_a_triple_quoted_string_is_copied_whole_while_line_breaks_elsewhere_are_escaped():
    for text, value in (
        ('<lact e>edit(old="""say "hi\n""", new="a\nb")</lact>', 'say "hi\n'),
        ("<lact e>edit(old='''it's\n''', new=\"a\nb\")</lact>", "it's\n"),
        ('<lact e>edit(old="""a\\"""b\nc""", new="a\nb")</lact>', 'a"""b\nc'),
        ('<lact e>edit(old="""x\\\\""", new="a\nb")</lact>', "x\\"),
        ('<lact e>edit(old="""abc""", new="a\nb")</lact>', "abc"),
        ('<lact e>edit(old="""ab""", new="a\nb")</lact>', "ab"),
        ('<lact e>edit(old="""""", new="a\nb")</lact>', ""),
        ('<lact e>edit(old="""a""" """b""", new="a\nb")</lact>', "ab"),
        ('<lact e>edit(old="""say "hi" ok""",\n     new="a\nb")</lact>', 'say "hi" ok'),
        ("<lact e>edit(old='''it's''',\n     new=\"a\nb\")</lact>", "it's"),
    ):
        p = parse(text)
        assert p.lacts[0].args == {"old": value, "new": "a\nb"}, text


def test_a_string_directly_after_a_triple_quoted_one_is_still_read_as_a_string():
    p = parse('<lact e>edit(old="""a""""b\nc", new="d")</lact>')
    assert p.lacts[0].args == {"old": "ab\nc", "new": "d"}


@pytest.mark.parametrize(
    "text, args",
    [
        ("<lact r>run(n=0X1F, cmd=<<EOF\nls\nEOF)</lact>", {"n": 31, "cmd": "ls"}),
        ('<lact r>run(n=0X1F,\n    cmd="a\nb")</lact>', {"n": 31, "cmd": "a\nb"}),
        ("<lact r>run(n=0X1F, cmd='ls'</lact>", {"n": 31, "cmd": "ls"}),
        ("<lact r>run(cmd='ls', n=0X1F</lact>", {"cmd": "ls", "n": 31}),
        ("<lact r>run(n=0X1F, m=0X2)</lact>", {"n": 31, "m": 2}),
    ],
)
def test_a_hex_literal_is_a_number_to_every_scan(text, args):
    assert _calls(text) == [("r", args)]


def test_a_hex_literal_in_out_is_a_number():
    assert parse("OUT{n: 0X1F, m: [0X2]}").out.fields == {"n": 31, "m": [2]}


def test_json_words_inside_containers_are_python_literals():
    assert parse_call("f(x={'a': true, 'b': [null, false]})") == ("f", {"x": {"a": True, "b": [None, False]}})


@pytest.mark.xfail(
    sys.version_info < (3, 12),
    raises=LNDLError,
    strict=True,
    reason="known defect: on 3.11 an invalid escape warns as DeprecationWarning, which the reader does "
    "not silence, so under warnings-as-errors (this suite's setting) the call is unreadable",
)
def test_a_regex_escape_in_a_string_argument_is_kept_as_written():
    assert parse_call('grep(pattern="\\s+\\d")') == ("grep", {"pattern": "\\s+\\d"})


# heredocs


def test_two_heredocs_with_the_same_tag_in_one_call_are_two_values():
    p = parse("<lact e>edit(old=<<EOF\none\nEOF, new=<<EOF\ntwo\nEOF)</lact>")
    assert p.lacts[0].args == {"old": "one", "new": "two"} and p.problems == []


def test_an_escaped_quote_before_a_heredoc_does_not_end_its_string():
    p = parse('<lact e>edit(old="a \\" b", new=<<EOF\nhi\nEOF)</lact>')
    assert p.lacts[0].args == {"old": 'a " b', "new": "hi"}


def test_a_heredoc_opening_inside_a_triple_quoted_string_with_quotes_in_it_is_text():
    p = parse('<lact n>note(text="""say "x=<<EOF\nhi\nEOF" please""")</lact>')
    assert p.lacts[0].args == {"text": 'say "x=<<EOF\nhi\nEOF" please'}
    p = parse("<lact n>note(text='''it's x=<<EOF\nhi\nEOF ok''')</lact>")
    assert p.lacts[0].args == {"text": "it's x=<<EOF\nhi\nEOF ok"}


def test_a_heredoc_with_no_closing_line_and_no_closing_tag_is_prose():
    p = parse("<lact a>run(cmd=<<EOF\necho hi")
    assert p.lacts == [] and p.problems == ["<lact a> has no closing tag, so it was read as prose"]
    p = parse("<lact a>run(cmd='x')")
    assert p.lacts == [] and p.problems == ["<lact a> has no closing tag, so it was read as prose"]


def test_a_heredoc_never_closed_in_a_later_call_ends_at_that_calls_own_closing_tag():
    turn = '<lact a>f(x=1)</lact>\n<lact b>run(cmd=<<EOF\necho hi</lact>\n<lact c>g(y="z")</lact>'
    assert _calls(turn) == [("a", {"x": 1}), ("b", {"cmd": "echo hi"}), ("c", {"y": "z"})]
    assert parse(turn).problems == [_repaired("b", CLOSERS)]


def test_markdown_code_after_a_later_call_with_a_heredoc_never_closed_is_still_quoted():
    for quoted in ("`<lvar q>quoted</lvar>`", "\n```\n<lvar q>quoted</lvar>\n```\n"):
        turn = f"<lact a>f()</lact>\n<lact b>g(x=<<EOF\nbody</lact> and {quoted} then <lvar v>kept</lvar>"
        p = parse(turn)
        assert [(v.alias, v.value) for v in p.lvars] == [("v", "kept")], quoted
        assert _calls(turn) == [("a", {}), ("b", {"x": "body"})]


def test_a_heredoc_never_closed_takes_other_tags_as_text_up_to_its_calls_closing_tag():
    p = parse("<lact a>run(cmd=<<EOF\nsee <lact b> and </lvar> here</lact>")
    assert _calls("<lact a>run(cmd=<<EOF\nsee <lact b> and </lvar> here</lact>") == [
        ("a", {"cmd": "see <lact b> and </lvar> here"})
    ]
    assert p.problems == [_repaired("a", CLOSERS)]


def test_a_fence_in_a_heredoc_never_pairs_with_one_in_later_prose():
    turn = "<lact a>run(cmd=<<EOF\n```\nEOF)</lact>\nThe log:\n```\nok\n"
    assert _calls(turn) == [("a", {"cmd": "```"})]
    # nor when the heredoc is never closed and ends at the closing tag
    turn = "<lact a>run(cmd=<<EOF\n```</lact>\n<lact b>g(y=1)</lact>\n```"
    assert _calls(turn) == [("a", {"cmd": "```"}), ("b", {"y": 1})]


def test_a_fence_in_an_earlier_calls_heredoc_stays_blank_after_later_calls():
    turn = (
        "<lact a>run(cmd=<<EOF\n```\nEOF)</lact>\n<lact b>run(cmd=<<EOF\nls\nEOF)</lact>\n"
        "<lact c>g(y=1)</lact>\n```"
    )
    assert [alias for alias, _ in _calls(turn)] == ["a", "b", "c"]


def test_a_quoted_tag_after_a_heredoc_call_is_still_quoted():
    turn = "<lact a>run(cmd=<<EOF\nxyz\nEOF)</lact> then `<lvar q>quoted</lvar>` and <lvar v>kept</lvar>"
    p = parse(turn)
    assert [(v.alias, v.value) for v in p.lvars] == [("v", "kept")] and p.problems == []


def test_a_heredoc_shaped_opening_in_prose_after_a_call_does_not_hide_the_next_call():
    turn = "<lact a>f(x=1)</lact>\nIn shell, x=<<EOF\n<lact b>g(y=2)</lact>\nEOF)"
    assert _calls(turn) == [("a", {"x": 1}), ("b", {"y": 2})]


def test_a_closing_tag_in_a_string_argument_is_text_with_quotes_after_the_call():
    for turn in ('<lact a>f(x="</lact>")</lact> then "done"', '<lact a>f(x="</lact>")</lact> then "done".'):
        assert _calls(turn) == [("a", {"x": "</lact>"})] and parse(turn).problems == [], turn


def test_a_closing_tag_in_a_string_before_two_heredocs_is_text():
    turn = '<lact a>f(a="</lact>", b=<<X\nx\nX, c=<<Y\ny\nY)</lact>'
    assert _calls(turn) == [("a", {"a": "</lact>", "b": "x", "c": "y"})] and parse(turn).problems == []


def test_a_call_whose_argument_is_not_a_literal_keeps_its_string_aware_bounds():
    with pytest.raises(LNDLError, match=r"^f\(\.\.\.\): y= must be a literal"):
        parse('<lact a>f(x="</lact>", y=undefined)</lact>')


def test_an_unreadable_call_with_a_fenced_heredoc_is_bounded_at_its_own_closing_tag():
    turn = "<lact a>run(cmd=<<EOF\n```\nEOF, x=)</lact>\nlog:\n```\n<lact b>g()</lact>"
    with pytest.raises(LNDLError) as e:
        parse(turn)
    assert str(e.value).startswith("cannot read the call 'run(cmd=<<EOF\\n```\\nEOF, x=)': ")


# bodies that are not calls


def test_a_value_in_a_lact_is_bounded_by_its_first_closing_tag():
    p = parse('<lact s>echo "a </lact> b"</lact>')
    assert [(v.alias, v.value) for v in p.lvars] == [("s", 'echo "a')] and p.lacts == []


def test_a_value_in_a_lact_keeps_its_spec_and_field():
    p = parse("<lact report.summary s>git status</lact>")
    assert p.lvars == [Lvar(alias="s", value="git status", spec="report", field="summary")]


def test_an_empty_lact_is_kept_as_an_empty_value():
    p = parse("<lact a></lact>")
    assert p.lvars == [Lvar(alias="a", value="")] and p.lacts == []
    assert p.problems == [
        "<lact a> held a value, not a call, so it was kept as <lvar a>; to act on it, point at it from a "
        "command: name(field=*a)"
    ]


@pytest.mark.parametrize("closed", [True, False])
def test_a_heredoc_in_a_value_blanks_its_fence_for_the_rest_of_the_turn(closed):
    body = "cat x=<<EOF\n```\nEOF" if closed else "cat x=<<EOF\n```"
    p = parse(f"<lact s>{body}</lact>\n<lact b>g(y=1)</lact>\n```")
    assert [(v.alias, v.value) for v in p.lvars] == [("s", body)]
    assert [(la.alias, la.args) for la in p.lacts] == [("b", {"y": 1})]


# the scan


def test_an_opener_with_no_closer_costs_a_notice_and_the_scan_goes_on():
    p = parse('<lvar a>no closer here <lact b>g(y="z")</lact>')
    assert [(la.alias, la.args) for la in p.lacts] == [("b", {"y": "z"})]
    assert p.problems == ["<lvar a> has no closing tag, so it was read as prose"]


def test_an_out_never_closed_is_prose_and_the_scan_goes_on():
    p = parse("OUT{a: 1 <lvar v>x</lvar>")
    assert p.out is None and p.problems == [] and [v.alias for v in p.lvars] == ["v"]


def test_an_empty_out_is_no_finish():
    for text in ("OUT{}", "OUT{ \n }", "A turn without OUT{} runs on."):
        p = parse(text)
        assert p.out is None and p.problems == [], text


def test_a_construct_right_after_an_out_is_read():
    p = parse("OUT{done: [v]}<lvar v>x</lvar>")
    assert p.out.fields == {"done": ["v"]} and [v.alias for v in p.lvars] == ["v"]


def test_spans_are_the_constructs_and_never_the_problems():
    text = "a <lact …>x</lact> OUT{a: 1} <lvar v>y</lvar> <lvar w>no closer"
    assert spans(text) == [(19, 28), (29, 45)]
    assert [text[s:e] for s, e in spans(text)] == ["OUT{a: 1}", "<lvar v>y</lvar>"]


# OUT{}


def test_out_takes_a_trailing_comma_and_no_space_after_commas():
    assert parse_out("a: 1, ").fields == {"a": 1}
    assert parse_out("a: 1,b: [x],c: y").fields == {"a": 1, "b": ["x"], "c": ["y"]}


def test_out_nested_brackets_are_one_field():
    assert parse_out("a: [[1], {'k': (2, 3)}], b: 2").fields == {"a": [[1], {"k": (2, 3)}], "b": 2}


def test_out_a_single_alias_then_more_fields():
    out = parse_out("a: x, b: 2, c: [y, z]")
    assert out.fields == {"a": ["x"], "b": 2, "c": ["y", "z"]}
    assert isinstance(out.fields["a"], Aliases) and isinstance(out.fields["c"], Aliases)


def test_out_true_false_and_none_are_literals_other_spellings_are_aliases():
    out = parse_out("a: True, b: False, c: None, d: [True], e: [False], f: [None]")
    assert out.fields == {"a": True, "b": False, "c": None, "d": [True], "e": [False], "f": [None]}
    assert not any(isinstance(v, Aliases) for v in out.fields.values())
    spellings = ["true", "TRUE", "false", "FALSE", "none", "NONE"]
    out = parse_out(
        ", ".join(f"k{i}: {w}" for i, w in enumerate(spellings)) + f", all: [{', '.join(spellings)}]"
    )
    assert out.fields == {**{f"k{i}": [w] for i, w in enumerate(spellings)}, "all": spellings}
    assert all(isinstance(v, Aliases) for v in out.fields.values())


def test_out_a_forgotten_opening_bracket_is_not_read_as_aliases():
    with pytest.raises(LNDLError, match="neither aliases"):
        parse_out("answer: a_b]")


@pytest.mark.parametrize(
    "body, message",
    [
        ('"a b": 1', "OUT{...}: '\"a b\"' is not a spec name"),
        ("1x: 1", "OUT{...}: '1x' is not a spec name"),
        ("no colon", "OUT{...}: 'no colon' needs the form key: value"),
    ],
)
def test_out_errors_name_the_part(body, message):
    with pytest.raises(LNDLError) as e:
        parse_out(body)
    assert str(e.value) == message


def test_out_strings_keep_escaped_quotes_braces_and_backslashes():
    out = parse(r'OUT{answer: "say \"}\" now", path: "C:\\", n: 1}').out
    assert out.fields == {"answer": 'say "}" now', "path": "C:\\", "n": 1}


def test_two_outs_are_named_exactly():
    p = parse("OUT{a: 1} OUT{b: 2}")
    assert p.out is None
    assert p.problems == ["OUT{...} could not be read, so this turn ran without it: one OUT{...} per turn"]


# stray command tags


def test_stray_tags_are_command_names_only():
    names = {"edit", "Run", "lvar", "lact", "note.get"}
    text = "<p>x</p> <edit> <Run > <note.get> <lvar> <lact> <other>"
    assert stray_tags(text, names) == ["Run", "edit", "note.get"]
    assert stray_tags("<edit a>", {"edit"}) == []


# the prose view


def test_plain_keeps_the_prose_lines_without_the_constructs():
    text = "Looking   first.\n<lvar a>x</lvar>\n\n\n\nThen\t<lact b>f(x=1)</lact>  done.\nOUT{a: 1}\n  "
    assert plain(text) == "Looking first.\n\nThen done."
    assert plain("one\ntwo") == "one\ntwo"
    assert plain("") == ""


# known defects: each test states the intended behaviour


@pytest.mark.xfail(
    strict=True,
    reason="known defect: an apostrophe in a heredoc body makes the scan read the call's closing tag as "
    "inside a string, so a closing tag in an earlier string argument ends the call (ADR-0004/C1, C2)",
)
def test_a_heredoc_body_with_an_apostrophe_does_not_move_the_calls_end():
    turn = "<lact a>f(x='</lact>', y=<<EOF\nit's\nEOF)</lact>"
    assert _calls(turn) == [("a", {"x": "</lact>", "y": "it's"})] and parse(turn).problems == []


@pytest.mark.xfail(
    strict=True,
    reason="known defect: backticks inside string arguments of two calls pair as a markdown fence, "
    "which hides the calls between them and loses the turn (ADR-0004/C1)",
)
def test_a_fence_inside_string_arguments_is_text():
    assert _calls('<lact a>f(x="```")</lact>\n<lact b>g(y="```")</lact>') == [
        ("a", {"x": "```"}),
        ("b", {"y": "```"}),
    ]


@pytest.mark.xfail(
    strict=True,
    reason="known defect: a fence inside a string argument pairs with a fence in a later value, which "
    "drops both constructs (ADR-0004/C1)",
)
def test_a_fence_inside_a_string_argument_does_not_pair_with_a_later_value():
    p = parse('<lact a>f(x="```")</lact>\n<lvar b>```</lvar>')
    assert _calls('<lact a>f(x="```")</lact>\n<lvar b>```</lvar>') == [("a", {"x": "```"})]
    assert [(v.alias, v.value) for v in p.lvars] == [("b", "```")]


@pytest.mark.xfail(
    strict=True,
    reason="known defect: backticks in two sibling values pair as markdown code, so the first value "
    "swallows the second (ADR-0004/C1: each construct owns its span)",
)
@pytest.mark.parametrize("tick", ["```", "`"])
def test_backticks_in_sibling_values_do_not_pair(tick):
    p = parse(f"<lvar a>{tick}x</lvar> <lvar b>y{tick}</lvar>")
    assert [(v.alias, v.value) for v in p.lvars] == [("a", f"{tick}x"), ("b", f"y{tick}")]


@pytest.mark.xfail(
    strict=True,
    reason="known defect: a string literal spelled like the internal pointer placeholder is read as a "
    "pointer (ADR-0004/C1: a quoted string means the string)",
)
@pytest.mark.parametrize("where", ["call", "out"])
def test_a_string_spelled_like_the_pointer_placeholder_is_a_string(where):
    if where == "call":
        assert parse_call('echo(text="\\x00ptr:secret")') == ("echo", {"text": "\x00ptr:secret"})
    else:
        assert parse('OUT{answer: "\\x00ptr:a"}').out.fields == {"answer": "\x00ptr:a"}


@pytest.mark.xfail(
    strict=True,
    reason="known defect: a string literal spelled like the internal heredoc placeholder is replaced by "
    "the call's heredoc (ADR-0004/C1: a quoted string means the string)",
)
@pytest.mark.parametrize(
    "literal, value",
    [
        ('"\\x00doc:0"', "\x00doc:0"),
        ('"\\x00doc:-1"', "\x00doc:-1"),
        ('["\\x00doc:0"]', ["\x00doc:0"]),
        ('("\\x00doc:0",)', ("\x00doc:0",)),
        ('{"k": "\\x00doc:0"}', {"k": "\x00doc:0"}),
    ],
)
def test_a_string_spelled_like_the_heredoc_placeholder_beside_a_heredoc_is_a_string(literal, value):
    assert parse_call(f"echo(text={literal}, body=<<EOF\nhi\nEOF)") == ("echo", {"text": value, "body": "hi"})


@pytest.mark.parametrize("literal", ["{*a: 1}", "{'k': 1, *a: 2}", "{*a}", "{1, *a}"])
def test_a_pointer_where_a_key_or_a_set_member_stands_is_refused(literal):
    why = "*a: a pointer stands where a value stands, not as a key or a set member"
    with pytest.raises(LNDLError, match=re.escape(why)):
        parse_call(f"f(x={literal})")
    with pytest.raises(LNDLError, match=re.escape(why)):
        parse(f"<lact a>f(x={literal})</lact>")
    p = parse(f"OUT{{x: {literal}}}")
    assert p.out is None
    assert p.problems == [f"OUT{{...}} could not be read, so this turn ran without it: {why}"]


@pytest.mark.parametrize("key", ["*a", "\x00ptr:a"])
def test_a_quoted_key_spelled_like_a_pointer_or_its_placeholder_is_a_string(key):
    name, args = parse_call(f"f(x={{{key!r}: 1}})")
    assert (name, args) == ("f", {"x": {key: 1}})
    assert pointers_in(args) == []


@pytest.mark.xfail(
    strict=True,
    reason="known defect: a pointer that starts a continuation line inside a literal is not read, "
    "because the token before it is the line break (ADR-0004/C1: arguments are literals or pointers)",
)
def test_a_pointer_that_starts_a_continuation_line_is_read():
    assert parse_call("send(cc=[*c,\n        *d])") == ("send", {"cc": [Pointer("c"), Pointer("d")]})


def test_a_call_needing_a_comma_and_its_closers_names_both_repairs():
    p = parse('<lact r>run(cmd="ls" timeout=1</lact>')
    assert p.lacts[0].args == {"cmd": "ls", "timeout": 1}
    assert len(p.problems) == 1 and COMMA in p.problems[0] and CLOSERS in p.problems[0]


@pytest.mark.xfail(
    strict=True,
    reason="known defect: the call is stripped before its closers are supplied, so a string left open "
    "at the closing tag loses the whitespace it ends with (ADR-0004/A2: the call the model meant, short "
    "of its closers)",
)
def test_a_string_left_open_at_the_closing_tag_keeps_its_trailing_space():
    assert _calls('<lact j>join(items=*xs, sep=", </lact>') == [("j", {"items": Pointer("xs"), "sep": ", "})]


@pytest.mark.xfail(
    strict=True,
    reason="known defect: a missing comma after a pointer, True, False, None or a JSON word is not "
    "supplied (ADR-0004/C3)",
)
@pytest.mark.parametrize("value", ["*a", "True", "None", "true", "null"])
def test_a_missing_comma_after_any_argument_is_supplied(value):
    name, args, repairs = parse_call_noting(f"run(cmd={value} timeout=1)")
    assert name == "run" and args["timeout"] == 1 and repairs == ("comma",)


_TOO_DEEP = "known defect: deeply nested input raises RecursionError out of parse instead of LNDLError"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "<lact a>f(x=" + "-" * 3000 + "1)</lact>",
            id="unary-argument",
            marks=pytest.mark.xfail(raises=RecursionError, strict=True, reason=_TOO_DEEP),
        ),
        pytest.param(
            "<lact a>" + ".".join(["a"] * 3000) + "(x=1)</lact>",
            id="dotted-callee",
            marks=pytest.mark.xfail(raises=RecursionError, strict=True, reason=_TOO_DEEP),
        ),
        # from 3.12 the parser builds this without recursing, and the literal is then refused
        pytest.param(
            "OUT{a: " + "-" * 3000 + "1}",
            id="unary-out",
            marks=pytest.mark.xfail(
                sys.version_info < (3, 12), raises=RecursionError, strict=True, reason=_TOO_DEEP
            ),
        ),
    ],
)
def test_deeply_nested_input_is_an_lndl_error_or_a_program(text):
    try:
        result = parse(text)
    except LNDLError as e:
        result = e
    assert isinstance(result, (Program, LNDLError))


@pytest.mark.xfail(
    strict=True,
    reason="known defect: plain() cuts constructs with its own pattern, not the parser's scan, so it "
    "keeps text the parser read as a construct and drops text it read as prose",
)
@pytest.mark.parametrize(
    "text, prose",
    [
        ('<lact e>echo(text="a </lact> b")</lact> ok', "ok"),
        ("<lact a>run(cmd=<<EOF\n</lact>\nEOF)</lact> ok", "ok"),
        ("<lvar n>a <lvar x>b</lvar> c</lvar> ok", "ok"),
        ("<lact a>f()</lact > ok", "ok"),
        ("OUT {x: 1} ok", "ok"),
        ('OUT{done: {summary: "x"}} ok', "ok"),
        ("say `<lvar x>y</lvar>` ok", "say `<lvar x>y</lvar>` ok"),
    ],
)
def test_plain_is_the_prose_the_parser_leaves(text, prose):
    assert plain(text) == prose


@pytest.mark.parametrize("closed", [True, False], ids=["closed", "unclosed"])
@pytest.mark.parametrize("nl", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_a_heredoc_body_reads_as_lf_lines_whether_or_not_its_closing_line_is_there(nl, closed):
    """ADR-0004/C2: a CRLF body reads as its lines, LF-ended; a body never closed runs to the call's
    closing tag and reads the same way."""
    text = (
        f"<lact a>f(x=<<EOF{nl}one{nl}two{nl}EOF)</lact>"
        if closed
        else f"<lact a>f(x=<<EOF{nl}one{nl}two</lact>"
    )
    p = parse(text)
    assert p.lacts[0].args == {"x": "one\ntwo"}
    assert p.problems == ([] if closed else [_repaired("a", CLOSERS)])


@pytest.mark.parametrize("bad", ["\ud800", "\x00"], ids=["lone-surrogate", "nul"])
def test_text_the_interpreter_refuses_is_an_lndl_error(bad):
    """A lone surrogate or a NUL byte in a string argument: the interpreter's own refusal becomes the
    call's LNDLError, `spans` still bounds the call, and inside `OUT{}` it is a problem, not a crash."""
    text = f'<lact a>f(x="{bad}")</lact>'
    with pytest.raises(LNDLError, match="cannot read the call"):
        parse(text)
    with pytest.raises(LNDLError, match="cannot read the call"):
        parse_call(f'f(x="{bad}")')
    assert spans(text) == [(0, len(text))]
    p = parse(f'OUT{{answer: "{bad}"}}')
    assert p.out is None
    assert len(p.problems) == 1 and p.problems[0].startswith("OUT{...} could not be read")


@pytest.mark.parametrize(
    "sep", ["\u2028", "\u2029", "\x85", "\x0c", "\x0b"], ids=["u2028", "u2029", "nel", "ff", "vt"]
)
def test_a_separator_inside_a_string_does_not_move_what_stands_on_a_later_line(sep):
    """`str.splitlines` breaks at these; the tokenizer's lines break at `\\n` only. Positions on the
    lines after the string are the tokenizer's, so a pointer or a missing comma there is found where
    it stands."""
    p = parse(f'<lact a>f(text="a{sep}b",\n ref=*x)</lact>')
    assert p.lacts[0].args == {"text": f"a{sep}b", "ref": Pointer(name="x")}
    assert p.problems == []
    p = parse(f'<lact b>g(text="a{sep}b",\n n=1 m=2)</lact>')
    assert p.lacts[0].args == {"text": f"a{sep}b", "n": 1, "m": 2}
    assert p.problems == [_repaired("b", COMMA)]


def test_a_quoted_out_is_text():
    """`OUT{}` inside markdown code is the model talking about the syntax, like a quoted tag: it is
    not the turn's output, and it does not make a real one beside it a second OUT."""
    assert parse("`OUT{answer: 1}`").out is None
    assert parse("```\nOUT{answer: 1}\n```").out is None
    p = parse("say `OUT{answer: 1}` then\nOUT{answer: 2}")
    assert p.out is not None and p.out.fields == {"answer": 2}
    assert p.problems == []
