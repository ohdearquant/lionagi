"""The checkpoint's sources in `hub/context.py` (ADR-0019/D2): the actor's reader works on a synthetic
transcript holding one row of every kind a live one carries, placeholder text throughout; each source
harvests its kind, and an absent source is a value."""

import asyncio
import io
import json
import shlex
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lion_cli.areas import context_api
from lion_cli.context import checkpoint, restore, show, status
from lion_cli.main import main

from hub.agent import Agent
from hub.context import (
    INBOX_MONITOR,
    context_figure,
    cursor,
    directives,
    lanes,
    posture,
    pulls,
    spawns,
    topic,
    transcript_turns,
    watches,
)
from hub.khive import KhiveError
from lionagi import Actor, Kind, Record, Reply, fold
from lionagi.checkpoint import (
    DIRECTIVES,
    LAZY,
    CheckpointError,
    NotIgnored,
    Var,
    load,
    render_prompt,
    save,
    take,
    to_history,
)
from tests.test_agent import REPLY, FakeKhive, mail, profile_for
from tests.test_areas import call, registry, serve_on

FIXTURE = Path(__file__).parent / "fixtures" / "session_transcript.jsonl"
AT_TAKE = datetime(2026, 9, 24, 10, 5, tzinfo=UTC)  # the fixture's timed Monitor has not timed out yet
ACTOR = "lambda:x"
# one mark per kind the reader drops: tool calls and results, thinking, every hook and harness frame
MARKS = (
    "REMINDER-INSIDE-MARK",
    "REMINDER-LEAD-MARK",
    "THINKING-MARK",
    "TOOLUSE-MARK",
    "TOOLRESULT-MARK",
    "TOOLRESULT-ORPHAN-MARK",
    "AGENT-PROMPT-MARK",
    "ATTACHED-RESULT-MARK",
    "COMPACT-SUMMARY-MARK",
    "META-MARK",
    "CAVEAT-MARK",
    "COMMAND-ARGS-MARK",
    "LOCAL-STDOUT-MARK",
    "SKILL-MARK",
    "SYSTEM-SOURCED-MARK",
    "ATTACHMENT-MARK",
    "Request interrupted",
    "apps:terse",
    "/compact",
)
PROMPTS = [
    "placeholder prompt one\n\nplaceholder prompt one, second line",
    "placeholder prompt two",
    "placeholder prompt 3",
    "placeholder prompt 4",
    "placeholder prompt 5",
    "placeholder prompt 6",
    "placeholder prompt seven, with an image",
    "placeholder prompt 8",
    "placeholder prompt nine, typed by the actor tool",
    "placeholder prompt 10",
    "placeholder prompt 11",
]


def rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def monitor_commands() -> dict[str, str]:
    """The Monitor commands as the fixture holds them, by tool_use id: the bytes a restore must keep."""
    out = {}
    for r in rows():
        if r.get("type") == "assistant":
            for b in r["message"]["content"]:
                if b.get("type") == "tool_use" and b["name"] == "Monitor":
                    out[b["id"]] = b["input"]["command"]
    return out


def actor_dir(root: Path, actor: str = ACTOR) -> Path:
    d = root / "actor"
    (d / ".khive").mkdir(parents=True)
    (d / ".khive" / "config.toml").write_text(f'[actor]\nid = "{actor}"\n')
    return d


def harvest(root: Path, now: datetime = AT_TAKE) -> list[Var]:
    typed = root / "typed.jsonl"
    typed.write_text(json.dumps({"ts": 1, "window": "s:w", "text": PROMPTS[8]}) + "\n")
    return [
        *directives("no-such-session", FIXTURE, typed, sessions=root / "sessions"),
        *watches(FIXTURE, ACTOR, now),
        *spawns(FIXTURE, now),
        *posture(FIXTURE),
    ]


class TaskKhive:
    """gtd.tasks as khive answers it: a bare list of task rows; the unread box as `Khive.inbox` hands it
    (a list of messages, or an error to raise)."""

    def __init__(self, tasks=None, fail: str | None = None, mail=()):
        self.actor, self.cwd, self.ops = ACTOR, None, []
        self.tasks, self.fail, self.mail = tasks or [], fail, mail

    async def exec(self, ops: str) -> list:
        self.ops.append(ops)
        if self.fail:
            raise KhiveError(self.fail)
        return [self.tasks]

    async def inbox(self, *, limit: int = 50, **kw) -> list:
        self.ops.append(f"inbox limit={limit} {kw}")
        if self.fail:
            raise KhiveError(self.fail)
        if isinstance(self.mail, Exception):
            raise self.mail
        return list(self.mail)[:limit] if isinstance(self.mail, list | tuple) else self.mail


TASK = {"id": "1ane0001", "title": "placeholder lane", "priority": "p1", "status": "active", "properties": {}}
PR = {
    "number": 7,
    "title": "placeholder pull request",
    "url": "https://example.invalid/pull/7",
    "reviewDecision": "REVIEW_REQUIRED",
}


def unread(mid: str, sender: str, subject: str, thread: str | None = None) -> dict:
    props = {"from_actor": sender, **({"thread_id": thread} if thread else {})}
    return {"id": mid, "subject": subject, "content": "placeholder", "properties": props}


def gh_says(monkeypatch, rc: int = 0, out: str = "[]", err: str = "") -> list:
    """A gh that answers every call with (rc, out, err); each call's argv and cwd are kept."""
    calls: list = []

    async def run(argv, **kw):
        calls.append((argv, kw.get("cwd")))
        return rc, out, err

    monkeypatch.setattr("hub.context.sh", run)
    return calls


def isolated_git(monkeypatch, root: Path) -> None:
    """No global or system git config (a global excludes file would answer the ignore question)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(root / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg"))


def test_the_reader_keeps_each_prompt_with_its_final_text_and_no_dropped_kind_survives(tmp_path):
    raw = FIXTURE.read_text()
    assert all(m in raw for m in MARKS)  # every dropped kind is in the fixture, so its absence is earned
    view = transcript_turns(FIXTURE, 10**6)
    person = [t for t in view["kept"] if t["kind"] == "person"]
    assert [t["input"] for t in person] == PROMPTS  # the reminder block inside turn one cut out
    # a wake the harness opened is a turn too, labelled by its notice, whether it came as a user row
    # or as an attachment; its result text stays out
    events = [t for t in view["kept"] if t["kind"] == "event"]
    assert [t["input"] for t in events] == [
        "[event] NOTIF-DONE-MARK",
        "[event] NOTIF-EVENT-MARK line",
        '[event] Agent "spawn four" finished',
    ]
    assert events[-1]["text"] == "placeholder text after the attached wake"
    assert [t["kind"] for t in view["kept"]][:3] == ["person", "person", "event"]
    texts = [t["text"] for t in person]
    assert texts[:3] == [
        "placeholder final text one",
        "placeholder final text two",
        "placeholder final text 3",
    ]
    first = next(r for r in rows() if r.get("promptSource") == "typed")
    assert (person[0]["turn"], person[0]["at"]) == (first["uuid"], first["timestamp"])
    vars_ = harvest(tmp_path)
    ckpt = take({"transcript": str(FIXTURE)}, vars_, actor=ACTOR, why="manual", budget=10**6, view=view)
    kept, held = json.dumps(ckpt.view["kept"]), json.dumps([asdict(v) for v in ckpt.vars])
    assert [m for m in MARKS if m in kept or m in held] == []
    # the posture is the last text block without its LNDL, its lines kept
    assert posture(FIXTURE)[0].value == "placeholder posture line one\n\nplaceholder posture line two"


def test_each_source_harvests_its_kind_and_an_absent_source_is_a_value(tmp_path):
    registry = tmp_path / "sessions_registry.json"
    registry.write_text(json.dumps({"active_topic": "placeholder-topic", "sessions": {}}))
    lane_vars, said = asyncio.run(lanes(TaskKhive([TASK])))
    got = {v.kind: v for v in [*harvest(tmp_path), *topic(registry), *lane_vars]}
    assert set(got) == {"directive", "watch", "spawn", "posture", "topic", "lane"}
    assert got["topic"].value == "placeholder-topic" and got["topic"].source == "registry"
    assert got["lane"].value == {
        "kind": "task",
        "id": "1ane0001",
        "title": "placeholder lane",
        "awaiting": None,
        "priority": "p1",
    }
    assert said == "gtd.tasks active; comm.inbox unread"
    # khive down: no lanes, and the reason is the value; no registry, no topic; no figure filed, nulls
    assert asyncio.run(lanes(TaskKhive(fail="kkernel exited 1: down"))) == (
        [],
        "gtd.tasks unavailable: KhiveError: kkernel exited 1: down; "
        "comm.inbox unavailable: KhiveError: kkernel exited 1: down",
    )
    assert topic(tmp_path / "absent.json") == []
    assert context_figure("no-such-session", tmp_path) == {"used": None, "window": None, "pct": None}
    (tmp_path / "s1.json").write_text(
        json.dumps(
            {
                "context_window": {
                    "used_percentage": 37,
                    "total_input_tokens": 370,
                    "context_window_size": 1000,
                }
            }
        )
    )
    assert context_figure("s1", tmp_path) == {"used": 370, "window": 1000, "pct": 37}


def test_the_lanes_are_the_active_tasks_the_threads_awaiting_a_reply_and_the_open_pull_requests(
    tmp_path, monkeypatch
):
    # the lanes were the active tasks alone, and `source.lanes` said open PRs and threads were not read
    box = [  # newest first, as the box hands it
        unread("m3", "lambda:owner", "re: placeholder one", "th1"),
        unread("m2", "lambda:peer", "placeholder two", "th2"),
        unread("m1", "lambda:owner", "placeholder one", "th1"),
        unread("m0", "lambda:peer", "placeholder three"),  # no thread: the message is its own
        "not a message",
    ]
    calls = gh_says(monkeypatch, out=json.dumps([PR, "not a pull request"]))
    d = actor_dir(tmp_path)
    khive = TaskKhive([TASK], mail=box)
    done = checkpoint(d, "manual", transcript=FIXTURE, khive=khive, now=AT_TAKE)
    ckpt = load(d)
    said = (
        "gtd.tasks active; comm.inbox unread (1 invalid; the first: str, not an object); "
        "gh pr list open, authored by @me (1 invalid; the first: str, not an object)"
    )
    assert ckpt.source["lanes"] == said
    got = {v.name: v.value for v in ckpt.vars if v.kind == "lane"}
    assert got == {
        "lane.1ane0001": {
            "kind": "task",
            "id": "1ane0001",
            "title": "placeholder lane",
            "awaiting": None,
            "priority": "p1",
        },
        "lane.thread.th1": {
            "kind": "thread",
            "id": "th1",
            "title": "re: placeholder one",
            "awaiting": "a reply to lambda:owner",
        },
        "lane.thread.th2": {
            "kind": "thread",
            "id": "th2",
            "title": "placeholder two",
            "awaiting": "a reply to lambda:peer",
        },
        "lane.thread.m0": {
            "kind": "thread",
            "id": "m0",
            "title": "placeholder three",
            "awaiting": "a reply to lambda:peer",
        },
        "lane.pr.7": {
            "kind": "pr",
            "id": 7,
            "title": "placeholder pull request",
            "awaiting": "REVIEW_REQUIRED",
            "url": "https://example.invalid/pull/7",
        },
    }
    assert set(got) <= set(done["vars"])
    assert {v.source for v in ckpt.vars if v.kind == "lane"} == {"khive", "gh"}
    # the box is read unread and without a wait; nothing marks it read
    assert khive.ops[1:] == ["inbox limit=50 {}"]
    # the directory's own repository, the gh account's own pull requests, open
    ((argv, cwd),) = calls
    assert cwd == str(d.resolve()) and argv[1:3] == ["pr", "list"]
    assert argv[argv.index("--author") + 1] == "@me" and argv[argv.index("--state") + 1] == "open"
    prompt = render_prompt(ckpt)
    assert f"Lanes ({said}):" in prompt and "placeholder pull request" in prompt and '"id": "th2"' in prompt


def test_a_lane_source_that_cannot_answer_says_why_and_a_full_page_says_it_is_one(tmp_path, monkeypatch):
    many = [unread(f"m{i}", "lambda:peer", f"placeholder {i}", f"th{i}") for i in range(60)]
    got, said = asyncio.run(lanes(TaskKhive(mail=many)))
    assert (
        len(got) == 50
        and said == "gtd.tasks active; comm.inbox unread (a full page of 50: more may be unread)"
    )
    boxed = TaskKhive({"tasks": [TASK]}, mail=KhiveError("comm.inbox: down"))
    got, said = asyncio.run(lanes(boxed))
    assert [v.name for v in got] == ["lane.1ane0001"]
    assert said == "gtd.tasks active; comm.inbox unavailable: KhiveError: comm.inbox: down"
    assert asyncio.run(lanes(TaskKhive("not tasks")))[1].startswith(
        "gtd.tasks unavailable: answered str, not a list of tasks; comm.inbox unread"
    )

    def gh(rc=0, out="[]", err="", raises=None):
        async def run(argv, **kw):
            if raises is not None:
                raise raises
            return rc, out, err

        return asyncio.run(pulls(tmp_path, run))

    assert gh(raises=FileNotFoundError("gh")) == ([], "gh pr list unavailable: FileNotFoundError: gh")
    assert gh(rc=1, err="no git remotes found\n") == ([], "gh pr list unavailable: no git remotes found")
    assert gh(rc=4) == ([], "gh pr list unavailable: rc 4")
    assert gh(out="not json") == ([], "gh pr list unavailable: no list of pull requests in 'not json'")
    assert (
        gh(out='{"number": 7}')[1] == "gh pr list unavailable: no list of pull requests in '{\"number\": 7}'"
    )
    page = [{**PR, "number": n, "reviewDecision": ""} for n in range(1, 101)]
    got, said = gh(out=json.dumps(page))
    assert len(got) == 100 and got[0].value["awaiting"] is None
    assert said == "gh pr list open, authored by @me (a full page of 100: more may be open)"


def test_a_row_of_the_wrong_shape_is_counted_invalid_and_never_aborts_the_take(tmp_path, monkeypatch):
    # a `properties` that was no object raised at `.get` and aborted the take; a pull request numbered 0
    # or with no number became `lane.pr.0`, `lane.pr.None`
    tasks = [{**TASK, "properties": 42}, {**TASK, "id": None}, TASK]
    box = [
        {**unread("m3", "lambda:peer", "placeholder three"), "properties": 42},
        unread("m2", "lambda:peer", "placeholder two", "th2"),
        {**unread("m1", "lambda:peer", "placeholder one"), "id": 5},
        {**unread("m0", "lambda:peer", "placeholder zero"), "properties": {"thread_id": 42}},
    ]
    numbers = [0, None, True, -3, "8"]  # None: the row carries no number
    prs = [{**PR, "number": n} if n is not None else {"title": "placeholder"} for n in numbers]
    gh_says(monkeypatch, out=json.dumps([*prs, PR]))
    d = actor_dir(tmp_path)
    checkpoint(d, "manual", transcript=FIXTURE, khive=TaskKhive(tasks, mail=box), now=AT_TAKE)
    ckpt = load(d)
    assert ckpt.source["lanes"] == (
        "gtd.tasks active (2 invalid; the first: properties int, not an object); "
        "comm.inbox unread (3 invalid; the first: properties int, not an object); "
        "gh pr list open, authored by @me (5 invalid; the first: number 0, not a positive int)"
    )
    lane_names = sorted(v.name for v in ckpt.vars if v.kind == "lane")
    assert lane_names == ["lane.1ane0001", "lane.pr.7", "lane.thread.th2"]

    def pr(row):
        async def run(argv, **kw):
            return 0, json.dumps([row]), ""

        return asyncio.run(pulls(tmp_path, run))[1]

    for n, row in zip(numbers, prs, strict=True):
        why = f"number {n!r}, not a positive int"
        assert pr(row) == f"gh pr list open, authored by @me (1 invalid; the first: {why})"
    for row, why in [(tasks[1], "id None"), ({**TASK, "id": ""}, "id ''"), ({**TASK, "id": 9}, "id 9")]:
        assert asyncio.run(lanes(TaskKhive([row])))[1].startswith(
            f"gtd.tasks active (1 invalid; the first: {why}, not a non-empty string); "
        )
    # a null `properties` reads as an empty one
    got, said = asyncio.run(lanes(TaskKhive([{**TASK, "properties": None}])))
    assert [v.name for v in got] == ["lane.1ane0001"] and said == "gtd.tasks active; comm.inbox unread"
    for row, why in [(box[2], "id 5"), (box[3], "id 42")]:
        assert asyncio.run(lanes(TaskKhive(mail=[row])))[1].endswith(
            f"comm.inbox unread (1 invalid; the first: {why}, not a non-empty string)"
        )
    # a box that is not a list of messages is no read, never an empty and complete one
    for answer in ("placeholder box", {"m2": box[1]}):
        got, said = asyncio.run(lanes(TaskKhive([TASK], mail=answer)))
        assert [v.name for v in got] == ["lane.1ane0001"]
        why = f"answered {type(answer).__name__}, not a list of messages"
        assert said == f"gtd.tasks active; comm.inbox unavailable: {why}"


def test_the_inbox_line_quotes_a_monitor_path_with_a_space(monkeypatch):
    script = "/opt/my tools/inbox-monitor.sh"
    monkeypatch.setattr("hub.context.INBOX_MONITOR", script)
    monkeypatch.setattr("hub.context.INBOX_PRIORITY", ())
    (inbox,) = watches(FIXTURE, ACTOR, AT_TAKE + timedelta(days=1))
    assert shlex.split(inbox.value["command"]) == ["bash", script, ACTOR]


def test_a_watch_that_only_names_the_inbox_script_does_not_stand_for_it(tmp_path):
    raw, name = FIXTURE.read_text(), Path(INBOX_MONITOR).name
    arm = "bash /tmp/watch-one.sh --actor lambda:x"

    def names(command: str) -> list[str]:
        path = tmp_path / "t.jsonl"
        path.write_text(raw.replace(arm, json.dumps(command)[1:-1]))  # the command sits in a JSON string
        return [w.name for w in watches(path, ACTOR, AT_TAKE)]

    runs_it = (
        f"bash /opt/tools/{name} {ACTOR}",
        f"/opt/tools/{name} {ACTOR}",
        f"zsh {name}",
        f"env bash /opt/tools/{name} {ACTOR}",
        f"env TZ=UTC /opt/tools/{name}",
        f"bash -x {name} {ACTOR}",
        f"bash --noprofile /opt/tools/{name} {ACTOR}",
        f"bash -o pipefail {name}",
        f"bash -c '/opt/tools/{name} {ACTOR}'",
        f"bash -xc 'env TZ=UTC {name}'",
        f"bash -- /opt/tools/{name}",
        f"bash --debugger /opt/tools/{name}",
        f"zsh --login {name}",
        f"bash -c -x /opt/tools/{name}",
    )
    for runs in runs_it:
        assert names(runs) == ["watch.tmon1", "watch.tmon2"], runs
    mentions_it = (
        f"tail -f /tmp/{name}.log",
        f"grep x /tmp/{name}",
        f"true # {name}",
        f"bash '{name}",
        f"bash --version {name}",
        f"bash -c 'echo {name}'",
        f"bash --rcfile {name}",
        f'bash -c "\'{name}"',
        f"bash -o {name}",
        f"bash -n {name}",
        f"sh -s {name}",
        f"zsh --norc {name}",
        f"bash +c {name}",
        f"bash -o noexec {name}",
        f"bash -o -c /opt/tools/{name}",
        f"bash -O extglob {name}",
    )
    for mentions in mentions_it:
        assert names(mentions) == ["watch.tmon1", "watch.tmon2", "watch.inbox"], mentions


def test_a_typed_prompt_holding_a_unicode_line_separator_is_still_the_actor_tools(tmp_path):
    said = "placeholder prompt 10"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(FIXTURE.read_text().replace("placeholder prompt 10", "placeholder\\u2028prompt 10"))
    typed = tmp_path / "typed.jsonl"
    typed.write_text(json.dumps({"ts": 1, "text": said}, ensure_ascii=False) + "\n")
    assert " " in typed.read_text()  # the line holds the raw separator, as a writer may leave it
    kept = [d.value for d in directives("no-such-session", transcript, typed, sessions=tmp_path / "s")]
    assert PROMPTS[8] in kept and said not in kept and PROMPTS[10] in kept


def test_watches_are_the_monitors_still_running_verbatim_and_the_inbox_line_is_added_once(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "hub.context.INBOX_PRIORITY", ("lambda:owner", ACTOR)
    )  # the actor itself is not repeated
    ws = watches(FIXTURE, ACTOR, AT_TAKE)
    cmds = monitor_commands()
    # tmon3 has a later TaskStop; the other two stand, each command byte for byte
    assert [w.name for w in ws] == ["watch.tmon1", "watch.tmon2", "watch.inbox"]
    assert [w.value["command"] for w in ws[:2]] == [cmds["toolu_m1"], cmds["toolu_m2"]]
    assert ws[2].value["command"] == f"bash {INBOX_MONITOR} {ACTOR} lambda:owner"
    assert sum(INBOX_MONITOR in w.value["command"] for w in ws) == 1
    # every Monitor ends at its timeout, persistent or not (tmon1 says persistent, an hour long);
    # the tool kills them all at the deadline, so a day later only the inbox line stands
    later = [w.name for w in watches(FIXTURE, ACTOR, AT_TAKE + timedelta(days=1))]
    assert later == ["watch.inbox"]
    # tmon1 asked for an hour; the tool caps every deadline at 30 minutes, so both are gone at 31
    soon = [w.name for w in watches(FIXTURE, ACTOR, AT_TAKE + timedelta(minutes=24))]  # 10:29, armed 10:00
    assert soon == ["watch.tmon1", "watch.tmon2", "watch.inbox"]
    assert [w.name for w in watches(FIXTURE, ACTOR, AT_TAKE + timedelta(minutes=26))] == ["watch.inbox"]
    # a transcript that already runs the inbox monitor: no line added
    raw = FIXTURE.read_text()
    armed = tmp_path / "armed.jsonl"
    armed.write_text(raw.replace("bash /tmp/watch-one.sh --actor lambda:x", f"bash {INBOX_MONITOR} {ACTOR}"))
    assert [w.name for w in watches(armed, ACTOR, AT_TAKE)] == ["watch.tmon1", "watch.tmon2"]
    # the same command armed twice and both running: one watch, the newer
    assert raw.count('"task_id": "tmon3"') == 2  # the TaskStop's input and its toolUseResult
    twice = tmp_path / "twice.jsonl"
    twice.write_text(
        raw.replace('"task_id": "tmon3"', '"task_id": "tmon9"').replace(
            "bash /tmp/watch-three.sh", "bash /tmp/watch-one.sh --actor lambda:x"
        )
    )
    assert [w.name for w in watches(twice, ACTOR, AT_TAKE)] == ["watch.tmon2", "watch.tmon3", "watch.inbox"]
    # the same command re-armed with a SHORT deadline: the re-arm is the watch, and once it is dead the
    # older, longer arm does not come back (the newest arm decides, live or not)
    # (tmon3: ten minutes, armed 10:00:08; tmon1: the same command, capped at 30 minutes)
    names = [w.name for w in watches(twice, ACTOR, AT_TAKE + timedelta(minutes=24))]  # 10:29
    assert names == ["watch.tmon2", "watch.inbox"]  # tmon3 is dead and tmon1 does not come back
    # a zone-less timestamp is the transcript's own zone, UTC, never the host's
    naive = tmp_path / "naive.jsonl"
    naive.write_text(raw.replace(".000Z", ".000"))
    assert ".000Z" not in naive.read_text() and "T10:00:04.000" in naive.read_text()
    assert [w.name for w in watches(naive, ACTOR, AT_TAKE + timedelta(minutes=26))] == ["watch.inbox"]
    assert [w.name for w in watches(naive, ACTOR, AT_TAKE + timedelta(minutes=24))][:2] == [
        "watch.tmon1",
        "watch.tmon2",
    ]
    # the tool's own expiry notice ends a watch before its deadline would
    ended = tmp_path / "expired.jsonl"
    notice = {
        "type": "user",
        "promptSource": "system",
        "timestamp": "2026-09-24T10:03:00.000Z",
        "message": {
            "role": "user",
            "content": "<task-notification>\n<task-id>tmon2</task-id>\n<summary>Monitor event</summary>\n"
            "<event>[Monitor expired after 30m with 0 events delivered. Re-arm it if you still need the "
            "watch.]</event>\n</task-notification>",
        },
    }
    ended.write_text(raw + json.dumps(notice) + "\n")
    assert [w.name for w in watches(ended, ACTOR, AT_TAKE)] == ["watch.tmon1", "watch.inbox"]


def test_concurrent_saves_each_land_and_latest_names_one_of_them(tmp_path):
    import threading

    d = actor_dir(tmp_path)
    source = {"transcript": str(FIXTURE), "context": {"used": 1, "window": 10, "pct": 10}}
    view = transcript_turns(FIXTURE, 400)
    ckpts = [
        take(source, harvest(tmp_path), actor=ACTOR, why="manual", budget=400, view=view) for _ in range(12)
    ]
    errors: list[BaseException] = []

    def go(c):
        try:
            save(c, d)
        except BaseException as e:  # noqa: BLE001 - the test reports whatever a thread raised
            errors.append(e)

    threads = [threading.Thread(target=go, args=(c,)) for c in ckpts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    ctx = d / ".khive" / "context"
    files = sorted(ctx.glob("ckpt-*.json"))
    assert len(files) == 12 and not list(ctx.glob(".latest.json.*"))
    latest = json.loads((ctx / "latest.json").read_text())["id"]
    assert (ctx / f"{latest}.json").exists()


def test_the_loop_cursor_header_and_posture_are_read_and_an_event_turn_renders_as_a_wake(tmp_path):
    d = actor_dir(tmp_path)
    loop = d / ".khive" / "loop"
    loop.mkdir(parents=True)
    (loop / "archive").mkdir()
    (loop / "archive" / "x_loop.md").write_text("# old\nARCHIVED-MARK\n")
    (loop / "x_loop.md").write_text(
        "# lambda:x loop cursor\n\n> HEADER-MARK newest record first\n\n## Posture\nPOSTURE-MARK holding\n\n"
        "## LOG\nLOG-MARK 10:00 did things\n"
    )
    got = {v.name: v for v in cursor(d)}
    assert got["cursor.header"].value == "# lambda:x loop cursor\n\n> HEADER-MARK newest record first"
    assert got["cursor.posture"].value == "POSTURE-MARK holding" and got["cursor.posture"].kind == "posture"
    # a header past LAZY is named in the prompt and read whole by `show`, never inlined
    long_header = "# lambda:x loop cursor\n\n> " + "HEADER-LONG-MARK " * 40
    (loop / "x_loop.md").write_text(long_header + "\n\n## Posture\nPOSTURE-MARK holding\n")
    assert len(long_header) > LAZY
    ckpt_long = take({}, cursor(d), actor=ACTOR, why="manual", budget=400)
    long_prompt = render_prompt(ckpt_long)
    assert "HEADER-LONG-MARK HEADER-LONG-MARK" not in long_prompt and "cursor.header" in long_prompt
    save(ckpt_long, d)
    assert show(d, "cursor.header") == long_header.strip()
    (loop / "x_loop.md").write_text(
        "# lambda:x loop cursor\n\n> HEADER-MARK newest record first\n\n## Posture\nPOSTURE-MARK holding\n\n"
        "## LOG\nLOG-MARK 10:00 did things\n"
    )
    assert "LOG-MARK" not in json.dumps({k: v.value for k, v in got.items()})
    assert cursor(tmp_path / "nowhere") == []
    view = transcript_turns(FIXTURE, 10**6)
    ckpt = take(
        {"transcript": str(FIXTURE)},
        [*harvest(tmp_path), *cursor(d)],
        actor=ACTOR,
        why="manual",
        budget=10**6,
        view=view,
    )
    prompt = render_prompt(ckpt)
    assert '] wake:\n```\n[event] Agent "spawn four" finished\n```' in prompt
    assert "] in:\n```\nplaceholder prompt two" in prompt
    assert "POSTURE-MARK holding" in prompt and "ARCHIVED-MARK" not in prompt
    # the two postures carry two labels: the transcript's and the cursor's
    assert (
        prompt.count("Your posture at the take:") == 1
        and "Your loop file's posture (cursor.posture):" in prompt
    )


def test_the_first_line_re_arms_each_watch_verbatim_before_any_work(tmp_path):
    view = transcript_turns(FIXTURE, 400)
    source = {"transcript": str(FIXTURE), "context": {"used": 900, "window": 1000, "pct": 90}}
    ckpt = take(source, harvest(tmp_path), actor=ACTOR, why="threshold", budget=400, view=view)
    prompt = render_prompt(ckpt)
    first = prompt.splitlines()[0]
    assert ckpt.id in first and ACTOR in first and "(90%)" in first and "threshold" in first
    assert "re-arm these 3 watch(es)" in first and "before any other work" in first.lower()
    watched = [v.value["command"] for v in ckpt.vars if v.kind == "watch"]
    assert all(c in prompt for c in watched)  # the one with backticks and quotes included
    assert "````\n" + monitor_commands()["toolu_m2"] + "\n````" in prompt  # a fence longer than its ```
    work = prompt.index("The person's words")
    assert all(prompt.index(c) < work for c in watched)
    assert prompt.index("The person's words") < prompt.index("Lanes") < prompt.index("The last")
    assert prompt.rstrip().splitlines()[-1].startswith("What to do first:")


def test_the_ceiling_eight_directives_half_the_budget_and_the_watch_list(tmp_path):
    ring = tmp_path / "sessions"
    ring.mkdir()
    said = [
        {"role": "person", "content": f"placeholder directive {i}", "timestamp": 1790000000 + i}
        for i in range(9)
    ]
    (ring / "s1.json").write_text(json.dumps({"session_id": "s1", "messages": said}))
    got = directives("s1", FIXTURE, tmp_path / "none.jsonl", sessions=ring)
    assert [d.value for d in got] == [f"placeholder directive {i}" for i in range(1, 9)]  # nine in, eight out
    # from the transcript: an event that woke the actor AFTER the owner's last words is not one of them
    raw = FIXTURE.read_text().splitlines()
    late_event = next(r for r in raw if '"type": "attachment"' in r and "aleg4" in r)
    tail = tmp_path / "tail.jsonl"
    tail.write_text("\n".join([*raw, late_event.replace("-000000000059", "-000000000259")]) + "\n")
    from_tail = directives("no-ring", tail, tmp_path / "none.jsonl", sessions=tmp_path / "no-sessions")
    assert from_tail[-1].value == PROMPTS[-1] and not any("[event]" in str(v.value) for v in from_tail)
    assert {d.source for d in got} == {"hook"} and DIRECTIVES == 8
    (ring / "s2.json").write_text(json.dumps({"directives": [f"said {i}" for i in range(9)]}))
    assert [d.value for d in directives("s2", FIXTURE, tmp_path / "none.jsonl", sessions=ring)][0] == "said 1"
    # no ring: the transcript's newest prompts the actor tool did not type, eight always
    fallback = [d.value for d in directives("s3", FIXTURE, tmp_path / "none.jsonl", sessions=ring)]
    assert fallback == PROMPTS[-8:]
    typed = [d.value for d in harvest(tmp_path) if d.kind == "directive"]
    assert typed == [p for p in PROMPTS if p != PROMPTS[8]][-8:]
    # render carries the newest eight however many it is handed
    nine = [Var(f"directive.{i}", "directive", f"placeholder word {i}", "hook", "") for i in range(9)]
    prompt = render_prompt(take({}, nine, actor=ACTOR, why="manual", budget=100))
    assert "placeholder word 0" not in prompt and all(f"placeholder word {i}" in prompt for i in range(1, 9))
    # the kept view is half the budget, the latest turn always kept
    for budget in (40, 400, 4000):
        view = transcript_turns(FIXTURE, budget)
        sizes = [(len(t["input"]) + len(t["text"])) // 4 for t in view["kept"]]
        assert sum(sizes) <= budget // 2 or len(sizes) == 1
        assert view["kept"][-1]["input"] == PROMPTS[-1] and len(view["kept"]) + len(view["hidden"]) == 14
    first = transcript_turns(FIXTURE, 10**6)["kept"][0]
    assert transcript_turns(FIXTURE, 0)["hidden"][0] == f"{first['turn']}@{first['at']}"


def test_legs_are_agents_the_transcript_shows_running(tmp_path):
    got = spawns(FIXTURE, AT_TAKE)
    # aleg2 ended by its own completion notice, aleg3 by a TaskStop, aleg4 by a notice that only ever
    # reached the transcript as queue bookkeeping and an attachment (measured 2026-09-24 on an actor
    # whose spawns finished while it worked: no user row carried the completion)
    assert [(v.name, v.value) for v in got] == [
        ("spawn.aleg1", {"id": "aleg1", "task": "spawn one", "state": "running-at-take"})
    ]
    raw = FIXTURE.read_text().splitlines()
    deaf = tmp_path / "deaf.jsonl"
    deaf.write_text("\n".join(r for r in raw if "ATTACHED-RESULT-MARK" not in r) + "\n")
    # the control: with the notice gone, aleg4 reads as alive
    assert [v.name for v in spawns(deaf, AT_TAKE)] == ["spawn.aleg1", "spawn.aleg4"]
    # each carrier alone ends it: the queue rows without the attachment, the attachment without them
    queue_only = tmp_path / "queue.jsonl"
    queue_only.write_text("\n".join(r for r in raw if '"type": "attachment"' not in r) + "\n")
    assert [v.name for v in spawns(queue_only, AT_TAKE)] == ["spawn.aleg1"]
    attach_only = tmp_path / "attach.jsonl"
    attach_only.write_text("\n".join(r for r in raw if '"type": "queue-operation"' not in r) + "\n")
    assert [v.name for v in spawns(attach_only, AT_TAKE)] == ["spawn.aleg1"]
    # a resumed agent keeps its task id: a late duplicate of the OLD call's notice does not end the new
    # call (the notice names the call it belongs to)
    spawn = next(r for r in raw if '"id": "toolu_a4"' in r)
    result = next(r for r in raw if '"tool_use_id": "toolu_a4"' in r)
    late = next(r for r in raw if '"type": "attachment"' in r and "aleg4" in r)
    resumed = tmp_path / "resumed.jsonl"
    resumed.write_text(
        "\n".join(
            [
                *raw,
                spawn.replace("toolu_a4", "toolu_a4b").replace("-000000000057", "-000000000157"),
                result.replace("toolu_a4", "toolu_a4b").replace("-000000000058", "-000000000158"),
                late.replace("-000000000059", "-000000000159"),
            ]
        )
        + "\n"
    )
    assert [v.name for v in spawns(resumed, AT_TAKE)] == ["spawn.aleg1", "spawn.aleg4"]
    # a spawn older than the bound with no notice is dead: a restart loses its children silently
    assert spawns(FIXTURE, AT_TAKE + timedelta(hours=13)) == []


def test_a_prompt_the_harness_requeued_is_one_turn(tmp_path):
    raw = FIXTURE.read_text().splitlines()
    # the last prompt row again, twice, with no assistant text between: the same turn, the newest kept
    last = next(line for line in reversed(raw) if '"placeholder prompt 11"' in line)
    again = tmp_path / "again.jsonl"
    again.write_text(
        "\n".join(raw + [last.replace('"uuid": "', f'"uuid": "again{i}-') for i in (1, 2)]) + "\n"
    )
    view = transcript_turns(again, 10**6)
    before = transcript_turns(FIXTURE, 10**6)["kept"]
    # the answered original stays a turn of its own; the two unanswered copies are one, the newest
    assert len(view["kept"]) == len(before) + 1
    assert view["kept"][-1]["turn"].startswith("again2-") and view["kept"][-1]["text"] == ""
    assert view["kept"][-2]["input"] == PROMPTS[-1] and view["kept"][-2]["text"] == before[-1]["text"]


def test_a_take_refused_by_the_gitignore_writes_nothing_and_an_ignored_one_lands(
    tmp_path, monkeypatch, capsys
):
    isolated_git(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    d = actor_dir(repo)
    ckpt = take({}, [], actor=ACTOR, why="manual", budget=100)
    with pytest.raises(NotIgnored, match="does not ignore it"):
        save(ckpt, d)
    assert not (d / ".khive" / "context").exists()
    with pytest.raises(SystemExit) as out:
        main(["context", "checkpoint", "--dir", str(d), "--transcript", str(FIXTURE)])
    assert out.value.code == 1
    assert "lion context checkpoint: " in capsys.readouterr().err
    assert not (d / ".khive" / "context").exists()
    # the pattern a repo that tracks other .khive files uses, checked before the directory exists
    # (measured: `check-ignore .khive/context` said no, the directory form says yes)
    # (this actor dir sits BELOW the repo root, so the pattern needs `**/`; an actor that is the root
    # writes `.khive/context/`, the form the remedy names)
    (repo / ".gitignore").write_text("**/.khive/context/\n")
    assert not (d / ".khive" / "context").exists()
    written = save(ckpt, d)
    assert written.is_file() and json.loads((written.parent / "latest.json").read_text()) == {
        "id": written.stem
    }
    # outside any repository the take proceeds
    outside = tmp_path / "plain"
    probe = subprocess.run(["git", "-C", str(tmp_path), "rev-parse"], capture_output=True, text=True)
    assert "not a git repository" in probe.stderr  # the control: tmp_path itself is outside a repository
    assert save(ckpt, actor_dir(outside)).is_file()


def test_no_model_is_called_by_the_checkpoint_the_restore_or_the_status(tmp_path, monkeypatch):
    called: list[str] = []

    async def no_run(*a, **k):
        called.append("Actor.run")
        raise AssertionError("a checkpoint ran a model")

    monkeypatch.setattr(Actor, "run", no_run)
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    done = checkpoint(d, "manual", transcript=FIXTURE, khive=TaskKhive([TASK]), now=AT_TAKE)
    got = restore(d)
    now = status(d, session="no-such-session")
    assert called == []
    assert done["id"] == got["id"] == now["latest"] and done["kept"] + done["hidden"] == 14
    assert "watch.tmon1" in done["vars"] and "lane.1ane0001" in done["vars"]


def test_a_long_var_is_its_name_size_and_loader_while_a_persons_words_and_watches_stay_whole(tmp_path):
    words = "placeholder words, said once " * 40
    command = "bash /tmp/long-watch.sh " + "a" * 500
    lanes_ = [{"kind": "task", "id": f"t{i}", "title": "placeholder lane"} for i in range(20)]
    given = [
        Var("directive.1", "directive", words, "hook", ""),
        Var(
            "watch.t1",
            "watch",
            {"command": command, "description": "long", "timeout_ms": None},
            "transcript",
            "",
        ),
        Var("posture", "posture", "q" * (LAZY + 1), "notes", "7", ref='note.get(key="posture")'),
        Var("lanes", "lane", lanes_, "khive", ""),
        Var("short", "fact", "s" * LAZY, "declared", ""),
    ]
    c = take({}, given, actor=ACTOR, why="manual", budget=100)
    by = {v.name: v for v in c.vars}
    # a ref names the store that keeps the value; without one the checkpoint file keeps it
    assert (by["posture"].value, by["posture"].size) == (None, LAZY + 1)
    assert by["lanes"].value == lanes_ and by["lanes"].size == len(json.dumps(lanes_, ensure_ascii=False))
    assert by["short"].size is None and by["directive.1"].size is None and by["watch.t1"].size is None
    prompt = render_prompt(c)
    assert words in prompt and command in prompt and "s" * LAZY in prompt
    assert "q" * (LAZY + 1) not in prompt and '"t19"' not in prompt
    assert f'{LAZY + 1} chars, not shown here; load it with `note.get(key="posture")`' in prompt
    assert (
        f"{by['lanes'].size} chars, not shown here; load it with `lion context show --id {c.id} --var lanes`"
        in prompt
    )


class Raises:
    """A backend a checkpoint must never reach."""

    def __init__(self):
        self.calls: list = []

    async def __call__(self, messages):
        self.calls.append(messages)
        raise AssertionError("the resident's checkpoint called its backend")


class Growing:
    """A backend whose reported prompt count grows past the view budget: the fold event the chair's
    record carries when a session-keeping CLI's history outgrows the view."""

    def __init__(self, *turns: tuple[str, int]):
        self.turns, self.calls, self.seen = list(turns), [], []

    async def __call__(self, messages):
        self.seen.append(list(messages))
        self.calls.append({"total_cost_usd": 0.0})
        text, reported = self.turns.pop(0)
        return Reply(text, prompt_tokens=reported)


def resident(khive: FakeKhive, backend, **kw) -> Agent:
    return Agent(
        Actor("echo", privileges={"comm.send"}),
        khive,
        backend,
        profile_for,
        continuous=True,
        poll_ms=20,
        **kw,
    )


FAILS = '<lact {}>note.get(key="absent")</lact>'  # a failed command: the burst gets one more turn
LONG_POSTURE = "quiet now; " + "placeholder posture " * 25


def serve_one(agent: Agent) -> list[str]:
    lines: list[str] = []
    asyncio.run(agent.serve(until=lambda: len(lines) >= 1, log=lines.append))
    return lines


def test_the_resident_checkpoints_at_its_fold_and_its_next_process_resumes_the_same_view(tmp_path):
    backend = Growing((FAILS.format("g1"), 1_000), (FAILS.format("g2"), 500_000), (LONG_POSTURE, 500_000))
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    agent = resident(khive, backend)
    lines = serve_one(agent)
    (run,) = agent.runs
    folds = [e for e in run.record if e.kind is Kind.SYSTEM and "directive" in e.meta]
    assert len(folds) == 1 and folds[0].meta["directive"].refs and agent.errors == []
    assert [line for line in lines if line.startswith("checkpoint ")] == [
        f"checkpoint {load(tmp_path).id} (threshold) at seq {run.record.seq}"
    ]
    ckpt = load(tmp_path)
    assert (
        ckpt.why == "threshold" and ckpt.source["kind"] == "resident" and ckpt.source["seq"] == run.record.seq
    )
    assert ckpt.actor == khive.actor and ckpt.source["context"]["used"] > 60_000  # the figure the fold used
    # the posture note is over LAZY: carried by its key and size, the note keeps the body
    held = {v.name: v for v in ckpt.vars}
    note = agent.actor.notes_for(run.profile).get("posture").value
    assert len(note) > LAZY and (held["posture"].value, held["posture"].size) == (None, len(note))
    assert held["posture"].ref == 'note.get(key="posture")' and "last_wake" in held
    # no result rides inline: the record stays where source.record points
    text = next((tmp_path / ".khive" / "context").glob("ckpt-*.json")).read_text()
    assert "no note 'absent'" not in text and "no note 'absent'" in Path(ckpt.source["record"]).read_text()
    history = to_history(ckpt)
    assert [(e.seq, e.kind, e.content) for e in history] == [(e.seq, e.kind, e.content) for e in run.record]
    # the next process: its first run starts from the checkpoint, the view folded where it folded
    khive2 = FakeKhive(mail("lambda:a", "one more", mid="c3d4e5f6"), cwd=tmp_path)
    backend2 = Growing(("still here", 2_000))
    agent2 = resident(khive2, backend2)
    serve_one(agent2)
    (run2,) = agent2.runs
    n = len(history)
    assert [e.content for e in list(run2.record)[:n]] == [e.content for e in run.record]

    def shape(record, upto):
        return [
            (v.entry.seq, v.hidden, v.summary)
            for v in fold(record, protect_input=False)
            if v.entry.seq < upto
        ]

    assert shape(run2.record, n) == shape(run.record, n)
    hidden = [v.entry.name for v in fold(run.record, protect_input=False) if v.hidden]
    first_view = "\n".join(m.content for m in backend2.seen[0])
    assert hidden and all(name in first_view for name in hidden) and agent2.errors == []
    # the take makes no model call: a backend that raises is never reached
    agent.backend = Raises()
    agent._checkpoint(run, "manual")
    assert agent.backend.calls == [] and agent.errors == [] and load(tmp_path).why == "manual"


def test_the_resident_checkpoints_when_stop_ends_its_run(tmp_path):
    backend = Growing((FAILS.format("g1"), 1_000), ("never read", 1_000))
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    agent = resident(khive, backend, max_rounds=1)
    serve_one(agent)
    (run,) = agent.runs
    ckpt = load(tmp_path)
    assert ckpt.why == "end" and ckpt.source["seq"] == run.record.seq and agent.errors == []
    assert len(backend.calls) == 1  # the second round's notification ended the run before the model


def test_a_restore_that_cannot_replay_is_said_and_the_run_starts_fresh(tmp_path):
    rec = Record("r0")
    rec.append(Kind.INPUT, "placeholder")
    source = {"record": str(tmp_path / "gone.jsonl"), "run": "r0"}
    save(take(source, [], actor="lambda:a:echo", why="end", budget=100, record=rec), tmp_path)
    with pytest.raises(CheckpointError, match="cannot be read"):
        to_history(load(tmp_path))
    khive = FakeKhive(mail("lambda:a", "say hi back"), cwd=tmp_path)
    agent = resident(khive, Growing((REPLY, 1_000)))
    lines = serve_one(agent)
    (run,) = agent.runs
    assert lines[0].startswith("restore failed, the run starts fresh: restore: CheckpointError")
    assert len(agent.errors) == 1 and "cannot be read" in agent.errors[0]
    first = list(run.record)[0]
    assert first.kind is Kind.INPUT and "say hi back" in first.content


def test_lion_context_set_checkpoint_show_restore_and_status_through_the_cli(tmp_path, monkeypatch, capsys):
    import lion_cli.context as cli

    monkeypatch.setattr("hub.context.INBOX_PRIORITY", ("lambda:owner",))

    def down(*a, **k):
        raise KhiveError("kkernel not found on PATH or under ~/.cargo/bin")

    monkeypatch.setattr(cli, "Khive", down)
    gh_says(monkeypatch, rc=1, err="no git remotes found")
    d = actor_dir(tmp_path)
    big = "b" * (LAZY + 100)
    main(
        ["context", "set", "--dir", str(d), "--name", "next", "--kind", "fact", "--value", "placeholder next"]
    )
    main(["context", "set", "--dir", str(d), "--name", "big", "--kind", "fact", "--value", big])
    main(
        ["context", "set", "--dir", str(d), "--name", "mcp", "--kind", "fact", "--value", '["khive", "lion"]']
    )
    declared = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert declared["value"] == ["khive", "lion"] and declared["source"] == "declared"
    before = status(d)
    assert (before["pending_vars"], before["latest"], before["stale"]) == (3, None, True)
    main(["context", "checkpoint", "--dir", str(d), "--transcript", str(FIXTURE), "--why", "rotate"])
    done = json.loads(capsys.readouterr().out)
    assert {"next", "big", "mcp"} <= set(done["vars"]) and done["context"] == {
        "used": None,
        "window": None,
        "pct": None,
    }
    assert load(d).source["lanes"] == (
        "gtd.tasks and comm.inbox unavailable: kkernel not found on PATH or under ~/.cargo/bin; "
        "gh pr list unavailable: no git remotes found"
    )
    after = status(d)
    assert after["latest"] == done["id"] and after["pending_vars"] == 0  # the checkpoint drained them
    main(["context", "restore", "--dir", str(d)])
    prompt = capsys.readouterr().out
    assert prompt.startswith(f"Checkpoint {done['id']} of {ACTOR}")
    assert (
        'MCP servers expected: ["khive", "lion"]' in prompt and "What to do first: placeholder next" in prompt
    )
    assert (
        big not in prompt
        and f"- big: {len(big)} chars, not shown here; load it with `lion context show" in prompt
    )
    main(["context", "show", "--dir", str(d), "--id", done["id"], "--var", "big"])
    assert capsys.readouterr().out == big + "\n"
    main(["context", "show", "--dir", str(d), "--var", "mcp"])
    assert json.loads(capsys.readouterr().out) == ["khive", "lion"]
    main(["context", "restore", "--dir", str(d), "--json"])
    got = json.loads(capsys.readouterr().out)
    assert set(got) == {"id", "first_prompt", "watches", "spawns"} and got["first_prompt"] == prompt.rstrip(
        "\n"
    )
    assert got["watches"][-1]["command"].endswith(f"{ACTOR} lambda:owner")
    for argv, said in (
        (["restore", "--id", "../../etc/passwd"], "is not a name"),
        (["show", "--var", "nosuch"], "has no var 'nosuch'"),
    ):
        with pytest.raises(SystemExit) as out:
            main(["context", argv[0], "--dir", str(d), *argv[1:]])
        assert out.value.code == 1 and said in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["context", "set", "--dir", str(d), "--name", "x", "--kind", "nonsense", "--value", "v"])


def test_a_declared_value_holding_a_line_separator_is_read_back_whole(tmp_path):
    import lion_cli.context as cli

    d = actor_dir(tmp_path)
    odd = ["a\nb", "a\x85b", "a\u2028b", "a\u2029b"]  # the writer keeps all but LF unescaped
    for i, value in enumerate(odd):
        cli.set_var(d, f"v{i}", "fact", value)
    got, _ = cli._pending(d)
    assert [v.value for v in got] == odd and status(d)["pending_vars"] == 4


def test_the_context_routes_answer_for_a_registered_desk_only(tmp_path, monkeypatch):
    import lion_cli.context as cli

    d = actor_dir(tmp_path)
    reg = registry(tmp_path, d)
    code, doc = context_api(reg, "GET", "/api/context/status", {"dir": str(d)})
    assert code == 200 and doc["latest"] is None
    code, doc = context_api(reg, "GET", "/api/context/status", {"dir": str(tmp_path / "elsewhere")})
    assert code == 404 and "not a desk" in doc["detail"]
    code, doc = context_api(reg, "GET", "/api/context/restore", {"dir": str(d)})
    assert code == 409 and "latest.json is absent" in doc["detail"]
    # the desk has no transcript of its own here: the take refuses, and says so
    code, doc = context_api(reg, "POST", "/api/context/checkpoint", {"dir": str(d), "why": "end"})
    assert code == 409 and doc["ok"] is False and "no Claude Code transcript" in doc["detail"]
    monkeypatch.setattr(cli, "newest_transcript", lambda directory: FIXTURE)
    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    code, doc = context_api(reg, "POST", "/api/context/checkpoint", {"dir": str(d), "why": "end"})
    assert code == 200 and doc["id"].startswith("ckpt-") and "lane.1ane0001" in doc["vars"]
    code, doc = context_api(reg, "GET", "/api/context/restore", {"dir": str(d), "id": doc["id"]})
    assert code == 200 and doc["first_prompt"].startswith("Checkpoint ckpt-")
    assert context_api(reg, "DELETE", "/api/context/status", {"dir": str(d)})[0] == 404
    # a declaration over the wire: the same pending file `lion context set` writes, refused on a bad kind
    declared = {"dir": str(d), "name": "n", "kind": "fact", "value": "v"}
    code, doc = context_api(reg, "POST", "/api/context/set", declared)
    assert code == 200 and doc["name"] == "n" and doc["source"] == "declared"
    assert (d / ".khive" / "context" / "pending.jsonl").read_text().count("\n") == 1
    code, doc = context_api(reg, "POST", "/api/context/set", {"dir": str(d), "name": "n", "kind": "nope"})
    assert code == 409 and "kind" in doc["detail"]
    assert context_api(reg, "POST", "/api/context/set", {"dir": str(tmp_path / "x"), "name": "n"})[0] == 404


def test_the_served_context_routes_sit_behind_the_same_gates(tmp_path):
    d = actor_dir(tmp_path)
    port = serve_on(registry(tmp_path, d))
    code, text, _ = call(port, "GET", f"/api/context/status?dir={d}")
    assert code == 200 and json.loads(text)["pending_vars"] == 0
    assert call(port, "GET", f"/api/context/status?dir={tmp_path}")[0] == 404
    code, text, _ = call(
        port, "GET", f"/api/context/status?dir={d}", headers={"Host": f"evil.example:{port}"}
    )
    assert code == 403 and "is not this server" in text  # the Host gate, as on every other route
    body = json.dumps({"dir": str(d), "why": "end"})
    json_type = {"Content-Type": "application/json"}
    assert (
        call(port, "POST", "/api/context/checkpoint", body, {**json_type, "Origin": "http://evil.example"})[0]
        == 403
    )
    code, text, _ = call(port, "POST", "/api/context/checkpoint", body, json_type)
    assert code == 409 and json.loads(text)["ok"] is False  # no transcript for this desk: refused, said
    assert not (d / ".khive" / "context").exists() or not list((d / ".khive" / "context").glob("ckpt-*.json"))


def test_a_precompact_hook_takes_the_sessions_checkpoint_from_what_claude_code_hands_it(
    tmp_path, monkeypatch, capsys
):
    # nothing in the code started a Claude Code session's checkpoint: a person or a program had to run
    # `lion context checkpoint`, and the newest transcript stood in for the session compacting
    import lion_cli.context as cli

    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    d = actor_dir(tmp_path)
    given = {
        "session_id": "s-compacting",
        "transcript_path": str(FIXTURE),
        "cwd": str(d),
        "hook_event_name": "PreCompact",
        "trigger": "auto",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(given)))
    main(["context", "checkpoint", "--hook"])  # no --dir: the hook's cwd names the session's directory
    done = json.loads(capsys.readouterr().out)
    ckpt = load(d)
    assert ckpt.id == done["id"] and ckpt.why == "precompact" and "lane.1ane0001" in done["vars"]
    assert (ckpt.source["session_id"], ckpt.source["transcript"], ckpt.source["cwd"]) == (
        "s-compacting",
        str(FIXTURE),
        str(d.resolve()),
    )
    for text, said in (
        ("not json", "the hook's input is not JSON"),
        ("[]", "hook event None: only PreCompact takes a checkpoint; nothing was taken"),
        (json.dumps({**given, "hook_event_name": "Stop"}), "hook event 'Stop': only PreCompact takes"),
        # an event missing its identity is refused, never filled in from the directory's newest transcript
        (
            json.dumps({k: v for k, v in given.items() if k != "transcript_path"}),
            "hook's transcript_path is missing",
        ),
        (json.dumps({**given, "session_id": ""}), "hook's session_id is missing"),
        (json.dumps({**given, "cwd": 7}), "hook's cwd is missing or not a string"),
    ):
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        with pytest.raises(SystemExit) as out:
            main(["context", "checkpoint", "--hook"])
        assert out.value.code == 1 and said in capsys.readouterr().err
    assert [p.stem for p in (d / ".khive" / "context").glob("ckpt-*.json")] == [done["id"]]


def test_a_claude_code_checkpoint_in_a_residents_directory_is_refused_by_its_next_process(
    tmp_path, monkeypatch
):
    # one latest.json serves both arms: a take through the context routes, which reach a resident's
    # directory only, becomes the latest; the resident's next process refuses it and starts fresh, and
    # its own checkpoint stays on disk
    import lion_cli.context as cli

    d = actor_dir(tmp_path, "lambda:a:echo")
    agent = resident(
        FakeKhive(mail("lambda:a", "say hi back"), cwd=d),
        Growing((FAILS.format("g1"), 1_000), ("never read", 1_000)),
        max_rounds=1,
    )
    serve_one(agent)
    own = load(d)
    assert own.source["kind"] == "resident" and agent.errors == []
    monkeypatch.setattr(cli, "newest_transcript", lambda directory: FIXTURE)
    monkeypatch.setattr(cli, "Khive", lambda *a: TaskKhive([TASK]))
    gh_says(monkeypatch)
    code, doc = context_api(
        registry(tmp_path, d), "POST", "/api/context/checkpoint", {"dir": str(d), "why": "manual"}
    )
    assert code == 200 and load(d).id == doc["id"] != own.id and load(d).source["kind"] == "claude-code"
    again = resident(FakeKhive(mail("lambda:a", "one more", mid="c3d4e5f6"), cwd=d), Growing((REPLY, 1_000)))
    lines = serve_one(again)
    refused = f"{doc['id']} is a claude-code checkpoint: its restore is the first prompt"
    assert again.errors == [f"restore: CheckpointError: {refused}"]
    assert lines[0] == f"restore failed, the run starts fresh: restore: CheckpointError: {refused}"
    (run,) = again.runs
    first = list(run.record)[0]
    assert first.kind is Kind.INPUT and "one more" in first.content and "say hi back" not in first.content
    assert load(d, own.id) == own  # the resident's checkpoint is on disk, no longer the one latest names


def test_only_the_first_invalid_row_is_spelled_out():
    from hub.context import _valid

    class Counted:
        calls = 0

        def __repr__(self):
            Counted.calls += 1
            return "x" * 200

    rows = [{"number": Counted()} for _ in range(100)] + [{"number": 3}]
    good, note = _valid(rows, "pr")
    assert [r["number"] for r in good] == [3]
    assert note == f" (100 invalid; the first: number {'x' * 40}, not a positive int)"
    assert Counted.calls == 1  # the other 99 are counted, never rendered
