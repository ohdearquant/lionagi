# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A profile's ``scratch`` gives each ``li agent`` run on a claude_code model one writable
directory, ``<run dir>/scratch``, removed at run end unless kept. Every other launch surface
and provider is refused with a named reason, and a refusal at launch leaves no run directory.
Removal takes that one directory by its recorded path and never follows a link.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi._errors import ConfigurationError
from lionhub.orchestration.run_scratch import RunScratch, remove_scratch
from lionhub.orchestration.spawn import _parse_profile, profile_config

from .test_agent_resume_model_override import _wire_agent_stubs

child_sandbox = importlib.import_module("lionhub.agent.child_sandbox")
claude_code = importlib.import_module("lionhub.providers.anthropic.claude_code")


def _role(lines: str, model: str = "claude_code/sonnet") -> str:
    return f"---\nmodel: {model}\n{lines}---\nReview.\n"


# --------------------------------------------------------------------------- the profile key


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("scratch: true\n", "remove"),
        ("scratch: keep\n", "keep"),
        ("scratch: false\n", None),
        ("", None),
    ],
)
def test_the_profile_key_reads_true_keep_or_nothing(line, expected):
    profile = _parse_profile("role", _role(line))
    assert profile.scratch == expected
    assert "scratch" not in profile.extra
    config = profile_config(profile)
    if expected is None:
        assert "scratch" not in config
    else:
        assert config["scratch"] == expected


@pytest.mark.parametrize("value", ["1", "Keep", "''", "null", "[keep]", "remove"])
def test_any_other_value_is_refused_with_the_key_named(value):
    with pytest.raises(ConfigurationError, match="scratch must be true, false or keep"):
        _parse_profile("role", _role(f"scratch: {value}\n"))


# --------------------------------------------------------------------------- removal


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    return run_dir


def _outside(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep me")
    return outside, sentinel


def test_create_makes_an_owner_only_directory_in_the_run(tmp_path):
    run_dir = _run_dir(tmp_path)
    scratch = RunScratch.create(run_dir, keep=False)
    assert scratch.path == run_dir / "scratch"
    assert stat.S_IMODE(os.lstat(scratch.path).st_mode) == 0o700
    assert scratch.record() == {"path": str(run_dir / "scratch"), "keep": False}


def test_create_refuses_a_name_already_there(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "scratch").mkdir()
    with pytest.raises(FileExistsError):
        RunScratch.create(run_dir, keep=False)


def test_finish_removes_the_directory_and_says_so(tmp_path):
    scratch = RunScratch.create(_run_dir(tmp_path), keep=False)
    (scratch.path / "nested").mkdir()
    (scratch.path / "nested" / "file.txt").write_text("x")
    entry = scratch.finish()
    assert entry == {
        "path": str(scratch.path),
        "keep": False,
        "removed": True,
        "removal_error": None,
    }
    assert not os.path.lexists(scratch.path)


def test_a_kept_directory_stays(tmp_path):
    scratch = RunScratch.create(_run_dir(tmp_path), keep=True)
    (scratch.path / "result.txt").write_text("x")
    entry = scratch.finish()
    assert entry["removed"] is False and entry["removal_error"] is None
    assert (scratch.path / "result.txt").read_text() == "x"


def test_a_link_inside_is_removed_as_a_link_and_its_targets_survive(tmp_path):
    outside, sentinel = _outside(tmp_path)
    scratch = RunScratch.create(_run_dir(tmp_path), keep=False)
    os.symlink(sentinel, scratch.path / "to-file")
    os.symlink(outside, scratch.path / "to-dir")
    entry = scratch.finish()
    assert entry["removed"] is True and entry["removal_error"] is None
    assert sentinel.read_text() == "keep me"
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel.txt"]


def test_a_link_as_the_scratch_directory_is_refused_by_name_and_left(tmp_path):
    outside, sentinel = _outside(tmp_path)
    scratch = RunScratch.create(_run_dir(tmp_path), keep=False)
    os.rmdir(scratch.path)
    os.symlink(outside, scratch.path)
    entry = scratch.finish()
    assert entry["removed"] is False
    assert "is a symbolic link" in entry["removal_error"]
    assert os.path.islink(scratch.path)
    assert sentinel.read_text() == "keep me"


def _a_file_named_scratch(run_dir: Path, tmp: Path) -> Path:
    (run_dir / "scratch").write_text("x")
    return run_dir / "scratch"


def _a_directory_named_otherwise(run_dir: Path, tmp: Path) -> Path:
    (run_dir / "other").mkdir()
    return run_dir / "other"


def _a_scratch_outside_the_run(run_dir: Path, tmp: Path) -> Path:
    (tmp / "scratch").mkdir()
    return tmp / "scratch"


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        (_a_file_named_scratch, "not a directory"),
        (_a_directory_named_otherwise, "is not named"),
        (_a_scratch_outside_the_run, "not directly in the run"),
    ],
)
def test_a_path_that_is_not_this_runs_scratch_directory_is_refused(tmp_path, arrange, reason):
    run_dir = _run_dir(tmp_path)
    path = arrange(run_dir, tmp_path)
    error = remove_scratch(path, run_dir)
    assert reason in error
    assert os.path.lexists(path)


def test_a_directory_already_gone_is_not_an_error(tmp_path):
    scratch = RunScratch.create(_run_dir(tmp_path), keep=False)
    os.rmdir(scratch.path)
    assert scratch.finish()["removal_error"] is None


@pytest.mark.skipif(
    os.chmod not in os.supports_follow_symlinks, reason="needs a mode change that follows no link"
)
def test_a_read_only_directory_inside_is_removed(tmp_path):
    scratch = RunScratch.create(_run_dir(tmp_path), keep=False)
    locked = scratch.path / "cache" / "pkg"
    locked.mkdir(parents=True)
    (locked / "mod.go").write_text("x")
    os.chmod(locked, 0o555)
    os.chmod(locked.parent, 0o555)
    entry = scratch.finish()
    assert entry["removed"] is True and entry["removal_error"] is None


# --------------------------------------------------------------------------- the claude_code child


@pytest.fixture
def _stubbed_claude(monkeypatch, tmp_path_factory):
    """As the child-sandbox tests do: a throwaway HOME, a Claude binary at the floor, no backend."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(claude_code, "resolve_claude_cli", lambda: "claude")
    monkeypatch.setattr(
        child_sandbox, "_cli_version", lambda binary: child_sandbox.CLAUDE_CLI_FLOOR
    )
    monkeypatch.setattr(child_sandbox, "check_sandbox_backend", lambda: None)
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", home / ".lionagi")
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", home / ".lionagi" / "state.db")


def _request(repo: Path, **kwargs):
    policy = child_sandbox.ChildSandboxPolicy(True, ("~/.ssh",), ("~/.cache/uv",))
    return claude_code.ClaudeCodeRequest(
        prompt="hi", repo=repo, child_sandbox=policy.to_dict(), **kwargs
    )


def _settings(args: list[str]) -> dict:
    (value,) = [args[i + 1] for i, arg in enumerate(args) if arg == "--settings"]
    return json.loads(value)


@pytest.mark.usefixtures("_stubbed_claude")
def test_the_scratch_directory_is_the_one_write_root_added(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    without = _settings(_request(repo).as_cmd_args())
    with_scratch = _settings(_request(repo, scratch_dir=str(scratch)).as_cmd_args())
    added = [
        p
        for p in with_scratch["sandbox"]["filesystem"]["allowWrite"]
        if p not in without["sandbox"]["filesystem"]["allowWrite"]
    ]
    assert added == [os.path.realpath(scratch)]
    assert os.path.realpath(scratch) in json.dumps(with_scratch["hooks"])
    assert os.path.realpath(scratch) not in json.dumps(without)


@pytest.mark.usefixtures("_stubbed_claude")
def test_a_scratch_path_that_is_a_link_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside, _ = _outside(tmp_path)
    link = tmp_path / "scratch"
    os.symlink(outside, link)
    with pytest.raises(ConfigurationError, match="symbolic link or not a directory"):
        _request(repo, scratch_dir=str(link)).as_cmd_args()


@pytest.mark.usefixtures("_stubbed_claude")
def test_a_scratch_path_that_is_gone_adds_nothing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    gone = tmp_path / "scratch"
    assert _settings(_request(repo, scratch_dir=str(gone)).as_cmd_args()) == _settings(
        _request(repo).as_cmd_args()
    )


def test_the_scratch_path_is_never_serialized(tmp_path):
    request = claude_code.ClaudeCodeRequest(prompt="hi", scratch_dir=str(tmp_path))
    assert "scratch_dir" not in request.model_dump()
    endpoint = claude_code.ClaudeCodeCLIEndpoint()
    endpoint._runtime_state["scratch_dir"] = str(tmp_path)
    assert str(tmp_path) not in json.dumps(endpoint.to_dict(), default=str)
    payload, _ = endpoint.create_payload({"messages": [{"role": "user", "content": "hi"}]})
    assert payload["request"].scratch_dir == str(tmp_path)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stubbed_claude")
@pytest.mark.parametrize("inherited", [None, {"PATH": "/usr/bin", "TMPDIR": "/elsewhere"}])
async def test_the_child_writes_its_temporary_files_in_scratch(monkeypatch, tmp_path, inherited):
    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    seen: dict = {}

    async def fake_ndjson(cmd, *, env=None, **kwargs):
        seen["env"] = env
        return
        yield  # pragma: no cover - an async generator that yields nothing

    monkeypatch.setattr(claude_code, "ndjson_from_cli", fake_ndjson)
    request = _request(repo, scratch_dir=str(scratch), env=inherited)
    async for _ in claude_code._ndjson_from_cmd(request, None):
        pass
    for name in ("TMPDIR", "TMP", "TEMP"):
        assert seen["env"][name] == os.path.realpath(scratch)
    if inherited is not None:
        assert seen["env"]["PATH"] == "/usr/bin"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stubbed_claude")
async def test_without_scratch_the_child_environment_is_untouched(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    seen: dict = {}

    async def fake_ndjson(cmd, *, env=None, **kwargs):
        seen["env"] = env
        return
        yield  # pragma: no cover

    monkeypatch.setattr(claude_code, "ndjson_from_cli", fake_ndjson)
    async for _ in claude_code._ndjson_from_cmd(_request(repo), None):
        pass
    assert seen["env"] is None


# --------------------------------------------------------------------------- li agent


def _wire_run(monkeypatch, tmp_path: Path, role_text: str) -> dict:
    import lionhub.serve.cli.agent as agent_mod

    _wire_agent_stubs(monkeypatch, tmp_path)
    profile = _parse_profile("role", role_text)
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
    state: dict = {"allocated": 0, "manifests": [], "run_dir": tmp_path / "runs" / "r"}

    def allocate():
        state["allocated"] += 1
        state["run_dir"].mkdir(parents=True)
        return SimpleNamespace(
            run_id="r",
            state_root=state["run_dir"],
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
            write_manifest=lambda data: state["manifests"].append(copy.deepcopy(data)),
        )

    monkeypatch.setattr(agent_mod, "allocate_run", allocate)
    return state


def _during_the_turn(monkeypatch, act=None) -> dict:
    from lionagi import Branch

    seen: dict = {}

    async def fake_operate(self, instruction=None, **kw):
        path = getattr(self.chat_model.endpoint, "_runtime_state", {}).get("scratch_dir")
        seen["path"] = path
        seen["was_dir"] = path is not None and os.path.isdir(path)
        if act is not None and path is not None:
            act(Path(path))
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(("line", "keep"), [("scratch: true\n", False), ("scratch: keep\n", True)])
async def test_a_claude_code_run_has_its_scratch_and_records_its_end(
    monkeypatch, tmp_path, line, keep
):
    from lionhub.serve.cli.agent import _run_agent

    state = _wire_run(monkeypatch, tmp_path, _role(line))
    seen = _during_the_turn(monkeypatch)

    await _run_agent(None, "review", agent_name="role")

    path = state["run_dir"] / "scratch"
    assert seen == {"path": str(path), "was_dir": True}
    assert state["manifests"][0].get("scratch") is None
    assert any(m.get("scratch") == {"path": str(path), "keep": keep} for m in state["manifests"])
    final = state["manifests"][-1]["scratch"]
    assert final == {"path": str(path), "keep": keep, "removed": not keep, "removal_error": None}
    assert path.is_dir() is keep


@pytest.mark.asyncio
async def test_a_link_put_in_place_of_scratch_is_left_and_named_in_run_json(monkeypatch, tmp_path):
    from lionhub.serve.cli.agent import _run_agent

    outside, sentinel = _outside(tmp_path)
    state = _wire_run(monkeypatch, tmp_path, _role("scratch: true\n"))

    def swap(path: Path) -> None:
        os.rmdir(path)
        os.symlink(outside, path)

    _during_the_turn(monkeypatch, act=swap)
    await _run_agent(None, "review", agent_name="role")

    final = state["manifests"][-1]["scratch"]
    assert final["removed"] is False
    assert "is a symbolic link" in final["removal_error"]
    assert sentinel.read_text() == "keep me"


@pytest.mark.asyncio
async def test_a_link_inside_scratch_does_not_reach_its_target(monkeypatch, tmp_path):
    from lionhub.serve.cli.agent import _run_agent

    outside, sentinel = _outside(tmp_path)
    state = _wire_run(monkeypatch, tmp_path, _role("scratch: true\n"))
    _during_the_turn(monkeypatch, act=lambda path: os.symlink(outside, path / "out"))

    await _run_agent(None, "review", agent_name="role")

    assert state["manifests"][-1]["scratch"]["removed"] is True
    assert sentinel.read_text() == "keep me"


@pytest.mark.asyncio
async def test_a_run_without_scratch_takes_none_and_records_none(monkeypatch, tmp_path):
    from lionhub.serve.cli.agent import _run_agent

    state = _wire_run(monkeypatch, tmp_path, _role(""))
    seen = _during_the_turn(monkeypatch)

    await _run_agent(None, "review", agent_name="role")

    assert seen["path"] is None
    assert all("scratch" not in m for m in state["manifests"])
    assert not (state["run_dir"] / "scratch").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "reason"),
    [
        ("codex/gpt-5.5", "sandbox probe record of format 4"),
        ("gemini-cli/gemini-2.5-pro", "does not bound reads"),
    ],
)
async def test_another_provider_is_refused_before_the_run_exists(
    monkeypatch, tmp_path, model, reason
):
    from lionhub.serve.cli.agent import _run_agent

    state = _wire_run(monkeypatch, tmp_path, _role("scratch: true\n", model=model))
    _during_the_turn(monkeypatch)

    with pytest.raises(ConfigurationError, match=reason) as caught:
        await _run_agent(None, "review", agent_name="role")

    assert "declares scratch" in str(caught.value)
    assert state["allocated"] == 0
    assert not state["run_dir"].exists()


def test_an_api_model_run_is_refused_with_its_reason():
    from lionhub.serve.cli.agent import _SCRATCH_REFUSAL_API, _scratch_refusal

    assert _scratch_refusal("anthropic", api_launch=True, is_cli=False) == _SCRATCH_REFUSAL_API
    assert _scratch_refusal("claude_code", api_launch=False, is_cli=True) is None
    assert "pi provider" in _scratch_refusal("pi", api_launch=False, is_cli=True)


# --------------------------------------------------------------------------- flows and fan-outs


@pytest.mark.asyncio
async def test_an_orchestrator_declaring_scratch_is_refused_before_its_run(monkeypatch, tmp_path):
    import lionhub.serve.cli.orchestrate._orchestration as orch_mod

    profile = _parse_profile("orc", _role("scratch: true\n"))
    allocated: list = []
    monkeypatch.setattr(orch_mod, "load_agent_profile", lambda name, *a, **kw: profile)
    monkeypatch.setattr(orch_mod, "allocate_run", lambda *a, **kw: allocated.append(1))
    with pytest.raises(ConfigurationError, match="declares scratch, which a flow"):
        await orch_mod.setup_orchestration(
            pattern_name="Fanout",
            model_spec=None,
            agent_name="orc",
            save_dir=tmp_path / "runs",
            cwd=str(tmp_path),
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            no_mcp_config=True,
        )
    assert allocated == []
    assert not (tmp_path / "runs").exists()


def test_a_worker_role_declaring_scratch_is_refused_when_it_is_resolved(monkeypatch):
    import lionhub.serve.cli.orchestrate._orchestration as orch_mod

    profile = _parse_profile("reviewer", _role("scratch: keep\n"))
    monkeypatch.setattr(orch_mod, "load_agent_profile", lambda name, *a, **kw: profile)
    with pytest.raises(ConfigurationError, match="'reviewer' declares scratch"):
        orch_mod.resolve_worker_spec("reviewer")
