# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The schedule verbs, end to end, through the real `li` subprocess.

Nothing here mocks the child. The defect these verbs are exposed to is not in
either half but in the seam: the dispatcher renders argv from a schema projected
off one parser, and the child then parses those tokens with the parser it builds
for itself. A test that stubs the subprocess asserts that the first half agrees
with itself.

What is substituted is the Studio the child talks to — a real HTTP server on a
loopback port, recording every request. That keeps the assertions about the
request the child actually composed, and keeps the suite from writing to
whatever schedule store the machine running it happens to have.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lionagi.mcp import dispatch

SCHEDULE_ROW = {
    "id": "sched-1",
    "name": "nightly",
    "enabled": 1,
    "trigger_type": "cron",
    "cron_expr": "0 9 * * *",
    "action_kind": "agent",
    "action_cwd": "/tmp/somewhere",
    "action_project": "lionagi",
    "next_fire_at": None,
}


class _Studio(BaseHTTPRequestHandler):
    """Just enough of the schedules API to answer one call of each shape."""

    recorded: list[dict] = []

    def _answer(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.recorded.append(
            {"method": method, "path": self.path, "body": json.loads(raw) if raw else None}
        )
        path = self.path
        if path == "/api/schedules/":
            if method == "POST":
                return self._answer(201, {"id": "sched-1", "name": "nightly", "created_at": 1.5})
            return self._answer(200, {"schedules": [SCHEDULE_ROW]})
        if path == "/api/schedules/limits":
            return self._answer(
                200,
                {
                    "max_scheduled_concurrent": 0,
                    "current_inflight": 2,
                    "max_adhoc_concurrent": 4,
                    "current_adhoc_inflight": 1,
                },
            )
        if path.startswith("/api/schedules/missing"):
            return self._answer(404, {"detail": "Schedule 'missing' not found"})
        if path.endswith("/status"):
            return self._answer(200, {"schedule": SCHEDULE_ROW, "latest_run": None, "exit_code": 2})
        if path.endswith("/trigger"):
            return self._answer(200, {"ok": True, "run_id": "run-9"})
        if path.endswith("/enable"):
            return self._answer(200, {"ok": True, "enabled": True})
        if path.endswith("/disable"):
            return self._answer(200, {"ok": True, "enabled": False})
        if "/runs" in path:
            return self._answer(
                200, {"runs": [{"id": "run-9"}], "limit": 5, "offset": 0, "has_next": False}
            )
        if method == "DELETE":
            return self._answer(200, {"ok": True})
        return self._answer(200, SCHEDULE_ROW)

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's own spelling
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_DELETE(self):  # noqa: N802
        self._handle("DELETE")

    def log_message(self, *args):  # keep the suite's output clean
        pass


@pytest.fixture
def studio(monkeypatch):
    _Studio.recorded = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Studio)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # The child process reads this, so it has to be on the environment the
    # dispatcher hands down rather than patched into any object.
    monkeypatch.setenv("LIONAGI_STUDIO_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield _Studio
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def call(op: str, args: dict | None = None) -> dict:
    answer = asyncio.run(dispatch.request(ops=[{"op": op, "args": args or {}}]))
    return answer["ops"][0]


def result_of(op: str, args: dict | None = None) -> dict:
    entry = call(op, args)
    assert entry["ok"] is True, entry
    return entry["result"]["data"]


# ── reads ────────────────────────────────────────────────────────────────────


def test_list_returns_the_rows_and_a_count(studio):
    data = result_of("schedule.list")
    assert data["count"] == 1
    assert data["schedules"][0]["id"] == "sched-1"


def test_get_names_the_schedule_in_the_path(studio):
    data = result_of("schedule.get", {"id": "sched-1"})
    assert data["schedule"]["id"] == "sched-1"
    assert studio.recorded[-1]["path"] == "/api/schedules/sched-1"


def test_a_missing_schedule_is_not_found_rather_than_empty(studio):
    entry = call("schedule.get", {"id": "missing"})
    assert entry["ok"] is False
    assert entry["error"]["kind"] == "not_found"


def test_status_reports_the_view_verbatim(studio):
    data = result_of("schedule.status", {"id": "sched-1"})
    assert data["status"]["exit_code"] == 2
    assert data["status"]["latest_run"] is None


def test_runs_composes_limit_and_repeated_status_into_the_query(studio):
    data = result_of(
        "schedule.runs", {"id": "sched-1", "limit": 5, "status": ["failed", "timed_out"]}
    )
    assert data["runs"] == [{"id": "run-9"}]
    assert data["has_next"] is False
    path = studio.recorded[-1]["path"]
    assert path == "/api/schedules/sched-1/runs?limit=5&status=failed&status=timed_out"


def test_a_limit_outside_the_route_bound_is_refused_before_the_call(studio):
    entry = call("schedule.runs", {"id": "sched-1", "limit": 500})
    assert entry["ok"] is False
    assert entry["error"]["kind"] == "invalid_input"
    assert studio.recorded == []


def test_limits_says_unlimited_rather_than_leaving_a_falsy_cap_to_be_read(studio):
    data = result_of("schedule.limits")
    assert data["max_scheduled_concurrent"] == 0
    assert data["unlimited"] is True
    assert data["current_inflight"] == 2
    # The ad-hoc lane's own cap is additive to the scheduled cap above, and
    # must be surfaced independently rather than folded into it.
    assert data["max_adhoc_concurrent"] == 4
    assert data["adhoc_unlimited"] is False
    assert data["current_adhoc_inflight"] == 1


def test_a_studio_that_is_not_running_is_unavailable_not_an_empty_list(monkeypatch):
    # Port 1 on loopback refuses immediately; nothing is listening there.
    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:1")
    entry = call("schedule.list")
    assert entry["ok"] is False
    assert entry["error"]["kind"] == "unavailable"
    assert "127.0.0.1:1" in entry["error"]["message"]


# ── validate: a local file, no Studio and no database ────────────────────────

_VALID_SET = """
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: demo
  project: lionagi
schedules:
  nightly:
    trigger:
      cron:
        expression: "0 9 * * *"
        timezone: America/New_York
    target:
      kind: agent
      profile: reviewer
      prompt: "summarize"
    execution:
      cwd: {cwd}
"""


@pytest.fixture
def declared(tmp_path, monkeypatch):
    """A directory holding the ScheduleSet and the profile it names.

    Resolving an agent target loads the named profile, and profiles are looked
    up from the working directory outward before falling back to the home
    directory. Left to the ambient machine the document resolves wherever a
    `reviewer` profile happens to be installed and fails everywhere else, which
    is how a set naming a real fleet profile passed here and failed on a clean
    checkout. The child is a subprocess, so the isolation has to be the working
    directory it inherits rather than anything patched into this process.
    """
    agents = tmp_path / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text("---\nmodel: anthropic/claude-sonnet-5\n---\nBody.\n")
    monkeypatch.chdir(tmp_path)

    def _write(body: str) -> str:
        path = tmp_path / "schedules.yaml"
        path.write_text(body)
        return str(path)

    return tmp_path, _write


def test_validate_resolves_a_good_document(declared):
    tmp_path, write = declared
    data = result_of("schedule.validate", {"file": write(_VALID_SET.format(cwd=tmp_path))})
    assert data["valid"] is True
    assert data["errors"] == []
    assert "nightly" in data["schedules"]


def test_validate_reports_a_bad_document_as_an_answer_not_a_refusal(declared):
    tmp_path, write = declared
    path = write(
        _VALID_SET.format(cwd=tmp_path).replace('expression: "0 9 * * *"', 'expression: "nope"')
    )
    data = result_of("schedule.validate", {"file": path})
    assert data["valid"] is False
    # Named, so that the profile going missing cannot make this pass for a
    # reason that has nothing to do with the cron expression under test.
    assert [e["name"] for e in data["errors"]] == ["nightly"]
    assert "nope" in data["errors"][0]["message"]


def test_validate_refuses_a_relative_path(tmp_path):
    entry = call("schedule.validate", {"file": "schedules.yaml"})
    assert entry["ok"] is False
    assert entry["error"]["kind"] == "invalid_input"
    assert "absolute" in entry["error"]["message"]


# ── mutations ────────────────────────────────────────────────────────────────


def test_create_reports_the_row_written_and_when_it_next_fires(studio, tmp_path):
    data = result_of(
        "schedule.create",
        {"name": "nightly", "cron": "0 9 * * *", "prompt": "summarize", "cwd": str(tmp_path)},
    )
    assert data["id"] == "sched-1"
    posted = studio.recorded[0]
    assert posted["method"] == "POST"
    assert posted["body"]["cron_expr"] == "0 9 * * *"
    assert posted["body"]["action_cwd"] == str(tmp_path)

    # The row is read back, so what the caller sees is what landed rather than
    # what it sent — the execution root in particular, which this command
    # resolves from its own environment when the caller names none.
    assert data["schedule"]["available"] is True
    assert data["schedule"]["value"]["action_cwd"] == "/tmp/somewhere"

    # The point of the field: a cron string is echoed back unchanged whether or
    # not it means what the caller thought, so the resolved instant is reported.
    fire = data["resolved_next_fire"]
    assert fire["available"] is True, fire
    assert fire["value"]["rfc3339"].split("T")[1].startswith("09:00")
    assert fire["value"]["timezone"]


def test_create_refuses_a_relative_execution_root(studio):
    entry = call("schedule.create", {"name": "n", "cron": "0 9 * * *", "cwd": "relative/dir"})
    assert entry["ok"] is False
    assert entry["error"]["kind"] == "invalid_input"
    assert studio.recorded == []


def test_create_carries_the_cli_rule_that_once_and_max_runs_conflict(studio):
    entry = call(
        "schedule.create",
        {"name": "n", "cron": "0 9 * * *", "once": True, "max_runs": 3, "cwd": "/tmp"},
    )
    assert entry["ok"] is False
    assert "mutually exclusive" in entry["error"]["message"]
    assert studio.recorded == []


def test_trigger_reports_an_accepted_fire_and_claims_nothing_about_the_run(studio):
    data = result_of("schedule.trigger", {"id": "sched-1"})
    assert data == {"schedule_id": "sched-1", "run_id": "run-9", "fire_accepted": True}
    assert studio.recorded[-1]["method"] == "POST"
    # Nothing in the payload can be read as a status, because at this moment the
    # occurrence row may not even be written yet.
    assert "status" not in data


def test_enable_and_disable_report_the_state_that_was_committed(studio):
    assert result_of("schedule.enable", {"id": "sched-1"}) == {
        "schedule_id": "sched-1",
        "enabled": True,
    }
    assert result_of("schedule.disable", {"id": "sched-1"}) == {
        "schedule_id": "sched-1",
        "enabled": False,
    }


def test_delete_reports_the_deletion_the_store_confirmed(studio):
    assert result_of("schedule.delete", {"id": "sched-1"}) == {
        "schedule_id": "sched-1",
        "deleted": True,
    }
    assert studio.recorded[-1]["method"] == "DELETE"


def test_deleting_a_schedule_that_is_gone_does_not_report_success(studio):
    entry = call("schedule.delete", {"id": "missing"})
    assert entry["ok"] is False
    assert entry["error"]["kind"] == "not_found"


def test_export_returns_its_documents_inline(monkeypatch, tmp_path):
    # An empty store of its own: this verb reads the lifecycle database, and a
    # test must not report on whichever one the machine running it has.
    monkeypatch.setenv("LIONAGI_HOME", str(tmp_path))
    data = result_of("schedule.export")
    # An empty store still converts: one document carrying no schedules, and an
    # empty report. "Nothing to export" is an answer, not a refusal.
    assert [d["yaml"].count("schedules: {}") for d in data["documents"]] == [1]
    assert data["report"] == []
    assert data["blocked_count"] == 0
    assert data["ready_count"] == 0


# ── the closed surface ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("schedule.status", {"id": "x", "wait": True}),
        ("schedule.status", {"id": "x", "as_json": True}),
        ("schedule.trigger", {"id": "x", "wait": True}),
        ("schedule.runs", {"id": "x", "as_json": True}),
        ("schedule.export", {"output": "/tmp/out.yaml"}),
        ("schedule.export", {"report": "/tmp/report.txt"}),
    ],
    ids=[
        "status-wait",
        "status-json",
        "trigger-wait",
        "runs-json",
        "export-output",
        "export-report",
    ],
)
def test_a_flag_the_machine_path_does_not_honour_is_not_admitted(op, args):
    entry = call(op, args)
    assert entry["ok"] is False
    assert entry["error"]["kind"] == "invalid_input"
    assert "unknown parameter" in entry["error"]["message"]


@pytest.mark.parametrize("name", ["schedule.apply", "schedule.run"])
def test_the_two_schedule_verbs_left_absent_say_why(name):
    entry = call(name)
    assert entry["ok"] is False
    assert entry["error"]["kind"] == "unavailable"
    assert entry["error"]["message"]


def test_every_admitted_parameter_is_one_the_child_parser_accepts():
    """The seam, checked directly: what is advertised has to parse downstream.

    A parameter admitted on this surface but unknown to the parser the child
    builds would be rendered into argv and then rejected there, which is a
    rejection a caller cannot anticipate from the schema it was given.
    """
    from lionagi.cli.machine_schedule import _schedule_subparsers
    from lionagi.mcp import verbs

    parsers = _schedule_subparsers()
    for verb in verbs.VERBS.values():
        if not (verb.cli_path or "").startswith("schedule "):
            continue
        dests = {a.dest for a in parsers[verb.cli_path.split()[1]]._actions}
        assert set(verb.admits or ()) <= dests, verb.name
