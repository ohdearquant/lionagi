"""The checkpoint's sources in `hub/context.py` (ADR-0019/D2): the actor's reader works on a synthetic
transcript holding one row of every kind a live one carries, placeholder text throughout; each source
harvests its kind, and an absent source is a value."""

import asyncio
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hub.agent import Agent
from hub.context import (
    INBOX_MONITOR,
    context_figure,
    directives,
    lanes,
    posture,
    spawns,
    topic,
    transcript_turns,
    watches,
)
from hub.khive import KhiveError
from lionagi import Actor, Reply
from lionagi.checkpoint import (
    DIRECTIVES,
    NotIgnored,
    Var,
    render_prompt,
    save,
    take,
)
from tests.test_agent import FakeKhive, profile_for

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
    """gtd.tasks as khive answers it: a bare list of task rows."""

    def __init__(self, tasks=None, fail: str | None = None):
        self.actor, self.cwd, self.ops = ACTOR, None, []
        self.tasks, self.fail = tasks or [], fail

    async def exec(self, ops: str) -> list:
        self.ops.append(ops)
        if self.fail:
            raise KhiveError(self.fail)
        return [self.tasks]


TASK = {"id": "1ane0001", "title": "placeholder lane", "priority": "p1", "status": "active", "properties": {}}


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
    assert said == "gtd.tasks active; open PRs and threads not read"
    # khive down: no lanes, and the reason is the value; no registry, no topic; no figure filed, nulls
    assert asyncio.run(lanes(TaskKhive(fail="kkernel exited 1: down"))) == (
        [],
        "unavailable: KhiveError: kkernel exited 1: down",
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
