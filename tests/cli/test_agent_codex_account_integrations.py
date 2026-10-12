# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A codex leg's account integrations follow the role it runs under: on a new
`li agent` run, on a resume (never the grant a stored branch may carry), and
for the orchestrator and each worker of an orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi.cli._providers import _parse_profile
from tests.cli.orchestrate.test_worker_artifact_directive import _build_worker
from tests.cli.test_agent_resume_model_override import _wire_agent_stubs

_GRANTING_ROLE = (
    "---\nmodel: codex/gpt-5.5\ncontain_account_integrations: true\n"
    "account_connectors: [gmail]\n---\n\nWork.\n"
)
_CONTAINED_ROLE = "---\nmodel: codex/gpt-5.5\ncontain_account_integrations: true\n---\n\nWork.\n"
_PLAIN_ROLE = "---\nmodel: codex/gpt-5.5\n---\n\nWork.\n"


def _use_role(monkeypatch, text: str) -> None:
    import lionagi.cli.agent as agent_mod

    profile = _parse_profile("role", text)
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)


def _capture_chat_kwargs(monkeypatch) -> list[dict]:
    from lionagi import Branch

    captured: list[dict] = []

    async def fake_operate(self, instruction=None, **kw):
        captured.append(dict(self.chat_model.endpoint.config.kwargs))
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    return captured


def _stored_codex_branch(tmp_path: Path) -> tuple[str, Path]:
    """A persisted codex branch whose stored config still carries a grant, as a
    row written by an older version or another writer could."""
    from lionagi import Branch, iModel

    branch = Branch(chat_model=iModel(provider="codex", model="gpt-5.5"))
    data = branch.to_dict()
    kwargs = data["chat_model"]["endpoint"]["config"]["kwargs"]
    kwargs["account_connectors"] = ["github"]
    kwargs["contain_account_integrations"] = True
    path = tmp_path / f"{branch.id}.json"
    path.write_text(json.dumps(data))
    return str(branch.id), path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (_GRANTING_ROLE, {"account_connectors": ["gmail"], "contain_account_integrations": True}),
        (_CONTAINED_ROLE, {"contain_account_integrations": True}),
        (_PLAIN_ROLE, {}),
    ],
)
async def test_a_new_run_takes_the_roles_settings(monkeypatch, tmp_path, role, expected):
    from lionagi.cli.agent import _run_agent

    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, role)
    captured = _capture_chat_kwargs(monkeypatch)

    await _run_agent("codex/gpt-5.5", "do the thing", agent_name="role")

    kwargs = captured[-1]
    for name in ("account_connectors", "contain_account_integrations"):
        assert kwargs.get(name) == expected.get(name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected_connectors", "expected_containment"),
    [(_GRANTING_ROLE, ["gmail"], True), (_PLAIN_ROLE, None, False)],
)
async def test_a_resume_runs_with_the_roles_grant_not_the_stored_one(
    monkeypatch, tmp_path, role, expected_connectors, expected_containment
):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.agent import _run_agent

    branch_id, path = _stored_codex_branch(tmp_path)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", path))
    _wire_agent_stubs(monkeypatch, tmp_path)
    _use_role(monkeypatch, role)
    captured = _capture_chat_kwargs(monkeypatch)

    await _run_agent(None, "continue", resume=branch_id, agent_name="role")

    assert captured[-1].get("account_connectors") == expected_connectors
    assert captured[-1].get("contain_account_integrations", False) is expected_containment


class _Reached(Exception):
    pass


@pytest.mark.asyncio
async def test_the_orchestrator_takes_its_roles_settings(monkeypatch):
    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli.orchestrate._orchestration import setup_orchestration

    seen: dict = {}

    def _capture(*a, **kw):
        seen.update(kw)
        raise _Reached

    monkeypatch.setattr(
        orch_mod, "load_agent_profile", lambda name, *a, **kw: _parse_profile(name, _GRANTING_ROLE)
    )
    monkeypatch.setattr(orch_mod, "build_imodel_from_spec", _capture)

    with pytest.raises(_Reached):
        await setup_orchestration(
            pattern_name="Fanout",
            model_spec=None,
            agent_name="orchestrator",
            save_dir=None,
            cwd=None,
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
        )

    assert seen["account_connectors"] == ("gmail",)
    assert seen["contain_account_integrations"] is True


_WORKER_PROFILE = SimpleNamespace(
    raw_body=True,
    system_prompt="You are a specialist.",
    effort=None,
    yolo=False,
    fast_mode=False,
    khive_injection=None,
    account_connectors=("gmail",),
    contain_account_integrations=True,
)


@pytest.mark.parametrize(
    ("bare", "connectors", "contained"),
    [(False, ("gmail",), True), (True, (), False)],
)
def test_a_worker_takes_its_roles_settings_and_a_bare_worker_none(
    tmp_path, bare, connectors, contained
):
    _env, _imodel, built = _build_worker(
        tmp_path,
        agent_id="specialist",
        role="specialist",
        bare=bare,
        profile=_WORKER_PROFILE,
        provider="codex",
    )
    kwargs = built["imodel_kwargs"]
    assert kwargs["account_connectors"] == connectors
    assert kwargs["contain_account_integrations"] is contained
