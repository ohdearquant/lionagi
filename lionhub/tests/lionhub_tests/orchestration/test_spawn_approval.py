# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Model builder approval controls."

from __future__ import annotations

from lionhub.orchestration.spawn import PROVIDER_BYPASS_KWARGS, build_chat_model
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


def test_build_chat_model_bypass_applies_bypass_kwargs(monkeypatch):
    """build_chat_model with bypass=True injects PROVIDER_BYPASS_KWARGS for codex."""
    import lionhub.orchestration.spawn as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_chat_model(
        "codex", "gpt-5.3-codex-spark", yolo=False, verbose=False, theme=None, bypass=True
    )

    assert len(captor.captures) == 1
    kwargs = captor.captures[0]
    expected = PROVIDER_BYPASS_KWARGS["codex"]
    for k, v in expected.items():
        assert kwargs.get(k) == v, f"Expected {k}={v!r} in kwargs, got {kwargs.get(k)!r}"


def test_build_chat_model_bypass_takes_precedence_over_yolo(monkeypatch):
    """When both bypass and yolo are True, bypass wins (bypass_approvals, not full_auto)."""
    import lionhub.orchestration.spawn as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_chat_model(
        "codex", "gpt-5.3-codex-spark", yolo=True, verbose=False, theme=None, bypass=True
    )

    assert len(captor.captures) == 1
    kwargs = captor.captures[0]
    # bypass_approvals from PROVIDER_BYPASS_KWARGS (not full_auto from PROVIDER_YOLO_KWARGS)
    assert kwargs.get("bypass_approvals") is True
    assert "full_auto" not in kwargs, "full_auto (yolo) must not override bypass"


def test_build_chat_model_no_bypass_no_yolo_returns_str_for_codex(tmp_path, monkeypatch):
    """Without any flags AND without a global codex effort, build_chat_model
    returns a plain spec string (no iModel). The empty CODEX_HOME is the
    premise, stated here rather than inherited from the suite's isolation: a
    global model_reasoning_effort would rightly turn this into an iModel."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    result = build_chat_model("codex", "gpt-5.3-codex-spark", yolo=False, verbose=False, theme=None)
    assert isinstance(result, str)
    assert result == "codex/gpt-5.3-codex-spark"


def test_build_chat_model_bypass_claude_applies_permission_mode(monkeypatch):
    """bypass for claude provider injects permission_mode=bypassPermissions."""
    import lionhub.orchestration.spawn as pmod
    from lionagi.testing import IModelKwargCaptor

    captor = IModelKwargCaptor.fresh()
    monkeypatch.setattr(pmod, "iModel", captor)

    build_chat_model("claude", "sonnet", yolo=False, verbose=False, theme=None, bypass=True)

    assert len(captor.captures) == 1
    kwargs = captor.captures[0]
    assert kwargs.get("permission_mode") == "bypassPermissions"
