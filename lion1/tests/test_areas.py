"""`lion areas`: the registry, one row per resident read from its own files (serving is the pid
file's lock, the wake and today's spend the agent's own notes), the table and the page, and the
served JSON; one resident's page (its wakes from serve.log, the conversation with it, the form that
mails it) and the served routes behind it."""

import asyncio
import fcntl
import http.client
import ipaddress
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import lion_cli.areas as areas_cli
import pytest
from lion_cli.areas import _peer_allowed, _rewrite_budget, _tail, make_server

from hub.agent.agent import _line
from hub.areas import (
    _WAKE,
    Area,
    agent_store,
    controls_info,
    identity,
    load_areas,
    log_tail,
    page,
    resident,
    resident_page,
    status,
    table,
    talk,
    waiting_mail,
    wakes,
)
from lion1.khive import KhiveError, lit
from tests.lionfake import resident_store, unread

TODAY = "2026-09-22"
WAKE_LINE = (
    "2026-09-23 20:14:46 wake 1: Success after 1 turns, 1 mail, 1 sent; 1 model call(s), 512345 in, "
    "4321 out, $0.1234; 39 idle poll(s) before it at 0 tokens"
)
RESTART = "serving lambda:x:desk from /x as pid 4321; ticks created: []; programs gh=/g git=/t"


@pytest.fixture(autouse=True)
def _stub(kkernel_stub):
    """Every resident directory here reads its notes through the stub binary (ADR-0025 D6)."""


class FakeKhive:
    """What the page calls on a Khive, recorded: no kkernel, no store, no mail."""

    def __init__(self, sent: list[dict] | None = None, got: list[dict] | None = None):
        self.ops: list[str] = []
        self.calls: list[dict] = []
        self.sent, self.got = sent or [], got or []

    async def exec(self, ops: str) -> list:
        self.ops.append(ops)
        if ops.startswith("comm.inbox("):
            return [{"messages": self.sent if 'box="sent"' in ops else self.got}]
        return [{"id": "x"}]


def fetch(url: str | urllib.request.Request, timeout: float = 10) -> bytes:
    """A GET's body with its connection closed: the suite reads an unclosed socket as an error. A page
    reads each resident's notes through the stub binary, a Python process per read, so under a full
    suite's load a few residents take seconds; the bound is the stub's, measured, never the page's."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def agent_dir(tmp_path: Path, name: str, *, role: str, cursor: dict, spend: dict | None) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    section = {"desk": "[desk]\nevery = 'every:5m'\n"}.get(role, "")
    (d / "chores.toml").write_text(f'actor = "lambda:x:{name}"\nowner = "lambda:x"\nmodel = "m"\n{section}')
    store = resident_store(d)
    store.put("cursor", cursor, run="-", seq=0)
    if spend is not None:
        store.put("spend", spend, run="-", seq=0)
    return d


def test_a_residents_notes_are_refused_where_its_config_puts_them_in_the_main_store(tmp_path, kkernel_stub):
    """`agent_store` builds a `Store`, so it refuses as every store does (ADR-0026 C8): a resident whose
    config does not route the lion pack's tables gets no note store and nothing reaches the binary; the
    page says the notes are unread."""
    d = tmp_path / "r"
    (d / ".khive").mkdir(parents=True)
    (d / "chores.toml").write_text('actor = "lambda:x:r"\n')
    (d / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x:r"\n')
    with pytest.raises(KhiveError, match="would resolve to the main store"):
        agent_store(d)
    assert not kkernel_stub.with_name("lion.json").exists()  # the binary never ran
    problem = resident(Area("a", "lambda:a", [str(d)]), str(d), TODAY).problem
    assert problem.startswith("notes: ") and "would resolve to the main store" in problem
    resident_store(d).put("cursor", {"wake": 1}, run="-", seq=0)  # the control: routed, the same call writes
    assert kkernel_stub.with_name("lion.json").exists()


def test_the_registry_reads_areas_and_a_missing_file_is_no_areas(tmp_path):
    reg = tmp_path / "areas.toml"
    reg.write_text('[area.a]\nchair = "lambda:a"\ndesks = ["/x", "/y"]\n\n[area.b]\nchair = "lambda:b"\n')
    assert load_areas(reg) == [Area("a", "lambda:a", ["/x", "/y"]), Area("b", "lambda:b", [])]
    assert load_areas(tmp_path / "none.toml") == []
    reg.write_text("[area.c]\ndesks = []\n")
    with pytest.raises(ValueError, match=r"\[area.c\] has no chair"):
        load_areas(reg)


def test_a_residents_row_reads_its_own_files_and_serving_is_the_lock_not_the_pid_file(tmp_path):
    d = agent_dir(
        tmp_path,
        "desk",
        role="desk",
        cursor={"wake": 7, "at": f"{TODAY} 11:09:15", "outcome": "Success"},
        spend={"day": TODAY, "runs": 7, "mail": 9, "cost": 0.0123},
    )
    area = Area("x", "lambda:x", [str(d)])
    r = resident(area, str(d), TODAY)
    assert (r.actor, r.role, r.model, r.wake, r.last_outcome) == ("lambda:x:desk", "desk", "m", 7, "Success")
    assert (r.runs_today, r.mail_today, r.cost_today, r.problem) == (7, 9, 0.0123, "")
    assert r.serving is False and r.pid is None and r.in_wake is False
    # a pid file nobody locks (a stale one) is not serving; a locked one is, whatever the pid says
    (d / "agent.pid").write_text("424242")
    r = resident(area, str(d), TODAY)
    assert r.serving is False and r.pid == 424242
    fd = os.open(d / "agent.pid", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = resident(area, str(d), TODAY)
        assert r.serving is True and r.pid == 424242
    finally:
        os.close(fd)
    # yesterday's spend is not today's; a wake in flight reads as in_wake; a missing config is a problem
    m = agent_dir(
        tmp_path,
        "chores",
        role="chores",
        cursor={"wake": 3, "at": f"{TODAY} 12:00:00", "outcome": "running"},
        spend={"day": "2026-09-21", "runs": 40, "mail": 40, "cost": 1.0},
    )
    r = resident(area, str(m), TODAY)
    assert (
        r.role == "chores" and r.in_wake is True and (r.runs_today, r.mail_today, r.cost_today) == (0, 0, 0.0)
    )
    (m / "chores.toml").unlink()
    r = resident(area, str(m), TODAY)
    assert r.actor == "?" and r.problem.startswith("chores.toml: ")
    never = tmp_path / "never"
    never.mkdir()
    (never / "chores.toml").write_text('actor = "lambda:x:n"\n')
    resident_store(never)
    r = resident(area, str(never), TODAY)
    assert r.problem == "no notes yet: never ran from this directory" and r.wake == 0


def test_the_table_and_the_page_say_the_counts_and_each_residents_state(tmp_path):
    d = agent_dir(
        tmp_path,
        "desk",
        role="desk",
        cursor={"wake": 2, "at": f"{TODAY} 10:00:00", "outcome": "Success"},
        spend=None,
    )
    areas = [Area("x", "lambda:x", [str(d)]), Area("empty", "lambda:e", [])]
    rows = status(areas, TODAY)
    text = table(areas, rows)
    assert text.startswith(
        "2 resident(s) in 2 area(s), 0 serving, 0 in a wake".replace("2 resident", "1 resident")
    )
    assert "x: chair lambda:x, 1 resident(s)" in text and "empty: chair lambda:e, 0 resident(s)" in text
    row = "lambda:x:desk (desk, m): stopped; wake 2 at 2026-09-22 10:00:00 Success; today 0 run(s), 0 mail"
    assert row + ", $0.0000" in text
    html = page(areas, rows, refresh=3)
    assert '<meta http-equiv=refresh content="3">' in html and "<span class=off>stopped</span>" in html
    assert (
        "lambda:x:desk" in html and "chair lambda:x" in html and "1 resident(s), 2 area(s), 0 serving" in html
    )
    assert table([], []).startswith("no areas:") and "no areas:" in page([], [])


def test_the_served_page_and_json_reread_the_registry_on_every_request(tmp_path):
    d = agent_dir(
        tmp_path,
        "desk",
        role="desk",
        cursor={"wake": 1, "at": f"{TODAY} 10:00:00", "outcome": "Success"},
        spend=None,
    )
    reg = tmp_path / "areas.toml"
    reg.write_text(f'[area.x]\nchair = "lambda:x"\ndesks = ["{d}"]\n')
    port = serve_on(reg)
    rows = json.loads(fetch(f"http://127.0.0.1:{port}/api/areas", timeout=1))
    assert [r["actor"] for r in rows] == ["lambda:x:desk"] and rows[0]["serving"] is False
    html = fetch(f"http://127.0.0.1:{port}/").decode()
    assert "lambda:x:desk" in html and 'content="2"' in html
    reg.write_text(f'[area.x]\nchair = "lambda:x"\ndesks = ["{d}"]\n[area.y]\nchair = "lambda:y"\n')
    html = fetch(f"http://127.0.0.1:{port}/").decode()
    assert "chair lambda:y" in html  # re-read, not cached
    with pytest.raises(urllib.error.HTTPError) as e:
        fetch(f"http://127.0.0.1:{port}/nope")
    e.value.close()


def test_the_command_prints_the_table_from_a_given_registry(tmp_path):
    d = agent_dir(
        tmp_path,
        "desk",
        role="desk",
        cursor={"wake": 1, "at": f"{TODAY} 10:00:00", "outcome": "Success"},
        spend=None,
    )
    reg = tmp_path / "areas.toml"
    reg.write_text(f'[area.x]\nchair = "lambda:x"\ndesks = ["{d}"]\n')
    done = subprocess.run(
        [sys.executable, "-m", "lion_cli.main", "areas", "--file", str(reg)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert done.returncode == 0, done.stderr
    assert (
        "1 resident(s) in 1 area(s), 0 serving, 0 in a wake" in done.stdout and "lambda:x:desk" in done.stdout
    )


def test_wakes_read_the_serve_log_keep_restarts_and_odd_wake_lines_and_a_missing_log_is_no_rows(tmp_path):
    d = tmp_path / "resident"
    d.mkdir()
    assert wakes(str(d)) == [] and log_tail(str(d)) == ""
    failed = "2026-09-23 20:15:00 wake 2 failed: KhiveError: down"
    lines = ["Traceback (most recent call last):", RESTART, WAKE_LINE, failed, "2026-09-23 20:16:00 held: x"]
    (d / "serve.log").write_text("\n".join(lines) + "\n")
    rows = wakes(str(d))[::-1]  # the page shows newest first; oldest first here to index by line
    assert rows[0] == {"at": "", "raw": RESTART, "restart": True}
    assert rows[1] == {
        "at": "2026-09-23 20:14:46",
        "wake": 1,
        "outcome": "Success",
        "turns": 1,
        "mail": 1,
        "sent": 1,
        "calls": 1,
        "tokens_in": 512345,
        "tokens_out": 4321,
        "cost": 0.1234,
        "idle": 39,  # an older line: no context, no restarts, neither key
        "raw": WAKE_LINE,
    }
    assert rows[2] == {"at": "2026-09-23 20:15:00", "raw": failed}  # kept whole, never dropped
    assert len(rows) == 3  # the traceback and the held line are not wake rows
    assert wakes(str(d), n=1) == rows[-1:]
    assert log_tail(str(d), n=2).splitlines() == lines[-2:]
    assert log_tail(str(d)).splitlines() == lines[1:]  # this run only: the older traceback is cut


def test_identity_is_none_without_a_config_or_without_an_actor_id(tmp_path):
    assert identity(tmp_path) is None
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\ndisplay_name = "no id"\n')
    assert identity(tmp_path) is None


def test_talk_merges_my_sent_box_and_my_inbox_oldest_first_and_marks_nothing_read():
    def msg(i: str, at: str, frm: str, to: str) -> dict:
        return {"id": i, "from": frm, "to": to, "subject": f"s{i}", "content": f"c{i}", "created_at": at}

    # 03.5Z sorts before 03Z as text: the order is by time; b is in both boxes and is shown once
    khive = FakeKhive(
        sent=[
            msg("b", "2026-09-23T20:00:02Z", "person", "lambda:x"),
            msg("d", "2026-09-23T20:00:03Z", "person", "lambda:x"),
        ],
        got=[
            msg("c", "2026-09-23T20:00:03.5Z", "lambda:x", "person"),
            msg("a", "2026-09-23T20:00:01Z", "lambda:x", "person"),
            msg("b", "2026-09-23T20:00:02Z", "person", "lambda:x"),
        ],
    )
    rows = asyncio.run(talk(khive, "lambda:x"))
    assert [r["id"] for r in rows] == ["a", "b", "d", "c"]
    assert rows[1] == {
        "at": "2026-09-23T20:00:02Z",
        "from": "person",
        "to": "lambda:x",
        "subject": "sb",
        "content": "cb",
        "id": "b",
    }
    assert sorted(khive.ops) == [
        'comm.inbox(box="sent", to_actor="lambda:x", limit=50)',
        'comm.inbox(from_actor="lambda:x", status="all", limit=50)',
    ]
    assert [r["id"] for r in asyncio.run(talk(khive, "lambda:x", limit=2))] == ["d", "c"]


def test_the_resident_page_shows_its_wakes_the_talk_and_the_form_only_with_an_identity(tmp_path):
    d = agent_dir(
        tmp_path,
        "desk",
        role="desk",
        cursor={"wake": 1, "at": "2026-09-23 20:14:46", "outcome": "Success"},
        spend=None,
    )
    (d / "serve.log").write_text(f"{RESTART}\n{WAKE_LINE}\n")
    row = resident(Area("x", "lambda:x", [str(d)]), str(d), TODAY)
    said = {
        "at": "2026-09-23T20:14:46Z",
        "from": "person",
        "to": "lambda:x:desk",
        "subject": "hello <desk>",
        "content": "line one\nline <two>",
        "id": "m1",
    }
    html = resident_page(row, wakes(str(d)), log_tail(str(d)), [said], "person", 7, str(d))
    assert "<h2>lambda:x:desk" in html and '<a href="/">' in html
    assert "<td>512345</td>" in html and "<td>4321</td>" in html and "<td>$0.1234</td>" in html
    assert "<td>39</td>" in html and "<tr class=restart><td colspan=11>restarted as pid " in html
    assert "<b>hello &lt;desk&gt;</b>" in html and "line one\nline &lt;two&gt;</pre>" in html
    assert "person → lambda:x:desk" in html and "<details><summary>log tail</summary>" in html
    assert "<form method=post action=/send " in html and f'name=dir value="{d}"' in html
    assert 'name=once value="' in html and "required" in html
    assert "<input name=subject" in html and "<textarea name=content" in html
    assert "setTimeout(tick,7000)" in html and "http-equiv=refresh" not in html
    assert html.count("<script>") == 1 and "no identity" not in html
    bare_reply = resident_page(row, [], "", [{**said, "subject": ""}], "person", 7, str(d), sent="m9")
    assert "<b></b>" not in bare_reply and "sent, message m9" in bare_reply
    bare = resident_page(row, [], "", [], None, 7, str(d))
    assert "no identity: run lion areas --serve PORT --as DIR where DIR holds .khive/config.toml" in bare
    assert "<form" not in bare and "no serve.log yet" in bare
    down = resident_page(row, [], "", "kkernel exited 1: down", "person", 7, str(d))
    assert "the conversation could not be read: kkernel exited 1: down" in down and "<form" in down


def test_the_served_resident_page_reads_and_mails_only_a_registered_resident_as_the_identity(tmp_path):
    d = agent_dir(
        tmp_path,
        "desk",
        role="desk",
        cursor={"wake": 1, "at": "2026-09-23 20:14:46", "outcome": "Success"},
        spend=None,
    )
    (d / "serve.log").write_text(f"{WAKE_LINE}\n")
    reg = tmp_path / "areas.toml"
    reg.write_text(f'[area.x]\nchair = "lambda:x"\ndesks = ["{d}"]\n')

    def start(ident) -> int:
        return serve_on(reg, ident)

    def post(port: int, form: dict) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)  # never follows the 303
        body = urllib.parse.urlencode(form)
        head = {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{port}"}
        conn.request("POST", "/send", body, head)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp

    khive = FakeKhive()
    port = start(("person", khive))
    q = urllib.parse.urlencode({"dir": str(d)})
    index = fetch(f"http://127.0.0.1:{port}/").decode()
    assert f'<a href="/r?{q}">lambda:x:desk</a>' in index and "you are person" in index
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/r?{q}", timeout=2) as got:
        html = got.read().decode()
    assert got.status == 200 and "<h2>lambda:x:desk" in html and "<td>512345</td>" in html
    assert khive.ops and all(o.startswith("comm.inbox(") for o in khive.ops)  # read, never marked read
    with pytest.raises(urllib.error.HTTPError) as e:
        fetch(f"http://127.0.0.1:{port}/r?dir=/tmp/elsewhere")
    e.value.close()
    assert e.value.code == 404
    api = json.loads(fetch(f"http://127.0.0.1:{port}/api/resident?{q}"))
    assert set(api) == {"resident", "wakes", "tail", "talk", "me", "controls", "budget", "wake", "waiting"}
    assert api["me"] == "person" and api["resident"]["actor"] == "lambda:x:desk" and api["talk"] == []
    assert api["wakes"][0]["tokens_in"] == 512345 and api["tail"] == WAKE_LINE
    khive.ops.clear()
    assert post(port, {"dir": str(d), "subject": "s", "content": "  \n "}).status == 400
    assert post(port, {"dir": "/tmp/elsewhere", "content": "hi"}).status == 404
    assert khive.ops == []  # nothing sent for either
    # a form on another site, or a rebound host name, gets nothing: no read, no send
    forged = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    forged.request(
        "POST",
        "/send",
        urllib.parse.urlencode({"dir": str(d), "content": "hi"}),
        {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://evil.example"},
    )
    refused = forged.getresponse()
    refused.read()
    forged.close()
    assert refused.status == 403 and khive.ops == []
    rebound = urllib.request.Request(f"http://127.0.0.1:{port}/r?{q}", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as rebound_err:
        fetch(rebound)
    rebound_err.value.close()
    assert rebound_err.value.code == 403 and khive.ops == []
    content = 'hi "there"\nsecond line'
    once = "ab" * 16
    sent = post(port, {"dir": str(d), "subject": "", "content": content, "to": "lambda:evil", "once": once})
    assert sent.status == 303 and sent.getheader("Location") == f"/r?{q}&sent=x"
    (op,) = khive.ops
    assert op.startswith("comm.send(") and 'to="lambda:x:desk"' in op and lit(content) in op
    assert f'idempotency_key="ui:{once}"' in op and khive.calls == []
    assert "lambda:evil" not in op and "subject=" not in op  # the recipient is never a form value
    bare = start(None)
    assert post(bare, {"dir": str(d), "content": "hi"}).status == 400
    assert "no identity: run lion areas" in fetch(f"http://127.0.0.1:{bare}/r?{q}").decode()


# ---- the API the phone app reads ----

CHORES = (
    'actor = "lambda:x:desk"\nmodel = "m"\n[desk]\nevery = "every:5m"\n\n'
    "# the caps, edited from the phone\n"
    "[budget]\n"
    "cost_per_day = 1.5  # dollars, the day's cap\n"
    "# a comment inside the table\n"
    "wakes_per_hour = 6\n"
    "hops_per_thread = 12\n"
    "\n"
    "[wake]\n"
    'default = "batch"\n'
    'batch_after = "30m"\n'
    "batch_size = 5\n"
    "quiet = []\n"
    "[[wake.rule]]\n"
    'from = ["lambda:owner"]\n'
    'then = "now"\n'
)
LABEL = "com.example.x.agent"
ROW_KEYS = [
    "area",
    "chair",
    "dir",
    "actor",
    "role",
    "model",
    "serving",
    "pid",
    "in_wake",
    "wake",
    "last_at",
    "last_outcome",
    "runs_today",
    "mail_today",
    "cost_today",
    "unknown_today",
    "problem",
    "controls",
    "waiting",
]


def app_dir(root: Path, *, plists: int = 1, deferred: list | None = None) -> Path:
    cursor = {"wake": 45, "at": "2026-09-24 10:52:43", "outcome": "Quiet"}
    d = agent_dir(root, "desk", role="desk", cursor=cursor, spend=None)
    (d / "chores.toml").write_text(CHORES)
    for i in range(plists):
        with open(d / f"job{i}.plist", "wb") as f:
            plistlib.dump({"Label": LABEL, "ProgramArguments": ["/usr/bin/true"]}, f)
    if deferred is not None:
        agent_store(d).put("deferred", {"mail": deferred}, run="-", seq=0)
    return d


def registry(root: Path, *desks: Path) -> Path:
    reg = root / "areas.toml"
    reg.write_text('[area.x]\nchair = "lambda:x"\ndesks = [' + ", ".join(f'"{d}"' for d in desks) + "]\n")
    return reg


_servers: list = []  # every server a test started, ended by `servers_end` so no thread outlives its test


@pytest.fixture(autouse=True)
def servers_end():
    yield
    while _servers:
        server = _servers.pop()
        server.shutdown()
        server.server_close()


def serve_on(reg: Path, ident=None, allow: list[str] | None = None) -> int:
    server = make_server(reg, "127.0.0.1", 0, 2, ident, allow)  # port 0: the kernel's free one
    _servers.append(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def call(port: int, method: str, path: str, body: str | None = None, headers: dict | None = None):
    """(status, body text, headers); http.client sends no Origin, as a native client does not."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body, headers or {})
    r = conn.getresponse()
    text = r.read().decode()
    conn.close()
    return r.status, text, dict(r.getheaders())


def control(port: int, doc: dict, headers: dict | None = None) -> tuple[int, dict]:
    code, text, head = call(
        port, "POST", "/control", json.dumps(doc), {"Content-Type": "application/json", **(headers or {})}
    )
    assert head["Content-Type"] == "application/json"  # /control always answers JSON
    return code, json.loads(text)


def areas_rows(port: int) -> list[dict]:
    return json.loads(fetch(f"http://127.0.0.1:{port}/api/areas"))


def recorder(path: Path, record: Path, rc: int, err: str = "") -> Path:
    """A launchctl that writes its argv, one per line, to `record` and exits `rc`."""
    said = f'echo "{err}" >&2\n' if err else ""
    path.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{record}"\n{said}exit {rc}\n')
    path.chmod(0o755)
    return path


class FailingKhive(FakeKhive):
    async def exec(self, ops: str) -> list:
        self.ops.append(ops)
        raise KhiveError("kkernel exited 1: down")


def test_the_wake_pattern_reads_a_line_with_and_without_the_context_suffixes_and_each_alone(tmp_path):
    base = {"wake": 45, "outcome": "Quiet", "turns": 1, "mail": ["a", "b"], "sent": 0, "idle_polls": 0}
    tokens = {"calls": 1, "in": 56129, "out": 1038, "cost": 0.0624}
    at = "2026-09-24 10:52:43"  # serve prints each line after the time (apps/cli/lion_cli/agent.py)
    old = f"{at} {_line({**base, 'tokens': tokens})}"
    ctx = f"{at} {_line({**base, 'tokens': {**tokens, 'context': 28324}})}"
    both = f"{at} {_line({**base, 'tokens': {**tokens, 'context': 28324, 'restarts': 2}})}"
    alone = f"{at} {_line({**base, 'tokens': {**tokens, 'restarts': 1}})}"
    assert ctx.endswith("before it at 0 tokens; last turn stood in 28324 tokens")
    assert both.endswith("; last turn stood in 28324 tokens, 2 session restart(s)")
    assert alone.endswith("before it at 0 tokens, 1 session restart(s)")
    for line, context, restarts in (
        (old, None, None),
        (ctx, "28324", None),
        (both, "28324", "2"),
        (alone, None, "1"),
    ):
        m = _WAKE.match(line)
        assert m and (m["wake"], m["tokens_in"], m["cost"]) == ("45", "56129", "0.0624"), line
        assert (m["context"], m["restarts"]) == (context, restarts), line
    d = tmp_path / "r"
    d.mkdir()
    (d / "serve.log").write_text("\n".join([old, ctx, both, alone]) + "\n")
    rows = wakes(str(d))[::-1]
    assert "context" not in rows[0] and "restarts" not in rows[0]  # an older line: absent
    assert rows[1] == {  # the contract's wake row, field for field
        "at": at,
        "wake": 45,
        "outcome": "Quiet",
        "turns": 1,
        "mail": 2,
        "sent": 0,
        "calls": 1,
        "tokens_in": 56129,
        "tokens_out": 1038,
        "cost": 0.0624,
        "idle": 0,
        "context": 28324,
        "restarts": 0,
        "raw": ctx,
    }
    assert (rows[2]["context"], rows[2]["restarts"]) == (28324, 2)
    assert "context" not in rows[3] and rows[3]["restarts"] == 1


def test_a_days_cost_that_misses_calls_of_unknown_cost_is_marked_partial_in_every_view(tmp_path):
    cursor = {"wake": 2, "at": f"{TODAY} 11:09:15", "outcome": "Success"}
    partial = {"day": TODAY, "runs": 2, "mail": 2, "cost": 0.3, "unknown": 1}
    d = agent_dir(tmp_path, "desk", role="desk", cursor=cursor, spend=partial)
    whole_day = {"day": TODAY, "runs": 1, "mail": 1, "cost": 0.3, "unknown": 0}  # counted: none missed
    done = agent_dir(tmp_path, "whole", role="desk", cursor=cursor, spend=whole_day)
    area = Area("x", "lambda:x", [str(d), str(done)])
    r, whole = resident(area, str(d), TODAY), resident(area, str(done), TODAY)
    assert (r.cost_today, r.unknown_today, whole.unknown_today) == (0.3, 1, 0)
    mark = "$0.3000 (partial: 1 call(s) of unknown cost)"
    text = table([area], [r, whole])
    assert f"today 2 run(s), 2 mail, {mark}\n" in text and text.endswith("today 1 run(s), 1 mail, $0.3000")
    html = page([area], [r, whole])
    assert f"<td>2 run(s), 2 mail, {mark}</td>" in html and "<td>1 run(s), 1 mail, $0.3000</td>" in html
    base = {"wake": 2, "outcome": "Success", "turns": 1, "mail": ["a"], "sent": 1, "idle_polls": 4}
    tokens = {"calls": 2, "in": 900, "out": 40, "cost": 0.3, "unknown": 1, "context": 800}
    line = f"{TODAY} 11:09:15 {_line({**base, 'tokens': tokens})}"
    m = _WAKE.match(line)
    assert m and (m["cost"], m["unknown"], m["idle"], m["context"]) == ("0.3000", "1", "4", "800"), line
    (d / "serve.log").write_text(f"{line}\n{WAKE_LINE}\n")
    rows = wakes(str(d))[::-1]
    assert (rows[0]["cost"], rows[0]["unknown"], rows[0]["context"]) == (0.3, 1, 800)
    assert "unknown" not in rows[1]  # the older line prints no count
    html = resident_page(r, wakes(str(d)), "", [], None, 7, str(d))
    assert f"today 2 run(s), 2 mail, {mark}</p>" in html and f"<td>{mark}</td>" in html
    assert "<td>$0.1234</td>" in html  # the complete wake beside it carries no mark


@pytest.mark.parametrize(
    "before",
    [
        {"day": TODAY, "runs": 2, "mail": 2, "cost": 0.3},
        {"day": TODAY, "runs": 2, "mail": 2, "cost": 0.3, "unknown": None},
    ],
)
def test_a_days_row_from_before_the_count_is_marked_partial_by_an_unknown_number_in_every_view(
    tmp_path, before
):
    # the row as the version before the count left it (no key), and as the first wake after it writes it
    cursor = {"wake": 2, "at": f"{TODAY} 11:09:15", "outcome": "Success"}
    d = agent_dir(tmp_path, "desk", role="desk", cursor=cursor, spend=before)
    counted = {"day": TODAY, "runs": 1, "mail": 1, "cost": 0.3, "unknown": 0}
    done = agent_dir(tmp_path, "whole", role="desk", cursor=cursor, spend=counted)
    area = Area("x", "lambda:x", [str(d), str(done)])
    r, whole = resident(area, str(d), TODAY), resident(area, str(done), TODAY)
    assert (r.cost_today, r.unknown_today, whole.unknown_today) == (0.3, None, 0)
    mark = "$0.3000 (partial: calls of unknown cost before this count went uncounted)"
    text = table([area], [r, whole])
    assert f"today 2 run(s), 2 mail, {mark}\n" in text and text.endswith("today 1 run(s), 1 mail, $0.3000")
    html = page([area], [r, whole])
    assert f"<td>2 run(s), 2 mail, {mark}</td>" in html and "<td>1 run(s), 1 mail, $0.3000</td>" in html
    html = resident_page(r, wakes(str(d)), "", [], None, 7, str(d))
    assert f"today 2 run(s), 2 mail, {mark}</p>" in html


@pytest.mark.parametrize(
    ("spend", "read"),
    [
        ({"day": TODAY, "runs": 2, "mail": 2, "cost": -0.6, "unknown": 0}, (0.0, None)),
        ({"day": TODAY, "runs": 2, "mail": 2, "unknown": 0}, (0.0, None)),
        ({"day": TODAY, "runs": 2, "mail": 2, "cost": 0.3, "unknown": "2"}, (0.3, None)),
        ({"runs": 2, "mail": 2, "cost": 0.3, "unknown": 0}, (0.0, None)),
        ([TODAY, 0.3, 0], (0.0, None)),
        ({"day": "2000-01-01", "runs": 2, "mail": 2, "cost": -0.6, "unknown": "2"}, (0.0, 0)),
    ],
    ids=["negative", "no-cost", "str-count", "no-day", "not-a-row", "another-day"],
)
def test_a_days_row_the_agent_would_not_have_written_reads_in_the_views_as_the_cap_reads_it(
    tmp_path, spend, read
):
    cursor = {"wake": 2, "at": f"{TODAY} 11:09:15", "outcome": "Success"}
    d = agent_dir(tmp_path, "desk", role="desk", cursor=cursor, spend=spend)
    area = Area("x", "lambda:x", [str(d)])
    r = resident(area, str(d), TODAY)
    assert (r.cost_today, r.unknown_today) == read
    mark = f"${read[0]:.4f}" + (
        "" if read[1] == 0 else " (partial: calls of unknown cost before this count went uncounted)"
    )
    assert table([area], [r]).endswith(mark)


def test_waiting_is_the_deferred_notes_mail_and_every_row_carries_its_count_and_the_controls(tmp_path):
    deferred = [
        {
            "id": "d1d1d1d1",
            "from": "x",
            "properties": {"from_actor": "daemon:review"},
            "subject": "verdict",
            "content": "the body stays in the note",
            "deferred_at": "2026-09-24 11:02:10",
            "deferred_ts": 1790000000.0,
        },
        {
            "id": "e2",
            "from": "lambda:owner",
            "subject": None,
            "deferred_at": "2026-09-24 11:03:00",
            "deferred_ts": 5,
        },
    ]
    d = app_dir(tmp_path, deferred=deferred)
    assert waiting_mail(str(d)) == [
        {
            "id": "d1d1d1d1",
            "from": "daemon:review",
            "subject": "verdict",
            "deferred_at": "2026-09-24 11:02:10",
            "deferred_ts": 1790000000.0,
        },
        {
            "id": "e2",
            "from": "lambda:owner",
            "subject": "",
            "deferred_at": "2026-09-24 11:03:00",
            "deferred_ts": 5.0,
        },
    ]
    assert waiting_mail(str(tmp_path)) is None  # no actor to read as: unread, never a count of zero
    port = serve_on(registry(tmp_path, d))
    (row,) = areas_rows(port)
    assert list(row) == ROW_KEYS and row["waiting"] == 2 and row["dir"] == str(d)
    assert row["controls"] == {
        "held_by_file": False,
        "hold_reason": "",
        "wake_now_pending": False,
        "launchd_label": LABEL,
        "idle": True,
    }
    q = urllib.parse.urlencode({"dir": str(d)})
    api = json.loads(fetch(f"http://127.0.0.1:{port}/api/resident?{q}"))
    assert list(api) == ["resident", "wakes", "tail", "talk", "me", "controls", "budget", "wake", "waiting"]
    assert api["resident"] == row and api["controls"] == row["controls"]  # the row of /api/areas
    assert api["waiting"] == waiting_mail(str(d)) and api["me"] is None and api["talk"] == []
    assert api["budget"] == {"cost_per_day": 1.5, "wakes_per_hour": 6, "hops_per_thread": 12}
    assert api["wake"] == {
        "default": "batch",
        "batch_after": "30m",
        "batch_size": 5,
        "quiet": [],
        "rule": [{"from": ["lambda:owner"], "then": "now"}],
    }
    (d / "chores.toml").write_text('actor = "lambda:x:desk"\n')  # a missing table is {}
    api = json.loads(fetch(f"http://127.0.0.1:{port}/api/resident?{q}"))
    assert api["budget"] == {} and api["wake"] == {}
    unread(d)  # notes that cannot be read: the count is null on the row and the page, never zero
    (row,) = areas_rows(port)
    api = json.loads(fetch(f"http://127.0.0.1:{port}/api/resident?{q}"))
    assert row["waiting"] is None and api["waiting"] is None
    code, text, _ = call(port, "GET", "/api/resident?dir=/tmp/elsewhere")
    assert code == 404 and text.startswith("no resident at '/tmp/elsewhere'")


def test_the_guards_refuse_a_peer_off_the_tailnet_a_foreign_host_or_origin_and_admit_a_native_post(
    tmp_path, monkeypatch
):
    middle = str(ipaddress.ip_network("100.64.0.0/10")[1 << 21])  # the range's middle, no device's address
    inside = ("127.0.0.1", "::1", "100.64.0.1", middle, "100.127.255.254", "::ffff:100.70.1.2")
    assert all(_peer_allowed(a) for a in inside)
    outside = ("100.63.255.255", "100.128.0.1", "192.168.1.20", "8.8.8.8", "::ffff:8.8.8.8", "", "x")
    assert not any(_peer_allowed(a) for a in outside)
    d = app_dir(tmp_path)
    port = serve_on(registry(tmp_path, d), allow=["device.example.net", "100.64.0.1"])
    # Host: the bound host:port, 127.0.0.1, localhost and every --allow; nothing else
    for host in (f"device.example.net:{port}", f"100.64.0.1:{port}", f"localhost:{port}"):
        assert call(port, "GET", "/api/areas", headers={"Host": host})[0] == 200
    code, text, _ = call(port, "GET", "/api/areas", headers={"Host": f"evil.example:{port}"})
    assert code == 403 and "is not this server" in text
    hold = {"dir": str(d), "action": "hold", "reason": "r"}
    code, doc = control(port, hold, {"Host": f"evil.example:{port}"})
    assert code == 403 and doc["ok"] is False and not (d / "hold").exists()
    # Origin: present, it must be http(s):// + an allowed host; absent (a native client), it passes
    for origin in ("http://evil.example", f"http://evil.example:{port}", "null", f"ftp://127.0.0.1:{port}"):
        code, doc = control(port, hold, {"Origin": origin})
        assert code == 403 and "is not this server" in doc["detail"] and not (d / "hold").exists(), origin
    for origin in (f"https://device.example.net:{port}", f"http://127.0.0.1:{port}"):
        assert control(port, {**hold, "action": "resume"}, {"Origin": origin})[0] == 200
    code, doc = control(port, hold)  # no Origin header at all
    assert code == 200 and doc["ok"] is True and (d / "hold").exists()
    # a GET's Origin is not checked (C11 names a POST): its answer carries no CORS header, so a page
    # on another site cannot read it
    foreign = {"Host": f"localhost:{port}", "Origin": "http://evil.example"}
    code, _, head = call(port, "GET", "/api/areas", headers=foreign)
    assert code == 200 and not [k for k in head if k.lower().startswith("access-control-")]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)  # `/events` writes its own head
    conn.request("GET", "/events", headers=foreign)
    resp = conn.getresponse()
    head = dict(resp.getheaders())
    resp.close()
    conn.close()
    assert resp.status == 200 and not [k for k in head if k.lower().startswith("access-control-")]
    # a peer off the tailnet: refused before the body is read, nothing done
    (d / "hold").unlink()
    seen: list[str] = []
    monkeypatch.setattr(areas_cli, "_peer_allowed", lambda a: seen.append(a) or False)
    code, doc = control(port, hold)
    assert code == 403 and "outside loopback/Tailscale" in doc["detail"] and not (d / "hold").exists()
    assert seen == ["127.0.0.1"] and call(port, "GET", "/api/areas")[0] == 403


def test_control_holds_resumes_and_wakes_from_a_json_or_a_form_body_and_notes_each_control(tmp_path):
    d = app_dir(tmp_path)
    port = serve_on(registry(tmp_path, d), ident=("person", FakeKhive()))
    code, doc = control(port, {"dir": str(d), "action": "hold", "reason": "lunch\nthen more"})
    assert (code, doc) == (200, {"ok": True, "action": "hold", "detail": "held: lunch then more"})
    first = (d / "hold").read_text().splitlines()[0]
    assert first.startswith("held by person from the app at ") and first.endswith(": lunch then more")
    (row,) = areas_rows(port)
    assert row["controls"]["held_by_file"] is True and row["controls"]["hold_reason"] == first
    assert control(port, {"dir": str(d), "action": "resume"}) == (
        200,
        {"ok": True, "action": "resume", "detail": "resumed"},
    )
    assert not (d / "hold").exists()
    assert control(port, {"dir": str(d), "action": "resume"})[1]["detail"] == "was not held"
    # form-encoded, as a browser form posts: the same control, a JSON answer
    form = urllib.parse.urlencode({"dir": str(d), "action": "wake"})
    code, text, head = call(
        port, "POST", "/control", form, {"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert code == 200 and head["Content-Type"] == "application/json"
    assert json.loads(text)["detail"] == "wake requested" and (d / "wake-now").read_text().startswith(
        "person at "
    )
    assert areas_rows(port)[0]["controls"]["wake_now_pending"] is True
    # JSON under a form Content-Type (URLSession's default when the header is left out) reads as JSON
    body = json.dumps({"dir": str(d), "action": "hold"})
    code, text, _ = call(
        port, "POST", "/control", body, {"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert code == 200 and (d / "hold").exists()
    # bad input is 400, a dir outside the registry 404 with nothing written; every answer JSON
    assert control(port, {"dir": str(d), "action": "explode"})[0] == 400
    code, text, _ = call(port, "POST", "/control", "{not json", {"Content-Type": "application/json"})
    assert code == 400 and json.loads(text) == {"ok": False, "action": "", "detail": "the body is not JSON"}
    code, text, _ = call(port, "POST", "/control", "[1]", {"Content-Type": "application/json"})
    assert code == 400 and json.loads(text)["detail"] == "the body is not a JSON object"
    other = tmp_path / "other"
    other.mkdir()
    code, doc = control(port, {"dir": str(other), "action": "hold"})
    assert code == 404 and doc["ok"] is False and doc["action"] == "hold"
    assert not (other / "hold").exists() and not (other / "landing").exists()
    log = (d / "landing" / "controls.log").read_text().splitlines()
    assert [line.split(" ")[1:4] for line in log] == [
        ["person", "hold", "200"],
        ["person", "resume", "200"],
        ["person", "resume", "200"],
        ["person", "wake", "200"],
        ["person", "hold", "200"],
        ["person", "explode", "400"],
    ]
    assert not (d / "serve.log").exists()  # the resident's own log is never written


def test_cap_rewrites_only_the_two_keys_keeps_every_comment_and_refuses_what_it_cannot_rewrite(tmp_path):
    d = app_dir(tmp_path)
    (d / "chores.toml").chmod(0o640)
    port = serve_on(registry(tmp_path, d))
    code, doc = control(port, {"dir": str(d), "action": "cap", "cost_per_day": 40, "wakes_per_hour": "60"})
    assert code == 200 and doc["ok"] is True and (doc["cost_per_day"], doc["wakes_per_hour"]) == (40.0, 60)
    assert doc["budget"] == {"cost_per_day": 40.0, "wakes_per_hour": 60, "hops_per_thread": 12}
    want = CHORES.replace("cost_per_day = 1.5  #", "cost_per_day = 40.0  #").replace(
        "wakes_per_hour = 6\n", "wakes_per_hour = 60\n"
    )
    assert (d / "chores.toml").read_text() == want  # every other byte kept, the comments too
    assert (d / "chores.toml").stat().st_mode & 0o777 == 0o640
    code, doc = control(port, {"dir": str(d), "action": "cap", "wakes_per_hour": 1})
    assert code == 200 and "cost_per_day" not in doc and doc["budget"]["cost_per_day"] == 40.0
    before = (d / "chores.toml").read_bytes()
    bad = [
        {"cost_per_day": -1},
        {"cost_per_day": "x"},
        {"cost_per_day": "nan"},
        {"cost_per_day": True},
        {"wakes_per_hour": 1.5},
        {"wakes_per_hour": -2},
        {"wakes_per_hour": 0},
        {},
    ]
    for fields in bad:
        code, doc = control(port, {"dir": str(d), "action": "cap", **fields})
        assert code == 400 and doc["ok"] is False, fields
    assert (d / "chores.toml").read_bytes() == before
    # a [budget] the text rewrite cannot reach in place is refused, the file untouched
    inline = 'actor = "lambda:x:desk"\nbudget = { cost_per_day = 1.0 }\n'
    (d / "chores.toml").write_text(inline)
    code, doc = control(port, {"dir": str(d), "action": "cap", "cost_per_day": 2})
    assert code == 409 and "could not be rewritten in place" in doc["detail"]
    assert (d / "chores.toml").read_text() == inline
    (d / "chores.toml").unlink()
    assert control(port, {"dir": str(d), "action": "cap", "cost_per_day": 2})[0] == 409
    # a missing table is appended; a missing key goes after the table's last key, not after blanks
    assert (
        _rewrite_budget('actor = "a"', {"cost_per_day": "2.0"})
        == 'actor = "a"\n\n[budget]\ncost_per_day = 2.0\n'
    )
    assert _rewrite_budget("", {"cost_per_day": "1.0"}) == "[budget]\ncost_per_day = 1.0\n"
    table = "[budget] # caps\ncost_per_day = 1.0\n\n# next\n[wake]\n"
    assert _rewrite_budget(table, {"wakes_per_hour": "3"}) == (
        "[budget] # caps\ncost_per_day = 1.0\nwakes_per_hour = 3\n\n# next\n[wake]\n"
    )


def test_restart_refuses_without_one_plist_or_in_a_wake_and_kickstarts_the_label(tmp_path, monkeypatch):
    record = tmp_path / "argv.txt"
    monkeypatch.setattr(areas_cli, "_LAUNCHCTL", str(recorder(tmp_path / "launchctl", record, 0)))
    none, two, one = (
        app_dir(tmp_path / "a", plists=0),
        app_dir(tmp_path / "b", plists=2),
        app_dir(tmp_path / "c"),
    )
    port = serve_on(registry(tmp_path, none, two, one))
    assert control(port, {"dir": str(none), "action": "restart"}) == (
        409,
        {"ok": False, "action": "restart", "detail": f"no launchd plist in {none}"},
    )
    code, doc = control(port, {"dir": str(two), "action": "restart"})
    assert code == 409 and doc["detail"].startswith("2 launchd plists in ") and not record.exists()
    code, doc = control(port, {"dir": str(one), "action": "restart"})
    assert code == 200 and doc["ok"] is True and (doc["label"], doc["rc"]) == (LABEL, 0)
    assert record.read_text().splitlines() == ["kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"]
    record.unlink()
    # in a wake: the serving pid has a child whose argv reads like the model CLI's; refused unless forced
    parent = subprocess.Popen(
        ["/bin/bash", "-c", '(exec -a "claude -p" /bin/sleep 30) & wait'], start_new_session=True
    )
    try:
        (one / "agent.pid").write_text(str(parent.pid))
        for _ in range(50):
            if not controls_info(str(one))["idle"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the child never showed to pgrep")
        assert [r["controls"]["idle"] for r in areas_rows(port) if r["dir"] == str(one)] == [False]
        code, doc = control(port, {"dir": str(one), "action": "restart"})
        assert (code, doc["detail"]) == (409, "in a wake") and not record.exists()
        code, doc = control(port, {"dir": str(one), "action": "restart", "force": "1"})
        assert code == 200 and record.read_text().splitlines()[-1] == f"gui/{os.getuid()}/{LABEL}"
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.wait()
    (one / "agent.pid").unlink()
    # launchctl refusing: 502 with what it said
    failing = recorder(tmp_path / "launchctl-fails", record, 113, "Could not find service")
    monkeypatch.setattr(areas_cli, "_LAUNCHCTL", str(failing))
    code, doc = control(port, {"dir": str(one), "action": "restart"})
    assert (
        code == 502 and doc["ok"] is False and doc["rc"] == 113 and "Could not find service" in doc["detail"]
    )


def test_cap_keeps_a_crlf_files_line_endings_and_refuses_one_that_is_not_utf8(tmp_path):
    d = app_dir(tmp_path)
    crlf = CHORES.replace("\n", "\r\n").encode()
    (d / "chores.toml").write_bytes(crlf)
    port = serve_on(registry(tmp_path, d))
    code, doc = control(port, {"dir": str(d), "action": "cap", "wakes_per_hour": 60})
    assert code == 200 and doc["wakes_per_hour"] == 60
    assert (d / "chores.toml").read_bytes() == crlf.replace(
        b"wakes_per_hour = 6\r\n", b"wakes_per_hour = 60\r\n"
    )
    (d / "chores.toml").write_bytes(b"# \xff\n" + crlf)
    code, doc = control(port, {"dir": str(d), "action": "cap", "wakes_per_hour": 7})
    assert code == 409 and doc["detail"].startswith("chores.toml is not UTF-8, nothing was written")


@pytest.mark.parametrize("text", ["\u00b2", "\u0662", "12a", ""])
def test_a_pid_file_holding_anything_but_ascii_digits_names_no_pid_and_never_raises(tmp_path, text):
    # str.isdigit admits a superscript int refuses and another script's digits int reads as a number
    d = agent_dir(tmp_path, "desk", role="desk", cursor={"wake": 3, "outcome": "Quiet"}, spend=None)
    area = Area("x", "lambda:x", [str(d)])
    (d / "agent.pid").write_text(text)
    r = resident(area, str(d), TODAY)
    assert (r.serving, r.pid, r.problem) == (False, None, "")
    fd = os.open(d / "agent.pid", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = resident(area, str(d), TODAY)
        assert (r.serving, r.pid) == (True, None)  # the lock is the claim; the text names no pid
        assert controls_info(str(d))["idle"] is True  # a quiet cursor, and no pid to ask pgrep about
    finally:
        os.close(fd)
    (d / "agent.pid").write_text("424242")  # the control: ASCII digits are a pid
    assert resident(area, str(d), TODAY).pid == 424242


def test_a_running_wake_under_a_held_lock_is_not_idle_without_a_model_child(tmp_path):
    """An in-process backend has no `claude -p` child: the cursor under a held lock says it is waking."""
    d = agent_dir(tmp_path, "desk", role="desk", cursor={"wake": 3, "outcome": "running"}, spend=None)
    (d / "agent.pid").write_text("424242")  # no such process: pgrep finds no child either way
    assert controls_info(str(d))["idle"] is True  # nobody holds the lock: a stale cursor, not a wake
    fd = os.open(d / "agent.pid", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    notes = agent_store(d)
    try:
        assert controls_info(str(d))["idle"] is False
        notes.put("cursor", {"wake": 3, "outcome": "Quiet"}, run="-", seq=1)
        assert controls_info(str(d))["idle"] is True
        notes.put("cursor", {"wake": 0}, run="-", seq=2)  # a cursor that names no outcome says nothing
        assert controls_info(str(d))["idle"] is False
        notes.delete("cursor")
        assert controls_info(str(d))["idle"] is False  # served, no cursor yet: a first wake may be starting
        unread(d)
        assert controls_info(str(d))["idle"] is False  # unread reads as busy
    finally:
        os.close(fd)


def test_a_pid_file_that_cannot_be_read_or_locked_is_that_residents_problem_and_no_holder(
    tmp_path, monkeypatch
):
    """Only a refused shared lock is a holder. A lock call that fails some other way, or a pid file
    that cannot be opened, is said on that resident's row; the other residents still get theirs,
    and the control surface reads the unknown as busy."""
    import errno

    from hub.areas import _held

    d = agent_dir(tmp_path, "desk", role="desk", cursor={"wake": 3, "outcome": "Quiet"}, spend=None)
    other = agent_dir(tmp_path, "chores", role="chores", cursor={"wake": 1, "outcome": "Quiet"}, spend=None)
    (d / "agent.pid").write_text("424242")
    area = Area("x", "lambda:x", [str(d), str(other)])
    assert _held(tmp_path / "gone.pid") == (False, None)  # vanished between a listing and the read
    real = fcntl.flock

    def no_locks(fd, op):
        if op & fcntl.LOCK_SH:
            raise OSError(errno.ENOLCK, "No locks available")
        return real(fd, op)

    monkeypatch.setattr(fcntl, "flock", no_locks)
    r = resident(area, str(d), TODAY)
    assert r.serving is False and r.pid is None  # neither claim survives a lock call that failed
    assert r.problem == f"agent.pid: [Errno {errno.ENOLCK}] No locks available"
    assert controls_info(str(d))["idle"] is False  # unknown is not idle: a restart would take `force`
    monkeypatch.setattr(fcntl, "flock", real)
    assert resident(area, str(d), TODAY).problem == ""  # the control: the same file, locks working
    os.chmod(d / "agent.pid", 0)
    try:
        rows = status([area], TODAY)
        assert [r.problem.split(":")[0] for r in rows] == ["agent.pid", ""] and rows[0].serving is False
        assert controls_info(str(d))["idle"] is False
    finally:
        os.chmod(d / "agent.pid", 0o644)
    assert [r.problem for r in status([area], TODAY)] == ["", ""]


def test_a_replayed_once_answers_the_first_result_and_acts_once(tmp_path, monkeypatch):
    record = tmp_path / "argv.txt"
    monkeypatch.setattr(areas_cli, "_LAUNCHCTL", str(recorder(tmp_path / "launchctl", record, 0)))
    d = app_dir(tmp_path)
    port = serve_on(registry(tmp_path, d))
    wake = {"dir": str(d), "action": "wake", "once": "c0" * 16}
    first = control(port, wake)
    assert first[0] == 200
    (d / "wake-now").unlink()  # the resident took it
    assert control(port, wake) == first and not (d / "wake-now").exists()  # answered, not written again
    restart = {"dir": str(d), "action": "restart", "once": "ab" * 16}
    assert control(port, restart) == control(port, restart)
    assert record.read_text().count("kickstart") == 1
    refused = {"dir": str(d), "action": "cap", "cost_per_day": -1, "once": "0f" * 16}
    first_refusal = control(port, refused)
    assert first_refusal[0] == 400
    assert control(port, {**refused, "cost_per_day": 3}) == first_refusal  # the once's first answer stands
    assert "cost_per_day = 1.5" in (d / "chores.toml").read_text()
    assert (
        control(port, {**wake, "once": "c1" * 16})[0] == 200 and (d / "wake-now").exists()
    )  # a new once acts
    assert len((d / "landing" / "controls.log").read_text().splitlines()) == 4


def test_send_answers_json_under_accept_from_a_json_or_form_body_with_no_origin(tmp_path):
    d = app_dir(tmp_path)
    khive = FakeKhive()
    port = serve_on(registry(tmp_path, d), ident=("person", khive))
    head = {"Content-Type": "application/json", "Accept": "application/json"}
    once = "de" * 16
    body = json.dumps({"dir": str(d), "subject": "s", "content": "hi", "once": once})
    code, text, got = call(port, "POST", "/send", body, head)
    assert (code, json.loads(text), got["Content-Type"]) == (200, {"ok": True, "id": "x"}, "application/json")
    (op,) = khive.ops
    assert 'to="lambda:x:desk"' in op and 'subject="s"' in op and f'idempotency_key="ui:{once}"' in op
    code, text, _ = call(port, "POST", "/send", json.dumps({"dir": "/tmp/elsewhere", "content": "hi"}), head)
    assert code == 404 and json.loads(text)["ok"] is False
    code, text, _ = call(port, "POST", "/send", json.dumps({"dir": str(d), "content": " "}), head)
    assert (code, json.loads(text)) == (400, {"ok": False, "detail": "content is empty: nothing was sent"})
    form = urllib.parse.urlencode({"dir": str(d), "content": "yo"})
    code, text, _ = call(
        port,
        "POST",
        "/send",
        form,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    assert code == 200 and json.loads(text)["ok"] is True
    code, _, got = call(
        port, "POST", "/send", json.dumps({"dir": str(d), "content": "yo"}), head | {"Accept": ""}
    )
    assert code == 303 and got["Location"].startswith("/r?")  # no Accept: the old page's redirect
    assert len(khive.ops) == 3
    down = serve_on(registry(tmp_path, d), ident=("person", FailingKhive()))
    code, text, _ = call(down, "POST", "/send", json.dumps({"dir": str(d), "content": "hi"}), head)
    assert (code, json.loads(text)) == (502, {"ok": False, "detail": "kkernel exited 1: down"})
    bare = serve_on(registry(tmp_path, d))
    code, text, _ = call(bare, "POST", "/send", json.dumps({"dir": str(d), "content": "hi"}), head)
    assert code == 400 and json.loads(text)["detail"].startswith("no identity")


def read_events(resp, until, timeout: float, ids: list | None = None) -> list[tuple[str, object]]:
    """(event, data) off an open `/events` response until `until(events)` holds; `ids`, when given, is
    appended each event's (name, id), the id None for an event sent without one. A pass reads every
    resident's notes through the stub binary (see `fetch`), so the callers' deadline is the stub's."""
    events: list[tuple[str, object]] = []
    event, sent, deadline = "", None, time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = resp.readline().decode()
        if line.startswith("id: "):
            sent = line[len("id: ") :].strip()
        elif line.startswith("event: "):
            event = line[len("event: ") :].strip()
        elif line.startswith("data: "):
            events.append((event, json.loads(line[len("data: ") :])))
            if ids is not None:
                ids.append((event, sent))
            sent = None
            if until(events):
                return events
    raise AssertionError(f"not within {timeout}s: {events}")


def test_events_open_with_the_rows_then_the_history_and_deliver_a_new_line_within_two_seconds(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(areas_cli, "_PING_S", 0.5)
    d = app_dir(tmp_path)
    old = [f"2026-09-24 10:{i:02d}:00 held: line {i}" for i in range(25)]
    (d / "serve.log").write_text("\n".join(old) + "\na partial")  # the last line is being written
    port = serve_on(registry(tmp_path, d))

    def handlers() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if "process_request_thread" in t.name]

    # a handler of an earlier request can still be ending, so the stream's thread is the one new here
    before = set(handlers())
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    (stream,) = [t for t in handlers() if t not in before]
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    got = read_events(resp, lambda ev: sum(e == "log" for e, _ in ev) == 20, 5)
    assert got[0][0] == "areas" and got[0][1] == areas_rows(port)
    history = [x for e, x in got[1:]]
    assert [x["line"] for x in history] == old[-20:]  # the last 20, the partial line not among them
    assert all(x["dir"] == str(d) and x["history"] is True for x in history)
    new = "2026-09-24 11:00:00 wake 46: Quiet after 1 turns, 1 mail, 0 sent; 1 model call(s), 1 in, 1 out"
    started = time.monotonic()
    with open(d / "serve.log", "a") as f:
        f.write(" line\n" + new + "\n")
    got = read_events(resp, lambda ev: any(e == "log" and x["line"] == new for e, x in ev), 30)
    assert time.monotonic() - started < 2.0
    live = [x for e, x in got if e == "log"]
    assert [(x["line"], x["history"]) for x in live] == [("a partial line", False), (new, False)]
    # a row that changes is a new `areas` event; `ping` keeps the stream open
    (d / "hold").write_text("held by person from the app at now: r\n")
    got = read_events(resp, lambda ev: {"areas", "ping"} <= {e for e, _ in ev}, 30)
    assert [x for e, x in got if e == "areas"][-1][0]["controls"]["held_by_file"] is True
    assert ("ping", {}) in got
    # the client gone: its thread ends at the next write
    assert stream.is_alive()
    resp.close()
    conn.close()
    stream.join(5)
    assert not stream.is_alive(), "the /events thread outlived its client"


def open_stream(port: int):
    """An open `/events` response and the connection under it. A pass of the stream reads every
    resident's notes through the stub binary (see `fetch`), so the socket waits as long as that takes."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/events")
    return conn, conn.getresponse()


def api_revision(port: int) -> int:
    code, _, head = call(port, "GET", "/api/areas")
    assert code == 200
    return int(head["X-Areas-Revision"])


def test_a_stream_opened_after_a_poll_carries_rows_no_older_than_the_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(areas_cli, "_POLL_S", 5.0)  # a stream opened next is served the cached read
    d = app_dir(tmp_path)
    port = serve_on(registry(tmp_path, d))
    conn, resp = open_stream(port)
    (first,) = read_events(resp, lambda ev: bool(ev), 30)
    assert first[0] == "areas" and first[1][0]["controls"]["held_by_file"] is False
    (d / "hold").write_text("held by person from the app at now: r\n")
    code, text, head = call(port, "GET", "/api/areas")  # the poll that saw the change
    polled = json.loads(text)
    assert code == 200 and polled[0]["controls"]["held_by_file"] is True
    later, again = open_stream(port)
    ids: list = []
    (second,) = read_events(again, lambda ev: bool(ev), 5, ids)
    assert second == ("areas", polled)  # the stream did not fall back to the read before the poll's
    assert int(ids[0][1]) >= int(head["X-Areas-Revision"])
    for c in (resp, again, conn, later):
        c.close()


def test_the_api_and_the_stream_stamp_their_rows_with_one_rising_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(areas_cli, "_PING_S", 0.5)
    d = app_dir(tmp_path)
    port = serve_on(registry(tmp_path, d))
    started = time.time_ns() // 1000
    polled = api_revision(port)
    assert polled >= started  # the count starts at the wall clock in microseconds
    (d / "hold").write_text("held by person from the app at now: r\n")
    assert api_revision(port) > polled  # a read of rows that changed is a later read
    polled = api_revision(port)
    conn, resp = open_stream(port)
    ids: list = []
    read_events(resp, lambda ev: bool(ev), 30, ids)
    streamed = int(ids[0][1])
    assert ids[0][0] == "areas" and streamed >= polled  # the stream serves the read the poll last made
    (d / "hold").unlink()
    read_events(resp, lambda ev: {"areas", "ping"} <= {e for e, _ in ev}, 5, ids)
    changed = [int(i) for e, i in ids if e == "areas"]
    assert changed[-1] > streamed  # a changed row is a later read
    assert all(i is None for e, i in ids if e != "areas")  # only an `areas` event is stamped
    (d / "hold").write_text("held by person from the app at now: r\n")
    assert api_revision(port) > changed[-1]  # and a poll of rows that changed after the stream is later
    resp.close()
    conn.close()


def test_the_stream_sends_a_read_above_the_last_it_sent_though_its_rows_are_those_it_sent(
    tmp_path, monkeypatch
):
    # the stream waits on a gate between its passes, so the polls come between two of them; a `ping` ends each
    # pass, and with the poll interval long the stream's read is the last read, whichever request made it
    gate = threading.Semaphore(0)
    clock = types.SimpleNamespace(
        monotonic=time.monotonic, time_ns=time.time_ns, sleep=lambda _s: gate.acquire(timeout=10)
    )
    monkeypatch.setattr(areas_cli, "time", clock)
    monkeypatch.setattr(areas_cli, "_POLL_S", 3600.0)
    monkeypatch.setattr(areas_cli, "_PING_S", 0.0)
    d = app_dir(tmp_path)
    port = serve_on(registry(tmp_path, d))
    conn, resp = open_stream(port)

    def a_pass() -> list[tuple[str, object, str | None]]:
        ids: list = []
        got = read_events(resp, lambda ev: ev[-1][0] == "ping", 30, ids)
        return [(e, x, i) for (e, x), (_, i) in zip(got, ids, strict=True)]

    try:
        sent = a_pass()[0]
        assert sent[0] == "areas" and sent[1][0]["controls"]["held_by_file"] is False
        (d / "hold").write_text("held by person from the app at now: r\n")
        code, text, _ = call(port, "GET", "/api/areas")  # a read that sees the change...
        assert code == 200 and json.loads(text)[0]["controls"]["held_by_file"] is True
        (d / "hold").unlink()
        code, text, head = call(port, "GET", "/api/areas")  # ...and one that sees it undone
        assert code == 200 and json.loads(text) == sent[1]  # the rows the stream sent, at a higher revision
        assert int(head["X-Areas-Revision"]) > int(sent[2])
        gate.release()
        again = a_pass()
        assert again[0] == ("areas", sent[1], head["X-Areas-Revision"])  # sent for its revision, not its rows
        gate.release()
        assert [e for e, _, _ in a_pass()] == ["ping"]  # a revision it has sent is not sent twice
    finally:
        resp.close()
        conn.close()
        gate.release(5)  # the stream's next writes find the client gone


def test_a_read_whose_rows_are_unchanged_keeps_the_revision_and_the_stream_stays_quiet(tmp_path, monkeypatch):
    # the same gated stream: the reads come between two of its passes, and a `ping` ends each pass
    gate = threading.Semaphore(0)
    clock = types.SimpleNamespace(
        monotonic=time.monotonic, time_ns=time.time_ns, sleep=lambda _s: gate.acquire(timeout=10)
    )
    monkeypatch.setattr(areas_cli, "time", clock)
    monkeypatch.setattr(areas_cli, "_POLL_S", 3600.0)
    monkeypatch.setattr(areas_cli, "_PING_S", 0.0)
    d = app_dir(tmp_path)
    port = serve_on(registry(tmp_path, d))
    conn, resp = open_stream(port)

    def a_pass() -> list[tuple[str, object, str | None]]:
        ids: list = []
        got = read_events(resp, lambda ev: ev[-1][0] == "ping", 30, ids)
        return [(e, x, i) for (e, x), (_, i) in zip(got, ids, strict=True)]

    try:
        sent = a_pass()[0]
        assert sent[0] == "areas" and sent[1][0]["controls"]["held_by_file"] is False
        for _ in range(2):  # two reads of rows that have not changed answer at the revision the stream sent
            code, text, head = call(port, "GET", "/api/areas")
            assert code == 200 and json.loads(text) == sent[1]
            assert head["X-Areas-Revision"] == sent[2]
        gate.release()
        assert [e for e, _, _ in a_pass()] == ["ping"]  # so the stream has nothing to send
        (d / "hold").write_text("held by person from the app at now: r\n")
        code, text, head = call(port, "GET", "/api/areas")  # a read whose rows differ takes a later revision
        assert code == 200 and json.loads(text)[0]["controls"]["held_by_file"] is True
        assert int(head["X-Areas-Revision"]) > int(sent[2])
        gate.release()
        again = a_pass()
        assert again[0] == ("areas", json.loads(text), head["X-Areas-Revision"])  # and the stream sends it
        assert [e for e, _, _ in again] == ["areas", "ping"]
    finally:
        resp.close()
        conn.close()
        gate.release(5)  # the stream's next writes find the client gone


def test_the_revision_continues_above_the_last_when_the_clock_stands_still_or_steps_back(
    tmp_path, monkeypatch
):
    now = [7_000_000_000_000_000]  # the wall clock in microseconds
    clock = types.SimpleNamespace(monotonic=time.monotonic, sleep=time.sleep, time_ns=lambda: now[0] * 1000)
    monkeypatch.setattr(areas_cli, "time", clock)
    d = app_dir(tmp_path)
    port = serve_on(registry(tmp_path, d))
    hold = d / "hold"

    def changed() -> int:  # a read of rows that changed: the hold file toggles one of them
        if hold.exists():
            hold.unlink()
        else:
            hold.write_text("held by person from the app at now: r\n")
        return api_revision(port)

    first = api_revision(port)
    assert first == now[0]  # the first read is stamped with the clock
    assert changed() == first + 1  # a clock that stood still still gives a later revision
    now[0] -= 5_000_000
    assert changed() == first + 2  # and one that stepped back
    now[0] += 50_000_000
    assert changed() == now[0]  # the clock ahead again leads


def test_the_log_tail_reads_history_then_whole_new_lines_and_starts_over_on_a_shrunk_log(tmp_path):
    log = tmp_path / "serve.log"
    assert _tail(log, None) == ([], (0, b"", 0))  # no log yet
    log.write_text("\n".join(f"l{i}" for i in range(30)) + "\npart")
    lines, state = _tail(log, None)
    assert lines == [(f"l{i}", True) for i in range(10, 30)] and state[1] == b"part"
    with open(log, "a") as f:
        f.write("ial\r\n\nnext\n")
    lines, state = _tail(log, state)
    assert lines == [("partial", False), ("next", False)] and _tail(log, state)[0] == []
    log.write_text("fresh\n")  # rotated: shorter than the offset read to
    assert _tail(log, state)[0] == [("fresh", False)]
    later = tmp_path / "later.log"  # a log that appears after the connection: new lines, not history
    lines, state = _tail(later, None)
    later.write_text("first\n")
    assert lines == [] and _tail(later, state)[0] == [("first", False)]
    swapped = tmp_path / "swap.log"  # another file at the name, no shorter: read from its start
    swapped.write_text("".join(f"old{i}\n" for i in range(100)))
    lines, state = _tail(swapped, None)
    fresh = tmp_path / "swap.new"
    fresh.write_text("".join(f"new{i:03d}\n" for i in range(200)))
    os.replace(fresh, swapped)
    lines, _ = _tail(swapped, state)
    assert [s for s, _ in lines] == [f"new{i:03d}" for i in range(200)]
    big = tmp_path / "big.log"  # past the 64 KiB read: the history is still the last 20 whole lines
    big.write_text("".join(f"{i:06d} {'x' * 40}\n" for i in range(3000)))
    lines, _ = _tail(big, None)
    assert [s for s, _ in lines] == [f"{i:06d} {'x' * 40}" for i in range(2980, 3000)]


def test_the_wake_pattern_reads_the_task_suffix_with_and_without_the_other_two(tmp_path):
    base = {"wake": 46, "outcome": "Quiet", "turns": 1, "mail": [], "sent": 0, "idle_polls": 3}
    tokens = {"calls": 1, "in": 40000, "out": 900, "cost": 0.05}
    at = "2026-09-24 12:00:00"
    only = f"{at} {_line({**base, 'tokens': tokens, 'tasks': ['t1', 't2']})}"
    all3 = f"{at} {_line({**base, 'tokens': {**tokens, 'context': 30000, 'restarts': 1}, 'tasks': ['t1']})}"
    assert only.endswith("before it at 0 tokens; 2 task(s) of the queue")
    assert all3.endswith("; last turn stood in 30000 tokens, 1 session restart(s); 1 task(s) of the queue")
    for line, tasks, context in ((only, "2", None), (all3, "1", "30000")):
        m = _WAKE.match(line)
        assert m and (m["wake"], m["tasks"], m["context"]) == ("46", tasks, context), line
    d = tmp_path / "r"
    d.mkdir()
    (d / "serve.log").write_text(only + "\n" + all3 + "\n")
    rows = wakes(str(d))[::-1]
    assert rows[0]["tasks"] == 2 and "context" not in rows[0] and rows[0]["outcome"] == "Quiet"
    assert (rows[1]["tasks"], rows[1]["context"], rows[1]["restarts"]) == (1, 30000, 1)


def test_a_plist_whose_root_is_not_a_dict_or_whose_label_is_not_a_name_is_no_label_not_a_crash(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "job.plist").write_bytes(plistlib.dumps(["not-a-dict"]))
    assert controls_info(str(d))["launchd_label"] is None
    (d / "job.plist").write_bytes(plistlib.dumps({"Label": ""}))
    assert controls_info(str(d))["launchd_label"] is None
    (d / "job.plist").write_bytes(b"<plist version='1.0'><dict><key>Label</key><string>x</string>")
    assert controls_info(str(d))["launchd_label"] is None
    (d / "job.plist").write_bytes(plistlib.dumps({"Label": "com.example.x"}))
    assert controls_info(str(d))["launchd_label"] == "com.example.x"


# ---- the phone page's script, run by node against a stand-in for the browser ----

APP_PAGE = Path(__file__).with_name("app_page_harness.js")


def app_session(tmp_path: Path, scenario: str) -> dict:
    """What one scripted session left behind: the page `/app` serves, its script run by node in
    `app_page_harness.js` over a document, an EventSource and a fetch the scenario of that name drives."""
    node = shutil.which("node")
    assert node, "node is not on PATH: the phone page's script is run under node"
    html = fetch(f"http://127.0.0.1:{serve_on(registry(tmp_path))}/app").decode()
    done = subprocess.run(
        [node, str(APP_PAGE), scenario], input=html, capture_output=True, encoding="utf-8", timeout=60
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_app_log_box_holds_400_nodes_after_405_lines_and_a_reader_scrolled_up_stays_put(tmp_path):
    seen = app_session(tmp_path, "log")
    # the oldest five lines left the box, and a box read at its bottom follows the newest
    assert seen["full"] == {"nodes": 400, "first": "line 5", "last": "line 404", "following": True}
    # three more lines arrive with the reader 50 lines up: the oldest three go and the window keeps its line
    assert seen["scrolled"] == {"nodes": 400, "before": "line 55", "after": "line 55"}
    # the box built again from the buffer holds the same 400 lines
    assert seen["rebuilt"] == {"nodes": 400, "first": "line 8", "last": "line 407"}


def test_the_app_re_render_of_the_page_a_reader_is_on_keeps_a_scrolled_up_readers_lines_in_view(tmp_path):
    seen = app_session(tmp_path, "logRefresh")
    # a reader 30 lines up stays on line 30 when a control's answer reloads the page and the box is rebuilt
    assert seen["before"] == "line 30" and seen["control"] == {"nodes": 100, "view": "line 30"}
    # the same after a send's answer, from another place in the box
    assert seen["send"] == {"view": "line 12"}
    # three lines arrive while the page loads and a full buffer drops its oldest three: line 55 stays in view
    assert seen["full"] == {"before": "line 55", "after": "line 55", "scrollTop": 47 * 20, "nodes": 400}


def test_the_app_re_render_follows_a_reader_at_the_bottom_and_a_new_page_starts_there(tmp_path):
    seen = app_session(tmp_path, "logFollow")
    # a reader at the bottom is at the bottom of the rebuilt box, the line that came while it loaded included
    assert seen["refreshed"] == {"last": "line 100", "following": True}
    # a page reached by navigation starts at the bottom, though the last one was left scrolled up
    assert seen["renewed"] == {"view": "line 91", "following": True}


def test_the_app_takes_an_event_or_a_live_poll_answer_only_above_the_revision_shown(tmp_path):
    seen = app_session(tmp_path, "pollOrder")
    assert seen["streamNewer"] == ["stream12"]  # the stream's rows, then an older poll answer: they stay
    # an event does not abort the poll out, so an answer of a higher revision is taken
    assert seen["pollAboveEvent"] == ["poll15"]
    assert seen["pollTaken"] == ["poll20"]  # and one that met no event
    # its revision is the one shown: an event under it, though above the stream's rows before it, is dropped
    assert seen["olderStreamAfter"] == ["poll20"] and seen["newerStreamAfter"] == ["stream22"]


def test_the_app_refuses_an_event_under_a_poll_answer_shown_and_shows_one_above_it(tmp_path):
    seen = app_session(tmp_path, "pollThenEvent")
    assert seen["polled"] == ["polled"]  # a poll answered after the stream opened, at revision 20
    # an event read before that poll, arriving after it, is refused: the rows stay
    assert seen["delayed"] == ["polled"]
    assert seen["polledAfter"] == 0  # the rows refused were the connection's first: the polling ended
    assert seen["later"] == ["later"]  # an event above it is shown


def test_the_app_shows_an_event_above_a_restarted_servers_poll_though_below_the_old_rows(tmp_path):
    seen = app_session(tmp_path, "restartEvent")
    # no stream opened since the error, so the stream is down: a poll below the revision shown is shown now
    assert seen["polled"] == ["restarted"]
    assert seen["below"] == ["restarted"]  # an event under it is refused
    # and one above it is shown, though below the old server's rows: the revision is set, not raised
    assert seen["above"] == ["fresh"]


def test_the_app_takes_a_poll_answer_whatever_its_revision_so_a_restarted_server_is_shown(tmp_path):
    seen = app_session(tmp_path, "pollRestart")
    # a later poll is a later read: its rows are taken at the revision of the rows shown, and below it
    assert seen["sameRevision"] == ["poll1"] and seen["lowerRevision"] == ["poll2"]


def test_the_app_takes_a_poll_answer_above_the_stream_rows_shown_while_it_was_out(tmp_path):
    # the first rows do not abort the poll out: its answer, at a higher revision, is a later read and is shown
    # and becomes the revision shown, so an event under it is refused and one above it is shown
    seen = app_session(tmp_path, "pollInFlight")
    assert seen == {"aborted": False, "actors": ["late"], "refused": ["late"], "shown": ["after"]}


def test_the_app_refuses_a_poll_answer_not_above_the_revision_shown_while_the_stream_is_live(tmp_path):
    # the answer at 900 after rows at 995 is refused, and 995 stays the revision shown: 950 is refused too
    seen = app_session(tmp_path, "pollRefused")
    assert seen == {"actors": ["streamed"], "refused": ["streamed"], "shown": ["after"]}


def test_the_app_takes_a_poll_answer_whatever_its_revision_while_the_stream_is_down(tmp_path):
    seen = app_session(tmp_path, "pollDown")
    # the stream fell again before its first rows, so the answer at 300 is shown and is the revision shown
    assert seen["polled"] == ["polled"] and seen["below"] == ["polled"] and seen["above"] == ["fresh"]
    # and with rows at 350 shown, a poll once the stream is down answers at 200, under it, and is taken
    assert seen["lower"] == ["lower"]


def test_the_app_takes_the_first_rows_after_the_stream_opens_whatever_their_revision(tmp_path):
    seen = app_session(tmp_path, "epoch")
    # a restarted server may count below the revision shown: its rows, once the stream opens, are taken
    assert seen["afterOpen"] == ["restarted"]
    # within that connection the higher revision stays: an older event is dropped and a newer one taken
    assert seen["sameConnection"] == ["restarted"] and seen["newer"] == ["newer"]
    # a poll out when the stream opens again is aborted: its answer, though higher, shows nothing, and
    # the first rows after the open are taken
    assert seen["aborted"] is True and seen["acrossOpen"] == ["newer"]
    assert seen["reopened"] == ["reopened"]
    # and the answer of a poll sent since is taken whatever its revision, the stream being down again
    assert seen["polled"] == ["polled"]


def test_the_app_still_drops_rows_older_than_those_shown_while_no_stream_opens_between(tmp_path):
    assert app_session(tmp_path, "epochSame") == {"actors": ["first"]}


def test_the_app_composer_keeps_words_typed_while_a_send_is_out_through_its_answer_and_the_reload(tmp_path):
    seen = app_session(tmp_path, "sending")
    assert (seen["sent"]["subject"], seen["sent"]["content"]) == ("hello", "first")  # what was typed before
    assert (seen["subject"], seen["content"]) == ("hello", "first more")  # and what was typed after it
    assert seen["toast"] == "sent"


def test_the_app_composer_clears_what_a_send_took_and_a_new_page_starts_empty(tmp_path):
    # a send's answer clears the draft it sent; a control's answer reloads the page and leaves the draft
    # where it is; a navigation, even back to the same resident, is a new page with no draft
    seen = app_session(tmp_path, "clearing")
    assert seen == {"answered": "", "reloaded": "half a thought", "returned": ""}


def test_the_app_composer_keeps_the_words_of_a_refused_send_and_clears_them_once_one_is_accepted(tmp_path):
    seen = app_session(tmp_path, "refusedSend")
    # the refusal is toasted, nothing else is sent, and the composer still holds what was typed
    assert "send failed" in seen["refused"]["toast"] and seen["refused"]["sends"] == 1
    assert (seen["refused"]["subject"], seen["refused"]["content"]) == ("hello", "words")
    # the page's draft holds them too, so a rebuilt page restores them
    assert seen["reloaded"] == {"subject": "hello", "content": "words"}
    # the same words sent again and accepted are cleared
    assert seen["accepted"] == {"subject": "", "content": "", "toast": "sent"}


def test_the_app_drops_a_poll_answer_the_stream_overtook_and_applies_the_next_poll(tmp_path):
    seen = app_session(tmp_path, "pollStale")
    # rows the stream delivered at a newer revision while a slow poll was out are not replaced by its answer
    assert seen["afterStale"] == {"actors": ["streamed"], "conn": "live"}
    # a poll answer newer than the rows shown is applied, and the answer that was dropped freed the latch
    assert seen["afterPoll"] == {"actors": ["polled"], "polls": 2}


def test_the_app_starts_no_poll_while_one_is_out_and_a_failed_poll_frees_the_next(tmp_path):
    # a tick over one slow poll starts no second request; its failure is swallowed and the next tick polls;
    # the poll that was answered is not aborted when its time limit would have passed
    seen = app_session(tmp_path, "pollSerial")
    assert seen == {"out": 1, "after": 2, "actors": ["later"], "aborted": False}


def test_the_app_gives_up_a_poll_that_never_settles_and_the_next_tick_polls_again(tmp_path):
    seen = app_session(tmp_path, "pollHung")
    # nine seconds out, a poll is still waited for; after two intervals it is aborted and a later tick polls
    assert seen["waiting"] == {"polls": 1, "abandoned": False}
    assert seen["givenUp"] == {"polls": 2, "abandoned": True}
    # the given-up poll settling after all replaces no rows and leaves the poll that went out in its place
    # holding the latch, so the tick after it starts none; that poll's own answer is applied
    assert seen["afterLate"] == [] and seen["whileOut"] == 2
    assert seen["actors"] == ["next"]


def test_the_app_shows_a_history_batch_in_the_box_of_the_page_already_open(tmp_path):
    seen = app_session(tmp_path, "logHistory")
    # the batch arrives after the page rendered its box, and shows there without another navigation
    assert seen["shown"] == ["h1", "h2", "h3"]
    # another resident's batch leaves this box alone, and a live line still follows the batch
    assert seen["afterOther"] == ["h1", "h2", "h3", "live"]


def test_the_app_replaces_a_residents_log_with_the_tail_a_reconnect_replays(tmp_path):
    seen = app_session(tmp_path, "logReplay")
    # the replayed tail replaces the buffer: each line stands once, on the page and in a box built again
    assert seen == {"onPage": list("abcde"), "rebuilt": list("abcde")}


def test_the_app_restarts_the_dropped_line_count_of_a_log_a_history_batch_replaces(tmp_path):
    seen = app_session(tmp_path, "logReplayShed")
    # the new buffer dropped five lines as the old one had: the place taken before the batch does not move
    assert seen == {"nodes": 400, "view": "m52", "scrollTop": 50 * 20}


def test_the_app_shows_the_streams_rows_again_above_a_poll_answer_that_showed_others(tmp_path):
    seen = app_session(tmp_path, "pollThenStreamAgain")
    # a poll's answer above the stream's rows is shown; the stream's rows again, above it, are shown too
    assert seen == {"polled": ["b"], "streamed": ["a"]}


def test_the_app_log_box_drops_the_oldest_lines_under_a_reader_at_its_top(tmp_path):
    seen = app_session(tmp_path, "logTop")
    # the box holds 400 and a window at the top shows the oldest line that remains as the oldest go
    assert seen["atTop"] == {"before": "line 5", "after": "line 8", "scrollTop": 0, "nodes": 400}
    # a window two lines down loses the line it was on with the oldest, and rests at the top
    assert seen["nearTop"] == {"before": "line 10", "after": "line 11", "scrollTop": 0, "nodes": 400}
