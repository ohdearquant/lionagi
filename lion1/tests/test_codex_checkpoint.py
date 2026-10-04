"""A Codex session's checkpoint (ADR-0019/C8, D5): the rollout read once per file state, a prompt the
items the CLI marks `user.text` and nothing it wrote itself, a turn's last text, the thread's own facts,
the last `token_count` as the figure, the sub-agents it spawned that still hold work, the take on the
Codex vocabulary and the restore. Every rollout here is synthetic: placeholder text throughout, one
row of each kind a live one carries."""

import dataclasses
import io
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lion_cli.context import checkpoint, status
from lion_cli.main import main

from hub.backend import codex_checkpoint as cx
from hub.backend.checkpoint import DIRECTIVES, SPAWN_HOURS, fold_turns
from hub.backend.claude_code.checkpoint import SESSION_KINDS, SESSION_WHYS
from hub.backend.codex_checkpoint import (
    CODEX_KINDS,
    CODEX_WHYS,
    context_figure,
    directives,
    is_rollout,
    newest_rollout,
    posture,
    render_prompt,
    rollout_of,
    rollout_source,
    rollout_turns,
    spawns,
    take_codex,
)
from hub.khive import context_dir
from lion1 import Actor
from lion1 import checkpoint as core
from lion1.checkpoint import KINDS, LAZY, WHYS, CheckpointError
from tests.test_checkpoint import ACTOR as ONE
from tests.test_checkpoint import AT, ID, turn, var
from tests.test_context import ACTOR, TASK, TaskKhive, actor_dir, gh_says

THREAD = "01a00000-0000-7000-8000-000000000001"
PARENT = "01a00000-0000-7000-8000-000000000002"
OTHER = "01a00000-0000-7000-8000-000000000003"
AGENTS = [f"01a00000-0000-7000-8000-0000000000a{i}" for i in range(8)]
T0 = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
# one mark per thing the reader never reads into a prompt, a turn's text or a var
MARKS = (
    "BASE-INSTRUCTIONS-MARK",
    "DEVELOPER-MARK",
    "ENV-MARK",
    "AGENTS-MD-MARK",
    "PLUGINS-MARK",
    "WORLD-MARK",
    "USER-ITEM-MARK",
    "REASONING-MARK",
    "TOOLCALL-MARK",
    "TOOLOUTPUT-MARK",
    "SPAWN-MESSAGE-MARK",
    "AGENT-MESSAGE-MARK",
    "COMPACTED-MARK",
    "GOAL-MARK",
    "IMAGE-MARK",
)
PROMPTS = [
    "placeholder prompt one",
    "placeholder prompt two\nplaceholder prompt two, its second item",
    "placeholder prompt 3",
]


def meta(thread: str = THREAD, cwd: str = "/work/actor", **kw) -> dict:
    payload = {
        "session_id": thread,
        "id": thread,
        "timestamp": "2026-10-04T10:00:00.000Z",
        "cwd": cwd,
        "originator": "codex_exec",
        "cli_version": "0.156.1",
        "source": "exec",
        "thread_source": "user",
        "model_provider": "openai",
        "base_instructions": {"text": "BASE-INSTRUCTIONS-MARK"},
        **kw,
    }
    return {"type": "session_meta", "payload": payload}


def message(role: str, items: list, kinds: list, mid: str = "msg_0", **kw) -> dict:
    marks = {"turn_id": "t", "content_item_kinds": kinds}
    payload = {"type": "message", "id": mid, "role": role, "content": items, **kw}
    return {
        "type": "response_item",
        "payload": {**payload, "internal_chat_message_metadata_passthrough": marks},
    }


def text(t: str, kind: str = "input_text") -> dict:
    return {"type": kind, "text": t}


def typed(t: str, mid: str) -> dict:
    return message("user", [text(t)], ["user.text"], mid)


def said(t: str, phase: str = "final_answer") -> dict:
    return message("assistant", [text(t, "output_text")], ["unknown"], "msg_a", phase=phase)


def event(kind: str, **payload) -> dict:
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def activity(kind: str, thread: str, path: str) -> dict:
    item = {
        "type": "SubAgentActivity",
        "id": "i",
        "kind": kind,
        "agent_thread_id": thread,
        "agent_path": path,
    }
    return event("item_completed", thread_id=THREAD, turn_id="t", item=item)


def counted(used, window=258_400) -> dict:
    usage = {"input_tokens": used, "cached_input_tokens": 0, "output_tokens": 10, "total_tokens": 20}
    info = {"total_token_usage": usage, "last_token_usage": usage, "model_context_window": window}
    return event("token_count", info=info, rate_limits={"primary": {"used_percent": 1.0}})


def write(path: Path, rows: list, tail: str = "", start: datetime = T0) -> Path:
    """The rows as a rollout: each stamped a second after the one before, its ordinal its index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, r in enumerate(rows):
        at = (start + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
        lines.append(json.dumps({"timestamp": at, "ordinal": i, **r}) + "\n")
    path.write_text("".join(lines) + tail)
    return path


def at(i: int) -> str:
    return (T0 + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")


def session_rows(cwd: str = "/work/actor") -> list:
    """A live rollout's kinds in the order a session writes them: the CLI's own context first, then two
    turns, a spawn, a compaction and a third prompt still unanswered (twice, as a re-queue)."""
    injected = [
        text("<environment_context>ENV-MARK</environment_context>"),
        text("# AGENTS.md instructions for /work\n\nAGENTS-MD-MARK"),
        text("<recommended_plugins>PLUGINS-MARK</recommended_plugins>"),
    ]
    env = ["environments.environment_context", "agents_md.instructions", "plugins.recommendations"]
    return [
        meta(cwd=cwd),  # 0
        message("developer", [text("DEVELOPER-MARK")], ["permissions.instructions"], "msg_d"),  # 1
        message("user", injected, env, "msg_e"),  # 2
        {"type": "world_state", "payload": {"full": True, "state": {"agents_md": {"text": "WORLD-MARK"}}}},
        {"type": "turn_context", "payload": {"turn_id": "t1", "cwd": cwd, "model": "gpt-test-a"}},  # 4
        event("task_started", turn_id="t1", model_context_window=258_400),  # 5
        typed(PROMPTS[0], "msg_u1"),  # 6
        event("item_completed", item={"type": "UserMessage", "content": [text("USER-ITEM-MARK")]}),  # 7
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [text("REASONING-MARK")]}},
        said("placeholder commentary one", "commentary"),  # 9
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": "TOOLCALL-MARK"},
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "output": [text("TOOLOUTPUT-MARK")]},
        },  # 11
        {"type": "token_usage_record", "payload": {"usage": {"input_tokens": 5}}},  # 12
        counted(1_000),  # 13
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "namespace": "collaboration",
                "arguments": json.dumps({"task_name": "spawn_one", "message": "SPAWN-MESSAGE-MARK"}),
            },
        },  # 14
        activity("started", AGENTS[0], "/root/spawn_one"),  # 15
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "{}"}},  # 16
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "author": "/root/spawn_one",
                "recipient": "/root",
                "content": [text("AGENT-MESSAGE-MARK")],
            },
        },  # 17
        {"type": "inter_agent_communication_metadata", "payload": {"trigger_turn": True}},  # 18
        said("placeholder final text one"),  # 19
        event("task_complete", turn_id="t1", last_agent_message="placeholder final text one"),  # 20
        {"type": "turn_context", "payload": {"turn_id": "t2", "cwd": cwd, "model": "gpt-test-b"}},  # 21
        message(
            "user",
            [
                text("placeholder prompt two"),
                text("placeholder prompt two, its second item"),
                text("IMAGE-MARK", "input_image"),
            ],
            ["user.text", "user.text", "user.image"],
            "msg_u2",
        ),  # 22
        {
            "type": "compacted",
            "payload": {"message": "", "replacement_history": [typed("COMPACTED-MARK", "msg_c")["payload"]]},
        },  # 23
        event("item_completed", item={"type": "ContextCompaction", "id": "c"}),  # 24
        message("user", [text("<codex_internal_context>GOAL-MARK")], ["goal.internal_context"], "msg_g"),
        said("placeholder final text two"),  # 26
        counted(129_200),  # 27
        event("token_count", info=None, rate_limits={"primary": {"used_percent": 2.0}}),  # 28
        typed(PROMPTS[2], "msg_u3"),  # 29
        typed(PROMPTS[2], "msg_u3b"),  # 30: the same words again, no answer between: one exchange
    ]


def test_a_rollout_reads_each_typed_prompt_with_its_turns_last_text_and_nothing_the_cli_wrote(tmp_path):
    path = write(tmp_path / "r.jsonl", session_rows(), tail='{"type": "response_item", "payl')
    raw = path.read_text()
    assert all(m in raw for m in MARKS)  # every kind left out is in the file, so its absence is earned
    view = rollout_turns(path, 10**6)
    assert [t["input"] for t in view["kept"]] == PROMPTS
    # the turn's text is the last assistant text before the next prompt, a commentary never over it
    assert [t["text"] for t in view["kept"]] == [
        "placeholder final text one",
        "placeholder final text two",
        "",
    ]
    assert [(t["turn"], t["at"], t["kind"]) for t in view["kept"]] == [
        ("msg_u1", at(6), "person"),
        ("msg_u2", at(22), "person"),
        ("msg_u3b", at(30), "person"),
    ]
    held = [*directives(path), *spawns(path, T0 + timedelta(hours=1)), *posture(path)]
    c = take_codex(rollout_source(path), held, actor=ACTOR, why="manual", budget=10**6, view=view)
    kept, vars_ = json.dumps(c.view["kept"]), json.dumps([dataclasses.asdict(v) for v in c.vars])
    assert [m for m in MARKS if m in kept or m in vars_] == []
    assert [v.value for v in c.vars if v.kind == "directive"] == PROMPTS
    assert posture(path)[0].value == "placeholder final text two" and posture(path)[0].at == at(26)
    assert rollout_source(path)["unread_lines"] == 1  # the live file's last line, mid-write
    # a small budget folds the oldest first and keeps the latest, each folded one named by its message
    small = rollout_turns(path, 30)
    assert [t["input"] for t in small["kept"]] == [PROMPTS[2]]
    assert small["hidden"] == [f"msg_u1@{at(6)}", f"msg_u2@{at(22)}"]


def test_the_persons_words_are_the_newest_eight_prompts(tmp_path):
    said_ = [f"placeholder prompt {i}" for i in range(1, 11)]
    path = write(tmp_path / "r.jsonl", [meta(), *(typed(t, f"msg_{i}") for i, t in enumerate(said_))])
    got = directives(path)
    assert [v.value for v in got] == said_[2:] and [v.name for v in got] == [
        f"directive.{i}" for i in range(1, 9)
    ]


def test_the_source_is_the_threads_own_meta_and_the_figure_is_the_last_token_count(tmp_path):
    path = write(tmp_path / "r.jsonl", session_rows())
    assert rollout_source(path) == {
        "session_id": THREAD,
        "transcript": str(path),
        "session_cwd": "/work/actor",
        "model": "gpt-test-b",  # the newest turn_context's
        "model_provider": "openai",
        "cli_version": "0.156.1",
        "originator": "codex_exec",
        "thread_source": "user",
        "parent_thread_id": None,
        "agent_path": None,
        "unread_lines": 0,
        "unmarked_messages": 0,
    }
    # the last count with an info: a rate-limit notice after it tells no figure
    assert context_figure(path) == {"used": 129_200, "window": 258_400, "pct": 50}
    for info, figure in (
        ({"last_token_usage": {"input_tokens": True}, "model_context_window": -1}, (None, None, None)),
        ({"last_token_usage": {"input_tokens": 7}, "model_context_window": 0}, (7, 0, None)),
        ({"last_token_usage": "no", "model_context_window": 100}, (None, 100, None)),
    ):
        odd = write(tmp_path / "odd.jsonl", [meta(), event("token_count", info=info)])
        assert tuple(context_figure(odd).values()) == figure
    assert (
        context_figure(None)
        == context_figure(tmp_path / "absent.jsonl")
        == {
            "used": None,
            "window": None,
            "pct": None,
        }
    )
    bare = tmp_path / "bare.jsonl"
    bare.write_text(json.dumps(meta()) + "\n\n" + json.dumps("not an object") + "\n")
    assert context_figure(bare)["used"] is None and rollout_source(bare)["model"] is None


def test_a_user_message_whose_row_does_not_mark_each_item_is_counted_and_never_read(tmp_path):
    rows = [
        meta(),
        said("placeholder text before any prompt"),  # 1: the posture until a later text, no turn's
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [text("UNMARKED")]},
        },
        message("user", [text("SHORT-MARKS"), text("x")], ["user.text"], "msg_m"),  # one mark, two items
        message("user", "not a list", ["user.text"], "msg_n"),
        typed(PROMPTS[0], "msg_u1"),  # 5
        message(
            "assistant", [text("", "output_text"), "not an object", {"type": "refusal"}], ["unknown"] * 3
        ),
        said("placeholder final text one"),  # 7
    ]
    path = write(tmp_path / "r.jsonl", rows)
    assert [(t["input"], t["text"]) for t in rollout_turns(path, 10**6)["kept"]] == [
        (PROMPTS[0], "placeholder final text one")
    ]
    assert rollout_source(path)["unmarked_messages"] == 3
    assert [v.value for v in directives(path)] == [PROMPTS[0]]
    assert posture(path)[0].value == "placeholder final text one" and posture(path)[0].at == at(7)
    early = write(tmp_path / "early.jsonl", rows[:2])
    assert rollout_turns(early, 9)["kept"] == [] and posture(early)[0].at == at(1)


def test_a_rollout_is_read_once_per_file_state(tmp_path):
    path = write(tmp_path / "r.jsonl", session_rows())
    cx._read.cache_clear()
    snap = cx.snapshot(path, 9)  # a take's: its one file state and its view's budget
    for read in (rollout_source, directives, spawns, posture, context_figure):
        read(snap)
    rollout_turns(snap, 9)
    assert cx._read.cache_info().misses == 1
    with path.open("a") as f:  # a prompt lands: the file's size and time change, so it is read again
        f.write(
            json.dumps({"timestamp": at(40), "ordinal": 40, **typed("placeholder prompt four", "u4")}) + "\n"
        )
    assert directives(path)[-1].value == "placeholder prompt four" and cx._read.cache_info().misses == 2
    # the same size rewritten with a later mtime: read again too
    before = path.stat()
    path.write_text(path.read_text().replace("placeholder prompt four", "placeholder prompt FOUR"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
    assert path.stat().st_size == before.st_size
    assert directives(path)[-1].value == "placeholder prompt FOUR" and cx._read.cache_info().misses == 3


def test_a_same_size_rewrite_that_restores_the_mtime_is_read_anew(tmp_path):
    path = write(tmp_path / "r.jsonl", [meta(), typed("placeholder alpha", "m")])
    cx._read.cache_clear()
    assert [v.value for v in directives(path)] == ["placeholder alpha"]
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
    assert [v.value for v in directives(path)] == ["placeholder bravo"]


def test_a_typed_prompt_is_kept_as_typed_and_trimming_says_only_whether_it_is_empty(tmp_path):
    code = "  def f():\n    return 1\n\n"
    rows = [
        meta(),
        typed(code, "msg_u1"),
        said("placeholder final text one"),
        typed(" \n\t", "msg_u2"),  # the person's, with no words: no exchange, and it ends the one before
        said("placeholder final text two"),
    ]
    path = write(tmp_path / "r.jsonl", rows)
    view = rollout_turns(path, 10**6)
    assert [(t["input"], t["text"]) for t in view["kept"]] == [(code, "placeholder final text one")]
    c = take_codex(rollout_source(path), directives(path), actor=ONE, why="manual", budget=10**6, view=view)
    assert [v.value for v in c.vars] == [code] and c.view["kept"][0]["input"] == code
    assert f"```\n{code}\n```" in render_prompt(c)


def test_a_message_the_person_sent_or_nothing_marked_ends_the_exchange_before_it(tmp_path):
    unmarked = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}}
    rows = [
        meta(),
        typed(PROMPTS[0], "msg_u1"),
        message("user", [text("IMAGE-MARK", "input_image")], ["user.image"], "msg_i"),  # an image alone
        said("ANSWER-TO-THE-IMAGE-MARK"),
        typed(PROMPTS[1], "msg_u2"),
        unmarked,  # what wrote it is not known
        said("ANSWER-TO-THE-UNMARKED-MARK"),
        typed("placeholder prompt four", "msg_u4"),
        message("user", [], [], "msg_x"),  # a mark for each item, and no item: nothing is marked
        said("ANSWER-TO-THE-EMPTY-MARK"),
        typed(PROMPTS[2], "msg_u3"),
        # the CLI's own, inside the turn (its environment, a goal, a skill): the turn goes on
        message("user", [text("<environment_context>x")], ["environments.environment_context"], "msg_e"),
        said("placeholder final text three"),
    ]
    path = write(tmp_path / "r.jsonl", rows)
    assert [(t["input"], t["text"]) for t in rollout_turns(path, 10**6)["kept"]] == [
        (PROMPTS[0], ""),
        (PROMPTS[1], ""),
        ("placeholder prompt four", ""),
        (PROMPTS[2], "placeholder final text three"),
    ]
    assert rollout_source(path)["unmarked_messages"] == 2
    assert posture(path)[0].value == "placeholder final text three"


def test_a_row_appended_while_the_take_reads_is_after_the_take(tmp_path, monkeypatch, capsys):
    late = json.dumps({"timestamp": at(40), "ordinal": 40, **typed("LATE-PROMPT-MARK", "msg_late")}) + "\n"
    path = write(tmp_path / "r.jsonl", [meta(), typed(PROMPTS[0], "msg_u1")])
    opened = open

    def open_then_append(name, *a, **k):
        f = opened(name, *a, **k)
        with opened(path, "a") as w:
            w.write(late)
        return f

    cx._read.cache_clear()
    monkeypatch.setattr(cx, "open", open_then_append, raising=False)
    assert [t["input"] for t in rollout_turns(path, 10**6)["kept"]] == [PROMPTS[0]]
    monkeypatch.delattr(cx, "open")
    assert directives(path)[-1].value == "LATE-PROMPT-MARK"  # the next file state holds it
    # a whole row with no line end yet is the snapshot's last line, cut mid-write
    cut = write(tmp_path / "cut.jsonl", [meta(), typed(PROMPTS[0], "msg_u1")], tail=late.rstrip("\n"))
    assert [t["input"] for t in rollout_turns(cut, 10**6)["kept"]] == [PROMPTS[0]]
    assert rollout_source(cut)["unread_lines"] == 1
    # a take: a prompt landing after its first read of the rollout is in none of its reads
    import lion_cli.context as cli

    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    live = write(tmp_path / "live.jsonl", session_rows(str(d)))
    pulls = cli.pulls

    async def pulls_then_a_prompt(*a, **k):
        with live.open("a") as f:
            f.write(late)
        return await pulls(*a, **k)

    monkeypatch.setattr(cli, "pulls", pulls_then_a_prompt)
    main(["context", "checkpoint", "--dir", str(d), "--transcript", str(live)])
    done = json.loads(capsys.readouterr().out)
    c = core.load(context_dir(d), done["id"], kinds=SESSION_KINDS, whys=SESSION_WHYS)
    assert "LATE-PROMPT-MARK" in live.read_text()
    assert "LATE-PROMPT-MARK" not in json.dumps([dataclasses.asdict(v) for v in c.vars]) + json.dumps(c.view)


@pytest.mark.parametrize("field", ["size", "mtime_ns", "ino", "ctime_ns"])
def test_a_snapshot_that_differs_from_its_file_in_one_field_is_never_read(tmp_path, field):
    path = write(tmp_path / "r.jsonl", [meta(), typed(PROMPTS[0], "msg_u1")])
    cx._read.cache_clear()
    snap = cx.snapshot(path, 9)
    other = dataclasses.replace(snap, **{field: getattr(snap, field) + 1})
    with pytest.raises(CheckpointError, match="is not the file its snapshot was taken of$"):
        cx._read(other)
    assert [v.value for v in directives(snap)] == [PROMPTS[0]]  # the file's own state is read


@pytest.mark.parametrize("change", ["replaced", "cut"])
def test_a_rollout_replaced_or_cut_between_its_stat_and_its_read_is_taken_again(
    tmp_path, monkeypatch, change
):
    path = write(
        tmp_path / "r.jsonl", [meta(), typed("placeholder old", "u1"), typed("placeholder older", "u0")]
    )
    new = write(
        tmp_path / "new.jsonl",
        [
            meta(),
            typed("placeholder new, longer than the old one was", "u2"),
            typed("placeholder newest", "u3"),
        ],
    )
    opened, opens = open, []

    def change_then_open(name, *a, **k):
        if not opens and change == "replaced":
            os.replace(new, path)
        elif not opens:
            path.write_text("".join(path.read_text().splitlines(keepends=True)[:2]))
        opens.append(name)
        return opened(name, *a, **k)

    cx._read.cache_clear()
    monkeypatch.setattr(cx, "open", change_then_open, raising=False)
    snap = cx.snapshot(path, 10**6)
    monkeypatch.delattr(cx, "open")
    assert len(opens) == 2  # the first read refused, the second of the file as it then is
    assert (snap.size, snap.ino) == (path.stat().st_size, path.stat().st_ino)
    want = ["placeholder new, longer than the old one was", "placeholder newest"]
    assert [t["input"] for t in rollout_turns(snap, 10**6)["kept"]] == (
        want if change == "replaced" else ["placeholder old"]
    )
    assert rollout_source(snap)["unread_lines"] == 0


def test_a_rollout_rewritten_while_it_is_read_is_taken_again(tmp_path, monkeypatch):
    path = write(tmp_path / "r.jsonl", [meta(), typed("placeholder alpha", "u1")])
    opened, done = open, []

    def rewrite():  # the same size, in place: only its times say so
        if done:
            return
        done.append(1)
        before = path.stat()
        path.write_text(path.read_text().replace("placeholder alpha", "placeholder bravo"))
        while path.stat().st_ctime_ns == before.st_ctime_ns:  # a coarse clock: wait out its tick
            os.utime(path)
        assert path.stat().st_size == before.st_size

    class Rewritten:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self.f.__exit__(*a)

        def fileno(self):
            return self.f.fileno()

        def readline(self, n):
            raw = self.f.readline(n)  # the whole file is in the reader's buffer by now
            rewrite()
            return raw

    cx._read.cache_clear()
    monkeypatch.setattr(cx, "open", lambda *a, **k: Rewritten(opened(*a, **k)), raising=False)
    assert [v.value for v in directives(path)] == ["placeholder bravo"]
    assert cx._read.cache_info().currsize == 1 and done  # the read of the old bytes was never kept


def test_a_rollout_that_changes_under_every_read_is_refused_and_nothing_is_taken(
    tmp_path, monkeypatch, capsys
):
    import lion_cli.context as cli

    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    live = write(tmp_path / "live.jsonl", session_rows(str(d)))
    opened, opens = open, []

    def touch_then_open(name, *a, **k):
        opens.append(name)
        st = live.stat()
        os.utime(live, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # the same bytes, another state
        return opened(name, *a, **k)

    cx._read.cache_clear()
    monkeypatch.setattr(cx, "open", touch_then_open, raising=False)
    assert context_figure(live) == {"used": None, "window": None, "pct": None}
    assert len(opens) == cx.RETAKES + 1
    with pytest.raises(SystemExit) as out:
        main(["context", "checkpoint", "--dir", str(d), "--transcript", str(live)])
    assert out.value.code == 1 and "is not the file its snapshot was taken of" in capsys.readouterr().err
    assert not context_dir(d).exists() or not list(context_dir(d).iterdir())


def test_a_session_meta_after_the_first_row_is_never_the_threads_own(tmp_path, monkeypatch, capsys):
    import lion_cli.context as cli

    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    late = write(tmp_path / "late.jsonl", [typed("INHERITED-MARK", "u1"), meta(THREAD, str(d))])
    assert not is_rollout(late)
    assert rollout_source(late)["session_id"] is None and directives(late) == []
    # a file that stopped being a rollout after the take chose its reader: the snapshot's first row decides
    monkeypatch.setattr(cx, "is_rollout", lambda p: True)
    with pytest.raises(SystemExit) as out:
        main(["context", "checkpoint", "--dir", str(d), "--transcript", str(late)])
    assert out.value.code == 1 and "its first row names no owner" in capsys.readouterr().err
    assert not context_dir(d).exists() or not list(context_dir(d).iterdir())


def test_a_scan_keeps_only_what_the_takes_readers_read(tmp_path):
    old = "OLD-BODY-MARK " * 1_000
    newer = [
        (typed(f"placeholder prompt {i}", f"m{i}"), said(f"placeholder text {i}")) for i in range(DIRECTIVES)
    ]
    rows = [meta(), typed(old, "msg_old"), said("OLD-ANSWER-MARK"), *(r for pair in newer for r in pair)]
    path = write(tmp_path / "r.jsonl", rows)
    kept = json.dumps(dataclasses.asdict(cx._scan(path)))
    assert [m for m in ("BASE-INSTRUCTIONS-MARK", "OLD-BODY-MARK", "OLD-ANSWER-MARK") if m in kept] == []
    view = rollout_turns(path, 400)  # the old exchange folds at this budget: its name stays, its body goes
    assert view["hidden"] == [f"msg_old@{at(1)}"] and len(view["kept"]) == DIRECTIVES
    folded = json.dumps(dataclasses.asdict(cx._scan(path, 400)))
    assert "OLD-BODY-MARK" not in folded and "OLD-ANSWER-MARK" not in folded
    assert [v.value for v in directives(path)] == [f"placeholder prompt {i}" for i in range(DIRECTIVES)]
    assert rollout_turns(path, 10**6)["kept"][0]["input"] == old  # a budget that holds it keeps it


def test_a_forked_sub_agents_rollout_reads_its_own_rows_and_its_words_are_not_the_persons(tmp_path):
    own = meta(
        AGENTS[1],
        session_id=PARENT,
        thread_source="subagent",
        source={"subagent": {"thread_spawn": {"parent_thread_id": PARENT, "depth": 1}}},
        parent_thread_id=PARENT,
        agent_path="/root/spawn_one",
        forked_from_id=PARENT,
        subagent_history_start_ordinal=6,
    )
    rows = [
        own,  # 0: the thread's own
        meta(PARENT),  # 1: the parent's, carried with the fork
        typed("PARENT-PROMPT-MARK", "p1"),  # 2
        said("PARENT-TEXT-MARK"),  # 3
        counted(90_000),  # 4
        activity("started", AGENTS[2], "/root/other"),  # 5
        {"type": "turn_context", "payload": {"model": "gpt-test-sub"}},  # 6: its own history starts
        typed("placeholder task from the parent", "s1"),  # 7
        said("placeholder sub-agent text"),  # 8
    ]
    path = write(tmp_path / "sub.jsonl", rows)
    view = rollout_turns(path, 10**6)
    assert [(t["input"], t["text"]) for t in view["kept"]] == [
        ("placeholder task from the parent", "placeholder sub-agent text")
    ]
    src = rollout_source(path)
    assert (src["session_id"], src["thread_source"], src["parent_thread_id"], src["agent_path"]) == (
        AGENTS[1],
        "subagent",
        PARENT,
        "/root/spawn_one",
    )
    assert src["model"] == "gpt-test-sub" and context_figure(path)["used"] is None
    assert spawns(path, T0) == [] and directives(path) == []  # the parent's words are not the person's
    # the same rows in a thread a person started: its prompts are the person's words
    mine = write(tmp_path / "mine.jsonl", [meta(), *rows[6:]])
    assert [v.value for v in directives(mine)] == ["placeholder task from the parent"]
    assert [(v.name, v.source, v.at) for v in directives(mine)] == [("directive.1", "rollout", at(2))]


def test_a_spawn_is_a_sub_agent_the_rollout_shows_holding_work_within_the_bound(tmp_path):
    rows = [
        meta(),
        activity("started", AGENTS[0], "/root/working"),  # 1: started, nothing since
        activity("started", AGENTS[1], "/root/done"),
        activity("completed", AGENTS[1], "/root/done"),
        activity("started", AGENTS[2], "/root/handed_more"),
        activity("completed", AGENTS[2], "/root/handed_more"),
        activity("interacted", AGENTS[2], "/root/handed_more"),  # 6: more work after its completion
        activity("started", AGENTS[3], "/root/stopped"),
        activity("interacted", AGENTS[3], "/root/stopped"),
        activity("interrupted", AGENTS[3], "/root/stopped"),
        activity("interacted", AGENTS[4], "/root/spawn_one/nested"),  # another thread's spawn
        event("item_completed", item={"type": "SubAgentActivity", "kind": "started", "agent_thread_id": 7}),
        event("item_completed", item="not an object"),
    ]
    path = write(tmp_path / "r.jsonl", rows)
    got = spawns(path, T0 + timedelta(hours=1))
    assert [(v.name, v.value, v.source, v.at) for v in got] == [
        (
            f"spawn.{AGENTS[0]}",
            {"id": AGENTS[0], "path": "/root/working", "state": "running-at-take"},
            "rollout",
            at(1),
        ),
        (
            f"spawn.{AGENTS[2]}",
            {"id": AGENTS[2], "path": "/root/handed_more", "state": "running-at-take"},
            "rollout",
            at(4),
        ),
    ]
    # the bound: a spawn started SPAWN_HOURS before the take is taken as dead with its session
    edge = T0 + timedelta(seconds=1, hours=SPAWN_HOURS)
    assert [v.value["id"] for v in spawns(path, edge - timedelta(seconds=1))] == [AGENTS[0], AGENTS[2]]
    assert [v.value["id"] for v in spawns(path, edge)] == [AGENTS[2]]
    # a start whose time does not parse is not known to be running
    undated = tmp_path / "undated.jsonl"
    undated.write_text(
        json.dumps(meta())
        + "\n"
        + json.dumps({"timestamp": "soon", **activity("started", AGENTS[5], "/root/x")})
    )
    assert spawns(undated) == []


def test_the_codex_vocabulary_has_no_watch_and_its_take_refuses_text_the_cli_writes():
    assert CODEX_KINDS == (*KINDS, "spawn", "lane", "directive", "topic")
    assert CODEX_WHYS == (*WHYS, "rotate")
    assert set(CODEX_KINDS) <= set(SESSION_KINDS) and set(CODEX_WHYS) <= set(SESSION_WHYS)
    c = take_codex({"session_id": THREAD}, [], actor=ONE, why="rotate", budget=9)
    assert c.source == {"kind": "codex", "session_id": THREAD}
    assert c.view == {"budget": 9, "kept": [], "hidden": [], "summaries": []}
    with pytest.raises(CheckpointError, match="kind 'watch' is not one of fact, posture, spawn, lane"):
        take_codex({}, [var("w", "watch", {"command": "x"})], actor=ONE, why="manual", budget=1)
    with pytest.raises(CheckpointError, match="why 'precompact': one of threshold, end, manual, rotate$"):
        take_codex({}, [], actor=ONE, why="precompact", budget=1)
    for opening in (
        "<environment_context>x</environment_context>",
        "  # AGENTS.md instructions for /work",
        "<recommended_plugins>x",
        "<skill>x",
        "<codex_internal_context>x",
        "<external_codex_apps_open_page>x",
    ):
        with pytest.raises(CheckpointError, match="directive 'd' opens with text the Codex CLI writes"):
            take_codex({}, [var("d", "directive", opening, "rollout")], actor=ONE, why="manual", budget=1)
        view = {"budget": 1, "kept": [{"turn": "m1", "at": "t", "input": opening, "text": ""}], "hidden": []}
        with pytest.raises(CheckpointError, match="turn m1: its input opens with text the Codex CLI writes"):
            take_codex({}, [], actor=ONE, why="manual", budget=1, view=view)
    # words about such text are the person's; a long directive stays whole, a long fact goes lazy
    words = "said: <environment_context> is what the CLI writes " * 20
    ok = take_codex(
        {},
        [var("d", "directive", words, "rollout"), var("f", "fact", "f" * (LAZY + 1))],
        actor=ONE,
        why="manual",
        budget=1,
        view={"budget": 1, "kept": [{"turn": "m1", "at": "t", "input": None, "text": "x"}], "hidden": []},
    )
    assert [(v.name, v.size) for v in ok.vars] == [("d", None), ("f", LAZY + 1)]


def test_the_codex_restore_names_the_thread_first_and_asks_for_no_re_arm():
    view = fold_turns([turn(1, "t" * 400), turn(2, "text two")], 100)  # 102 + 2: the first goes
    source = {
        "session_id": THREAD,
        "transcript": "/r/rollout.jsonl",
        "model": "gpt-test-b",
        "cli_version": "0.156.1",
        "lanes": "tasks active",
        "context": {"used": 129_200, "window": 258_400, "pct": 50},
    }
    given = [
        var("directive.1", "directive", "placeholder word", "rollout", "10:01"),
        var("lane.t1", "lane", {"kind": "task", "id": "t1"}, "khive"),
        var("spawn.a", "spawn", {"id": "a", "path": "/root/one", "state": "running-at-take"}, "rollout"),
        var("posture", "posture", "p" * (LAZY + 1), "rollout"),
    ]
    c = take_codex(source, given, actor=ONE, why="rotate", budget=100, view=view, now=AT)
    assert render_prompt(c).splitlines() == [
        f"Checkpoint {ID} of actor:one, taken 2026-09-24T10:05:00+00:00 (rotate); context at the take "
        f"~129200 of 258400 tokens (50%). It was taken from Codex thread {THREAD} (model gpt-test-b, CLI "
        "0.156.1).",
        "",
        "The person's words, verbatim, oldest first (1):",
        "[10:01]",
        "```",
        "placeholder word",
        "```",
        "",
        "Lanes (tasks active):",
        '- {"kind": "task", "id": "t1"}',
        "",
        "Spawned agents the transcript shows running at the take:",
        '- {"id": "a", "path": "/root/one", "state": "running-at-take"}',
        "",
        "The last 1 exchange(s) as they stood; 1 earlier folded, the transcript keeps them: /r/rollout.jsonl",
        "[2026-09-24T10:00:02Z] in:",
        "```",
        "prompt 2",
        "```",
        "out:",
        "```",
        "text two",
        "```",
        "",
        "Your posture at the take:",
        f"{LAZY + 1} chars, not shown here; load it with `lion context show --id {ID} --var posture`",
        "",
        "What to do first: take up the newest of the person's words and the lanes from where the posture "
        "leaves them.",
    ]
    # a value off the Codex lists is refused by name, never left out; a declared `next` leads
    watch = var("w", "watch", {"command": "x"})
    kinds = ", ".join(CODEX_KINDS)
    with pytest.raises(
        CheckpointError, match=re.escape(f"var 'w' has kind 'watch', not one of {kinds}; nothing")
    ):
        render_prompt(dataclasses.replace(c, vars=(*c.vars, watch)))
    with pytest.raises(
        CheckpointError, match="why 'precompact' is not one of threshold, end, manual, rotate;"
    ):
        render_prompt(dataclasses.replace(c, why="precompact"))
    nxt = dataclasses.replace(c, vars=(*c.vars, var("next", "fact", "placeholder next")))
    assert render_prompt(nxt).endswith("\nWhat to do first: placeholder next")


def test_the_newest_rollout_is_the_directorys_own_thread_and_a_thread_id_names_one_file(
    tmp_path, monkeypatch
):
    sessions = tmp_path / "home" / "sessions"
    day = sessions / "2026" / "10" / "04"
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()

    def made(thread: str, cwd: Path, age: int, **kw) -> Path:
        p = write(day / f"rollout-2026-10-04T10-00-00-{thread}.jsonl", [meta(thread, str(cwd), **kw)])
        os.utime(p, (1_000_000 - age, 1_000_000 - age))
        return p

    mine = made(THREAD, here, 30)
    made(PARENT, here / ".", 20, thread_source="subagent")  # newer, a sub-agent's in the same directory
    made(OTHER, there, 10)  # newer still, another directory's
    (day / f"rollout-2026-10-04T10-00-00-{AGENTS[7]}.jsonl").write_text("")  # an empty file: no meta
    claude = day / "rollout-claude.jsonl"
    claude.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    assert newest_rollout(here, sessions) == (mine, 0)
    assert rollout_of(THREAD, sessions) == mine
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))  # the CLI's home, read at the call
    assert newest_rollout(here) == (mine, 0) and rollout_of(THREAD) == mine
    assert is_rollout(mine) and not is_rollout(claude) and not is_rollout(tmp_path / "absent.jsonl")
    with pytest.raises(CheckpointError, match="no Codex rollout under .* whose cwd is .*elsewhere"):
        newest_rollout(tmp_path / "elsewhere", sessions)
    with pytest.raises(CheckpointError, match=r"'\*' is not a Codex thread id"):
        rollout_of("*", sessions)
    with pytest.raises(CheckpointError, match=f"0 Codex rollouts name thread {AGENTS[6]}"):
        rollout_of(AGENTS[6], sessions)
    write(sessions / "2026" / "10" / "05" / f"rollout-2026-10-05T10-00-00-{THREAD}.jsonl", [meta()])
    with pytest.raises(CheckpointError, match=f"2 Codex rollouts name thread {THREAD}"):
        rollout_of(THREAD, sessions)


def test_lion_context_takes_a_codex_session_by_its_flag_or_by_its_rollout_and_restores_it(
    tmp_path, monkeypatch, capsys
):
    import lion_cli.context as cli

    async def no_run(*a, **k):
        raise AssertionError("a checkpoint ran a model")

    monkeypatch.setattr(Actor, "run", no_run)
    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    d = actor_dir(tmp_path)
    day = home / "sessions" / "2026" / "10" / "04"
    hour_ago = datetime.now(UTC) - timedelta(hours=1)  # the spawn in it is inside the bound
    path = write(day / f"rollout-2026-10-04T10-00-00-{THREAD}.jsonl", session_rows(str(d)), start=hour_ago)
    sub = meta(AGENTS[0], str(d), thread_source="subagent")
    later = write(day / f"rollout-2026-10-04T10-00-09-{AGENTS[0]}.jsonl", [sub, typed("SUB-MARK", "s")])
    os.utime(later, (path.stat().st_mtime + 60, path.stat().st_mtime + 60))
    main(
        ["context", "set", "--dir", str(d), "--name", "next", "--kind", "fact", "--value", "placeholder next"]
    )
    capsys.readouterr()

    def ckpt(*argv: str) -> dict:
        main(["context", "checkpoint", "--dir", str(d), *argv])
        return json.loads(capsys.readouterr().out)

    done = ckpt("--codex")  # the directory's newest rollout, the sub-agent's written after it aside
    c = core.load(context_dir(d), kinds=SESSION_KINDS, whys=SESSION_WHYS)
    assert (c.id, c.why, c.source["kind"], c.source["session_id"]) == (done["id"], "manual", "codex", THREAD)
    assert (c.source["transcript"], c.source["cwd"], c.source["session_cwd"]) == (
        str(path),
        str(d.resolve()),
        str(d),
    )
    assert done["context"] == {"used": 129_200, "window": 258_400, "pct": 50}
    assert c.source["lanes"] == "gtd.tasks active; comm.inbox unread; gh pr list open, authored by @me"
    assert done["vars"] == [
        "directive.1",
        "directive.2",
        "directive.3",
        f"spawn.{AGENTS[0]}",
        "lane.1ane0001",
        "posture",
        "next",
    ]
    assert (done["kept"], done["hidden"]) == (3, 0) and "SUB-MARK" not in json.dumps(c.view)
    main(["context", "restore", "--dir", str(d)])
    prompt = capsys.readouterr().out
    assert prompt.startswith(f"Checkpoint {done['id']} of {ACTOR}, taken ")
    assert f"It was taken from Codex thread {THREAD} (model gpt-test-b, CLI 0.156.1).\n" in prompt
    assert "re-arm" not in prompt and "What to do first: placeholder next" in prompt
    main(["context", "restore", "--dir", str(d), "--json"])
    assert json.loads(capsys.readouterr().out)["watches"] == []
    now = status(d, codex=True)
    assert now["latest"] == done["id"] and now["stale"] is False and now["context_now"]["used"] == 129_200
    assert status(d, session=THREAD, codex=True)["context_now"]["pct"] == 50
    assert status(d, session=OTHER, codex=True)["context_now"] == {"used": None, "window": None, "pct": None}
    main(["context", "status", "--dir", str(d), "--codex"])
    assert json.loads(capsys.readouterr().out)["context_now"]["window"] == 258_400
    # the rollout handed as the transcript is taken as a Codex session with no flag; --session names it
    assert load_kind(d, ckpt("--transcript", str(path), "--why", "rotate")) == ("codex", "rotate")
    assert load_kind(d, ckpt("--codex", "--session", THREAD)) == ("codex", "manual")
    claude = tmp_path / "claude.jsonl"
    claude.write_text(json.dumps({"type": "user", "uuid": "u", "message": {"content": "placeholder"}}) + "\n")
    for argv, why in (
        (
            ["--codex", "--transcript", str(claude)],
            "is not a Codex rollout: its first row is no session_meta",
        ),
        (["--codex", "--why", "precompact"], "a Codex session's is one of threshold, end, manual, rotate"),
        (
            ["--codex", "--session", OTHER, "--transcript", str(path)],
            f"is the rollout of thread {THREAD}, not",
        ),
        (["--codex", "--session", "../x"], "'../x' is not a Codex thread id"),
        (["--codex", "--session", OTHER], f"0 Codex rollouts name thread {OTHER}"),
    ):
        with pytest.raises(SystemExit) as out:
            main(["context", "checkpoint", "--dir", str(d), *argv])
        assert out.value.code == 1 and why in capsys.readouterr().err, argv
    with pytest.raises(SystemExit) as out:
        main(["context", "checkpoint", "--dir", str(tmp_path / "actor"), "--codex", "--hook"])
    assert out.value.code == 2 and "not allowed with argument" in capsys.readouterr().err
    elsewhere = actor_dir(tmp_path / "elsewhere")
    with pytest.raises(SystemExit) as out:
        main(["context", "checkpoint", "--dir", str(elsewhere), "--codex"])
    assert out.value.code == 1 and "no Codex rollout under" in capsys.readouterr().err
    # a declared watch has no Monitor to re-arm it in a Codex session: the take refuses it by name
    main(["context", "set", "--dir", str(d), "--name", "w", "--kind", "watch", "--value", "bash /tmp/w.sh"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as out:
        main(["context", "checkpoint", "--dir", str(d), "--codex"])
    assert (
        out.value.code == 1 and "var 'w': kind 'watch' is not one of fact, posture" in capsys.readouterr().err
    )
    assert len(list(context_dir(d).glob("ckpt-*.json"))) == 3  # each refusal wrote nothing


def load_kind(d: Path, done: dict) -> tuple[str, str]:
    c = core.load(context_dir(d), done["id"], kinds=SESSION_KINDS, whys=SESSION_WHYS)
    return c.source["kind"], c.why


def test_a_rollout_handed_to_the_precompact_hook_is_refused_and_writes_nothing(tmp_path, monkeypatch, capsys):
    # the PreCompact hook's input still names a Claude Code transcript; a rollout handed there is a
    # Codex session's, whose reasons hold no precompact: the take refuses and writes nothing
    import lion_cli.context as cli

    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    path = write(tmp_path / "r.jsonl", session_rows(str(d)))
    given = {
        "session_id": THREAD,
        "transcript_path": str(path),
        "cwd": str(d),
        "hook_event_name": "PreCompact",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(given)))
    with pytest.raises(SystemExit) as out:
        main(["context", "checkpoint", "--hook"])
    assert out.value.code == 1 and "why 'precompact': a Codex session's is one of" in capsys.readouterr().err
    assert not context_dir(d).exists() or not any(context_dir(d).iterdir())


def test_a_rollout_whose_first_row_names_no_owner_is_skipped_and_refused_by_name(
    tmp_path, monkeypatch, capsys
):
    import lion_cli.context as cli

    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    d = actor_dir(tmp_path)
    day = home / "sessions" / "2026" / "10" / "04"
    mine = write(
        day / f"rollout-2026-10-04T10-00-00-{THREAD}.jsonl", [meta(THREAD, str(d)), typed("mine", "u")]
    )
    nobody = meta(OTHER, str(d))
    del nobody["payload"]["thread_source"]
    unowned = write(
        day / f"rollout-2026-10-04T10-00-09-{OTHER}.jsonl", [nobody, typed("INHERITED-MARK", "p")]
    )
    os.utime(unowned, (mine.stat().st_mtime + 60, mine.stat().st_mtime + 60))
    main(["context", "checkpoint", "--dir", str(d), "--codex"])
    done = json.loads(capsys.readouterr().out)
    c = core.load(context_dir(d), done["id"], kinds=SESSION_KINDS, whys=SESSION_WHYS)
    assert c.source["session_id"] == THREAD and "INHERITED-MARK" not in json.dumps(c.view)
    assert done["skipped_unowned"] == 1
    assert directives(unowned) == []
    for argv in (["--session", OTHER], ["--transcript", str(unowned)]):
        with pytest.raises(SystemExit) as out:
            main(["context", "checkpoint", "--dir", str(d), "--codex", *argv])
        assert out.value.code == 1 and "its first row names no owner (thread_source None)" in (
            capsys.readouterr().err
        ), argv
    only = tmp_path / "only"
    only.mkdir()
    write(day / f"rollout-2026-10-04T10-00-10-{AGENTS[0]}.jsonl", [{**nobody, "payload": {"cwd": str(only)}}])
    with pytest.raises(CheckpointError, match="names no owner skipped$"):
        newest_rollout(only, home / "sessions")
    with pytest.raises(CheckpointError, match="; 1 whose first row"):
        newest_rollout(only, home / "sessions")


def test_rollouts_written_in_the_same_nanosecond_are_ordered_by_name_the_later_first(tmp_path, monkeypatch):
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    names = {
        THREAD: f"rollout-2026-10-04T09-00-00-{THREAD}.jsonl",
        PARENT: f"rollout-2026-10-04T10-00-00-{PARENT}.jsonl",  # started an hour later
    }
    for order in (
        (PARENT, THREAD),
        (THREAD, PARENT),
    ):  # made in each order: the order on disk decides nothing
        home = tmp_path / f"home-{order[0]}"
        monkeypatch.setenv("CODEX_HOME", str(home))
        for thread in order:
            p = write(home / "sessions" / "2026" / "10" / "04" / names[thread], [meta(thread, str(d))])
            os.utime(p, ns=(10**18, 10**18))
        done = checkpoint(d, "manual", codex=True, khive=TaskKhive([TASK]))
        taken = core.load(context_dir(d), done["id"], kinds=SESSION_KINDS, whys=SESSION_WHYS)
        assert taken.source["session_id"] == PARENT, order
