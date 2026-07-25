# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the schedule / team / casts / plugin / invoke tool surface.

The Studio HTTP transport and the invocation state-DB helpers are stubbed, so
these assert on the request each tool builds and the shape it hands back — the
two things a calling agent reads off the schema.
"""

from __future__ import annotations

import json

import pytest

from lionagi.mcp import surface


@pytest.fixture
def api(monkeypatch):
    """Replace the Studio transport with a recorder returning a canned payload."""
    calls: list[dict] = []
    reply: dict = {"ok": True, "status": 200, "data": {}}

    def fake_request(path, method="GET", body=None):
        calls.append({"path": path, "method": method, "body": body})
        return reply

    monkeypatch.setattr(surface, "_request", fake_request)

    class Recorder:
        def __init__(self):
            self.calls = calls

        def returns(self, data):
            reply["data"] = data

        def fails(self, error="boom", status=500):
            reply.clear()
            reply.update({"ok": False, "status": status, "error": error})

        @property
        def last(self):
            return calls[-1]

    return Recorder()


# --- schedule: request construction -------------------------------------------


def test_create_maps_every_trigger_and_action_field_into_the_api_body(api, tmp_path):
    flow = tmp_path / "spec.yaml"
    flow.write_text("nodes: []\n")

    api.returns({"id": "sched-1", "name": "nightly"})
    result = surface.schedule_create(
        name="nightly",
        trigger_type="github",
        github_repo="o/r",
        github_filter={"state": "open"},
        poll_interval_seconds=120,
        threshold_config={"metric": "failed_sessions", "op": "gt", "value": 5},
        action_kind="playbook",
        prompt="review it",
        model="claude/opus",
        agent="reviewer",
        playbook="pr-review",
        flow_yaml_file=str(flow),
        project="lionagi",
        cwd=str(tmp_path),
        description="nightly review",
        once=True,
        max_cost_usd=2.5,
        max_tokens=1000,
    )

    body = api.last["body"]
    assert api.last["method"] == "POST"
    # 'github' is an alias the API does not know, and 'playbook' is an alias for
    # the stored 'play' kind: both are normalized before the request goes out.
    assert body["trigger_type"] == "github_poll"
    assert body["action_kind"] == "play"
    assert body["github_repo"] == "o/r"
    assert body["github_filter"] == {"state": "open"}
    assert body["poll_interval_sec"] == 120
    assert body["threshold_config"]["metric"] == "failed_sessions"
    assert body["action_prompt"] == "review it"
    assert body["action_model"] == "claude/opus"
    assert body["action_agent"] == "reviewer"
    assert body["action_playbook"] == "pr-review"
    # The spec's contents are stored, not its path: a later edit to the file
    # cannot change what an existing schedule runs.
    assert body["action_flow_yaml"] == "nodes: []\n"
    assert body["action_project"] == "lionagi"
    assert body["action_cwd"] == str(tmp_path.resolve())
    assert body["max_runs"] == 1
    assert body["budget_usd"] == 2.5
    assert body["budget_tokens"] == 1000
    assert result == {
        "ok": True,
        "id": "sched-1",
        "name": "nightly",
        "schedule": {"id": "sched-1", "name": "nightly"},
        "warnings": [],
    }


def test_create_records_an_execution_root_even_with_no_project_or_cwd(api, monkeypatch, tmp_path):
    monkeypatch.setattr(surface, "_detect_project", lambda root: None)
    monkeypatch.chdir(tmp_path)
    api.returns({"id": "s", "name": "n"})

    surface.schedule_create(name="n", cron="0 9 * * *")

    # A schedule with neither root would fall through to the daemon's own
    # working directory when it fires.
    assert api.last["body"]["action_cwd"] == str(tmp_path)
    assert api.last["body"]["cron_expr"] == "0 9 * * *"


def test_create_rejects_contradictory_run_caps_before_any_request(api):
    result = surface.schedule_create(name="n", cron="* * * * *", once=True, max_runs=3)

    assert result["ok"] is False
    assert "mutually exclusive" in result["error"]
    assert api.calls == []


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"max_cost_usd": 0}, "max_cost_usd"),
        ({"max_tokens": -1}, "max_tokens"),
        ({"max_runs": 0}, "max_runs"),
        ({"poll_interval_seconds": 0}, "poll_interval_seconds"),
        ({"flow_yaml_file": "/nope/missing.yaml"}, "flow_yaml_file"),
        ({"cwd": "/nope/missing-dir"}, "cwd"),
    ],
)
def test_create_refuses_bad_values_without_reaching_the_api(api, kwargs, fragment):
    result = surface.schedule_create(name="n", cron="* * * * *", **kwargs)

    assert result["ok"] is False
    assert fragment in result["error"]
    assert api.calls == []


def test_create_rejects_a_chain_action_with_unknown_keys(api):
    result = surface.schedule_create(
        name="n", cron="* * * * *", on_success={"nope": 1, "on_success": None}
    )

    assert result["ok"] is False
    assert "unknown key" in result["error"]
    assert api.calls == []


def test_create_warns_when_a_chain_does_not_terminate_itself(api):
    api.returns({"id": "s", "name": "n"})

    result = surface.schedule_create(
        name="n", cron="* * * * *", on_fail={"prompt": "alert on-call"}
    )

    assert result["ok"] is True
    assert api.last["body"]["on_fail"] == {"prompt": "alert on-call"}
    # Omitting on_fail inside the chain child inherits the parent's, so the
    # chain re-fires at the next depth. The caller is told, not left to find out.
    assert any("on_fail" in w for w in result["warnings"])


def test_a_terminated_chain_produces_no_warning(api):
    api.returns({"id": "s", "name": "n"})

    result = surface.schedule_create(
        name="n", cron="* * * * *", on_success={"prompt": "notify", "on_success": None}
    )

    assert result["warnings"] == []


# --- schedule: query + control -------------------------------------------------


def test_list_returns_the_schedule_rows(api):
    api.returns({"schedules": [{"id": "s1", "name": "a", "enabled": True}]})

    result = surface.schedule_list()

    assert api.last["path"] == "/"
    assert result == {"ok": True, "schedules": [{"id": "s1", "name": "a", "enabled": True}]}


def test_get_returns_the_whole_row(api):
    api.returns({"id": "s1", "cron_expr": "0 9 * * *"})

    assert surface.schedule_get("s1") == {
        "ok": True,
        "schedule": {"id": "s1", "cron_expr": "0 9 * * *"},
    }
    assert api.last == {"path": "/s1", "method": "GET", "body": None}


def test_limits_reports_an_uncapped_scheduler_as_none(api):
    api.returns({"max_scheduled_concurrent": 0, "current_inflight": 2})

    assert surface.schedule_limits() == {"ok": True, "max_concurrent": None, "in_flight": 2}


@pytest.mark.parametrize(("enabled", "verb"), [(True, "enable"), (False, "disable")])
def test_set_enabled_posts_to_the_matching_endpoint(api, enabled, verb):
    result = surface.schedule_set_enabled("s1", enabled)

    assert api.last == {"path": f"/s1/{verb}", "method": "POST", "body": None}
    assert result == {"ok": True, "id": "s1", "enabled": enabled}


def test_delete_uses_the_delete_method(api):
    result = surface.schedule_delete("s1")

    assert api.last == {"path": "/s1", "method": "DELETE", "body": None}
    assert result == {"ok": True, "id": "s1", "deleted": True}


def test_trigger_returns_the_run_id_without_waiting(api):
    api.returns({"run_id": "r1"})

    result = surface.schedule_trigger("s1")

    assert api.last["path"] == "/s1/trigger"
    assert result == {"ok": True, "id": "s1", "run_id": "r1"}


def test_trigger_with_wait_polls_until_the_run_is_terminal(monkeypatch):
    replies = [
        {"ok": True, "status": 200, "data": {"run_id": "r1"}},
        {"ok": True, "status": 200, "data": {"id": "r1", "status": "running"}},
        {
            "ok": True,
            "status": 200,
            "data": {"id": "r1", "status": "completed", "outcome": {"code": "ok"}},
        },
    ]
    paths: list[str] = []

    def fake_request(path, method="GET", body=None):
        paths.append(path)
        return replies.pop(0)

    monkeypatch.setattr(surface, "_request", fake_request)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    result = surface.schedule_trigger("s1", wait=True)

    assert paths == ["/s1/trigger", "/runs/r1", "/runs/r1"]
    assert result["status"] == "completed"
    assert result["outcome"] == {"code": "ok"}


def test_runs_appends_every_status_filter_to_the_query(api):
    api.returns({"runs": [{"id": "r1", "status": "failed"}]})

    result = surface.schedule_runs("s1", limit=5, status=["failed", "timed_out"])

    assert api.last["path"] == "/s1/runs?limit=5&status=failed&status=timed_out"
    assert result == {"ok": True, "runs": [{"id": "r1", "status": "failed"}]}


def test_runs_refuses_a_limit_the_api_would_reject(api):
    result = surface.schedule_runs("s1", limit=500)

    assert result["ok"] is False
    assert api.calls == []


def test_run_looks_up_an_occurrence_not_a_schedule(api):
    api.returns({"id": "r1", "status": "completed"})

    assert surface.schedule_run("r1")["run"]["status"] == "completed"
    assert api.last["path"] == "/runs/r1"


def test_status_splits_the_schedule_from_its_latest_run(api):
    api.returns(
        {
            "schedule": {"id": "s1", "enabled": True},
            "latest_run": {"id": "r1", "status": "failed"},
            "exit_code": 1,
        }
    )

    result = surface.schedule_status("s1")

    assert api.last["path"] == "/s1/status"
    assert result["schedule"] == {"id": "s1", "enabled": True}
    assert result["latest_run"]["status"] == "failed"
    assert result["exit_code"] == 1


def test_status_defaults_the_exit_code_when_the_api_omits_it(api):
    api.returns({"schedule": {"id": "s1"}})

    assert surface.schedule_status("s1")["exit_code"] == 2


def test_a_transport_failure_is_returned_verbatim_not_raised(api):
    api.fails(error="Studio is down", status=None)

    result = surface.schedule_list()

    assert result["ok"] is False
    assert result["error"] == "Studio is down"


def test_unreachable_studio_is_reported_as_data(monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)

    result = surface.schedule_list()

    assert result["ok"] is False
    assert "cannot reach Studio" in result["error"]
    assert "li studio start" in result["error"]


# --- schedule: typed create ----------------------------------------------------


@pytest.fixture
def quick_create(monkeypatch):
    """Capture the compiled ScheduleMember instead of writing to the database."""
    from lionagi.studio.services import schedule_declaration as sd

    seen: dict = {}

    async def fake_create(db, name, member, *, cwd, project):
        seen.update({"name": name, "member": member, "cwd": cwd, "project": project})

        class Resolved:
            qualified_name = f"{project}/{name}" if project else name

        return "sched-typed", Resolved()

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(sd, "create_quick_schedule", fake_create)
    monkeypatch.setattr("lionagi.state.db.StateDB", FakeDB)
    monkeypatch.setattr(surface, "_detect_project", lambda root: "lionagi")
    return seen


def test_typed_create_pins_the_cron_expression_to_a_named_timezone(quick_create, tmp_path):
    result = surface.schedule_create_typed(
        name="digest",
        kind="agent",
        profile="researcher",
        prompt="summarize",
        model="claude/opus",
        cron="0 2 * * *",
        timezone="America/New_York",
        cwd=str(tmp_path),
        overlap="allow",
        missed_fire="run_once",
        max_runs=5,
        budget_usd=1.0,
        rate_limit={"max_fires": 3, "window_sec": 3600},
        description="nightly digest",
    )

    member = quick_create["member"]
    assert member.trigger.cron.expression == "0 2 * * *"
    assert member.trigger.cron.timezone == "America/New_York"
    assert member.target.profile == "researcher"
    assert member.target.prompt == "summarize"
    assert member.target.model == "claude/opus"
    assert member.execution.cwd == str(tmp_path.resolve())
    assert member.policies.overlap == "allow"
    assert member.policies.missedFire == "run_once"
    assert member.policies.maxRuns == 5
    assert member.policies.budget.usd == 1.0
    assert member.policies.rateLimit == {"max_fires": 3, "window_sec": 3600}
    assert result == {"ok": True, "id": "sched-typed", "qualified_name": "lionagi/digest"}


def test_typed_create_rejects_cron_without_a_timezone(quick_create):
    result = surface.schedule_create_typed(
        name="d", kind="agent", profile="p", prompt="x", cron="0 2 * * *"
    )

    assert result["ok"] is False
    assert "timezone" in result["error"]
    assert quick_create == {}


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"at": "2026-07-15T09:00:00-04:00", "every": "15m"}],
    ids=["none", "two"],
)
def test_typed_create_requires_exactly_one_trigger(quick_create, kwargs):
    result = surface.schedule_create_typed(
        name="d", kind="agent", profile="p", prompt="x", **kwargs
    )

    assert result["ok"] is False
    assert "exactly one" in result["error"]
    assert quick_create == {}


def test_typed_create_requires_the_target_fields_of_its_kind(quick_create):
    result = surface.schedule_create_typed(name="d", kind="flow", every="15m")

    assert result["ok"] is False
    assert "flow_file" in result["error"]
    assert quick_create == {}


def test_typed_create_builds_a_command_target_from_argv(quick_create):
    surface.schedule_create_typed(
        name="refresh",
        kind="command",
        executable="refresh-index",
        executable_args=["--incremental"],
        every="15m",
    )

    target = quick_create["member"].target
    assert target.kind == "command"
    assert target.executable == "refresh-index"
    assert target.args == ["--incremental"]
    assert quick_create["member"].trigger.every == "15m"


def test_typed_create_surfaces_a_name_collision_as_an_error(monkeypatch, quick_create):
    from lionagi.studio.services import schedule_declaration as sd

    async def collide(db, name, member, *, cwd, project):
        raise sd.ScheduleSetError([("lionagi/d", "a schedule named 'lionagi/d' already exists")])

    monkeypatch.setattr(sd, "create_quick_schedule", collide)

    result = surface.schedule_create_typed(
        name="d", kind="agent", profile="p", prompt="x", every="15m"
    )

    assert result["ok"] is False
    assert "already exists" in result["error"]


# --- team ----------------------------------------------------------------------


@pytest.fixture
def teams(monkeypatch, tmp_path):
    from lionagi.cli import team as team_mod

    monkeypatch.setattr(team_mod, "TEAMS_DIR", tmp_path / "teams")
    return team_mod


def test_team_create_writes_a_readable_team_file(teams):
    result = surface.team_create("build", ["alice", " bob ", ""])

    assert result["ok"] is True
    assert result["members"] == ["alice", "bob"]
    stored = json.loads((teams.TEAMS_DIR / f"{result['id']}.json").read_text())
    assert stored["name"] == "build"
    assert stored["members"] == ["alice", "bob"]
    assert result["file"].endswith(f"{result['id']}.json")


def test_team_create_refuses_an_empty_member_list(teams):
    result = surface.team_create("build", ["  "])

    assert result["ok"] is False
    assert list(teams.TEAMS_DIR.glob("*.json")) == [] if teams.TEAMS_DIR.exists() else True


def test_team_list_reports_members_and_message_counts(teams):
    created = surface.team_create("build", ["alice", "bob"])
    surface.team_send(created["id"], to="alice", content="hi", sender="bob")

    listed = surface.team_list()["teams"]

    assert listed == [
        {
            "id": created["id"],
            "name": "build",
            "members": ["alice", "bob"],
            "message_count": 1,
        }
    ]


def test_team_send_broadcasts_and_flags_unknown_names(teams):
    created = surface.team_create("build", ["alice"])

    result = surface.team_send(
        created["id"],
        to="all",
        content="starting",
        sender="mallory",
        kind="done",
        from_op="o3",
        artifacts=["report.md"],
    )

    assert result["recipients"] == ["*"]
    assert result["kind"] == "done"
    assert any("mallory" in w for w in result["warnings"])
    msg = surface.team_show(created["id"])["messages"][0]
    assert msg["kind"] == "done"
    assert msg["from_op"] == "o3"
    assert msg["artifacts"] == ["report.md"]
    assert msg["id"] == result["message_id"]


def test_team_send_resolves_a_team_by_name(teams):
    surface.team_create("build", ["alice"])

    result = surface.team_send("build", to="alice", content="hi")

    assert result["ok"] is True
    assert result["warnings"] == []


def test_team_send_reports_a_missing_team_as_an_error(teams):
    result = surface.team_send("nope", to="alice", content="hi")

    assert result["ok"] is False
    assert "nope" in result["error"]


def test_team_receive_consumes_only_this_members_unread_mail(teams):
    created = surface.team_create("build", ["alice", "bob"])
    surface.team_send(created["id"], to="alice", content="for alice", sender="bob")
    surface.team_send(created["id"], to="all", content="for everyone", sender="bob")

    first = surface.team_receive(created["id"], member="alice")
    second = surface.team_receive(created["id"], member="alice")
    other = surface.team_receive(created["id"], member="bob")

    assert [m["content"] for m in first["messages"]] == ["for alice", "for everyone"]
    assert first["count"] == 2
    # Read state is per member: a second read is empty for alice, but bob still
    # has the broadcast waiting.
    assert second["messages"] == []
    assert [m["content"] for m in other["messages"]] == ["for everyone"]


def test_team_show_does_not_consume_messages(teams):
    created = surface.team_create("build", ["alice"])
    surface.team_send(created["id"], to="alice", content="hi")

    shown = surface.team_show(created["id"])

    assert shown["team"]["name"] == "build"
    assert len(shown["messages"]) == 1
    assert surface.team_receive(created["id"], member="alice")["count"] == 1


# --- casts ---------------------------------------------------------------------


def test_casts_list_returns_name_description_pairs():
    result = surface.casts_list()

    assert result["ok"] is True
    assert result["kind"] == "roles"
    assert result["entries"]
    assert set(result["entries"][0]) == {"name", "description"}


def test_casts_list_can_return_modes_instead():
    modes = surface.casts_list(kind="modes")["entries"]
    roles = surface.casts_list(kind="roles")["entries"]

    assert modes
    assert {m["name"] for m in modes} != {r["name"] for r in roles}


def test_casts_get_finds_a_role_without_being_told_it_is_one():
    from lionagi.casts.catalog import build_catalog

    name = build_catalog()["roles"][0]["name"]

    result = surface.casts_get(name)

    assert result["kind"] == "role"
    assert result["entry"]["name"] == name
    assert "body" in result["entry"]


def test_casts_get_finds_a_mode_through_the_same_lookup():
    from lionagi.casts.catalog import build_catalog

    name = build_catalog()["modes"][0]["name"]

    result = surface.casts_get(name)

    assert result["kind"] == "mode"
    assert "behaviors" in result["entry"]


def test_casts_get_reports_an_unknown_name():
    result = surface.casts_get("not-a-cast")

    assert result["ok"] is False
    assert "not-a-cast" in result["error"]


# --- plugin --------------------------------------------------------------------


class _FakeState:
    def __init__(self, value):
        self.value = value


class _FakeRecord:
    def __init__(self, name, state="active", manifest=None):
        self.name = name
        self.version = "1.0"
        self.state = _FakeState(state)
        self.bundle_dir = f"/plugins/{name}"
        self.error = None
        self.manifest = manifest


@pytest.fixture
def registry(monkeypatch):
    from lionagi import plugins as plugins_mod

    records = [_FakeRecord("zeta"), _FakeRecord("alpha", state="untrusted")]

    class FakeRegistry:
        reset_calls = 0

        @classmethod
        def reset(cls):
            cls.reset_calls += 1

        @staticmethod
        def list_plugins():
            return records

        @staticmethod
        def get(name):
            return next((r for r in records if r.name == name), None)

    monkeypatch.setattr(plugins_mod, "PluginRegistry", FakeRegistry)
    monkeypatch.setattr("lionagi.plugins.discovery.discover_plugins", lambda: records)
    monkeypatch.setattr("lionagi.plugins.trust.gc_trust_records", lambda d: ["ghost"])
    return FakeRegistry


def test_plugin_list_sorts_by_name_and_reports_pruned_trust_records(registry):
    result = surface.plugin_list()

    assert [p["name"] for p in result["plugins"]] == ["alpha", "zeta"]
    assert result["plugins"][0]["state"] == "untrusted"
    assert result["pruned"] == ["ghost"]


def test_plugin_info_omits_the_disclosure_for_a_manifestless_bundle(registry):
    result = surface.plugin_info("zeta")

    assert result["plugin"]["state"] == "active"
    assert result["plugin"]["bundle_dir"] == "/plugins/zeta"
    assert result["disclosure"] is None


def test_plugin_info_returns_the_full_disclosure_when_there_is_a_manifest(registry, monkeypatch):
    registry.get("zeta").manifest = object()
    monkeypatch.setattr(
        "lionagi.plugins.trust.build_trust_disclosure",
        lambda record: {"name": record.name, "tools": [{"name": "t", "target": "m:f"}]},
    )

    result = surface.plugin_info("zeta")

    assert result["disclosure"]["tools"] == [{"name": "t", "target": "m:f"}]


def test_plugin_info_reports_an_unknown_name(registry):
    assert surface.plugin_info("nope")["ok"] is False


def test_plugin_trust_records_trust_and_echoes_what_was_approved(registry, monkeypatch):
    record = registry.get("alpha")
    record.manifest = type("M", (), {"name": "alpha"})()
    trusted: list = []
    monkeypatch.setattr("lionagi.plugins.trust.trust_plugin", trusted.append)
    monkeypatch.setattr(
        "lionagi.plugins.trust.build_trust_disclosure",
        lambda r: {"name": r.name, "tools": []},
    )

    result = surface.plugin_trust("alpha")

    assert trusted == [record]
    assert result == {
        "ok": True,
        "name": "alpha",
        "trusted": True,
        "disclosure": {"name": "alpha", "tools": []},
    }


def test_plugin_trust_refuses_a_plugin_with_no_valid_manifest(registry):
    result = surface.plugin_trust("zeta")

    assert result["ok"] is False
    assert "unknown or invalid" in result["error"]


@pytest.mark.parametrize("enabled", [True, False])
def test_plugin_set_enabled_writes_the_settings_flag(registry, monkeypatch, enabled):
    import contextlib

    settings: dict = {}

    @contextlib.contextmanager
    def fake_locked():
        yield settings

    monkeypatch.setattr("lionagi.plugins._user_settings.locked_user_settings", fake_locked)

    result = surface.plugin_set_enabled("zeta", enabled)

    assert settings == {"plugins": {"zeta": {"enabled": enabled}}}
    assert result == {"ok": True, "name": "zeta", "enabled": enabled}


def test_plugin_set_enabled_rejects_an_unknown_plugin(registry):
    assert surface.plugin_set_enabled("nope", True)["ok"] is False


# --- invoke --------------------------------------------------------------------


@pytest.fixture
def invocations(monkeypatch):
    from lionagi.cli import invoke as invoke_mod

    seen: dict = {}

    async def fake_start(*, skill, plugin, prompt, metadata):
        seen["start"] = {"skill": skill, "plugin": plugin, "prompt": prompt, "metadata": metadata}
        return "inv-1"

    async def fake_end(invocation_id, *, status, metadata):
        seen["end"] = {"id": invocation_id, "status": status, "metadata": metadata}
        if invocation_id != "inv-1":
            return None
        return {"id": invocation_id, "status": status, "session_count": 3}

    async def fake_list(*, skill, status, limit):
        seen["list"] = {"skill": skill, "status": status, "limit": limit}
        return [{"id": "inv-1", "skill": "show", "status": "completed", "session_count": 3}]

    monkeypatch.setattr(invoke_mod, "_start_invocation", fake_start)
    monkeypatch.setattr(invoke_mod, "_end_invocation", fake_end)
    monkeypatch.setattr(invoke_mod, "_list_invocations", fake_list)
    return seen


def test_invoke_start_passes_every_field_and_returns_the_id(invocations):
    result = surface.invoke_start(
        skill="show", plugin="lion", prompt="land the ADR", metadata={"plan": ["a"]}
    )

    assert invocations["start"] == {
        "skill": "show",
        "plugin": "lion",
        "prompt": "land the ADR",
        "metadata": {"plan": ["a"]},
    }
    assert result == {"ok": True, "invocation_id": "inv-1"}


def test_invoke_end_returns_the_closed_record(invocations):
    result = surface.invoke_end("inv-1", status="failed", metadata={"rounds": 2})

    assert invocations["end"] == {"id": "inv-1", "status": "failed", "metadata": {"rounds": 2}}
    assert result["invocation"]["session_count"] == 3


def test_invoke_end_reports_a_missing_invocation(invocations):
    result = surface.invoke_end("nope")

    assert result["ok"] is False
    assert "nope" in result["error"]


def test_invoke_list_forwards_its_filters(invocations):
    result = surface.invoke_list(skill="show", status="completed", limit=5)

    assert invocations["list"] == {"skill": "show", "status": "completed", "limit": 5}
    assert result["invocations"][0]["id"] == "inv-1"


# --- registration --------------------------------------------------------------


def test_every_tool_is_registered_on_the_server_and_documented():
    pytest.importorskip("fastmcp", reason="requires the 'mcp' extra")
    import asyncio

    from lionagi.mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    registered = {t.name: t for t in tools}

    for tool in surface.TOOLS:
        assert tool.__name__ in registered
        schema = registered[tool.__name__]
        # The description is what the calling agent reads to decide whether to
        # reach for the tool, so an empty or stub docstring is a defect.
        assert len(schema.description or "") > 60
        # Every parameter carries its own description in the schema: a flag with
        # no explanation is a flag the caller cannot use correctly.
        undocumented = [
            name
            for name, prop in schema.parameters.get("properties", {}).items()
            if not prop.get("description")
        ]
        assert not undocumented, f"{tool.__name__}: {undocumented}"
    # The existing submit surface is untouched by the registration.
    assert {"submit_agent", "submit_flow", "submit_fanout"} <= set(registered)
