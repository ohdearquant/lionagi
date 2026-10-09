"""A Claude Code session's checkpoint (ADR-0019): the run's take on the session's vocabulary, its view
the session's turns folded, a hook's frame refused, and the restore: the first prompt of a fresh
session, whose first line re-arms the watches. The transcript a take reads is one file state, its
prompts as typed."""

import asyncio
import dataclasses
import json
import os
import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from lion_cli.main import main

from hub.backend.claude_code import checkpoint as cc
from hub.backend.claude_code.checkpoint import (
    DIRECTIVES,
    SESSION_KINDS,
    SESSION_WHYS,
    directives,
    fold_turns,
    newest_transcript,
    posture,
    render_prompt,
    slug,
    take_session,
    transcript_source,
    transcript_turns,
)
from lion1.checkpoint import KINDS, LAZY, WHYS, CheckpointError, Var
from tests.lionfake import LionKhive
from tests.test_checkpoint import ACTOR, AT, ID, Book, Mark, Tags, turn, var
from tests.test_codex_checkpoint import at, write
from tests.test_context import actor_dir, gh_says, held, load, store_khive

IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


def typed(content, uid: str) -> dict:
    """A `user` row the person typed: words, or blocks of text and images."""
    return {
        "type": "user",
        "uuid": uid,
        "promptSource": "typed",
        "message": {"role": "user", "content": content},
    }


def said(text: str) -> dict:
    """An `assistant` row holding one text block."""
    content = [{"type": "text", "text": text}]
    return {
        "type": "assistant",
        "uuid": "a",
        "message": {"role": "assistant", "model": "m", "content": content},
    }


def test_the_session_vocabulary_is_the_runs_and_the_sessions_own():
    assert SESSION_KINDS == (*KINDS, "watch", "spawn", "lane", "directive", "topic")
    assert SESSION_WHYS == (*WHYS, "rotate", "precompact")
    assert DIRECTIVES == 8
    c = take_session(
        {"session_id": "s1", "transcript": "/t.jsonl"}, [], actor=ACTOR, why="precompact", budget=9
    )
    assert c.source == {"kind": "claude-code", "session_id": "s1", "transcript": "/t.jsonl"}
    assert c.view == {"budget": 9, "kept": [], "hidden": [], "summaries": []}
    with pytest.raises(
        CheckpointError, match="why 'later': one of threshold, end, manual, rotate, precompact"
    ):
        take_session({}, [], actor=ACTOR, why="later", budget=1)
    with pytest.raises(CheckpointError, match="kind 'blob' is not one of fact, posture, watch, spawn, lane"):
        take_session({}, [var("v", "blob", 1)], actor=ACTOR, why="manual", budget=1)


def test_a_session_take_refuses_a_hooks_frame_in_a_directive_or_a_kept_input():
    framed = (
        "<system-reminder>injected</system-reminder> and then the words",
        "  <command-name>/compact</command-name>",
        "<command-message>compact</command-message>",
        "<local-command-stdout>x</local-command-stdout>",
        "<task-notification>done</task-notification>",
        "the words, then <system-reminder>injected</system-reminder>",
        "an opener alone <system-reminder> is a mark",
        "a closer alone </system-reminder> is a mark",
        '<system-reminder source="hook">attributed</system-reminder> and words',
    )
    for text in framed:
        with pytest.raises(CheckpointError, match="directive 'd' carries a hook's frame; whatever read it"):
            take_session({}, [var("d", "directive", text, "hook")], actor=ACTOR, why="manual", budget=1)
        view = {
            "budget": 1,
            "kept": [{"turn": "u1", "at": "t", "input": text, "text": ""}],
            "hidden": [],
            "summaries": [],
        }
        with pytest.raises(CheckpointError, match="turn u1: its input carries a hook's frame"):
            take_session({}, [], actor=ACTOR, why="manual", budget=1, view=view)
    # a frame named mid-text is words about a frame; a fact may hold anything; an input may be None
    ok = take_session(
        {},
        [
            var("d", "directive", "said: <command-name> is a frame", "hook"),
            var("f", "fact", "<system-reminder>"),
        ],
        actor=ACTOR,
        why="manual",
        budget=1,
        view={
            "budget": 1,
            "kept": [{"turn": "u1", "at": "t", "input": None, "text": "x"}],
            "hidden": [],
            "summaries": [],
        },
    )
    assert len(ok.vars) == 2 and ok.view["kept"][0]["input"] is None


def test_a_long_var_is_its_name_size_and_loader_while_directives_and_watches_stay_whole():
    words = "placeholder words, said once " * 40
    command = "bash /tmp/long-watch.sh " + "a" * 500
    lanes = [{"kind": "task", "id": f"t{i}", "title": "placeholder lane"} for i in range(20)]
    given = [
        var("directive.1", "directive", words, "hook"),
        var(
            "watch.t1", "watch", {"command": command, "description": "long", "timeout_ms": None}, "transcript"
        ),
        var("posture", "posture", "q" * (LAZY + 1), "notes", "7", ref='note.get(key="posture")'),
        var("lanes", "lane", lanes, "khive"),
        var("short", "fact", "s" * LAZY),
        var("harvested", "fact", None, "transcript", size=1234),
    ]
    assert len(words) > LAZY < len(command)
    c = take_session({"session_id": "s1"}, given, actor=ACTOR, why="manual", budget=100, now=AT)
    by = {v.name: v for v in c.vars}
    # a ref names the store that keeps the value; without one the checkpoint carries it
    assert (by["posture"].value, by["posture"].size) == (None, LAZY + 1)
    assert by["lanes"].value == lanes and by["lanes"].size == len(json.dumps(lanes, ensure_ascii=False))
    assert by["short"].size is None and by["directive.1"].size is None and by["watch.t1"].size is None
    assert by["harvested"] == given[5]  # a size already set is a harvester's word: not recomputed
    prompt = render_prompt(c)
    assert words in prompt and command in prompt and "s" * LAZY in prompt
    assert "q" * (LAZY + 1) not in prompt and '"t19"' not in prompt
    assert f'{LAZY + 1} chars, not shown here; load it with `note.get(key="posture")`' in prompt
    show = f"chars, not shown here; load it with `lion context show --id {ID} --var"
    assert f"- {by['lanes'].size} {show} lanes`" in prompt
    assert f"- harvested: 1234 {show} harvested`" in prompt


def test_fold_turns_folds_the_oldest_first_until_the_rest_fits_half_the_budget():
    turns = [turn(i, "t" * 40) for i in range(4)]  # (8 + 40) // 4 = 12 each
    whole = fold_turns(turns, 100)
    assert whole == {"budget": 100, "kept": turns, "hidden": [], "summaries": []}
    assert (
        whole["kept"][0] == turns[0] and whole["kept"][0] is not turns[0]
    )  # a copy, the turn is the caller's
    half = fold_turns(turns, 48)  # 48 to fit in 24: two go
    assert half["kept"] == turns[2:] and half["hidden"] == [
        "u0@2026-09-24T10:00:00Z",
        "u1@2026-09-24T10:00:01Z",
    ]
    assert fold_turns(turns, 0)["kept"] == turns[3:]  # the latest turn always stays
    assert fold_turns([], 10) == {"budget": 10, "kept": [], "hidden": [], "summaries": []}
    bare = fold_turns([{"turn": "u", "at": "a", "input": None, "text": None}], 0)
    assert bare["kept"] == [{"turn": "u", "at": "a", "input": None, "text": None}]


@given(st.lists(st.tuples(st.text(max_size=50), st.text(max_size=50)), max_size=12), st.integers(0, 200))
def test_fold_turns_keeps_the_latest_and_folds_no_more_than_it_must(pairs, budget):
    turns = [
        {"turn": f"u{i}", "at": str(i), "input": inp, "text": text} for i, (inp, text) in enumerate(pairs)
    ]
    view = fold_turns(turns, budget)
    hidden = len(view["hidden"])
    assert len(view["kept"]) + hidden == len(turns) and view["summaries"] == [] and view["budget"] == budget
    assert view["hidden"] == [f"u{i}@{i}" for i in range(hidden)] and view["kept"] == turns[hidden:]
    if turns:
        size = [(len(t["input"]) + len(t["text"])) // 4 for t in turns]
        assert sum(size[hidden:]) <= budget // 2 or len(view["kept"]) == 1
        if hidden:
            assert sum(size[hidden - 1 :]) > budget // 2  # the last one folded had to go


def test_the_first_line_re_arms_each_watch_verbatim_before_any_work():
    turns = [turn(1, "t" * 400), turn(2, "text two"), turn(3, "", kind="event"), turn(4, "text four")]
    view = fold_turns(turns, 100)  # 102 + 4 + 2 + 4: the first goes
    source = {
        "session_id": "s1",
        "transcript": "/t/x.jsonl",
        "lanes": "tasks active",
        "context": {"used": 900, "window": 1000, "pct": 90},
    }
    inbox = {
        "command": "bash /tmp/w.sh `x`",
        "description": "the inbox",
        "timeout_ms": 1800000,
        "persistent": True,
    }
    given = [
        var("watch.m1", "watch", inbox, "transcript", "10:00"),
        var("watch.m2", "watch", {"command": "cat <<'EOF'\n```\nEOF"}, "transcript"),
        var("watch.m3", "watch", "bash /tmp/three.sh", "transcript"),
        var("mcp", "fact", ["khive", "lion"]),
        var("loop", "fact", "/x/.khive/loop/x_loop.md"),
        var("next", "fact", "placeholder next"),
        var("branch", "fact", "src/lion1/checkpoint"),
        *[var(f"directive.{i}", "directive", f"placeholder word {i}", "hook", f"10:0{i}") for i in range(9)],
        var(
            "lane.t1",
            "lane",
            {"kind": "task", "id": "t1", "title": "placeholder lane", "awaiting": None},
            "khive",
        ),
        var("spawn.a1", "spawn", {"id": "a1", "task": "job one", "state": "running-at-take"}, "transcript"),
        var("topic", "topic", "placeholder-topic", "registry"),
        var("posture", "posture", "quiet now", "transcript"),
        var("loop.posture", "posture", "holding", "notes"),
    ]
    c = take_session(source, given, actor=ACTOR, why="threshold", budget=100, view=view, now=AT)
    expected = [
        "Checkpoint s1@4 of actor:one, taken 2026-09-24T10:05:00+00:00 (threshold); context at the take "
        "~900 of 1000 tokens (90%). Before any other work, re-arm these 3 watch(es) with the Monitor tool, "
        "each command exactly as written:",
        "1. the inbox (timeout_ms 1800000, persistent True):",
        "```",
        "bash /tmp/w.sh `x`",
        "```",
        "2. watch.m2:",
        "````",
        "cat <<'EOF'",
        "```",
        "EOF",
        "````",
        "3. watch.m3:",
        "```",
        "bash /tmp/three.sh",
        "```",
        'MCP servers expected: ["khive", "lion"]',
        "Loop file: /x/.khive/loop/x_loop.md",
        "",
        "The person's words, verbatim, oldest first (8):",
        *[line for i in range(1, 9) for line in (f"[10:0{i}]", "```", f"placeholder word {i}", "```")],
        "",
        "Lanes (tasks active):",
        '- {"kind": "task", "id": "t1", "title": "placeholder lane", "awaiting": null}',
        "",
        "Spawned agents the transcript shows running at the take:",
        '- {"id": "a1", "task": "job one", "state": "running-at-take"}',
        "",
        "Topic: placeholder-topic",
        "",
        "Declared:",
        "- branch: src/lion1/checkpoint",
        "",
        "The last 3 exchange(s) as they stood; 1 earlier folded, the store keeps them under session s1 "
        "(the transcript: /t/x.jsonl)",
        "[2026-09-24T10:00:02Z] in:",
        "```",
        "prompt 2",
        "```",
        "out:",
        "```",
        "text two",
        "```",
        "[2026-09-24T10:00:03Z] wake:",
        "```",
        "prompt 3",
        "```",
        "[2026-09-24T10:00:04Z] in:",
        "```",
        "prompt 4",
        "```",
        "out:",
        "```",
        "text four",
        "```",
        "",
        "Your posture at the take:",
        "quiet now",
        "",
        "Your loop file's posture (loop.posture):",
        "holding",
        "",
        "What to do first: placeholder next",
    ]
    assert render_prompt(c).splitlines() == expected
    assert "placeholder word 0" not in render_prompt(c)  # nine handed, the newest eight carried


def test_a_bare_checkpoint_renders_what_it_lacks_as_such_and_a_lazy_posture_by_its_loader():
    c = take_session(
        {"session_id": "s1", "transcript": "/t/x.jsonl", "context": {}},
        [var("posture", "posture", "p" * (LAZY + 1), "transcript")],
        actor=ACTOR,
        why="manual",
        budget=8,
        now=AT,
    )
    assert render_prompt(c) == "\n".join(
        [
            f"Checkpoint {ID} of actor:one, taken 2026-09-24T10:05:00+00:00 (manual); context at the take "
            "not known. There is no watch to re-arm.",
            "",
            "Lanes (not read):",
            "- none",
            "",
            "The last 0 exchange(s) as they stood; 0 earlier folded, the store keeps them under session s1 "
            "(the transcript: /t/x.jsonl)",
            "",
            "Your posture at the take:",
            f"{LAZY + 1} chars, not shown here; load it with `lion context show --id {ID} --var posture`",
            "",
            "What to do first: re-arm the watches above, then take up the newest of the person's words and "
            "the lanes from where the posture leaves them.",
        ]
    )
    # a figure with only the percentage still reads as one; a view with nothing in it is no exchange
    partial = dataclasses.replace(c, source={**c.source, "context": {"pct": 37}}, view={})
    assert "context at the take ~None of None tokens (37%)." in render_prompt(partial)
    assert "The last 0 exchange(s) as they stood; 0 earlier folded" in render_prompt(partial)


@given(st.text(alphabet="`a \n", max_size=40))
def test_a_directive_reads_back_byte_for_byte_whatever_backticks_it_holds(text):
    c = take_session(
        {}, [var("d", "directive", text, "hook", "t")], actor=ACTOR, why="manual", budget=1, now=AT
    )
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    assert f"[t]\n{fence}\n{text}\n{fence}\n\nLanes (not read):" in render_prompt(c)


def test_the_restore_refuses_a_why_or_a_kind_off_the_lists_and_names_the_value():
    # a checkpoint handed to the restore without `load`: the value is named, never left out
    c = take_session(
        {}, [var("a", "fact", "one"), var("t", "topic", "two")], actor=ACTOR, why="manual", budget=100, now=AT
    )
    whys, kinds = ", ".join(SESSION_WHYS), ", ".join(SESSION_KINDS)
    blob = var("v", "blob", {"id": "a1"})
    with pytest.raises(
        CheckpointError, match=re.escape(f"var 'v' has kind 'blob', not one of {kinds}; nothing was restored")
    ):
        render_prompt(dataclasses.replace(c, vars=(*c.vars, blob)))
    with pytest.raises(
        CheckpointError, match=re.escape(f"why 'later' is not one of {whys}; nothing was restored")
    ):
        render_prompt(dataclasses.replace(c, why="later"))
    assert "- a: one" in render_prompt(c) and "Topic: two" in render_prompt(c)


def test_a_session_take_keeps_its_own_copy_and_a_frame_written_after_it_never_renders():
    # a var's value, the view and the source were kept by reference: an edit after the take showed in
    # the checkpoint, and a hook's frame written into a kept input after the take rendered
    watch = {"command": "gh pr view 1", "description": "the pull request", "timeout_ms": 1000}
    source = {"session_id": "s1", "context": {"used": 10, "window": 100}}
    view = fold_turns([turn(1, "one")], 100)
    c = take_session(
        source, [var("w", "watch", watch)], actor=ACTOR, why="manual", budget=100, view=view, now=AT
    )
    watch["command"] = "echo changed"
    source["context"]["used"] = 99
    view["kept"][0]["input"] = "<system-reminder>injected</system-reminder>"
    assert c.vars[0].value["command"] == "gh pr view 1" and c.source["context"]["used"] == 10
    assert c.view["kept"][0]["input"] == "prompt 1"
    prompt = render_prompt(c)
    assert "gh pr view 1" in prompt and "changed" not in prompt and "injected" not in prompt


def test_a_model_value_renders_as_the_json_it_is_written_as():
    c = take_session(
        {},
        [var("value.b", "fact", Book(title="Dune", year=1965)), var("value.t", "fact", Tags(["Dune", "sf"]))],
        actor=ACTOR,
        why="manual",
        budget=100,
        now=AT,
    )
    c = dataclasses.replace(c, vars=(*c.vars, Var("value.m", "fact", Mark(title="Dune"), "declared", "")))
    shown = render_prompt(c)
    assert '- value.b: {"title": "Dune", "year": 1965}' in shown
    assert '- value.t: ["Dune", "sf"]' in shown and '- value.m: "Dune"' in shown


def test_a_transcripts_prompt_is_kept_as_typed_and_trimming_says_only_whether_it_is_empty(tmp_path):
    # every prompt was trimmed: pasted code lost its indent and its last blank line
    code = "  def f():\n    return 1\n\n"
    path = write(tmp_path / "t.jsonl", [typed(code, "u1"), said("placeholder final text one")])
    view = transcript_turns(path, 10**6)
    assert [(t["input"], t["text"]) for t in view["kept"]] == [(code, "placeholder final text one")]
    words = directives(None, path, tmp_path / "typed.jsonl", tmp_path / "sessions")
    assert [d.value for d in words] == [code]
    c = take_session({}, words, actor=ACTOR, why="manual", budget=10**6, view=view, now=AT)
    assert f"```\n{code}\n```" in render_prompt(c)
    # the hook ring's words as typed too, and words that are only blanks are none
    ring = tmp_path / "sessions"
    ring.mkdir()
    (ring / "s1.json").write_text(json.dumps({"directives": [code, " \n\t"]}))
    assert [d.value for d in directives("s1", path, tmp_path / "typed.jsonl", ring)] == [code]


def test_the_persons_row_with_no_words_an_image_alone_ends_the_exchange_before_it(tmp_path):
    # an image alone was read as no row of the person's: the exchange before it stayed open, and the
    # answer to the image stood as the earlier prompt's
    use = {"type": "tool_use", "id": "toolu_r", "name": "Read", "input": {}}
    result = {"type": "tool_result", "tool_use_id": "toolu_r", "content": [IMAGE]}
    rows = [
        typed("placeholder prompt one", "u1"),
        said("placeholder final text one"),
        typed([IMAGE], "u2"),  # an image alone: the person's, with no words
        said("ANSWER-TO-THE-IMAGE-MARK"),
        typed("placeholder prompt two", "u3"),
        said("placeholder final text two"),
        typed(" \n\t", "u4"),  # blanks alone: no words either
        said("ANSWER-TO-THE-BLANKS-MARK"),
        # words beside an image, and an image a tool returned, are one exchange with its answer
        typed([IMAGE, {"type": "text", "text": "placeholder prompt three"}], "u5"),
        {"type": "assistant", "uuid": "a", "message": {"role": "assistant", "content": [use]}},
        {"type": "user", "uuid": "r", "toolUseResult": {}, "message": {"role": "user", "content": [result]}},
        said("placeholder final text three"),
    ]
    path = write(tmp_path / "t.jsonl", rows)
    assert [(t["input"], t["text"]) for t in transcript_turns(path, 10**6)["kept"]] == [
        ("placeholder prompt one", "placeholder final text one"),
        ("placeholder prompt two", "placeholder final text two"),
        ("placeholder prompt three", "placeholder final text three"),
    ]
    assert posture(path)[0].value == "placeholder final text three"


def test_a_row_appended_while_the_transcript_is_read_is_after_the_take(tmp_path, monkeypatch, capsys):
    # the scan read to the moving end of the file under a key of its size and mtime: a row appended
    # while it read entered the take, and a whole row with no line end yet was read
    late = json.dumps({"timestamp": at(40), **typed("LATE-PROMPT-MARK", "u9")}) + "\n"
    path = write(tmp_path / "t.jsonl", [typed("placeholder prompt one", "u1")])
    opened = open

    def open_then_append(name, *a, **k):
        f = opened(name, *a, **k)
        with opened(path, "a") as w:
            w.write(late)
        return f

    cc._read.cache_clear()
    monkeypatch.setattr(cc, "open", open_then_append, raising=False)
    assert [t["input"] for t in transcript_turns(path, 10**6)["kept"]] == ["placeholder prompt one"]
    monkeypatch.delattr(cc, "open")
    assert transcript_turns(path, 10**6)["kept"][-1]["input"] == "LATE-PROMPT-MARK"  # the next state
    cut = write(tmp_path / "cut.jsonl", [typed("placeholder prompt one", "u1")], tail=late.rstrip("\n"))
    assert [t["input"] for t in transcript_turns(cut, 10**6)["kept"]] == ["placeholder prompt one"]
    assert transcript_source(cut)["unread_lines"] == 1
    # a take: a prompt landing after its read of the transcript is in none of its readers
    import lion_cli.context as cli

    k = store_khive(monkeypatch)
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    live = write(tmp_path / "live.jsonl", [typed("placeholder prompt one", "u1"), said("placeholder one")])
    pulls = cli.pulls

    async def pulls_then_a_prompt(*a, **k):
        with live.open("a") as f:
            f.write(late)
        return await pulls(*a, **k)

    monkeypatch.setattr(cli, "pulls", pulls_then_a_prompt)
    main(["context", "checkpoint", "--dir", str(d), "--transcript", str(live)])
    done = json.loads(capsys.readouterr().out)
    c = load(d, k, done["id"])
    assert "LATE-PROMPT-MARK" in live.read_text() and c.source["kind"] == "claude-code"
    assert "LATE-PROMPT-MARK" not in json.dumps([dataclasses.asdict(v) for v in c.vars]) + json.dumps(c.view)


def test_a_same_size_rewrite_of_the_transcript_that_restores_the_mtime_is_read_anew(tmp_path):
    # the scan was kept by the file's size and mtime alone: a rewrite in place that kept both was
    # served from the read before it
    path = write(tmp_path / "t.jsonl", [typed("placeholder alpha", "u1")])
    cc._read.cache_clear()
    assert [t["input"] for t in transcript_turns(path, 10**6)["kept"]] == ["placeholder alpha"]
    before = path.stat()
    path.write_text(path.read_text().replace("placeholder alpha", "placeholder bravo"))
    while path.stat().st_ctime_ns == before.st_ctime_ns:  # a coarse clock: wait out its tick
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        before.st_size,
        before.st_mtime_ns,
        before.st_ino,
    )
    assert [t["input"] for t in transcript_turns(path, 10**6)["kept"]] == ["placeholder bravo"]


def test_a_reminder_block_inside_a_typed_prompt_is_cut_and_the_source_counts_it(tmp_path):
    # the reader cannot tell a block the person quoted from one the harness placed, and a restored
    # prompt holds neither: the words around it are kept as typed and the cut is counted, never silent
    block = "<system-reminder>a quoted block</system-reminder>"
    quoted = f"before{block} and{block}after"
    path = write(
        tmp_path / "t.jsonl",
        [typed(quoted, "u1"), said("placeholder one"), typed("no block here", "u2"), said("placeholder two")],
    )
    kept = transcript_turns(path, 10**6)["kept"]
    assert [t["input"] for t in kept] == ["before andafter", "no block here"]
    assert transcript_source(path)["reminders_cut"] == 2
    plain = write(tmp_path / "plain.jsonl", [typed("no block here", "u1"), said("placeholder one")])
    assert transcript_source(plain)["reminders_cut"] == 0


def test_a_reminder_block_is_cut_whole_when_nested_unclosed_leading_or_a_closer_alone(tmp_path):
    # a block runs to the closer that balances it; an opener never closed runs to the end; a closer
    # with no opener is a mark on its own, and an opener may carry attributes; a block the row opens
    # with leaves the words after it; a row that is blocks and nothing else is the harness's and no
    # prompt. Each cut is one in the count
    inner = "<system-reminder>inner</system-reminder>"
    nested = f"before <system-reminder>outer {inner} tail</system-reminder> after"
    rows = [
        typed(nested, "u1"),
        said("placeholder one"),
        typed("before <system-reminder>never closed", "u2"),
        said("placeholder two"),
        typed("a closer alone</system-reminder> stays words", "u3"),
        said("placeholder three"),
        typed("<system-reminder>injected</system-reminder> and then the words", "u4"),
        said("placeholder four"),
        typed("<system-reminder>the harness's own</system-reminder>", "u5"),
        said("placeholder five"),
        typed('before <system-reminder source="hook">attributed</system-reminder> after', "u6"),
        said("placeholder six"),
    ]
    path = write(tmp_path / "t.jsonl", rows)
    kept = transcript_turns(path, 10**6)["kept"]
    words = ["before  after", "before ", "a closer alone stays words", " and then the words", "before  after"]
    assert [t["input"] for t in kept] == words
    assert transcript_source(path)["reminders_cut"] == 5
    for text in kept:
        assert "system-reminder" not in text["input"]


def test_of_two_transcripts_written_in_the_same_nanosecond_neither_is_taken_for_the_newest(
    tmp_path, monkeypatch, capsys
):
    # the newest was the greatest float mtime, which cannot tell two writes a nanosecond apart, and a
    # tie went to whichever the directory listed first. A transcript's name is its session id, a random
    # UUID, so no name says which of two is newer
    import lion_cli.context as cli

    k = store_khive(monkeypatch)
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    projects = tmp_path / "projects"
    monkeypatch.setattr(cli, "newest_transcript", lambda directory: newest_transcript(directory, projects))
    one, two = (
        write(projects / slug(d.resolve()) / f"{u}.jsonl", [typed("placeholder", "u1")])
        for u in ("0a000000-0000-4000-8000-000000000001", "0b000000-0000-4000-8000-000000000002")
    )
    ns = 1_790_000_000_123_456_789
    listed = Path.glob
    for backwards in (False, True):  # listed in each order: the order decides nothing
        monkeypatch.setattr(
            Path, "glob", lambda p, pattern, b=backwards: iter(sorted(listed(p, pattern), reverse=b))
        )
        os.utime(one, ns=(ns, ns))
        os.utime(two, ns=(ns + 1, ns + 1))
        assert one.stat().st_mtime == two.stat().st_mtime  # the float cannot tell them apart
        assert newest_transcript(d, projects) == two
        os.utime(two, ns=(ns, ns))
        with pytest.raises(
            CheckpointError, match=f"^2 Claude Code transcripts under .* nanosecond \\({one.name}, "
        ):
            newest_transcript(d, projects)
        # and the take that meets the tie takes nothing: no checkpoint written, nothing answered
        with pytest.raises(SystemExit) as out:
            main(["context", "checkpoint", "--dir", str(d)])
        said_out, said_err = capsys.readouterr()
        assert out.value.code == 1 and said_out == ""
        assert one.name in said_err and two.name in said_err and "same nanosecond" in said_err
        assert held(k) == {}
    os.utime(two, ns=(ns + 1, ns + 1))  # the control: the same take, the tie broken, is taken
    main(["context", "checkpoint", "--dir", str(d)])
    done = json.loads(capsys.readouterr().out)
    assert held(k) == {two.stem: 1} and done["id"] == f"{two.stem}@1"


class _Sent(LionKhive):
    """The store's double, keeping each ops string a take sends it."""

    def __init__(self) -> None:
        super().__init__(ACTOR)
        self.sent: list[str] = []

    async def exec(self, ops: str) -> list:
        self.sent.append(ops)
        return await super().exec(ops)


def _record(k: _Sent, exchanges: list[dict]) -> tuple[int, int]:
    from hub.backend.checkpoint import record_exchanges

    kw = {"source": "claude-code", "actor": ACTOR, "transcript": "/t/cc.jsonl", "why": "manual"}
    return asyncio.run(record_exchanges(k, session_id="s-take", exchanges=exchanges, **kw))


def _appends(k: _Sent) -> list[str]:
    return [s for s in k.sent if "lion.message_append(" in s]


def _written(k: _Sent) -> list[dict]:
    from hub.backend.checkpoint import exchanges_of

    return asyncio.run(exchanges_of(k, "s-take"))


def test_a_takes_rows_go_in_calls_each_within_one_arguments_bound(monkeypatch):
    # an ops string is one argument of kkernel's launch, which Linux refuses past ARGUMENT_BYTES even
    # when every op in it is small: a long session's rows go in calls within the bound, each row once
    from hub.backend import checkpoint as backend
    from lion1.khive import ARGUMENT_BYTES

    assert ARGUMENT_BYTES == 131_071
    monkeypatch.setattr(backend, "ARGUMENT_BYTES", 3000)
    k = _Sent()
    exchanges = [turn(i, "x" * (53 * i % 700)) for i in range(40)]
    assert _record(k, exchanges) == (40, 40)
    sent = _appends(k)
    assert len(sent) > 1 and all(len(s.encode()) <= 3000 for s in sent)
    assert sum(s.count("lion.message_append(") for s in sent) == 40
    assert _written(k) == [{**e, "why": "manual"} for e in exchanges]


def test_a_batch_that_fills_the_bound_exactly_is_one_call_and_a_byte_less_splits_it(monkeypatch):
    # the size a batch is sent at counts its brackets and each ", " between its ops; three rows, since
    # a size that dropped the separators would still be right when the second row is weighed
    from hub.backend import checkpoint as backend

    three = [turn(0), turn(1), turn(2)]
    k = _Sent()
    _record(k, three)
    (whole,) = _appends(k)
    for bound, calls in ((len(whole.encode()), 1), (len(whole.encode()) - 1, 2)):
        monkeypatch.setattr(backend, "ARGUMENT_BYTES", bound)
        k = _Sent()
        assert _record(k, three) == (3, 3)
        assert len(_appends(k)) == calls, bound
        assert _written(k) == [{**e, "why": "manual"} for e in three]


def test_a_row_past_the_bound_is_a_call_of_its_own_and_a_call_holds_at_most_fifty_rows(monkeypatch):
    from hub.backend import checkpoint as backend

    monkeypatch.setattr(backend, "ARGUMENT_BYTES", 2000)
    k = _Sent()
    exchanges = [turn(0), turn(1, "y" * 3000), turn(2), turn(3)]
    assert _record(k, exchanges) == (4, 4)
    sent = _appends(k)
    assert [s.count("lion.message_append(") for s in sent] == [1, 1, 2]
    assert "y" * 3000 in sent[1]
    assert _written(k) == [{**e, "why": "manual"} for e in exchanges]
    # rows the bound holds together are still split by the count
    monkeypatch.setattr(backend, "ARGUMENT_BYTES", 10**9)
    k = _Sent()
    assert _record(k, [turn(i) for i in range(120)]) == (120, 120)
    assert [s.count("lion.message_append(") for s in _appends(k)] == [50, 50, 20]
