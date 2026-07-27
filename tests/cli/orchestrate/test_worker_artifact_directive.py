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

# ── Half 1: the prompt names the directory ───────────────────────────────────

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


# ── build_worker_branch names exactly the directory it launches the worker in ──


def test_worker_prompt_names_the_cwd_it_is_launched_with(tmp_path):
    """The named directory and the `repo` kwarg are the same value.

    This is the property that makes the named path writable at all: the
    file-editing tool refuses absolute paths outside the working directory.
    """
    import asyncio

    from lionagi.cli.orchestrate import _orchestration as orch

    built: dict = {}

    class _Endpoint:
        def __init__(self):
            self.config = SimpleNamespace(kwargs={}, provider="claude_code")

    class _IModel:
        is_cli = True

        def __init__(self):
            self.endpoint = _Endpoint()

    imodel = _IModel()

    def _fake_build_imodel(*args, **kwargs):
        return imodel

    class _Branch:
        def __init__(self, **kw):
            built.update(kw)
            self.name = kw.get("name")
            self.id = "b1"

    run = SimpleNamespace(
        agent_artifact_dir=lambda aid: tmp_path / "artifacts" / aid,
    )
    env = orch.OrchestrationEnv(
        run=run,
        session=SimpleNamespace(include_branches=lambda b: None),
        orc_branch=SimpleNamespace(id="orc"),
        builder=None,
        orc_profile=None,
        default_model_spec="claude_code/sonnet",
        bare=True,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=str(tmp_path),
    )

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(orch, "build_imodel_from_spec", _fake_build_imodel)
        mp.setattr(orch, "Branch", _Branch)
        mp.setattr(orch, "_hand_mcp_servers", lambda *a, **k: None)
        mp.setattr(
            orch,
            "_resolve_worker_model_spec",
            lambda env, role, override: ("claude_code/sonnet", None, None),
        )
        mp.setattr(orch, "team_worker_system", lambda *a, **k: "")
        asyncio.run(orch.build_worker_branch(env, agent_id="researcher", role="researcher"))
    finally:
        mp.undo()

    repo = imodel.endpoint.config.kwargs["repo"]
    assert Path(repo) == tmp_path / "artifacts" / "researcher"
    assert f"ARTIFACT DIRECTORY: {repo}" in built["system"]
    # And it is registered for the end-of-run report.
    assert env.worker_artifact_dirs["researcher"] == Path(repo)


# ── Half 2: the run reports where each worker actually wrote ─────────────────


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


def test_a_missing_directory_reads_as_nothing_written_not_as_success(tmp_path):
    entries = collect_worker_artifacts(_env_with_dirs({"a": tmp_path / "never-created"}))
    assert entries[0]["agent_id"] == "a"
    assert entries[0]["files"] == []


def test_report_is_emitted_and_recorded_by_finalize(tmp_path, monkeypatch, caplog):
    from lionagi.cli.orchestrate._orchestration import finalize_orchestration

    d = tmp_path / "researcher"
    d.mkdir()
    (d / "out.md").write_text("x")

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
