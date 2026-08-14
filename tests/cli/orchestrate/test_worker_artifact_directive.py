# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Workers are told where their output goes, and the run reports where it went."""

from pathlib import Path
from types import SimpleNamespace

import pytest

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
        mp.setattr(orch, "build_imodel_from_spec", lambda *a, **k: imodel)
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


def _codex_worker_args(tmp_path, *, team_data=None) -> list[str]:
    from lionagi.providers.openai.codex import CodexCodeRequest

    _env, imodel, _built = _build_worker(
        tmp_path,
        agent_id="worker-1",
        role="worker",
        provider="codex",
        team_data=team_data,
    )
    request = CodexCodeRequest(prompt="work", **imodel.endpoint.config.kwargs)
    return request.as_cmd_args()


def test_codex_team_worker_grants_only_the_teams_directory(tmp_path, monkeypatch):
    from lionagi.cli import team as team_module

    teams_dir = tmp_path / "state" / "teams"
    teams_dir.mkdir(parents=True)
    monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

    args = _codex_worker_args(tmp_path, team_data={"id": "team-1"})

    assert _add_dir_values(args) == [str(tmp_path.resolve()), str(teams_dir.resolve())]


def test_codex_worker_without_team_does_not_grant_the_teams_directory(tmp_path, monkeypatch):
    from lionagi.cli import team as team_module

    teams_dir = tmp_path / "state" / "teams"
    teams_dir.mkdir(parents=True)
    monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

    args = _codex_worker_args(tmp_path)

    assert _add_dir_values(args) == [str(tmp_path.resolve())]


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
