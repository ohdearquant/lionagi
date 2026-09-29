import asyncio

from lion_cli.view import Console
from pydantic import BaseModel

from lionagi import Actor, Profile, RunRequest, lndl
from lionagi.record import Kind
from tests.test_actor import scripted
from tests.test_actor_execution import Mark, _exec_actor, _exec_run


def test_same_tag_text_inside_closed_body_remains_verbatim():
    body = "literal </lact>\nexample=<<EOF\npayload"
    turn = f"<lact e>edit(new=<<EOF\n{body}\nEOF)</lact>"
    p = lndl.parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [("e", {"new": body})]
    assert p.problems == []
    assert [turn[a:b] for a, b in lndl.spans(turn)] == [turn]


def test_call_shaped_body_text_does_not_invoke_a_handler():
    class Edit(BaseModel):
        new: str

    class Run(BaseModel):
        cmd: str

    actor = Actor("opaque-body")
    written = []
    executed = []

    @actor.handler(Edit)
    async def edit(req, ctx):
        written.append(req.new)
        return "recorded"

    @actor.handler(Run)
    async def run(req, ctx):
        executed.append(req.cmd)
        return "recorded, no shell used"

    body = 'literal </lact>\nexample=<<EOF\n<lact x>run(cmd="only example text")</lact>'
    turn = f"<lact e>edit(new=<<EOF\n{body}\nEOF)</lact>"
    asyncio.run(actor.run(Profile("p"), RunRequest(max_rounds=1), scripted(turn)))
    assert executed == [], executed
    assert written == [body]


def test_fake_heredoc_inside_string_does_not_unprotect_its_closing_tag():
    value = "x=<<EOF\n</lact>\nEOF)"
    turn = '<lact e>echo(text="""' + value + '""")</lact>'
    p = lndl.parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [("e", {"text": value})]
    assert p.problems == []


def _left_open_then(turn, calls, lacts):
    # the quote left open at the first closing tag pairs with a later one: that tag still ends the call
    assert [turn[a:b] for a, b in lndl.spans(turn)] == calls
    p = lndl.parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == lacts
    assert len(p.problems) == 1 and p.problems[0].startswith("<lact e>") and "closers" in p.problems[0]


def test_a_string_left_open_before_apostrophes_ends_its_call_at_the_first_closing_tag():
    _left_open_then(
        '<lact e>edit(new="y</lact>\n<lact r>run(cmd="it\'s")</lact>\n'
        "Don't worry, <lact s>say(x='a')</lact>",
        ['<lact e>edit(new="y</lact>', '<lact r>run(cmd="it\'s")</lact>', "<lact s>say(x='a')</lact>"],
        [("e", {"new": "y"}), ("r", {"cmd": "it's"}), ("s", {"x": "a"})],
    )


def test_a_string_left_open_before_an_odd_prose_quote_ends_its_call_at_the_first_closing_tag():
    _left_open_then(
        '<lact e>edit(new="y</lact>\nI think "it works.\n<lact r>run(cmd="ls")</lact>',
        ['<lact e>edit(new="y</lact>', '<lact r>run(cmd="ls")</lact>'],
        [("e", {"new": "y"}), ("r", {"cmd": "ls"})],
    )


def test_a_triple_quote_left_open_before_a_prose_triple_quote_ends_its_call_at_the_first_closing_tag():
    _left_open_then(
        '<lact e>edit(new="""y</lact>\n""" <lact r>run(cmd="ls")</lact>',
        ['<lact e>edit(new="""y</lact>', '<lact r>run(cmd="ls")</lact>'],
        [("e", {"new": "y"}), ("r", {"cmd": "ls"})],
    )


def test_a_quote_left_open_in_a_value_that_is_not_a_call_ends_it_at_the_first_closing_tag():
    turn = "<lact e>echo \"hi</lact>\n<lact r>run(cmd=\"it's\")</lact>\nDon't, <lact s>say(x='a')</lact>"
    assert [turn[a:b] for a, b in lndl.spans(turn)] == [
        '<lact e>echo "hi</lact>',
        '<lact r>run(cmd="it\'s")</lact>',
        "<lact s>say(x='a')</lact>",
    ]
    p = lndl.parse(turn)
    assert [(la.alias, la.args) for la in p.lacts] == [("r", {"cmd": "it's"}), ("s", {"x": "a"})]
    assert [(v.alias, v.value) for v in p.lvars] == [("e", 'echo "hi')]
    assert len(p.problems) == 1 and p.problems[0].startswith("<lact e> held a value"), p.problems


def test_console_still_surfaces_failure_when_result_cannot_land():
    actor = Actor("console-no-landing")

    @actor.handler(Mark)
    async def squat(req, ctx):
        ctx.run.record.append(Kind.INPUT, "taken", alias=ctx.alias)
        return "ran"

    run, notice = _exec_run(actor, '<lact s1>mark(path="a")</lact>')
    result = run.record.get("s1")
    assert result.kind is Kind.INPUT
    system = [e for e in run.record if e.kind is Kind.SYSTEM][-1]
    rendered = Console("model", color=False).render(system)
    assert rendered and any("s1" in str(piece) for piece in rendered), (notice, rendered)


def test_a_started_failure_whose_result_landed_prints_once_as_its_result():
    for turn, alias in (
        ('<lact t1>touch(path="a")</lact>', "t1"),  # failed after starting
        ('<lact h>hold()</lact><lact k>cut(target="h")</lact>', "h"),  # completion unknown
    ):
        run, _ = _exec_run(_exec_actor("console-landed", [], []), turn)
        say = Console("model", color=False)
        pieces = [str(piece) for e in run.record for piece in say.render(e)]
        named = [p for p in pieces if f" {alias} " in p and "→" not in p]  # past the call line itself
        assert len(named) == 1 and named[0].startswith(f"[red]✗ {alias} "), pieces
