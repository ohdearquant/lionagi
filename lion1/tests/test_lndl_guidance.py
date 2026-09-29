import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lionagi.lndl import Pointer, guidance, parse, parse_call

# The OUT example line is padded to a column; the padding is checked on its own below, so the golden
# texts compare it squeezed to one space.
_OUT_PAD = re.compile(r"^(  OUT\{[^\n]*?: \[a1, \.\.\.\]\}) +(?=finish:)", re.MULTILINE)


def _squeezed(text: str) -> str:
    return _OUT_PAD.sub(r"\1 ", text)


def _swap(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


ALL = """\
You act by writing LNDL in your reply. Three constructs, where `a1` stands for an alias you pick (a bare \
word, written right after the tag name):
  <lvar a1>value</lvar>                       name a value so you can point at it later
  <lact a1>name(field=value, ...)</lact>      ask for something to happen; `name` is one of the commands \
listed below; arguments are literals or pointers like *a1
  OUT{name: [a1, ...]} finish: the aliases fill the named output (this run's outputs: listed below); a \
single-field output also takes a literal, OUT{name: 3}, and any output takes a dict literal of its fields, \
OUT{name: {field: value, ...}}
Example: <lact f1>read_file(path="src/x.py")</lact> then, next round, <lact g1>grep(pattern="def ", \
text=*f1)</lact>. A multi-line value, or one with quotes in it (a script, a code block, a shell line), is \
written as a heredoc argument, nothing escaped:
  <lact r1>run(cmd=<<EOF
  ...the lines, verbatim...
  EOF)</lact>
or goes in its own <lvar s1>...</lvar> and is passed as a pointer, run(cmd=*s1).
A pointer *alias refers to anything you named, to any entry by its record name (_r41, _m12; names starting \
with _ are the record's), or to a note (note.key). Values are never repeated for you; point at them.
Notes outlive this run: <lvar note.key>text</lvar> writes one under `key`, *note.key reads it, note.list() \
and note.find(query="...") find them, note.get(key="...") reads one whole, note.delete(key="...") removes \
one, and the next run of this profile starts with them in reach. Write a note when you have established \
something a later round or a later run should not have to rediscover: a fact about the material, the \
plan, what is still unverified. A few lines each; write the same key again as it changes.
Your view is the record: everything stays in it where it landed, addressable by name, until you hide it \
or the view outgrows its budget, when the runtime folds the oldest results to placeholders once. Hide in \
one batch when a phase ends (context.hide(refs=["_r41", "_r42"])), replace a finished span with a few \
lines (context.summarize), and bring an entry back when you need it again (context.show).
A turn without OUT{} runs every <lact> and continues; their results arrive in the next system \
notification under their aliases. A turn with OUT{} runs every <lact> too, but OUT{} waits only for what \
it names or points at: once it is accepted the run ends and any <lact> not finished is cancelled.
Aliases are unique within a turn. Tags are siblings; a complete tag quoted inside a value stays text."""

NONE = """\
You act by writing LNDL in your reply. Three constructs, where `a1` stands for an alias you pick (a bare \
word, written right after the tag name):
  <lvar a1>value</lvar>                       name a value so you can point at it later
  <lact a1>name(field=value, ...)</lact>      ask for something to happen; `name` is one of the commands \
listed below; arguments are literals or pointers like *a1
  OUT{name: [a1, ...]} finish: the aliases fill the named output (this run's outputs: listed below); a \
single-field output also takes a literal, OUT{name: 3}, and any output takes a dict literal of its fields, \
OUT{name: {field: value, ...}}
Example: <lact f1>first(field="value")</lact> then, next round, <lact g1>second(text=*f1)</lact>, `first` \
and `second` standing for commands listed below. A multi-line value, or one with quotes in it (a script, \
a code block, a shell line), is written as a heredoc argument, nothing escaped:
  <lact r1>name(field=<<EOF
  ...the lines, verbatim...
  EOF)</lact>
or goes in its own <lvar s1>...</lvar> and is passed as a pointer, name(field=*s1).
A pointer *alias refers to anything you named, to any entry by its record name (_r41, _m12; names starting \
with _ are the record's), or to a note (note.key). Values are never repeated for you; point at them.
Notes outlive this run: <lvar note.key>text</lvar> writes one under `key`, *note.key reads it, the next \
run of this profile starts with them in reach. Write a note when you have established something a later \
round or a later run should not have to rediscover: a fact about the material, the plan, what is still \
unverified. A few lines each; write the same key again as it changes.
Your view is the record: everything stays in it where it landed, addressable by name, until you hide it \
or the view outgrows its budget, when the runtime folds the oldest results to placeholders once.
A turn without OUT{} runs every <lact> and continues; their results arrive in the next system \
notification under their aliases. A turn with OUT{} runs every <lact> too, but OUT{} waits only for what \
it names or points at: once it is accepted the run ends and any <lact> not finished is cancelled.
Aliases are unique within a turn. Tags are siblings; a complete tag quoted inside a value stays text."""

DEFAULTS = (
    "read_file",
    "grep",
    "run",
    "note.list",
    "note.find",
    "note.get",
    "note.delete",
    "context.hide",
    "context.summarize",
    "context.show",
)

LOOKUP = 'note.list() and note.find(query="...") find them, '
GET = 'note.get(key="...") reads one whole, '
DELETE = 'note.delete(key="...") removes one, '
CURATING = (
    ' Hide in one batch when a phase ends (context.hide(refs=["_r41", "_r42"])), replace a finished span '
    "with a few lines (context.summarize), and bring an entry back when you need it again (context.show)."
)


def _with_outputs(text: str, outputs: tuple[str, ...]) -> str:
    o = outputs[0]
    text = _swap(text, "OUT{name: [a1, ...]}", f"OUT{{{o}: [a1, ...]}}")
    text = _swap(text, "(this run's outputs: listed below)", f"(this run's outputs: {', '.join(outputs)})")
    text = _swap(text, "OUT{name: 3}", f"OUT{{{o}: 3}}")
    return _swap(text, "OUT{name: {field: value, ...}}", f"OUT{{{o}: {{field: value, ...}}}}")


def test_the_guidance_for_every_command_is_this_text():
    assert _squeezed(guidance()) == ALL
    assert guidance(commands=None) == guidance()


def test_a_profile_listing_every_default_command_gets_the_same_text_as_one_with_all_commands():
    assert guidance(commands=DEFAULTS) == guidance()
    assert guidance(("done",), commands=list(DEFAULTS) + ["edit"]) == guidance(("done",))


def test_the_guidance_for_a_profile_with_no_commands_is_this_text():
    assert _squeezed(guidance(commands=())) == NONE


def test_the_guidance_names_the_runs_outputs():
    assert _squeezed(guidance(("report",))) == _with_outputs(ALL, ("report",))
    assert _squeezed(guidance(("report", "notes"))) == _with_outputs(ALL, ("report", "notes"))
    assert _squeezed(guidance(("report",), commands=())) == _with_outputs(NONE, ("report",))


def test_a_subset_without_note_or_context_commands_gets_the_concrete_examples_and_nothing_else():
    text = guidance(commands=("read_file", "grep", "run"))
    expected = _swap(_swap(ALL, LOOKUP + GET + DELETE + "and ", ""), CURATING, "")
    assert _squeezed(text) == expected


@pytest.mark.parametrize(
    "commands, lookup",
    [
        (("note.list",), ""),
        (("note.find",), ""),
        (("note.list", "note.find"), LOOKUP + "and "),
        (("note.list", "note.find", "note.get"), LOOKUP + GET + "and "),
        (("note.list", "note.find", "note.delete"), LOOKUP + DELETE + "and "),
        (("note.get", "note.delete"), ""),
    ],
)
def test_the_note_commands_are_described_only_as_far_as_the_profile_has_them(commands, lookup):
    text = guidance(commands=commands)
    assert _squeezed(text) == _swap(
        NONE, "*note.key reads it, the next run", f"*note.key reads it, {lookup}the next run"
    )


@pytest.mark.parametrize(
    "commands, curating",
    [
        (("context.hide",), False),
        (("context.summarize",), False),
        (("context.show",), False),
        (("context.hide", "context.summarize"), False),
        (("context.summarize", "context.show"), False),
        (("context.hide", "context.show"), False),
        (("context.hide", "context.summarize", "context.show"), True),
    ],
)
def test_curating_the_view_is_described_only_when_the_profile_has_all_three_commands(commands, curating):
    text = guidance(commands=commands)
    once = "placeholders once."
    assert _squeezed(text) == (_swap(NONE, once, once + CURATING) if curating else NONE)


@pytest.mark.parametrize(
    "commands, concrete_example, concrete_run",
    [
        (("read_file",), False, False),
        (("grep",), False, False),
        (("read_file", "grep"), True, False),
        (("run",), False, True),
    ],
)
def test_the_examples_name_real_commands_only_when_the_profile_has_them(
    commands, concrete_example, concrete_run
):
    text = guidance(commands=commands)
    assert ('<lact f1>read_file(path="src/x.py")</lact>' in text) is concrete_example
    assert ('<lact f1>first(field="value")</lact>' in text) is not concrete_example
    assert ("<lact r1>run(cmd=<<EOF\n" in text and "pointer, run(cmd=*s1).\n" in text) is concrete_run
    assert (
        "<lact r1>name(field=<<EOF\n" in text and "pointer, name(field=*s1).\n" in text
    ) is not concrete_run


def _presented(name: str, text: str) -> bool:
    if name == "run":  # `run` is also an English word in the text
        return "run(" in text
    return re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", text) is not None


@given(st.sets(st.sampled_from(DEFAULTS)), st.lists(st.from_regex(r"[a-z][a-z_]{0,8}", fullmatch=True)))
def test_a_command_outside_the_subset_is_never_presented(subset, others):
    # ADR-0001/C5: the prompt is generated from the profile's subset alone
    commands = set(subset) | (set(others) - set(DEFAULTS))
    text = guidance(commands=commands)
    for name in DEFAULTS:
        if name not in commands:
            assert not _presented(name, text), name


def test_every_default_command_is_presented_when_the_profile_has_them_all():
    # the control for the property above: the instrument finds each name when it is there
    text = guidance(commands=DEFAULTS)
    assert all(_presented(name, text) for name in DEFAULTS)


def test_the_heredoc_is_named_before_the_pointer_form():
    # ADR-0004/D3: the heredoc is the first form for a multi-line or quoted value
    for text in (guidance(), guidance(commands=())):
        heredoc = text.index("is written as a heredoc argument")
        pointer = text.index("or goes in its own <lvar s1>...</lvar> and is passed as a pointer")
        opening = text.index("=<<EOF\n")
        assert heredoc < opening < pointer


def test_the_examples_in_the_guidance_read_as_they_are_shown():
    text = guidance(("report",))
    first = '<lact f1>read_file(path="src/x.py")</lact>'
    second = '<lact g1>grep(pattern="def ", text=*f1)</lact>'
    heredoc = "<lact r1>run(cmd=<<EOF\n  ...the lines, verbatim...\n  EOF)</lact>"
    for snippet in (first, second, heredoc, "<lvar note.key>text</lvar>", "OUT{report: 3}"):
        assert snippet in text, snippet
    p = parse(f"{first}\n{second}\n{heredoc}\n<lvar note.key>text</lvar>\nOUT{{report: 3}}")
    assert [(a.alias, a.name, a.args) for a in p.lacts] == [
        ("f1", "read_file", {"path": "src/x.py"}),
        ("g1", "grep", {"pattern": "def ", "text": Pointer("f1")}),
        ("r1", "run", {"cmd": "  ...the lines, verbatim..."}),
    ]
    assert [(v.alias, v.value) for v in p.lvars] == [("note.key", "text")]
    assert p.out.fields == {"report": 3} and p.problems == []
    assert parse_call("run(cmd=*s1)") == ("run", {"cmd": Pointer("s1")})
    assert parse_call('context.hide(refs=["_r41", "_r42"])') == ("context.hide", {"refs": ["_r41", "_r42"]})
    assert parse_call('note.find(query="...")') == ("note.find", {"query": "..."})


def _column(line: str) -> int:
    return re.match(r"\s*\S+(?: \S+)*? {2,}|\s*\S+ ", line).end()


def test_the_out_line_description_starts_at_the_same_column_whatever_the_output_name():
    columns = set()
    for outputs in ((), ("x",), ("report",), ("a_much_longer_name",)):
        line = guidance(outputs).splitlines()[3]
        columns.add(line.index("finish:"))
    assert len(columns) == 1


def test_an_output_name_too_long_for_the_column_is_followed_by_one_space():
    o = "an_output_name_longer_than_the_column"
    assert f"  OUT{{{o}: [a1, ...]}} finish: the aliases" in guidance((o,))


@pytest.mark.xfail(
    strict=True,
    reason="known defect: the OUT line's description starts one column left of the two lines above it",
)
def test_the_three_construct_lines_align_their_descriptions():
    lines = guidance(("report",)).splitlines()[1:4]
    assert len({_column(line) for line in lines}) == 1
