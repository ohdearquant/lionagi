"""A provider's own markup around LNDL is the backend's to rewrite: the parser knows no provider
(ADR-0004/D2), so `hub.harness.markup` turns a model's native tool-call syntax, in the text or
parsed out into `tool_calls`, into the tags the parser reads."""

from hub.harness.markup import normalize, tool_calls_text
from lionagi.lndl import parse


def test_native_tool_calls_carrying_lndl_become_the_text_the_model_wrote():
    calls = [
        {
            "type": "function",
            "function": {
                "name": 'lact a1>list_dir(path=".")</lact>\n'
                '<｜DSML｜ invoke name="lact a2>grep(pattern="class C", path="d.py")</lact>\n'
                "</｜DSML｜ calls>",
                "arguments": "{}",
            },
        }
    ]
    p = parse(normalize(tool_calls_text(calls)))
    assert [(la.alias, la.name, la.args) for la in p.lacts] == [
        ("a1", "list_dir", {"path": "."}),
        ("a2", "grep", {"pattern": "class C", "path": "d.py"}),
    ]
    # the provider swallowed the closing tags as its own parameter closes
    swallowed = [
        {
            "function": {
                "name": 'lact a9>read_file(path="t/m.py")</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n'
                '<｜DSML｜ invoke name="lact a10>read_file(path="t/t.py", start=1, end=120)'
                "</｜DSML｜ parameter>\n"
                "</｜DSML｜ invoke>\n</｜DSML｜ calls>",
                "arguments": "{}",
            }
        }
    ]
    p = parse(normalize(tool_calls_text(swallowed)))
    assert [(la.alias, la.args) for la in p.lacts] == [
        ("a9", {"path": "t/m.py"}),
        ("a10", {"path": "t/t.py", "start": 1, "end": 120}),
    ]
    # a whole native call, real name and JSON arguments, is one <lact> under a generated alias
    native = [{"function": {"name": "run", "arguments": '{"cmd": "ls\\ncat x", "timeout": 5}'}}]
    p = parse(normalize(tool_calls_text(native)))
    assert p.lacts[0].name == "run" and p.lacts[0].args == {"cmd": "ls\ncat x", "timeout": 5}
    assert p.lacts[0].alias.startswith("auto_") and not p.problems


def test_native_markup_around_a_tag_is_read_as_the_tag():
    text = (
        "<｜DSML｜ calls>\n"
        '<｜DSML｜ invoke name="lact g1>grep(pattern="def f", path="a.py")</lact>\n'
        '<｜DSML｜ invoke name="lact r1>read_file(path="a.py", start=1, end=20)</lact>\n'
        "</｜DSML｜ calls>"
    )
    p = parse(normalize(text))
    assert [(a.alias, a.name) for a in p.lacts] == [("g1", "grep"), ("r1", "read_file")]
    assert p.lacts[1].args == {"path": "a.py", "start": 1, "end": 20}


def test_a_whole_native_call_becomes_a_lact_under_a_generated_alias():
    text = (
        "<｜DSML｜ calls>\n"
        '<｜DSML｜ invoke name="read_file">\n'
        '<｜DSML｜ parameter name="path" string="true">django/forms/models.py</｜DSML｜ parameter>\n'
        '<｜DSML｜ parameter name="start" string="false">1150</｜DSML｜ parameter>\n'
        "</｜DSML｜ invoke>\n"
        '<｜DSML｜ invoke name="run">\n'
        '<｜DSML｜ parameter name="cmd" string="true">ls; git log --oneline -3\ncat x</｜DSML｜ parameter>\n'
        "</｜DSML｜ invoke>\n"
        "</｜DSML｜ calls>"
    )
    p = parse(normalize(text))
    assert [a.name for a in p.lacts] == ["read_file", "run"]
    assert p.lacts[0].args == {"path": "django/forms/models.py", "start": 1150}
    assert p.lacts[1].args == {"cmd": "ls; git log --oneline -3\ncat x"}
    assert all(a.alias.startswith("auto_") for a in p.lacts) and p.lacts[0].alias != p.lacts[1].alias
    assert (
        parse(normalize(text)).lacts[0].alias == p.lacts[0].alias
    )  # stable: the same call names the same alias


def test_text_without_the_markup_and_a_well_formed_literal_pass_through_untouched():
    plain = '<lact a>run(cmd="ls")</lact>\nOUT{answer: "<｜DSML｜ invoke name="lact z>x()">"}'
    assert normalize("hello") == "hello"
    assert normalize(plain) == plain  # the markup inside an OUT string is the string's, not a tag


INNER = (
    "<｜DSML｜ calls>\n"
    '<｜DSML｜ invoke name="echo">\n'
    '<｜DSML｜ parameter name="text" string="true">'
    '<lact inner>rm(path="x")</lact></｜DSML｜ parameter>\n'
    "</｜DSML｜ invoke>\n"
    "</｜DSML｜ calls>"
)


def test_a_tag_inside_a_native_calls_string_argument_is_that_arguments_text():
    # the well-formed <lact inner> inside the parameter was kept as a construct of its own and the
    # native markup around it stripped as noise: the argument became a call, the call was lost
    p = parse(normalize(INNER))
    assert [(la.name, la.args) for la in p.lacts] == [("echo", {"text": '<lact inner>rm(path="x")</lact>'})]


def test_a_tag_whose_argument_holds_native_markup_is_left_as_written():
    text = '<lact a>echo(text="<｜DSML｜ invoke name=\\"x\\"> literal")</lact>'
    assert normalize(text) == text


def test_two_identical_native_calls_in_one_turn_are_two_calls():
    # the alias was a digest of the call's text alone, so two equal calls got one alias and the
    # parser kept the first
    one = (
        '<｜DSML｜ invoke name="echo">\n'
        '<｜DSML｜ parameter name="text" string="true">again</｜DSML｜ parameter>\n'
        "</｜DSML｜ invoke>\n"
    )
    p = parse(normalize("<｜DSML｜ calls>\n" + one + one + "</｜DSML｜ calls>"))
    assert [(la.name, la.args) for la in p.lacts] == [("echo", {"text": "again"})] * 2
    assert len({la.alias for la in p.lacts}) == 2
    call = {"function": {"name": "echo", "arguments": '{"text": "again"}'}}
    p = parse(tool_calls_text([call, call]))  # the same for calls the provider parsed out
    assert len(p.lacts) == 2 and len({la.alias for la in p.lacts}) == 2
