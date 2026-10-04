"""`lion areas`: the residents by area, at the terminal, as a page that reloads itself, or as the API
the phone app reads (live over Server-Sent Events, with controls back onto each resident)."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import os
import plistlib
import re
import subprocess
import threading
import time
import tomllib
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.parsers.expat import ExpatError

from hub.app import app_shell, icon_svg, manifest_webmanifest, service_worker_js
from hub.areas import (
    Area,
    area_rows,
    controls_info,
    identity,
    load_areas,
    log_tail,
    page,
    resident,
    resident_json,
    resident_page,
    rows_json,
    status,
    table,
    talk,
    wakes,
)
from hub.khive import Khive, KhiveError, lit
from lion1.checkpoint import CheckpointError

from .context import checkpoint as context_checkpoint
from .context import restore as context_restore
from .context import set_var as context_set
from .context import status as context_status

__all__ = ("context_api", "serve", "areas_main")

# Tailscale's CGNAT range: the page answers only over a tailnet or loopback.
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_LAUNCHCTL = "/bin/launchctl"  # absolute: under launchd PATH is /usr/bin:/bin:/usr/sbin:/sbin
_HISTORY = 20  # the serve.log lines per desk an `/events` connection opens with
_POLL_S = 1.0  # how often `/events` re-reads the registry, the residents and their logs
_PING_S = 15.0
_WRITE_S = 30.0  # a client that takes longer than this to accept a write is dropped
_ONCE_KEEP = 100  # the `/control` answers kept for a replayed `once`
_MAX_BODY = 1 << 20

# a table or array-of-tables header ends the table before it; `[budget]` may carry a comment
_TABLE_RE = re.compile(r"^[ \t]*\[")
_BUDGET_RE = re.compile(r"^[ \t]*\[[ \t]*budget[ \t]*\][ \t]*(?:#.*)?$")
_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][\w-]*)(?P<eq>[ \t]*=[ \t]*)"
    r"(?P<val>[^#\r\n]*?)(?P<rest>[ \t]*(?:#.*)?\r?\n?)$"
)


def _peer_allowed(addr: str) -> bool:
    """Loopback or inside the Tailscale CGNAT range; anything else is refused before the request
    is read any further."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip in _TAILSCALE_CGNAT


def _rewrite_budget(text: str, updates: dict[str, str]) -> str:
    """Rewrite only the named keys inside `[budget]`, keeping every other byte (comments
    included); a missing key is added after the table's last key, a missing table appended at the
    end."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _BUDGET_RE.match(line.rstrip("\r\n"))), None)
    if start is None:
        head = text if not text or text.endswith("\n") else text + "\n"
        block = "[budget]\n" + "".join(f"{k} = {v}\n" for k, v in updates.items())
        return head + ("\n" if head else "") + block
    end = next((j for j in range(start + 1, len(lines)) if _TABLE_RE.match(lines[j])), len(lines))
    remaining = dict(updates)
    last = start  # the table's last line that is neither blank nor a comment
    for i in range(start + 1, end):
        body = lines[i].strip()
        if body and not body.startswith("#"):
            last = i
        m = _KEY_RE.match(lines[i])
        if m and m["key"] in remaining:
            lines[i] = f"{m['indent']}{m['key']}{m['eq']}{remaining.pop(m['key'])}{m['rest']}"
    if remaining:
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines.insert(last + 1, "".join(f"{k} = {v}\n" for k, v in remaining.items()))
    return "".join(lines)


def _text(body: dict, key: str) -> str:
    v = body.get(key)
    return "" if v is None else str(v)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _tail(
    log: Path, state: tuple[int, bytes, int] | None
) -> tuple[list[tuple[str, bool]], tuple[int, bytes, int]]:
    """The whole lines a serve.log gained since `state` (the offset read to, a partial last line and
    the inode read), each with whether it is history. First sight is the last `_HISTORY` lines as
    history; a partial last line waits for its newline; a log that shrank (truncated) or is another
    file at the name (rotated, replaced) is read from its start; no log yet is no lines, and its
    first lines are new."""
    try:
        f = open(log, "rb")
    except FileNotFoundError:
        return [], state or (0, b"", 0)
    with f:
        st = os.fstat(f.fileno())
        size, ino = st.st_size, st.st_ino
        if state is None:
            f.seek(max(size - 65536, 0))
            data = f.read()
            whole, _, partial = data.rpartition(b"\n")
            lines = whole.split(b"\n") if whole else []
            if size > 65536 and lines:
                lines = lines[1:]  # the seek landed inside a line
            said = [s for s in (b.decode(errors="replace").rstrip("\r") for b in lines) if s]
            return [(s, True) for s in said[-_HISTORY:]], (f.tell(), partial, ino)
        pos, partial, seen = state
        if size < pos or ino != seen:
            pos, partial = 0, b""
        f.seek(pos)
        data = partial + f.read()
        end = f.tell()
    whole, _, partial = data.rpartition(b"\n")
    said = [s for s in (b.decode(errors="replace").rstrip("\r") for b in whole.split(b"\n")) if s]
    return [(s, False) for s in said], (end, partial, ino)


def _desk(registry: Path, want: str) -> tuple[Area, str] | None:
    # as written in the registry, or as a Resident's `dir` prints it (the link the page carries)
    for a in load_areas(registry):
        if any(want in (d, str(Path(d))) for d in a.desks):
            return a, want
    return None


_CONTEXT_ROUTES = {
    ("GET", "/api/context/status"),
    ("GET", "/api/context/restore"),
    ("POST", "/api/context/checkpoint"),
    ("POST", "/api/context/set"),
}


def context_api(registry: Path, method: str, path: str, params: dict) -> tuple[int, dict]:
    """`GET /api/context/status?dir=`, `GET /api/context/restore?dir=&id=`, `POST
    /api/context/checkpoint` `{dir, why}` and `POST /api/context/set` `{dir, name, kind, value}` onto a
    registered desk: (status, the JSON answer). The work is `lion context`'s own; a dir the registry
    does not name is 404, a refused take, restore or declaration 409."""
    if (method, path) not in _CONTEXT_ROUTES:
        return 404, {"ok": False, "detail": f"no route {method} {path}"}
    d = str(params.get("dir") or "")
    if _desk(registry, d) is None:
        return 404, {
            "ok": False,
            "detail": f"no resident at {d!r}: not a desk in {registry}; nothing was read",
        }
    try:
        if path == "/api/context/status":
            return 200, context_status(d)
        if path == "/api/context/restore":
            return 200, context_restore(d, str(params.get("id") or "") or None)
        if path == "/api/context/set":
            name, kind = str(params.get("name") or ""), str(params.get("kind") or "")
            return 200, context_set(d, name, kind, params.get("value"))
        return 200, context_checkpoint(d, str(params.get("why") or "manual"))
    except CheckpointError as e:
        return 409, {"ok": False, "detail": str(e)}


def make_server(
    registry: Path,
    host: str,
    port: int,
    refresh: int,
    identity: tuple[str, Khive] | None = None,
    allow: list[str] | None = None,
) -> ThreadingHTTPServer:
    """`/` is the page, `/api/areas` the rows as JSON; every request re-reads the registry and the
    residents' own files, so the page is never staler than its refresh. `/r?dir=` is one
    resident's page (`/api/resident?dir=` its JSON) and `POST /send` mails that resident as
    `identity`. `/events` streams the rows and the logs live, `POST /control` is the write path back
    onto a resident. A dir the registry does not name is 404 everywhere, so nothing is read,
    written or mailed outside it. Bound, not yet serving: `serve_forever` runs it, `shutdown` then
    `server_close` end it; `hosts` on it are the names it answers to."""
    me, khive = identity or (None, None)
    hosts: set[str] = set()  # the names this server answers to, known once the port is
    origins: set[str] = set()  # the Origins a browser POST may carry: http(s) + an allowed host
    once_cache: OrderedDict[str, tuple[int, dict]] = OrderedDict()  # /control's idempotency, last 100
    # one control at a time: a replayed `once` racing its first request must find the first answer
    control_lock = threading.Lock()
    # the registry and the residents are read through one source, under one lock: `/events` serves the
    # snapshot of the last read for every connection (each read runs a pgrep per serving resident) and
    # `/api/areas` makes a read of its own, which becomes the snapshot. A read whose rows differ from the
    # snapshot's takes the next revision and one whose rows are the same keeps it, so of two payloads the
    # one with the higher revision is the later change
    snapshot: dict[str, Any] = {"at": float("-inf"), "areas": [], "rows": None, "rev": 0}
    snapshot_lock = threading.Lock()

    def read_rows() -> None:  # with `snapshot_lock` held
        areas = load_areas(registry)
        rows = area_rows(status(areas))
        if rows == snapshot["rows"]:  # nothing changed: the revision stays, and so does what was sent for it
            snapshot.update(at=time.monotonic(), areas=areas)
            return
        # the wall clock in microseconds, above the last revision given. The count starts at 0 with each
        # server, so it is the wall clock that keeps a restarted server's revisions above the earlier one's,
        # unless the clock stepped back, which the page's stream epoch covers
        rev = max(snapshot["rev"] + 1, time.time_ns() // 1000)
        snapshot.update(at=time.monotonic(), areas=areas, rows=rows, rev=rev)

    def poll_rows() -> tuple[list[Area], list[dict] | None, int]:
        with snapshot_lock:
            if time.monotonic() - snapshot["at"] >= _POLL_S:
                try:
                    read_rows()
                except (OSError, ValueError):  # a registry mid-write: the next poll reads it whole
                    snapshot.update(at=time.monotonic(), areas=[], rows=None)
            return snapshot["areas"], snapshot["rows"], snapshot["rev"]

    def desk(want: str) -> tuple[Area, str] | None:
        return _desk(registry, want)

    class Handler(BaseHTTPRequestHandler):
        def answer(
            self,
            code: int,
            body: str,
            ctype: str = "text/plain; charset=utf-8",
            location: str = "",
            revision: int = 0,
        ) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if location:
                self.send_header("Location", location)
            if revision:
                self.send_header("X-Areas-Revision", str(revision))
            self.end_headers()
            self.wfile.write(data)

        def answer_json(self, code: int, doc: dict) -> None:
            self.answer(code, json.dumps(doc), "application/json")

        def wants_json(self) -> bool:
            # `/control` always answers JSON; `/send` does under `Accept: application/json`
            path = urlsplit(self.path).path
            return path == "/control" or "application/json" in self.headers.get("Accept", "")

        def guarded(self) -> bool:
            # off the tailnet or loopback: refused before anything else is read. A loopback page is
            # reachable by any site the browser visits: a Host that is not this server (DNS
            # rebinding) or a POST a browser sent from another origin (a cross-site form) is refused
            # too. A native client sends no Origin: the peer and Host guards are its admission
            peer = self.client_address[0]
            refusal = ""
            if not _peer_allowed(peer):
                refusal = f"{peer} is outside loopback/Tailscale; nothing was done"
            elif (host := self.headers.get("Host", "")) not in hosts:
                refusal = f"host {host!r} is not this server; nothing was done"
            elif self.command == "POST":
                origin = self.headers.get("Origin")
                if origin is not None and origin not in origins:
                    refusal = f"origin {origin!r} is not this server; nothing was done"
            if not refusal:
                return True
            if self.command == "POST" and self.wants_json():
                self.answer_json(403, {"ok": False, "action": "", "detail": refusal})
            else:
                self.answer(403, refusal + "\n")
            return False

        def run(self, coro):
            # each request runs in its own thread with no loop running: one loop per call; the
            # envelopes Khive keeps are for a console's log, and a server would hold them forever
            try:
                return asyncio.run(coro)
            finally:
                khive.calls.clear()

        def body(self) -> dict | str:
            """The POST body's fields: JSON under `Content-Type: application/json` (or a body that
            opens with `{`, a native client that left the header out), else form-encoded. A str
            says what was wrong with it."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return "Content-Length is not a number"
            if not 0 <= length <= _MAX_BODY:
                return f"the body is {length} bytes; at most {_MAX_BODY}"
            text = (self.rfile.read(length) if length else b"").decode(errors="replace")
            ctype = self.headers.get("Content-Type", "").lower()
            if "application/json" in ctype or text.lstrip().startswith("{"):
                try:
                    doc = json.loads(text or "{}")
                except ValueError:
                    return "the body is not JSON"
                return doc if isinstance(doc, dict) else "the body is not a JSON object"
            return {k: (v[0] if v else "") for k, v in parse_qs(text, keep_blank_values=True).items()}

        # ---- GET ----

        def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
            if not self.guarded():
                return
            url = urlsplit(self.path)
            if url.path == "/favicon.ico":
                self.answer(204, "")
                return
            if url.path == "/app":
                self.answer(200, app_shell(), "text/html; charset=utf-8")
                return
            if url.path == "/app/manifest.webmanifest":
                self.answer(200, manifest_webmanifest(), "application/manifest+json")
                return
            if url.path == "/app/sw.js":
                self.answer(200, service_worker_js(), "text/javascript; charset=utf-8")
                return
            if url.path == "/app/icon.svg":
                self.answer(200, icon_svg(), "image/svg+xml")
                return
            if url.path == "/events":
                self._events()
                return
            if url.path == "/api/areas":
                with snapshot_lock:
                    read_rows()
                    rows, revision = snapshot["rows"], snapshot["rev"]
                self.answer(200, json.dumps(rows, indent=1), "application/json", revision=revision)
                return
            if url.path == "/":
                areas = load_areas(registry)
                self.answer(200, page(areas, status(areas), refresh, me), "text/html; charset=utf-8")
                return
            if url.path.startswith("/api/context/"):
                params = {k: v[0] for k, v in parse_qs(url.query).items() if v}
                self.answer_json(*context_api(registry, "GET", url.path, params))
                return
            if url.path not in ("/r", "/api/resident"):
                self.send_error(404)
                return
            query = parse_qs(url.query)
            want = (query.get("dir") or [""])[0]
            found = desk(want)
            if found is None:
                self.answer(404, f"no resident at {want!r}: not a desk in {registry}\n")
                return
            area, d = found
            row = resident(area, d)
            wake_rows, tail = wakes(d), log_tail(d)
            talk_rows: list[dict] | str = []
            if khive is not None and row.actor in ("?", ""):
                talk_rows = f"the resident's actor could not be read: {row.problem}"
            elif khive is not None:
                try:
                    talk_rows = self.run(talk(khive, row.actor))
                except KhiveError as e:
                    talk_rows = str(e)
            if url.path == "/r":
                sent = (query.get("sent") or [""])[0]
                body = resident_page(row, wake_rows, tail, talk_rows, me, refresh, d, sent=sent)
                self.answer(200, body, "text/html; charset=utf-8")
            else:
                self.answer(200, resident_json(row, wake_rows, tail, talk_rows, me), "application/json")

        def _events(self) -> None:
            """SSE, one thread per connection (ThreadingHTTPServer's). Every `_POLL_S` re-read the
            registry and the residents: `areas` on connect and whenever the last read, a poll's
            included, has a revision above the last sent, its rows changed or not, its `id` that
            revision; per desk the last `_HISTORY` serve.log lines as history on first sight, then
            each new line; `ping` every `_PING_S`. A write the client is gone for, or stalled on,
            ends the thread."""
            try:
                self.connection.settimeout(_WRITE_S)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
            except OSError:
                return

            def emit(event: str, data: Any, revision: int = 0) -> bool:
                try:
                    stamp = f"id: {revision}\n" if revision else ""
                    self.wfile.write(f"{stamp}event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                    return True
                except OSError:  # BrokenPipe, ConnectionReset: the client went away
                    return False

            tails: dict[str, tuple[int, bytes, int]] = {}
            sent = 0  # the revision of the last `areas` event
            next_ping = time.monotonic() + _PING_S
            while True:
                areas, rows, revision = poll_rows()
                if rows is not None and revision > sent:
                    if not emit("areas", rows, revision):
                        return
                    sent = revision
                for a in areas:
                    for listed in a.desks:
                        d = str(Path(listed))  # as the row's `dir` prints it, so the app can join them
                        lines, tails[d] = _tail(Path(d) / "serve.log", tails.get(d))
                        for line, history in lines:
                            if not emit("log", {"dir": d, "line": line, "history": history}):
                                return
                if time.monotonic() >= next_ping:
                    if not emit("ping", {}):
                        return
                    next_ping = time.monotonic() + _PING_S
                time.sleep(_POLL_S)

        # ---- POST ----

        def do_POST(self) -> None:  # noqa: N802 (the stdlib's name)
            if not self.guarded():
                return
            path = urlsplit(self.path).path
            if path == "/control":
                self._control()
            elif path == "/send":
                self._send()
            elif path.startswith("/api/context/"):
                body = self.body()
                if isinstance(body, str):
                    self.answer_json(400, {"ok": False, "detail": f"{body}; nothing was taken"})
                    return
                self.answer_json(*context_api(registry, "POST", path, body))
            else:
                self.send_error(404)

        def _send(self) -> None:
            wants_json = self.wants_json()

            def fail(code: int, detail: str) -> None:
                if wants_json:
                    self.answer_json(code, {"ok": False, "detail": detail})
                else:
                    self.answer(code, detail + "\n")

            body = self.body()
            if isinstance(body, str):
                fail(400, f"{body}; nothing was sent")
                return
            d, subject, content, once = (_text(body, k) for k in ("dir", "subject", "content", "once"))
            found = desk(d)
            if found is None:
                fail(404, f"no resident at {d!r}: not a desk in {registry}; nothing was sent")
                return
            if not content.strip():
                fail(400, "content is empty: nothing was sent")
                return
            if khive is None:
                fail(400, "no identity: start lion areas with --as DIR; nothing was sent")
                return
            # the recipient is the resident's own actor, read from its chores.toml, never the form
            row = resident(*found)
            if row.actor in ("?", ""):
                fail(500, f"the resident's actor could not be read: {row.problem}; nothing was sent")
                return
            args = [f"to={lit(row.actor)}"]
            if subject.strip():
                args.append(f"subject={lit(subject)}")
            args.append(f"content={lit(content)}")
            once = once.replace("-", "").lower()  # a UUID with its hyphens is the same token
            if re.fullmatch(r"[0-9a-f]{32}", once):  # the page's own token: a repeat replays, never resends
                args.append(f"idempotency_key={lit('ui:' + once)}")
            try:
                (res,) = self.run(khive.exec(f"comm.send({', '.join(args)})"))
            except KhiveError as e:
                fail(502, str(e))
                return
            sent_id = str((res or {}).get("id") or "")
            if wants_json:
                self.answer_json(200, {"ok": True, "id": sent_id})
                return
            where = {"dir": d, "sent": sent_id}
            self.answer(303, "", location="/r?" + urlencode({k: v for k, v in where.items() if v}))

        def _control(self) -> None:
            body = self.body()
            if isinstance(body, str):
                self.answer_json(400, {"ok": False, "action": "", "detail": body})
                return
            d, action = _text(body, "dir"), _text(body, "action")
            once = _text(body, "once").replace("-", "").lower()  # a UUID with its hyphens is the same token
            with control_lock:
                if once and once in once_cache:  # the first answer again, nothing done twice
                    code, resp = once_cache[once]
                    self.answer_json(code, resp)
                    return
                if desk(d) is None:
                    detail = f"no resident at {d!r}: not a desk in {registry}; nothing was done"
                    self.answer_json(404, {"ok": False, "action": action, "detail": detail})
                    return
                code, resp = self._act(Path(d), action, body)
                if once:
                    once_cache[once] = (code, resp)
                    while len(once_cache) > _ONCE_KEEP:
                        once_cache.popitem(last=False)
            self.answer_json(code, resp)

        def _act(self, d: Path, action: str, body: dict) -> tuple[int, dict]:
            """One control onto a registered resident: (status, the JSON answer). The resident reads
            `hold`, `wake-now` and chores.toml at its next poll (hub/agent/agent.py); this process
            only writes them, and notes each control in `<dir>/landing/controls.log`."""
            who = me or "unknown"
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            extra: dict[str, Any] = {}
            try:
                if action == "hold":
                    reason = _one_line(_text(body, "reason"))  # the first line is the resident's reason
                    (d / "hold").write_text(f"held by {who} from the app at {now}: {reason}\n")
                    code, detail = 200, f"held: {reason}" if reason else "held"
                elif action == "resume":
                    try:
                        (d / "hold").unlink()
                        code, detail = 200, "resumed"
                    except FileNotFoundError:
                        code, detail = 200, "was not held"
                elif action == "wake":
                    (d / "wake-now").write_text(f"{who} at {now}\n")
                    code, detail = 200, "wake requested"
                elif action == "restart":
                    code, detail, extra = self._restart(d, body)
                elif action == "cap":
                    code, detail, extra = self._cap(d, body)
                else:
                    code, detail = 400, f"unknown action {action!r}: hold, resume, wake, restart or cap"
            except OSError as e:
                code, detail = 500, f"{action}: {type(e).__name__}: {e}"
            try:
                (d / "landing").mkdir(parents=True, exist_ok=True)
                with open(d / "landing" / "controls.log", "a") as f:
                    f.write(f"{now} {who} {_one_line(action) or '-'} {code} {_one_line(detail)}\n")
            except OSError:
                pass  # the note is for people; the control's own answer says what happened
            return code, {"ok": code == 200, "action": action, "detail": detail, **extra}

        def _restart(self, d: Path, body: dict) -> tuple[int, str, dict]:
            plists = sorted(d.glob("*.plist"))
            if len(plists) != 1:
                if not plists:
                    return 409, f"no launchd plist in {d}", {}
                return 409, f"{len(plists)} launchd plists in {d}: which one is the job is not known", {}
            try:
                with open(plists[0], "rb") as f:
                    doc = plistlib.load(f)
            except (OSError, plistlib.InvalidFileException, ValueError, ExpatError) as e:
                return 409, f"could not read {plists[0].name}: {e}", {}
            label = doc.get("Label") if isinstance(doc, dict) else None
            if not isinstance(label, str) or not label:
                return 409, f"{plists[0].name} has no Label", {}
            force = body.get("force") in ("1", 1, True, "true")
            if not force and not controls_info(str(d))["idle"]:
                return 409, "in a wake", {"label": label}
            argv = [_LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired) as e:
                return 502, f"launchctl: {type(e).__name__}: {e}", {"label": label}
            said = _one_line(proc.stderr or proc.stdout)
            detail = f"kickstart {label}: rc {proc.returncode}" + (f": {said}" if said else "")
            return (200 if proc.returncode == 0 else 502), detail, {"label": label, "rc": proc.returncode}

        def _cap(self, d: Path, body: dict) -> tuple[int, str, dict]:
            values: dict[str, float | int] = {}
            raw = body.get("cost_per_day")
            if raw is not None and str(raw) != "":
                try:
                    if isinstance(raw, bool):
                        raise ValueError
                    v = float(raw)
                except (TypeError, ValueError):
                    return 400, f"cost_per_day {raw!r} is not a number", {}
                if not math.isfinite(v) or v < 0:
                    return 400, f"cost_per_day {raw!r}: a finite number >= 0", {}
                values["cost_per_day"] = v
            raw = body.get("wakes_per_hour")
            if raw is not None and str(raw) != "":
                try:
                    if isinstance(raw, bool):
                        raise ValueError
                    n = float(raw)
                except (TypeError, ValueError):
                    return 400, f"wakes_per_hour {raw!r} is not a number", {}
                if not n.is_integer() or n < 1:  # no wakes at all is a hold, not a cap
                    return 400, f"wakes_per_hour {raw!r}: a whole number >= 1 (hold stops wakes)", {}
                values["wakes_per_hour"] = int(n)
            if not values:
                return 400, "cap needs cost_per_day and/or wakes_per_hour", {}
            path = d / "chores.toml"
            try:
                text = path.read_bytes().decode()  # bytes, so a CRLF file keeps its line endings
            except FileNotFoundError:
                return 409, f"no chores.toml in {d}", {}
            except UnicodeDecodeError as e:
                return 409, f"chores.toml is not UTF-8, nothing was written: {e}", {}
            try:
                before = tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                return 409, f"chores.toml does not parse, nothing was written: {e}", {}
            new_text = _rewrite_budget(text, {k: repr(v) for k, v in values.items()})
            # the rewrite is textual: read it back and require that the two keys, and nothing else,
            # changed, so a `[budget]` written some other way (dotted keys, an inline table) is
            # refused rather than broken for the resident's next reload
            try:
                after = tomllib.loads(new_text)
            except tomllib.TOMLDecodeError:
                after = None
            budget = before.get("budget", {})
            if not isinstance(budget, dict) or after != {**before, "budget": {**budget, **values}}:
                return 409, "chores.toml's [budget] could not be rewritten in place; nothing was written", {}
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_bytes(new_text.encode())
            os.chmod(tmp, path.stat().st_mode & 0o7777)
            os.replace(tmp, path)
            detail = " ".join(f"{k}={v}" for k, v in values.items())
            return 200, detail, {**values, "budget": after["budget"]}

        def log_message(self, fmt: str, *args) -> None:  # one line per request is noise here
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    bound = server.server_address[1]
    hosts.update({f"{host}:{bound}", f"127.0.0.1:{bound}", f"localhost:{bound}"})
    for a in allow or []:
        hosts.add(f"{a}:{bound}")
    origins.update(f"{scheme}://{h}" for scheme in ("http", "https") for h in hosts)
    server.hosts = frozenset(hosts)  # type: ignore[attr-defined]
    return server


def serve(
    registry: Path,
    host: str,
    port: int,
    refresh: int,
    identity: tuple[str, Khive] | None = None,
    allow: list[str] | None = None,
) -> None:
    """`make_server`, run until Ctrl-C."""
    server = make_server(registry, host, port, refresh, identity, allow)
    bound = server.server_address[1]
    me = identity[0] if identity else None
    who = f"you are {me}" if me else "no identity"
    # flushed: under nohup or launchd stdout is a file, and the line would wait for the buffer
    print(
        f"lion areas on http://{host}:{bound}/ (registry {registry}); {who}; "
        f"answers to {', '.join(sorted(server.hosts))}; Ctrl-C ends",  # type: ignore[attr-defined]
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def areas_main(a: argparse.Namespace) -> None:
    registry = Path(a.file).expanduser()
    if a.serve is not None:
        serve(registry, a.host, a.serve, a.refresh, identity(a.as_dir), a.allow)
        return
    areas = load_areas(registry)
    rows = status(areas)
    if a.json:
        print(
            json.dumps(
                {"areas": [vars(x) for x in areas], "residents": json.loads(rows_json(rows))}, indent=1
            )
        )
    else:
        print(table(areas, rows))
