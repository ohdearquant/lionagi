"""The served actor (ADR-0021): one service over an actor's offers and the runs it started, each read
as rows, bus events and its state, spoken to and ended from outside. The HTTP transport over it is
tests/test_serve_http.py."""

import ast
import asyncio
import json
import os
import re
import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel

import hub.serve.service as serve
import lion1.actor as actor_module
from hub.serve.service import Offer, ServeError, Service
from lion1 import Actor, Kind, Profile, Reply, RunRequest, render_guidance
from lion1.actor import ProfileError
from lion1.checkpoint import entry_row, load_log
from lion1.record import Entry
from tests.lionfake import store as fresh_store

ROOT = Path(__file__).resolve().parents[1]
FINISH = 'OUT{answer: {text: "done"}}'


class Answer(BaseModel):
    text: str


class Echo(BaseModel):
    """Say a text back."""

    text: str


class Wait(BaseModel):
    """Wait until the test lets the command through."""

    key: str


def bounded(main, seconds: float = 10.0):
    """`main()` on a fresh loop under a bound; no task it started outlives it."""

    async def outer():
        result = await asyncio.wait_for(main(), seconds)
        await asyncio.sleep(0)  # a done callback the last step queued runs
        left = asyncio.all_tasks() - {asyncio.current_task()}
        assert not left, left
        return result

    return asyncio.run(outer())


def turns(*texts: str, calls: list | None = None):
    it = iter(texts)

    async def backend(messages):
        if calls is not None:
            calls.append(messages)
        return next(it)

    return backend


class Held:
    """A backend whose every call waits for the test to answer it."""

    def __init__(self):
        self.asked: asyncio.Queue = asyncio.Queue()
        self.calls = 0

    async def __call__(self, messages):
        self.calls += 1
        answer = asyncio.get_running_loop().create_future()
        await self.asked.put(answer)
        return await answer

    async def answer(self, text: str) -> None:
        (await self.asked.get()).set_result(text)

    async def next(self) -> asyncio.Future:
        """The next call, once it is in flight: the future its answer goes on."""
        return await self.asked.get()


def actor(name: str) -> tuple[Actor, dict[str, asyncio.Event]]:
    """An actor with `echo` and `wait`; `wait(key=k)` holds until the test sets `gates[k]`."""
    a = Actor(name, store=fresh_store())
    gates: dict[str, asyncio.Event] = {}

    @a.handler(Echo, effect="pure")
    async def echo(req: Echo, ctx) -> str:
        return req.text

    @a.handler(Wait)
    async def wait(req: Wait, ctx) -> str:
        await gates.setdefault(req.key, asyncio.Event()).wait()
        return req.key

    return a, gates


def runs_of(a: Actor) -> list:
    """Each run of `a` as it starts, read off the bus as a client cannot: the record to compare with."""
    seen: list = []
    a.bus.subscribe("run.started", lambda ev: seen.append(a.runs[ev.payload["run"]]))
    return seen


def plain(e: Entry) -> dict:
    """An entry as a published row reads: its log row through JSON."""
    return json.loads(json.dumps(serve._jsonable(entry_row(e))))


async def to_end(sub) -> list[tuple]:
    """Every frame of a stream to its close."""
    return [frame async for frame in sub]


def queued(sub) -> list[tuple]:
    """The frames a stream holds that its reader has not taken, taken now."""
    out = []
    while sub._frames:
        out.append(sub._frames.popleft())
    return out


def log_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- D1, C12, C13 -----------------------------------------------------------------------------------


class Lookup(BaseModel):
    """Find a word in the glossary.

    The second line is the guidance's too, never the card's."""

    word: str


class Ping(BaseModel):
    n: int


def test_the_card_lists_each_command_as_the_model_is_told_it_and_nothing_of_the_prompt(store):
    a = Actor("serve-card", privileges={"glossary"}, store=store)

    @a.handler(Lookup, requires={"glossary"}, effect="read")
    async def lookup(req: Lookup, ctx) -> str:
        return req.word

    @a.handler(Ping, effect="pure")
    async def ping(req: Ping, ctx) -> int:
        return req.n

    service = Service(a)
    reader = Profile(
        "reader",
        specs=frozenset({"lookup", "ping", "context.hide"}),
        privileges=frozenset({"glossary"}),
        system="THE ROLE PROMPT, never shown to a client",
        defaults={"max_turns": 7},
    )
    service.offer(reader, turns(), RunRequest(emits=(Answer,), time_budget=30.0), converse=True)
    service.offer(Profile("everything", language="program"), turns())
    card = service.card()
    assert card["actor"] == "serve-card" and card["protocol"] == 1
    first, second = card["offers"]
    subset = a.subset(reader)
    assert [c["name"] for c in first["commands"]] == list(subset) == ["context.hide", "lookup", "ping"]
    for command in first["commands"]:
        spec = subset.get(command["name"])
        told = render_guidance(spec).splitlines()[0]  # the line the model reads for this command
        assert told == (command["name"] if command["doc"] is None else f"{command['name']}: {command['doc']}")
        assert command["schema"] == spec.model_json_schema()
        assert command["effect"] == a.handlers[command["name"]].effect
        assert command["privileges"] == sorted(a.handlers[command["name"]].requires)
    assert first["commands"][1] == {
        "name": "lookup",
        "doc": "Find a word in the glossary.",
        "schema": Lookup.model_json_schema(),
        "effect": "read",
        "privileges": ["glossary"],
    }
    assert first["commands"][2]["doc"] is None
    assert first["outputs"] == [{"name": "answer", "schema": Answer.model_json_schema()}]
    assert first["bounds"] == {
        "max_turns": 7,
        "max_idle": 3,
        "time_budget": 30.0,
        "cost_budget": None,
        "context_budget": 200_000,
        "max_refusals": 3,
    }
    assert (first["name"], first["language"], first["converse"]) == ("reader", "core", True)
    assert (second["name"], second["language"], second["converse"]) == ("everything", "program", False)
    assert [c["name"] for c in second["commands"]] == list(a.subset(Profile("everything")))
    assert second["outputs"] == [] and second["bounds"]["max_turns"] == 100
    assert "THE ROLE PROMPT" not in json.dumps(card)


def test_an_offer_is_fixed_when_made_and_one_the_service_cannot_serve_is_refused_by_name():
    a, _ = actor("serve-offer")
    service = Service(a)
    defaults = {"max_turns": 5}
    template = RunRequest(max_idle=2)
    offer = service.offer(Profile("p", defaults=defaults), turns(), template)
    assert isinstance(offer, Offer) and offer.name == "p" and service.offers == {"p": offer}
    defaults["max_turns"] = 50  # the caller's dict, changed after the offer was made
    assert offer.profile.defaults == {"max_turns": 5} and offer.request == template
    assert offer.request is not template
    assert service.card()["offers"][0]["bounds"]["max_turns"] == 5

    async def wait(run):
        return False

    with pytest.raises(ValueError, match="'q': the template sets wait"):
        service.offer(Profile("q"), turns(), RunRequest(wait=wait))
    with pytest.raises(ValueError, match="'q': the template sets reply_on_commands"):
        service.offer(Profile("q"), turns(), RunRequest(reply_on_commands=True))
    with pytest.raises(ValueError, match="'p' is already offered"):
        service.offer(Profile("p"), turns())
    with pytest.raises(ProfileError, match="no handler for"):
        service.offer(Profile("q", specs=frozenset({"nothing"})), turns())
    assert list(service.offers) == ["p"]


def _imports(path: Path) -> set[str]:
    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add("." if node.level else node.module)
    return imported


def test_the_core_serves_nothing_and_the_transport_is_the_serve_extra():
    # C12: the core neither serves nor gains a dependency; FastAPI and uvicorn are the `serve` extra, and
    # only the transport imports them, the service knowing no HTTP
    core = set().union(*(_imports(p) for p in (ROOT / "src" / "lion1").rglob("*.py")))
    assert {"asyncio", "pydantic"} <= core  # the walk reads imports at all
    assert not {m for m in core if m.split(".")[0] in ("fastapi", "starlette", "uvicorn", "hub")}
    assert not (ROOT / "src" / "lion1" / "serve.py").exists() and not hasattr(Actor, "serve")
    service = {m.split(".")[0] for m in _imports(ROOT / "hub" / "serve" / "service.py")}
    assert "lion1" in service and not service & {"fastapi", "starlette", "uvicorn", "http"}
    transport = {m.split(".")[0] for m in _imports(ROOT / "hub" / "serve" / "transport.py")}
    assert {"fastapi", "uvicorn"} <= transport
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == ["pydantic>=2.11"]
    assert project["optional-dependencies"]["serve"] == ["fastapi>=0.115", "uvicorn>=0.30"]


# --- C1, C2, D2, D13 --------------------------------------------------------------------------------

SCRIPT = (
    '<lact a1>echo(text="one")</lact><lact a2>echo(text="two")</lact>',
    "Both echoed; finishing.",
    'OUT{answer: {text: "one, two"}}',
)


def test_a_served_run_records_what_actor_run_records_for_the_same_request():
    direct, _ = actor("serve-parity-direct")
    served, _ = actor("serve-parity-served")
    request = RunRequest(inputs=("say one and two",), emits=(Answer,), max_turns=5)
    alone = asyncio.run(direct.run(Profile("p"), request, turns(*SCRIPT)))
    service = Service(served)
    service.offer(Profile("p"), turns(*SCRIPT), RunRequest(emits=(Answer,), max_turns=5))

    async def main():
        state, created = await service.start({"offer": "p", "inputs": ["say one and two"]})
        final = (await to_end(service.events(state["id"])))[-1][1]
        return state, created, final, service.record(state["id"])

    state, created, final, record = bounded(main)
    assert created and state["phase"] == "working" and state["offer"] == "p"
    assert re.fullmatch(r"[0-9a-f]{16}", state["id"]) and state["run"] != state["id"]

    def clockless(row: dict) -> dict:  # the clock is a reading, not the run
        return json.loads(re.sub(r"time=\d+s", "time=Ns", json.dumps(row)))

    assert [clockless(r) for r in record["rows"]] == [clockless(plain(e)) for e in alone.record]
    assert record["more"] is False and final["rows"] == len(alone.record)
    assert final["outcome"] == {"kind": "Success"} and final["output"] == {"answer": {"text": "one, two"}}
    assert (final["phase"], final["turn"], final["pending"], final["error"]) == ("ended", 3, [], None)


BAD_STARTS = [
    ("not an object", "bad_request"),
    ({}, "bad_request"),
    ({"offer": 1}, "bad_request"),
    ({"offer": "p", "prompt": "be someone else"}, "bad_request"),
    ({"offer": "p", "system": "x"}, "bad_request"),
    ({"offer": "p", "inputs": "go"}, "bad_request"),
    ({"offer": "p", "inputs": None}, "bad_request"),
    ({"offer": "p", "inputs": [""]}, "bad_request"),
    ({"offer": "p", "inputs": [1]}, "bad_request"),
    ({"offer": "p", "inputs": ["x"] * 17}, "bad_request"),
    ({"offer": "p", "inputs": ["\ud800"]}, "bad_request"),
    ({"offer": "p", "inputs": ["x" * (1 << 20) + "y"]}, "too_large"),
    ({"offer": "p", "inputs": ["é" * (1 << 19) + "y"]}, "too_large"),
    ({"offer": "p", "bounds": []}, "bad_request"),
    ({"offer": "p", "bounds": {"view_budget": 5}}, "bad_request"),
    ({"offer": "p", "bounds": {"max_turns": 0}}, "bad_request"),
    ({"offer": "p", "bounds": {"max_turns": True}}, "bad_request"),
    ({"offer": "p", "bounds": {"max_idle": 2.0}}, "bad_request"),
    ({"offer": "p", "bounds": {"context_budget": "9"}}, "bad_request"),
    ({"offer": "p", "bounds": {"max_refusals": -1}}, "bad_request"),
    ({"offer": "p", "bounds": {"time_budget": True}}, "bad_request"),
    ({"offer": "p", "bounds": {"time_budget": 0}}, "bad_request"),
    ({"offer": "p", "bounds": {"time_budget": -1.5}}, "bad_request"),
    ({"offer": "p", "bounds": {"time_budget": float("inf")}}, "bad_request"),
    ({"offer": "p", "bounds": {"cost_budget": float("nan")}}, "bad_request"),
    ({"offer": "p", "bounds": {"cost_budget": 10**400}}, "bad_request"),
    ({"offer": "p", "bounds": {"cost_budget": "1"}}, "bad_request"),
    ({"offer": "p", "bounds": {"max_turns": 101}}, "bad_request"),  # above the offer's 100
    ({"offer": "p", "bounds": {"max_idle": 4}}, "bad_request"),  # above the offer's 3
    ({"offer": "p", "resume": None}, "bad_request"),
    ({"offer": "p", "resume": "../../etc/passwd"}, "bad_request"),
    ({"offer": "p", "resume": "0123456789ABCDEF"}, "bad_request"),
    ({"offer": "p", "resume": "0123456789abcde"}, "bad_request"),
    ({"offer": "p", "once": None}, "bad_request"),
    ({"offer": "p", "once": ""}, "bad_request"),
    ({"offer": "p", "once": "x" * 65}, "bad_request"),
    ({"offer": "p", "once": "a key"}, "bad_request"),
    ({"offer": "p", "once": 7}, "bad_request"),
    ({"offer": "q"}, "unknown_offer"),
]


@pytest.mark.parametrize("fields, code", BAD_STARTS)
def test_a_start_outside_the_table_is_refused_whole_before_an_id_or_a_log_is_reserved(fields, code, tmp_path):
    a, _ = actor("serve-refused")
    service = Service(a, dir=tmp_path)
    calls: list = []
    service.offer(Profile("p"), turns(FINISH, calls=calls), RunRequest(emits=(Answer,)))

    async def main():
        with pytest.raises(ServeError) as refused:
            await service.start(fields)
        return refused.value

    refusal = bounded(main)
    assert refusal.code == code and refusal.reason
    assert sorted(tmp_path.iterdir()) == [] and service.runs == {} and calls == [] and a.runs == {}


def test_a_start_at_or_below_the_offers_bounds_runs_there_and_a_null_bound_takes_the_offers():
    a, _ = actor("serve-bounds")
    service = Service(a)
    service.offer(Profile("p"), turns("idle", "idle", "idle", FINISH), RunRequest(emits=(Answer,)))
    service.offer(Profile("timed"), turns(FINISH), RunRequest(emits=(Answer,), max_turns=3))
    seen = runs_of(a)

    async def main():
        low = {
            "max_turns": 2,
            "max_idle": 3,
            "time_budget": 10**300,
            "cost_budget": 0.5,
            "context_budget": None,
        }
        first, _ = await service.start({"offer": "p", "inputs": ["go"], "bounds": low})
        end = (await to_end(service.events(first["id"])))[-1][1]
        second, _ = await service.start({"offer": "timed", "bounds": {"max_turns": 3, "time_budget": 2}})
        await to_end(service.events(second["id"]))
        return end

    end = bounded(main)
    assert seen[0].bounds == {
        "max_turns": 2,
        "max_idle": 3,
        "time_budget": 10**300,
        "cost_budget": 0.5,
        "context_budget": 200_000,
        "max_refusals": 3,
    }
    assert end["outcome"] == {"kind": "Exhausted", "turns": 2, "reason": "turns"}
    assert seen[1].bounds["max_turns"] == 3 and seen[1].bounds["time_budget"] == 2


def test_a_start_past_the_live_count_is_refused_and_the_next_after_an_end_is_not():
    a, _ = actor("serve-live")
    service = Service(a, max_live=1)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))

    async def main():
        first, _ = await service.start({"offer": "p", "inputs": ["one"]})
        with pytest.raises(ServeError) as refused:
            await service.start({"offer": "p", "inputs": ["two"]})
        await backend.answer(FINISH)
        await to_end(service.events(first["id"]))
        second, _ = await service.start({"offer": "p", "inputs": ["three"]})
        await backend.answer(FINISH)
        await to_end(service.events(second["id"]))
        return refused.value

    refusal = bounded(main)
    assert refusal.code == "too_many" and "1 runs are live" in refusal.reason
    assert [s["phase"] for s in service.runs_state()["runs"]] == ["ended", "ended"]


class _Planned:
    """A backend whose `plan` cannot be read: `Actor.run` raises before `run.started`."""

    @property
    def plan(self):
        raise RuntimeError("no plan today")

    async def __call__(self, messages):  # never called
        raise AssertionError("called")


@pytest.mark.parametrize("with_dir", [True, False])
def test_a_start_actor_run_refuses_is_refused_with_its_reason_and_leaves_no_run_log_or_key(
    tmp_path, with_dir
):
    a, _ = actor("serve-run-refused")
    service = Service(a, dir=tmp_path if with_dir else None)
    service.offer(Profile("p"), _Planned())
    unreplayable = RunRequest(history=(Entry(0, Kind.TEXT, "x", alias="_mine"),))
    service.offer(Profile("q"), turns(FINISH), unreplayable)
    every = service.events()

    async def main():
        fields = {"offer": "p", "once": "k"}
        first, repeat = await asyncio.gather(
            service.start(fields), service.start(fields), return_exceptions=True
        )
        with pytest.raises(ServeError) as again:  # the key was released with the refusal
            await service.start(fields)
        with pytest.raises(ServeError) as history:
            await service.start({"offer": "q"})
        return first, repeat, again.value, history.value

    first, repeat, again, history = bounded(main)
    assert first.code == repeat.code == again.code == "refused"
    # the repeat in flight waited for it
    assert first.reason == repeat.reason == "RuntimeError: no plan today"
    assert history.code == "refused" and history.reason.startswith("ValueError: alias '_mine' starts with _")
    assert sorted(tmp_path.iterdir()) == [] and service.runs == {} and a.runs == {}
    assert queued(every) == [("runs", {"runs": []})]  # a refused start never appears on the stream
    every.close()


def test_two_starts_in_one_loop_turn_reserve_two_ids_and_two_logs(tmp_path):
    a, _ = actor("serve-two")
    service = Service(a, dir=tmp_path)
    service.offer(Profile("p"), turns(FINISH, FINISH), RunRequest(emits=(Answer,)))

    async def main():
        (one, made_one), (two, made_two) = await asyncio.gather(
            service.start({"offer": "p", "inputs": ["one"]}), service.start({"offer": "p", "inputs": ["two"]})
        )
        for state in (one, two):
            await to_end(service.events(state["id"]))
        return one, two, made_one and made_two

    one, two, made = bounded(main)
    assert made and one["id"] != two["id"] and one["run"] != two["run"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(f"{s['id']}.jsonl" for s in (one, two))
    for state, said in ((one, "one"), (two, "two")):
        lines = log_lines(tmp_path / f"{state['id']}.jsonl")
        assert lines[0] == {
            "kind": "SERVE",
            "version": 1,
            "id": state["id"],
            "offer": "p",
            "resumed": None,
            "replayed": 0,
        }
        assert lines[1]["kind"] == "INPUT" and lines[1]["content"] == said
        assert lines[-1]["kind"] == "END" and lines[-1]["state"]["phase"] == "ended"


def test_a_start_over_an_existing_log_is_refused_and_the_draw_skips_every_id_in_use(tmp_path, monkeypatch):
    a, _ = actor("serve-draw")
    service = Service(a, dir=tmp_path)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))
    drawn = iter(["a" * 16, "a" * 16, "b" * 16, "c" * 16, "d" * 16, "e" * 16])
    monkeypatch.setattr(serve.secrets, "token_hex", lambda n: next(drawn))
    (tmp_path / f"{'b' * 16}.jsonl").symlink_to(tmp_path / "elsewhere")  # a link to nothing is a name in use
    (tmp_path / f"{'c' * 16}.jsonl").write_text("")
    opened = serve._opened

    def raced(path):  # another writer creates the file between the draw and the create
        path.write_text("theirs\n")
        return opened(path)

    async def main():
        first, _ = await service.start({"offer": "p"})  # a: free
        second, _ = await service.start({"offer": "p"})  # a is live, b a dangling link, c a file: d
        monkeypatch.setattr(serve, "_opened", raced)
        with pytest.raises(ServeError) as over:  # e, created by someone else first
            await service.start({"offer": "p"})
        for state in (first, second):
            await backend.answer(FINISH)
            await to_end(service.events(state["id"]))
        return first, second, over.value

    first, second, over = bounded(main)
    assert (
        first["id"] == "a" * 16
        and second["id"] == "d" * 16
        and list(service.runs) == [first["id"], second["id"]]
    )
    assert over.code == "log_failed" and over.reason.startswith(
        f"the log {'e' * 16}.jsonl could not be created"
    )
    assert (tmp_path / f"{'e' * 16}.jsonl").read_text() == "theirs\n"  # what it found is left as it was
    assert (tmp_path / f"{'b' * 16}.jsonl").is_symlink() and not (tmp_path / "elsewhere").exists()


def test_a_repeated_once_key_starts_nothing_and_answers_the_first_run_while_it_is_readable(monkeypatch):
    a, _ = actor("serve-once")
    service = Service(a, keep=1)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))
    monkeypatch.setattr(serve, "_KEYS", 2)

    async def main():
        fields = {"offer": "p", "inputs": ["go"], "once": "first"}
        (one, made), (same, again) = await asyncio.gather(service.start(fields), service.start(dict(fields)))
        with pytest.raises(ServeError) as other:
            await service.start({**fields, "inputs": ["something else"]})
        mine, mine_made = await service.start(fields)  # the key is unchanged by the conflict
        await backend.answer(FINISH)
        await to_end(service.events(one["id"]))
        after, _ = await service.start(fields)  # ended: its state as it is now
        two, _ = await service.start({"offer": "p", "once": "second"})
        await backend.answer(FINISH)
        await to_end(service.events(two["id"]))  # keep=1: the first run is dropped, no log to read
        with pytest.raises(ServeError) as gone:
            await service.start(fields)
        # past two keys the first is forgotten
        three, _ = await service.start({"offer": "p", "once": "third"})
        await backend.answer(FINISH)
        await to_end(service.events(three["id"]))
        anew, anew_made = await service.start(fields)
        await backend.answer(FINISH)
        await to_end(service.events(anew["id"]))
        return one, made, same, again, other.value, mine, mine_made, after, gone.value, anew, anew_made

    one, made, same, again, other, mine, mine_made, after, gone, anew, anew_made = bounded(main)
    assert made and not again and same["id"] == one["id"] and backend.calls == 4
    assert other.code == "conflict" and "'first'" in other.reason
    assert mine["id"] == one["id"] and not mine_made
    assert after["id"] == one["id"] and after["phase"] == "ended"
    assert gone.code == "gone"
    assert anew_made and anew["id"] != one["id"]


def test_a_repeated_start_whose_runs_id_was_forgotten_is_gone_and_never_an_unknown_run(monkeypatch):
    """A `once` key can outlive the memory of its run's id: starts with no key drop runs and push the id
    out. The repeat is still a repeat of a run that existed, so it answers gone as D13 says."""
    monkeypatch.setattr(serve, "_GONE", 1)
    a, _ = actor("serve-once-forgotten")
    service = Service(a, keep=0)
    service.offer(Profile("p"), turns(FINISH, FINISH), RunRequest(emits=(Answer,)))

    async def main():
        fields = {"offer": "p", "inputs": ["go"], "once": "first"}
        one, _ = await service.start(fields)
        await to_end(service.events(one["id"]))
        with pytest.raises(ServeError) as remembered:
            await service.start(fields)
        other, _ = await service.start({"offer": "p", "inputs": ["go"]})
        await to_end(service.events(other["id"]))
        with pytest.raises(ServeError) as forgotten:
            await service.start(fields)
        with pytest.raises(ServeError) as read:
            service.state(one["id"])
        return one["id"], remembered.value, forgotten.value, read.value

    sid, remembered, forgotten, read = bounded(main)
    assert list(service._dropped) != [sid] and sid not in service._dropped
    assert (remembered.code, forgotten.code, read.code) == ("gone", "gone", "unknown_run")
    assert sid in forgotten.reason


# --- C3, C8, D3 -------------------------------------------------------------------------------------


def rows_of(frames: list[tuple]) -> list[dict]:
    return [row for name, data in frames if name == "rows" for row in data["rows"]]


def test_rows_reach_the_log_the_record_read_and_every_stream_once_in_order_as_the_record_has_them(tmp_path):
    a, gates = actor("serve-rows")
    service = Service(a, dir=tmp_path)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))
    seen = runs_of(a)

    async def main():
        every = service.events()
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        mine = service.events(state["id"])
        gates["w"] = asyncio.Event()
        await backend.answer('<lact a1>echo(text="one")</lact><lact w1>wait(key="w")</lact>')
        gates["w"].set()
        await backend.answer(FINISH)
        frames = await to_end(mine)
        every.close()
        return state, frames, queued(every)

    state, frames, everything = bounded(main)
    record = [plain(e) for e in seen[0].record]
    assert [r["seq"] for r in record] == list(range(len(record))) and len(record) >= 8
    first, *middle, last = frames
    assert first[0] == "state" and first[1]["phase"] == "working"
    assert last[0] == "state" and last[1]["phase"] == "ended" and last[1]["rows"] == len(record)
    starts = [data["start"] for name, data in middle if name == "rows"]
    sizes = [len(data["rows"]) for name, data in middle if name == "rows"]
    assert starts[0] == first[1]["rows"] and starts == [
        starts[0] + sum(sizes[:i]) for i in range(len(starts))
    ]
    assert all(data["more"] is False and data["id"] == state["id"] for name, data in middle if name == "rows")
    assert service.record(state["id"])["rows"][: starts[0]] + rows_of(middle) == record
    assert service.record(state["id"]) == {"id": state["id"], "start": 0, "rows": record, "more": False}
    lines = log_lines(tmp_path / f"{state['id']}.jsonl")
    assert lines[1:-1] == record and lines[-1] == {"kind": "END", "state": last[1]}
    assert rows_of(everything) == record  # the stream of every run, open from before the start
    bus = [data for name, data in everything if name == "bus"]
    assert [b["kind"] for b in bus] == [ev.kind for ev in a.bus.log if ev.payload.get("run") == seen[0].id]
    assert bus[0]["kind"] == "run.started" and bus[-1]["kind"] == "run.completed"
    assert {b["id"] for b in bus} == {state["id"]}


def test_a_fold_in_turn_one_is_whole_on_the_row_the_log_the_read_and_the_stream(tmp_path):
    a, _ = actor("serve-fold")
    service = Service(a, dir=tmp_path)
    service.offer(Profile("p"), turns(FINISH), RunRequest(emits=(Answer,), view_budget=200))
    seen = runs_of(a)

    async def main():
        every = service.events()
        state, _ = await service.start({"offer": "p", "inputs": ["word " * 2000]})
        await to_end(service.events(state["id"]))
        every.close()
        return state, queued(every)

    state, everything = bounded(main)
    notice = seen[0].record.by_seq(1)
    assert "directive" in notice.meta  # the fold rides the notification
    whole = plain(notice)
    streamed = [r for r in rows_of(everything) if r["seq"] == 1]
    logged = [r for r in log_lines(tmp_path / f"{state['id']}.jsonl") if r.get("seq") == 1]
    assert streamed == logged == [service.record(state["id"])["rows"][1]] == [whole]
    assert whole["meta"]["directive"]["op"] == "hide"


def test_a_stream_that_overflows_is_closed_never_waited_for_and_a_new_one_opens_with_the_state(
    monkeypatch,
):
    monkeypatch.setattr(serve, "_FRAMES", 3)
    a, _ = actor("serve-overflow")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        slow = service.events(state["id"])  # its reader takes nothing while the run goes on
        await backend.answer('<lact a1>echo(text="one")</lact>')
        await backend.answer(FINISH)
        late = await to_end(slow)
        again = await to_end(service.events(state["id"]))
        return state, late, again

    state, late, again = bounded(main)
    assert len(late) == 3 and late[0][0] == "state" and late[0][1]["phase"] == "working"
    assert [name for name, _ in late[1:]] != ["state", "state"]  # closed on the overflow, not at the end
    # subscribing again resumes it: the state, the rows past it, the end
    assert again[0][0] == "state" and again[-1] == ("state", service.state(state["id"]))
    assert again[-1][1]["phase"] == "ended" and service._subs == set()
    whole = service.record(state["id"])["rows"]
    assert whole[: again[0][1]["rows"]] + rows_of(again) == whole


class Flaky:
    """A log file that takes `good` writes, then fails as `how` says: a short write, a write that raises,
    or a flush that raises after the row's bytes landed."""

    def __init__(self, raw, good: int, how: str):
        self.raw, self.good, self.how, self.failing = raw, good, how, False

    def write(self, data: bytes) -> int:
        if self.good:
            self.good -= 1
            return self.raw.write(data)
        if self.how == "short":
            return self.raw.write(data[: len(data) // 2])
        if self.how == "raise":
            raise OSError(28, "No space left on device")
        self.failing = True
        return self.raw.write(data)

    def flush(self) -> None:
        if self.failing:
            raise OSError(5, "Input/output error")

    def close(self) -> None:
        self.raw.close()


@pytest.mark.parametrize("backend_kind", ["async", "sync"])
@pytest.mark.parametrize("at", ["first", "later"])
@pytest.mark.parametrize("how", ["short", "raise", "flush"])
def test_a_failed_append_latches_the_log_stops_the_run_and_nothing_is_served_or_written_after_it(
    tmp_path, monkeypatch, how, at, backend_kind, store
):
    # the third write is turn 1's notification, the sixth turn 2's: each fails at its `turn.started`,
    # after that turn's stop check, so the turn's call still goes out and its message stands
    good, script, outcome = {
        "first": (2, ("Thinking it over.",), {"kind": "Stopped", "turn": 2}),
        "later": (5, ('<lact a1>echo(text="x")</lact>', FINISH), {"kind": "Success"}),
    }[at]
    opened = serve._opened
    monkeypatch.setattr(serve, "_opened", lambda path: Flaky(opened(path), good, how))
    it = iter(script)
    calls: list[int] = []

    def sync_backend(messages):
        calls.append(1)
        return next(it)

    async def async_backend(messages):
        return sync_backend(messages)

    a, _ = actor("serve-latch")
    service = Service(a, dir=tmp_path)
    service.offer(
        Profile("p"),
        {"sync": sync_backend, "async": async_backend}[backend_kind],
        RunRequest(emits=(Answer,)),
    )
    seen = runs_of(a)

    async def main():
        every = service.events()
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        final = (await to_end(service.events(state["id"])))[-1][1]
        every.close()
        return state, final, queued(every)

    state, final, everything = bounded(main)
    run = seen[0]
    served = good - 1  # the rows confirmed before the failure: every write but the SERVE row's
    assert final["outcome"] == outcome and len(calls) == len(script)
    assert final["error"].startswith("the log failed: OSError: ") and final["rows"] == served
    assert service.state(state["id"]) == final and len(run.record) > served
    assert len([ev for ev in a.bus.log if ev.kind == "turn.started"]) == len(script)  # none after it
    rows = service.record(state["id"])["rows"]
    assert rows == rows_of(everything) == [plain(e) for e in run.record][:served]
    raw = (tmp_path / f"{state['id']}.jsonl").read_bytes().split(b"\n")
    assert [json.loads(line) for line in raw[1 : 1 + served]] == rows
    tail = raw[1 + served :]  # nothing past the failed row, the END row included
    if how == "short":
        assert len(tail) == 1 and tail[0] and not tail[0].endswith(b"}")
    elif how == "raise":
        assert tail == [b""]
    else:  # the bytes landed whole and their flush failed: on disk, and never served
        assert len(tail) == 2 and json.loads(tail[0]) == plain(run.record.by_seq(served)) and tail[1] == b""
    # after a restart the log is read whole: cut short, its whole rows read back, the cut one dropped
    after = Service(Actor("serve-latch-restarted", store=store), dir=tmp_path)
    assert after.state(state["id"])["phase"] == "cut_short"
    reread = after.record(state["id"])["rows"]
    assert reread == rows + ([plain(run.record.by_seq(served))] if how == "flush" else [])


# --- C4, D4, D12 ------------------------------------------------------------------------------------


async def until_state(sub, test) -> list[tuple]:
    """A stream's frames up to and with the first state that passes `test`."""
    frames = []
    async for frame in sub:
        frames.append(frame)
        if frame[0] == "state" and test(frame[1]):
            return frames
    raise AssertionError(f"the stream closed first: {frames}")


def phases(frames: list[tuple], sid: str | None = None) -> list[tuple]:
    return [(d["phase"], d["closed"]) for name, d in frames if name == "state" and sid in (None, d["id"])]


def test_a_stream_opened_after_the_end_is_the_final_state_and_one_racing_the_end_takes_the_rest():
    a, _ = actor("serve-late")
    service = Service(a)
    service.offer(Profile("p"), turns('<lact a1>echo(text="x")</lact>', FINISH), RunRequest(emits=(Answer,)))
    held: list[str] = []
    racing: list = []
    # subscribed before the service's own handler: this one runs between the closure's append and its publish
    a.bus.subscribe("run.completed", lambda ev: racing.append(service.events(held[0])))

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        held.append(state["id"])
        whole = await to_end(service.events(state["id"]))
        late = await to_end(service.events(state["id"]))
        return state, whole, late, await to_end(racing[0])

    state, whole, late, raced = bounded(main)
    final = whole[-1][1]
    assert final["phase"] == "ended" and late == [("state", final)]
    record = service.record(state["id"])["rows"]
    assert raced[0][0] == "state" and raced[0][1]["phase"] == "working" and raced[0][1]["rows"] < len(record)
    assert record[: raced[0][1]["rows"]] + rows_of(raced) == record and raced[-1] == ("state", final)


def test_an_aborted_run_closes_its_stream_with_a_final_state_naming_the_error_and_no_outcome(tmp_path):
    async def down(messages):
        raise RuntimeError("the model is down")

    a, _ = actor("serve-abort")
    service = Service(a, dir=tmp_path)
    service.offer(Profile("p"), down)

    async def main():
        every = service.events()
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        frames = await to_end(service.events(state["id"]))
        every.close()
        return state, frames, queued(every)

    state, frames, everything = bounded(main)
    final = frames[-1][1]
    assert (final["phase"], final["outcome"], final["output"]) == ("aborted", None, None)
    assert final["error"] == "RuntimeError: the model is down" and final["run"] == state["run"]
    assert phases(everything) == [("working", False), ("aborted", False)]
    assert everything[0] == ("runs", {"runs": []}) and everything[-1] == ("state", final)
    assert log_lines(tmp_path / f"{state['id']}.jsonl")[-1] == {"kind": "END", "state": final}


# --- C5, C15, D5 ------------------------------------------------------------------------------------


def test_a_conversation_waits_after_a_reply_takes_input_and_end_closes_it_on_both_streams():
    a, _ = actor("serve-talk")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, converse=True)
    seen = runs_of(a)

    async def main():
        every = service.events()
        state, _ = await service.start({"offer": "p", "inputs": ["hi"]})
        sid = state["id"]
        mine = service.events(sid)
        await backend.answer("Hello. What next?")
        frames = await until_state(mine, lambda d: d["phase"] == "waiting")
        handle = service.say(sid, "Tell me more.")
        await backend.answer("More, then.")
        frames += await until_state(mine, lambda d: d["phase"] == "waiting")
        closed = service.end(sid)
        frames += await to_end(mine)
        with pytest.raises(ServeError) as late:
            service.say(sid, "one more")
        every.close()
        return sid, handle, closed, frames, queued(every), late.value

    sid, handle, closed, frames, everything, late = bounded(main)
    run = seen[0]
    expected = [
        ("working", False),
        ("waiting", False),
        ("working", False),
        ("waiting", False),
        ("waiting", True),
        ("working", True),
        ("ended", True),
    ]
    assert phases(frames) == expected and phases(everything, sid) == expected
    assert closed["closed"] is True and closed["phase"] == "waiting"
    final = frames[-1][1]
    assert (
        final["outcome"] == {"kind": "Stopped", "turn": 3} and final["pending"] == [] and backend.calls == 2
    )
    taken = [e for e in run.record if e.kind is Kind.INPUT and e.meta.get("inbound")]
    assert [(e.content, e.meta["origin"], e.meta["inbound"]) for e in taken] == [
        ("Tell me more.", "client", handle)
    ]
    assert [(i.origin_actor, i.origin_run, i.origin_alias, i.reply) for i in run.consumed] == [
        ("client", sid, None, False)
    ]
    waits = [d for name, d in frames if name == "bus" and d["kind"] == "run.waiting"]
    assert [w["payload"] for w in waits] == [{"run": run.id}] * 2
    with pytest.raises(ServeError) as again:
        service.end(sid)
    assert late.code == again.value.code == "ended"


def test_input_lands_on_the_run_it_names_and_never_on_the_actors_shared_inbox():
    a, _ = actor("serve-named")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, converse=True)
    seen = runs_of(a)

    async def main():
        one, _ = await service.start({"offer": "p", "inputs": ["one"]})
        two, _ = await service.start({"offer": "p", "inputs": ["two"]})
        await backend.answer("Hi one.")
        await backend.answer("Hi two.")
        for state in (one, two):
            await until_state(service.events(state["id"]), lambda d: d["phase"] == "waiting")
        assert a.inbox == []
        handle = service.say(two["id"], "for two")
        assert a.inbox == [] and seen[0].inbox == [] and [i.handle.id for i in seen[1].inbox] == [handle]
        await backend.answer("Got it, two.")
        await until_state(service.events(two["id"]), lambda d: d["phase"] == "waiting")
        for state in (one, two):
            service.end(state["id"])
            await to_end(service.events(state["id"]))
        return handle

    handle = bounded(main)
    for run, said in zip(seen, ([], ["for two"]), strict=True):
        assert [e.content for e in run.record if e.kind is Kind.INPUT and e.meta.get("inbound")] == said
    assert [e.meta["inbound"] for e in seen[1].record if e.meta.get("inbound")] == [handle]


def test_input_mid_turn_waits_for_the_next_turn_and_input_never_taken_is_listed_at_the_end(monkeypatch):
    monkeypatch.setattr(serve, "_KEYS", 2)
    a, _ = actor("serve-input")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))
    seen = runs_of(a)

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        sid = state["id"]
        first = service.say(sid, "first", once="k1")
        repeat = service.say(sid, "first", once="k1")
        with pytest.raises(ServeError) as other:
            service.say(sid, "something else", once="k1")
        assert [i.handle.id for i in seen[0].inbox] == [first]  # queued on the run, not taken mid-turn
        await backend.answer('<lact a1>echo(text="x")</lact>')
        call = await backend.next()  # turn 2 took it, and its call is out
        queued_ = [service.say(sid, f"input {i}") for i in range(64)]
        with pytest.raises(ServeError) as full:
            service.say(sid, "the 65th")
        pending = service.state(sid)["pending"]
        call.set_result(FINISH)
        final = (await to_end(service.events(sid)))[-1][1]
        with pytest.raises(ServeError) as late:
            service.say(sid, "after the end")
        again = service.say(sid, "first", once="k1")  # a repeat is answered before the phase check
        return first, repeat, other.value, queued_, full.value, pending, final, late.value, again

    first, repeat, other, queued_, full, pending, final, late, again = bounded(main)
    assert repeat == again == first and re.fullmatch(r"[0-9a-f]{16}", first)
    assert other.code == "conflict" and full.code == "too_many" and late.code == "ended"
    assert pending == queued_ and final["pending"] == queued_ and final["outcome"] == {"kind": "Success"}
    taken = [e for e in seen[0].record if e.kind is Kind.INPUT and e.meta.get("inbound")]
    assert [(e.content, e.turn) for e in taken] == [("first", 2)]


BAD_INPUTS = [
    ("", None, "bad_request"),
    (7, None, "bad_request"),
    ("x" * (1 << 20) + "y", None, "too_large"),
    ("\udfff", None, "bad_request"),
    ("x", "", "bad_request"),
    ("x", "a b", "bad_request"),
    ("x", "k" * 65, "bad_request"),
    ("x", 3, "bad_request"),
]


@pytest.mark.parametrize("text, once, code", BAD_INPUTS)
def test_input_outside_its_form_is_refused_and_nothing_is_queued(text, once, code):
    a, _ = actor("serve-bad-input")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))
    seen = runs_of(a)

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        with pytest.raises(ServeError) as refused:
            service.say(state["id"], text, once)
        with pytest.raises(ServeError) as unknown:
            service.say("0" * 16, "x")
        pending = list(seen[0].inbox)
        await backend.answer(FINISH)
        await to_end(service.events(state["id"]))
        return refused.value, unknown.value, pending

    refused, unknown, pending = bounded(main)
    assert refused.code == code and unknown.code == "unknown_run" and pending == []


async def waiting(service, sid: str) -> None:
    await until_state(service.events(sid), lambda d: d["phase"] == "waiting")


@pytest.mark.parametrize(
    "when", ["the first turn", "waiting", "mid-call", "commands", "a retry", "input queued"]
)
def test_end_closes_a_conversation_at_its_next_wait_wherever_it_lands(when):
    """However far the exchange in flight has to go, through command turns or a retry, it ends Stopped
    at its next wait; input after the end is refused, input queued before it stays listed."""
    a, gates = actor("serve-end")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, converse=True)

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["hi"]})
        sid, queued_ = state["id"], []
        if when == "the first turn":
            service.end(sid)
            await backend.answer("Hello.")
        elif when == "waiting":
            await backend.answer("Hello.")
            await waiting(service, sid)
            service.end(sid)
        elif when == "mid-call":
            await backend.answer("Hello.")
            await waiting(service, sid)
            service.say(sid, "more")
            call = await backend.next()
            service.end(sid)
            call.set_result("More.")
        elif when == "commands":
            gates["w"] = asyncio.Event()
            await backend.answer('<lact w1>wait(key="w")</lact>')
            service.end(sid)
            gates["w"].set()
            await backend.answer("Done waiting.")  # the turn after the command still runs
        elif when == "a retry":
            service.end(sid)
            await backend.answer("<lact a1>echo(1)</lact>")  # unreadable: a retry turn follows
            await backend.answer("Hello.")
        else:
            queued_.append(service.say(sid, "first"))
            service.end(sid)
            await backend.answer("Hello.")
        with pytest.raises(ServeError) as refused:
            service.say(sid, "after the end")
        return queued_, (await to_end(service.events(sid)))[-1][1], refused.value

    queued_, final, refused = bounded(main)
    at_turn = {
        "the first turn": 2,
        "waiting": 2,
        "mid-call": 3,
        "commands": 3,
        "a retry": 3,
        "input queued": 2,
    }
    assert final["outcome"] == {"kind": "Stopped", "turn": at_turn[when]} and final["closed"] is True
    assert final["pending"] == queued_ and refused.code == "ended"


def test_end_is_refused_on_a_run_no_conversation_and_a_wake_that_finds_nothing_sleeps_again():
    a, _ = actor("serve-not-talk")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))
    service.offer(Profile("talk"), backend, converse=True)

    async def main():
        task, _ = await service.start({"offer": "p", "inputs": ["go"]})
        with pytest.raises(ServeError) as refused:
            service.end(task["id"])
        await backend.answer(FINISH)
        await to_end(service.events(task["id"]))
        talk, _ = await service.start({"offer": "talk", "inputs": ["hi"]})
        call = await backend.next()
        service.say(talk["id"], "while you think")  # wakes nothing yet: the run is in its call
        call.set_result("Hello.")
        await backend.answer("Taken.")  # the next turn took it; its wake is still set
        await waiting(service, talk["id"])  # the wait found the wake and nothing to take, and slept
        calls = backend.calls
        await asyncio.sleep(0.05)
        still = service.state(talk["id"])["phase"], backend.calls == calls
        service.end(talk["id"])
        await to_end(service.events(talk["id"]))
        return refused.value, still

    refused, still = bounded(main)
    assert refused.code == "not_conversation" and still == ("waiting", True)


# --- C6, D6 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "when, converse",
    [("waiting", True)] + [(w, c) for w in ("mid-call", "a command") for c in (False, True)],
)
def test_stop_ends_a_run_stopped_at_its_next_turn_unless_it_ended_first(when, converse):
    a, gates = actor("serve-stop")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)), converse=converse)
    seen = runs_of(a)

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        sid = state["id"]
        if when == "waiting":
            await backend.answer("Hello.")
            await waiting(service, sid)
            service.stop(sid)
        elif when == "mid-call":
            await backend.next()  # in flight, and never answered: the stop cuts it
            service.stop(sid)
        else:
            gates["w"] = asyncio.Event()
            await backend.answer('<lact w1>wait(key="w")</lact>')
            await asyncio.sleep(0)
            service.stop(sid)
            gates["w"].set()  # the pending command settles first
        return (await to_end(service.events(sid)))[-1][1]

    final = bounded(main)
    run = seen[0]
    assert final["outcome"] == {"kind": "Stopped", "turn": 2} and backend.calls == 1
    if when == "a command":
        assert run.record.get("w1").content.ok and run.record.get("w1").content.value == "w"
    if when == "mid-call":
        assert any("cut this turn short" in str(e.content) for e in run.record if e.kind is Kind.SYSTEM)


@pytest.mark.parametrize("converse", [False, True])
def test_an_interrupt_cuts_the_call_in_flight_and_the_run_goes_on_or_waits_for_input(converse):
    a, _ = actor("serve-interrupt")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)), converse=converse)
    seen = runs_of(a)

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        sid = state["id"]
        await backend.next()
        cut = service.interrupt(sid)
        if converse:
            await waiting(service, sid)  # no message came of the cut turn, and the person speaks next
            service.say(sid, "go on")
        await backend.answer(FINISH)
        final = (await to_end(service.events(sid)))[-1][1]
        return cut, final

    cut, final = bounded(main)
    assert cut["cut"] is True and cut["phase"] == "working" and set(cut) - {"cut"} == set(final)
    assert final["outcome"] == {"kind": "Success"} and backend.calls == 2
    texts = [e.content for e in seen[0].record if e.kind is Kind.TEXT]
    assert texts == [FINISH]


def test_an_interrupt_with_no_call_in_flight_cuts_nothing():
    a, gates = actor("serve-interrupt-idle")
    service = Service(a)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        gates["w"] = asyncio.Event()
        await backend.answer('<lact w1>wait(key="w")</lact>')
        await asyncio.sleep(0)
        cut = service.interrupt(state["id"])
        gates["w"].set()
        await backend.answer(FINISH)
        return cut, (await to_end(service.events(state["id"])))[-1][1]

    cut, final = bounded(main)
    assert cut["cut"] is False and final["outcome"] == {"kind": "Success"}


@pytest.mark.parametrize("order", ["interrupt then cancel", "cancel then interrupt", "cancel alone"])
def test_a_cancel_before_the_commit_is_an_abort_with_no_outcome_even_with_an_interrupt(order, tmp_path):
    a, _ = actor("serve-cancel")
    service = Service(a, dir=tmp_path)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        sid = state["id"]
        await backend.next()
        if order == "interrupt then cancel":
            service.interrupt(sid)
            cancelled = await service.cancel(sid)
        elif order == "cancel then interrupt":
            cancelling = asyncio.ensure_future(service.cancel(sid))
            await asyncio.sleep(0)  # the cancel requested, the task not yet woken
            service.interrupt(sid)
            cancelled = await cancelling
        else:
            cancelled = await service.cancel(sid)
        with pytest.raises(ServeError) as again:
            await service.cancel(sid)
        return sid, cancelled, again.value

    sid, cancelled, again = bounded(main)
    assert (cancelled["phase"], cancelled["outcome"], cancelled["output"]) == ("aborted", None, None)
    assert cancelled["error"] is None and backend.calls == 1 and again.code == "ended"  # a cancel is no error
    assert log_lines(tmp_path / f"{sid}.jsonl")[-1] == {"kind": "END", "state": cancelled}


@pytest.mark.parametrize("control", ["cancel", "stop"])
def test_a_cancel_in_the_last_reap_aborts_with_no_outcome_ever_shown_and_a_stop_changes_nothing(
    control,
    store,
):
    """The runtime sets a run's outcome, then reaps what `OUT{}` did not name, then returns. A command
    that absorbs its cancel holds that reap open: a cancel landing there is before the commit, an abort
    whose outcome no state ever showed; a stop there leaves the outcome as it was."""
    a = Actor(f"serve-last-reap-{control}", store=store)
    entered, absorbed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @a.handler(Wait)
    async def wait(req: Wait, ctx) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            absorbed.set()
            await release.wait()
        return "settled"

    async def accept(run, fields):
        await entered.wait()

    service = Service(a)
    service.offer(
        Profile("p"),
        turns('<lact a1>wait(key="x")</lact>' + FINISH),
        RunRequest(emits=(Answer,), accept=accept),
    )

    async def main():
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        sid = state["id"]
        frames = asyncio.ensure_future(to_end(service.events(sid)))
        await absorbed.wait()  # the output was accepted, and the reap waits on the command it cancelled
        held = service.runs[sid]
        during = (service.state(sid), type(held.run.outcome).__name__)
        if control == "cancel":
            ending = asyncio.ensure_future(service.cancel(sid))
            await asyncio.sleep(0)
        else:
            service.stop(sid)
        release.set()
        final = await ending if control == "cancel" else None
        seen = await frames
        return sid, during, final, seen

    sid, (during, inside), final, seen = bounded(main)
    assert inside == "Success" and (during["phase"], during["outcome"], during["output"]) == (
        "working",
        None,
        None,
    )
    states = [data for name, data in seen if name == "state"]
    if control == "cancel":
        assert (final["phase"], final["outcome"], final["output"], final["error"]) == (
            "aborted",
            None,
            None,
            None,
        )
        assert states[-1] == final and all(st["outcome"] is None and st["output"] is None for st in states)
    else:
        assert all(st["outcome"] is None for st in states[:-1])
        assert (states[-1]["phase"], states[-1]["outcome"]["kind"]) == ("ended", "Success")
        assert states[-1]["output"] == {"answer": {"text": "done"}}
    assert service.state(sid) == states[-1]


def test_a_starting_run_takes_a_cancel_only_and_ends_aborted_with_no_runtime_id(tmp_path):
    a, _ = actor("serve-starting")
    service = Service(a, dir=tmp_path)
    service.offer(Profile("p"), turns(FINISH), RunRequest(emits=(Answer,)), converse=True)

    async def main():
        starting = asyncio.ensure_future(service.start({"offer": "p", "inputs": ["go"]}))
        await asyncio.sleep(0)  # the id and the log are reserved; the run's task has not taken a step
        (sid,) = service.runs
        assert service.state(sid)["phase"] == "starting"
        refusals = []
        for control in (service.say, service.end, service.interrupt, service.stop):
            with pytest.raises(ServeError) as refused:
                control(*((sid, "x") if control == service.say else (sid,)))
            refusals.append(refused.value.code)
        cancelled = await service.cancel(sid)
        state, created = await starting
        return sid, refusals, cancelled, state, created

    sid, refusals, cancelled, state, created = bounded(main)
    assert refusals == ["starting"] * 4 and created and state == cancelled
    assert (cancelled["phase"], cancelled["run"], cancelled["turn"], cancelled["rows"]) == (
        "aborted",
        None,
        None,
        0,
    )
    assert [r["kind"] for r in log_lines(tmp_path / f"{sid}.jsonl")] == ["SERVE", "END"]


# --- C7, D7 -----------------------------------------------------------------------------------------


def line(doc) -> bytes:
    return (json.dumps(doc) + "\n").encode()


def served_row(sid: str, resumed: str | None = None) -> dict:
    return {"kind": "SERVE", "version": 1, "id": sid, "offer": "p", "resumed": resumed, "replayed": 0}


def entry(seq: int, kind: str = "INPUT", content="go", alias: str | None = None) -> dict:
    return {
        "seq": seq,
        "kind": kind,
        "name": f"x{seq}",
        "turn": 0,
        "alias": alias,
        "meta": {},
        "content": content,
    }


def test_a_resume_replays_a_stored_runs_log_and_a_restart_reads_each_log_by_its_own_end_row(tmp_path, store):
    calls: list = []

    async def backend(messages):
        calls.append(messages)
        if len(calls) == 1:
            return FINISH
        raise RuntimeError("the model is down")

    a, _ = actor("serve-resume")
    service = Service(a, dir=tmp_path)
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))
    seen = runs_of(a)

    async def main():
        first, _ = await service.start({"offer": "p", "inputs": ["go"]})
        await to_end(service.events(first["id"]))
        second, _ = await service.start({"offer": "p", "inputs": ["again"], "resume": first["id"]})
        await to_end(service.events(second["id"]))
        return first, second

    first, second = bounded(main)
    one, two = (log_lines(tmp_path / f"{s['id']}.jsonl") for s in (first, second))
    history = one[1:-1]
    assert two[0] == {
        "kind": "SERVE",
        "version": 1,
        "id": second["id"],
        "offer": "p",
        "resumed": first["id"],
        "replayed": len(history),
    }
    assert two[1 : 1 + len(history)] == history == [plain(e) for e in seen[1].record][: len(history)]
    assert two[1 + len(history)]["content"] == "again"
    assert history[-1]["content"].startswith("<system closure") and "Success" in history[-1]["content"]
    assert [plain(e) for e in load_log(tmp_path / f"{second['id']}.jsonl")] == two[1:-1]  # SERVE, END skipped
    for at, state in enumerate((first, second)):
        os.utime(tmp_path / f"{state['id']}.jsonl", (1_000_000 + at, 1_000_000 + at))
    after = Service(Actor("serve-resume-restarted", store=store), dir=tmp_path)
    # the resumed run replayed its ancestor's closure, and its own END row is what says how it ended
    listed = [(s["id"], s["phase"], s["outcome"], s["resumed"]) for s in after.runs_state()["runs"]]
    assert listed == [
        (first["id"], "ended", {"kind": "Success"}, None),
        (second["id"], "aborted", None, first["id"]),
    ]
    assert after.state(second["id"]) == two[-1]["state"] and after.record(second["id"])["rows"] == two[1:-1]


A, B, C, D, E, F, G, H = (ch * 16 for ch in "abcdef09")


def test_after_a_restart_the_newest_logs_are_kept_by_end_row_and_one_without_a_serve_row_is_not(
    tmp_path,
    store,
):
    def write(sid: str, data: bytes, at: int) -> None:
        path = tmp_path / f"{sid}.jsonl"
        path.write_bytes(data)
        os.utime(path, (at, at))

    ended = {"id": A, "offer": "p", "phase": "ended", "outcome": {"kind": "Success"}, "pending": []}
    write(A, line(served_row(A)) + line(entry(0)) + line({"kind": "END", "state": ended}), 100)
    write(B, line(served_row(B)) + line(entry(0)) + b'{"kind": "END", "sta', 300)  # the END row cut
    write(C, line(entry(0)) + line({"kind": "END", "state": ended}), 400)  # no SERVE row
    write(D, line(served_row(E)) + line(entry(0)), 500)  # a SERVE row of another id
    working = {**ended, "id": E, "phase": "working"}
    write(E, line(served_row(E)) + line(entry(0)) + line({"kind": "END", "state": working}), 200)
    write(G, b'{"kind": "SERVE", "vers', 600)  # its SERVE row cut
    (tmp_path / f"{F}.jsonl").mkdir()
    (tmp_path / f"{H}.jsonl").symlink_to(tmp_path / "nowhere")
    (tmp_path / "notes.jsonl").write_bytes(line(served_row("notes")))
    after = Service(Actor("serve-listing", store=store), dir=tmp_path, keep=2)
    assert list(after.runs) == [E, B]  # the newest two by modification time; A is older and not held
    assert after.state(A) == ended  # read from its log all the same
    cut = after.state(B)
    assert cut == {
        "id": B,
        "offer": "p",
        "resumed": None,
        "run": None,
        "phase": "cut_short",
        "turn": None,
        "cost": None,
        "rows": None,
        "pending": None,
        "closed": None,
        "outcome": None,
        "output": None,
        "error": None,
    }
    assert after.state(E)["phase"] == "cut_short"  # an END row saying the run went on is no end
    assert after.record(B) == {"id": B, "start": 0, "rows": [entry(0)], "more": False}
    assert after.record(B, 1)["rows"] == []

    async def main():
        return await to_end(after.events(B)), await asyncio.gather(
            *(after.cancel(sid) for sid in (A, B)), return_exceptions=True
        )

    frames, cancels = bounded(main)
    assert frames == [("state", cut)] and [c.code for c in cancels] == ["ended", "ended"]
    refusals = {}
    for sid in (C, D, F, G, H, "notes", "../" + A):
        with pytest.raises(ServeError) as refused:
            after.state(sid)
        refusals[sid] = refused.value.code
    assert set(refusals.values()) == {"unknown_run"}
    for call in (
        lambda: after.say(B, "x"),
        lambda: after.end(B),
        lambda: after.interrupt(B),
        lambda: after.stop(B),
    ):
        with pytest.raises(ServeError) as refused:
            call()
        assert refused.value.code == "ended"
    with pytest.raises(ServeError) as past:
        after.record(B, 2)
    assert past.value.code == "bad_request" and "0 to 1" in past.value.reason


def test_a_resume_names_a_stored_runs_id_and_one_whose_log_cannot_be_replayed_is_refused(tmp_path, store):
    def write(sid: str, *docs) -> None:
        (tmp_path / f"{sid}.jsonl").write_bytes(
            b"".join(d if isinstance(d, bytes) else line(d) for d in docs)
        )

    write(A, entry(0))  # no SERVE row
    write(B, served_row(B), b"not json\n", entry(1))
    write(C, served_row(C), entry(0, "TEXT", "a", alias="x"), entry(1, "TEXT", "b", alias="x"))
    write(D, served_row(D), {"kind": "INPUT", "content": "no seq"})
    files = sorted(tmp_path.iterdir())
    a, _ = actor("serve-resume-refused")
    backend = Held()
    without = Service(a)
    without.offer(Profile("p"), backend)
    service = Service(a, dir=tmp_path)
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))

    async def main():
        live, _ = await service.start({"offer": "p", "inputs": ["go"]})
        codes = {}
        for name, svc, resume in (
            ("no directory", without, A),
            ("no log", service, E),
            ("no SERVE row", service, A),
            ("live", service, live["id"]),
            ("a bad line", service, B),
            ("no replay", service, C),
            ("a bad row", service, D),
        ):
            with pytest.raises(ServeError) as refused:
                await svc.start({"offer": "p", "resume": resume})
            codes[name] = (refused.value.code, refused.value.reason)
        await backend.answer(FINISH)
        await to_end(service.events(live["id"]))
        return live, codes

    live, codes = bounded(main)
    assert {k: v[0] for k, v in codes.items()} == {
        "no directory": "no_resume",
        "no log": "no_resume",
        "no SERVE row": "no_resume",
        "live": "no_resume",
        "a bad line": "unreadable_log",
        "no replay": "unreadable_log",
        "a bad row": "unreadable_log",
    }
    assert (
        codes["no replay"][1]
        == f"the history of {C} does not replay: ValueError: alias 'x' already names _m0"
    )
    assert codes["a bad line"][1].startswith(f"the log of {B} cannot be read: ValueError: ")
    assert codes["a bad row"][1] == f"the log of {D} cannot be read: KeyError: 'seq'"
    # no log left behind
    assert sorted(tmp_path.iterdir()) == sorted([*files, tmp_path / f"{live['id']}.jsonl"])
    restarted = Service(Actor("serve-resume-read", store=store), dir=tmp_path)
    with pytest.raises(ServeError) as unreadable:
        restarted.record(B)
    assert unreadable.value.code == "unreadable_log"


def test_a_dropped_run_reads_whole_from_its_log_and_one_whose_log_failed_is_gone_until_a_restart(
    tmp_path, monkeypatch, store
):
    a, _ = actor("serve-dropped")
    service = Service(a, dir=tmp_path, keep=0)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))

    async def main():
        kept, _ = await service.start({"offer": "p", "inputs": ["go"]})
        call = await backend.next()
        handle = service.say(kept["id"], "never taken")
        call.set_result(FINISH)
        final = (await to_end(service.events(kept["id"])))[-1][1]
        opened = serve._opened
        monkeypatch.setattr(serve, "_opened", lambda path: Flaky(opened(path), 2, "raise"))
        failed, _ = await service.start({"offer": "p", "inputs": ["go"]})
        await backend.answer("Thinking it over.")
        failed_final = (await to_end(service.events(failed["id"])))[-1][1]
        return kept, handle, final, failed, failed_final

    kept, handle, final, failed, failed_final = bounded(main)
    assert service.runs == {}  # keep=0: both dropped as they ended
    assert final["pending"] == [handle] and service.state(kept["id"]) == final
    assert service.record(kept["id"])["rows"] == log_lines(tmp_path / f"{kept['id']}.jsonl")[1:-1]
    assert failed_final["error"].startswith("the log failed")
    for read in (service.state, service.record, service.events):
        with pytest.raises(ServeError) as gone:
            read(failed["id"])
        assert gone.value.code == "gone"
    assert (
        Service(Actor("serve-dropped-again", store=store), dir=tmp_path).state(failed["id"])["phase"]
        == "cut_short"
    )


# --- C14 --------------------------------------------------------------------------------------------


def test_the_ids_of_dropped_runs_are_remembered_to_a_count_and_one_forgotten_is_an_unknown_run(monkeypatch):
    monkeypatch.setattr(serve, "_GONE", 2)
    a, _ = actor("serve-forgotten")
    service = Service(a, keep=0)
    service.offer(Profile("p"), turns(FINISH, FINISH, FINISH), RunRequest(emits=(Answer,)))

    async def main():
        ids = []
        for _ in range(3):
            state, _ = await service.start({"offer": "p", "inputs": ["go"]})
            await to_end(service.events(state["id"]))
            ids.append(state["id"])
        return ids

    ids = bounded(main)
    codes = []
    for sid in ids:
        with pytest.raises(ServeError) as refused:
            service.state(sid)
        codes.append(refused.value.code)
    assert service.runs == {} and list(service._dropped) == ids[1:]
    assert codes == ["unknown_run", "gone", "gone"]


def test_a_dropped_run_whose_id_is_forgotten_reads_from_its_log_as_after_a_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_GONE", 1)
    a, _ = actor("serve-forgotten-logged")
    service = Service(a, dir=tmp_path, keep=0)
    service.offer(Profile("p"), turns(FINISH, FINISH), RunRequest(emits=(Answer,)))

    async def main():
        ids = []
        for _ in range(2):
            state, _ = await service.start({"offer": "p", "inputs": ["go"]})
            await to_end(service.events(state["id"]))
            ids.append(state["id"])
        return ids

    first, second = bounded(main)
    assert service.runs == {} and list(service._dropped) == [second]
    for sid in (first, second):
        state = service.state(sid)
        assert (state["phase"], state["outcome"]["kind"]) == ("ended", "Success")
        assert service.record(sid)["rows"] == [json.loads(json.dumps(r)) for r in service.record(sid)["rows"]]
        assert len(service.record(sid)["rows"]) == state["rows"] > 0


@pytest.mark.parametrize(
    "capacity",
    [
        {"max_live": 0},
        {"max_live": -1},
        {"max_live": True},
        {"max_live": 2.0},
        {"keep": -1},
        {"keep": True},
        {"keep": 1.5},
    ],
    ids=repr,
)
def test_a_capacity_that_is_no_count_is_refused_when_the_service_is_made(capacity, store):
    (name,) = capacity
    with pytest.raises(ValueError, match=f"^{name} is a count of at least"):
        Service(Actor("serve-capacity", store=store), **capacity)
    made = Service(Actor("serve-capacity-least", store=store), max_live=1, keep=0)
    assert (made.max_live, made.keep) == (1, 0)


@pytest.mark.parametrize("with_dir", [False, True])
def test_an_ended_run_past_keep_is_dropped_oldest_first_and_a_read_between_two_pages_says_so(
    tmp_path, monkeypatch, with_dir
):
    monkeypatch.setattr(serve, "_PAGE", 2)
    a, _ = actor("serve-keep")
    service = Service(a, dir=tmp_path if with_dir else None, keep=1)
    backend = Held()
    service.offer(Profile("p"), backend, RunRequest(emits=(Answer,)))

    async def main():
        first, _ = await service.start({"offer": "p", "inputs": ["go"]})
        await backend.answer('<lact a1>echo(text="x")</lact>')
        await backend.answer(FINISH)
        final = (await to_end(service.events(first["id"])))[-1][1]
        page = service.record(first["id"])
        second, _ = await service.start({"offer": "p", "inputs": ["go"]})
        await backend.answer(FINISH)
        await to_end(service.events(second["id"]))
        third, _ = await service.start({"offer": "p", "inputs": ["go"]})  # live: held whatever keep says
        held = list(service.runs)
        reads = {}
        for name, read in (
            ("record", lambda: service.record(first["id"], 2)),
            ("state", lambda: service.state(first["id"])),
            ("events", lambda: service.events(first["id"])),
            ("input", lambda: service.say(first["id"], "x")),
        ):
            try:
                reads[name] = read()
            except ServeError as e:
                reads[name] = e.code
        await backend.answer(FINISH)
        await to_end(service.events(third["id"]))
        return first, second, third, final, page, held, reads

    first, second, third, final, page, held, reads = bounded(main)
    assert page["start"] == 0 and len(page["rows"]) == 2 and page["more"] is True and final["rows"] > 4
    assert held == [second["id"], third["id"]]
    if with_dir:
        rows = log_lines(tmp_path / f"{first['id']}.jsonl")[1:-1]
        assert rows[:2] == page["rows"] and reads["record"] == {
            "id": first["id"],
            "start": 2,
            "rows": rows[2:4],
            "more": True,
        }
        assert reads["state"] == final and reads["input"] == "ended"
        assert queued(reads["events"]) == [("state", final)]
    else:
        assert reads == {"record": "gone", "state": "gone", "events": "gone", "input": "gone"}
        with pytest.raises(ServeError) as unknown:
            service.state("f" * 16)
        assert unknown.value.code == "unknown_run"


# --- D9 ---------------------------------------------------------------------------------------------


def test_every_outcome_kind_is_projected_with_its_own_fields_and_the_output_beside_it():
    a, _ = actor("serve-outcomes")
    service = Service(a)

    async def never(run, output):
        return "not good enough"

    service.offer(Profile("success"), turns(FINISH), RunRequest(emits=(Answer,)))
    service.offer(Profile("exhausted"), turns("thinking"), RunRequest(max_turns=1))
    service.offer(
        Profile("refused"), turns(FINISH), RunRequest(emits=(Answer,), accept=never, max_refusals=1)
    )
    service.offer(Profile("stopped"), turns("Hello."), converse=True)

    async def main():
        finals = {}
        for name in service.offers:
            state, _ = await service.start({"offer": name, "inputs": ["go"]})
            if name == "stopped":
                await waiting(service, state["id"])
                service.end(state["id"])
            finals[name] = (await to_end(service.events(state["id"])))[-1][1]
        return finals

    finals = bounded(main)
    assert {k: (v["outcome"], v["output"]) for k, v in finals.items()} == {
        "success": ({"kind": "Success"}, {"answer": {"text": "done"}}),
        "exhausted": ({"kind": "Exhausted", "turns": 1, "reason": "turns"}, None),
        "refused": ({"kind": "Refused", "why": "not good enough", "times": 1}, None),
        "stopped": ({"kind": "Stopped", "turn": 2}, None),
    }
    assert all(list(f) == list(finals["success"]) for f in finals.values())
    assert list(finals["success"]) == [
        "id",
        "offer",
        "resumed",
        "run",
        "phase",
        "turn",
        "cost",
        "rows",
        "pending",
        "closed",
        "outcome",
        "output",
        "error",
    ]


class Odd(BaseModel):
    """Answer with values JSON cannot hold as they are."""

    n: int


class Unprintable:
    def __repr__(self) -> str:
        raise RuntimeError("no repr")


def test_a_value_json_cannot_hold_is_carried_by_its_repr_and_never_as_the_object(store):
    a = Actor("serve-odd", store=store)
    cycle: list = [1]
    cycle.append(cycle)
    deep: list = []
    cur = deep
    for _ in range(300):  # past pydantic's depth, inside the runtime's
        cur.append([])
        cur = cur[0]

    @a.handler(Odd, effect="pure")
    async def odd(req: Odd, ctx) -> dict:
        ctx.emit("odd.seen", thing=Unprintable(), when=float("inf"))
        return {"bytes": b"\xff\xfe", "nan": float("nan"), "cycle": cycle, "deep": deep, "keys": {1: (2, 3)}}

    async def free(messages):
        asked = any("o1" in m.content for m in messages)
        return Reply(FINISH if asked else "<lact o1>odd(n=1)</lact>", cost="free")

    service = Service(a)
    service.offer(Profile("p"), free, RunRequest(emits=(Answer,)))

    async def main():
        every = service.events()
        state, _ = await service.start({"offer": "p", "inputs": ["go"]})
        await to_end(service.events(state["id"]))
        every.close()
        return state, queued(every)

    state, everything = bounded(main)
    final = service.state(state["id"])
    assert final["cost"] is None  # a cost that is no number reads as spent, and NaN is not JSON
    (result,) = [r for r in service.record(state["id"])["rows"] if r["kind"] == "RESULT"]
    value = result["content"]["value"]
    assert (
        value["bytes"] == {"repr": "b'\\xff\\xfe'"}
        and value["nan"] is None
        and value["keys"] == {"1": [2, 3]}
    )
    assert value["cycle"] == [1, {"repr": "[1, [...]]"}]
    depth, cur = 0, value["deep"]
    while isinstance(cur, list):
        depth, cur = depth + 1, cur[0]
    assert set(cur) == {"repr"} and cur["repr"].startswith("[[") and depth < 300
    (seen,) = [d["payload"] for name, d in everything if name == "bus" and d["kind"] == "odd.seen"]
    assert (
        seen["thing"]["repr"].startswith("<tests.test_serve.Unprintable object at 0x")
        and seen["when"] is None
    )
    json.dumps(everything, allow_nan=False)  # every frame is JSON as it stands


def test_a_start_is_refused_while_an_actor_named_client_is_registered(monkeypatch):
    a, _ = actor("serve-client-name")
    service = Service(a)
    service.offer(Profile("p"), turns(FINISH), RunRequest(emits=(Answer,)))
    # a finished run answers each input to the actor its origin names, and a client's input names `client`
    monkeypatch.setitem(actor_module._ACTORS, "client", a)

    async def main():
        with pytest.raises(ServeError) as refused:
            await service.start({"offer": "p"})
        return refused.value

    refused = bounded(main)
    assert refused.code == "refused" and "'client'" in refused.reason and service.runs == {}


# --- the edges of D3, D5 and D7 ---------------------------------------------------------------------


def test_a_comment_line_is_queued_only_on_an_open_stream_with_room(monkeypatch, store):
    monkeypatch.setattr(serve, "_FRAMES", 2)
    service = Service(Actor("serve-ping", store=store))
    sub = service.events()
    sub.ping()
    sub.ping()  # the queue is full: a comment line is dropped, and the stream stays open
    full, still_open = list(sub._frames), sub._open
    sub.close()
    sub.ping()
    assert full == list(sub._frames) == [("runs", {"runs": []}), (None, None)] and still_open


@pytest.mark.parametrize("how", ["short", "raise", "flush"])
def test_a_serve_row_that_cannot_be_written_is_log_failed_and_leaves_no_file_run_or_key(
    tmp_path, monkeypatch, how
):
    opened = serve._opened
    monkeypatch.setattr(serve, "_opened", lambda path: Flaky(opened(path), 0, how))
    a, _ = actor("serve-head-row")
    service = Service(a, dir=tmp_path)
    service.offer(Profile("p"), turns(FINISH), RunRequest(emits=(Answer,)))

    async def main():
        with pytest.raises(ServeError) as failed:
            await service.start({"offer": "p", "once": "k"})
        return failed.value

    failed = bounded(main)
    assert failed.code == "log_failed" and re.fullmatch(
        r"the log [0-9a-f]{16}\.jsonl could not be written: .+", failed.reason
    )
    assert sorted(tmp_path.iterdir()) == [] and service.runs == {} and a.runs == {}
    assert service._starts == {} and service._off is None  # nothing was held, the bus included


def test_a_run_cancelled_while_starting_whose_end_row_fails_is_latched_and_stopped(tmp_path, monkeypatch):
    opened = serve._opened
    monkeypatch.setattr(serve, "_opened", lambda path: Flaky(opened(path), 1, "raise"))
    a, _ = actor("serve-end-row")
    service = Service(a, dir=tmp_path)
    service.offer(Profile("p"), turns(FINISH), RunRequest(emits=(Answer,)))

    async def main():
        starting = asyncio.ensure_future(service.start({"offer": "p", "inputs": ["go"]}))
        await asyncio.sleep(0)  # reserved, its task not yet stepped
        (sid,) = service.runs
        cancelled = await service.cancel(sid)
        state, _ = await starting
        return sid, cancelled, state

    sid, cancelled, state = bounded(main)
    assert cancelled == state and (state["phase"], state["run"]) == ("aborted", None)
    assert state["error"] == "the log failed: OSError: [Errno 28] No space left on device"
    assert [r["kind"] for r in log_lines(tmp_path / f"{sid}.jsonl")] == ["SERVE"]
    assert service.runs[sid].latched and service.runs[sid].stopped and service.runs[sid].log is None


def test_an_input_once_key_past_the_last_held_acts_again(monkeypatch):
    monkeypatch.setattr(serve, "_KEYS", 2)
    a, _ = actor("serve-input-keys")
    service = Service(a)
    backend = Held()
    service.offer(Profile("talk"), backend, converse=True)

    async def main():
        state, _ = await service.start({"offer": "talk", "inputs": ["hi"]})
        sid = state["id"]
        await backend.next()  # in its call: input is queued, not taken
        first = service.say(sid, "one", "k1")
        second = service.say(sid, "two", "k2")
        repeat = service.say(sid, "one", "k1")
        third = service.say(sid, "three", "k3")  # past two keys, k1 is forgotten
        again = service.say(sid, "other text", "k1")
        pending = service.state(sid)["pending"]
        await service.cancel(sid)
        return first, second, repeat, third, again, pending

    first, second, repeat, third, again, pending = bounded(main)
    assert repeat == first and again not in (first, second, third)
    assert pending == [first, second, third, again]


def test_a_second_end_while_the_run_is_live_answers_its_state_and_sends_no_frame():
    a, _ = actor("serve-end-twice")
    service = Service(a)
    backend = Held()
    service.offer(Profile("talk"), backend, converse=True)

    async def main():
        state, _ = await service.start({"offer": "talk", "inputs": ["hi"]})
        sid = state["id"]
        call = await backend.next()
        sub = service.events(sid)
        first, second = service.end(sid), service.end(sid)
        call.set_result("Bye.")
        return sid, first, second, await to_end(sub)

    sid, first, second, frames = bounded(main)
    assert first == second and first["closed"] is True
    assert phases(frames) == [("working", False), ("working", True), ("ended", True)]
    assert service.state(sid)["outcome"] == {"kind": "Stopped", "turn": 2}
