# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Leg-host mode: what it accepts, what it refuses, and what it never hands a leg."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from lionhub.orchestration.scheduler.subprocess import spawn_and_wait
from lionhub.serve.daemon import leg_host

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "example-bearer-value"  # noqa: S105 — a test value, never a real credential
HEAD = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A leg host's home: one repository configured, a claude profile and a codex profile."""
    home = tmp_path / "home"
    agents = home / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text("---\nmodel: claude_code/sonnet\n---\n\nReview.\n")
    (agents / "coder.md").write_text("---\nmodel: codex/gpt-5.3-codex-spark\n---\n\nCode.\n")
    (agents / "bare.md").write_text("---\neffort: high\n---\n\nNo model.\n")
    (home / ".lionagi" / "leg_repos.yaml").write_text(
        f"lionagi:\n  clone: {tmp_path / 'clone'}\n  worktrees: {tmp_path / 'no-such-root'}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(leg_host.LEG_HOST_ENV, "1")
    # The suite turns the child sandbox off for every test; a leg host reads its own settings.
    import yaml

    from lionhub.agent import child_sandbox

    settings = home / ".lionagi" / "settings.yaml"
    monkeypatch.setattr(
        child_sandbox,
        "_global_settings",
        lambda: (yaml.safe_load(settings.read_text()) or {}) if settings.exists() else {},
    )
    return home


def _request(**overrides: object) -> dict:
    data = {
        "action_kind": "agent",
        "action_agent": "reviewer",
        "action_prompt": "review the diff",
        "action_repo": "lionagi",
        "action_head": HEAD,
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def test_a_well_formed_leg_launch_is_accepted(host: Path) -> None:
    launch = leg_host.validate_launch(_request())
    assert (launch.repo.name, launch.head, launch.agent) == ("lionagi", HEAD, "reviewer")


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        ({"action_kind": "flow"}, "only agent launches"),
        ({"action_kind": "command"}, "only agent launches"),
        ({"action_cwd": "/tmp"}, "action_cwd"),
        ({"action_extra_args": ["x"]}, "action_extra_args"),
        ({"action_project": "p"}, "action_project"),
        ({"action_model": "codex/gpt-5"}, "action_model"),
        ({"action_agent": "../reviewer"}, "bare profile name"),
        ({"action_agent": "team/reviewer"}, "bare profile name"),
        ({"action_agent": "Reviewer"}, "bare profile name"),
        ({"action_prompt": "  "}, "action_prompt"),
        ({"action_head": None}, "40-character"),
        ({"action_head": HEAD[:12]}, "40-character"),
        ({"action_head": HEAD.upper()}, "40-character"),
        ({"action_head": "main"}, "40-character"),
        ({"action_repo": "other"}, "not a repository"),
        ({"action_agent": "coder"}, "runs on codex"),
        ({"action_agent": "bare"}, "no named provider"),
        ({"action_agent": "missing"}, "cannot be read"),
    ],
)
def test_the_leg_host_refusals(host: Path, overrides: dict, names: str) -> None:
    with pytest.raises(ValueError, match=names):
        leg_host.validate_launch(_request(**overrides))


def _logins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An accounts directory holding one Claude Code account, ``alpha``, set for the daemon."""
    logins = tmp_path / "logins"
    logins.mkdir()
    (logins / "claude-alpha").write_text("example-login\n")
    monkeypatch.setenv("LIONAGI_LOGINS_DIR", str(logins))
    return logins


def test_a_launch_may_name_an_account_in_the_hosts_accounts_directory(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _logins(tmp_path, monkeypatch)
    assert leg_host.validate_launch(_request(action_account="alpha")).account == "alpha"
    assert leg_host.validate_launch(_request()).account is None


@pytest.mark.parametrize(
    ("account", "names"),
    [
        ("beta", "not an account this host has for claude_code"),
        ("Alpha", "not an account name"),
        ("../alpha", "not an account name"),
    ],
)
def test_an_account_without_an_entry_or_a_valid_name_is_refused_at_launch(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: str, names: str
) -> None:
    logins = _logins(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=f"action_account: .*{names}") as refused:
        leg_host.validate_launch(_request(action_account=account))
    # The refusal goes back to the caller in a 422, so it names no path on this host.
    assert str(logins) not in str(refused.value)
    assert str(tmp_path) not in str(refused.value)


def test_a_project_profile_cannot_replace_the_hosts_own(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working directory's .lionagi/agents never decides what a leg runs as."""
    project = tmp_path / "checkout"
    (project / ".lionagi" / "agents").mkdir(parents=True)
    (project / ".lionagi" / "agents" / "reviewer.md").write_text(
        "---\nmodel: codex/gpt-5.3-codex-spark\n---\n\nShadow.\n"
    )
    monkeypatch.chdir(project)
    assert leg_host.validate_launch(_request()).agent == "reviewer"


@pytest.mark.parametrize("name", [".lionagi", ".mcp.json", ".claude"])
def test_a_tree_carrying_leg_configuration_is_refused(tmp_path: Path, name: str) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    assert leg_host.worktree_refusal(tree) is None
    target = tree / name
    target.mkdir() if name != ".mcp.json" else target.write_text("{}")
    assert name in (leg_host.worktree_refusal(tree) or "")


# Worktree removal: only <root>/<invocation id>, only by `git worktree remove`.


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def _record(*args: str) -> tuple[int, str]:
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(leg_host, "_git", _record)
    return calls


@pytest.mark.parametrize(
    "invocation_id",
    ["../escape", "abc/../../x", "ABCDEF123456", "short", "a" * 33, "", "0123abcd/.."],
)
def test_removal_refuses_any_path_that_is_not_a_daemon_id(
    host: Path, git_calls: list, invocation_id: str
) -> None:
    repo = leg_host.load_repos()["lionagi"]
    assert asyncio.run(leg_host.remove_worktree(repo, invocation_id)) is False
    assert git_calls == []


def test_removal_runs_only_git_worktree_remove_on_the_contained_path(
    host: Path, git_calls: list, tmp_path: Path
) -> None:
    repo = leg_host.load_repos()["lionagi"]
    assert asyncio.run(leg_host.remove_worktree(repo, "0123abcd4567")) is True
    [call] = git_calls
    assert call[:5] == ("-C", str(tmp_path / "clone"), "worktree", "remove", "--force")
    assert call[5] == str((tmp_path / "no-such-root").resolve() / "0123abcd4567")


def test_the_sweep_removes_only_id_shaped_entries_that_are_not_live(
    host: Path, git_calls: list, tmp_path: Path
) -> None:
    root = tmp_path / "no-such-root"
    for name in ("0123abcd4567", "89abcdef0123", "not-an-id", "README"):
        (root / name).mkdir(parents=True)
    repo = leg_host.load_repos()["lionagi"]
    removed = asyncio.run(leg_host.sweep_worktrees(repo, live={"89abcdef0123"}))
    assert removed == ["0123abcd4567"]
    assert [call[-1].rsplit("/", 1)[-1] for call in git_calls] == ["0123abcd4567"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_worktree_is_made_at_the_head_and_removed(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    (origin / "file").write_text("one\n")
    _git(origin, "add", "file")
    _git(origin, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "one")
    head = _git(origin, "rev-parse", "HEAD")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    repo = leg_host.LegRepo(name="r", clone=clone, worktrees=tmp_path / "trees")
    launch = leg_host.LegLaunch(repo=repo, head=head, agent="reviewer")

    path = asyncio.run(leg_host.make_worktree(launch, "0123abcd4567"))

    assert path == (tmp_path / "trees").resolve() / "0123abcd4567"
    assert _git(path, "rev-parse", "HEAD") == head
    assert asyncio.run(leg_host.remove_worktree(repo, "0123abcd4567")) is True
    assert not path.exists()


def test_an_unknown_head_is_a_setup_failure(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    repo = leg_host.LegRepo(name="r", clone=clone, worktrees=tmp_path / "trees")
    with pytest.raises(leg_host.LegSetupError, match="cannot fetch"):
        asyncio.run(
            leg_host.make_worktree(leg_host.LegLaunch(repo, HEAD, "reviewer"), "0123abcd4567")
        )


# What a leg is handed.


def test_the_leg_environment_is_the_allow_list() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        "CLAUDE_CODE_OAUTH_TOKEN": "login",
        "LIONAGI_STUDIO_AUTH_TOKEN": TOKEN,
        "LIONAGI_REMOTE_LEGBOX_TOKEN": TOKEN,
        "GH_TOKEN": "gh",
        "OPENAI_API_KEY": "key",
        "LIONAGI_STATE_DB_URL": "postgres://x",
    }
    parent["LIONAGI_HOME"] = "/srv/lionagi"
    parent["LIONAGI_LOGINS_DIR"] = "/srv/logins"
    assert leg_host.leg_env(parent) == {
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        "CLAUDE_CODE_OAUTH_TOKEN": "login",
        "LIONAGI_HOME": "/srv/lionagi",
        "LIONAGI_LOGINS_DIR": "/srv/logins",
    }


def test_spawn_and_wait_runs_the_child_on_the_given_environment_only(monkeypatch) -> None:
    monkeypatch.setenv("LEG_HOST_PROBE_PARENT_ONLY", "1")
    seen: list[tuple[bytes, int]] = []
    script = (
        "import os, sys; "
        "sys.stdout.write(('PARENT' if 'LEG_HOST_PROBE_PARENT_ONLY' in os.environ else 'CLEAN') + 'x' * 40)"
    )
    code, _tail = asyncio.run(
        spawn_and_wait(
            [sys.executable, "-c", script],
            "0123abcd4567",
            env={"PATH": os.environ["PATH"]},
            stdout_sink=lambda out, total: seen.append((out, total)),
            stdout_sink_bytes=2048,
            deadline_seconds=30,
        )
    )
    assert code == 0
    [(out, total)] = seen
    assert out.startswith(b"CLEAN") and total == len(out) == 45


def test_a_result_keeps_the_last_bytes_and_says_it_was_cut(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(leg_host, "RESULT_CAP_BYTES", 8)
    leg_host.write_result("0123abcd4567", b"0123456789final!", total=16)
    assert leg_host.read_result("0123abcd4567") == {"result": "89final!", "truncated": True}
    mode = stat.S_IMODE(leg_host.result_path("0123abcd4567").stat().st_mode)
    assert mode == 0o600
    with pytest.raises(ValueError):
        leg_host.result_path("../../etc/passwd")


# Starting.


def test_startup_refusals(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    (host / ".claude.json").write_text('{"mcpServers": {"store": {"command": "x"}}}')
    reasons = leg_host.startup_refusals(host="0.0.0.0")
    assert len(reasons) == 3
    assert "LIONAGI_STUDIO_AUTH_TOKEN" in reasons[0]
    assert "0.0.0.0" in reasons[1]
    assert "store" in reasons[2]


@pytest.mark.parametrize(
    "bind", ["127.0.0.1", "::1", "localhost", "100.101.102.103", "fd7a:115c:a1e0::5"]
)
def test_a_safe_host_starts(host: Path, monkeypatch: pytest.MonkeyPatch, bind: str) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", TOKEN)
    assert leg_host.startup_refusals(host=bind) == []


# Over HTTP.


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    import lionagi_studio.app as app_mod
    import lionhub.serve.daemon.services.launches as svc
    import lionhub.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(svc, "_launch_semaphore", None)
    return TestClient(
        app_mod.create_app(), raise_server_exceptions=False, base_url="http://127.0.0.1:8765"
    )


def test_a_leg_launch_turns_mcp_off_and_echoes_its_head(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lionhub.serve.daemon.services.launches as svc

    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", TOKEN)
    recorded: dict = {}

    async def _record(argv, **kwargs):
        recorded.update(kwargs, argv=argv)
        return "0123abcd4567"

    async def _no_sweep(repo, *, live):
        return []

    monkeypatch.setattr(svc, "launch_detached_argv", _record)
    monkeypatch.setattr(leg_host, "sweep_worktrees", _no_sweep)
    client = _client(monkeypatch, tmp_path)

    resp = client.post(
        "/api/launches/", json=_request(), headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert resp.status_code == 202, resp.text
    assert resp.json() == {
        "invocation_id": "0123abcd4567",
        "action_kind": "agent",
        "action_head": HEAD,
    }
    argv = recorded["argv"]
    assert argv.index("--no-mcp-config") < argv.index("--")
    assert recorded["leg"].head == HEAD
    assert recorded["node_metadata"] == {"repo": "lionagi", "head": HEAD}


def test_a_named_account_reaches_li_agent_and_is_echoed(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lionhub.serve.daemon.services.launches as svc

    _logins(tmp_path, monkeypatch)
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", TOKEN)
    recorded: dict = {}

    async def _record(argv, **kwargs):
        recorded.update(kwargs, argv=argv)
        return "0123abcd4567"

    async def _no_sweep(repo, *, live):
        return []

    monkeypatch.setattr(svc, "launch_detached_argv", _record)
    monkeypatch.setattr(leg_host, "sweep_worktrees", _no_sweep)
    client = _client(monkeypatch, tmp_path)

    resp = client.post(
        "/api/launches/",
        json=_request(action_account="alpha"),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["action_account"] == "alpha"
    argv = recorded["argv"]
    at = argv.index("--account")
    assert argv[at + 1] == "alpha" and at < argv.index("--")
    assert recorded["leg"].account == "alpha"

    missing = client.post(
        "/api/launches/",
        json=_request(action_account="beta"),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert missing.status_code == 422 and "action_account" in missing.json()["detail"]


def test_a_leg_host_refuses_other_writes_but_only_after_the_bearer(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", TOKEN)
    client = _client(monkeypatch, tmp_path)
    body = {"name": "x", "action_kind": "agent", "action_prompt": "hi", "every": "15m"}

    assert client.post("/api/schedules/", json=body).status_code == 401
    refused = client.post(
        "/api/schedules/", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert refused.status_code == 403
    caps = client.get("/api/launches/capabilities", headers={"Authorization": f"Bearer {TOKEN}"})
    assert caps.json() == {
        "leg_host": True,
        "pinned_head": True,
        "named_account": True,
        "max_concurrent": 2,
    }


def test_a_plain_daemon_serves_no_pinned_heads_and_keeps_no_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(leg_host.LEG_HOST_ENV, raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/launches/", json=_request())
    assert resp.status_code == 422
    assert "leg-host mode" in resp.json()["detail"]
    named = client.post(
        "/api/launches/",
        json={
            "action_kind": "agent",
            "action_agent": "reviewer",
            "action_prompt": "hi",
            "action_account": "alpha",
        },
    )
    assert named.status_code == 422 and "action_account" in named.json()["detail"]
    assert client.get("/api/launches/0123abcd4567/result").status_code == 404
    assert client.get("/api/launches/capabilities").json()["leg_host"] is False


# The child sandbox is what confines a leg's reads, so a leg host runs nothing without it.


def _write_settings(home: Path, text: str) -> None:
    (home / ".lionagi" / "settings.yaml").write_text(text)


def test_a_host_with_the_child_sandbox_off_does_not_start(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", TOKEN)
    _write_settings(host, "child_sandbox:\n  enabled: false\n")

    [reason] = leg_host.startup_refusals(host="127.0.0.1")

    assert "child_sandbox is disabled" in reason


def test_a_profile_exempted_from_a_read_denial_is_refused(host: Path) -> None:
    _write_settings(
        host, "child_sandbox:\n  profiles:\n    reviewer:\n      allow_read: ['~/.secrets']\n"
    )

    with pytest.raises(ValueError, match=r"exempted from reading ~/\.secrets"):
        leg_host.validate_launch(_request())


def test_the_secrets_directory_is_denied_by_default() -> None:
    from lionhub.agent.child_sandbox import DEFAULT_DENY_READ, resolve_child_sandbox

    assert "~/.secrets" in DEFAULT_DENY_READ
    assert "~/.secrets" in resolve_child_sandbox(None, settings={}).deny_read
