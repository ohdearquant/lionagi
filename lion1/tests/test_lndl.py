import pytest

from lion1 import lndl
from lion1.lndl import Aliases, LNDLError, Pointer, guidance, parse


def test_prose_with_the_three_constructs():
    text = """Let me look first.
<lvar plan>read the file, then decide</lvar>
<lact f>read_file(path="src/main.py", limit=40)</lact>
<lact g>grep(pattern="def ", text=*f, flags=["i"])</lact>
Then I will finish.
OUT{report: [plan, g], score: 0.9}
"""
    p = parse(text)
    assert [v.alias for v in p.lvars] == ["plan"]
    assert p.lvars[0].value == "read the file, then decide"
    assert [a.alias for a in p.lacts] == ["f", "g"]
    assert p.lacts[0].name == "read_file"
    assert p.lacts[0].args == {"path": "src/main.py", "limit": 40}
    assert p.lacts[1].args == {"pattern": "def ", "text": Pointer("f"), "flags": ["i"]}
    assert p.lacts[1].pointers() == [Pointer("f")]
    assert p.out is not None
    assert p.out.fields == {"report": ["plan", "g"], "score": 0.9}


def test_namespaced_headers_fill_output_fields():
    p = parse("<lvar report.title t>Hello</lvar><lact report.body b>fetch(url='x')</lact>OUT{report: [t, b]}")
    assert (p.lvars[0].spec, p.lvars[0].field) == ("report", "title")
    assert (p.lacts[0].spec, p.lacts[0].field) == ("report", "body")


def test_note_prefixed_alias_and_dotted_command_names():
    p = parse("<lvar note.wake17>tests green, see *r41</lvar><lact h>context.hide(refs=['r41'])</lact>")
    assert p.lvars[0].alias == "note.wake17"
    assert p.lacts[0].name == "context.hide"
    assert p.out is None


def test_pointer_inside_nested_literals():
    p = parse("<lact x>send(to='a', body={'ref': *note.k, 'items': [*m1, 2]})</lact>")
    assert p.lacts[0].args["body"] == {"ref": Pointer("note.k"), "items": [Pointer("m1"), 2]}


def test_no_constructs_is_a_legal_turn():
    p = parse("Just thinking out loud this turn.")
    assert p.lvars == [] and p.lacts == [] and p.out is None


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("<lact a>f(1)</lact>", "keyword only"),
        ("<lact a>f(x=undefined_name)</lact>", "literal"),
    ],
)
def test_unreadable_lndl_names_the_problem(text, fragment):
    with pytest.raises(LNDLError, match=fragment):
        parse(text)


def test_a_bad_header_is_prose_not_a_dead_turn():
    p = parse('<lvar a b c>x</lvar> <lact a1>echo(text="ran")</lact>')
    assert [la.alias for la in p.lacts] == ["a1"] and p.lvars == []
    assert p.problems == [
        "<lvar a b c>: the header is `alias` or `spec.field alias`, space separated; it was read as prose"
    ]


def test_attribute_style_header_is_read_as_the_alias():
    p = parse('<lact alias="init_file">read_file(path="a")</lact><lvar alias=v>1</lvar>')
    assert p.lacts[0].alias == "init_file" and p.lvars[0].alias == "v"


def test_out_alone_with_a_bare_alias():
    p = parse("OUT{answer: a}")
    assert p.out.fields == {"answer": ["a"]}
    assert isinstance(p.out.fields["answer"], Aliases)


def test_out_brackets_holding_literals_are_a_literal_list():
    p = parse('OUT{answer: [14], names: ["a", "b"], n: 3}')
    assert p.out.fields == {"answer": [14], "names": ["a", "b"], "n": 3}
    assert not any(isinstance(v, Aliases) for v in p.out.fields.values())


def test_out_dict_literal_with_bare_keys():
    p = parse('OUT{done: {summary: "fixed it", files: ["a.py"]}}')
    assert p.out.fields == {"done": {"summary": "fixed it", "files": ["a.py"]}}


def test_real_newlines_inside_a_string_argument_are_kept():
    p = parse(
        '<lact e>edit(path="a.py", old="def f():\n    return 1\n", new="def f():\n    return 2\n")</lact>'
    )
    assert p.lacts[0].args["old"] == "def f():\n    return 1\n"
    assert p.lacts[0].args["new"] == "def f():\n    return 2\n"
    p = parse('<lact r>run(cmd="python -c \\"\nimport x\\"")</lact>')
    assert p.lacts[0].args["cmd"] == 'python -c "\nimport x"'


def test_a_call_that_is_still_unreadable_names_the_heredoc_route():
    with pytest.raises(LNDLError, match="written as a heredoc argument"):
        parse("<lact r>run(cmd=python -c\nimport x)</lact>")  # bare words across lines: no repair reads it


def test_a_models_slips_are_repaired_and_named():
    # the closing tag standing in for `")`: the string and the call are closed, the turn is told
    p = parse('<lact e>edit(path="a.py", old="x = 1", new="x = 2\ny = 3</lact>')
    assert p.lacts[0].args == {"path": "a.py", "old": "x = 1", "new": "x = 2\ny = 3"}
    assert len(p.problems) == 1 and "closers were missing" in p.problems[0]
    p = parse('<lact r>run(cmd="ls" timeout=1)</lact>')  # the comma between arguments left out
    assert p.lacts[0].args == {"cmd": "ls", "timeout": 1} and "comma" in p.problems[0]
    p = parse("<lact g>grep(pattern='a', paths=['x.py', 'y.py'</lact>")
    assert p.lacts[0].args == {"pattern": "a", "paths": ["x.py", "y.py"]}
    # a triple-quoted value with real line breaks rides through the repairs untouched
    old, new = 'old="""def f():\n    "doc"\n"""', 'new="""def f():\n    \\"\\"\\"doc\\"\\"\\"\n"""'
    p = parse(f'<lact e>edit(path="a", {old} {new})</lact>')
    assert (
        p.lacts[0].args["old"] == 'def f():\n    "doc"\n'
        and p.lacts[0].args["new"] == 'def f():\n    """doc"""\n'
    )
    assert "comma" in p.problems[0]
    # not every slip has a repair
    with pytest.raises(LNDLError, match=r"cannot read the call 'run\(cmd=\"ls\", timeout=\)'"):
        parse('<lact r>run(cmd="ls", timeout=)</lact>')


def test_a_heredoc_argument_is_taken_verbatim_with_nothing_inside_it_read():
    body = "cat > /tmp/t.py <<'PY'\nprint(\"a\", 'b', *args)  # OUT{x: 1} and )\nPY\npython /tmp/t.py"
    p = parse(f"<lact r1>run(cmd=<<EOF\n{body}\nEOF, timeout=60)</lact>")
    assert p.lacts[0].args == {"cmd": body, "timeout": 60} and p.out is None
    # the closing line followed by the next argument with no comma, or by nothing at all
    p = parse("<lact r2>run(cmd=<<EOF\nls -la\nEOF\ntimeout=300)</lact>")
    assert p.lacts[0].args == {"cmd": "ls -la", "timeout": 300} and "comma" in p.problems[0]
    p = parse("<lact r3>run(cmd=<<EOF\nls -la\nEOF</lact>")
    assert p.lacts[0].args == {"cmd": "ls -la"} and "closers" in p.problems[0]
    # opened and never closed: the value runs to the end of the call
    p = parse('<lact r4>run(cmd=<<EOF\npython -c "\nimport x\nprint(x)\n"</lact>')
    assert p.lacts[0].args == {"cmd": 'python -c "\nimport x\nprint(x)\n"'} and "closers" in p.problems[0]
    # two heredocs in one call, quoted tag, and a pointer beside them
    p = parse('<lact e1>edit(path=*f, old=<<"A"\n    x = "1"\nA, new=<<B\n    x = "2"\n    y = 3\nB)</lact>')
    assert p.lacts[0].args == {"path": Pointer("f"), "old": '    x = "1"', "new": '    x = "2"\n    y = 3'}


def test_a_heredoc_body_holding_an_unmatched_closer_is_taken_whole():
    # a span copied from a file that shows a `</lact>` line was cut at that line and the edit
    # handler wrote the truncated text as a success
    body = "echo two\nEOF)</lact>"
    p = parse(f'<lact e1>edit(path="g.md", old=<<OLD\necho one\nOLD, new=<<NEW\n{body}\nNEW)</lact>')
    assert p.lacts[0].args == {"path": "g.md", "old": "echo one", "new": body} and p.problems == []
    # a whole tag inside a body is data too: one call, its body verbatim
    p = parse("<lact a>run(cmd=<<EOF\n<lact b>x()</lact>\nEOF)</lact>")
    assert [la.alias for la in p.lacts] == ["a"] and p.lacts[0].args == {"cmd": "<lact b>x()</lact>"}
    # a body left open still runs to the end of the call, closers supplied
    p = parse("<lact r4>run(cmd=<<EOF\necho </lact>")
    assert p.lacts[0].args == {"cmd": "echo"} and "closers" in p.problems[0]
    assert "heredoc" in guidance(("report",))


def test_a_heredoc_body_written_with_crlf_keeps_its_lines_not_its_line_endings():
    # the body was sliced to the closing line and only a trailing `\n` was stripped, so a
    # CRLF-written body left a stray `\r` on every line (ADR-0004/C2: "the lines verbatim" means
    # the lines, not their transport line endings).
    p = parse("<lact r1>run(cmd=<<EOF\r\nset -e\r\necho hi\r\nexit 0\r\nEOF)</lact>")
    assert p.lacts[0].args == {"cmd": "set -e\necho hi\nexit 0"}
    p = parse("<lact r2>run(cmd=<<EOF\nset -e\necho hi\nexit 0\nEOF)</lact>")  # the LF control
    assert p.lacts[0].args == {"cmd": "set -e\necho hi\nexit 0"}


# A heredoc is read in its own call's arguments, and its body is opaque until its closing line.


def _cut(text: str) -> list[str]:
    return [text[a:b] for a, b in lndl.spans(text)]


def test_heredoc_repair_preserves_following_call():
    # the call's closers left off after the closing line, then another heredoc call
    turn = (
        '<lact w1>write(path="notes.txt", content=<<EOF\nhello\nEOF\n</lact>\n'
        "<lact r1>run(cmd=<<EOF\nls\nEOF)</lact>"
    )
    p = parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [
        ("w1", {"path": "notes.txt", "content": "hello"}),
        ("r1", {"cmd": "ls"}),
    ]
    assert len(p.problems) == 1 and p.problems[0].startswith("<lact w1>") and "closers" in p.problems[0]
    assert _cut(turn) == [turn[: turn.index("\n<lact r1>")], turn[turn.index("<lact r1>") :]]
    # a legal closing line anywhere after the opening ends the body, whatever the body holds before it
    turn = "<lact a>run(cmd=<<EOF\necho hi</lact>\n<lact b>run(cmd=<<EOF\nls\nEOF)</lact>"
    p = parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [
        ("a", {"cmd": "echo hi</lact>\n<lact b>run(cmd=<<EOF\nls"})
    ]
    assert p.problems == [] and _cut(turn) == [turn]
    # prose after the call that ends on the tag does not take the call's closing tag
    p = parse('<lact w1>write(path="n.txt", content=<<EOF\nhello\nEOF\n</lact>\nI ended it with\nEOF')
    assert [(la.alias, la.args) for la in p.lacts] == [("w1", {"path": "n.txt", "content": "hello"})]


def test_a_heredoc_with_no_closing_line_anywhere_ends_at_its_calls_closing_tag():
    turn = '<lact a>run(cmd=<<EOF\necho hi</lact>\n<lact b>run(cmd="ls")</lact>'
    p = parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [("a", {"cmd": "echo hi"}), ("b", {"cmd": "ls"})]
    assert len(p.problems) == 1 and p.problems[0].startswith("<lact a>") and "closers" in p.problems[0]
    assert _cut(turn) == [turn[: turn.index("\n<lact b>")], turn[turn.index("<lact b>") :]]


def test_longer_identifier_is_not_terminator():
    p = parse("<lact r1>run(cmd=<<PY\nPYTHONPATH=. pytest -q\nPY)</lact>")
    assert p.lacts[0].args == {"cmd": "PYTHONPATH=. pytest -q"} and p.problems == []
    p = parse("<lact r2>run(cmd=<<EOF\nEOFX = 1\nEOF_DONE=yes\nEOF, timeout=5)</lact>")
    assert p.lacts[0].args == {"cmd": "EOFX = 1\nEOF_DONE=yes", "timeout": 5} and p.problems == []


def test_separate_heredoc_fences_do_not_pair():
    turn = (
        '<lact e1>edit(path="README.md", old="Intro.", new=<<B\nIntro.\n```python\nB)</lact>\n'
        '<lact e2>edit(path="README.md", old="The end.", new=<<C\n```\nThe end.\nC)</lact>'
    )
    p = parse(turn)
    assert [(la.alias, la.args["new"]) for la in p.lacts] == [
        ("e1", "Intro.\n```python"),
        ("e2", "```\nThe end."),
    ]
    assert _cut(turn) == [turn[: turn.index("\n<lact e2>")], turn[turn.index("<lact e2>") :]]
    # nor with a fence in the prose after the call
    turn = '<lact a>run(cmd=<<EOF\n```\nEOF)</lact>\nThe log follows.\n```\n<lact b>read(path="x")</lact>'
    p = parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [("a", {"cmd": "```"}), ("b", {"path": "x"})]


def test_fenced_fake_heredoc_cannot_hide_real_call():
    turn = '```text\nexample=<<EOF\n```\n<lact r>echo(text="real")</lact>\nEOF'
    p = parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [("r", {"text": "real"})] and p.problems == []
    assert _cut(turn) == ['<lact r>echo(text="real")</lact>']
    # an opening in prose is not a call's argument, fenced or not
    p = parse('Shell writes x=<<EOF\nlike this.\n<lact r>echo(text="real")</lact>\nEOF')
    assert [(la.alias, la.args) for la in p.lacts] == [("r", {"text": "real"})]
    # nor is one inside a call's string literal, whose "closing line" sits in a later call's body
    p = parse(
        '<lact a>note(text="""the form is cmd=<<EOF\nthen the lines""")</lact>\n'
        "<lact b>run(cmd=<<SH\ncat > f <<EOF\nhi\nEOF\nPYTHONPATH=. pytest\nSH)</lact>"
    )
    assert [(la.alias, la.args) for la in p.lacts] == [
        ("a", {"text": "the form is cmd=<<EOF\nthen the lines"}),
        ("b", {"cmd": "cat > f <<EOF\nhi\nEOF\nPYTHONPATH=. pytest"}),
    ]


def test_a_closing_tag_inside_a_string_argument_is_text():
    for turn, args in (
        ('<lact e>echo(text="""before\n</lact>\nafter""")</lact>', {"text": "before\n</lact>\nafter"}),
        ('<lact e>echo(text="a </lact> b")</lact>', {"text": "a </lact> b"}),
        ('<lact e>edit(old="</lact>", new=<<EOF\nx\nEOF)</lact>', {"old": "</lact>", "new": "x"}),
    ):
        p = parse(turn)
        assert [(la.alias, la.args) for la in p.lacts] == [("e", args)] and p.problems == [], turn
        assert _cut(turn) == [turn], turn


def test_a_string_left_open_at_the_closing_tag_ends_its_call_before_the_next():
    # the closing tag standing in for `")`, then more quotes: the calls after it are still their own
    turn = '<lact e>edit(path="a.py", old="x", new="y</lact>\n<lact r>run(cmd="pytest")</lact>'
    p = parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [
        ("e", {"path": "a.py", "old": "x", "new": "y"}),
        ("r", {"cmd": "pytest"}),
    ]
    assert len(p.problems) == 1 and p.problems[0].startswith("<lact e>") and "closers" in p.problems[0]
    assert _cut(turn) == [turn[: turn.index("\n<lact r>")], turn[turn.index("<lact r>") :]]
    p = parse('<lact e>edit(new="y</lact>\nThen I say "done".')
    assert [(la.alias, la.args) for la in p.lacts] == [("e", {"new": "y"})] and "closers" in p.problems[0]


# the closing-line forms and repairs the heredoc reader keeps


def test_every_legal_closing_line_ends_the_heredoc():
    for turn, args, note in (
        ("<lact r>run(cmd=<<EOF\nls\nEOF, timeout=5)</lact>", {"cmd": "ls", "timeout": 5}, None),
        ("<lact r>run(cmd=<<EOF\nls\nEOF)</lact>", {"cmd": "ls"}, None),
        ("<lact r>run(cmd=<<EOF\nls\n  EOF  )</lact>", {"cmd": "ls"}, None),
        ("<lact r>run(cmd=<<EOF\nls\nEOF\ntimeout=5)</lact>", {"cmd": "ls", "timeout": 5}, "comma"),
        ("<lact r>run(cmd=<<EOF\nls\nEOF\n</lact>", {"cmd": "ls"}, "closers"),
        ("<lact r>run(cmd=<<EOF\nls\nEOF</lact>", {"cmd": "ls"}, "closers"),
    ):
        p = parse(turn)
        assert p.lacts[0].args == args, turn
        assert (note in p.problems[0]) if note else p.problems == [], turn


def test_a_heredoc_never_closed_ends_at_its_call_with_the_closers_notice():
    p = parse('<lact a>run(cmd=<<EOF\necho one\necho two</lact>\n<lact b>read(path="x")</lact>')
    assert [(la.alias, la.args) for la in p.lacts] == [
        ("a", {"cmd": "echo one\necho two"}),
        ("b", {"path": "x"}),
    ]
    assert len(p.problems) == 1 and p.problems[0].startswith("<lact a>") and "closers" in p.problems[0]


def test_crlf_and_lf_turns_read_as_the_same_calls():
    lf = (
        '<lact a>run(cmd=<<EOF\nset -e\n\n  echo "hi"\nEOF, timeout=5)</lact>\nthen\n'
        "<lact b>run(cmd=<<SH\nls\nSH)</lact>"
    )
    read = [[(la.alias, la.args) for la in parse(t).lacts] for t in (lf, lf.replace("\n", "\r\n"))]
    assert read[0] == read[1] == [("a", {"cmd": 'set -e\n\n  echo "hi"', "timeout": 5}), ("b", {"cmd": "ls"})]


def test_a_closed_body_is_verbatim_whatever_it_holds():
    body = (
        "    def f():\n\n        return \"x\", 'y'  # OUT{x: 1}\n"
        "<lvar v>not a value</lvar> </lact> `<lact z>`\n"
    )
    p = parse(f'<lact e>edit(path="a.py", new=<<PYEND\n{body}PYEND)</lact>')
    assert p.lvars == [] and p.out is None and p.problems == []
    assert [(la.alias, la.args) for la in p.lacts] == [("e", {"path": "a.py", "new": body[:-1]})]
    assert parse("<lact e>echo(text=<<EOF\nEOF)</lact>").lacts[0].args == {"text": ""}


def test_the_console_cuts_heredoc_calls_where_the_parser_reads_them():
    turn = (
        "Reading it.\n<lact a>run(cmd=<<EOF\ncat <<X\n</lact>\nX\nEOF)</lact>\n"
        "Then\n<lvar n>kept</lvar> done."
    )
    p = parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [("a", {"cmd": "cat <<X\n</lact>\nX"})]
    assert _cut(turn) == [turn[turn.index("<lact a>") : turn.index("\nThen")], "<lvar n>kept</lvar>"]


def test_a_single_call_with_its_closers_left_off_reads():
    p = lndl.parse('<lact w1>write(path="notes.txt", content=<<EOF\nhello\nEOF\n</lact>')
    assert [(a.alias, a.args) for a in p.lacts] == [("w1", {"path": "notes.txt", "content": "hello"})]


def test_two_heredoc_calls_the_first_with_closers_left_off_stay_two_calls():
    turn = (
        '<lact w1>write(path="notes.txt", content=<<EOF\nhello\nEOF\n</lact>\n'
        "<lact r1>run(cmd=<<EOF\nls\nEOF)</lact>"
    )
    p = lndl.parse(turn)
    assert [a.alias for a in p.lacts] == ["w1", "r1"], [(a.alias, a.args) for a in p.lacts]
    assert p.lacts[0].args["content"] == "hello"


def test_a_body_line_that_starts_with_the_tag_is_not_the_closing_line():
    p = lndl.parse("<lact r1>run(cmd=<<PY\nPYTHONPATH=. pytest -q\nPY)</lact>")
    assert p.lacts[0].args == {"cmd": "PYTHONPATH=. pytest -q"}


def test_a_shell_variable_line_under_an_sh_heredoc_is_not_the_closing_line():
    p = lndl.parse("<lact r2>run(cmd=<<SH\nSHELL=/bin/bash make test\nSH)</lact>")
    assert p.lacts[0].args == {"cmd": "SHELL=/bin/bash make test"}


def test_each_heredoc_call_reads_on_its_own_whatever_its_body_holds():
    turn = (
        '<lact e1>edit(path="README.md", old="Intro.", new=<<B\nIntro.\n```python\nB)</lact>\n'
        '<lact e2>edit(path="README.md", old="The end.", new=<<C\n```\nThe end.\nC)</lact>'
    )
    first = lndl.parse(turn.split("\n<lact e2>")[0])
    assert first.lacts[0].args["new"] == "Intro.\n```python"
    p = lndl.parse(turn)
    assert [a.alias for a in p.lacts] == ["e1", "e2"]
    assert p.lacts[1].args["new"] == "```\nThe end."


def test_a_lact_holding_a_value_instead_of_a_call_is_kept_as_an_lvar_and_named():
    p = parse("<lact s3>git log --oneline -5; echo ----; git status</lact>\n<lact a1>run(cmd=*s3)</lact>")
    assert [v.alias for v in p.lvars] == ["s3"] and p.lvars[0].value.startswith("git log")
    assert [la.alias for la in p.lacts] == ["a1"] and p.lacts[0].args == {"cmd": Pointer("s3")}
    assert len(p.problems) == 1 and "<lact s3> held a value" in p.problems[0]
    p = parse("<lact note.gap>the property path is unverified: check getmro</lact>")
    assert p.lvars[0].alias == "note.gap" and p.lvars[0].value.startswith("the property path")
    # a call shape stays a call
    with pytest.raises(LNDLError, match=r"cannot read the call 'run\(cmd=\"ls\", timeout=\)'"):
        parse('<lact r>run(cmd="ls", timeout=)</lact>')


def test_stray_command_tags_are_reported():
    from lion1.lndl import stray_tags

    assert stray_tags("<edit>\npath: a\n</edit>", {"edit", "run"}) == ["edit"]
    assert stray_tags("<lact e>edit(path='a', old='b', new='c')</lact>", {"edit"}) == []


def test_an_alias_used_twice_in_one_turn_keeps_the_first_and_reports_the_second():
    p = parse('<lvar c1>x</lvar>\n<lact c1>run(cmd="ls")</lact>\n<lact c2>run(cmd="pwd")</lact>')
    assert [v.alias for v in p.lvars] == ["c1"]
    assert [a.alias for a in p.lacts] == ["c2"]
    assert p.problems == ["alias 'c1' is used twice in one turn; the second <lact> was dropped"]


def test_json_words_read_as_python_literals():
    p = parse('<lact g>grep(pattern="x", ignore_case=true, limit=null, deep=[false])</lact>')
    assert p.lacts[0].args == {"pattern": "x", "ignore_case": True, "limit": None, "deep": [False]}


def test_guidance_names_the_runs_outputs_in_the_out_example():
    from lion1.lndl import guidance

    assert "OUT{done: [a1, ...]}" in guidance(("done",))
    assert "OUT{done: {field: value, ...}}" in guidance(("done",))
    assert "OUT{name: [a1, ...]}" in guidance()


@pytest.mark.parametrize(
    "text, fragment",
    [
        ('<lact w1>write(path="a", text="x")</lact> OUT{no colon}', "key: value"),
        ('<lact w1>write(path="a", text="x")</lact> OUT{report: {port: 9090, changed: [*a, 1}}', "neither"),
    ],
)
def test_an_unreadable_out_keeps_the_turns_commands_and_names_the_problem(text, fragment):
    p = parse(text)
    assert p.out is None
    assert [la.alias for la in p.lacts] == ["w1"]
    assert len(p.problems) == 1 and "ran without it" in p.problems[0] and fragment in p.problems[0]


def test_pointers_inside_out_literals():
    p = parse("OUT{summary: {port: 9090, changed: *ch, extra: [*a1, 2]}}")
    assert p.out.fields["summary"] == {"port": 9090, "changed": Pointer("ch"), "extra": [Pointer("a1"), 2]}
    assert parse("OUT{answer: *a1}").out.fields["answer"] == Pointer("a1")


@pytest.mark.parametrize(
    "text, fields",
    [
        ('OUT{answer: "hello, world"}', {"answer": "hello, world"}),
        ('OUT{answer: "open {"}', {"answer": "open {"}),
        ('OUT{done: {summary: "fixed }", files: []}}', {"done": {"summary": "fixed }", "files": []}}),
        (
            "OUT{done: {summary: 'first, foo: bar', files: []}}",
            {"done": {"summary": "first, foo: bar", "files": []}},
        ),
    ],
)
def test_out_strings_keep_their_commas_braces_and_colons(text, fields):
    program = parse(text)
    assert program.out is not None and program.out.fields == fields and program.problems == []


def test_guidance_describes_only_the_default_commands_the_profile_has():
    everything = guidance(("done",))
    assert "context.hide(refs=" in everything and "note.list() and note.find" in everything
    bare = guidance(("done",), commands=("read_file",))
    assert "context.hide" not in bare and "note.list" not in bare and "note.find" not in bare
    assert "<lvar note.key>text</lvar>" in bare  # writing a note needs no command


def test_a_literal_that_looks_like_the_heredoc_marker_is_a_string():
    program = parse('<lact a>echo(text="\\x00doc:0")</lact>')  # the model writes the escape, not the byte
    assert program.lacts[0].args == {"text": "\x00doc:0"} and program.problems == []
    assert parse('OUT{answer: "\\x00doc:0"}').out.fields == {"answer": "\x00doc:0"}


@pytest.mark.parametrize(
    "text, out, lacts",
    [
        (
            "OUT{answer: \"<lact a>echo(text='side effect')</lact>\"}",
            {"answer": "<lact a>echo(text='side effect')</lact>"},
            [],
        ),
        ('OUT{answer: "Explain OUT{x: 1}"}', {"answer": "Explain OUT{x: 1}"}, []),
        ('OUT{answer: """He said "hello, world" and }"""}', {"answer": 'He said "hello, world" and }'}, []),
        (
            '<lact a>echo(text="""x=<<EOF\nexample\nEOF\n""")</lact>',
            None,
            [("a", {"text": "x=<<EOF\nexample\nEOF\n"})],
        ),
        ("<lact a>echo(text=<<EOF\nEOF)</lact>", None, [("a", {"text": ""})]),
        (
            "<lact a>echo(text=<<EOF\nOUT{x: 1} is how you finish\nEOF)</lact>",
            None,
            [("a", {"text": "OUT{x: 1} is how you finish"})],
        ),
        ('OUT{done: [a]} <lact a>echo(text="after")</lact>', {"done": ["a"]}, [("a", {"text": "after"})]),
    ],
)
def test_literal_spans_are_opaque_to_construct_discovery(text, out, lacts):
    """A string's contents are never read for tags, OUT{} or heredocs, whichever pass would have
    seen them: one scan finds the constructs, and each construct owns its span."""
    p = lndl.parse(text)
    assert p.problems == []
    assert (p.out.fields if p.out else None) == out
    assert [(la.alias, la.args) for la in p.lacts] == lacts


def test_tag_shaped_prose_costs_a_notice_and_never_the_turn():
    """An opener with a bad alias, or with no closer, is prose: named in the problems, the scan goes
    on right after it, and the turn's real constructs still run. A tag quoted in markdown code is
    text and costs nothing."""
    p = lndl.parse('The form is <lact …>name(...)</lact>. <lact a1>echo(text="ran")</lact>')
    assert [(la.alias, la.args) for la in p.lacts] == [("a1", {"text": "ran"})]
    assert p.problems == ["<lact …>: '…' is not a valid alias; it was read as prose"]
    p = lndl.parse("I quoted `<lvar note.probe2>inner` earlier. <lvar note.findings>v7 clean</lvar>")
    assert [(v.alias, v.value) for v in p.lvars] == [("note.findings", "v7 clean")] and p.problems == []
    p = lndl.parse("Write <lvar note.key>value and close it. Nothing else this turn.")
    assert p.lvars == [] and p.problems == ["<lvar note.key> has no closing tag, so it was read as prose"]
    assert lndl.parse("<lvar note..>x</lvar>").problems == [
        "<lvar note..>: 'note..' is not a valid alias; it was read as prose"
    ]
    fenced = '```\n<lact a1>echo(text="quoted")</lact>\n```\n<lact a2>echo(text="ran")</lact>'
    assert [la.alias for la in lndl.parse(fenced).lacts] == ["a2"]


@pytest.mark.parametrize("literal", ["True", "False", "None"])
def test_an_alias_spelled_true_false_or_none_is_refused_at_declaration(literal):
    # OUT{} never reads these three spellings as Aliases (they fall back to the Python
    # literal), so a declared <lvar True> was silently mis-read instead of resolving; refuse it
    # at declaration, the same place `<lvar note..>` is refused above.
    p = lndl.parse(f'<lvar {literal}>x</lvar> <lact a1>echo(text="ran")</lact>')
    assert p.lvars == []  # not declared
    assert [la.alias for la in p.lacts] == ["a1"]  # the rest of the turn still runs
    assert p.problems == [
        f"<lvar {literal}>: alias {literal!r} reads as a Python literal in OUT{{}}; pick another; it was "
        "read as prose"
    ]


def test_a_value_holding_a_tag_keeps_it_whole():
    """Same-kind tags nest, so a note that quotes a complete tag keeps it verbatim; a bare opener
    quoted inside the value does not balance, and then the first closer ends the value as it always
    did, the quoted opener kept as text. The other kind inside a value is text."""
    p = lndl.parse(
        "<lvar note.f>finding: <lvar x>inner</lvar> truncates; <lact y>run()</lact> is inert</lvar>"
    )
    assert [(v.alias, v.value) for v in p.lvars] == [
        ("note.f", "finding: <lvar x>inner</lvar> truncates; <lact y>run()</lact> is inert")
    ]
    assert p.lacts == [] and p.problems == []
    p = lndl.parse("<lvar note.f>notes are written with <lvar note.KEY>, then closed</lvar> tail")
    assert [(v.alias, v.value) for v in p.lvars] == [
        ("note.f", "notes are written with <lvar note.KEY>, then closed")
    ]
    assert p.problems == []
