# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Workers are told where their output goes, and the run reports where it went."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi.cli._providers import _add_workload_kwarg
from lionagi.cli.orchestrate._common import (
    BARE_WORKER_SYSTEM,
    bare_worker_system,
    retarget_artifact_section,
    worker_artifact_section,
)
from lionagi.cli.orchestrate._orchestration import (
    _emit_worker_artifact_report,
    collect_worker_artifacts,
)
from lionagi.providers._subagent_admission import DEFAULT_WORKLOAD

# Half 1: the prompt names the directory

# The sentence the worker prompt used to carry: a claim about text the harness
# does not control. Its absence is the fix, so it is asserted directly.
_OLD_ASSERTION = "Your instruction tells you where to write output"


def test_prompt_names_the_artifact_directory():
    prompt = bare_worker_system(artifact_dir="/tmp/run-x/artifacts/researcher")
    assert "ARTIFACT DIRECTORY: /tmp/run-x/artifacts/researcher" in prompt


def test_prompt_no_longer_asserts_what_the_instruction_contains():
    assert _OLD_ASSERTION not in bare_worker_system(artifact_dir="/tmp/run-x/a")
    assert _OLD_ASSERTION not in bare_worker_system()
    assert _OLD_ASSERTION not in BARE_WORKER_SYSTEM


def test_module_level_constant_still_builds_without_a_directory():
    assert isinstance(BARE_WORKER_SYSTEM, str)
    assert "ARTIFACT DIRECTORY: your working directory." in BARE_WORKER_SYSTEM
    # Coherent with no directory in hand: it still says where output goes.
    assert "Write every output file there" in BARE_WORKER_SYSTEM


def test_grant_spawn_still_composes_with_the_directive():
    prompt = bare_worker_system(grant_spawn=True, artifact_dir="/tmp/run-x/a")
    assert "ARTIFACT DIRECTORY: /tmp/run-x/a" in prompt
    assert "Workflow expansion" in prompt
    assert "you are a leaf executor" not in prompt


def test_relative_directory_is_refused():
    # A relative name would resolve against whatever cwd the worker has, which
    # is the ambiguity the directive exists to remove.
    with pytest.raises(ValueError, match="absolute path"):
        worker_artifact_section("artifacts/researcher")


def test_retarget_replaces_an_inherited_directive():
    inherited = bare_worker_system(artifact_dir="/tmp/run-x/artifacts/emitter")
    retargeted = retarget_artifact_section(inherited, "/tmp/run-x/artifacts/spawned")
    assert "ARTIFACT DIRECTORY: /tmp/run-x/artifacts/spawned" in retargeted
    assert "/tmp/run-x/artifacts/emitter" not in retargeted


def test_retarget_appends_when_no_directive_is_present():
    out = retarget_artifact_section("A profile body with no directive.", "/tmp/run-x/a")
    assert "A profile body with no directive." in out
    assert "ARTIFACT DIRECTORY: /tmp/run-x/a" in out


def test_retarget_without_workspace_uses_truthful_output_only_guidance():
    inherited = bare_worker_system(artifact_dir="/tmp/run-x/artifacts/emitter")

    retargeted = retarget_artifact_section(
        inherited,
        "/tmp/run-x/artifacts/spawned",
        workspace_assigned=False,
    )

    assert "ARTIFACT DIRECTORY: /tmp/run-x/artifacts/spawned" in retargeted
    assert "/tmp/run-x/artifacts/emitter" not in retargeted
    assert "not an assigned working directory" in retargeted
    assert "It is your working directory" not in retargeted
    assert "SESSION PERSISTENCE" in retargeted


def test_retarget_preserves_profile_authored_policy_around_a_directive():
    inherited = """\
ARTIFACT DIRECTORY: /tmp/run-x/artifacts/emitter

Write every output file there after first obeying this policy:
NEVER disclose secrets.
Reference upstream artifacts only by paths you were given.

Remain concise."""

    retargeted = retarget_artifact_section(
        inherited,
        "/tmp/run-x/artifacts/spawned",
    )

    assert "ARTIFACT DIRECTORY: /tmp/run-x/artifacts/spawned" in retargeted
    assert "/tmp/run-x/artifacts/emitter" not in retargeted
    assert "NEVER disclose secrets." in retargeted
    assert "Remain concise." in retargeted


# build_worker_branch names and records each worker's artifact destination


def _build_worker(
    tmp_path,
    *,
    agent_id,
    role,
    bare=True,
    profile=None,
    is_cli=True,
    system_prompt_override=None,
    provider="claude_code",
    team_data=None,
):
    """Run `build_worker_branch` with no model, no MCP, and no I/O.

    Returns ``(env, imodel, built)`` where ``built`` holds the kwargs the
    verbatim path passed to `Branch`, or the captured `AgentSpec` under
    ``"spec"`` when the casts factory path was taken instead. Both paths are
    exercised for real up to the point a prompt is handed over.
    """
    import asyncio

    import pytest as _pytest

    from lionagi.cli.orchestrate import _orchestration as orch

    built: dict = {}

    class _Endpoint:
        def __init__(self):
            self.is_cli = is_cli
            self.config = SimpleNamespace(kwargs={}, provider=provider)

    class _IModel:
        def __init__(self):
            self.is_cli = is_cli
            self.endpoint = _Endpoint()

    imodel = _IModel()

    class _Branch:
        def __init__(self, **kw):
            built.update(kw)
            self.name = kw.get("name")
            self.id = "b1"

    async def _fake_create_agent(spec, **kw):
        built["spec"] = spec
        return _Branch(system=spec.extra_prompt, name=None)

    def _fake_build_imodel(*a, **kw):
        built["imodel_kwargs"] = kw
        _add_workload_kwarg(
            imodel.endpoint.config.kwargs,
            imodel.endpoint,
            kw.get("workload", DEFAULT_WORKLOAD),
        )
        return imodel

    env = orch.OrchestrationEnv(
        run=SimpleNamespace(agent_artifact_dir=lambda aid: tmp_path / "artifacts" / aid),
        session=SimpleNamespace(include_branches=lambda b: None),
        orc_branch=SimpleNamespace(id="orc"),
        builder=None,
        orc_profile=None,
        orc_profile_name=None,
        default_model_spec="claude_code/sonnet",
        bare=bare,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=str(tmp_path),
        team_data=team_data,
    )

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(orch, "build_imodel_from_spec", _fake_build_imodel)
        mp.setattr(orch, "Branch", _Branch)
        mp.setattr(orch, "create_agent", _fake_create_agent)
        mp.setattr(orch, "_hand_mcp_servers", lambda *a, **k: None)
        mp.setattr(orch, "register_profile_injection", lambda *a, **k: None)
        mp.setattr(
            orch,
            "_resolve_worker_model_spec",
            lambda env, role, override: ("claude_code/sonnet", profile, None),
        )
        mp.setattr(orch, "team_worker_system", lambda *a, **k: "")
        asyncio.run(
            orch.build_worker_branch(
                env,
                agent_id=agent_id,
                role=role,
                system_prompt_override=system_prompt_override,
            )
        )
    finally:
        mp.undo()

    return env, imodel, built


def _add_dir_values(args: list[str]) -> list[str]:
    return [args[index + 1] for index, value in enumerate(args) if value == "--add-dir"]


def _light_profile():
    return SimpleNamespace(
        raw_body=True,
        system_prompt="Read and report.",
        effort=None,
        yolo=False,
        fast_mode=False,
        khive_injection=None,
        workload="light",
    )


def _codex_worker_kwargs(tmp_path, *, team_data=None, light=False, provider="codex") -> dict:
    _env, imodel, _built = _build_worker(
        tmp_path,
        agent_id="worker-1",
        role="worker",
        provider=provider,
        team_data=team_data,
        bare=not light,
        profile=_light_profile() if light else None,
    )
    return imodel.endpoint.config.kwargs


def _codex_worker_args(tmp_path, *, team_data=None, light=False, provider="codex") -> list[str]:
    from lionagi.providers.openai.codex import CodexCodeRequest

    kwargs = _codex_worker_kwargs(tmp_path, team_data=team_data, light=light, provider=provider)
    request = CodexCodeRequest(prompt="work", **kwargs)
    return request.as_cmd_args()


# An alias of the codex provider is the same provider, and gets the same grant.
@pytest.mark.parametrize("provider", ["codex", "openai-codex", "Codex"])
def test_codex_team_worker_grants_only_the_teams_directory(tmp_path, monkeypatch, provider):
    from lionagi.cli import team as team_module

    teams_dir = tmp_path / "state" / "teams"
    teams_dir.mkdir(parents=True)
    monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

    args = _codex_worker_args(tmp_path, team_data={"id": "team-1"}, provider=provider)

    assert _add_dir_values(args) == [str(tmp_path.resolve()), str(teams_dir.resolve())]


def test_codex_worker_without_team_does_not_grant_the_teams_directory(tmp_path, monkeypatch):
    from lionagi.cli import team as team_module

    teams_dir = tmp_path / "state" / "teams"
    teams_dir.mkdir(parents=True)
    monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

    args = _codex_worker_args(tmp_path)

    assert _add_dir_values(args) == [str(tmp_path.resolve())]


def test_a_light_codex_worker_gets_no_extra_writable_root_and_starts(tmp_path):
    """Codex refuses a light request any directory outside its working directory, so
    the worker is handed none and writes only its artifact directory."""
    kwargs = _codex_worker_kwargs(tmp_path, light=True)
    assert kwargs["workload"] == "light"
    assert kwargs["repo"] == tmp_path / "artifacts" / "worker-1"

    args = _codex_worker_args(tmp_path, light=True)

    assert _add_dir_values(args) == []
    assert args[args.index("--sandbox") + 1] == "workspace-write"


@pytest.mark.parametrize("provider", ["codex", "openai-codex", "Codex"])
def test_a_light_codex_team_worker_is_refused_before_it_starts(tmp_path, monkeypatch, provider):
    """Team mode needs the teams directory writable, which light codex cannot grant."""
    from lionagi._errors import CLIInputRefusedError
    from lionagi.cli import team as team_module

    teams_dir = tmp_path / "state" / "teams"
    teams_dir.mkdir(parents=True)
    monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

    with pytest.raises(CLIInputRefusedError, match="add_dir"):
        _codex_worker_args(tmp_path, team_data={"id": "team-1"}, light=True, provider=provider)


def test_a_light_claude_worker_keeps_the_project_root(tmp_path):
    """Light Claude Code bounds the tool list, not the filesystem, so it keeps the
    project root it reads from."""
    _env, imodel, _built = _build_worker(
        tmp_path, agent_id="worker-1", role="worker", bare=False, profile=_light_profile()
    )
    assert imodel.endpoint.config.kwargs["workload"] == "light"
    assert imodel.endpoint.config.kwargs["add_dir"] == [str(tmp_path.resolve())]


def test_worker_prompt_names_the_cwd_it_is_launched_with(tmp_path):
    """The named directory and the `repo` kwarg are the same value.

    This is the property that makes the named path writable at all: the
    file-editing tool refuses absolute paths outside the working directory.
    """
    env, imodel, built = _build_worker(tmp_path, agent_id="researcher", role="researcher")

    repo = imodel.endpoint.config.kwargs["repo"]
    assert Path(repo) == tmp_path / "artifacts" / "researcher"
    assert f"ARTIFACT DIRECTORY: {repo}" in built["system"]
    # And it is registered for the end-of-run report.
    assert env.worker_artifact_dirs["researcher"] == Path(repo)


def test_a_casts_role_worker_prompt_names_its_own_directory(tmp_path):
    """The default (non-`--bare`) worker composes from a role body, not from
    `bare_worker_system`, so its directive has to be attached on that path too."""
    from lionagi.cli.orchestrate._orchestration import _is_casts_role

    assert _is_casts_role("researcher"), "test premise: researcher is a built-in casts role"

    env, imodel, built = _build_worker(
        tmp_path, agent_id="researcher", role="researcher", bare=False
    )

    repo = imodel.endpoint.config.kwargs["repo"]
    assert Path(repo) == tmp_path / "artifacts" / "researcher"
    assert f"ARTIFACT DIRECTORY: {repo}" in built["spec"].extra_prompt
    assert env.worker_artifact_dirs["researcher"] == Path(repo)


def test_a_profile_authored_body_worker_is_told_its_directory(tmp_path):
    """A profile that authored a body runs that body verbatim, with no role to
    compose from — the one live prompt path that could otherwise reach a worker
    carrying no directive at all."""
    profile = SimpleNamespace(
        raw_body=True,
        system_prompt="You are a specialist. Do the work you are given.",
        effort=None,
        yolo=False,
        fast_mode=False,
        khive_injection=None,
    )
    env, imodel, built = _build_worker(
        tmp_path, agent_id="specialist", role="specialist", bare=False, profile=profile
    )

    repo = imodel.endpoint.config.kwargs["repo"]
    assert "You are a specialist." in built["system"], "the authored body still runs"
    assert f"ARTIFACT DIRECTORY: {repo}" in built["system"]
    assert env.worker_artifact_dirs["specialist"] == Path(repo)


def test_a_profile_body_naming_another_directory_is_retargeted(tmp_path):
    """An authored body is prose the harness did not write. If it names a
    directory of its own, the worker cannot write there — only its cwd."""
    profile = SimpleNamespace(
        raw_body=True,
        system_prompt="ARTIFACT DIRECTORY: /somewhere/the/author/chose\n\nDo the work.",
        effort=None,
        yolo=False,
        fast_mode=False,
        khive_injection=None,
    )
    _env, imodel, built = _build_worker(
        tmp_path, agent_id="specialist", role="specialist", bare=False, profile=profile
    )

    repo = imodel.endpoint.config.kwargs["repo"]
    assert f"ARTIFACT DIRECTORY: {repo}" in built["system"]
    assert "/somewhere/the/author/chose" not in built["system"]


@pytest.mark.parametrize(
    ("bare", "profile", "system_prompt_override", "prompt_key"),
    [
        (True, None, None, "system"),
        (False, None, None, "spec"),
        (
            False,
            SimpleNamespace(
                raw_body=True,
                system_prompt="A profile-authored API worker.",
                effort=None,
                yolo=False,
                fast_mode=False,
                khive_injection=None,
            ),
            None,
            "system",
        ),
        (False, None, "An explicitly configured API worker.", "system"),
    ],
)
def test_initial_api_worker_paths_use_output_only_artifact_guidance(
    tmp_path,
    bare,
    profile,
    system_prompt_override,
    prompt_key,
):
    """Ensure every initial API prompt treats its artifact path as output-only.

    CLI-only endpoint settings must remain absent.
    """
    env, imodel, built = _build_worker(
        tmp_path,
        agent_id="api-worker",
        role="researcher",
        bare=bare,
        profile=profile,
        is_cli=False,
        system_prompt_override=system_prompt_override,
    )

    prompt = built[prompt_key]
    if prompt_key == "spec":
        prompt = prompt.extra_prompt
    artifact_dir = tmp_path / "artifacts" / "api-worker"
    assert f"ARTIFACT DIRECTORY: {artifact_dir}" in prompt
    assert "not an assigned working directory" in prompt
    assert "It is your working directory" not in prompt
    assert "repo" not in imodel.endpoint.config.kwargs
    assert "add_dir" not in imodel.endpoint.config.kwargs
    assert env.worker_artifact_dirs["api-worker"] == artifact_dir


def test_a_reactively_spawned_branch_stops_naming_the_emitters_directory(tmp_path):
    """A clone inherits the emitter's prompt, so the directive it carries names
    a directory that is no longer its own — on a real `Branch`, not a stand-in
    for one, because the rewrite goes through the message manager."""
    from lionagi import Branch
    from lionagi.cli.orchestrate.flow import _retarget_spawn_prompt

    emitter_dir = tmp_path / "artifacts" / "emitter"
    spawn_dir = tmp_path / "artifacts" / "spawn-1"
    branch = Branch(system=bare_worker_system(artifact_dir=emitter_dir))

    _retarget_spawn_prompt(branch, spawn_dir)

    system_text = branch.msgs.system.content.system_message
    assert f"ARTIFACT DIRECTORY: {spawn_dir}" in system_text
    assert str(emitter_dir) not in system_text


# Half 2: the run reports where each worker actually wrote


def _env_with_dirs(dirs: dict[str, Path]) -> SimpleNamespace:
    return SimpleNamespace(worker_artifact_dirs=dirs)


def test_a_worker_that_wrote_nothing_is_named_not_omitted(tmp_path):
    wrote = tmp_path / "wrote"
    wrote.mkdir()
    (wrote / "research.md").write_text("x")
    empty = tmp_path / "empty"
    empty.mkdir()

    entries = collect_worker_artifacts(_env_with_dirs({"a": wrote, "b": empty}))

    by_id = {e["agent_id"]: e for e in entries}
    assert set(by_id) == {"a", "b"}, "an empty worker must not be dropped from the report"
    assert by_id["a"]["files"] == ["research.md"]
    assert by_id["b"]["files"] == []


def test_an_all_empty_run_does_not_render_as_a_clean_report(tmp_path, caplog):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    env = _env_with_dirs({"a": tmp_path / "a", "b": tmp_path / "b"})

    entries = collect_worker_artifacts(env)
    with caplog.at_level("INFO", logger="lionagi.cli.hint"):
        _emit_worker_artifact_report(entries)

    out = caplog.text
    assert "a: produced nothing" in out
    assert "b: produced nothing" in out
    # The header alone is not a pass-shaped result: every worker has a row.
    assert out.count("produced nothing") == 2


def test_nested_files_are_listed_relative_to_the_artifact_dir(tmp_path):
    d = tmp_path / "a"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "notes.md").write_text("x")
    entries = collect_worker_artifacts(_env_with_dirs({"a": d}))
    assert entries[0]["files"] == ["sub/notes.md"]


def test_a_missing_directory_is_reported_as_missing_not_as_nothing_written(tmp_path, caplog):
    """Registration creates the directory, so its later absence is not the worker's silence.

    ``rglob`` over a path that does not exist yields nothing, which would render
    identically to a worker that ran and chose to write no files. The two are
    different events — one is the worker's own outcome, the other means the
    directory the run handed out was removed by something else — so reporting
    them the same way would attribute a deletion to the worker.
    """
    entries = collect_worker_artifacts(_env_with_dirs({"a": tmp_path / "never-created"}))
    assert entries[0]["agent_id"] == "a"
    assert entries[0]["status"] == "missing"
    assert entries[0]["files"] == []

    with caplog.at_level("INFO", logger="lionagi.cli.hint"):
        _emit_worker_artifact_report(entries)
    assert "a: MISSING" in caplog.text
    assert "produced nothing" not in caplog.text


def test_an_invalid_recorded_path_does_not_stop_other_workers(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "notes.md").write_text("x")
    invalid = Path(f"{tmp_path / 'invalid'}\x00")

    entries = collect_worker_artifacts(_env_with_dirs({"invalid": invalid, "good": good}))

    assert entries[0]["status"] == "unreadable"
    assert "ValueError" in entries[0]["error"]
    assert entries[1]["status"] == "reported"
    assert entries[1]["files"] == ["notes.md"]


def test_a_path_below_a_file_is_reported_as_missing(tmp_path):
    parent = tmp_path / "file"
    parent.write_text("x")

    entries = collect_worker_artifacts(_env_with_dirs({"a": parent / "child"}))

    assert entries[0]["status"] == "missing"
    assert "does not exist" in entries[0]["error"]


def test_an_unreachable_child_directory_is_reported_as_unreadable(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "visible.md").write_text("x")
    hidden = root / "hidden"
    hidden.mkdir()
    (hidden / "notes.md").write_text("x")
    original_scandir = os.scandir

    def restricted_scandir(path):
        if Path(path) == hidden:
            raise PermissionError("directory cannot be listed")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", restricted_scandir)

    entries = collect_worker_artifacts(_env_with_dirs({"a": root}))

    assert entries[0]["status"] == "unreadable"
    assert "PermissionError" in entries[0]["error"]
    assert entries[0]["files"] == []


def test_a_worker_the_run_expected_but_never_registered_is_named_and_warned(tmp_path, caplog):
    """A worker launched without a directory must not read as a smaller run.

    The reporting map alone cannot show this: it has no row to omit. Only the
    run's own statement of which workers it would have makes the omission
    visible, which is why that roster is recorded separately.
    """
    registered = tmp_path / "registered"
    registered.mkdir()
    env = SimpleNamespace(
        worker_artifact_dirs={"registered": registered},
        expected_worker_ids=["registered", "unregistered"],
    )

    entries = collect_worker_artifacts(env)
    by_id = {e["agent_id"]: e for e in entries}
    assert set(by_id) == {"registered", "unregistered"}
    assert by_id["unregistered"]["status"] == "unregistered"
    assert by_id["unregistered"]["dir"] is None

    with caplog.at_level("INFO"):
        _emit_worker_artifact_report(entries)
    assert "unregistered: NOT REGISTERED" in caplog.text
    # Loud, not just a row: the run says plainly that output cannot be located.
    assert "never had an artifact directory recorded" in caplog.text


def test_report_is_emitted_and_recorded_by_finalize(tmp_path, monkeypatch, caplog):
    from lionagi.cli.orchestrate._orchestration import finalize_orchestration

    d = tmp_path / "researcher"
    d.mkdir()
    (d / "out.md").write_text("x")
    # Registration creates the directory, so a worker that wrote nothing still
    # has one — that is what "produced nothing" means, as distinct from MISSING.
    (tmp_path / "critic").mkdir()

    orc = SimpleNamespace(
        id="orc",
        chat_model=SimpleNamespace(endpoint=SimpleNamespace(config=SimpleNamespace(provider="p"))),
        name="orchestrator",
        to_dict=lambda: {},
    )
    run = SimpleNamespace(
        ensure_state_dirs=lambda: None,
        branch_path=lambda bid: tmp_path / f"{bid}.json",
        run_id="r1",
    )
    env = SimpleNamespace(
        run=run,
        session=SimpleNamespace(branches=[orc]),
        orc_branch=orc,
        worker_artifact_dirs={"researcher": d, "critic": tmp_path / "critic"},
    )
    monkeypatch.setattr(
        "lionagi.cli.orchestrate._orchestration.save_last_branch_pointer",
        lambda *a, **k: None,
    )

    with caplog.at_level("INFO", logger="lionagi.cli.hint"):
        finalize_orchestration(env, kind="fanout", prompt="p", emit_hints=False)

    # emit_hints=False silences the resume pointers, not the artifact record.
    out = caplog.text
    assert "researcher: 1 file(s)" in out
    assert "critic: produced nothing" in out

    extras = env._finalize_extras
    by_id = {e["agent_id"]: e for e in extras["worker_artifacts"]}
    assert by_id["researcher"]["files"] == ["out.md"]
    assert by_id["critic"]["files"] == []


def test_a_directory_the_run_cannot_reach_is_unreadable_not_missing(tmp_path):
    """A row saying "I could not look" is not a row saying "it was removed".

    The trigger is a directory whose PARENT cannot be traversed, not one that
    is merely mode 000 itself: a locked directory is still stat-able, because
    that only needs the traverse bit on its parent.

    The probe used to sit outside the try, and it went wrong differently on
    either side of 3.14. On 3.13 and below `exists()` raises here, and nothing
    caught it, so one unreachable worker directory took down the whole harvest
    including every other worker's row. On 3.14 it answers False instead, so
    the row read `missing` with an error naming a deletion that never happened
    for a directory sitting right there. Both are now `unreadable`, which is
    the status this function already carried for exactly this case.
    """

    locked = tmp_path / "locked"
    (locked / "child").mkdir(parents=True)
    (locked / "child" / "notes.md").write_text("x")
    locked.chmod(0o000)
    try:
        try:
            (locked / "child").stat()
        except PermissionError:
            pass
        else:
            pytest.skip("the 0o000 mode does not bind for this process")

        entries = collect_worker_artifacts(_env_with_dirs({"a": locked / "child"}))

        assert entries[0]["status"] == "unreadable"
        assert "does not exist" not in (entries[0]["error"] or "")
        assert "PermissionError" in entries[0]["error"]
    finally:
        locked.chmod(0o755)


def test_a_genuinely_absent_directory_is_still_missing(tmp_path):
    """The must-match control: the fix must not turn every absent row unreadable."""

    entries = collect_worker_artifacts(_env_with_dirs({"a": tmp_path / "never-created"}))
    assert entries[0]["status"] == "missing"
    assert "does not exist" in entries[0]["error"]


async def _spawned_branch_kwargs(
    tmp_path,
    monkeypatch,
    workload: str,
    team_data=None,
    *,
    from_orchestrator: bool = False,
    provider: str = "codex",
    emitted_by: str | None = None,
    emitter_provider: str | None = None,
    emitter_contained: bool = False,
    independent: bool = True,
    assignee: str | None = None,
    role_alias: str | None = None,
    role_contained: bool = False,
    role_provider: str = "claude_code",
    handed: list | None = None,
    expected: list | None = None,
    refuses: bool = False,
) -> dict:
    """Run a reactively spawned branch through the setup `_execute_dag` hands the DAG
    runner, and return the request kwargs it leaves on the branch. The spawn is a clone
    of a worker's branch, or of the orchestrator's when *from_orchestrator*; both carry
    *workload*. When *emitted_by* names a class, the spawn is an independent request
    naming no assignee, built by the run's node builder for a worker of that class (or
    for the orchestrator itself, when it is "orchestrator") and cloned from the
    orchestrator's branch, as the executor clones it. *emitter_provider* puts that worker
    on another engine than the orchestrator's, and *emitter_contained* runs it with no
    settings files read (``contain_user_settings``). With *independent* false the request is a
    dependent one, cloned from that worker's branch. With *assignee* the request names a
    role of that name on *role_provider* (Claude unless named), and is cloned from its
    branch, which *role_contained* runs with no settings files read. *role_alias* plans a
    worker of that name on the role and has the request name the worker, as a team
    instance (``reviewer-2``) is named. *handed* collects the
    server set each hand-off gives the spawn and whether it is the whole set. *expected*
    collects the spawn ids the run is told to expect a worker for. With *refuses* the node builder must refuse
    the request, and the refusal's text comes back as ``{"refusal": ...}``."""
    import lionhub.orchestration.engines as engines_mod
    from lionagi import Branch, iModel
    from lionagi.casts.emission import SpawnRequest
    from lionagi.cli.orchestrate import flow as flow_mod

    class _Captured(Exception):
        pass

    captured: dict = {}

    class _Run:
        async def run_dag(self, *args, **kwargs):
            captured["setup"] = kwargs["spawn_branch_setup"]
            captured["node_builder"] = kwargs["node_builder"]
            captured["executor_ref"] = kwargs["executor_ref"]
            raise _Captured

    class _Engine:
        def new_run(self, session=None):
            return _Run()

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(engines_mod, "PlanningEngine", _Engine)
    monkeypatch.setattr(flow_mod, "_persist_session_phase", _noop)
    monkeypatch.setattr(flow_mod, "_persist_control_poller_state_bounded", _noop)

    def _hand(imodel, servers, **kwargs):
        if handed is not None:
            handed.append((servers, kwargs.get("exclusive")))

    monkeypatch.setattr(flow_mod, "_hand_mcp_servers", _hand)

    def _branch(
        name: str, branch_workload: str = workload, branch_provider: str = provider
    ) -> Branch:
        from lionagi.service.connections.registry import EndpointRegistry

        canonical = EndpointRegistry.canonical_provider(branch_provider)
        chat_model = iModel(
            provider=branch_provider,
            model={
                "codex": "gpt-5.5",
                "openai": "gpt-4.1-mini",
                "gemini_code": "gemini-2.5-pro",
            }.get(canonical, "sonnet"),
            **({"api_key": "dummy-key"} if branch_provider == "openai" else {}),
        )
        _add_workload_kwarg(
            chat_model.endpoint.config.kwargs,
            chat_model.endpoint,
            branch_workload,
        )
        return Branch(
            system=bare_worker_system(artifact_dir=tmp_path / "artifacts" / name),
            chat_model=chat_model,
        )

    orchestrator = _branch("orchestrator")
    role_branch = None
    if assignee is not None:
        role_branch = _branch(assignee, branch_provider=role_provider)
        if role_contained:
            from lionagi.cli.orchestrate._orchestration import _isolate_worker_cli_config

            _isolate_worker_cli_config(role_branch.chat_model, contain_user_settings=True)
    env = SimpleNamespace(
        orc_branch=orchestrator,
        run=SimpleNamespace(agent_artifact_dir=lambda aid: tmp_path / "artifacts" / aid),
        session=SimpleNamespace(branches=[], observe=lambda *a, **k: None),
        builder=SimpleNamespace(get_graph=lambda: None),
        worker_artifact_dirs={},
        cwd=str(tmp_path),
        verbose=False,
        effort=None,
        _live_persist=None,
        team_data=team_data,
        exchange=None,
        expect_worker=(lambda spawn_id: None) if expected is None else expected.append,
    )
    dag_state = SimpleNamespace(
        reactive=True,
        spawn_roles=None,
        node_ids={},
        known_nodes=set(),
        deps_by_node={},
        worker_models={},
        role_base={} if role_branch is None else {assignee: role_branch},
        role_artifact_defaults={},
        op_segments=[],
        max_spawn=5,
    )
    plan = SimpleNamespace(
        assignments=[SimpleNamespace(assignee=assignee)] if role_alias else [],
        agent_ids=[role_alias] if role_alias else [],
    )
    with pytest.raises(_Captured):
        await flow_mod._execute_dag(env, plan, dag_state, max_concurrent=1, max_ops=5)

    if emitted_by is None:
        source = orchestrator if from_orchestrator else _branch("emitter")
        operation = SimpleNamespace(metadata={"spawn_id": "spawn-1"})
    else:
        emitter = SimpleNamespace(id="emitter-op")
        emitter_branch = (
            orchestrator
            if emitted_by == "orchestrator"
            else _branch("emitter", emitted_by, emitter_provider or provider)
        )
        if emitter_contained:
            from lionagi.cli.orchestrate._orchestration import _isolate_worker_cli_config

            _isolate_worker_cli_config(emitter_branch.chat_model, contain_user_settings=True)
        captured["executor_ref"]["executor"] = SimpleNamespace(
            operation_branches={emitter.id: emitter_branch}
        )
        request = SpawnRequest(
            instruction="check the result",
            independent=independent,
            assignee=role_alias or assignee,
        )
        if refuses:
            from lionagi._errors import CLIInputRefusedError

            with pytest.raises(CLIInputRefusedError) as refused:
                captured["node_builder"](request, emitter)
            return {"refusal": str(refused.value)}
        operation = captured["node_builder"](request, emitter)
        if role_branch is not None:
            assert operation.branch_id == role_branch.id
            source = role_branch
        else:
            assert operation.branch_id is None
            source = orchestrator if independent else emitter_branch
    orchestrator_kwargs = dict(orchestrator.chat_model.endpoint.config.kwargs)
    branch = source.clone()
    captured["setup"](operation, branch)
    kwargs = branch.chat_model.endpoint.config.kwargs
    assert kwargs["repo"] == tmp_path / "artifacts" / "spawn-1"
    # The spawn's setup changes the clone's request, never the orchestrator's own.
    assert orchestrator.chat_model.endpoint.config.kwargs == orchestrator_kwargs
    return kwargs


@pytest.mark.asyncio
async def test_a_spawned_light_codex_worker_gets_no_extra_writable_root_and_starts(
    tmp_path, monkeypatch
):
    from lionagi.providers.openai.codex import CodexCodeRequest

    kwargs = await _spawned_branch_kwargs(tmp_path, monkeypatch, "light")

    assert kwargs.get("add_dir", []) == []
    CodexCodeRequest(prompt="work", **kwargs)


@pytest.mark.asyncio
async def test_a_spawned_heavy_codex_worker_keeps_the_project_root(tmp_path, monkeypatch):
    kwargs = await _spawned_branch_kwargs(tmp_path, monkeypatch, DEFAULT_WORKLOAD)

    assert kwargs["add_dir"] == [str(tmp_path.resolve())]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "openai-codex", "Codex"])
async def test_a_spawned_heavy_codex_team_worker_gets_the_teams_directory(
    tmp_path, monkeypatch, provider
):
    """A reactive spawn in team mode gets the same teams-directory grant a planned
    worker gets, or it could not write to the team inbox."""
    from lionagi.cli import team as team_module

    teams_dir = tmp_path / "state" / "teams"
    teams_dir.mkdir(parents=True)
    monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

    kwargs = await _spawned_branch_kwargs(
        tmp_path, monkeypatch, DEFAULT_WORKLOAD, team_data={"id": "team-1"}, provider=provider
    )

    assert kwargs["add_dir"] == [str(tmp_path.resolve()), str(teams_dir.resolve())]


@pytest.mark.asyncio
@pytest.mark.parametrize("emitted_by", [None, "orchestrator"])
async def test_an_unassigned_spawn_of_a_light_orchestrator_runs_heavy(
    tmp_path, monkeypatch, emitted_by
):
    """A spawn with no assignee and no worker behind it, asked for by nobody or by the
    orchestrator itself, is cloned from the orchestrator's branch. The orchestrator's class
    confines only its own process, so the spawn runs as a bare worker: heavy, with the
    project root."""
    from lionagi.providers.openai.codex import CodexCodeRequest

    kwargs = await _spawned_branch_kwargs(
        tmp_path, monkeypatch, "light", from_orchestrator=True, emitted_by=emitted_by
    )

    assert "workload" not in kwargs
    assert kwargs["add_dir"] == [str(tmp_path.resolve())]
    assert CodexCodeRequest(prompt="work", **kwargs).workload == DEFAULT_WORKLOAD


@pytest.mark.asyncio
async def test_an_independent_spawn_a_light_worker_asks_for_stays_light(tmp_path, monkeypatch):
    """An independent request naming no assignee is cloned from the orchestrator's branch,
    here a heavy one. The worker that asked is light, so the spawn is light too: asking for
    an agent is not a way out of the asking worker's class."""
    from lionagi.providers.openai.codex import CodexCodeRequest

    kwargs = await _spawned_branch_kwargs(
        tmp_path, monkeypatch, DEFAULT_WORKLOAD, emitted_by="light"
    )

    assert kwargs.get("add_dir", []) == []
    assert CodexCodeRequest(prompt="work", **kwargs).workload == "light"


@pytest.mark.asyncio
async def test_an_independent_spawn_a_heavy_worker_asks_for_runs_heavy(tmp_path, monkeypatch):
    """The same request from a heavy worker under a light orchestrator runs heavy, with the
    project root: the orchestrator's class does not reach the agents its workers ask for."""
    from lionagi.providers.openai.codex import CodexCodeRequest

    kwargs = await _spawned_branch_kwargs(
        tmp_path, monkeypatch, "light", emitted_by=DEFAULT_WORKLOAD
    )

    assert kwargs["add_dir"] == [str(tmp_path.resolve())]
    assert CodexCodeRequest(prompt="work", **kwargs).workload == DEFAULT_WORKLOAD


@pytest.mark.asyncio
async def test_an_independent_spawn_a_light_worker_asks_for_on_another_engine_is_refused(
    tmp_path, monkeypatch
):
    """Under a Claude Code orchestrator, a light codex worker's independent request naming
    no assignee would run as a Claude Code agent, where light removes only the file-writing
    tools and Bash can still write anywhere. It is refused before any node exists."""
    expected: list = []
    message = (
        await _spawned_branch_kwargs(
            tmp_path,
            monkeypatch,
            DEFAULT_WORKLOAD,
            provider="claude_code",
            emitted_by="light",
            emitter_provider="codex",
            expected=expected,
            refuses=True,
        )
    )["refusal"]
    assert "orchestrator's engine (claude_code)" in message
    assert "runs on codex" in message
    # Refused before the node is built, so the run expects no worker for it.
    assert expected == []


@pytest.mark.asyncio
async def test_an_independent_spawn_a_light_worker_asks_for_under_an_api_orchestrator_is_refused(
    tmp_path, monkeypatch
):
    """Under an API orchestrator the request would run as an API call, which the light
    class does not confine at all, so it is refused as under another CLI engine."""
    refused = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="openai",
        emitted_by="light",
        emitter_provider="codex",
        refuses=True,
    )
    assert "orchestrator's engine (openai)" in refused["refusal"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "emitter_provider"),
    [
        ("claude_code", "claude"),
        ("claude_code", "claude-code"),
        ("claude", "claude_code"),
        ("codex", "openai-codex"),
    ],
)
async def test_an_independent_spawn_a_light_worker_asks_for_on_its_own_engine_is_not_refused(
    tmp_path, monkeypatch, provider, emitter_provider
):
    """Two spellings of one provider are one engine, so a light worker's independent
    request under an orchestrator on its own engine, spelled differently, runs light."""
    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider=provider,
        emitted_by="light",
        emitter_provider=emitter_provider,
    )

    assert kwargs["workload"] == "light"


@pytest.mark.asyncio
async def test_a_dependent_spawn_a_light_worker_asks_for_on_another_engine_runs_light_on_its_own(
    tmp_path, monkeypatch
):
    """A dependent request naming no assignee is cloned from the branch of the worker that
    asked, so under a Claude Code orchestrator a light codex worker's request runs as a
    light codex agent and is not refused."""
    from lionagi.providers.openai.codex import CodexCodeRequest

    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by="light",
        emitter_provider="codex",
        independent=False,
    )

    assert kwargs.get("add_dir", []) == []
    assert CodexCodeRequest(prompt="work", **kwargs).workload == "light"


@pytest.mark.asyncio
async def test_an_independent_spawn_a_heavy_worker_asks_for_on_another_engine_runs_heavy(
    tmp_path, monkeypatch
):
    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        emitter_provider="codex",
    )

    assert "workload" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [True, False])
async def test_an_agent_a_contained_claude_worker_asks_for_reads_no_settings(
    tmp_path, monkeypatch, independent
):
    """A request naming no assignee from a worker that reads no settings files: a dependent
    agent copies that worker's branch, an independent one copies the orchestrator's, which
    reads them, and is contained from what the request recorded. Either way asking for an
    agent is not a way back to the settings the worker runs without."""
    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        emitter_contained=True,
        independent=independent,
    )

    assert kwargs["setting_sources"] == ""
    assert kwargs["permission_mode"] == "dontAsk"
    assert kwargs["disallowed_tools"] == ["Agent", "Task"]


@pytest.mark.asyncio
async def test_an_agent_a_claude_worker_with_its_settings_asks_for_keeps_them(
    tmp_path, monkeypatch
):
    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
    )

    assert kwargs.get("setting_sources") is None
    assert kwargs.get("disallowed_tools") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role_contained", [False, True])
async def test_an_agent_a_contained_claude_worker_asks_for_by_role_reads_no_settings(
    tmp_path, monkeypatch, role_contained
):
    """A request naming a role is cloned from that role's branch, which reads the settings
    files unless the role is contained too, so naming a role is no way back to them either."""
    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        emitter_contained=True,
        assignee="reviewer",
        role_contained=role_contained,
    )

    assert kwargs["setting_sources"] == ""
    assert kwargs["permission_mode"] == "dontAsk"
    assert kwargs["disallowed_tools"] == ["Agent", "Task"]


@pytest.mark.asyncio
async def test_an_agent_a_claude_worker_with_its_settings_asks_for_by_role_keeps_them(
    tmp_path, monkeypatch
):
    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        assignee="reviewer",
    )

    assert kwargs.get("setting_sources") is None
    assert kwargs.get("disallowed_tools") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role_provider", ["gemini_code", "gemini-cli", "pi"])
async def test_a_contained_claude_worker_cannot_ask_for_a_role_on_a_cli_that_reads_its_own_settings(
    tmp_path, monkeypatch, role_provider
):
    """Only a Claude spawn can be contained and only a codex one runs with its user config
    left out, so a role on any other CLI would read the settings the asking worker runs
    without: the request is refused before the node is built."""
    expected: list = []
    result = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        emitter_contained=True,
        assignee="reviewer",
        role_provider=role_provider,
        expected=expected,
        refuses=True,
    )

    assert "role 'reviewer' runs on" in result["refusal"]
    assert "contain_user_settings" in result["refusal"]
    assert expected == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("role_provider", "refused"), [("gemini_code", True), ("codex", False)])
async def test_a_contained_claude_worker_asking_for_a_team_instance_is_held_to_its_role(
    tmp_path, monkeypatch, role_provider, refused
):
    """A team instance's name (reviewer-2) resolves to its role before the check, as the
    node builder resolves it, so naming the instance is no way around the refusal."""
    expected: list = []
    result = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        emitter_contained=True,
        assignee="reviewer",
        role_alias="reviewer-2",
        role_provider=role_provider,
        expected=expected,
        refuses=refused,
    )

    if refused:
        assert "role 'reviewer-2' runs on gemini_code" in result["refusal"]
        assert expected == []
    else:
        assert result["ignore_user_config"] is True


@pytest.mark.asyncio
async def test_a_claude_worker_with_its_settings_can_ask_for_a_role_on_another_cli(
    tmp_path, monkeypatch
):
    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        assignee="reviewer",
        role_provider="gemini_code",
    )

    assert kwargs.get("setting_sources") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role_provider", ["codex", "openai-codex", "Codex"])
async def test_a_contained_claude_worker_can_ask_for_a_codex_role(
    tmp_path, monkeypatch, role_provider
):
    kwargs = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        emitter_contained=True,
        assignee="reviewer",
        role_provider=role_provider,
    )

    assert kwargs["ignore_user_config"] is True
    assert kwargs.get("profile") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [True, False])
async def test_an_unnamed_agent_a_contained_claude_worker_asks_for_on_a_gemini_orchestrator(
    tmp_path, monkeypatch, independent
):
    """An independent agent is cloned from the orchestrator's branch, so on a gemini
    orchestrator it is refused; a dependent one is cloned from the asking worker's and stays
    contained."""
    result = await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="gemini_code",
        emitted_by=DEFAULT_WORKLOAD,
        emitter_provider="claude_code",
        emitter_contained=True,
        independent=independent,
        refuses=independent,
    )

    if independent:
        assert "the orchestrator runs on gemini_code" in result["refusal"]
        return
    assert result["setting_sources"] == ""
    assert result["permission_mode"] == "dontAsk"


@pytest.mark.asyncio
@pytest.mark.parametrize("emitter_contained", [False, True])
async def test_a_contained_spawn_with_no_server_set_is_handed_the_empty_set_whole(
    tmp_path, monkeypatch, emitter_contained
):
    """With no worker set resolved, a spawn keeps the servers its CLI discovers, unless it
    is contained: those would start while dontAsk refused their tools, so it is handed the
    empty set as its whole set."""
    handed: list = []
    await _spawned_branch_kwargs(
        tmp_path,
        monkeypatch,
        DEFAULT_WORKLOAD,
        provider="claude_code",
        emitted_by=DEFAULT_WORKLOAD,
        emitter_contained=emitter_contained,
        handed=handed,
    )

    assert handed == ([({}, True)] if emitter_contained else [(None, False)])


@pytest.mark.asyncio
async def test_a_spawned_claude_team_worker_gets_no_teams_directory(tmp_path, monkeypatch):
    """The teams-directory grant is a codex sandbox root; a claude worker, planned or
    spawned, keeps the project root alone."""
    from lionagi.cli import team as team_module

    teams_dir = tmp_path / "state" / "teams"
    teams_dir.mkdir(parents=True)
    monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

    kwargs = await _spawned_branch_kwargs(
        tmp_path, monkeypatch, DEFAULT_WORKLOAD, team_data={"id": "team-1"}, provider="claude_code"
    )

    assert kwargs["add_dir"] == [str(tmp_path.resolve())]


@pytest.mark.asyncio
async def test_a_spawned_light_codex_team_worker_is_refused_before_it_starts(tmp_path, monkeypatch):
    """Team mode needs the teams directory writable, which light codex cannot grant,
    for a reactive spawn as for a planned worker."""
    from lionagi._errors import CLIInputRefusedError
    from lionagi.cli import team as team_module
    from lionagi.providers.openai.codex import CodexCodeRequest

    teams_dir = tmp_path / "state" / "teams"
    teams_dir.mkdir(parents=True)
    monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

    kwargs = await _spawned_branch_kwargs(
        tmp_path, monkeypatch, "light", team_data={"id": "team-1"}
    )

    assert kwargs["add_dir"] == [str(teams_dir.resolve())]
    with pytest.raises(CLIInputRefusedError, match="add_dir"):
        CodexCodeRequest(prompt="work", **kwargs)
