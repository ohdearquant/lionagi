"""The lion pack's verbs (ADR-0025 D4, D6) answered in memory: the test double of the store's binary.
`answer(ops)` takes the DSL the client sends (one call, a `[parallel]` batch, or an `a | b` chain)
and returns the results in order, or raises `KhiveError` with the kind the pack would give. The
stub `kkernel` of `conftest.py` runs the same class over a JSON file, so a test through the real
client and a test through a patched `exec` see one behaviour. `blob.put` and `blob.get` are served
too, for a message's carried fields (D8); a ref here is a SHA-256, where the store's is a BLAKE3."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lion1 import khive as khive_module
from lion1.khive import Khive, KhiveError, lit
from lion1.store import Store

__all__ = (
    "LionFake",
    "LionKhive",
    "actor_dir",
    "parse",
    "patch_khive",
    "resident_store",
    "routed_config",
    "store",
    "unflushed_store",
    "unread",
)

_SOURCES = ("lion1", "claude_code", "codex")
_ROLES = ("system", "user", "assistant", "tool")
_STATUSES = ("ok", "refused", "limit", "cut", "failed")
_OUTCOMES = ("success", "continue", "exhausted", "refused", "stopped")
_PIECE_BYTES = 90 * 1024  # as the pack's (D8): every piece of a carried field but the last
_REFS_INLINE = 256  # as the pack's: past this many pieces, the refs are an index
_PAGE_BYTES = 2 * 1024 * 1024  # as the pack's `lion.messages` page (D9)
_MARKER = "[{} bytes of text carried in the blob store, read by lion.messages]"


def _split(text: str, sep: str) -> list[str]:
    """Top-level split: outside JSON strings and nested brackets."""
    parts, depth, quoted, start, i = [], 0, False, 0, 0
    while i < len(text):
        c = text[i]
        if quoted:
            if c == "\\":
                i += 1
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            start = i + len(sep)
            i += len(sep) - 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def parse(ops: str) -> list[tuple[str, dict]]:
    """`verb(args)` calls in order: a `[a, b]` batch or an `a | b` chain is its calls."""
    ops = ops.strip()
    if ops.startswith("[") and ops.endswith("]"):
        return [c for part in _split(ops[1:-1], ",") for c in parse(part)]
    if len(_split(ops, "|")) > 1:
        return [c for part in _split(ops, "|") for c in parse(part)]
    verb, _, rest = ops.partition("(")
    assert rest.endswith(")"), ops
    args: dict = {}
    for pair in _split(rest[:-1], ","):
        k, _, v = pair.partition("=")
        args[k.strip()] = json.loads(v, parse_constant=_bareword)
    return [(verb.strip(), args)]


def _bareword(word: str) -> None:
    # Python's JSON takes NaN and Infinity; the pack's parser refuses them, and so does this one
    raise ValueError(f"{word!r} is a bareword, not a valid value")


def _fresh() -> str:
    return uuid.uuid4().hex


@dataclass
class LionFake:
    sessions: dict = field(default_factory=dict)
    messages: dict = field(default_factory=dict)  # session id -> {seq: row}
    calls: dict = field(default_factory=dict)
    runs: dict = field(default_factory=dict)
    steps: dict = field(default_factory=dict)  # flow run id -> {idx: row}
    notes: dict = field(default_factory=dict)  # profile -> {key: row}
    blobs: dict = field(default_factory=dict)  # ref -> the piece's bytes, base64
    ops: list = field(default_factory=list)
    sent: list = field(default_factory=list)  # each comm.send's arguments: a result mail is read here
    refuse_send: str | None = None  # set: every comm.send raises it, a transport that failed
    clock: int = 1_000_000

    def answer(self, ops: str) -> list:
        self.ops.append(ops)
        return [self.one(verb, args) for verb, args in parse(ops)]

    def _now(self) -> int:
        self.clock += 1
        return self.clock

    def one(self, verb: str, p: dict) -> Any:
        if verb == "comm.send":  # the one other verb a run's actor calls: its result mail (ADR-0026 C4)
            if self.refuse_send is not None:
                raise KhiveError(self.refuse_send)
            self.sent.append(dict(p))
            return {"id": f"m{len(self.sent)}", "thread_id": p.get("thread_id") or f"t{len(self.sent)}"}
        if verb == "blob.put":
            data = base64.b64decode(p["bytes"], validate=True)
            ref = hashlib.sha256(data).hexdigest()
            self.blobs[ref] = p["bytes"]
            return {"content_ref": ref, "size": len(data)}
        if verb == "blob.get":
            if p["content_ref"] not in self.blobs:
                raise KhiveError(
                    f"blob.get: no object stored under content_ref {p['content_ref']}", "not_found"
                )
            b64 = self.blobs[p["content_ref"]]
            return {"content_ref": p["content_ref"], "bytes": b64, "size": len(base64.b64decode(b64))}
        if not verb.startswith("lion."):
            raise KhiveError(f"{verb}: the lion fake serves lion.* and blob.put/get only")
        name = verb[len("lion.") :]
        if name == "session_open":
            if p["source"] not in _SOURCES:
                raise KhiveError(f"source {p['source']!r}: lion1, claude_code or codex")
            sid = p.get("id") or _fresh()
            self.sessions[sid] = {
                "id": sid,
                "namespace": "local",
                "source": p["source"],
                "provider_session_id": p.get("provider_session_id"),
                "actor": p.get("actor"),
                "profile": p.get("profile"),
                "transcript": p.get("transcript"),
                "message_count": 0,
                "started": self._now(),
                "ended": None,
            }
            self.messages.setdefault(sid, {})
            return {"id": sid, "started": self.sessions[sid]["started"]}
        if name == "session_close":
            s = self._row(self.sessions, p["id"], "session")
            s["ended"] = self._now()
            s["message_count"] = len(self.messages.get(p["id"], {}))
            return dict(s)
        if name in ("message_append", "message_append_carried"):
            if (name == "message_append_carried") != ("carried" in p):
                raise KhiveError(
                    "a row with carried fields goes through lion.message_append_carried"
                    if name == "message_append"
                    else "lion.message_append_carried takes carried"
                )
            if p["role"] not in _ROLES:
                raise KhiveError(f"role {p['role']!r}: system, user, assistant or tool")
            self._row(self.sessions, p["session_id"], "session")
            box = self.messages[p["session_id"]]
            if p["seq"] in box:
                raise KhiveError(f"session {p['session_id']} already holds seq {p['seq']}")
            text = p["text"]
            carried = p.get("carried")
            if carried is not None:
                if not carried or set(carried) - {"text", "raw", "directive"}:
                    raise KhiveError(
                        f"carried: text, raw and directive only, at least one: {sorted(carried)}"
                    )
                for field, own in (
                    ("text", text or None),
                    ("raw", p.get("raw")),
                    ("directive", p.get("directive")),
                ):
                    if field in carried:
                        if own is not None:
                            raise KhiveError(f"a carried {field} leaves the row's own {field} empty")
                        whole = self._whole(field, carried[field])
                        if field == "text":
                            text = whole
            box[p["seq"]] = {
                "session_id": p["session_id"],
                "seq": p["seq"],
                "namespace": "local",
                "role": p["role"],
                "kind": p["kind"],
                "turn": p.get("turn") or 0,
                "alias": p.get("alias"),
                # as the pack: a carried text's column names it (D11)
                "text": _MARKER.format(carried["text"]["size"])
                if carried and "text" in carried
                else p["text"],
                "raw": p.get("raw"),
                "content_hash": f"h{len(text)}",
                "parent_seq": p.get("parent_seq"),
                "directive": p.get("directive"),
                "created": self._now(),
                "carried": json.dumps(carried) if carried is not None else None,
            }
            r = box[p["seq"]]
            return {
                "session_id": r["session_id"],
                "seq": r["seq"],
                "content_hash": r["content_hash"],
                "created": r["created"],
            }
        if name == "call_open":
            if p["seq_to"] < p["seq_from"]:
                raise KhiveError(f"seq_to {p['seq_to']} before seq_from {p['seq_from']}")
            cid = _fresh()
            self.calls[cid] = {
                "id": cid,
                "namespace": "local",
                "session_id": p["session_id"],
                "turn": p["turn"],
                "model": p["model"],
                "seq_from": p["seq_from"],
                "seq_to": p["seq_to"],
                "folded": p.get("folded"),
                "reply_seq": None,
                "tokens_in": None,
                "tokens_out": None,
                "cost": None,
                "duration_ms": None,
                "status": "open",
                "error": None,
                "opened": self._now(),
                "closed": None,
            }
            return {"id": cid, "opened": self.calls[cid]["opened"]}
        if name == "call_close":
            if p["status"] not in _STATUSES:
                raise KhiveError(f"status {p['status']!r}: ok, refused, limit, cut or failed")
            if p["status"] == "ok" and p.get("reply_seq") is None:
                raise KhiveError("an ok call names its reply_seq")
            c = self._row(self.calls, p["id"], "call")
            c.update(
                status=p["status"],
                reply_seq=p.get("reply_seq"),
                tokens_in=p.get("tokens_in"),
                tokens_out=p.get("tokens_out"),
                cost=p.get("cost"),
                duration_ms=p.get("duration_ms"),
                error=p.get("error"),
                closed=self._now(),
            )
            return {"id": p["id"], "closed": c["closed"]}
        if name == "run_open":
            rid = p.get("id") or _fresh()
            self.runs[rid] = {
                "id": rid,
                "namespace": "local",
                "actor": p.get("actor"),
                "profile": p["profile"],
                "session_id": p["session_id"],
                "bounds": p.get("bounds"),
                "trigger": p.get("trigger"),
                "continues": p.get("continues"),
                "outcome": None,
                "cost": None,
                "turns": None,
                "output": None,
                "started": self._now(),
                "ended": None,
            }
            if (s := p.get("step")) is not None:
                self.steps.setdefault(s["flow_run_id"], {})[s["idx"]] = {
                    "flow_run_id": s["flow_run_id"],
                    "idx": s["idx"],
                    "namespace": "local",
                    "name": s["name"],
                    "profile": p["profile"],
                    "run_id": rid,
                    "inputs_from": json.dumps(s["inputs_from"]) if s.get("inputs_from") is not None else None,
                    "outcome": None,
                }
            return {"id": rid, "started": self.runs[rid]["started"]}
        if name == "run_close":
            if p["outcome"] not in _OUTCOMES:
                raise KhiveError(
                    f"outcome {p['outcome']!r}: success, continue, exhausted, refused or stopped"
                )
            r = self._row(self.runs, p["id"], "run")
            r.update(
                outcome=p["outcome"],
                cost=p.get("cost"),
                turns=p.get("turns"),
                output=p.get("output"),
                ended=self._now(),
            )
            for steps in self.steps.values():
                for st in steps.values():
                    if st["run_id"] == p["id"]:
                        st["outcome"] = p["outcome"]
            return {"id": p["id"], "ended": r["ended"]}
        if name == "get":
            table, key = p["table"], p["id"]
            if table == "messages":
                rows = [self.messages.get(key, {})[k] for k in sorted(self.messages.get(key, {}))]
                if any(r.get("carried") for r in rows):  # the pack's refusal: this form shows a blank (D9)
                    raise KhiveError(f"session {key} holds carried rows: read it with lion.messages")
                return {"count": len(rows), "rows": rows}
            if table == "steps":
                rows = [self.steps.get(key, {})[k] for k in sorted(self.steps.get(key, {}))]
                return {"count": len(rows), "rows": rows}
            store = {"sessions": self.sessions, "calls": self.calls, "runs": self.runs}.get(table)
            if store is None:
                raise KhiveError(f"table {table!r}: sessions, messages, calls, runs or steps")
            return dict(self._row(store, key, table))
        if name == "messages":
            limit = p.get("limit", 200)
            if not 1 <= limit <= 1000:
                raise KhiveError(f"limit {limit}: 1 to 1000")
            box = self.messages.get(p["session_id"], {})
            after = p.get("after", -1)
            rows = [dict(box[k]) for k in sorted(box) if k > after][: limit + 1]
            page, size = [], 0
            for r in rows[:limit]:
                n = len(json.dumps(r))
                if page and size + n > _PAGE_BYTES:
                    break
                size += n
                page.append(r)
            more = len(page) < len(rows)
            return {"count": len(page), "rows": page, "next_after": page[-1]["seq"] if more else None}
        if name == "runs":
            limit = p.get("limit", 20)
            if not 1 <= limit <= 200:
                raise KhiveError(f"limit {limit}: 1 to 200")
            rows = sorted(self.runs.values(), key=lambda r: -r["started"])
            if p.get("actor") is not None:
                rows = [r for r in rows if r["actor"] == p["actor"]]
            rows = [dict(r) for r in rows[:limit]]
            return {"count": len(rows), "rows": rows}
        if name == "sessions":
            limit = p.get("limit", 20)
            if not 1 <= limit <= 200:
                raise KhiveError(f"limit {limit}: 1 to 200")
            rows = sorted(self.sessions.values(), key=lambda s: -s["started"])
            for key in ("source", "actor"):
                if p.get(key) is not None:
                    rows = [s for s in rows if s[key] == p[key]]
            rows = [dict(s) for s in rows[:limit]]
            return {"count": len(rows), "rows": rows}
        if name.startswith("note_"):
            return self._note(name, p)
        raise KhiveError(f"unknown verb: lion does not serve {verb!r}")

    def _refs(self, field: str, m: dict) -> list[str]:
        if (
            not isinstance(m.get("size"), int)
            or m["size"] < 1
            or set(m) not in ({"size", "pieces"}, {"size", "index"})
        ):
            raise KhiveError(
                f"carried {field}: {m!r} is not {{size, pieces}} or {{size, index}} of at least one byte"
            )
        k = -(-m["size"] // _PIECE_BYTES)
        if ("pieces" in m) != (k <= _REFS_INLINE):
            raise KhiveError(
                f"carried {field}: {k} pieces are listed when at most {_REFS_INLINE}, and indexed past that"
            )
        if "index" in m:
            if m["index"].get("size") != 64 * k:
                raise KhiveError(
                    f"carried {field}: an index of {m['index'].get('size')} bytes for {k} pieces"
                )
            text = self._whole(field, m["index"])
            refs = [text[i : i + 64] for i in range(0, len(text), 64)]
        else:
            refs = m["pieces"]
        if len(refs) != k:
            raise KhiveError(f"carried {field}: {m['size']} bytes are {k} pieces, not {len(refs)}")
        return refs

    def _whole(self, field: str, m: dict) -> str:
        """A carried field's text from its pieces, refused as the pack refuses it."""
        refs, pieces, left = self._refs(field, m), [], m["size"]
        for i, ref in enumerate(refs):
            if ref not in self.blobs:
                raise KhiveError(f"carried {field} piece {i} ({ref}) is not in the blob store", "not_found")
            piece, want = base64.b64decode(self.blobs[ref]), min(left, _PIECE_BYTES)
            if len(piece) != want:
                held = len(piece)
                raise KhiveError(f"carried {field} piece {i} holds {held} bytes where its place takes {want}")
            pieces.append(piece)
            left -= len(piece)
        try:
            return b"".join(pieces).decode()
        except UnicodeDecodeError:
            raise KhiveError(f"carried {field}: its bytes are not UTF-8") from None

    def _row(self, store: dict, key: str, what: str) -> dict:
        if key not in store:
            raise KhiveError(f"not found: {what} {key} is not in this namespace", kind="not_found")
        return store[key]

    def _note(self, name: str, p: dict) -> Any:
        if not p.get("profile") or (name != "note_list" and name != "note_find" and not p.get("key")):
            raise KhiveError("a note has a profile and a key")
        box = self.notes.setdefault(p["profile"], {})
        if name == "note_put":
            json.loads(p["value"])  # the value is JSON, as the pack checks
            prior = box.get(p["key"])
            box[p["key"]] = {
                "key": p["key"],
                "value": p["value"],
                "run": p["run"],
                "seq": p["seq"],
                "version": (prior["version"] + 1) if prior else 1,
                "written": self._now(),
            }
            return dict(box[p["key"]])
        if name == "note_get":
            return dict(self._row(box, p["key"], f"note {p['key']} of {p['profile']}"))
        if name == "note_list":
            keys = sorted(k for k in box if k.startswith(p.get("prefix") or ""))
            return {"count": len(keys), "rows": [dict(box[k]) for k in keys]}
        if name == "note_find":
            if not p["query"].strip():
                raise KhiveError("note_find: a blank query would match every note; list instead")
            q = p["query"].lower()
            keys = sorted(k for k, n in box.items() if q in k.lower() or q in n["value"].lower())
            return {"count": len(keys), "rows": [dict(box[k]) for k in keys]}
        if name == "note_delete":
            was = self._row(box, p["key"], f"note {p['key']} of {p['profile']}")
            del box[p["key"]]
            return dict(was)
        raise KhiveError(f"unknown verb: lion does not serve lion.{name!r}")

    # the stub binary's persistence: the whole state as one JSON document
    def to_json(self) -> str:
        return json.dumps(
            {
                "sessions": self.sessions,
                "messages": {k: {str(s): r for s, r in v.items()} for k, v in self.messages.items()},
                "calls": self.calls,
                "runs": self.runs,
                "steps": {k: {str(i): r for i, r in v.items()} for k, v in self.steps.items()},
                "notes": self.notes,
                "blobs": self.blobs,
                "clock": self.clock,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> LionFake:
        d = json.loads(text)
        return cls(
            sessions=d["sessions"],
            messages={k: {int(s): r for s, r in v.items()} for k, v in d["messages"].items()},
            calls=d["calls"],
            runs=d["runs"],
            steps={k: {int(i): r for i, r in v.items()} for k, v in d["steps"].items()},
            notes=d["notes"],
            blobs=d.get("blobs", {}),
            clock=d["clock"],
        )


class LionKhive:
    """A `Khive` whose every op is answered by `LionFake` in this process: the store's double for a test
    that builds an `Actor`."""

    def __init__(self, actor: str = "actor") -> None:
        self.actor = actor
        self.cwd = None
        self.lion = LionFake()

    async def exec(self, ops: str) -> list:
        return self.lion.answer(ops)

    def exec_sync(self, ops: str) -> list:
        return self.lion.answer(ops)


def store() -> Store:
    return Store(LionKhive())


class _Unflushed(Store):
    async def flush(self, run) -> int:
        run.store_flushed = len(run.record)
        return 0

    async def close(self, run) -> None:
        outcome = type(run.outcome).__name__.lower() if run.outcome is not None else "stopped"
        cost = run.cost if math.isfinite(run.cost) else None  # as `Store.close`: the store takes no NaN
        await self.khive.exec(
            f"lion.run_close(id={lit(run.id)}, outcome={lit(outcome)}, cost={lit(cost)}, "
            f"turns={run.turn}, output=null) | lion.session_close(id={lit(run.id)})"
        )


def unflushed_store() -> Store:
    """`store()` that writes no message rows and no output, for a test whose run holds a message of
    megabytes: the in-memory pack parses each op a character at a time, and under coverage that write
    alone takes the whole of a service test's 10 s bound. What a row holds is the store's tests' to say."""
    return _Unflushed(LionKhive())


def routed_config(actor: str, d: Path, packs: tuple[str, ...] = ()) -> str:
    """The khive config a directory of this runtime holds: the actor, and the lion pack's tables
    routed to their own file beside the main store (ADR-0026 C8); `packs` adds a `[runtime]` line."""
    text = f"[actor]\nid = {json.dumps(actor)}\n"
    if packs:
        text += f"\n[runtime]\npacks = {json.dumps(list(packs))}\n"
    main, lion = d / ".khive" / "main.db", d / ".khive" / "lion.db"
    text += f'\n[[backends]]\nname = "main"\npath = {json.dumps(str(main))}\n'
    text += f'\n[[backends]]\nname = "lion"\npath = {json.dumps(str(lion))}\n'
    text += '\n[packs.lion]\nbackend = "lion"\n'
    return text


def actor_dir(d: Path, actor: str = "console") -> Path:
    """`d` made a directory whose `.khive/config.toml` names `actor` and routes the lion pack's
    tables, the file a `Khive` and a `Store` over it need."""
    (d / ".khive").mkdir(parents=True, exist_ok=True)
    (d / ".khive" / "config.toml").write_text(routed_config(actor, d))
    return d


_NO_BINARY = Path(__file__).resolve().parent / "bin" / "kkernel"  # refuses if it is ever run


def patch_khive(monkeypatch) -> LionFake:
    """Every `Khive` in the process answered by one `LionFake`, `exec` and `exec_sync` both, so no
    subprocess runs for an op. The client resolves its binary to `tests/bin/kkernel`, which refuses to
    run, so a machine with no `kkernel` builds the same client. The fake, for a test that reads its rows."""
    lion = LionFake()
    which = shutil.which
    monkeypatch.setattr(
        khive_module,
        "shutil",
        SimpleNamespace(which=lambda name: str(_NO_BINARY) if name == "kkernel" else which(name)),
    )

    async def exec_(self, ops: str) -> list:
        return lion.answer(ops)

    def exec_sync(self, ops: str) -> list:
        return lion.answer(ops)

    monkeypatch.setattr(Khive, "exec", exec_)
    monkeypatch.setattr(Khive, "exec_sync", exec_sync)
    return lion


def resident_store(d: Path):
    """The `agent` notes of a resident directory under the stub binary: its `.khive/config.toml` written
    to name the actor its chores.toml names (the real binary resolves the actor from that file) and to
    route the lion pack's tables (ADR-0026 C8), then
    the store `hub.areas.agent_store` reads. `unread(d)` takes the config away again."""
    from hub.areas import agent_store, chores_toml  # the stub binary imports this module: keep it light

    (d / ".khive").mkdir(exist_ok=True)
    (d / ".khive" / "config.toml").write_text(routed_config(chores_toml(d)["actor"], d))
    return agent_store(d)


def unread(d: Path) -> None:
    (d / ".khive" / "config.toml").unlink()
