"""The store (ADR-0025): a run's session, messages, calls and run rows written as it goes, a second run
replayed from the message rows alone, the profiles' notes as rows, and the client's synchronous call.
The binary is the stub `kkernel` of conftest, serving the lion pack from `tests/lionfake.py`; the last
test runs the same run against the real binary when `LION1_KHIVE` names it."""

import asyncio
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from hub.chat import chat
from lion1 import Actor, Kind, Profile, Reply, RunRequest, Store, Success
from lion1.checkpoint import row_json
from lion1.khive import Khive, KhiveError
from tests.lionfake import LionFake, LionKhive, parse, routed_config
from tests.test_actor import Done, scripted
from tests.test_chat import asks, reader

__all__ = ()

ACTOR = "lion1:store-test"
TURNS = ("thinking about it", 'OUT{done: {summary: "read", files: []}}')


def client(d: Path, binary: str | None = None) -> Khive:
    (d / ".khive").mkdir(exist_ok=True)
    (d / ".khive" / "config.toml").write_text(routed_config(ACTOR, d))
    return Khive(d, ACTOR, kkernel=binary)


def test_a_store_refuses_a_config_that_would_put_the_lion_tables_in_the_main_store(tmp_path, kkernel_stub):
    (tmp_path / ".khive").mkdir()
    cfg = tmp_path / ".khive" / "config.toml"
    unrouted = (
        '[actor]\nid = "a"\n',  # no backends at all: everything in the main store
        '[actor]\nid = "a"\n[[backends]]\nname = "main"\npath = "m.db"\n[[backends]]\nname = "lion"\n'
        'path = "l.db"\n',
        '[actor]\nid = "a"\n[[backends]]\nname = "lion"\npath = "l.db"\n[packs.lion]\nbackend = "lion"\n',
        '[actor]\nid = "a"\n[[backends]]\nname = "main"\npath = "m.db"\n[[backends]]\nname = "lion"\n'
        '[packs.lion]\nbackend = "lion"\n',
    )
    for text in unrouted:  # the lion entry missing, unrouted, without main, or without a path
        cfg.write_text(text)
        with pytest.raises(KhiveError, match="would resolve to the main store"):
            Store(Khive(tmp_path, "a"))
    cfg.write_text("[actor\n")
    with pytest.raises(KhiveError, match="config.toml"):
        Store(Khive(tmp_path, "a"))
    cfg.write_text(routed_config("a", tmp_path))
    assert Store(Khive(tmp_path, "a")).database == tmp_path / ".khive" / "lion.db"
    assert Store(LionKhive()).database is None  # a double has no file to route


def backends(main: str, lion: str, lion_kind: str = "") -> str:
    return (
        f'[actor]\nid = "a"\n[[backends]]\nname = "main"\npath = {json.dumps(main)}\n'
        f'[[backends]]\nname = "lion"\npath = {json.dumps(lion)}\n{lion_kind}[packs.lion]\nbackend = "lion"\n'
    )


def test_a_store_refuses_a_lion_path_that_reaches_the_main_stores_file(tmp_path, kkernel_stub, monkeypatch):
    """The binary runs in the client's directory, takes a relative path from there and `~` as the home,
    and opens one database for two names that reach one file: each such config would write the lion
    pack's tables into the main store, so the store refuses it, as it does a path SQLite reads as a URI,
    two backends of one name and a lion backend that is not a file. The directory the check resolves from
    is the client's, never the process's."""
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    home.mkdir()
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(elsewhere)  # a relative path read from here would name another file
    (tmp_path / ".khive").mkdir()
    cfg = tmp_path / ".khive" / "config.toml"
    main = tmp_path / ".khive" / "main.db"
    (tmp_path / "alias.db").symlink_to(main)  # dangling until the binary makes main: still main's file
    same = {
        "one path": (str(main), str(main)),
        "relative to the client's directory": (str(main), ".khive/main.db"),
        "the home": ("~/m.db", str(home / "m.db")),
        "a symlink": (str(main), str(tmp_path / "alias.db")),
        "a dot-dot": (str(main), str(tmp_path / ".khive" / "x" / ".." / "main.db")),
        "letter case, neither file made": (str(main), str(tmp_path / ".KHIVE" / "Main.DB")),
        "a Unicode form, neither file made": (
            str(tmp_path / "caf\u00e9.db"),
            str(tmp_path / "cafe\u0301.db"),
        ),
    }
    firm = Path("/System/Volumes/Data") / os.path.realpath(tmp_path).lstrip("/")
    if firm.exists() and os.path.realpath(firm) != os.path.realpath(tmp_path):  # macOS: a second name
        same["a firmlink, neither file made"] = (str(main), str(firm / ".khive" / "main.db"))
    assert not main.exists()  # the ones above are refused by their names, before the binary makes a file
    for m, lion in same.values():
        cfg.write_text(backends(m, lion))
        with pytest.raises(KhiveError, match="is the main store's own file"):
            Store(Khive(tmp_path, "a"))
    for m, lion in ((str(main), f"file:{main}?mode=rwc"), (f"file:{main}", "l.db")):
        cfg.write_text(backends(m, lion))
        with pytest.raises(KhiveError, match="SQLite reads as a URI"):
            Store(Khive(tmp_path, "a"))
    main.write_bytes(b"")
    (tmp_path / "hard.db").hardlink_to(main)  # two paths, one file: the binary's canonical paths differ
    cfg.write_text(backends(str(main), str(tmp_path / "hard.db")))
    with pytest.raises(KhiveError, match="is the main store's own file"):
        Store(Khive(tmp_path, "a"))
    cfg.write_text(backends(str(main), "l.db") + '[[backends]]\nname = "lion"\npath = "other.db"\n')
    with pytest.raises(KhiveError, match="two backends share a name"):
        Store(Khive(tmp_path, "a"))
    cfg.write_text(backends(str(main), "l.db", 'kind = "memory"\n'))
    with pytest.raises(KhiveError, match="would resolve to the main store"):
        Store(Khive(tmp_path, "a"))
    # the controls: a relative lion path that is its own file, from the client's directory
    cfg.write_text(backends(".khive/main.db", "rows/lion.db"))
    assert Store(Khive(tmp_path, "a")).database == tmp_path / "rows" / "lion.db"
    cfg.write_text(backends(str(main), "l.db", 'kind = "sqlite"\n'))
    assert Store(Khive(tmp_path, "a")).database == tmp_path / "l.db"
    cfg.write_text(backends(str(main), "l.db").replace(f"path = {json.dumps(str(main))}", 'kind = "memory"'))
    assert Store(Khive(tmp_path, "a")).database == tmp_path / "l.db"  # a main in memory has no file to reach


def one(k: Khive, ops: str):
    (got,) = k.exec_sync(ops)
    return got


def a_run(store: Store, **req):
    actor = Actor("clerk", store=store)
    turns = scripted(*TURNS)

    async def billed(messages):
        return Reply(await turns(messages), prompt_tokens=11, cost=0.001)

    req = RunRequest(inputs=("read the file",), emits=(Done,), max_turns=3, **req)
    return actor, asyncio.run(actor.run(Profile("clerk"), req, billed))


def rows_of(k: Khive, run_id: str) -> dict:
    return {
        "session": one(k, f'lion.get(table="sessions", id="{run_id}")'),
        "run": one(k, f'lion.get(table="runs", id="{run_id}")'),
        "messages": one(k, f'lion.get(table="messages", id="{run_id}")')["rows"],
    }


def check_rows(k: Khive, run) -> None:
    got = rows_of(k, run.id)
    assert isinstance(run.outcome, Success)
    assert got["session"]["source"] == "lion1" and got["session"]["ended"] is not None
    assert got["session"]["message_count"] == len(run.record) == len(got["messages"])
    assert got["run"]["outcome"] == "success" and got["run"]["turns"] == run.turn
    assert got["run"]["cost"] == pytest.approx(run.cost) and json.loads(got["run"]["output"]) == {
        "done": {"summary": "read", "files": []}
    }
    assert [m["seq"] for m in got["messages"]] == [e.seq for e in run.record]
    assert [m["role"] for m in got["messages"]] == [
        {"input": "user", "system": "system", "text": "assistant", "result": "tool"}[e.kind.value]
        for e in run.record
    ]
    texts = [m["text"] for m in got["messages"] if m["role"] == "assistant"]
    assert texts == list(TURNS)
    calls = one(k, 'lion.runs(limit=5, actor="clerk")')
    assert run.id in [r["id"] for r in calls["rows"]]


def test_a_run_writes_its_rows_and_a_second_run_replays_them(tmp_path, kkernel_stub):
    k = client(tmp_path)
    store = Store(k)
    _, run = a_run(store, trigger=("mail-1",))
    check_rows(k, run)
    got = rows_of(k, run.id)
    assert json.loads(got["run"]["trigger"]) == ["mail-1"] and got["run"]["continues"] is None
    # every call row: opened before the backend answered, closed ok with the reply's seq and the price
    opened = [o for o in k.calls if "lion.call_open(" in o["ops"]]
    closed = [o for o in k.calls if "lion.call_close(" in o["ops"]]
    assert len(opened) == len(closed) == run.turn == 2
    close_args = [parse(o["ops"])[0][1] for o in closed]
    assert [c["status"] for c in close_args] == ["ok", "ok"]
    assert [c["reply_seq"] for c in close_args] == [e.seq for e in run.record if e.kind is Kind.TEXT]
    assert all(c["tokens_in"] == 11 and c["cost"] == 0.001 and c["duration_ms"] >= 0 for c in close_args)
    # C1: when a call opens, every entry before its reply is a row already (the reply is the next
    # entry); C2: each reply seq names a message row
    ops = [o["ops"] for o in k.calls]
    opens = [i for i, o in enumerate(ops) if "lion.call_open(" in o]
    for i, c in zip(opens, close_args, strict=True):
        written = sum(ops[j].count("lion.message_append(") for j in range(i))
        assert written == sum(1 for e in run.record if e.seq < c["reply_seq"])
    assert all(c["reply_seq"] in [m["seq"] for m in got["messages"]] for c in close_args)
    # the message rows are the entries: a second run continues from them, with no checkpoint file (C3)
    history = asyncio.run(store.history(run.id))
    assert [e.kind for e in history] == [e.kind for e in run.record]
    assert [e.content for e in history] == [e.content for e in run.record]
    _, again = a_run(store, history=history, continues=run.id)
    assert isinstance(again.outcome, Success) and len(again.record) == 2 * len(
        run.record
    )  # replayed, then the same again
    assert rows_of(k, again.id)["run"]["continues"] == run.id
    # it writes its own entries and none it replayed: those are the rows of the run it continues (C3)
    own = [e.seq for e in again.record][len(history) :]
    assert [m["seq"] for m in rows_of(k, again.id)["messages"]] == own


def test_a_refused_reply_closes_its_call_refused_with_the_reason(tmp_path, kkernel_stub):
    k = client(tmp_path)
    actor = Actor("clerk", store=Store(k))

    async def refusing(messages):  # noqa: RUF029
        return Reply("no", refused="the provider said no")

    run = asyncio.run(actor.run(Profile("clerk"), RunRequest(inputs=("x",), max_turns=1), refusing))
    closed = [parse(o["ops"])[0][1] for o in k.calls if "lion.call_close(" in o["ops"]]
    assert [(c["status"], c["error"]) for c in closed] == [("refused", "the provider said no")]
    assert rows_of(k, run.id)["run"]["outcome"] == type(run.outcome).__name__.lower()


def test_a_malformed_cost_reaches_no_row_and_the_run_still_closes(tmp_path, kkernel_stub):
    # a call's row keeps only a figure the run would sum, and a run read as spent by a malformed one
    # (ADR-0003/S7) closes its row with no cost: the store, like the pack's parser, takes no NaN
    k = client(tmp_path)
    actor = Actor("clerk", store=Store(k))
    costs = iter([0.25, -0.3])

    async def billed(messages):  # noqa: RUF029
        return Reply("still reading", cost=next(costs))

    runs = [
        asyncio.run(actor.run(Profile("clerk"), RunRequest(inputs=("x",), max_turns=1), billed))
        for _ in range(2)
    ]
    closed = [parse(o["ops"])[0][1] for o in k.calls if "lion.call_close(" in o["ops"]]
    assert [c["cost"] for c in closed] == [0.25, None]
    assert [rows_of(k, run.id)["run"]["cost"] for run in runs] == [0.25, None]
    assert [rows_of(k, run.id)["run"]["outcome"] for run in runs] == ["exhausted", "exhausted"]


def test_a_request_that_raises_closes_its_call_failed_and_the_run_stopped(tmp_path, kkernel_stub):
    k = client(tmp_path)
    actor = Actor("clerk", store=Store(k))

    async def broken(messages):  # noqa: RUF029
        raise RuntimeError("the provider fell over")

    with pytest.raises(RuntimeError, match="fell over"):
        asyncio.run(actor.run(Profile("clerk"), RunRequest(inputs=("x",), max_turns=1), broken))
    (run_id,) = [r["id"] for r in one(k, 'lion.runs(limit=5, actor="clerk")')["rows"]]
    closed = [parse(o["ops"])[0][1] for o in k.calls if "lion.call_close(" in o["ops"]]
    assert [(c["status"], c["error"]) for c in closed] == [("failed", "RuntimeError: the provider fell over")]
    got = rows_of(k, run_id)
    assert got["run"]["outcome"] == "stopped" and got["session"]["ended"] is not None
    assert actor.runs == {}  # and nothing is left behind in the actor either
    # a store that fails while the rows close does not hide what ended the run
    real = k.exec

    async def failing_close(ops: str):
        if "lion.call_close(" in ops or "lion.run_close(" in ops:
            raise KhiveError("the store is down")
        return await real(ops)

    k.exec = failing_close  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="fell over"):
        asyncio.run(actor.run(Profile("clerk"), RunRequest(inputs=("x",), max_turns=1), broken))

    # a flush that fails before any call opens ends the run with the store's error and no call row
    async def failing_flush(ops: str):
        if "lion.message_append(" in ops:
            raise KhiveError("no room")
        return await real(ops)

    k.exec = failing_flush  # type: ignore[method-assign]
    with pytest.raises(KhiveError, match="no room"):
        asyncio.run(actor.run(Profile("clerk"), RunRequest(inputs=("x",), max_turns=1), broken))
    assert sum("lion.call_open(" in o["ops"] for o in k.calls) == 2  # the two runs above, none here
    # a store that cannot open the run leaves no run behind, and the error is the store's
    k.exec = lambda ops: (_ for _ in ()).throw(KhiveError("the store is down"))  # type: ignore[method-assign]
    with pytest.raises(KhiveError, match="the store is down"):
        asyncio.run(actor.run(Profile("clerk"), RunRequest(inputs=("x",), max_turns=1), broken))
    assert actor.runs == {}


def test_a_cut_call_closes_its_row_cut_and_the_run_goes_on(tmp_path, kkernel_stub):
    """As `test_chat`'s interrupt test, with the store on: the abandoned call's row is closed `cut` with
    no reply, and the next call's row is closed `ok`."""
    k = client(tmp_path)
    actor, profile = reader(tmp_path)
    actor.store = Store(k)
    started = asyncio.Event()
    calls: list[int] = []

    async def backend(messages):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            await asyncio.sleep(30)
        return "Back to you."

    async def cut_when_started():
        await started.wait()
        next(iter(actor.runs.values())).interrupt()

    async def main():
        cutter = asyncio.ensure_future(cut_when_started())
        try:
            return await asyncio.wait_for(
                chat(actor, profile, backend, asks("go", "still there?", None)), timeout=20
            )
        finally:
            cutter.cancel()

    run = asyncio.run(main())
    closed = [parse(o["ops"])[0][1] for o in k.calls if "lion.call_close(" in o["ops"]]
    assert [(c["status"], c["reply_seq"] is None) for c in closed] == [("cut", True), ("ok", False)]
    assert rows_of(k, run.id)["run"]["outcome"] == "stopped"
    assert [m["text"] for m in rows_of(k, run.id)["messages"] if m["role"] == "assistant"] == ["Back to you."]


def test_the_note_store_reads_and_writes_rows_and_folds_ascii_case(tmp_path, kkernel_stub):
    notes = Store(client(tmp_path)).notes("clerk")
    assert notes.version == 0 and notes.get("plan") is None and notes.delete("plan") is None
    first = notes.put("plan", {"port": 8080}, run="r1", seq=3)
    assert (first.key, first.value, first.run, first.seq, first.version) == (
        "plan",
        {"port": 8080},
        "r1",
        3,
        1,
    )
    assert notes.put("plan", "Straße", run="r2", seq=5).version == 2
    notes.put("plan.b", [1, 2], run="r2", seq=6)
    assert [n.key for n in notes.list()] == ["plan", "plan.b"] and [n.key for n in notes.list("plan.")] == [
        "plan.b"
    ]
    assert [n.key for n in notes.find("STRASSE")] == []  # the pack folds ASCII case only (ADR-0007 D6)
    assert [n.key for n in notes.find("STRA")] == ["plan"] and [n.key for n in notes.find("PLAN")] == [
        "plan",
        "plan.b",
    ]
    with pytest.raises(ValueError, match="blank"):
        notes.find("  ")
    assert notes.version > 0
    assert notes.delete("plan").version == 2 and notes.get("plan") is None
    other = Store(client(tmp_path)).notes("other")
    assert other.list() == []  # another profile's rows


def test_the_store_refuses_as_the_pack_does_and_the_client_names_the_kind(tmp_path, kkernel_stub):
    k = client(tmp_path)
    with pytest.raises(KhiveError, match="not found") as e:
        k.exec_sync('lion.get(table="runs", id="nope")')
    assert e.value.kind == "not_found"
    with pytest.raises(KhiveError, match="source 'elsewhere'"):  # the first op's own refusal, named
        k.exec_sync('[lion.session_open(source="elsewhere"), lion.get(table="runs", id="x")]')
    with pytest.raises(KhiveError, match="serves lion"):
        k.exec_sync("comm.inbox(limit=1)")


def test_exec_sync_times_out_and_refuses_output_it_cannot_parse(tmp_path):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config(ACTOR, tmp_path))
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    with pytest.raises(KhiveError, match="did not answer within 0.2s"):
        Khive(tmp_path, ACTOR, kkernel=str(slow), timeout=0.2).exec_sync("lion.runs(limit=1)")
    noisy = tmp_path / "noisy"
    noisy.write_text("#!/bin/sh\necho 'not json'\n")
    noisy.chmod(0o755)
    with pytest.raises(KhiveError, match="unparseable kkernel output"):
        Khive(tmp_path, ACTOR, kkernel=str(noisy)).exec_sync("lion.runs(limit=1)")


def test_replay_skips_a_message_row_that_is_not_an_entry_and_a_refusal_other_than_not_found_is_raised(
    tmp_path, kkernel_stub
):
    k = client(tmp_path)
    store = Store(k)
    _, run = a_run(store)
    odd = json.dumps(
        {"kind": "USAGE", "content": {}, "seq": 99}
    )  # a log row of another kind, as `entry_of` reads them
    k.exec_sync(
        f'lion.message_append(session_id="{run.id}", seq=99, role="tool", kind="result", text="x", '
        f"raw={json.dumps(odd)})"
    )
    history = asyncio.run(store.history(run.id))
    assert len(history) == len(run.record)  # the odd row is skipped, not raised
    notes = store.notes("clerk")

    def refusing(ops):
        raise KhiveError("kkernel exited 1: permission denied", kind="permission")

    k.exec_sync = refusing
    with pytest.raises(KhiveError, match="permission denied"):
        notes.get("plan")
    with pytest.raises(KhiveError, match="permission denied"):
        notes.delete("plan")


def test_a_history_refuses_a_chain_with_a_run_the_store_does_not_hold_or_one_that_loops(store):
    def opened(run: str, continues: str) -> None:  # the ops `Store.open` sends, the chain written by hand
        store.khive.exec_sync(
            f'lion.session_open(id="{run}", source="lion1", actor="a", profile="p") | '
            f'lion.run_open(id="{run}", profile="p", session_id="{run}", actor="a", bounds="{{}}", '
            f'trigger=null, continues="{continues}")'
        )

    opened("after-gone", "r-gone")
    with pytest.raises(KhiveError) as gone:  # the missing run is one link back, not the one named
        asyncio.run(store.history("after-gone"))
    assert gone.value.kind == "not_found" and "r-gone" in str(gone.value)
    opened("loop-a", "loop-b")
    opened("loop-b", "loop-a")
    with pytest.raises(KhiveError, match="run loop-a continues itself through loop-a <- loop-b") as loop:
        asyncio.run(store.history("loop-a"))
    assert loop.value.kind == "invalid"


def test_a_row_json_cannot_carry_whole_is_written_with_each_part_it_cannot_carry_as_its_str():
    ring: list = [1]
    ring.append(ring)
    nan, inf = float("nan"), float("inf")
    value = {(1, 2): "x", 3: nan, None: [ring, (inf, 2.5)], True: "t", 1.5: "f", nan: "k"}
    assert row_json(value) == (
        '{"(1, 2)": "x", "3": NaN, "null": [[1, "[1, [...]]"], [Infinity, 2.5]], "true": "t", "1.5": "f", '
        '"NaN": "k"}'
    )  # the key and the value that holds itself as their str; a float that is not finite as the log writes it
    assert row_json({"a": [1, (2, 3)]}) == json.dumps({"a": [1, [2, 3]]})  # what JSON carries whole, as it is


class Pairs(BaseModel):
    pass


def test_a_replay_reads_a_float_that_is_not_finite_as_that_float_and_a_key_json_cannot_hold_as_its_str():
    """A result's value reads back in the form the row wrote it (ADR-0019/D1): NaN and infinity as the
    floats they were, as the log wrote them before the store; a tuple key as its str."""
    store = Store(LionKhive())
    actor = Actor("clerk", store=store)

    @actor.handler(Pairs)
    def pairs(req, ctx) -> dict:
        return {(1, 2): "x", "nan": float("nan"), "inf": [float("inf")]}

    async def backend(messages):
        return "<lact pair>pairs()</lact>"

    run = asyncio.run(actor.run(Profile("p"), RunRequest(inputs=("go",), max_turns=1), backend))
    (got,) = [e.content.value for e in asyncio.run(store.history(run.id)) if e.alias == "pair"]
    assert got["(1, 2)"] == "x" and math.isnan(got["nan"]) and got["inf"] == [math.inf]


def test_an_actor_without_a_store_is_refused_at_construction():
    with pytest.raises(TypeError, match="built with a store"):
        Actor("bare", store=None)
    with pytest.raises(TypeError):
        Actor("bare")  # the keyword is required, not defaulted


def test_the_fake_parses_the_dsl_the_client_writes():
    assert parse('lion.get(table="runs", id="a, b)")') == [("lion.get", {"table": "runs", "id": "a, b)"})]
    assert [v for v, _ in parse("[lion.runs(limit=1), lion.runs(limit=2)]")] == ["lion.runs", "lion.runs"]
    assert [a["limit"] for _, a in parse("lion.runs(limit=1) | lion.runs(limit=2)")] == [1, 2]
    fake = LionFake()
    with pytest.raises(KhiveError, match="serves lion"):
        fake.answer('kg.create(name="x")')
    text = fake.to_json()
    assert LionFake.from_json(text).to_json() == text


def _daemon_pids(binary: str) -> dict[int, str | None]:
    # the daemon and the events child it spawns, which keeps the store as its directory: a daemon that
    # died without its shutdown leaves the child running, holding the store's events file. Each pid
    # comes with the events file its child's `--db` names ("" when the line names none), None for a daemon
    rows = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=True).stdout
    found: dict[int, str | None] = {}
    for pid, _, command in (row.strip().partition(" ") for row in rows.splitlines()):
        args = command.split()
        if command.strip().startswith(f"{binary} mcp --daemon"):
            found[int(pid)] = None
        elif command.strip().startswith(f"{binary} events-daemon"):
            found[int(pid)] = args[args.index("--db") + 1] if "--db" in args[:-1] else ""
    return found


def _serves(db: str | None, cwd: str, where: Path) -> bool:
    """A daemon works for the store it runs in; an events child also names its file, which must be in it:
    a child in the same directory serving another store's file is not this store's to stop."""
    if db is None:
        return True
    store = Path(os.path.realpath(where))
    return bool(db) and Path(os.path.realpath(os.path.join(cwd, db))).is_relative_to(store)


def _daemons(binary: str, where: Path) -> list[int]:
    """The pids of `binary`'s daemons working in `where`, read off the process table."""
    pids = _daemon_pids(binary)
    if not pids:
        return []
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    looked = subprocess.run(
        [lsof, "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, sorted(pids)))],
        capture_output=True,
        text=True,
    )
    found, read, pid = [], set(), None
    for line in looked.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n"):
            read.add(pid)
            cwd = line[1:]
            if os.path.realpath(cwd) == os.path.realpath(where) and _serves(pids[pid], cwd, where):
                found.append(pid)
    # lsof exits 1 when a pid it was given is gone before it looks, so its code cannot tell an exit from
    # a failed look; the table can. A daemon still listed whose directory went unread fails here: read as
    # working elsewhere, it would let the caller remove a directory a live daemon holds
    missed = pids.keys() - read
    unread = sorted(missed & _daemon_pids(binary).keys()) if missed else []
    assert not unread, (
        f"lsof read no directory for daemons {unread} (exit {looked.returncode}): {looked.stderr}"
    )
    return found


def _stop(binary: str, where: Path) -> None:
    # SIGTERM is the daemon's one stop path: it drains, removes its socket and exits. A pid is signalled
    # only as the process table names it, read just before, and the wait reads the table again rather
    # than probing the pid: a daemon that exited and left its number to another process is neither
    # signalled nor waited on
    for pid in _daemons(binary, where):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 30
    while left := _daemons(binary, where):
        assert time.monotonic() < deadline, f"daemons {left} still running 30 s after SIGTERM"
        time.sleep(0.2)


def _short_dirs() -> tuple[Path, Path]:
    # both directories short, under /tmp: under a pytest tmp_path (170 characters and more) the binary
    # creates the lion file but not the main one, and every op fails its audit obligation (measured);
    # the home is the test's own, so a stray default would resolve there and never in the real one
    home = Path(tempfile.mkdtemp(prefix="lion1-home-", dir="/tmp"))
    try:
        return home, Path(tempfile.mkdtemp(prefix="lion1-store-", dir="/tmp"))
    except BaseException:  # a fixture runs nothing after `yield` when its setup raises: the first goes here
        shutil.rmtree(home)
        raise


@pytest.fixture
def binary_dirs():
    home, store = _short_dirs()
    yield home, store
    # `exec` starts a daemon and its events child in the store's directory and nothing stops them:
    # stopped before the directories go, a failed run's included, or each run leaves them holding
    # deleted files
    _stop(os.environ["LION1_KHIVE"], store)
    shutil.rmtree(home)
    shutil.rmtree(store)


def test_the_short_dirs_leave_nothing_behind_when_the_second_cannot_be_made(monkeypatch):
    made = []
    real = tempfile.mkdtemp

    def mkdtemp(**kw):
        if made:
            raise OSError("no room")
        made.append(Path(real(**kw)))
        return str(made[-1])

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    with pytest.raises(OSError, match="no room"):
        _short_dirs()
    assert len(made) == 1 and not made[0].exists()


@pytest.mark.parametrize(
    ("tables", "signalled"),
    [
        # the daemon exited and its pid went to another process: the table names nothing
        ([[], []], []),
        # running, then gone after the signal: one SIGTERM, and the wait never probes the pid
        ([[41], [41], []], [(41, signal.SIGTERM)]),
    ],
)
def test_stop_signals_and_waits_on_only_what_the_process_table_names(monkeypatch, tables, signalled):
    sent, reads = [], iter(tables)
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(f"{__name__}._daemons", lambda binary, where: next(reads))
    _stop("lion1-khive", Path("/x"))
    assert sent == signalled


DAEMON = "41 lion1-khive mcp --daemon\n"
EVENTS = "52 lion1-khive events-daemon --db /x/.khive/khive.db.events.db\n"


@pytest.mark.parametrize(
    ("table", "lsof", "table_after", "expect", "reads"),
    [
        # lsof names the daemon's directory: it is found, and the table is not read again
        (DAEMON, (0, "p41\nn/x\n"), None, [41], 1),
        # an events child whose daemon died without its shutdown: found by its directory, as the daemon is
        (EVENTS, (0, "p52\nn/x\n"), None, [52], 1),
        # its events file named relative to its directory: the same store
        (EVENTS.replace("/x/.khive", ".khive"), (0, "p52\nn/x\n"), None, [52], 1),
        # a child in the same directory serving another store's events file, or naming none: not this store's
        (EVENTS.replace("/x/", "/y/"), (0, "p52\nn/x\n"), None, [], 1),
        (EVENTS.replace(" --db /x/.khive/khive.db.events.db", ""), (0, "p52\nn/x\n"), None, [], 1),
        # gone before lsof looked: lsof exits 1 with no row, and the table, read again, no longer lists it
        (DAEMON, (1, ""), "", [], 2),
        # still listed and its directory went unread: refused, never read as working elsewhere
        (DAEMON, (1, ""), DAEMON, r"lsof read no directory for daemons \[41\] \(exit 1\)", 2),
    ],
)
def test_daemons_reads_an_unread_directory_as_an_exit_only_when_the_table_agrees(
    monkeypatch, table, lsof, table_after, expect, reads
):
    tables, read = iter([table, table_after]), []

    def run(argv, **_):
        if argv[0] == "ps":
            read.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=next(tables), stderr="")
        return subprocess.CompletedProcess(argv, lsof[0], stdout=lsof[1], stderr="lsof: permission denied")

    monkeypatch.setattr(subprocess, "run", run)
    if isinstance(expect, str):
        with pytest.raises(AssertionError, match=expect):
            _daemons("lion1-khive", Path("/x"))
    else:
        assert _daemons("lion1-khive", Path("/x")) == expect
    assert len(read) == reads


@pytest.mark.skipif(not os.environ.get("LION1_KHIVE"), reason="LION1_KHIVE names no built binary")
def test_the_same_run_against_the_built_binary(binary_dirs, monkeypatch):
    # the config routes the lion pack's tables to their own file beside a main store it also names
    # (ADR-0026 C8; the binary's default, with no backends declared, is the one under the home
    # directory): the test then proves the lion file is the one written, and the main one holds none
    binary = os.environ["LION1_KHIVE"]
    monkeypatch.delenv("KHIVE_DB", raising=False)  # a single-file database would move every pack at once
    monkeypatch.delenv("KHIVE_NO_DAEMON")  # the daemon `exec` starts is part of what runs here
    home, tmp_path = binary_dirs
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / ".khive").mkdir()
    cfg = tmp_path / ".khive" / "config.toml"
    cfg.write_text(routed_config(ACTOR, tmp_path, packs=("kg", "lion")))
    main, db = tmp_path / ".khive" / "main.db", tmp_path / ".khive" / "lion.db"
    k = Khive(tmp_path, ACTOR, kkernel=binary)
    assert Store(k).database == db
    # the binary names every target it resolved on every call: both this test named, no other
    said = subprocess.run(
        [binary, "exec", "lion.runs(limit=1)"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stderr
    assert f"database: config-declared backends govern storage targets: main={main}, lion={db}" in said, said
    _, run = a_run(Store(k), trigger=("mail-1",))
    check_rows(k, run)
    _, again = a_run(Store(k), history=asyncio.run(Store(k).history(run.id)), continues=run.id)
    assert rows_of(k, again.id)["run"]["continues"] == run.id
    notes = Store(k).notes("clerk")  # every kind the pack writes lands in that file: the notes too
    notes.put("plan", {"step": 1}, run=run.id, seq=1)
    assert notes.get("plan").value == {"step": 1} and db.stat().st_size > 0
    assert not (home / ".khive" / "khive.db").exists()
    # the rows are in the lion file and not in the main one: each opened alone as the main store
    for path, count in ((db, 2), (main, 0)):
        cfg.write_text(
            f'[actor]\nid = "{ACTOR}"\n\n[runtime]\npacks = ["kg", "lion"]\n\n[[backends]]\nname = "main"\n'
            f"path = {json.dumps(str(path))}\n"
        )
        got = subprocess.run(
            [binary, "exec", "lion.runs(limit=10)"], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout
        assert json.loads(got)["results"][0]["result"]["count"] == count, (path, got)
    # the run went through the daemon `exec` starts, and once stopped none is left in its directory
    assert _daemons(binary, tmp_path)
    _stop(binary, tmp_path)
    assert _daemons(binary, tmp_path) == []


@pytest.mark.skipif(not os.environ.get("LION1_KHIVE"), reason="LION1_KHIVE names no built binary")
def test_a_harness_session_rides_the_same_tables_against_the_built_binary(binary_dirs, monkeypatch):
    # ADR-0025 C4: a Claude Code or Codex session is a session in the store's own tables, each exchange
    # a message row. The checkpoint's own functions write and read them against the real binary, not the
    # stub, so the Rust producer and the Python consumer are proven to agree on the row shapes.
    from hub.backend.checkpoint import exchanges_of, latest_session, record_exchanges, session_of

    binary = os.environ["LION1_KHIVE"]
    monkeypatch.delenv("KHIVE_DB", raising=False)
    monkeypatch.delenv("KHIVE_NO_DAEMON")
    home, tmp_path = binary_dirs
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config(ACTOR, tmp_path, packs=("kg", "lion")))
    k = Khive(tmp_path, ACTOR, kkernel=binary)
    sid = "cc-built"
    exchanges = [
        {"turn": 0, "at": "t0", "input": "hello", "text": "hi", "kind": "prompt"},
        {"turn": 1, "at": "t1", "input": "again", "text": "ok", "kind": "prompt"},
    ]
    kw = {
        "source": "claude-code",
        "session_id": sid,
        "actor": ACTOR,
        "transcript": "/t/cc.jsonl",
        "why": "manual",
    }
    assert asyncio.run(session_of(k, sid)) is None
    assert asyncio.run(record_exchanges(k, exchanges=exchanges, **kw)) == (2, 2)
    # a second take of the session at the same count writes nothing
    assert asyncio.run(record_exchanges(k, exchanges=exchanges, **kw)) == (2, 0)
    row = asyncio.run(latest_session(k, ACTOR))
    assert (row["id"], row["source"], row["transcript"]) == (sid, "claude_code", "/t/cc.jsonl")
    assert asyncio.run(exchanges_of(k, sid)) == [{**e, "why": "manual"} for e in exchanges]
    _stop(binary, tmp_path)


@pytest.mark.skipif(not os.environ.get("LION1_KHIVE"), reason="LION1_KHIVE names no built binary")
def test_a_flows_step_rows_against_the_built_binary(binary_dirs, monkeypatch):
    # ADR-0026 C2 on the real pack: each step's row written by `run_open` as the driver hands it, closed
    # with its run, and the rows read back by `steps`, `result` and `messages`
    from contextlib import asynccontextmanager

    from hub.flow import Flow, Step, run_flow

    binary = os.environ["LION1_KHIVE"]
    monkeypatch.delenv("KHIVE_DB", raising=False)
    monkeypatch.delenv("KHIVE_NO_DAEMON")
    home, tmp_path = binary_dirs
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config(ACTOR, tmp_path, packs=("kg", "lion")))
    store = Store(Khive(tmp_path, ACTOR, kkernel=binary))

    @asynccontextmanager
    async def cast(name):
        yield Actor(ACTOR, store=store), Profile(name), scripted('OUT{result: "done"}')

    flow = Flow(
        "two",
        (
            Step("plan", "writer", "plan {topic}"),
            Step("fan", "writer", "do {item} after {plan.text}", "items"),
        ),
        "fan",
    )
    ended = asyncio.run(run_flow(store, flow, {"topic": "tea", "items": '["a", "b"]'}, cast))
    assert ended.outcome == "success" and ended.output == [{"result": "done"}, {"result": "done"}]
    steps = asyncio.run(store.steps(ended.id))
    assert [
        (s["idx"], s["name"], s["profile"], s["outcome"], json.loads(s["inputs_from"])) for s in steps
    ] == [
        (0, "plan", "writer", "success", []),
        (1, "fan[0]", "writer", "success", ["plan"]),
        (2, "fan[1]", "writer", "success", ["plan"]),
    ]
    for s in steps:
        row = asyncio.run(store.result(s["run_id"]))
        assert row["outcome"] == "success" and json.loads(row["trigger"]) == [ended.id]
    texts = [m["text"] for m in asyncio.run(store.messages(steps[2]["run_id"])) if m["role"] == "user"]
    assert texts == ['do b after OUT{result: "done"}']
    flow_row = asyncio.run(store.result(ended.id))
    assert (flow_row["profile"], flow_row["outcome"], flow_row["turns"]) == ("flow:two", "success", 0)
    assert json.loads(flow_row["output"]) == [{"result": "done"}, {"result": "done"}]
    _stop(binary, tmp_path)
