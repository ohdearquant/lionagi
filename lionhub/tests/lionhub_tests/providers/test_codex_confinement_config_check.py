# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The sandbox-off refusal reads the codex config whatever mode the request itself sets.

Under a permission profile no ``--sandbox`` flag is emitted, so a request's own mode
(``full_auto``, ``sandbox``) does not displace what the config sets. A config whose
``sandbox_mode`` is ``danger-full-access`` is refused for every request, including the ones that
name a mode of their own. The launch gate refuses the same config afterwards, so an end-to-end
launch cannot tell the two checks apart; these tests call the refusal on its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi._errors import CLIInputRefusedError
from lionhub.providers.openai import codex as codex_module
from lionhub.providers.openai.codex import CodexCodeRequest


@pytest.fixture
def sandbox_on(monkeypatch):
    """The child sandbox on with the default policy (the suite switches it off by default)."""
    import lionhub.agent.child_sandbox as child_sandbox

    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {})


def _home_with(tmp_path: Path, config: str) -> dict[str, str]:
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(config)
    return {"CODEX_HOME": str(home)}


def _plain(tmp_path: Path, env: dict[str, str], **kw):
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, env=env, **kw)
    return codex_module._plain_request(request)


REQUEST_MODES = [
    pytest.param({}, id="no-mode"),
    pytest.param({"full_auto": True}, id="full_auto"),
    pytest.param({"sandbox": "workspace-write"}, id="sandbox-workspace-write"),
    pytest.param({"sandbox": "read-only"}, id="sandbox-read-only"),
]


@pytest.mark.parametrize("kw", REQUEST_MODES)
def test_a_config_turning_the_sandbox_off_is_refused_whatever_mode_the_request_sets(
    tmp_path, sandbox_on, kw
):
    env = _home_with(tmp_path, 'sandbox_mode = "danger-full-access"\n')
    plain = _plain(tmp_path, env, **kw)
    with pytest.raises(CLIInputRefusedError, match="danger-full-access"):
        codex_module._refuse_sandbox_off_while_confined(plain, tmp_path)


@pytest.mark.parametrize("kw", REQUEST_MODES)
def test_a_config_that_leaves_the_sandbox_on_is_not_refused(tmp_path, sandbox_on, kw):
    env = _home_with(tmp_path, 'sandbox_mode = "workspace-write"\n')
    plain = _plain(tmp_path, env, **kw)
    assert codex_module._refuse_sandbox_off_while_confined(plain, tmp_path) is None
