# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Operator's backend-served model catalog and the provider/model/effort
selection it validates before a turn is ever accepted -- see lionagi/studio/operator/catalog.py.
"""

from __future__ import annotations

import pytest

from lionagi.studio.operator.catalog import (
    OperatorSelectionError,
    catalog_entries,
    resolve_selection,
)
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.store import OperatorStore, OperatorValidationError


class ScriptedEngine:
    async def _stream(self, _turn):
        yield

    def stream(self, turn):
        return self._stream(turn)


def _patch_state_db(monkeypatch: pytest.MonkeyPatch, path) -> None:
    import lionagi.cli._runs as runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(sessions_mod, "_DB", str(path))
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", path.parent / "runs")


# ── catalog_entries() ───────────────────────────────────────────────────────


def test_catalog_entries_cover_claude_codex_and_gemini_with_a_fable_entry():
    entries = catalog_entries()
    by_id = {entry["id"]: entry for entry in entries}
    assert by_id["claude-fable-5"]["provider"] == "claude_code"
    assert "codex" in {entry["provider"] for entry in entries}
    assert "gemini_code" in {entry["provider"] for entry in entries}
    # Claude has no none/minimal/ultra tier.
    assert set(by_id["sonnet"]["efforts"]) == {"low", "medium", "high", "xhigh", "max"}
    # Codex additionally accepts none/minimal/ultra.
    codex_entry = next(e for e in entries if e["provider"] == "codex")
    assert {"none", "minimal", "ultra"} <= set(codex_entry["efforts"])
    # gemini-code folds effort into the model name -- only 3 tiers are meaningful.
    gemini_entry = next(e for e in entries if e["provider"] == "gemini_code")
    assert set(gemini_entry["efforts"]) == {"low", "medium", "high"}


# ── resolve_selection() ─────────────────────────────────────────────────────


def test_resolve_selection_rejects_an_unknown_model():
    with pytest.raises(OperatorSelectionError, match="Unknown Operator model"):
        resolve_selection(provider=None, model="not-a-real-model", effort=None)


def test_resolve_selection_rejects_an_unknown_provider():
    with pytest.raises(OperatorSelectionError, match="Unknown Operator provider"):
        resolve_selection(provider="not-a-real-provider", model=None, effort=None)


def test_resolve_selection_rejects_a_provider_model_mismatch():
    with pytest.raises(OperatorSelectionError, match="belongs to provider"):
        resolve_selection(provider="codex", model="sonnet", effort=None)


def test_resolve_selection_rejects_an_effort_the_providers_cannot_accept():
    # gemini-code only accepts low/medium/high (folded into the --model name).
    with pytest.raises(OperatorSelectionError, match="does not accept effort"):
        resolve_selection(provider=None, model="gemini-3.6-flash", effort="xhigh")
    # codex has no "ultra-plus" tier at all.
    with pytest.raises(OperatorSelectionError, match="does not accept effort"):
        resolve_selection(provider="codex", model=None, effort="not-a-real-effort")


def test_resolve_selection_rejects_effort_with_no_provider_or_model():
    with pytest.raises(OperatorSelectionError, match="requires a provider or model"):
        resolve_selection(provider=None, model=None, effort="high")


def test_resolve_selection_accepts_a_valid_codex_effort_and_infers_provider():
    provider, model, effort = resolve_selection(
        provider=None, model="gpt-5.3-codex", effort="xhigh"
    )
    assert (provider, model, effort) == ("codex", "gpt-5.3-codex", "xhigh")


def test_resolve_selection_leaves_unspecified_fields_none_for_backward_compat():
    # A turn that specifies nothing must resolve to (None, None, None) so the
    # env-var default path in build_operator_branch is untouched.
    assert resolve_selection(provider=None, model=None, effort=None) == (None, None, None)


# ── coordinator.submit() rejects a bad selection before the turn is accepted ─


@pytest.mark.asyncio
async def test_submit_rejects_an_unknown_model_before_accepting_the_turn(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await coordinator.startup()
    snapshot = await coordinator.create_conversation(title="Bad model")
    cid = snapshot["conversation"]["id"]

    with pytest.raises(OperatorValidationError):
        await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
            model="not-a-real-model",
        )
    # Rejected before acceptance: no active turn was created.
    conversation = await coordinator.store.get_conversation(cid)
    assert conversation["activeRequestId"] is None
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_submit_rejects_a_provider_model_mismatch(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await coordinator.startup()
    snapshot = await coordinator.create_conversation(title="Mismatch")
    cid = snapshot["conversation"]["id"]

    with pytest.raises(OperatorValidationError):
        await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
            model="sonnet",
            provider="codex",
        )
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_submit_rejects_an_effort_the_selected_provider_cannot_honor(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=ScriptedEngine)
    await coordinator.startup()
    snapshot = await coordinator.create_conversation(title="Bad effort")
    cid = snapshot["conversation"]["id"]

    with pytest.raises(OperatorValidationError):
        await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
            model="gemini-3.5-flash",
            effort="xhigh",
        )
    await coordinator.shutdown()


# ── build_operator_branch: provider now travels with the turn ──────────────


def test_build_operator_branch_drives_the_selected_provider_not_just_the_env_default(
    tmp_path, monkeypatch
):
    """The structural bug this feature fixes: selecting a non-Claude model in
    the UI used to still hand it to the env-var-pinned provider (always
    claude_code by default). The turn's own provider must now win."""
    from lionagi.studio.operator.engine import build_operator_branch
    from lionagi.studio.operator.types import OperatorEngineTurn

    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_PROVIDER", "claude_code")

    async def request_permission(*_args):
        raise AssertionError("branch construction cannot request permission")

    branch = build_operator_branch(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="use codex please",
            context={},
            history=(),
            request_permission=request_permission,
            store_path=str(tmp_path / "state.db"),
            provider="codex",
            model="gpt-5.3-codex",
        )
    )
    assert branch.chat_model.endpoint.config.provider == "codex"
    assert branch.chat_model.endpoint.config.kwargs["model"] == "gpt-5.3-codex"
    # Codex's CLI request model has no Claude-specific permission/MCP wiring.
    assert "mcp_servers" not in branch.chat_model.endpoint.config.kwargs
    assert "permission_prompt_tool_name" not in branch.chat_model.endpoint.config.kwargs


def test_build_operator_branch_defaults_to_the_env_var_when_the_turn_specifies_nothing(
    tmp_path, monkeypatch
):
    """Backward compat: a turn with no provider/model still resolves through
    the environment defaults exactly as before this feature."""
    from lionagi.studio.operator.engine import build_operator_branch
    from lionagi.studio.operator.types import OperatorEngineTurn

    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_PROVIDER", "claude_code")
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_MODEL", "opus")

    async def request_permission(*_args):
        raise AssertionError("branch construction cannot request permission")

    branch = build_operator_branch(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="hi",
            context={},
            history=(),
            request_permission=request_permission,
            store_path=str(tmp_path / "state.db"),
        )
    )
    assert branch.chat_model.endpoint.config.provider == "claude_code"


def test_build_operator_branch_selects_the_fable_model_through_the_claude_provider(
    tmp_path, monkeypatch
):
    from lionagi.studio.operator.engine import build_operator_branch
    from lionagi.studio.operator.types import OperatorEngineTurn

    monkeypatch.delenv("LIONAGI_STUDIO_OPERATOR_PROVIDER", raising=False)

    async def request_permission(*_args):
        raise AssertionError("branch construction cannot request permission")

    branch = build_operator_branch(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="hi",
            context={},
            history=(),
            request_permission=request_permission,
            store_path=str(tmp_path / "state.db"),
            model="claude-fable-5",
        )
    )
    # Fable is served through the same claude_code provider path as sonnet/opus/haiku.
    assert branch.chat_model.endpoint.config.provider == "claude_code"
    assert "studio_permission" in branch.chat_model.endpoint.config.kwargs["mcp_servers"]


def test_build_operator_branch_folds_codex_effort_into_the_reasoning_effort_kwarg(tmp_path):
    from lionagi.studio.operator.engine import build_operator_branch
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("branch construction cannot request permission")

    branch = build_operator_branch(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="hi",
            context={},
            history=(),
            request_permission=request_permission,
            store_path=str(tmp_path / "state.db"),
            provider="codex",
            model="gpt-5.3-codex",
            effort="high",
        )
    )
    assert branch.chat_model.endpoint.config.kwargs["reasoning_effort"] == "high"


def test_build_operator_branch_folds_gemini_effort_into_the_model_name(tmp_path):
    from lionagi.studio.operator.engine import build_operator_branch
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("branch construction cannot request permission")

    branch = build_operator_branch(
        OperatorEngineTurn(
            conversation_id="conversation",
            request_id="request",
            instruction="hi",
            context={},
            history=(),
            request_permission=request_permission,
            store_path=str(tmp_path / "state.db"),
            provider="gemini_code",
            model="gemini-3.5-flash",
            effort="high",
        )
    )
    # gemini-code has no effort kwarg -- it is folded into the resolved model name.
    assert "reasoning_effort" not in branch.chat_model.endpoint.config.kwargs
    assert "effort" not in branch.chat_model.endpoint.config.kwargs
    assert branch.chat_model.endpoint.config.kwargs["model"] == "Gemini 3.5 Flash (High)"


# ── store: provider/model/effort persistence ────────────────────────────────


@pytest.mark.asyncio
async def test_select_provider_model_resets_the_session_when_provider_changes(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    await store.select_provider_model(conversation_id, provider="claude_code", model="sonnet")
    await store.set_provider_session_id(conversation_id, "session-1")
    conversation = await store.get_conversation(conversation_id)
    assert conversation["providerSessionId"] == "session-1"

    await store.select_provider_model(conversation_id, provider="codex", model="gpt-5.3-codex")
    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.3-codex"
    assert conversation["providerSessionId"] is None


@pytest.mark.asyncio
async def test_select_provider_model_leaves_an_unspecified_field_untouched(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    await store.select_provider_model(conversation_id, provider="codex", model="gpt-5.3-codex")
    await store.set_provider_session_id(conversation_id, "session-1")

    # Selecting only a model on a conversation that already pinned a provider
    # must not clear that provider pin.
    await store.select_provider_model(conversation_id, model="gpt-5.4")
    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.4"
    assert conversation["providerSessionId"] is None


@pytest.mark.asyncio
async def test_submit_turn_persists_effort_per_turn_not_per_conversation(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        conversation_id,
        instruction="hi",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
        effort="xhigh",
    )
    turn = await store.get_turn(accepted["requestId"])
    assert turn["effort"] == "xhigh"


@pytest.mark.asyncio
async def test_provider_and_effort_columns_are_added_to_a_preexisting_store(tmp_path):
    """Same additive-migration contract as provider_session_id/provider_model:
    a pre-existing StateDB file predates the `provider` and `effort` columns."""
    import aiosqlite

    db_path = tmp_path / "state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE studio_operator_conversations ("
            "id TEXT PRIMARY KEY, project TEXT, title TEXT, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "next_sequence INTEGER NOT NULL DEFAULT 1, active_request_id TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "archived_at REAL, deleted_at REAL)"
        )
        await db.commit()

    store = OperatorStore(db_path)
    conversation_id = (await store.create_conversation())["id"]
    await store.select_provider_model(conversation_id, provider="codex", model="gpt-5.3-codex")
    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.3-codex"


@pytest.mark.asyncio
async def test_the_turn_handed_to_the_engine_keeps_the_selected_provider_and_effort(
    tmp_path, monkeypatch
):
    """The coordinator rebuilds the engine turn twice on the way to streaming.
    Both rebuilds must carry the whole selection, not the subset whoever wrote
    the call site happened to list.

    The built-in engine reads provider and effort off the Branch it was handed
    rather than off the turn, so a dropped field is invisible until a different
    engine is supplied through engine_factory -- which is exactly what this does.
    """
    import asyncio

    captured: list = []

    class CapturingEngine:
        async def _stream(self, turn):
            captured.append(turn)
            return
            yield  # pragma: no cover - makes this an async generator

        def stream(self, turn):
            return self._stream(turn)

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=CapturingEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="Selection"))["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="run it",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
        model="sonnet",
        effort="high",
    )

    for _ in range(200):
        if captured:
            break
        await asyncio.sleep(0.02)
    assert captured, "the engine was never handed a turn"
    turn = captured[0]
    assert turn.model == "sonnet"
    assert turn.provider == "claude_code"
    assert turn.effort == "high"
    # run_dir is what that last rebuild exists to attach; if it is missing the
    # assertions above would pass on a turn that never went through it.
    assert turn.run_dir is not None
    await coordinator.shutdown()
