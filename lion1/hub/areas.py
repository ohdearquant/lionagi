"""Areas, chairs and their residents, read for a person: how many long-running agents there are,
whose area each is in, whether it is serving, whether it is in a wake now, what it did today.

Each area has a chair, and the chair's residents (its desk, its chores) work its inbox. The registry
is one TOML file, `areas.toml`; each row here is read from what the resident itself wrote (its
`chores.toml`, its notes, its pid file), nothing computed that it did not write (ADR-0016/C4).
Serving is the pid file's lock, the same lock `serve` holds."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import html
import json
import math
import os
import plistlib
import re
import subprocess
import time
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from xml.parsers.expat import ExpatError

from lion1.actor import reported_cost
from lion1.khive import Khive, KhiveError, actor_name, lit
from lion1.store import KhiveNoteStore, Store

__all__ = (
    "DEFAULT_REGISTRY",
    "NOTES_UNREAD",
    "partial",
    "day_spend",
    "Area",
    "Resident",
    "load_areas",
    "resident",
    "status",
    "table",
    "page",
    "controls_info",
    "chores_toml",
    "budget_table",
    "wake_table",
    "waiting_mail",
    "area_rows",
    "rows_json",
    "wake_row",
    "wakes",
    "log_tail",
    "identity",
    "talk",
    "resident_page",
    "resident_json",
)

DEFAULT_REGISTRY = Path.home() / ".lionagi" / "areas.toml"
_PGREP = "/usr/bin/pgrep"

_STYLE = (
    "<style>body{font:14px/1.4 -apple-system,Helvetica,sans-serif;margin:2em;color:#222}"
    "table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.35em .6em;"
    "border-bottom:1px solid #ddd;vertical-align:top}th{background:#f4f4f4}"
    ".on{color:#1a7f37;font-weight:600}.wake{color:#9a6700;font-weight:600}.off{color:#999}"
    ".bad{color:#b00}.area{background:#fafafa;font-weight:600}small{color:#666}"
    ".restart td{color:#888;font-size:90%}.note{color:#888}.msg{border-bottom:1px solid #eee;padding:.4em 0}"
    "pre{margin:.3em 0;white-space:pre-wrap;overflow-wrap:anywhere}details pre{max-height:24em;overflow:auto}"
    "</style>"
)

# the line `serve` prints after each wake (hub/agent/agent.py); the tail after ` wake ` is optional so
# a wake line of another shape (a failed wake, a field printed as None) still matches and is kept whole.
# The context the last turn stood in, and the sessions the backend started over from a folded view
# during the burst, follow when any: both optional and each printed on its own (`_line` in
# hub/agent/agent.py), so an older line without them still matches; the tasks of the queue the wake
# presented follow the same way, and so does the count of calls of unknown cost beside the dollars.
# The phone app parses the `log` event's lines with this same pattern.
_WAKE = re.compile(
    r"(?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) wake (?:(?P<wake>\d+): (?P<outcome>.+?) after "
    r"(?P<turns>\d+) turns, (?P<mail>\d+) mail, (?P<sent>\d+) sent; (?P<calls>\d+) model call\(s\), "
    r"(?P<tokens_in>\d+) in, (?P<tokens_out>\d+) out, \$(?P<cost>[\d.]+)"
    r"(?: \(partial: (?P<unknown>\d+) call\(s\) of unknown cost\))?; (?P<idle>\d+) idle poll\(s\) "
    r"before it at 0 tokens"
    r"(?:; last turn stood in (?P<context>\d+) tokens)?(?:, (?P<restarts>\d+) session restart\(s\))?"
    r"(?:; (?P<tasks>\d+) task\(s\) of the queue)?"
    r"$)?"
)


def partial(unknown: int | None) -> str:
    """The mark beside a sum of costs that misses `unknown` calls; nothing when it misses none, and
    a day whose row predates the count (None) is partial by an unknown number. Every reader that
    sums prints this one text, and `_WAKE` reads the counted form back off the wake line."""
    if unknown is None:
        return " (partial: calls of unknown cost before this count went uncounted)"
    return f" (partial: {unknown} call(s) of unknown cost)" if unknown else ""


COUNTS = ("calls", "in", "out", "mail", "runs", "idle", "idle_last")  # the counts the agent writes
COUNT_CAP = 2**53  # past any count the agent writes, inside what every JSON reader carries whole
TIME_CAP = 2**40  # past any clock the agent reads, inside what `time.localtime` formats


def is_count(v: Any) -> bool:
    """A count the agent could have written: a whole number from 0 below 2**53."""
    return type(v) is int and 0 <= v < COUNT_CAP


def is_time(t: Any) -> bool:
    """A time the agent could have written: a number from the epoch on, finite, inside the range the
    hold line can format (an int past a float's range cannot even be compared with the clock)."""
    return type(t) in (int, float) and 0 <= t < TIME_CAP


def row_as_written(row: dict) -> bool:
    """Whether a day row reads as the agent wrote it: every count a count, and `wakes` a list of times.
    A row that does not is one the agent could not have written whole, and its day is partial."""
    times = row.get("wakes")
    return all(is_count(row.get(k)) for k in COUNTS) and isinstance(times, list) and all(map(is_time, times))


def day_spend(row: Any, day: str | None) -> tuple[float, int | None]:
    """A resident's stored day row as the cap and every report read it: dollars spent and calls of
    unknown cost, (0, 0) with no row or another day's. A same-day row with no count is from before the
    count: its calls went uncounted, and no later wake can count them, so the day reads as partial to
    its end (None, never 0). A row the agent would not have written reads as none spent and the day as
    partial, never as room below zero: dollars the run would not sum (ADR-0003/S7), as a negative sum
    an earlier version kept; no dollars; dollars or a count with no day to place them; a note that is
    not a row. Such a row's dollars are lost to the cap, as a sum is never split back into the figures
    it summed. A count that is not a whole number of calls below 2**53 (what the agent writes, and
    what every JSON reader carries whole) keeps the row's dollars and reads as None; so does a counted
    row whose other counts or wakes are not as the agent writes them (`row_as_written`): the agent's
    next write marks that day partial, and the cap and the views read it so before the write."""
    if row is None:
        return 0.0, 0
    if not isinstance(row, dict) or not isinstance(row.get("day", ""), str):
        return 0.0, None
    if "day" not in row:
        return (0.0, None) if "cost" in row or "unknown" in row else (0.0, 0)
    if row["day"] != day:
        return 0.0, 0
    cost = reported_cost(row["cost"]) if "cost" in row else math.nan
    if math.isnan(cost):
        return 0.0, None
    unknown = row.get("unknown")
    return cost, unknown if unknown is None or (is_count(unknown) and row_as_written(row)) else None


@dataclass
class Area:
    name: str
    chair: str  # the chair's actor id
    desks: list[str] = field(default_factory=list)  # the desks' directories, one resident each


# how a resident's `problem` opens when its notes cannot be read: its day is then unknown
NOTES_UNREAD = "notes: "


@dataclass
class Resident:
    area: str
    chair: str
    dir: str
    actor: str
    role: str  # desk or chores: from the sections chores.toml carries
    model: str
    serving: bool  # the pid file is locked by a live process
    pid: int | None
    in_wake: bool  # the cursor says a wake is running now
    wake: int  # wakes so far, from the cursor
    last_at: str
    last_outcome: str
    runs_today: int
    mail_today: int
    cost_today: float
    unknown_today: int | None  # today's calls of unknown cost: cost_today is partial while this is not 0
    problem: str = ""  # what could not be read, when something could not


def load_areas(path: Path | str = DEFAULT_REGISTRY) -> list[Area]:
    """The registry: `[area.<name>]` tables with `chair` and `desks`. A missing file is no
    areas, said by the caller; a table missing `chair`, or whose chair is not an actor's name
    (`actor_name`), raises with the name, and so does an area that lists one directory twice, since
    two rows of one directory are one agent named twice."""
    path = Path(path)
    if not path.exists():
        return []
    doc = tomllib.loads(path.read_text())
    areas = doc.get("area", {})  # absent is no areas; false, 0, "" and [] are not a table either
    if not isinstance(areas, dict):
        raise ValueError("areas.toml: area is not a table of areas")
    out = []
    for name, t in areas.items():
        if not isinstance(t, dict) or "chair" not in t:
            raise ValueError(f"areas.toml: [area.{name}] has no chair")
        desks = t.get("desks", [])
        if not isinstance(desks, list):
            raise ValueError(f"areas.toml: [area.{name}] desks is not a list of directories")
        desks = [str(a) for a in desks]
        for i, a in enumerate(desks):
            if Path(a) in [Path(b) for b in desks[:i]]:
                raise ValueError(f"areas.toml: [area.{name}] desks lists {a} twice")
        out.append(Area(name, actor_name(t["chair"], f"areas.toml: [area.{name}] chair"), desks))
    return out


def _held(pid_file: Path) -> tuple[bool, int | None]:
    """(locked by a live process, the pid it wrote): the lock is the claim, the pid is for people.
    Only a refused shared lock is a holder; a pid file that cannot be opened, read or locked at all
    raises its OSError, for the caller to say, rather than reading as serving or as stopped."""
    try:
        fd = os.open(pid_file, os.O_RDONLY)
    except FileNotFoundError:
        return False, None
    try:
        text = os.read(fd, 64).decode(errors="replace").strip()
        # ASCII digits only: isdigit alone admits superscripts int refuses and other scripts' digits
        pid = int(text) if text.isascii() and text.isdigit() else None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # a holder has it exclusive: this fails
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True, pid
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False, pid
    finally:
        os.close(fd)


def resident(area: Area, directory: str, today: str | None = None) -> Resident:
    """One resident's row, from its own directory. What cannot be read lands in `problem`, never
    as a plausible zero."""
    d = Path(directory)
    today = today or time.strftime("%Y-%m-%d")
    actor = model = "?"
    role = "chores"
    problem = []
    cfg_path = d / "chores.toml"
    try:
        cfg = tomllib.loads(cfg_path.read_text())
        actor, model = str(cfg.get("actor", "?")), str(cfg.get("model", "?"))
        role = "desk" if "desk" in cfg else "chores"
    except (OSError, tomllib.TOMLDecodeError) as e:
        problem.append(f"chores.toml: {e}")
    try:
        serving, pid = _held(d / "agent.pid")
    except OSError as e:  # one resident's unreadable lock is its own problem, not the whole table's
        serving, pid = False, None
        problem.append(f"agent.pid: {e}")
    cursor: dict[str, Any] = {}
    spend: dict[str, Any] = {}
    row: Any = None  # the day's row as stored, read by `day_spend` as the cap reads it
    try:
        notes = agent_notes(d)
    except (KhiveError, ValueError) as e:
        problem.append(f"{NOTES_UNREAD}{e}")
    else:
        if not notes:
            problem.append("no notes yet: never ran from this directory")
        cursor = notes.get("cursor") if isinstance(notes.get("cursor"), dict) else {}
        row = notes.get("spend")
        spend = row if isinstance(row, dict) else {}
    if spend.get("day") != today:
        spend = {}
    cost, unknown = day_spend(row, today)
    return Resident(
        area=area.name,
        chair=area.chair,
        dir=str(d),
        actor=actor,
        role=role,
        model=model,
        serving=serving,
        pid=pid,
        in_wake=cursor.get("outcome") == "running",
        wake=int(cursor.get("wake") or 0),
        last_at=str(cursor.get("at") or ""),
        last_outcome=str(cursor.get("outcome") or ""),
        runs_today=spend["runs"] if is_count(spend.get("runs")) else 0,  # a count the agent wrote, else 0
        mail_today=spend["mail"] if is_count(spend.get("mail")) else 0,  # (`day_spend` reads the day partial)
        cost_today=cost,
        unknown_today=unknown,
        problem="; ".join(problem),
    )


def status(areas: list[Area], today: str | None = None) -> list[Resident]:
    return [resident(a, d, today) for a in areas for d in a.desks]


def table(areas: list[Area], rows: list[Resident]) -> str:
    """The person's view at a terminal: one line per area, one per resident under it."""
    if not areas:
        return f"no areas: {DEFAULT_REGISTRY} (or --file) is missing or empty"
    serving = sum(1 for r in rows if r.serving)
    waking = sum(r.in_wake for r in rows)
    lines = [f"{len(rows)} resident(s) in {len(areas)} area(s), {serving} serving, {waking} in a wake"]
    for a in areas:
        mine = [r for r in rows if r.area == a.name]
        lines.append(f"\n{a.name}: chair {a.chair}, {len(mine)} resident(s)")
        for r in mine:
            state = ("IN WAKE" if r.in_wake else "serving") if r.serving else "stopped"
            line = (
                f"  {r.actor} ({r.role}, {r.model}): {state}"
                + (f" pid {r.pid}" if r.serving and r.pid else "")
                + f"; wake {r.wake}"
                + (f" at {r.last_at} {r.last_outcome}" if r.last_at else "")
                + f"; today {r.runs_today} run(s), {r.mail_today} mail, ${r.cost_today:.4f}"
                + partial(r.unknown_today)
            )
            if r.problem:
                line += f"; PROBLEM {r.problem}"
            lines.append(line)
    return "\n".join(lines)


def _state(r: Resident) -> str:
    if r.serving and r.in_wake:
        return "<span class=wake>in a wake</span>"
    if r.serving:
        return f"<span class=on>serving</span> <small>pid {r.pid}</small>"
    return "<span class=off>stopped</span>"


def page(areas: list[Area], rows: list[Resident], refresh: int = 5, me: str | None = None) -> str:
    """The same view as one HTML page that reloads itself; no script, no framework. Each resident's
    name opens its own page; `me` is who that page talks as."""
    h = html.escape
    serving = sum(1 for r in rows if r.serving)
    parts = [
        '<!doctype html><meta charset=utf-8><link rel=icon href="data:,">',
        f'<meta http-equiv=refresh content="{int(refresh)}">',
        "<title>lion areas</title>",
        _STYLE,
        f"<h2>lion areas <small>{len(rows)} resident(s), {len(areas)} area(s), {serving} serving, "
        f"{sum(r.in_wake for r in rows)} in a wake · {h(time.strftime('%Y-%m-%d %H:%M:%S'))}</small></h2>",
        f"<p>you are {h(me)}</p>" if me else "<p>no identity: start with --as DIR</p>",
        "<table><tr><th>resident</th><th>role</th><th>model</th><th>state</th><th>wakes</th>"
        "<th>last wake</th><th>today</th><th>directory</th></tr>",
    ]
    for a in areas:
        mine = [r for r in rows if r.area == a.name]
        parts.append(
            f"<tr class=area><td colspan=8>{h(a.name)} <small>chair {h(a.chair)} · {len(mine)} resident(s)"
            f"</small></td></tr>"
        )
        for r in mine:
            last = f"{h(r.last_at)} {h(r.last_outcome)}" if r.last_at else ""
            today = (
                f"{r.runs_today} run(s), {r.mail_today} mail, ${r.cost_today:.4f}{partial(r.unknown_today)}"
            )
            if r.problem.startswith("no notes yet"):
                problem = "<br><span class=note>never started</span>"  # not a fault: no wake yet
            else:
                problem = f"<br><span class=bad>{h(r.problem)}</span>" if r.problem else ""
            link = h("/r?" + urlencode({"dir": r.dir}))
            parts.append(
                f'<tr><td><a href="{link}">{h(r.actor)}</a>{problem}</td><td>{h(r.role)}</td>'
                f"<td>{h(r.model)}</td><td>{_state(r)}</td><td>{r.wake}</td><td>{last}</td><td>{today}</td>"
                f"<td><small>{h(r.dir)}</small></td></tr>"
            )
    if not areas:
        parts.append(f"<tr><td colspan=8>no areas: {h(str(DEFAULT_REGISTRY))} is missing or empty</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def _row(r: Resident) -> dict:
    return {**asdict(r), "cost_today": round(r.cost_today, 4)}  # a sum of floats, as the page prints it


def controls_info(directory: str) -> dict:
    """The control surface for one resident, read live: whether its own hold file is set (and its
    first line), a pending wake-now, the `Label` of its one launchd plist (None with zero or more
    than one), and whether it is idle: no wake running under a held lock (the cursor says `running`
    while the pid lock is held, whatever the backend) and no live `claude -p` child of its pid."""
    d = Path(directory)
    hold = d / "hold"
    hold_reason = ""
    if hold.exists():
        try:
            hold_reason = hold.read_text(errors="replace").splitlines()[0].strip()
        except (OSError, IndexError):
            hold_reason = ""
    plists = list(d.glob("*.plist"))
    launchd_label = None
    if len(plists) == 1:
        try:
            with open(plists[0], "rb") as f:
                doc = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
            doc = None
        label = doc.get("Label") if isinstance(doc, dict) else None
        launchd_label = label if isinstance(label, str) and label else None
    idle = True
    pid_file = d / "agent.pid"
    try:
        running = _held(pid_file)[0]
    except OSError:  # an unreadable lock says nothing, so it reads as busy, as an unread pgrep does
        running, idle = False, False
    if running:  # a wake on an in-process backend has no child to find
        # idle only on a cursor that names a finished wake: a first wake starts before any cursor
        # exists, and an unreadable one says nothing, so both read as busy, as an unread pgrep does
        try:
            cursor = agent_notes(d).get("cursor")
            outcome = cursor.get("outcome") if isinstance(cursor, dict) else None
            idle = isinstance(outcome, str) and outcome not in ("", "running")
        except (KhiveError, ValueError):
            idle = False
    if idle and pid_file.exists():
        try:
            text = pid_file.read_text(errors="replace").strip()
        except OSError:
            text = ""
        if text.isascii() and text.isdigit():
            # absolute: under launchd PATH is /usr/bin:/bin:/usr/sbin:/sbin. pgrep exits 0 when a child
            # matched and 1 when none did; any other answer, or none, reads as busy, so a restart then
            # takes `force` rather than cutting a wake short on a guess
            try:
                found = subprocess.run(
                    [_PGREP, "-P", text, "-f", "claude -p"], capture_output=True, timeout=5
                )
                idle = found.returncode == 1
            except (OSError, subprocess.TimeoutExpired):
                idle = False
    return {
        "held_by_file": hold.exists(),
        "hold_reason": hold_reason,
        "wake_now_pending": (d / "wake-now").exists(),
        "launchd_label": launchd_label,
        "idle": idle,
    }


def agent_store(directory: str | Path) -> KhiveNoteStore:
    """The `agent` notes of the resident at `directory`, in the store its actor writes (ADR-0025 D6):
    `chores.toml` names the actor, the directory's own `.khive/config.toml` must agree, and each read
    is one call of the binary. Raises `KhiveError` when the binary or the config is missing, or when
    the config would put the lion pack's tables in the main store (a `Store`'s refusal, ADR-0026 C8),
    and `ValueError` when chores.toml names no actor."""
    actor = chores_toml(directory).get("actor")
    if not isinstance(actor, str) or not actor:
        raise ValueError("chores.toml names no actor")
    return Store(Khive(Path(directory), actor)).notes("agent")


def agent_notes(directory: str | Path) -> dict[str, Any]:
    """Every note of the resident at `directory`, value by key, in one call of the binary; empty when
    it never ran from there. Raises as `agent_store` does, and `KhiveError` when the store refuses."""
    return {n.key: n.value for n in agent_store(directory).list()}


def chores_toml(directory: str) -> dict:
    """The resident's chores.toml as a table; one that is missing or does not parse is empty."""
    try:
        return tomllib.loads((Path(directory) / "chores.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def budget_table(directory: str) -> dict:
    return dict(chores_toml(directory).get("budget") or {})


def wake_table(directory: str) -> dict:
    return dict(chores_toml(directory).get("wake") or {})


def waiting_mail(directory: str) -> list[dict] | None:
    """The mail a `[wake]` rule deferred, kept under the `deferred` note until the batch window
    opens (hub/agent/agent.py `_triage` keeps each whole, stamped `deferred_at`/`deferred_ts`): who
    sent it, the subject and when it was deferred, the body left in the note. No note yet is no mail
    waiting; notes that cannot be read are None, never a count of zero."""
    try:
        deferred = agent_notes(directory).get("deferred")
    except (KhiveError, ValueError):
        return None
    mail = deferred.get("mail") if isinstance(deferred, dict) else None
    out = []
    for m in mail if isinstance(mail, list) else []:
        if not isinstance(m, dict):
            continue
        props = m.get("properties") if isinstance(m.get("properties"), dict) else {}
        try:
            ts = float(m.get("deferred_ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        out.append(
            {
                "id": str(m.get("id") or ""),
                "from": str(props.get("from_actor") or m.get("from") or "?"),  # as the agent's `_sender`
                "subject": str(m.get("subject") or ""),
                "deferred_at": str(m.get("deferred_at") or ""),
                "deferred_ts": ts,
            }
        )
    return out


def _api_row(r: Resident, controls: dict | None = None, waiting: list[dict] | None = None) -> dict:
    """One resident as the app reads it: `_row` plus its live `controls` and the count of mail
    waiting for the window."""
    found = waiting if waiting is not None else waiting_mail(r.dir)
    return {
        **_row(r),
        "controls": controls if controls is not None else controls_info(r.dir),
        "waiting": None if found is None else len(found),  # null: the notes could not be read
    }


def area_rows(rows: list[Resident]) -> list[dict]:
    """The rows of `/api/areas` and of the `areas` SSE event, which carry the same shape as
    `resident` of `/api/resident`."""
    return [_api_row(r) for r in rows]


def rows_json(rows: list[Resident]) -> str:
    return json.dumps(area_rows(rows), indent=1)


def wake_row(line: str) -> dict | None:
    """One serve.log line read as a wake: the numbers `serve` printed, or the line kept whole with its
    time when it is a wake line of another shape; None for a line that is no wake line."""
    m = _WAKE.match(line)
    if m is None:
        return None
    if m["wake"] is None:
        return {"at": m["at"], "raw": line}
    row: dict[str, Any] = {}
    for k, v in m.groupdict().items():
        if k in ("at", "outcome"):
            row[k] = v
        elif k == "cost":
            row[k] = float(v)
        elif k not in ("context", "restarts", "tasks", "unknown"):
            row[k] = int(v)
    # the context and restarts suffixes: absent on an older line; a line that stood in a
    # context and started no session over prints no restarts, which is 0 restarts
    if m["context"] is not None:
        row["context"] = int(m["context"])
        row["restarts"] = int(m["restarts"] or 0)
    elif m["restarts"] is not None:
        row["restarts"] = int(m["restarts"])
    if m["tasks"] is not None:  # the queue tasks the wake presented
        row["tasks"] = int(m["tasks"])
    if m["unknown"] is not None:  # calls the wake's dollars miss
        row["unknown"] = int(m["unknown"])
    return {**row, "raw": line}


def wakes(directory: str, n: int = 30) -> list[dict]:
    """The last n wake lines of the resident's serve.log, newest first, with the numbers `serve`
    printed; a `serving ` line is a restart row, and a wake line of another shape is kept whole
    rather than dropped. No serve.log yet is no rows."""
    try:
        text = (Path(directory) / "serve.log").read_text(errors="replace")
    except FileNotFoundError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        if line.startswith("serving "):
            rows.append({"at": "", "raw": line, "restart": True})
        elif (row := wake_row(line)) is not None:
            rows.append(row)
    return rows[max(len(rows) - n, 0) :][::-1]


def log_tail(directory: str, n: int = 60) -> str:
    """The last n lines of this run's serve.log: from the last `serving ` line onward, so an earlier
    run's traceback does not read as the live state; no serve.log yet is ""."""
    try:
        lines = (Path(directory) / "serve.log").read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    starts = [i for i, line in enumerate(lines) if line.startswith("serving ")]
    if starts:
        lines = lines[starts[-1] :]
    return "\n".join(lines[max(len(lines) - n, 0) :])


def identity(directory: Path | str) -> tuple[str, Khive] | None:
    """Who the page talks as: the `[actor] id` of the directory's .khive/config.toml and a Khive
    bound to it. No config or no id is no identity, and the page then reads and sends no mail."""
    d = Path(directory).expanduser()
    cfg = d / ".khive" / "config.toml"
    if not cfg.exists():
        return None
    actor = (tomllib.loads(cfg.read_text()).get("actor") or {}).get("id")
    return (str(actor), Khive(d, str(actor))) if actor else None


async def talk(khive: Khive, actor: str, limit: int = 50) -> list[dict]:
    """The conversation between khive's actor and `actor`: what it sent `actor` and what `actor`
    sent it, oldest first, the last `limit`. comm.inbox reads without marking anything read."""
    sent, got = await asyncio.gather(
        khive.exec(f'comm.inbox(box="sent", to_actor={lit(actor)}, limit={int(limit)})'),
        khive.exec(f'comm.inbox(from_actor={lit(actor)}, status="all", limit={int(limit)})'),
    )
    by_id = {
        m["id"]: {
            "at": m["created_at"],
            "from": m["from"],
            "to": m["to"],
            "subject": m.get("subject") or "",
            "content": m.get("content") or "",
            "id": m["id"],
        }
        for m in sent[0]["messages"] + got[0]["messages"]
    }
    rows = sorted(by_id.values(), key=lambda m: datetime.fromisoformat(m["at"]))
    return rows[max(len(rows) - limit, 0) :]


def resident_page(
    row: Resident,
    wake_rows: list[dict],
    tail: str,
    talk_rows: list[dict] | str,
    me: str | None,
    refresh: int,
    directory: str,
    sent: str = "",
) -> str:
    """One resident's page: its row, its wakes, and the conversation with it, with a form that mails
    it as `me`. `talk_rows` is the error text when the conversation could not be read. One script
    reloads the page, and waits while a message is being written, a log tail is open or text is
    selected, so nothing a person is doing is wiped. `sent` is the id of a message just sent."""
    h = html.escape
    today = time.strftime("%Y-%m-%d")
    last = f" · last {h(row.last_at)} {h(row.last_outcome)}" if row.last_at else ""
    parts = [
        '<!doctype html><meta charset=utf-8><link rel=icon href="data:,">',
        f"<title>{h(row.actor)}</title>",
        _STYLE,
        '<p><a href="/">all residents</a></p>',
        f"<h2>{h(row.actor)} <small>{h(row.role)}, {h(row.model)} · area {h(row.area)}, chair "
        f"{h(row.chair)}</small></h2>",
        f"<p>{_state(row)} · {row.wake} wake(s){last} · today {row.runs_today} run(s), "
        f"{row.mail_today} mail, ${row.cost_today:.4f}" + partial(row.unknown_today) + "</p>",
    ]
    if row.problem:
        parts.append(f"<p class=bad>{h(row.problem)}</p>")
    parts.append("<h3>wakes</h3>")
    if wake_rows:
        parts.append(
            "<table><tr><th>time</th><th>wake</th><th>outcome</th><th>turns</th><th>mail</th><th>sent</th>"
            "<th>calls</th><th>in</th><th>out</th><th>$</th><th>idle polls</th></tr>"
        )
        for w in wake_rows:
            if "wake" not in w:
                if w.get("restart"):
                    pid = re.search(r" as pid (\d+)", w["raw"])
                    text = f"restarted as pid {pid[1]}" if pid else w["raw"]
                    parts.append(f"<tr class=restart><td colspan=11>{h(text)}</td></tr>")
                else:
                    parts.append(f"<tr class=bad><td colspan=11>{h(w['raw'])}</td></tr>")
                continue
            cells = [w["at"], w["wake"], w["outcome"], w["turns"], w["mail"], w["sent"], w["calls"]]
            cells += [
                w["tokens_in"],
                w["tokens_out"],
                f"${w['cost']:.4f}" + partial(w.get("unknown", 0)),
                w["idle"],
            ]
            parts.append("<tr>" + "".join(f"<td>{h(str(c))}</td>" for c in cells) + "</tr>")
        parts.append("</table>")
    else:
        parts.append("<p>no wake lines in serve.log yet</p>" if tail else "<p>no serve.log yet</p>")
    if tail:
        parts.append(f"<details><summary>log tail</summary><pre>{h(tail)}</pre></details>")
    parts.append(f"<h3>talk <small>{h(me)} and {h(row.actor)}</small></h3>" if me else "<h3>talk</h3>")
    if me is None:
        parts.append(
            "<p>no identity: run lion areas --serve PORT --as DIR where DIR holds .khive/config.toml</p>"
        )
    else:
        if isinstance(talk_rows, str):
            parts.append(f"<p class=bad>the conversation could not be read: {h(talk_rows)}</p>")
        elif not talk_rows:
            parts.append("<p><small>no mail between you yet</small></p>")
        for m in talk_rows if isinstance(talk_rows, list) else []:
            at = datetime.fromisoformat(m["at"]).astimezone()
            when = at.strftime("%H:%M:%S" if at.strftime("%Y-%m-%d") == today else "%Y-%m-%d %H:%M:%S")
            subject = f"<b>{h(m['subject'])}</b>" if m["subject"] else ""  # no empty bold for a bare reply
            parts.append(
                f"<div class=msg><small>{when}</small> {h(str(m['from']))} → {h(str(m['to']))} "
                f"{subject}<pre>{h(m['content'])}</pre></div>"
            )
        if sent:
            parts.append(f"<p class=note>sent, message {h(sent)}</p>")
        # `once` rides to comm.send as its idempotency key: a double click or a resend after an error
        # replays the first send instead of mailing twice; the button also goes dark on submit
        parts.append(
            "<form method=post action=/send onsubmit=\"this.querySelector('button').disabled=true\">"
            f'<input type=hidden name=dir value="{h(directory)}">'
            f'<input type=hidden name=once value="{uuid.uuid4().hex}">'
            "<p><input name=subject placeholder=subject size=60></p>"
            "<p><textarea name=content rows=6 cols=90 required placeholder=message></textarea></p>"
            f"<button type=submit>send to {h(row.actor)}</button></form>"
        )
    # a reload wipes what is being typed, closes an open log tail and drops a selection: wait for those
    ms = int(refresh) * 1000
    parts.append(
        "<script>setTimeout(function tick(){var f=document.querySelectorAll('textarea,input[name=subject]'),"
        "busy=document.querySelector('details[open]')||String(getSelection());"
        "for(var i=0;i<f.length;i++)if(f[i].value||f[i]===document.activeElement)busy=1;"
        f"if(busy)return setTimeout(tick,{ms});location.reload()}},{ms})</script>"
    )
    return "\n".join(parts)


def resident_json(
    row: Resident,
    wake_rows: list[dict],
    tail: str,
    talk_rows: list[dict] | str,
    me: str | None,
    controls: dict | None = None,
    budget: dict | None = None,
    wake: dict | None = None,
    waiting: list[dict] | None = None,
) -> str:
    """`/api/resident`: the row as `/api/areas` carries it, its wakes, the log tail, the talk (the
    error text when it could not be read), who the server talks as, and the controls, `[budget]`,
    `[wake]` and waiting mail read live. A TOML date in a table is its text."""
    controls = controls if controls is not None else controls_info(row.dir)
    waiting = waiting if waiting is not None else waiting_mail(row.dir)
    return json.dumps(
        {
            "resident": _api_row(row, controls, waiting),
            "wakes": wake_rows,
            "tail": tail,
            "talk": talk_rows,
            "me": me,
            "controls": controls,
            "budget": budget if budget is not None else budget_table(row.dir),
            "wake": wake if wake is not None else wake_table(row.dir),
            "waiting": waiting,
        },
        indent=1,
        default=str,
    )
