# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The write set of an API-model leg: editor and bash under the macOS Seatbelt sandbox.

The tool functions are called directly, so no security pre-hook runs: `guard_paths`
and `guard_destructive` are absent, and a refusal here comes from the sandbox. The
temporary directory and the uv cache are moved under the test's own directory, so
the only writable roots are the workspace and those two, and a sibling of the
workspace is outside all three.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi._errors import ConfigurationError
from lionagi.session.branch import Branch
from lionhub.agent import child_sandbox
from lionhub.agent.child_sandbox import CHILD_SANDBOX_OPT_OUT
from lionhub.tools import _seatbelt
from lionhub.tools._seatbelt import SeatbeltPolicy, seatbelt_available
from lionhub.tools.coding import CodingToolkit

from . import test_agent_api_provider as _provider_tests
from .test_agent_api_provider import _answer, _last_round, _profile, _reply

leg = _provider_tests.leg  # the scripted-endpoint fixture the provider tests run on

needs_seatbelt = pytest.mark.skipif(
    not seatbelt_available(), reason="needs macOS with /usr/bin/sandbox-exec"
)


@pytest.fixture
def roots(monkeypatch, tmp_path):
    """Workspace, outside directory and the policy's own writable roots, all resolved."""
    base = tmp_path.resolve()
    for name in ("workspace", "outside", "tmp", "uv-cache"):
        (base / name).mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base / "tmp"))
    monkeypatch.setattr(
        _seatbelt, "_uv_cache_dir", lambda *, _command_env_names=None: base / "uv-cache"
    )
    return base


def _bound_tools(workspace: Path, *, network: bool = False, deny_read=()) -> dict:
    policy = SeatbeltPolicy.for_workspace(workspace, network=network, deny_read=deny_read)
    toolkit = CodingToolkit(
        notify=False,
        workspace_root=str(workspace),
        tools=("reader", "editor", "bash"),
        seatbelt=policy,
    )
    return {t.func_callable.__name__: t.func_callable for t in toolkit.bind(Branch())}


def test_the_policy_writes_to_the_workspace_the_uv_cache_and_the_temporary_directory(roots):
    policy = SeatbeltPolicy.for_workspace(roots / "workspace")

    assert policy.writable_roots == (
        str(roots / "workspace"),
        str(roots / "uv-cache"),
        str(roots / "tmp"),
    )
    assert policy.read_only == (str(roots / "workspace" / ".git"),)
    assert policy.network is False


def test_the_network_rules_are_in_the_policy_only_when_asked_for(roots):
    off = SeatbeltPolicy.for_workspace(roots / "workspace").profile()
    on = SeatbeltPolicy.for_workspace(roots / "workspace", network=True).profile()

    assert off.startswith("(version 1)\n(deny default)\n")
    assert "network-outbound" not in off
    assert "(allow network-outbound)" in on
    assert "com.apple.SecurityServer" not in on


@needs_seatbelt
@pytest.mark.skipif(not os.access("/usr/bin/security", os.X_OK), reason="needs /usr/bin/security")
def test_a_network_on_command_cannot_fetch_a_keychain_item(roots):
    # A keychain of the test's own under the temporary directory, never the user's login keychain.
    store = str(roots / "tmp" / "scratch.keychain-db")
    item = os.urandom(16).hex()

    def security(*args: str, confined: bool = False) -> subprocess.CompletedProcess:
        argv = ["/usr/bin/security", *args]
        if confined:
            policy = SeatbeltPolicy.for_workspace(roots / "workspace", network=True)
            argv = policy.wrap(argv)
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )

    created = security("create-keychain", "-p", os.urandom(16).hex(), store)
    assert created.returncode == 0, created.stderr
    try:
        added = security(
            "add-generic-password", "-a", "probe", "-s", "lionagi-test", "-w", item, store
        )
        assert added.returncode == 0, added.stderr
        outside = security("find-generic-password", "-s", "lionagi-test", "-w", store)
        inside = security("find-generic-password", "-s", "lionagi-test", "-w", store, confined=True)
    finally:
        security("delete-keychain", store)

    assert outside.stdout.strip() == item, outside.stderr
    assert inside.returncode != 0
    assert item not in inside.stdout


def test_paths_travel_as_parameters_and_never_inside_the_policy_text(roots):
    workspace = roots / "workspace"
    argv = SeatbeltPolicy.for_workspace(workspace).wrap(["/bin/echo", "-n"])

    assert argv[0] == "/usr/bin/sandbox-exec"
    assert argv[-3:] == ["--", "/bin/echo", "-n"]
    assert f"WRITABLE_ROOT_0={workspace}" in argv
    assert f"READ_ONLY_0={workspace / '.git'}" in argv
    assert str(workspace) not in argv[argv.index("-p") + 1]


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_write_outside_the_working_directory_is_refused_by_the_sandbox(roots):
    tools = _bound_tools(roots / "workspace")
    target = roots / "outside" / "escaped.txt"

    result = await tools["bash"](f"touch {target}", cwd=str(roots / "workspace"))
    refused = SeatbeltPolicy.for_workspace(roots / "workspace").write_text(target, "x")

    assert result["return_code"] != 0
    assert "Operation not permitted" in result["stderr"]
    assert refused is not None and "Operation not permitted" in refused
    assert not target.exists()


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_write_through_a_symlink_that_points_outside_is_refused(roots):
    workspace = roots / "workspace"
    (workspace / "link").symlink_to(roots / "outside", target_is_directory=True)
    tools = _bound_tools(workspace)

    through_bash = await tools["bash"]("touch link/via-bash.txt", cwd=str(workspace))
    through_editor = await tools["editor"]("write", "link/via-editor.txt", content="x")
    through_writer = SeatbeltPolicy.for_workspace(workspace).write_text(
        workspace / "link" / "via-writer.txt", "x"
    )

    assert through_bash["return_code"] != 0
    assert "Operation not permitted" in through_bash["stderr"]
    assert through_editor["success"] is False
    assert through_writer is not None and "Operation not permitted" in through_writer
    assert list((roots / "outside").iterdir()) == []


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_write_inside_the_working_directory_succeeds_and_reads_back(roots):
    workspace = roots / "workspace"
    tools = _bound_tools(workspace)

    written = await tools["editor"]("write", "pkg/notes.txt", content="first line\n")
    read = await tools["reader"]("read", "pkg/notes.txt")
    edited = await tools["editor"](
        "edit", "pkg/notes.txt", old_string="first line", new_string="second line"
    )
    ran = await tools["bash"]("touch made-by-bash.txt", cwd=str(workspace))
    temp = await tools["bash"](f"touch {roots / 'tmp' / 'scratch.txt'}", cwd=str(workspace))

    assert written["success"] is True, written
    assert "first line" in read["content"]
    assert edited["success"] is True, edited
    assert (workspace / "pkg" / "notes.txt").read_text() == "second line\n"
    assert ran["return_code"] == 0, ran
    assert (workspace / "made-by-bash.txt").is_file()
    assert temp["return_code"] == 0, temp
    assert (roots / "tmp" / "scratch.txt").is_file()


@needs_seatbelt
@pytest.mark.asyncio
async def test_the_workspace_git_directory_stays_read_only(roots):
    workspace = roots / "workspace"
    (workspace / ".git" / "hooks").mkdir(parents=True)
    tools = _bound_tools(workspace)

    hook = await tools["bash"]("touch .git/hooks/pre-commit", cwd=str(workspace))
    ignore = await tools["editor"]("write", ".gitignore", content="dist/\n")

    assert hook["return_code"] != 0
    assert "Operation not permitted" in hook["stderr"]
    assert not (workspace / ".git" / "hooks" / "pre-commit").exists()
    assert ignore["success"] is True, ignore


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_hard_link_to_a_file_outside_the_roots_or_in_git_cannot_be_made(roots):
    workspace = roots / "workspace"
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("original\n")
    outside = roots / "outside" / "target.txt"
    outside.write_text("original\n")
    tools = _bound_tools(workspace)

    to_outside = await tools["bash"](f"ln {outside} outside-link", cwd=str(workspace))
    to_git = await tools["bash"]("ln .git/config git-link", cwd=str(workspace))
    (workspace / "made.txt").write_text("inside\n")
    inside = await tools["bash"]("ln made.txt made-link", cwd=str(workspace))

    for result in (to_outside, to_git):
        assert result["return_code"] != 0
        assert "Operation not permitted" in result["stderr"]
    assert not (workspace / "outside-link").exists()
    assert not (workspace / "git-link").exists()
    assert outside.read_text() == "original\n"
    assert inside["return_code"] == 0, inside
    assert (workspace / "made-link").stat().st_nlink == 2


def test_the_policy_denies_reading_only_the_paths_it_is_given(roots):
    workspace = roots / "workspace"
    # Without a list every read stays allowed.
    plain = SeatbeltPolicy.for_workspace(workspace)
    denied = SeatbeltPolicy.for_workspace(workspace, deny_read=[roots / "outside"])
    argv = denied.wrap(["/bin/echo"])

    assert plain.deny_read == ()
    assert "(deny file-read*" not in plain.profile()
    assert denied.deny_read == (str(roots / "outside"),)
    assert '(deny file-read* file-write* (subpath (param "DENY_READ_0")))' in denied.profile()
    assert f"DENY_READ_0={roots / 'outside'}" in argv
    assert str(roots / "outside") not in argv[argv.index("-p") + 1]


def test_a_workspace_inside_an_unreadable_path_is_refused(roots):
    with pytest.raises(ConfigurationError, match="denies reading"):
        SeatbeltPolicy.for_workspace(roots / "outside" / "ws", deny_read=[roots / "outside"])
    with pytest.raises(ConfigurationError, match="denies reading"):
        SeatbeltPolicy.for_workspace(roots / "outside", deny_read=[roots / "outside"])


def test_a_workspace_spelled_in_another_case_inside_an_unreadable_path_is_refused(roots):
    respelled = roots / "OUTSIDE"
    if not (respelled.exists() and os.path.samefile(respelled, roots / "outside")):
        pytest.skip("needs a case-insensitive volume")
    with pytest.raises(ConfigurationError, match="is inside"):
        SeatbeltPolicy.for_workspace(respelled / "ws", deny_read=[roots / "outside"])


def test_an_unreadable_path_holding_a_writable_root_is_refused(roots):
    with pytest.raises(ConfigurationError, match="writable root"):
        SeatbeltPolicy.for_workspace(roots / "workspace", deny_read=[roots / "tmp"])
    with pytest.raises(ConfigurationError, match="writable root"):
        SeatbeltPolicy.for_workspace(roots / "workspace", deny_read=[roots])


def test_an_unreadable_path_inside_the_workspace_is_refused_directly_or_through_a_link(roots):
    workspace = roots / "workspace"
    store = workspace / "store"
    store.mkdir()
    link = roots / "outside" / ".khive"
    link.symlink_to(store, target_is_directory=True)

    for entry in (store, link):
        with pytest.raises(ConfigurationError, match="inside the writable root"):
            SeatbeltPolicy.for_workspace(workspace, deny_read=[entry])


def test_every_link_an_unreadable_path_passes_through_is_denied(roots):
    target = roots / "outside" / "netrc-target"
    target.write_text("decoy\n")
    hop = roots / "outside" / "hop"
    hop.symlink_to(target)
    link = roots / "outside" / ".netrc"
    link.symlink_to(hop)

    policy = SeatbeltPolicy.for_workspace(roots / "workspace", deny_read=[link])

    assert policy.deny_read == (str(link), str(hop), str(target))


def test_a_link_under_a_linked_directory_is_denied_where_it_sits(roots):
    real = roots / "outside" / "real-config"
    real.mkdir()
    target = roots / "outside" / "netrc-target"
    target.write_text("decoy\n")
    (real / ".netrc").symlink_to(target)
    linked = roots / "outside" / ".config"
    linked.symlink_to(real, target_is_directory=True)

    policy = SeatbeltPolicy.for_workspace(roots / "workspace", deny_read=[linked / ".netrc"])

    assert str(real / ".netrc") in policy.deny_read
    assert str(target) in policy.deny_read


def test_a_link_in_the_workspace_reached_through_a_linked_directory_is_refused(roots):
    workspace = roots / "workspace"
    (workspace / "config").mkdir()
    (workspace / "config" / "gh").symlink_to("/dev/null")
    linked = roots / "outside" / ".config"
    linked.symlink_to(workspace / "config", target_is_directory=True)

    with pytest.raises(ConfigurationError, match="inside the writable root"):
        SeatbeltPolicy.for_workspace(workspace, deny_read=[linked / "gh"])


def test_an_unreadable_path_in_a_workspace_that_is_the_temporary_directory_is_refused(roots):
    (roots / "tmp" / "store").mkdir()
    with pytest.raises(ConfigurationError, match="inside the writable root"):
        SeatbeltPolicy.for_workspace(roots / "tmp", deny_read=[roots / "tmp" / "store"])


@needs_seatbelt
def test_an_unreadable_path_linked_to_dev_null_leaves_dev_null_readable(roots):
    history = roots / "outside" / ".zsh_history"
    history.symlink_to("/dev/null")
    policy = SeatbeltPolicy.for_workspace(roots / "workspace", deny_read=[history])
    proc = subprocess.run(
        policy.wrap(["/bin/cat", "/dev/null"]), capture_output=True, text=True, timeout=60
    )

    assert "/dev/null" not in policy.deny_read
    assert policy.deny_read == (str(history),)
    assert proc.returncode == 0, proc.stderr


@needs_seatbelt
def test_an_unreadable_path_inside_the_temporary_directory_cannot_be_written(roots):
    denied = roots / "tmp" / "store"
    denied.mkdir()
    (denied / "secret").write_text("decoy\n")
    policy = SeatbeltPolicy.for_workspace(roots / "workspace", deny_read=[denied])

    def run(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(policy.wrap(list(argv)), capture_output=True, text=True, timeout=60)

    overwrite = run("/bin/sh", "-c", f"echo planted > {denied / 'secret'}")
    moved = run("/bin/mv", str(denied / "secret"), str(roots / "tmp" / "moved"))
    created = run("/usr/bin/touch", str(denied / "new"))
    control = run("/usr/bin/touch", str(roots / "tmp" / "control"))

    for result in (overwrite, moved, created):
        assert result.returncode != 0
        assert "Operation not permitted" in result.stderr
    assert (denied / "secret").read_text() == "decoy\n"
    assert not (roots / "tmp" / "moved").exists() and not (denied / "new").exists()
    assert control.returncode == 0, control.stderr


def test_the_per_leg_uv_cache_is_removed_when_the_process_exits(roots, monkeypatch):
    registered = []
    monkeypatch.setattr(_seatbelt.atexit, "register", lambda *a: registered.append(a))

    policy = SeatbeltPolicy.for_workspace(roots / "workspace", per_leg_uv_cache=True)

    leg_cache = dict(policy.child_env)["UV_CACHE_DIR"]
    assert len(registered) == 1
    func, path, ignore_errors = registered[0]
    assert func is _seatbelt.shutil.rmtree and ignore_errors is True
    assert Path(path).resolve() == Path(leg_cache)


@needs_seatbelt
@pytest.mark.asyncio
async def test_an_unreadable_path_is_refused_to_bash_by_any_route_and_other_reads_work(roots):
    workspace = roots / "workspace"
    secret = roots / "outside" / "secret.txt"
    secret.write_text("decoy\n")
    (workspace / "link").symlink_to(roots / "outside", target_is_directory=True)
    (workspace / "own.txt").write_text("own\n")
    tools = _bound_tools(workspace, deny_read=[roots / "outside"])

    direct = await tools["bash"](f"cat {secret}", cwd=str(workspace))
    via_link = await tools["bash"]("cat link/secret.txt", cwd=str(workspace))
    listing = await tools["bash"](f"ls {roots / 'outside'}", cwd=str(workspace))
    hard_link = await tools["bash"](f"ln {secret} copy.txt", cwd=str(workspace))
    copied = await tools["bash"](f"cp {secret} copy.txt", cwd=str(workspace))
    own = await tools["bash"]("cat own.txt", cwd=str(workspace))

    for result in (direct, via_link, listing, hard_link, copied):
        assert result["return_code"] != 0, result
        assert "Operation not permitted" in result["stderr"], result
        assert "decoy" not in result["stdout"]
    assert not (workspace / "copy.txt").exists()
    assert own["return_code"] == 0, own
    assert own["stdout"].strip() == "own"


@needs_seatbelt
@pytest.mark.asyncio
async def test_an_unreadable_path_spelled_in_another_case_is_still_refused(roots):
    workspace = roots / "workspace"
    secret = roots / "outside" / "secret.txt"
    secret.write_text("decoy\n")
    respelled = roots / "OUTSIDE" / "secret.txt"
    if not respelled.exists():
        pytest.skip("the temporary directory is on a case-sensitive volume")
    tools = _bound_tools(workspace, deny_read=[roots / "outside"])

    result = await tools["bash"](f"cat {respelled}", cwd=str(workspace))

    assert result["return_code"] != 0, result
    assert "Operation not permitted" in result["stderr"]


def test_the_write_set_denies_reading_the_child_sandbox_credential_list(monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.agent.child_sandbox import DEFAULT_DENY_READ, _lionagi_home_credentials

    home = (tmp_path / "home").resolve()
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(_seatbelt, "_uv_cache_dir", lambda **kwargs: tmp_path / "uv-cache")

    spec = agent_mod._make_api_agent_spec(
        cwd=str(workspace), effort=None, system_prompt=None, role="implementer", tools="write"
    )

    denied = spec.seatbelt.deny_read
    for name in (".ssh", ".secrets", ".khive"):
        assert str(home / name) in denied
    expected = {str(Path(p).expanduser().resolve()) for p in DEFAULT_DENY_READ}
    assert expected <= set(denied)
    # lionagi's own .env, wherever LIONAGI_HOME puts it.
    own = {str(Path(p).expanduser().resolve()) for p in _lionagi_home_credentials()}
    assert own and own <= set(denied)
    assert not own <= {str(Path(p).expanduser().resolve()) for p in DEFAULT_DENY_READ}


def _accounts(tmp_path, monkeypatch) -> dict[str, Path]:
    """An accounts directory holding a Claude token file and a Codex row whose home is elsewhere."""
    logins = (tmp_path / "logins").resolve()
    logins.mkdir()
    token = logins / "claude-alpha"
    token.write_text("decoy-token\n")
    codex_home = (tmp_path / "codex-beta").resolve()
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("decoy-auth\n")
    (logins / "accounts.toml").write_text(f'[codex.beta]\nhome = "{codex_home}"\n')
    monkeypatch.setenv("LIONAGI_LOGINS_DIR", str(logins))
    return {"logins": logins, "token": token, "codex_home": codex_home}


def test_the_write_set_denies_reading_the_subscription_account_sign_ins(monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod

    home = (tmp_path / "home").resolve()
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(_seatbelt, "_uv_cache_dir", lambda **kwargs: tmp_path / "uv-cache")
    signins = _accounts(tmp_path, monkeypatch)

    spec = agent_mod._make_api_agent_spec(
        cwd=str(workspace), effort=None, system_prompt=None, role="implementer", tools="write"
    )

    assert {str(path) for path in signins.values()} <= set(spec.seatbelt.deny_read)


def test_the_write_set_is_refused_when_the_accounts_file_cannot_be_read(monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(_seatbelt, "_uv_cache_dir", lambda **kwargs: tmp_path / "uv-cache")
    logins = tmp_path / "logins"
    logins.mkdir()
    (logins / "accounts.toml").write_text("not toml [")
    monkeypatch.setenv("LIONAGI_LOGINS_DIR", str(logins))

    with pytest.raises(ConfigurationError, match="sign-ins") as refused:
        agent_mod._make_api_agent_spec(
            cwd=str(workspace), effort=None, system_prompt=None, role="implementer", tools="write"
        )
    # Turning the Claude Code child sandbox off does not lift this refusal, so it names no opt-out.
    assert CHILD_SANDBOX_OPT_OUT not in str(refused.value)


@needs_seatbelt
@pytest.mark.asyncio
async def test_bash_cannot_read_a_subscription_account_sign_in(roots, monkeypatch):
    from lionhub.agent.child_sandbox import credential_deny_read

    home = roots / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    signins = _accounts(roots / "outside", monkeypatch)
    workspace = roots / "workspace"
    (workspace / "own.txt").write_text("own\n")
    tools = _bound_tools(workspace, deny_read=credential_deny_read())

    token = await tools["bash"](f"cat {signins['token']}", cwd=str(workspace))
    listing = await tools["bash"](f"ls {signins['logins']}", cwd=str(workspace))
    auth = await tools["bash"](f"cat {signins['codex_home'] / 'auth.json'}", cwd=str(workspace))
    own = await tools["bash"]("cat own.txt", cwd=str(workspace))

    for result in (token, listing, auth):
        assert result["return_code"] != 0, result
        assert "Operation not permitted" in result["stderr"], result
        assert "decoy" not in result["stdout"]
    assert own["return_code"] == 0, own
    assert own["stdout"].strip() == "own"


@pytest.mark.parametrize("host", ["no-sandbox-exec", "not-macos"])
@pytest.mark.parametrize("resumed", [False, True], ids=["new", "resumed"])
@pytest.mark.asyncio
async def test_the_write_set_on_a_host_without_sandbox_exec_is_refused_before_a_run_exists(
    leg, monkeypatch, tmp_path, host, resumed
):
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.orchestration.runs import find_branch

    branch_id = None
    if resumed:
        leg.replies = [_last_round(), _answer("first")]
        _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))
        _, path = find_branch(branch_id)
        stored = json.loads(path.read_text())
        stored["metadata"]["api_launch"]["tools"] = "write"
        path.write_text(json.dumps(stored))
    if host == "no-sandbox-exec":
        monkeypatch.setattr(_seatbelt, "SANDBOX_EXEC", str(tmp_path / "missing" / "sandbox-exec"))
    else:
        monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="Seatbelt sandbox"):
        if resumed:
            await leg.run(_profile(api_tools="write"), "again", resume=branch_id)
        else:
            await leg.run(_profile(api_tools="write"))


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_api_network_must_be_a_boolean(value):
    import lionhub.serve.cli.agent as agent_mod

    with pytest.raises(ConfigurationError, match="api_network"):
        agent_mod._profile_api_launch(_profile(api_tools="write", api_network=value))


def test_api_network_applies_only_to_the_write_set():
    import lionhub.serve.cli.agent as agent_mod

    with pytest.raises(ConfigurationError, match="only to the write set"):
        agent_mod._profile_api_launch(_profile(api_tools="read", api_network=True))
    with pytest.raises(ConfigurationError, match="but no api_tools"):
        agent_mod._profile_api_launch(_profile(api_network=False))


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_write_set_leg_writes_inside_its_working_directory_through_the_loop(
    leg, monkeypatch
):
    monkeypatch.setattr(
        _seatbelt,
        "_uv_cache_dir",
        lambda *, _command_env_names=None: leg.workspace.parent / "uv-cache",
    )
    write = _reply(
        {
            "analysis": "write the summary",
            "action_requests": [
                {
                    "function": "editor",
                    "arguments": {
                        "action": "write",
                        "file_path": "summary.txt",
                        "content": "the notes say hello\n",
                    },
                }
            ],
            "action_strategy": "sequential",
            "extension_needed": True,
            "reason": {"title": "write", "content": "record the answer"},
        }
    )
    leg.replies = [write, _last_round(), _answer("written")]

    _, _, _, status, _ = await leg.run(_profile(api_tools="write"))

    assert status == "completed"
    assert set(leg.branch.acts.registry) == {"reader", "search", "context", "editor", "bash"}
    record = leg.branch.metadata["api_launch"]
    assert (record["tools"], record["network"]) == ("write", False)
    assert (leg.workspace / "summary.txt").read_text() == "the notes say hello\n"


def _fixed_child_parent_environment(monkeypatch, tmp_path):
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GH_TOKEN": "synthetic-declared-reference",
        "gh_token": "synthetic-case-alias",
        "SOMEVENDOR_API_KEY": "synthetic-pattern-credential",
        "MY_TOKENIZER_PATH": "literal=benign value",
        "LIONAGI_STATE_DB_URL": "sqlite:///synthetic-parent-store.db",
    }
    monkeypatch.setattr(_seatbelt.os, "environ", environment)
    return environment


def _capture_fixed_child(monkeypatch):
    captured = []

    def completed(argv, **kwargs):
        captured.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="/reported/uv-cache\n", stderr=b"")

    monkeypatch.setattr(_seatbelt.subprocess, "run", completed)
    return captured


def _assert_fixed_child_environment(environment, parent):
    assert "GH_TOKEN" not in environment and "gh_token" not in environment
    assert "SOMEVENDOR_API_KEY" not in environment
    assert "LIONAGI_STATE_DB_URL" not in environment
    assert environment["MY_TOKENIZER_PATH"] == parent["MY_TOKENIZER_PATH"]
    assert environment["PATH"] == parent["PATH"]


def test_uv_probe_uses_the_supplied_declared_names_at_policy_construction(monkeypatch, tmp_path):
    parent = _fixed_child_parent_environment(monkeypatch, tmp_path)
    before = dict(parent)
    captured = _capture_fixed_child(monkeypatch)
    monkeypatch.setattr(_seatbelt.shutil, "which", lambda name: "/synthetic/uv")

    policy = SeatbeltPolicy.for_workspace(
        tmp_path / "workspace", network=True, _command_env_names=frozenset({"GH_TOKEN"})
    )

    assert len(captured) == 1
    argv, kwargs = captured[0]
    assert argv == ["/synthetic/uv", "cache", "dir"]
    assert kwargs["timeout"] == 10 and kwargs["check"] is False
    assert kwargs["stdin"] is _seatbelt.subprocess.DEVNULL
    _assert_fixed_child_environment(kwargs["env"], parent)
    assert policy.writable_roots == (
        str((tmp_path / "workspace").resolve()),
        "/reported/uv-cache",
        str(Path(tempfile.gettempdir()).resolve()),
    )
    assert policy.read_only == (str((tmp_path / "workspace" / ".git").resolve()),)
    assert policy.network is True
    assert parent == before


def test_uv_probe_without_branch_names_uses_the_owned_declarations(monkeypatch, tmp_path):
    from lionagi import _command_env_metadata

    parent = _fixed_child_parent_environment(monkeypatch, tmp_path)
    captured = _capture_fixed_child(monkeypatch)
    monkeypatch.setattr(_seatbelt.shutil, "which", lambda name: "/synthetic/uv")
    monkeypatch.setattr(
        _command_env_metadata, "configured_command_env_names", lambda: frozenset({"GH_TOKEN"})
    )

    assert _seatbelt._uv_cache_dir() == Path("/reported/uv-cache")

    assert len(captured) == 1
    _assert_fixed_child_environment(captured[0][1]["env"], parent)


def test_confined_writer_keeps_the_prepared_mapping_and_fixed_child_arguments(
    monkeypatch, tmp_path
):
    captured = _capture_fixed_child(monkeypatch)
    monkeypatch.setattr(
        _seatbelt,
        "command_environment",
        lambda **kwargs: pytest.fail("a prepared writer mapping was rebuilt"),
    )
    policy = SeatbeltPolicy((str(tmp_path),), (str(tmp_path / ".git"),))
    path = tmp_path / "nested" / "note.txt"
    environment = {"PATH": "/usr/bin:/bin", "MY_TOKENIZER_PATH": "bound-nonsecret"}

    assert policy.write_text(path, "hello\n", 7.5, _command_env=environment) is None

    assert len(captured) == 1
    argv, kwargs = captured[0]
    assert argv == policy.wrap(
        [sys.executable, "-I", "-S", "-c", _seatbelt._WRITE_CHILD, str(path)]
    )
    assert kwargs["input"] == b"hello\n"
    assert kwargs["timeout"] == 7.5
    assert kwargs["capture_output"] is True and kwargs["check"] is False
    assert kwargs["env"] is environment
    assert environment == {"PATH": "/usr/bin:/bin", "MY_TOKENIZER_PATH": "bound-nonsecret"}


def test_confined_writer_without_a_mapping_uses_owned_names_and_no_grants(monkeypatch, tmp_path):
    from lionagi import _command_env_metadata

    parent = _fixed_child_parent_environment(monkeypatch, tmp_path)
    before = dict(parent)
    captured = _capture_fixed_child(monkeypatch)
    monkeypatch.setattr(
        _command_env_metadata, "configured_command_env_names", lambda: frozenset({"GH_TOKEN"})
    )
    policy = SeatbeltPolicy((str(tmp_path),))

    assert policy.write_text(tmp_path / "note.txt", "hello") is None

    assert len(captured) == 1
    _assert_fixed_child_environment(captured[0][1]["env"], parent)
    assert parent == before


@pytest.mark.parametrize("resumed", [False, True], ids=["new", "resumed"])
@pytest.mark.asyncio
async def test_the_api_write_policy_runs_no_uv_probe_new_or_resumed(leg, monkeypatch, resumed):
    monkeypatch.setattr(_seatbelt, "seatbelt_available", lambda: True)
    monkeypatch.setattr(
        _seatbelt, "_uv_cache_dir", lambda **kwargs: pytest.fail("the write policy probed uv")
    )
    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, first_status, _ = await leg.run(_profile(api_tools="write"))
    assert first_status == "completed"
    if resumed:
        leg.replies = [_last_round(), _answer("again")]
        _, _, _, status, _ = await leg.run(_profile(api_tools="write"), "again", resume=branch_id)
        assert status == "completed"


def test_the_api_write_policy_leaves_the_shared_uv_cache_out_and_gives_a_cache_of_its_own(
    roots,
):
    import lionhub.serve.cli.agent as agent_mod

    workspace = roots / "workspace"
    spec = agent_mod._make_api_agent_spec(
        cwd=str(workspace), effort=None, system_prompt=None, role="implementer", tools="write"
    )
    policy = spec.seatbelt
    env = dict(policy.child_env)

    assert policy.writable_roots == (str(workspace), str(roots / "tmp"))
    assert str(roots / "uv-cache") not in policy.writable_roots
    assert set(env) == {"UV_CACHE_DIR"}
    leg_cache = Path(env["UV_CACHE_DIR"])
    assert leg_cache.parent == roots / "tmp"
    assert leg_cache.is_dir() and list(leg_cache.iterdir()) == []
    second = agent_mod._make_api_agent_spec(
        cwd=str(workspace), effort=None, system_prompt=None, role="implementer", tools="write"
    )
    assert dict(second.seatbelt.child_env)["UV_CACHE_DIR"] != str(leg_cache)


@needs_seatbelt
@pytest.mark.asyncio
async def test_bash_gets_its_own_uv_cache_and_cannot_write_the_shared_one(roots):
    workspace = roots / "workspace"
    policy = SeatbeltPolicy.for_workspace(workspace, per_leg_uv_cache=True)
    toolkit = CodingToolkit(
        notify=False, workspace_root=str(workspace), tools=("bash",), seatbelt=policy
    )
    bash = {t.func_callable.__name__: t.func_callable for t in toolkit.bind(Branch())}["bash"]
    leg_cache = dict(policy.child_env)["UV_CACHE_DIR"]

    shown = await bash("printenv UV_CACHE_DIR", cwd=str(workspace))
    shared = await bash(f"touch {roots / 'uv-cache' / 'planted'}", cwd=str(workspace))
    own = await bash(f"touch {leg_cache}/entry", cwd=str(workspace))

    assert shown["return_code"] == 0, shown
    assert shown["stdout"].strip() == leg_cache
    assert shared["return_code"] != 0
    assert "Operation not permitted" in shared["stderr"]
    assert not (roots / "uv-cache" / "planted").exists()
    assert own["return_code"] == 0, own


def test_api_read_policy_does_not_probe_uv(monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod

    monkeypatch.setattr(
        _seatbelt, "_uv_cache_dir", lambda **kwargs: pytest.fail("read policy probed UV")
    )

    spec = agent_mod._make_api_agent_spec(
        cwd=str(tmp_path), effort=None, system_prompt=None, role="reviewer", tools="read"
    )

    assert spec.seatbelt is None


def test_a_network_on_policy_denies_connecting_inside_each_unreadable_path(roots):
    workspace = roots / "workspace"
    off = SeatbeltPolicy.for_workspace(workspace, deny_read=[roots / "outside"])
    on = SeatbeltPolicy.for_workspace(workspace, network=True, deny_read=[roots / "outside"])

    rule = '(deny network-outbound (subpath (param "DENY_READ_0")))'
    assert rule not in off.profile()
    assert on.profile().index(rule) > on.profile().index("(allow network-outbound)")


@contextlib.contextmanager
def _listener(path: Path):
    """A unix socket at ``path`` that answers each connection with ``pong``."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"pong")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.close()
        thread.join(timeout=5)


@needs_seatbelt
@pytest.mark.asyncio
async def test_a_listener_inside_an_unreadable_path_cannot_be_connected_to(roots):
    """connect() is judged by the network rules, not file-read*, so a daemon socket in a denied
    store directory needs its own rule, or the daemon reads the store for the command."""
    # A unix socket path is limited to about a hundred bytes, so the sockets sit in a short one.
    base = Path(tempfile.mkdtemp(prefix="sbx", dir="/private/tmp")).resolve()
    try:
        denied, open_dir = base / "denied", base / "open"
        denied.mkdir()
        open_dir.mkdir()
        (denied / "store.txt").write_text("decoy\n")
        workspace = roots / "workspace"
        on = _bound_tools(workspace, network=True, deny_read=[denied])
        off = _bound_tools(workspace, network=False, deny_read=[denied])
        (workspace / "client.py").write_text(
            "import socket, sys\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "s.connect(sys.argv[1])\n"
            "print(s.recv(4).decode())\n"
        )
        client = f"{shlex.quote(sys.executable)} -I client.py"
        with _listener(denied / "s"), _listener(open_dir / "s"):
            read = await on["bash"](f"cat {denied / 'store.txt'}", cwd=str(workspace))
            inside_off = await off["bash"](f"{client} {denied / 's'}", cwd=str(workspace))
            outside_on = await on["bash"](f"{client} {open_dir / 's'}", cwd=str(workspace))
            inside_on = await on["bash"](f"{client} {denied / 's'}", cwd=str(workspace))
            planted = await on["bash"](f"ln -s {denied / 's'} link", cwd=str(workspace))
            via_link = await on["bash"](f"{client} link", cwd=str(workspace))

        assert read["return_code"] != 0 and "decoy" not in read["stdout"], read
        assert inside_off["return_code"] != 0 and "pong" not in inside_off["stdout"], inside_off
        assert outside_on["return_code"] == 0, outside_on
        assert outside_on["stdout"].strip() == "pong"
        assert inside_on["return_code"] != 0, inside_on
        assert "pong" not in inside_on["stdout"]
        assert "Operation not permitted" in inside_on["stderr"], inside_on
        # A link the command plants in its own tree reaches the same socket and is refused too.
        assert planted["return_code"] == 0, planted
        assert via_link["return_code"] != 0 and "pong" not in via_link["stdout"], via_link
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_credential_list_names_the_daemon_reconnect_socket_directory():
    from lionhub.agent.child_sandbox import credential_deny_read
    from lionhub.serve.daemon.reconnect import default_socket_path

    named = {os.path.realpath(os.path.expanduser(entry)) for entry in credential_deny_read()}

    assert os.path.realpath(default_socket_path().parent) in named


@needs_seatbelt
@pytest.mark.asyncio
async def test_no_confined_command_can_connect_to_the_daemon_reconnect_socket(roots, monkeypatch):
    """The reconnect socket answers a same-user connect with a pairing code and the daemon's key."""
    import lionhub.serve.daemon.reconnect as reconnect
    from lionhub.agent.child_sandbox import credential_deny_read

    base = Path(tempfile.mkdtemp(prefix="sbx", dir="/private/tmp")).resolve()
    try:
        # A temporary LionAGI home whose store, and so whose reconnect socket, sit in a short path.
        lionagi_home = base / "home" / ".lionagi"
        monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", lionagi_home)
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", lionagi_home / "state.db")
        sock = reconnect.default_socket_path()
        assert sock == lionagi_home / "studio" / "studio-reconnect.sock"
        sock.parent.mkdir(parents=True, mode=0o700)
        (base / "open").mkdir()
        workspace = roots / "workspace"
        (workspace / "client.py").write_text(
            "import socket, sys\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "s.connect(sys.argv[1])\n"
            "print(s.recv(4).decode())\n"
        )
        client = f"{shlex.quote(sys.executable)} -I client.py"
        on = _bound_tools(workspace, network=True, deny_read=credential_deny_read())
        with _listener(sock), _listener(base / "open" / "s"):
            direct = socket.socket(socket.AF_UNIX)
            direct.connect(str(sock))
            live = direct.recv(4)
            direct.close()
            outside = await on["bash"](f"{client} {base / 'open' / 's'}", cwd=str(workspace))
            confined = await on["bash"](f"{client} {sock}", cwd=str(workspace))

        assert live == b"pong"
        assert outside["return_code"] == 0 and outside["stdout"].strip() == "pong", outside
        assert confined["return_code"] != 0, confined
        assert "pong" not in confined["stdout"]
        assert "Operation not permitted" in confined["stderr"], confined
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_write_set_denies_reading_the_paths_the_settings_add(monkeypatch, tmp_path):
    """``child_sandbox.deny_read`` in the global settings reaches the write set's policy."""
    import lionhub.serve.cli.agent as agent_mod

    home = (tmp_path / "home").resolve()
    home.mkdir()
    store = home / "private-store"
    store.mkdir()
    outside = (tmp_path / "outside-home").resolve()
    outside.mkdir()
    monkeypatch.setattr(
        child_sandbox,
        "_global_settings",
        lambda: {
            "child_sandbox": {"enabled": False, "deny_read": ["~/private-store", str(outside)]}
        },
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(_seatbelt, "_uv_cache_dir", lambda **kwargs: tmp_path / "uv-cache")

    spec = agent_mod._make_api_agent_spec(
        cwd=str(workspace), effort=None, system_prompt=None, role="implementer", tools="write"
    )

    # enabled: false turns off the Claude Code sandbox only; this policy keeps every entry.
    assert {str(store), str(outside)} <= set(spec.seatbelt.deny_read)


def test_the_write_set_denies_reading_every_credential_location_the_child_sandbox_lists(
    monkeypatch, tmp_path
):
    """Every entry of the built-in credential list is in the write set's unreadable list."""
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.agent.child_sandbox import credential_deny_read

    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(_seatbelt, "_uv_cache_dir", lambda **kwargs: tmp_path / "uv-cache")

    spec = agent_mod._make_api_agent_spec(
        cwd=str(workspace), effort=None, system_prompt=None, role="implementer", tools="write"
    )

    denied = set(spec.seatbelt.deny_read)
    listed = credential_deny_read()
    assert listed
    for entry in listed:
        written = os.path.expanduser(entry)
        assert written in denied or os.path.realpath(written) in denied, entry


def test_a_malformed_settings_block_refuses_the_write_set_without_an_opt_out(monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod

    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setattr(
        child_sandbox, "_global_settings", lambda: {"child_sandbox": {"deny_read": "relative"}}
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(_seatbelt, "_uv_cache_dir", lambda **kwargs: tmp_path / "uv-cache")

    with pytest.raises(ConfigurationError, match="deny_read") as refused:
        agent_mod._make_api_agent_spec(
            cwd=str(workspace), effort=None, system_prompt=None, role="implementer", tools="write"
        )
    assert CHILD_SANDBOX_OPT_OUT not in str(refused.value)
