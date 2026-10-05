"""The routes `lion areas --serve` answers under `/v2/` for the Mac app: the registry's areas and their
residents as agents, each one's definition, wakes and record, the mail waiting for the person the
server runs as, the default plugin (what the runtime and this tree register) and the models with
their providers.
Every answer is read from what already holds it: the registry, the residents' own files, the server's
own `models.toml` beside the registry, the providers' own answers. Admission, the idempotency of a
write and the control surface are the server's (`areas.py`); the work of every other route is here.

An agent's id is `<area>/<directory name>`, the directory's name followed by `~` and six hex digits of
its path when two desks of one area share a name. A definition is the resident's `chores.toml` read as
the app's shape, and written back key by key, every other byte of the file kept, through the loaders
the resident itself reloads it with."""

from __future__ import annotations

import hashlib
import json
import math
import os
import plistlib
import re
import shutil
import socket
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.parsers.expat import ExpatError

from hub.agent.agent import Agent, Budget, Gtd, Send, Wake, claude_code_options, codex_options
from hub.agent.chores import Aged, Check, Chores, Digest
from hub.agent.desk import Answer, Clarify, Desk, Leave, Route, Sweep, Task
from hub.areas import (
    NOTES_UNREAD,
    Area,
    agent_notes,
    area_rows,
    chores_toml,
    load_areas,
    status,
    wake_row,
    wakes,
)
from hub.backend.codex import MEASURED
from hub.backend.openrouter import OpenRouterError, api_key
from hub.tools import LocalTree, SearXNG, code, criteria, files, khive_ops, shell, web
from lion1.actor import DEFAULT_HANDLERS, Actor, Handler
from lion1.khive import Khive, KhiveError, lit
from lion1.spec import field_keys, spec_name
from lion1.store import Store

from .agent import DEFAULT_MODEL, MAX_TURNS

__all__ = ("CAN", "agent_ids", "agent_path", "agent_row", "wake_of", "Daemon")

# the write routes and library sections this server performs, by the contract's short names; every
# other one is absent from the router
CAN = ("definition", "control", "message", "foryou", "models", "check_provider", "open")

_PROVIDERS = ("claude_code", "codex", "openrouter")
_OPENROUTER = "https://openrouter.ai/api/v1"
_OPENROUTER_MODELS = _OPENROUTER + "/models"  # public: no key
_OPENROUTER_KEY = _OPENROUTER + "/key"  # the key's own limit and usage; an ordinary key reads it
_KEY_SERVICE = "OPENROUTER_API_KEY"  # the keychain item the OpenRouter backend reads (`api_key`)
_SECURITY = "/usr/bin/security"  # absolute: under launchd PATH is /usr/bin:/bin:/usr/sbin:/sbin
_OPEN = "/usr/bin/open"
_DAY_S = 86400.0  # how long a provider's list is kept
_MAX_LIST = 32 << 20  # the most of a provider's answer read
_WAKES = 50  # the wakes one page answers when the app asks for no number
_TIMELINE = 50
_MAIL = 50  # the person's messages one read lists, open and done each
# the profiles the tree builds, by the name each runs under: `Chores.profiles`, `chair` and `console`
_PROFILES = {
    "chores": "a resident's hat for admitted mail: it runs the chores its chores.toml names and reports them",
    "reader": "a resident's hat for mail from senders it does not work for: it says only what arrived",
    "chair": "a chair as the resident: what its trusted senders mail is its own work, answered on the thread",
    "console": "a chat with a person, at a terminal or in the app",
}
# what a resident does not take from the library: it runs a model string and nothing else (`backend_for`)
_UNKEPT = "not a setting this server keeps for a model: a resident takes only the model's name"
_EDITABLE = ("model", "limits.cost_per_day", "wake.at_most_per_hour", "wake.quiet", "instructions.extra")
_RESTART = ("model", "instructions.extra")  # read by `lion agent` at its start; the rest at its next poll
# how a resident's loaders refuse its chores.toml: a value they name, or a table that is no table
_REFUSED = (ValueError, TypeError, AttributeError, re.error)


def _loaders(cfg: dict) -> None:
    """The loaders the resident reloads its chores.toml with, and its start's own checks, run on the
    parsed file: they raise what the resident refuses it with."""
    Budget.load(cfg.get("budget"))
    Wake.load(cfg.get("wake"))
    Gtd.load(cfg.get("gtd"))
    claude_code_options(cfg.get("claude_code"))
    codex_options(cfg.get("codex"))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(local: str) -> str:
    """A local `YYYY-MM-DD HH:MM:SS` stamp, as the residents write theirs, in UTC."""
    t = time.mktime(time.strptime(local, "%Y-%m-%d %H:%M:%S"))
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _no(code: int, message: str, path: str | None = None) -> tuple[int, dict]:
    return code, {
        "ok": False,
        "errors": [{"path": path, "message": message} if path else {"message": message}],
    }


def agent_ids(areas: list[Area]) -> list[tuple[str, Area, str]]:
    """(id, area, directory as the registry writes it) for every resident, in the registry's order."""
    out = []
    for a in areas:
        names = [Path(d).name for d in a.desks]
        for d, name in zip(a.desks, names, strict=True):
            if not name or names.count(name) > 1:
                name += "~" + _mark(d, [o for o, n in zip(a.desks, names, strict=True) if n == name])
            out.append((f"{a.name}/{name}", a, d))
    return out


def _mark(directory: str, same: list[str]) -> str:
    """A directory's digest cut to six characters, or to as many more as it takes for no other
    directory of the same name in the area to share them."""
    mine = hashlib.sha256(str(Path(directory)).encode()).hexdigest()
    others = [hashlib.sha256(str(Path(o)).encode()).hexdigest() for o in same if Path(o) != Path(directory)]
    n = 6
    while n < len(mine) and any(o[:n] == mine[:n] for o in others):
        n += 1
    return mine[:n]


def _model(runtime: str) -> str:
    """The string a resident runs (`claude_code/<model>`, `codex/<model>` or an OpenRouter id) as
    `<provider>/<model id>`; a CLI's own default model is the provider's name alone."""
    return runtime if runtime.partition("/")[0] in ("claude_code", "codex") else f"openrouter/{runtime}"


def _runtime(model: str) -> str | None:
    """`<provider>/<model id>` as the string a resident runs; None for one that names no provider."""
    provider, _, rest = model.partition("/")
    if provider in ("claude_code", "codex"):
        return model
    return rest if provider == "openrouter" and rest else None


def _launched(directory: str) -> str | None:
    """The `--model` its one launchd plist starts it with, which wins over its file; None without."""
    plists = sorted(Path(directory).glob("*.plist"))
    if len(plists) != 1:
        return None
    try:
        with open(plists[0], "rb") as f:
            doc = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
        return None
    argv = doc.get("ProgramArguments") if isinstance(doc, dict) else None
    for i, arg in enumerate(argv if isinstance(argv, list) else []):
        if arg == "--model" and i + 1 < len(argv):
            return str(argv[i + 1])
        if isinstance(arg, str) and arg.startswith("--model="):
            return arg.partition("=")[2]
    return None


def _model_of(directory: str, cfg: dict) -> str:
    return _model(_launched(directory) or str(cfg.get("model") or DEFAULT_MODEL))


def _chair(area: Area, cfg: dict, row: dict) -> bool:
    return "chair" in cfg or row["actor"] == area.chair


def _front(cfg: dict) -> bool:
    desk = cfg.get("desk")
    return isinstance(desk, dict) and bool(desk) and bool(desk.get("front", True))


def _phrase(rule: dict) -> str:
    return f"{rule['phrase']['when']}: {rule['phrase']['then']}"


def _rules(cfg: dict) -> list[dict]:
    """What wakes the resident, as the contract's rules: its `[wake]` mail rules (each read by
    `Wake.load`, as the resident reads them), its chores' ticks, a due task of its `[gtd]` queue, a
    front desk's watch on its owner's box, and the `wake-now` file a person drops in its directory.
    A `drop` rule has no delivery: it is a check that always says no."""
    out: list[dict] = []

    def rule(rid: str, source: dict, deliver: dict | None, when: str, then: str, check: dict | None = None):
        r = {
            "id": rid,
            "on": True,
            "source": source,
            "deliver": deliver,
            "phrase": {"when": when, "then": then},
        }
        out.append(r if check is None else {**r, "check": check})

    wake = Wake.load(cfg.get("wake"))
    window = None
    if wake is not None:
        minutes = wake.batch_after / 60
        window = {
            "kind": "collect",
            "items": wake.batch_size,
            "minutes": int(minutes) if minutes.is_integer() else minutes,
        }
    then = {"now": "wake now", "batch": "collect it for the window", "drop": "drop it"}
    drop = {"kind": "drop", "about": "read and recorded, never woken for"}
    deliver = {"now": {"kind": "now"}, "batch": window, "drop": None}
    for i, r in enumerate(wake.rules if wake is not None else (), 1):
        source: dict[str, Any] = {"kind": "mail"}
        when = "mail"
        if r.senders:
            source["from"] = list(r.senders)
            when += f" from {', '.join(r.senders)}"
        if r.subject is not None:
            source["subject"] = r.subject
            when += f" with a subject matching {r.subject!r}"
        check = drop if r.then == "drop" else None
        if r.spent_over is not None:
            past = f"only once today's cost is past {r.spent_over:.0%} of the day's cap"
            check = {
                **(check or {"kind": "spent_over"}),
                "about": ((check or {}).get("about", "") + "; " if check else "") + past,
                "spent_over": r.spent_over,
            }
        rule(f"rule-{i}", source, deliver[r.then], when + " arrives", then[r.then], check)
    if wake is None:
        rule("mail", {"kind": "mail"}, {"kind": "now"}, "admitted mail arrives", "wake now")
    else:
        rule(
            "default",
            {"kind": "mail"},
            deliver[wake.default],
            "other admitted mail arrives",
            then[wake.default],
            drop if wake.default == "drop" else None,
        )
    if "chair" not in cfg:
        ticks = {
            n: a.get("every")
            for n, a in (cfg.get("chores") or {}).items()
            if isinstance(a, dict) and a.get("every")
        }
        ticks["digest"] = str(cfg["desk"].get("brief", "every:12h")) if _front(cfg) else "daily"
        if cfg.get("steward"):
            ticks["aged"] = cfg.get("aged_every", "every:1h")
        for name, every in ticks.items():
            rule(
                f"tick-{name}",
                {"kind": "schedule", "every": every},
                {"kind": "now"},
                f"the {name} tick fires ({every})",
                "wake now",
            )
    gtd = Gtd.load(cfg.get("gtd"))
    if gtd is not None and gtd.due == "now":
        rule(
            "gtd-due",
            {"kind": "schedule", "queue": "gtd"},
            {"kind": "now"},
            "a task of its queue comes due",
            "wake now",
        )
    if _front(cfg):
        owner = str(cfg.get("owner") or "?")
        rule(
            "watch",
            {"kind": "mail", "box": owner},
            {"kind": "now"},
            f"mail to {owner} the desk has not handled arrives",
            "wake now",
        )
    rule(
        "person",
        {"kind": "file", "name": "wake-now"},
        {"kind": "now"},
        "a person asks for a wake",
        "wake now",
    )
    return out


def _acted(directory: str, day: str) -> int | None:
    """The day's wakes that sent at least one message: the log says nothing else a wake did. None when
    the directory holds no serve.log: the count is in a log this server does not read."""
    if not (Path(directory) / "serve.log").is_file():
        return None
    return sum(
        1 for w in wakes(directory, 1 << 30) if "wake" in w and w["at"].startswith(day) and w["sent"] > 0
    )


def agent_row(area: Area, directory: str, row: dict, ident: str, day: str) -> dict:
    """One resident as the contract's AgentRow, from its `area_rows` row and its own files."""
    cfg = chores_toml(directory)
    controls = row["controls"]
    unread = NOTES_UNREAD in row["problem"]
    if row["problem"] and not row["problem"].startswith("no notes yet"):
        state = "error"
    elif not row["serving"]:
        state = "stopped"
    elif controls["held_by_file"]:
        state = "held"
    else:
        state = "running" if row["in_wake"] else "idle"
    last = (
        f"wake {row['wake']} {row['last_outcome']} at {row['last_at']}" if row["last_at"] else "no wake yet"
    )
    now = {
        "error": row["problem"],
        "stopped": f"stopped; {last}",
        "held": controls["hold_reason"] or "held",
        "running": f"in wake {row['wake']} since {row['last_at']}",
        "idle": last,
    }[state]
    try:
        woken = [_phrase(r) for r in _rules(cfg)]
    except _REFUSED as e:  # the resident refuses this file too, at its start
        woken = [f"chores.toml refused: {e}"]
    out = {
        "id": ident,
        "name": row["actor"] if row["actor"] not in ("", "?") else Path(directory).name,
        "area": area.name,
        "kind": "chair" if _chair(area, cfg, row) else "agent",
        "state": state,
        "now": now,
        "woken_by": woken,
        "model": _model_of(directory, cfg),
        "runs_in": "local",
        "today": {
            # partial, so unknown: the day missed calls of unknown cost, or its row predates their count;
            # notes that are there and do not read leave the whole day unknown
            "cost": row["cost_today"] if row["unknown_today"] == 0 and not unread else None,
            "wakes": None if unread else row["runs_today"],
            "acted": _acted(directory, day),
        },
    }
    spend = _spend(directory)
    if spend.get("day") == day and "calls" in spend:
        out["today"]["calls"] = int(spend["calls"])
    if state == "held" and controls["hold_reason"]:
        out["held_reason"] = controls["hold_reason"]
    return out


def _spend(directory: str) -> dict:
    try:
        spend = agent_notes(directory).get("spend")
    except (KhiveError, ValueError):
        return {}
    return spend if isinstance(spend, dict) else {}


def _outcome(w: dict) -> str:
    if w["outcome"] in ("Success", "Quiet"):
        return "acted" if w["sent"] > 0 else "nothing"
    return "cut" if w["outcome"] in ("Exhausted", "Stopped") else "failed"


def wake_of(w: dict) -> dict:
    """A numbered wake line (`wake_row`) as the contract's Wake. The line says how a wake ended, what it
    took in and spent; which rule fired and the commands it ran are not on it."""
    mail, tasks = w["mail"], w.get("tasks", 0)
    if mail:
        source = {"kind": "mail", "phrase": f"{mail} mail" + (f" and {tasks} task(s)" if tasks else "")}
    elif tasks:
        source = {"kind": "schedule", "phrase": f"{tasks} task(s) of its queue"}
    else:
        source = {"kind": "other", "phrase": "no mail and no task: a watch or a person's wake"}
    return {
        "id": str(w["wake"]),
        "at": _utc(w["at"]),
        "live": False,
        "source": source,
        "turns": w["turns"],
        "cost": None if w.get("unknown") else w["cost"],  # a wake that missed calls of unknown cost
        "outcome": _outcome(w),
        "text": w["raw"][len(w["at"]) + 1 :],
        "commands": [],
    }


def _type(prop: dict) -> str:
    """An argument's type as its JSON schema names it."""
    if "type" in prop:
        return prop["type"] if isinstance(prop["type"], str) else " | ".join(prop["type"])
    alts = prop.get("anyOf") or prop.get("oneOf")
    if alts:
        return " | ".join(_type(a) for a in alts)
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]
    return "any"


class _Nowhere:
    """The khive of the actors `_registered` builds: they are read for their registrations and never run,
    so every row is absent and nothing is written."""

    async def exec(self, ops: str) -> list:
        raise KhiveError("a library actor holds no rows", "not_found")

    def exec_sync(self, ops: str) -> list:
        raise KhiveError("a library actor holds no rows", "not_found")


@cache
def _registered() -> tuple[tuple[str, dict[str, Handler]], ...]:
    """Every command the runtime and this tree register, by group, each read off an actor it was
    registered on: its spec and its effect as the actor holds them. Nothing here calls a command, so
    what a registrar binds for its commands to call (a sandbox, a client, a provider) is never used."""
    groups = []
    store = Store(_Nowhere())
    for group, register in (
        ("files", lambda a: files(a, Path("."))),
        ("code", lambda a: code(a, LocalTree(Path(".")))),
        ("shell", lambda a: shell(a, None)),
        ("khive_ops", lambda a: khive_ops(a, None)),
        ("web", lambda a: web(a, SearXNG("http://localhost"))),
        ("criteria", lambda a: criteria(a, None)),
    ):
        actor = Actor(f"library {group}", store=store)
        groups.append((group, {n: actor.handlers[n] for n in sorted(register(actor))}))
    actor = Actor("library runtime", store=store)
    groups.append(("runtime", {h.name: actor.handlers[h.name] for h in DEFAULT_HANDLERS}))
    # a resident's own commands, registered as `build` registers them: the agent's `send`, the chores
    # and the front desk's
    actor = Actor("library resident", store=store)
    agent = Agent(actor, SimpleNamespace(actor="library", cwd=None), None, lambda mail: None)
    chores = Chores(agent, "library", None, {}, Path("ledger.jsonl"))
    Desk(agent, chores, None, Path("desk.jsonl"))
    for group, specs in (
        ("agent", (Send,)),
        ("chores", (Check, Digest, Aged)),
        ("desk", (Sweep, Answer, Leave, Clarify, Route, Task)),
    ):
        groups.append((group, {spec_name(s): actor.handlers[spec_name(s)] for s in specs}))
    return tuple(groups)


def _groups_of(cfg: dict) -> tuple[str, ...]:
    """The groups a resident's profile holds: a chair's (`chair` in lion_cli.agent) or the chores'
    (`Chores.profiles`), with the front desk's when it has one."""
    if "chair" in cfg:
        return ("files", "khive_ops", "agent", "runtime")
    return ("chores", "desk") if _front(cfg) else ("chores",)


def _tool(name: str, h: Handler, group: str) -> dict:
    """A command as the contract's tool. Its effect is the runtime's own (`Handler.effect`, the order it
    keeps among a turn's commands), `pure` read as `read`: a handler that keeps the default is `write`
    even when it only reads, which says more than it does, never less."""
    schema = h.spec.model_json_schema().get("properties", {})
    keys = field_keys(h.spec)
    args = []
    for fname, f in h.spec.model_fields.items():
        key = str(keys[fname][0])
        args.append(
            {
                "name": key,
                "type": _type(schema.get(key, {})),
                "required": f.is_required(),
                "about": f.description or "",
            }
        )
    doc = (h.spec.__doc__ or "").strip().splitlines()
    return {
        "name": name,
        "group": group,
        "kind": "function",
        "about": doc[0].strip() if doc else "",
        "args": args,
        "effect": "write" if h.effect == "write" else "read",
        "on": True,
    }


def _same(a: Any, b: Any, exact: bool = True) -> bool:
    """Equal all the way down, and of one type when `exact`: `40` is not the float `40.0` a file wrote,
    while a client's JSON may well send either for the other."""
    if not exact and all(isinstance(x, int | float) and not isinstance(x, bool) for x in (a, b)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y, exact) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], exact) for k in a)
    return a == b


_HEADER = re.compile(r"^[ \t]*\[")


def _set_key(text: str, table: str | None, key: str, current: Any, value: str) -> str:
    """`text` with `key` of `[table]` (the top level for None) set to the TOML `value`, every other byte
    kept: the value the file holds (`current`) is found as the shortest span after `key =` that reads
    back as it, so a comment after it, a multi-line string and a value holding `#` all stay whole. A
    missing key goes after the table's last line that is neither blank nor a comment, a missing table
    at the end. The caller reads the result back and refuses one that is not exactly what it asked."""
    lines = text.splitlines(keepends=True)
    head = re.compile(rf"^[ \t]*\[[ \t]*{re.escape(table)}[ \t]*\][ \t]*(?:#.*)?$") if table else None
    if head is None:
        start = -1
    else:
        start = next((i for i, line in enumerate(lines) if head.match(line.rstrip("\r\n"))), None)
        if start is None:
            body = text if not text or text.endswith("\n") else text + "\n"
            return body + ("\n" if body else "") + f"[{table}]\n{key} = {value}\n"
    end = next((j for j in range(start + 1, len(lines)) if _HEADER.match(lines[j])), len(lines))
    at = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=[ \t]*")
    offset = sum(len(line) for line in lines[: start + 1])
    for i in range(start + 1, end):
        m = at.match(lines[i])
        if m is None:
            offset += len(lines[i])
            continue
        p = offset + m.end()
        ends = [k + 1 for k in range(p, len(text)) if text[k] == "\n"] + [len(text)]
        for e in ends:  # the line the value ends on, then the shortest prefix of that line
            if not _reads(text[p:e], current):
                continue
            first = max(p + 1, text.rfind("\n", p, e - 1) + 1)
            cut = next(k for k in range(first, e + 1) if _reads(text[p:k], current))
            return text[:p] + value + text[cut:]
        break
    last = start
    for i in range(start + 1, end):
        body = lines[i].strip()
        if body and not body.startswith("#"):
            last = i
    if last >= 0 and not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, f"{key} = {value}\n")
    return "".join(lines)


def _reads(span: str, current: Any) -> bool:
    try:
        return _same(tomllib.loads("x = " + span)["x"], current)
    except tomllib.TOMLDecodeError:
        return False


def _toml(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a JSON string is a TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


def _flat(doc: Any, prefix: str = "") -> dict[str, Any]:
    """A definition's leaves by dotted path; an editable path and a list are one leaf each."""
    if not isinstance(doc, dict) or prefix in _EDITABLE:
        return {prefix: doc}
    out: dict[str, Any] = {}
    for k, v in doc.items():
        out.update(_flat(v, f"{prefix}.{k}" if prefix else str(k)))
    return out


def _quiet(q: Any) -> list[str] | str:
    """`wake.quiet` as `[wake] quiet`: null is none, `{from, to}` one range; what is wrong, as text."""
    if q is None:
        return []
    if not (isinstance(q, dict) and set(q) == {"from", "to"} and all(isinstance(v, str) for v in q.values())):
        return "wake.quiet is null or {from, to}, each like 23:00"
    return [f"{q['from']}-{q['to']}"]


class Daemon:
    """The v2 routes over one registry, as the person the server runs as (`me`, its `khive`; None
    reads and sends no mail). `run` drives a coroutine to its end on the caller's thread."""

    def __init__(self, registry: Path, me: str | None, khive: Khive | None):
        self.registry = registry
        self.me, self.khive = me, khive
        self.models_path = registry.with_name("models.toml")  # the models the person added, this server's
        self.state = registry.with_name("daemon")  # what this server keeps: the providers' lists
        self.checked: dict[str, dict] = {}  # provider name -> its last check's state, detail, checked_at
        self.lists_lock = threading.Lock()

    # ---- reading the registry ----

    def agents(self) -> list[tuple[str, Area, str, dict]]:
        """(id, area, directory, `area_rows` row) for every resident, read now."""
        areas = load_areas(self.registry)
        found = agent_ids(areas)
        return [(i, a, d, r) for (i, a, d), r in zip(found, area_rows(status(areas)), strict=True)]

    def find(self, ident: str) -> tuple[str, Area, str, dict] | None:
        areas = load_areas(self.registry)
        for i, a, d in agent_ids(areas):
            if i == ident:
                (row,) = area_rows(status([Area(a.name, a.chair, [d])]))
                return i, a, d, row
        return None

    # ---- the routes ----

    def get(self, path: list[str], query: dict, run: Callable) -> tuple[int, dict]:
        """A read route, by its path's segments after `v2`."""
        try:
            if path == ["today"]:
                return 200, self.today(run)
            if len(path) == 2 and path[0] == "areas":
                return self.area(path[1])
            if path[:1] == ["agents"]:
                ident, rest = agent_path(path[1:])
                found = self.find(ident) if ident else None
                if found is None:
                    return _no(404, f"no agent {ident!r} in {self.registry}")
                if rest == []:
                    return 200, self.agent(*found)
                if rest == ["wakes"]:
                    return self.wakes(found[2], query)
                if rest == ["record"]:
                    return self.record(found[2], query)
            if path == ["foryou"]:
                return self.foryou(run)
            if path == ["plugins"]:
                return 200, self.plugins()
            if len(path) == 2 and path[0] == "plugins":
                if path[1] != "lion":
                    return _no(404, f"no plugin {path[1]!r}: lion is the one this server has")
                return 200, self.plugin()
            if path == ["library", "models"]:
                return 200, self.models()
            if len(path) == 4 and path[:2] == ["library", "providers"] and path[3] in ("catalogue", "usage"):
                return self.catalogue(path[2], query) if path[3] == "catalogue" else self.usage(path[2])
            if path == ["profiles"]:
                return 200, {
                    "profiles": [
                        {"name": n, "about": about, "from": "lion"} for n, about in _PROFILES.items()
                    ]
                }
        except (OSError, ValueError) as e:  # a registry mid-write or unreadable: nothing was answered from it
            return _no(500, f"the registry could not be read: {type(e).__name__}: {e}")
        return _no(404, "no route GET /v2/" + "/".join(path))

    def route(self, method: str, path: list[str]) -> Callable[[dict, Callable], tuple[int, dict]] | None:
        """The write route a method and a path's segments after `v2` name, as a function of the body and
        `run`; None for one this server does not serve. The control is the server's (`areas.py`)."""
        if path[:1] == ["agents"]:
            ident, rest = agent_path(path[1:])
            if (method, rest) == ("PUT", ["definition"]):
                return lambda body, run: self.on(ident, lambda found: self.definition(found, body))
            if (method, rest) == ("POST", ["message"]):
                return lambda body, run: self.on(ident, lambda found: self.message(found, body, run))
            return None
        if method == "POST" and len(path) == 3 and path[0] == "foryou":
            if path[2] == "answer":
                return lambda body, run: self.answer(path[1], body, run)
            if path[2] == "done":
                return lambda body, run: self.done(path[1], run)
            return None
        if path[:2] == ["library", "models"]:
            ident = "/".join(path[2:])
            if method == "POST" and not ident:
                return lambda body, run: self.add_model(body)
            if method == "PATCH" and ident:
                return lambda body, run: self.patch_model(ident, body)
            if method == "DELETE" and ident:
                return lambda body, run: self.delete_model(ident)
            return None
        if path[:2] == ["library", "providers"] and len(path) == 4:
            name = path[2]
            if (method, path[3]) == ("POST", "check"):
                return lambda body, run: self.check(name)
            return None
        if (method, path) == ("POST", ["open"]):
            return lambda body, run: self.open(body)
        return None

    def on(
        self, ident: str, act: Callable[[tuple[str, Area, str, dict]], tuple[int, dict]]
    ) -> tuple[int, dict]:
        """`act` on the agent `ident` names; 404 when the registry names none."""
        found = self.find(ident) if ident else None
        if found is None:
            return _no(404, f"no agent {ident!r} in {self.registry}; nothing was done")
        return act(found)

    # ---- today, an area, an agent ----

    def rows(self, found: list[tuple[str, Area, str, dict]], day: str) -> list[tuple[Area, dict]]:
        return [(a, agent_row(a, d, r, i, day)) for i, a, d, r in found]

    def today(self, run: Callable) -> dict:
        day = time.strftime("%Y-%m-%d")
        found = self.agents()
        rows = self.rows(found, day)
        caps = [_cap(d) for _, _, d, _ in found]
        areas = []
        for a in load_areas(self.registry):
            mine = [r for area, r in rows if area.name == a.name]
            areas.append({"name": a.name, **_split(mine), "state": _area_state(mine), "today": _sum(mine)})
        try:
            unread = len(self._box(run, "unread")) if self.khive is not None else None
        except KhiveError:
            unread = None
        return {
            "date": day,
            "totals": {
                "running": sum(1 for _, r in rows if r["state"] == "running"),
                "wakes": _total([r["today"]["wakes"] for _, r in rows]),
                "acted": _total([r["today"]["acted"] for _, r in rows]),
                "cost": _total([r["today"]["cost"] for _, r in rows]),
                "cap": _total(caps),
            },
            "for_you": {"open": unread},
            "areas": areas,
            "daemon": {"version": version("lion1"), "host": socket.gethostname(), "can": list(CAN)},
        }

    def area(self, name: str) -> tuple[int, dict]:
        areas = [a for a in load_areas(self.registry) if a.name == name]
        if not areas:
            return _no(404, f"no area {name!r} in {self.registry}")
        found = [x for x in self.agents() if x[1].name == name]
        mine = [r for _, r in self.rows(found, time.strftime("%Y-%m-%d"))]
        timeline = []
        for i, _, d, _ in found:
            for w in wakes(d, _TIMELINE):
                if "wake" in w:
                    timeline.append(
                        {
                            "at": _utc(w["at"]),
                            "agent": i,
                            "text": w["raw"][len(w["at"]) + 1 :],
                            "raised": False,
                        }
                    )
            for r in _ledger(Path(d) / "ledger.jsonl"):
                if r.get("escalate") is True and isinstance(r.get("ts"), int | float):
                    found_ = "; ".join(str(f) for f in r.get("findings") or [])[:200]
                    at = datetime.fromtimestamp(r["ts"], UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    timeline.append(
                        {
                            "at": at,
                            "agent": i,
                            "text": f"escalated {r.get('chore')}: {found_}",
                            "raised": True,
                        }
                    )
            for r in _ledger(Path(d) / "desk.jsonl"):
                if r.get("class") == "escalated" and isinstance(r.get("utc"), str):
                    at = r["utc"][:19] + "Z"
                    text = f"escalated mail from {r.get('from')}: {r.get('subject')}"
                    timeline.append({"at": at, "agent": i, "text": text, "raised": True})
        timeline.sort(key=lambda t: t["at"], reverse=True)
        return 200, {"name": name, **_split(mine), "today": _sum(mine), "timeline": timeline[:_TIMELINE]}

    def agent(self, ident: str, area: Area, d: str, row: dict) -> dict:
        """The agent; a chores.toml its own loaders refuse has no definition and nothing editable, and
        `refused` says why: what a write of it would be refused for."""
        cfg = chores_toml(d)
        try:
            _loaders(cfg)
            definition, editable, refused = self._definition(area, d, cfg, row), self._editable(d, cfg), None
        except _REFUSED as e:
            definition, editable, refused = None, [], str(e)
        return {
            "row": agent_row(area, d, row, ident, time.strftime("%Y-%m-%d")),
            "definition": definition,
            "editable": editable,
            "refused": refused,
            "files": {"instance": str(Path(d) / "chores.toml")},
        }

    def _editable(self, d: str, cfg: dict) -> list[str]:
        out = list(_EDITABLE)
        if _launched(d) is not None:  # its plist's --model wins over the file at every start
            out.remove("model")
        if len((cfg.get("wake") or {}).get("quiet") or []) > 1:  # several ranges: the app's shape holds one
            out.remove("wake.quiet")
        if not isinstance(cfg.get("chair"), dict):  # only a chair carries instructions of its own
            out.remove("instructions.extra")
        return out

    def _definition(self, area: Area, d: str, cfg: dict, row: dict) -> dict:
        budget = Budget.load(cfg.get("budget"))
        quiet = (cfg.get("wake") or {}).get("quiet") or []
        q = re.fullmatch(r"\s*(\d\d:\d\d)\s*-\s*(\d\d:\d\d)\s*", str(quiet[0])) if quiet else None
        chair = isinstance(cfg.get("chair"), dict)
        names = dict(_registered())
        return {
            "name": row["actor"],
            "area": area.name,
            "built_from": {"kind": "blank"},
            "runs_in": "local",
            "model": _model_of(d, cfg),
            "limits": {
                "cost_per_day": budget.cost_per_day,
                "turns_per_wake": MAX_TURNS,
                "time_per_wake_s": None,
                "fold_at_tokens": None,
                "escalates_to": cfg.get("steward") or cfg.get("owner"),
            },
            "instructions": {
                "from_role": "chair" if chair else "desk" if _front(cfg) else "chores",
                "extra": str(cfg["chair"].get("system", "")) if chair else "",
            },
            "wake": {
                "rules": _rules(cfg),
                "at_most_per_hour": budget.wakes_per_hour,
                "quiet": {"from": q[1], "to": q[2]} if q else None,
                "at_cap": "hold",
                "run": "continuous" if chair else "fresh",
            },
            "plugins": [{"name": "lion", "items": [n for g in _groups_of(cfg) for n in names[g]]}],
            "shell_rules": None,
        }

    def wakes(self, d: str, query: dict) -> tuple[int, dict]:
        try:
            limit = int(query.get("limit") or _WAKES)
            before = int(query["before"]) if query.get("before") else None
        except ValueError:
            return _no(400, "limit and before are whole numbers")
        if not 0 < limit <= 500:
            return _no(400, "limit is 1 to 500")
        rows = [w for w in wakes(d, 1 << 30) if "wake" in w and (before is None or w["wake"] < before)]
        return 200, {"wakes": [wake_of(w) for w in rows[:limit]], "more": len(rows) > limit}

    def record(self, d: str, query: dict) -> tuple[int, dict]:
        """The serve.log's lines, each with its number in the file: after a number, or the lines of one
        wake, from the line after the wake before it (or the process's start) to its own."""
        try:
            lines = (Path(d) / "serve.log").read_text(errors="replace").splitlines()
        except FileNotFoundError:
            lines = []
        try:
            after = int(query["after"]) if query.get("after") else -1
            wake = int(query["wake"]) if query.get("wake") else None
        except ValueError:
            return _no(400, "after and wake are whole numbers")
        rows = []
        for seq, line in enumerate(lines):
            w = wake_row(line)
            rows.append(
                {"seq": seq, "at": _utc(w["at"]) if w else None, "line": line, "wake": (w or {}).get("wake")}
            )
        if wake is not None:
            ends = [
                k for k, r in enumerate(rows) if r["wake"] is not None or r["line"].startswith("serving ")
            ]
            mine = next((k for k in ends if rows[k]["wake"] == wake), None)
            if mine is None:
                return _no(404, f"no wake {wake} in the log")
            first = max((k for k in ends if k < mine), default=-1) + 1
            rows = rows[first : mine + 1]
        return 200, {"rows": [{k: v for k, v in r.items() if k != "wake"} for r in rows if r["seq"] > after]}

    # ---- the definition ----

    def definition(self, found: tuple[str, Area, str, dict], body: dict) -> tuple[int, dict]:
        ident, area, d, row = found
        want, dry = body.get("definition"), body.get("dry_run") is True
        if not isinstance(want, dict):
            return _no(400, "definition is an object; nothing was written", "definition")
        path = Path(d) / "chores.toml"
        try:
            raw = path.read_bytes()
            text = raw.decode()
            before = tomllib.loads(text)
        except FileNotFoundError:
            return _no(409, f"no chores.toml in {d}; nothing was written")
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            return _no(409, f"chores.toml does not read, nothing was written: {e}")
        try:
            _loaders(before)
            current = self._definition(area, d, before, row)
        except _REFUSED as e:
            return _no(409, f"chores.toml is refused by its own loaders, nothing was written: {e}")
        editable = self._editable(d, before)
        have = _flat(current)
        errors, changes, edits = [], [], []
        for p, value in _flat(want).items():
            if p not in have:
                errors.append({"path": p, "message": "no such field in a definition"})
            elif _same(value, have[p], exact=False):
                continue
            elif p not in editable:
                errors.append({"path": p, "message": "read-only for this agent"})
            else:
                edit = self._edit(p, value, before)
                if isinstance(edit, str):
                    errors.append({"path": p, "message": edit})
                else:
                    edits.append(edit)
                    changes.append({"path": p, "old": have[p], "new": value})
        if errors:
            return 400, {"ok": False, "errors": errors}
        expected = json.loads(json.dumps(before, default=str))
        new = text
        for table, key, value in edits:
            new = _set_key(
                new,
                table,
                key,
                (before.get(table) or {}).get(key) if table else before.get(key),
                _toml(value),
            )
            (expected.setdefault(table, {}) if table else expected)[key] = value
        try:
            after = tomllib.loads(new)
        except tomllib.TOMLDecodeError:
            after = None
        if after is None or json.loads(json.dumps(after, default=str)) != expected:
            return _no(409, "chores.toml could not be rewritten in place; nothing was written")
        try:
            _loaders(after)
        except _REFUSED as e:
            return _no(400, f"chores.toml would be refused: {e}; nothing was written")
        restart = any(c["path"] in _RESTART for c in changes)
        if dry or not changes:
            return 200, {"ok": True, "changes": changes, "needs_restart": restart}
        if path.read_bytes() != raw:  # written by someone else while this was read: theirs stands
            return _no(409, "chores.toml changed while this was read; nothing was written")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(new.encode())
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
        return 200, {"ok": True, "changes": changes, "needs_restart": restart}

    def _edit(self, p: str, value: Any, cfg: dict) -> tuple[str | None, str, Any] | str:
        """(table, key, value) of chores.toml for one changed editable path; what is wrong, as text."""
        number = isinstance(value, int | float) and not isinstance(value, bool)
        if p == "model":
            runtime = _runtime(value) if isinstance(value, str) else None
            if runtime is None:
                return "a model is <provider>/<model id>, the provider one of " + ", ".join(_PROVIDERS)
            known = {m["id"] for m in self.models()["models"]}
            if value not in known:
                return f"{value} is not a model of the library; add it first"
            return None, "model", runtime
        if p == "limits.cost_per_day":
            if not number or not _finite(value) or value < 0:
                return "a number of dollars, finite and >= 0"
            return "budget", "cost_per_day", float(value)
        if p == "wake.at_most_per_hour":
            if not number or not _finite(value) or not float(value).is_integer() or value < 1:
                return "a whole number >= 1 (hold stops wakes)"
            return "budget", "wakes_per_hour", int(value)
        if p == "wake.quiet":
            quiet = _quiet(value)
            return quiet if isinstance(quiet, str) else ("wake", "quiet", quiet)
        if not isinstance(value, str):
            return "a string"
        return "chair", "system", value

    # ---- mail: to an agent, and the person's ----

    def message(self, found: tuple[str, Area, str, dict], body: dict, run: Callable) -> tuple[int, dict]:
        ident, _, _, row = found
        text, images = body.get("text"), body.get("images")
        if images not in (None, []):
            return _no(
                400, "a message to an agent is mail, and mail carries text only; nothing was sent", "images"
            )
        if not isinstance(text, str) or not text.strip():
            return _no(400, "text is empty; nothing was sent", "text")
        if self.khive is None:
            return _no(409, "no identity: start lion areas with --as DIR; nothing was sent")
        if row["actor"] in ("?", ""):
            return _no(409, f"the agent's actor could not be read: {row['problem']}; nothing was sent")
        try:
            sent = run(self.khive.send(row["actor"], text, idempotency_key="ui:" + body["once"]))
        except KhiveError as e:
            return _no(502, str(e))
        return 200, {"ok": True, "id": str((sent or {}).get("id") or "")}

    def _box(self, run: Callable, which: str) -> list[dict]:
        """The person's mail: the unread whole, a page at a time until a short page or one that adds
        nothing new; the sent, its newest page."""
        out: dict[str, dict] = {}
        while True:
            args = f'box="sent", limit={_MAIL}' if which == "sent" else f'status="{which}", limit={_MAIL}'
            (page,) = run(self.khive.exec(f"comm.inbox({args}{f', offset={len(out)}' if out else ''})"))
            mail = page.get("messages", []) if isinstance(page, dict) else None
            if not isinstance(mail, list):
                raise KhiveError(f"comm.inbox answered {type(mail).__name__} for messages, not a list")
            new = {str(m.get("id")): m for m in mail if isinstance(m, dict) and str(m.get("id")) not in out}
            out.update(new)
            if which != "unread" or len(mail) < _MAIL or not new:
                return list(out.values())

    def _items(self, run: Callable) -> list[dict]:
        """The person's unread mail as For you rows, each open: read mail is not listed. A message on a
        thread the person wrote on is a reply, any other a note. Mail carries no choices."""
        senders = {}
        for i, _, d in agent_ids(load_areas(self.registry)):
            actor = chores_toml(d).get("actor")
            if isinstance(actor, str):
                senders.setdefault(actor, i)
        mine = {(m.get("properties") or {}).get("thread_id") for m in self._box(run, "sent")} - {None}
        items = []
        for m in self._box(run, "unread"):
            p = m.get("properties") or {}
            sender = str(p.get("from_actor") or m.get("from") or "?")
            ident = senders.get(sender)
            content = str(m.get("content") or "")
            at = str(m.get("created_at") or "")
            try:
                at = (
                    datetime.fromisoformat(at.replace("Z", "+00:00"))
                    .astimezone(UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
                )
            except ValueError:
                pass
            items.append(
                {
                    "id": str(m.get("full_id") or m.get("id")),
                    "kind": "reply" if p.get("thread_id") in mine else "note",
                    "open": True,
                    "title": str(m.get("subject") or "") or content.split("\n", 1)[0][:120],
                    "body": content,
                    "at": at,
                    "from": {
                        "agent": ident or sender,
                        "area": ident.partition("/")[0] if ident else None,
                    },
                    "choices": [],
                    "links": [{"label": "agent", "target": {"page": "agent", "ref": ident}}] if ident else [],
                    "_to": sender,
                    "_thread": p.get("thread_id"),
                }
            )
        items.sort(key=lambda x: x["at"], reverse=True)
        return items

    def foryou(self, run: Callable) -> tuple[int, dict]:
        if self.khive is None:
            return 200, {"items": []}
        try:
            items = self._items(run)
        except KhiveError as e:
            return _no(502, f"the person's mail could not be read: {e}")
        return 200, {"items": [_public(x) for x in items]}

    def _item(self, ident: str, run: Callable) -> tuple[int, dict] | dict:
        if self.khive is None:
            return _no(409, "no identity: start lion areas with --as DIR; nothing was done")
        try:
            items = self._items(run)
        except KhiveError as e:
            return _no(502, f"the person's mail could not be read: {e}; nothing was done")
        found = next((x for x in items if x["id"] == ident), None)
        return found if found is not None else _no(404, f"no item {ident!r} for {self.me}; nothing was done")

    def answer(self, ident: str, body: dict, run: Callable) -> tuple[int, dict]:
        item = self._item(ident, run)
        if isinstance(item, tuple):
            return item
        if body.get("choice") not in (None, ""):
            return _no(400, "this item offers no choices: it is mail; answer it with text", "choice")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _no(400, "text is empty; nothing was sent", "text")
        title = item["title"]
        try:
            run(
                self.khive.send(
                    item["_to"],
                    text,
                    subject=title if title.lower().startswith("re:") else f"Re: {title}",
                    thread_id=item["_thread"],
                    idempotency_key="ui:" + body["once"],
                )
            )
        except KhiveError as e:
            return _no(502, f"{e}; nothing was sent")
        try:
            run(self.khive.exec(f"comm.read(ids={lit([ident])})"))
        except KhiveError as e:
            return _no(502, f"sent to {item['_to']}, and the item could not be marked done: {e}")
        return 200, {"ok": True, "item": {**_public(item), "open": False}}

    def done(self, ident: str, run: Callable) -> tuple[int, dict]:
        item = self._item(ident, run)
        if isinstance(item, tuple):
            return item
        try:
            run(self.khive.exec(f"comm.read(ids={lit([ident])})"))
        except KhiveError as e:
            return _no(502, f"the item could not be marked done: {e}")
        return 200, {"ok": True}

    # ---- the library ----

    def plugin(self) -> dict:
        """The default plugin, `lion`: the commands the runtime and this tree register, the profiles a
        resident or a chat runs under, and no skills, since every instruction text the tree has is a
        profile's system prompt, given whole at the start and never loaded on demand."""
        cfgs = [(i, chores_toml(d)) for i, _, d in agent_ids(load_areas(self.registry))]
        used = {
            "chair": sum(1 for _, c in cfgs if "chair" in c),
            "chores": sum(1 for _, c in cfgs if "chair" not in c),
        }
        return {
            "name": "lion",
            "about": "what ships with lion: the runtime's own commands, this tree's tools and its profiles",
            "source": "default",
            "kind": "plugin",
            "on": True,
            "agents": [
                {"name": n, "about": about, "in_use": used.get(n, 0)} for n, about in _PROFILES.items()
            ],
            "skills": [],
            "tools": [_tool(n, h, group) for group, handlers in _registered() for n, h in handlers.items()],
            "not_used": [],
            "given_to": [i for i, _ in cfgs],
        }

    def plugins(self) -> dict:
        return {"plugins": [self.plugin()], "marketplaces": [], "shell_rules": []}

    def _store(self) -> dict[str, dict]:
        """`models.toml`: the models the person added, by id. None of it is a secret."""
        try:
            doc = tomllib.loads(self.models_path.read_text())
        except FileNotFoundError:
            return {}
        table = doc.get("model", {})
        if not isinstance(table, dict) or not all(isinstance(v, dict) for v in table.values()):
            raise ValueError(f"{self.models_path}: [model.<id>] tables")
        return table

    def _save(self, store: dict[str, dict]) -> None:
        lines = ["# the models added in the app; written whole by `lion areas --serve`\n"]
        for ident, m in store.items():
            lines.append(f"\n[model.{json.dumps(ident, ensure_ascii=False)}]\n")
            lines += [f"{k} = {_toml(v)}\n" for k, v in m.items()]
        text = "".join(lines)
        if tomllib.loads(text).get("model", {}) != store:
            raise ValueError("the models could not be written as TOML")
        self.models_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.models_path.with_name(
            f".{self.models_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(text)
        os.replace(tmp, self.models_path)

    def _key(self) -> tuple[bool | None, str]:
        """Whether the keychain holds the OpenRouter key the backend reads, never its value, and why
        when that could not be asked."""
        account = os.environ.get("OPENROUTER_KEYCHAIN_ACCOUNT", "khive")
        try:
            proc = subprocess.run(
                [_SECURITY, "find-generic-password", "-a", account, "-s", _KEY_SERVICE],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"the keychain could not be asked: {type(e).__name__}: {e}"
        if proc.returncode in (0, 44):  # 44: no such item
            return proc.returncode == 0, ""
        return None, f"the keychain could not be asked: security exited {proc.returncode}"

    def providers(self) -> list[dict]:
        out = []
        for name in ("claude_code", "codex"):
            cli = shutil.which("claude" if name == "claude_code" else "codex")
            p = {
                "name": name,
                "kind": name,
                "state": "ok" if cli else "off",
                "detail": f"the CLI at {cli}, on its own sign-in" if cli else "no CLI on this server's PATH",
                "signin": {"state": "unknown", "detail": "this server does not ask the CLI for its sign-in"},
                "limits": None,
            }
            out.append(p)
        present, why = self._key()
        account = os.environ.get("OPENROUTER_KEYCHAIN_ACCOUNT", "khive")
        detail = (
            why
            or (
                f"the key is in the keychain ({_KEY_SERVICE}, account {account})"
                if present
                else f"no key in the keychain ({_KEY_SERVICE}, account {account})"
            )
        ) + "; a process with OPENROUTER_API_KEY in its environment uses that instead"
        p = {
            "name": "openrouter",
            "kind": "openrouter",
            "state": "warn" if present is None else "ok" if present else "off",
            "detail": detail,
            "address": _OPENROUTER,
            "limits": None,
        }
        if present is not None:
            p["key"] = {"set": present}
        out.append(p)
        # `check`: whether `check` below asks this provider anything
        return [{**p, "check": p["name"] != "claude_code", **self.checked.get(p["name"], {})} for p in out]

    def models(self, extra: dict | None = None) -> dict:
        """The models of the library: those added (`models.toml`), those a resident runs and the one a
        resident runs when nothing names one, which is `default_for_new`. `extra` adds rows not yet
        written, for a dry run."""
        providers = {p["name"]: p for p in self.providers()}
        store = {**self._store(), **(extra or {})}
        default = _model(DEFAULT_MODEL)
        listed = self._cached()
        by_id = {m["id"]: m for m in (listed or {}).get("data", []) if isinstance(m, dict) and "id" in m}
        used: dict[str, list[str]] = {}
        for i, _, d in agent_ids(load_areas(self.registry)):
            used.setdefault(_model_of(d, chores_toml(d)), []).append(i)
        out = []
        for ident in dict.fromkeys([*store, *used, default]):
            m = store.get(ident, {})
            through, _, model_id = ident.partition("/")
            listing = by_id.get(model_id) if through == "openrouter" else None
            row = {
                "id": ident,
                "shown_as": m.get("shown_as") or (listing or {}).get("name") or ident,
                "through": through,
                "model_id": model_id,
                "state": (providers.get(through) or {}).get("state", "off"),
                "on_plan": through != "openrouter",
                "price": _price(listing) if listing else None,
                "context": (listing or {}).get("context_length"),
                "on_failure": "the wake fails and its owner is told",
                "default_for_new": ident == default,
                "images": "image" in ((listing or {}).get("architecture") or {}).get("input_modalities", [])
                if listing
                else None,
                "given_to": used.get(ident, []),
                "limits": None,
            }
            out.append(row)
        return {"providers": list(providers.values()), "models": out}

    def _cached(self, fetch: bool = False) -> dict | None:
        """OpenRouter's list as this server keeps it: `{fetched_at, data}`, read again from the provider
        once a day old when `fetch`; a list that cannot be fetched leaves the one kept, whatever its age."""
        path = self.state / "openrouter-models.json"
        with self.lists_lock:
            try:
                kept = json.loads(path.read_text())
            except (OSError, ValueError):
                kept = None
            stale = kept is None or time.time() - kept.get("at", 0) >= _DAY_S
            if not fetch or not stale:
                return kept
            body = _get(_OPENROUTER_MODELS, {})
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list):
                raise ValueError("OpenRouter's list has no data")
            kept = {"at": time.time(), "fetched_at": _now(), "data": data}
            self.state.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(kept))
            os.replace(tmp, path)
            return kept

    def catalogue(self, name: str, query: dict) -> tuple[int, dict]:
        if name != "openrouter":
            return _no(404, f"{name!r} has no list this server reads")
        try:
            kept = self._cached(fetch=True)
        except (OSError, ValueError, urllib.error.URLError) as e:
            kept = self._cached()
            if kept is None:
                return _no(502, f"OpenRouter's list could not be fetched: {e}")
        q = str(query.get("q") or "").lower()
        models = [
            {
                "model_id": m["id"],
                "name": str(m.get("name") or m["id"]),
                "context": m.get("context_length"),
                "price": _price(m),
                "takes": list((m.get("architecture") or {}).get("input_modalities") or []),
            }
            for m in kept["data"]
            if isinstance(m, dict)
            and isinstance(m.get("id"), str)
            and (q in m["id"].lower() or q in str(m.get("name") or "").lower())
        ]
        return 200, {"fetched_at": kept["fetched_at"], "models": models}

    def _openrouter_key(self) -> tuple[int, dict] | dict:
        """OpenRouter's own word on the key the backend would use (`api_key`: the environment, then the
        keychain): its `data`, or the refusal."""
        try:
            key = api_key()
        except OpenRouterError as e:
            return _no(409, str(e))
        try:
            body = _get(_OPENROUTER_KEY, {"Authorization": f"Bearer {key}"})
        except urllib.error.HTTPError as e:
            return _no(409 if e.code in (401, 403) else 502, f"OpenRouter answered HTTP {e.code} for the key")
        except (OSError, ValueError, urllib.error.URLError) as e:
            return _no(502, f"OpenRouter could not be asked: {type(e).__name__}: {e}")
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else _no(502, "OpenRouter's answer for the key has no data")

    def usage(self, name: str) -> tuple[int, dict]:
        """OpenRouter's figures for the key, as it reports them: the day and the month in UTC, and what
        the key's own limit leaves. Its per-day log (`/activity`) needs a management key, so `log` is
        empty for the ordinary key the backend uses."""
        if name != "openrouter":
            return _no(404, f"{name!r} reports no usage this server reads")
        data = self._openrouter_key()
        if isinstance(data, tuple):
            return data
        out: dict[str, Any] = {"fetched_at": _now()}
        for field, theirs in (
            ("credits_left", "limit_remaining"),
            ("used_today", "usage_daily"),
            ("used_month", "usage_monthly"),
        ):
            v = data.get(theirs)
            if isinstance(v, int | float) and not isinstance(v, bool):
                out[field] = v
        out["log"] = []
        return 200, out

    def check(self, name: str) -> tuple[int, dict]:
        """Ask the provider now. OpenRouter: one call with the key (`/key`). codex: the CLI's release, by
        the command its backend asks before any call (`codex --version`), against the releases the
        backend runs on; its sign-in is not asked. claude_code: nothing this server asks it was
        verified to make no model call, so it has no check."""
        if name == "claude_code":
            return _no(404, "claude_code has no check this server makes")
        if name == "codex":
            cli = shutil.which("codex")
            if cli is None:
                state, detail = "off", "no codex CLI on this server's PATH"
            else:
                try:
                    proc = subprocess.run(
                        [cli, "--version"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    found = re.search(r"\d+\.\d+\.\d+[^\s]*", proc.stdout) if proc.returncode == 0 else None
                except (OSError, subprocess.TimeoutExpired) as e:
                    proc, found = None, None
                    printed = f"{type(e).__name__}: {e}"
                else:
                    printed = " ".join((proc.stdout + proc.stderr).split())[:200]
                if found is None:
                    state, detail = "warn", f"codex --version printed no release ({printed})"
                elif found.group() in MEASURED:
                    state, detail = "ok", f"codex-cli {found.group()}, a release its backend runs on"
                else:
                    state = "warn"
                    detail = (
                        f"codex-cli {found.group()} is not a release its backend runs on "
                        f"({', '.join(MEASURED)}): every wake on it is refused"
                    )
            self.checked[name] = {"state": state, "detail": detail, "checked_at": _now()}
            return 200, {"ok": True, "provider": next(p for p in self.providers() if p["name"] == name)}
        if name != "openrouter":
            return _no(404, f"no provider {name!r}")
        data = self._openrouter_key()
        if isinstance(data, tuple):
            code, doc = data
            state = "warn" if code == 502 else "off"
            detail = doc["errors"][0]["message"]
        else:
            state, detail = "ok", "OpenRouter accepted the key"
            if isinstance(data.get("limit_remaining"), int | float):
                detail += f"; ${data['limit_remaining']} left of the key's limit"
        self.checked[name] = {"state": state, "detail": detail, "checked_at": _now()}
        provider = next(p for p in self.providers() if p["name"] == name)
        return 200, {"ok": True, "provider": provider}

    def add_model(self, body: dict) -> tuple[int, dict]:
        through, model_id = body.get("through"), body.get("model_id")
        if through not in _PROVIDERS:
            return _no(400, "through is one of " + ", ".join(_PROVIDERS), "through")
        if (
            not isinstance(model_id, str)
            or not re.fullmatch(r"[\x21-\x7e]*", model_id)
            or (through == "openrouter" and not model_id)
        ):
            return _no(400, "model_id is the provider's id for it, with no space", "model_id")
        bad = sorted(set(body) - {"once", "through", "model_id", "shown_as", "dry_run"})
        if bad:
            return _no(400, f"{', '.join(bad)}: {_UNKEPT}", bad[0])
        ident = f"{through}/{model_id}" if model_id else through
        shown = _shown_as(body)
        if isinstance(shown, tuple):
            return shown
        if through == "openrouter":  # an id OpenRouter does not list fails every wake: refused here instead
            try:
                kept = self._cached(fetch=True)
            except (OSError, ValueError, urllib.error.URLError) as e:
                return _no(502, f"OpenRouter's list could not be read to check {model_id}: {e}")
            if not any(isinstance(m, dict) and m.get("id") == model_id for m in kept["data"]):
                return _no(400, f"OpenRouter's list has no model {model_id!r}", "model_id")
        store = self._store()
        if ident in store:
            return _no(409, f"{ident} is already in the library")
        entry = {"through": through, "model_id": model_id, **shown}
        if body.get("dry_run") is not True:
            self._save({**store, ident: entry})
        model = next(m for m in self.models(extra={ident: entry})["models"] if m["id"] == ident)
        # check: null, as no test call is made (`test_model` is not served)
        return 200, {"ok": True, "model": model, "check": None}

    def patch_model(self, ident: str, body: dict) -> tuple[int, dict]:
        known = {m["id"]: m for m in self.models()["models"]}
        if ident not in known:
            return _no(404, f"no model {ident!r} in the library")
        bad = sorted(set(body) - {"once", "shown_as"})
        if bad:
            return _no(
                400,
                f"{', '.join(bad)}: "
                + ("the provider's, read-only" if bad[0] in ("price", "context") else _UNKEPT),
                bad[0],
            )
        shown = _shown_as(body)
        if isinstance(shown, tuple):
            return shown
        store = self._store()
        through, _, model_id = ident.partition("/")
        store[ident] = {**store.get(ident, {"through": through, "model_id": model_id}), **shown}
        self._save(store)
        return 200, {"ok": True, "model": next(m for m in self.models()["models"] if m["id"] == ident)}

    def delete_model(self, ident: str) -> tuple[int, dict]:
        store = self._store()
        used = [m for m in self.models()["models"] if m["id"] == ident and m["given_to"]]
        if used:
            return _no(409, f"{ident} is the model of {', '.join(used[0]['given_to'])}; nothing was removed")
        if ident not in store:
            return _no(404, f"no model {ident!r} was added to the library")
        self._save({k: v for k, v in store.items() if k != ident})
        return 200, {"ok": True}

    # ---- open ----

    def open(self, body: dict) -> tuple[int, dict]:
        """A file or a folder under a directory the registry names, in the editor or the Finder."""
        raw, how = body.get("path"), body.get("with")
        if how not in ("editor", "finder"):
            return _no(400, "with is editor or finder", "with")
        if not isinstance(raw, str) or not raw:
            return _no(400, "path names a file or a folder", "path")
        try:
            target = Path(raw).expanduser().resolve()
            roots = [Path(d).expanduser().resolve() for a in load_areas(self.registry) for d in a.desks]
        except RuntimeError:  # a link that loops on itself, which 3.12 and older raise on
            return _no(404, f"{raw} does not resolve to a file or a folder; nothing was opened", "path")
        if not any(target == r or r in target.parents for r in roots):
            return _no(404, f"{raw} is under no directory the registry names; nothing was opened", "path")
        if not target.exists():
            return _no(404, f"{raw} does not exist; nothing was opened", "path")
        if how == "editor" and target.is_dir():
            return _no(400, "a folder opens in the Finder", "with")
        argv = [_OPEN, "-R" if how == "finder" else "-t", str(target)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            return _no(502, f"open: {type(e).__name__}: {e}")
        if proc.returncode != 0:
            return _no(
                502, f"open exited {proc.returncode}: {' '.join((proc.stderr or proc.stdout).split())}"
            )
        return 200, {"ok": True}


def agent_path(segments: list[str]) -> tuple[str, list[str]]:
    """An agent's id and what follows it: `<area>/<name>` as two segments, or one that held `%2F`."""
    if not segments:
        return "", []
    if "/" in segments[0]:
        return segments[0], segments[1:]
    if len(segments) < 2:
        return "", []
    return f"{segments[0]}/{segments[1]}", segments[2:]


def _cap(d: str) -> float | None:
    """The day's cap the resident reads from its chores.toml; None for a file or a `[budget]` its
    loaders refuse, since the cap it runs under is then not known here."""
    try:
        return Budget.load(tomllib.loads((Path(d) / "chores.toml").read_text()).get("budget")).cost_per_day
    except (OSError, ValueError, TypeError, AttributeError):  # a TOMLDecodeError is a ValueError
        return None


def _finite(value: int | float) -> bool:
    """Whether a number of a body is one a float holds: a whole number past a float's range is not."""
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _total(figures: list[float | None]) -> float | None:
    """A sum of the day's figures, rounded as the page prints it; None when one of them is unknown."""
    return None if any(f is None for f in figures) else round(sum(figures), 4)


def _split(rows: list[dict]) -> dict:
    chairs = [r for r in rows if r["kind"] == "chair"]
    return {
        "chair": chairs[0] if chairs else None,
        "agents": [r for r in rows if r is not (chairs[0] if chairs else None)],
    }


def _sum(rows: list[dict]) -> dict:
    return {
        "cost": _total([r["today"]["cost"] for r in rows]),
        "wakes": _total([r["today"]["wakes"] for r in rows]),
    }


def _area_state(rows: list[dict]) -> str:
    states = {r["state"] for r in rows}
    return next((s for s in ("error", "running", "idle", "held") if s in states), "stopped")


def _ledger(path: Path) -> list[dict]:
    try:
        text = path.read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def _shown_as(body: dict) -> dict | tuple[int, dict]:
    if "shown_as" not in body:
        return {}
    if not isinstance(body["shown_as"], str):
        return _no(400, "shown_as is a string", "shown_as")
    return {"shown_as": body["shown_as"]}


def _price(m: dict) -> dict | None:
    """OpenRouter's price per token, as dollars per million tokens; a price it does not fix (a router's,
    given as -1) or cannot be read is null."""
    pricing = m.get("pricing") or {}
    try:
        prompt, completion = Decimal(str(pricing["prompt"])), Decimal(str(pricing["completion"]))
    except (KeyError, InvalidOperation):
        return None
    if prompt < 0 or completion < 0:
        return None
    return {"in": float(prompt * 1_000_000), "out": float(completion * 1_000_000)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error and is never followed: a followed one carries the request's headers, a
    key among them, to whatever address it names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_FETCH = urllib.request.build_opener(_NoRedirect)


def _get(url: str, headers: dict) -> Any:
    """A JSON GET that follows no redirect; the answer read to `_MAX_LIST` bytes at most."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        resp = _FETCH.open(req, timeout=30)
    except urllib.error.HTTPError as e:
        e.close()  # the refusal's code and reason outlive its socket
        raise
    with resp:
        raw = resp.read(_MAX_LIST + 1)
    if len(raw) > _MAX_LIST:
        raise ValueError(f"the answer is over {_MAX_LIST} bytes")
    return json.loads(raw)
