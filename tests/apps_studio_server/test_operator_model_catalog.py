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
    model_effort_choices,
    resolve_selection,
)
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.store import (
    OperatorConflictError,
    OperatorStore,
    OperatorValidationError,
)


class ScriptedEngine:
    async def _stream(self, _turn):
        yield

    def stream(self, turn):
        return self._stream(turn)


def _patch_state_db(monkeypatch: pytest.MonkeyPatch, path) -> None:
    import lionagi.cli._runs as runs_mod
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", path.parent / "runs")


# ── catalog_entries() ───────────────────────────────────────────────────────


def test_catalog_entries_cover_claude_codex_and_gemini_with_a_fable_entry():
    entries = catalog_entries()
    by_id = {entry["id"]: entry for entry in entries}
    assert by_id["claude-fable-5"]["provider"] == "claude_code"
    assert "codex" in {entry["provider"] for entry in entries}
    assert "gemini_code" in {entry["provider"] for entry in entries}
    # Efforts are per model, not per provider: the catalog offers a level only
    # when the request path will send that level unchanged. Claude has no
    # none/minimal/ultra tier at all, and xhigh survives on Opus alone.
    assert set(by_id["sonnet"]["efforts"]) == {"low", "medium", "high", "max"}
    assert set(by_id["opus"]["efforts"]) == {"low", "medium", "high", "xhigh", "max"}
    # Codex additionally accepts none/minimal; max and ultra clamp to xhigh on
    # every Codex model this catalog lists, so they are not offered.
    codex_entry = next(e for e in entries if e["provider"] == "codex")
    assert {"none", "minimal"} <= set(codex_entry["efforts"])
    assert {"max", "ultra"}.isdisjoint(codex_entry["efforts"])
    # gemini-code folds effort into the model name -- only 3 tiers are
    # meaningful, and Pro has no Medium tier.
    assert set(by_id["gemini-3.5-flash"]["efforts"]) == {"low", "medium", "high"}
    assert set(by_id["gemini-3.1-pro"]["efforts"]) == {"low", "high"}


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


# ── per-model effort ceilings ───────────────────────────────────────────────


def test_the_catalog_never_offers_an_effort_the_request_path_would_change():
    """The whole point of the per-model list: every offered level must survive
    the clamp that runs on the way to the provider. This walks the catalog
    rather than spot-checking, so a model added later cannot quietly offer a
    level that gets rewritten."""
    from lionagi.service.providers import (
        _GEMINI_EFFORT_CLAMP,
        _clamp_claude_effort,
        _clamp_codex_effort,
        _clamp_gemini_effort,
    )
    from lionagi.studio.operator.catalog import OPERATOR_MODEL_CATALOG

    checked = 0
    for spec in OPERATOR_MODEL_CATALOG:
        for effort in model_effort_choices(spec.id):
            checked += 1
            if spec.provider == "claude_code":
                assert _clamp_claude_effort(effort, spec.id) == effort, (spec.id, effort)
            elif spec.provider == "codex":
                assert _clamp_codex_effort(effort, spec.id) == effort, (spec.id, effort)
            else:
                is_pro = "pro" in spec.id
                assert _clamp_gemini_effort(effort, is_pro) == _GEMINI_EFFORT_CLAMP[effort], (
                    spec.id,
                    effort,
                )
    assert checked > 20, "sanity: the walk must actually have examined the catalog"


def test_a_clamped_effort_is_rejected_rather_than_silently_downgraded():
    """The three measured cases where the provider used to receive a level the
    operator never chose."""
    for model, effort in (("sonnet", "xhigh"), ("gpt-5.4", "ultra"), ("gemini-3.1-pro", "medium")):
        with pytest.raises(OperatorSelectionError, match="does not accept effort"):
            resolve_selection(provider=None, model=model, effort=effort)


def test_a_model_that_does_honor_the_level_still_accepts_it():
    """Rejecting must not become rejecting everything: each of the three above
    has a sibling for which the same level is honored."""
    assert resolve_selection(provider=None, model="opus", effort="xhigh") == (
        "claude_code",
        "opus",
        "xhigh",
    )
    assert resolve_selection(provider=None, model="gpt-5.4", effort="xhigh") == (
        "codex",
        "gpt-5.4",
        "xhigh",
    )
    assert resolve_selection(provider=None, model="gemini-3.5-flash", effort="medium") == (
        "gemini_code",
        "gemini-3.5-flash",
        "medium",
    )


# ── availability preflight is per selected provider ─────────────────────────


@pytest.mark.asyncio
async def test_a_codex_turn_is_not_refused_because_claude_is_the_env_default(tmp_path, monkeypatch):
    """The availability check runs before the branch is built, so it has to ask
    the turn which provider it selected. Reading the environment there refuses a
    perfectly runnable Codex or Gemini turn whenever the machine happens to have
    no Claude CLI installed.
    """
    import lionagi.providers.anthropic.claude_code as claude_mod
    from lionagi.studio.operator.engine import (
        BranchOperatorEngine,
        OperatorProviderUnavailableError,
    )
    from lionagi.studio.operator.types import OperatorEngineTurn

    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_PROVIDER", "claude_code")
    monkeypatch.setattr(claude_mod, "CLAUDE_CLI", None)

    def _turn(provider: str | None, model: str | None) -> OperatorEngineTurn:
        return OperatorEngineTurn(
            conversation_id="c1",
            request_id="r1",
            instruction="do it",
            context={"space": "mission", "route": "/", "filters": {}},
            history=[],
            request_permission=None,
            store_path=str(tmp_path / "state.db"),
            provider=provider,
            model=model,
        )

    engine = BranchOperatorEngine()

    # The control: a turn that selects nothing still falls back to the
    # environment, so the missing Claude CLI is still reported. Without this the
    # test would pass just as well against a preflight that never fires.
    with pytest.raises(OperatorProviderUnavailableError):
        stream = engine.stream(_turn(None, None))
        await stream.__anext__()

    # The regression: a selected Codex turn must get past the preflight. It
    # fails later for an unrelated reason (no codex binary, no network) or
    # succeeds; what matters is that it is not the Claude-unavailable refusal.
    stream = engine.stream(_turn("codex", "gpt-5.3-codex"))
    try:
        await stream.__anext__()
    except OperatorProviderUnavailableError as exc:  # pragma: no cover - the defect
        raise AssertionError(f"a codex turn was refused for a Claude reason: {exc}") from exc
    except StopAsyncIteration:
        pass
    except Exception:
        pass
    finally:
        await stream.aclose()


# ── clearing a conversation's pin ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_clearing_returns_a_pinned_conversation_to_the_default(tmp_path):
    """Omitting a model means "keep the pin", so there has to be a separate way
    to say "drop it" -- otherwise a conversation can never go back to the
    daemon's own default once it has been pinned once.
    """
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    await store.select_provider_model(conversation_id, provider="codex", model="gpt-5.4")
    await store.set_provider_session_id(conversation_id, "session-1")

    await store.clear_provider_model(conversation_id)
    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] is None
    assert conversation["providerModel"] is None
    # The session belonged to the pair that is gone.
    assert conversation["providerSessionId"] is None


@pytest.mark.asyncio
async def test_clearing_an_unpinned_conversation_keeps_its_session(tmp_path):
    """A client that cannot see the store may repeat the clear. Dropping the
    session is a consequence of a pin changing, so a repeat must not keep
    discarding sessions that nothing is invalidating."""
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    await store.set_provider_session_id(conversation_id, "session-1")

    await store.clear_provider_model(conversation_id)
    conversation = await store.get_conversation(conversation_id)
    assert conversation["providerSessionId"] == "session-1"


@pytest.mark.asyncio
async def test_a_cleared_turn_reaches_the_engine_with_no_selection(tmp_path, monkeypatch):
    """End to end through the coordinator: the pin is dropped before the turn is
    accepted, so this turn is the first one to run without it rather than the
    last one to run with it."""
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
    cid = (await coordinator.create_conversation(title="Clearing"))["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="pin it",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
        model="gpt-5.4",
    )
    for _ in range(200):
        if captured:
            break
        await asyncio.sleep(0.02)
    assert captured and captured[0].model == "gpt-5.4"
    for _ in range(400):
        conversation = await coordinator.store.get_conversation(cid)
        if conversation["activeRequestId"] is None:
            break
        await asyncio.sleep(0.02)
    assert conversation["activeRequestId"] is None, "the pinned turn never finished"
    assert conversation["providerModel"] == "gpt-5.4"

    captured.clear()
    await coordinator.submit(
        cid,
        instruction="unpin it",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=int(conversation["nextSequence"]) - 1,
        clear_selection=True,
    )
    for _ in range(200):
        if captured:
            break
        await asyncio.sleep(0.02)
    assert captured, "the engine was never handed the cleared turn"
    assert captured[0].model is None
    assert captured[0].provider is None
    await coordinator.shutdown()


def test_a_clear_request_cannot_also_carry_a_pin():
    """Clearing and pinning in one turn is a contradiction, and silently
    letting one win would make the wire ambiguous."""
    import pydantic

    from lionagi.studio.operator.types import OperatorTurnRequest

    body = {
        "instruction": "hi",
        "context": {"space": "mission", "route": "/", "filters": {}},
        "expectedLastSequence": 0,
    }
    # The control: the same body without a pin is accepted.
    assert OperatorTurnRequest.model_validate({**body, "clearSelection": True}).clear_selection

    with pytest.raises(pydantic.ValidationError, match="cannot be combined"):
        OperatorTurnRequest.model_validate({**body, "clearSelection": True, "model": "sonnet"})


# ── a refused turn must not move the pin ────────────────────────────────────


async def _pin_and_reserve(store, *, provider="codex", model="gpt-5.4"):
    """A conversation pinned to a pair, with a session, and a turn already running."""
    conversation_id = (await store.create_conversation())["id"]
    await store.select_provider_model(conversation_id, provider=provider, model=model)
    await store.set_provider_session_id(conversation_id, "session-1")
    await store.submit_turn(
        conversation_id,
        instruction="the turn already running",
        context={},
        expected_last_sequence=0,
    )
    return conversation_id


@pytest.mark.asyncio
async def test_a_clear_refused_for_an_active_turn_leaves_the_pin_alone(tmp_path):
    """The 409 and the pin change are decided against one snapshot of the row.

    A second client reversing a selection it cannot see gets refused, and the
    conversation it did not get to run must be exactly as it was: a rejected
    request that still drops the pin and the resumable session hands the
    already-accepted turn a different provider than the one it was accepted
    under.
    """
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = await _pin_and_reserve(store)

    with pytest.raises(OperatorConflictError):
        await store.submit_turn(
            conversation_id,
            instruction="reverse it",
            context={},
            expected_last_sequence=1,
            clear_selection=True,
        )

    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.4"
    assert conversation["providerSessionId"] == "session-1"


@pytest.mark.asyncio
async def test_a_selection_refused_for_an_active_turn_leaves_the_pin_alone(tmp_path):
    """Same row state, the other direction: a changed pin is refused whole."""
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = await _pin_and_reserve(store)

    with pytest.raises(OperatorConflictError):
        await store.submit_turn(
            conversation_id,
            instruction="switch provider",
            context={},
            expected_last_sequence=1,
            select_provider="claude_code",
            select_model="sonnet",
        )

    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.4"
    assert conversation["providerSessionId"] == "session-1"


@pytest.mark.asyncio
async def test_a_selection_refused_for_a_stale_cursor_leaves_the_pin_alone(tmp_path):
    """The cursor check rejects after the active-turn check, so it needs its own
    case: a guard placed between the two would pass the tests above and still
    let a stale-context rejection move the pin."""
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    await store.select_provider_model(conversation_id, provider="codex", model="gpt-5.4")
    await store.set_provider_session_id(conversation_id, "session-1")

    with pytest.raises(OperatorConflictError):
        await store.submit_turn(
            conversation_id,
            instruction="submitted against an old cursor",
            context={},
            expected_last_sequence=41,
            clear_selection=True,
        )

    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.4"
    assert conversation["providerSessionId"] == "session-1"


@pytest.mark.asyncio
async def test_an_accepted_turn_still_applies_the_selection(tmp_path):
    """The control for all three above: moving the write inside the transaction
    must not stop it happening. A guard that simply never applied the pin would
    pass every rejection case."""
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    await store.select_provider_model(conversation_id, provider="codex", model="gpt-5.4")
    await store.set_provider_session_id(conversation_id, "session-1")

    await store.submit_turn(
        conversation_id,
        instruction="accepted",
        context={},
        expected_last_sequence=0,
        clear_selection=True,
    )

    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] is None
    assert conversation["providerModel"] is None
    assert conversation["providerSessionId"] is None


@pytest.mark.asyncio
async def test_a_refused_submit_does_not_move_the_pin_through_the_coordinator(
    tmp_path, monkeypatch
):
    """The discriminating case for the whole selection-with-turn contract.

    The store-level cases above cannot catch the defect this replaces: inside
    ``submit_turn`` every rejection rolls its transaction back, so ordering
    within it is not observable. The defect was that the coordinator committed
    the pin through a separate store call before asking whether the turn was
    acceptable at all, so the rejection could not undo it. Only a test that
    goes through the coordinator sees that.
    """
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="Pinned"))["conversation"]["id"]
    await store.select_provider_model(cid, provider="codex", model="gpt-5.4")
    await store.set_provider_session_id(cid, "session-1")
    # Reserve the turn directly: the conversation now has one in flight.
    await store.submit_turn(cid, instruction="running", context={}, expected_last_sequence=0)

    with pytest.raises(OperatorConflictError):
        await coordinator.submit(
            cid,
            instruction="reverse the pin while a turn is running",
            context={},
            expected_last_sequence=1,
            clear_selection=True,
        )

    conversation = await store.get_conversation(cid)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.4"
    assert conversation["providerSessionId"] == "session-1"


@pytest.mark.asyncio
async def test_a_first_explicit_selection_drops_the_default_providers_session(tmp_path):
    """A NULL pin does not mean "no session was created under a pair".

    An unpinned conversation runs on whatever the environment resolves to, and
    the session it gets belongs to that pair. Pinning a different pair for the
    first time therefore invalidates the session exactly as changing an existing
    pin does. Treating NULL as "unset, so nothing to invalidate" hands the new
    provider a session id that another provider created, which the engine passes
    straight through as its resume argument.
    """
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    await store.set_provider_session_id(conversation_id, "default-provider-session")

    await store.submit_turn(
        conversation_id,
        instruction="first explicit selection",
        context={},
        expected_last_sequence=0,
        select_provider="gemini_code",
        select_model="gemini-3.5-flash",
    )

    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] == "gemini_code"
    assert conversation["providerModel"] == "gemini-3.5-flash"
    assert conversation["providerSessionId"] is None


@pytest.mark.asyncio
async def test_resending_the_pin_already_stored_keeps_the_session(tmp_path):
    """The control for the invalidation rule above.

    A composer submits its selection on every turn, so the common case is the
    stored pair being sent back unchanged. If that counted as a change, every
    turn would discard the provider session and the conversation would never
    resume anything. An invalidation rule that clears on NULL must still not
    clear on equal.
    """
    store = OperatorStore(tmp_path / "state.db")
    conversation_id = (await store.create_conversation())["id"]
    await store.select_provider_model(conversation_id, provider="codex", model="gpt-5.4")
    await store.set_provider_session_id(conversation_id, "session-1")

    await store.submit_turn(
        conversation_id,
        instruction="same selection again",
        context={},
        expected_last_sequence=0,
        select_provider="codex",
        select_model="gpt-5.4",
    )

    conversation = await store.get_conversation(conversation_id)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.4"
    assert conversation["providerSessionId"] == "session-1"


@pytest.mark.asyncio
async def test_a_refused_selection_does_not_move_the_pin_through_the_coordinator(
    tmp_path, monkeypatch
):
    """The clear path and the select path both have to survive a refusal.

    A regression covering only ``clearSelection`` passes against an
    implementation that moved just the clear inside the accepting transaction
    and left an ordinary model selection written ahead of it.
    """
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=ScriptedEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="Pinned"))["conversation"]["id"]
    await store.select_provider_model(cid, provider="codex", model="gpt-5.4")
    await store.set_provider_session_id(cid, "session-1")
    await store.submit_turn(cid, instruction="running", context={}, expected_last_sequence=0)

    with pytest.raises(OperatorConflictError):
        await coordinator.submit(
            cid,
            instruction="switch model while a turn is running",
            context={},
            expected_last_sequence=1,
            model="sonnet",
        )

    conversation = await store.get_conversation(cid)
    assert conversation["provider"] == "codex"
    assert conversation["providerModel"] == "gpt-5.4"
    assert conversation["providerSessionId"] == "session-1"


# ── a provider session belongs to what ran, not to what was pinned ──────────


class _CapturingEngine:
    """Records the turn it is handed instead of streaming it.

    The built-in engine reads provider and model off the Branch, so whether a
    resume was passed is only observable from the turn itself.
    """

    captured: list = []

    async def _stream(self, turn):
        type(self).captured.append(turn)
        return
        yield  # pragma: no cover - makes this an async generator

    def stream(self, turn):
        return self._stream(turn)


async def _run_one_turn(coordinator, cid: str):
    """Submit one turn and return only once that turn has finished.

    Both halves matter. The cursor is read here rather than by the caller
    because a turn's own completion writes bump ``nextSequence``, so a cursor
    read while a turn is still running is already stale by the time the next
    submit checks it. And waiting for ``activeRequestId`` to clear is what makes
    the read at the top of the next call safe: being handed to the engine is the
    middle of a turn, not the end of one.
    """
    import asyncio

    conversation = await coordinator.store.get_conversation(cid)
    before = len(_CapturingEngine.captured)
    await coordinator.submit(
        cid,
        instruction="go",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=int(conversation["nextSequence"]) - 1,
    )
    for _ in range(200):
        if len(_CapturingEngine.captured) > before:
            break
        await asyncio.sleep(0.02)
    assert len(_CapturingEngine.captured) > before, "the engine was never handed a turn"
    for _ in range(400):
        conversation = await coordinator.store.get_conversation(cid)
        if conversation["activeRequestId"] is None:
            break
        await asyncio.sleep(0.02)
    assert conversation["activeRequestId"] is None, "the turn never finished"
    return _CapturingEngine.captured[-1]


@pytest.mark.asyncio
async def test_an_unpinned_conversation_stops_resuming_when_the_default_model_moves(
    tmp_path, monkeypatch
):
    """The case the pin check could never see.

    An unpinned conversation has no pin to change, so nothing used to compare
    anything: it ran on whatever the environment resolved to, kept the session
    that pair produced, and resumed it under a different pair after the default
    moved.
    """
    _CapturingEngine.captured = []
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_MODEL", "sonnet")

    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_CapturingEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="Unpinned"))["conversation"]["id"]

    await _run_one_turn(coordinator, cid)
    conversation = await coordinator.store.get_conversation(cid)
    # The conversation is genuinely unpinned, which is the whole premise: if a
    # pin were set the existing check would already cover this.
    assert conversation["providerModel"] is None
    assert conversation["resolvedModel"] == "sonnet"
    await coordinator.store.set_provider_session_id(cid, "session-from-sonnet")

    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_MODEL", "opus")
    turn = await _run_one_turn(coordinator, cid)

    assert turn.provider_session_id is None
    assert (await coordinator.store.get_conversation(cid))["resolvedModel"] == "opus"
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_an_unpinned_conversation_keeps_resuming_while_the_default_holds_still(
    tmp_path, monkeypatch
):
    """The over-firing arm. A check that drops the session every turn also
    passes the test above, and it would silently end resuming for everyone."""
    _CapturingEngine.captured = []
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_MODEL", "sonnet")

    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_CapturingEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="Steady"))["conversation"]["id"]

    await _run_one_turn(coordinator, cid)
    await coordinator.store.set_provider_session_id(cid, "session-from-sonnet")

    turn = await _run_one_turn(coordinator, cid)

    assert turn.provider_session_id == "session-from-sonnet"
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_a_conversation_that_predates_this_check_keeps_its_session(tmp_path):
    """A stored resolution of NULL means no turn ever recorded one, which is not
    evidence that anything changed.

    Every conversation alive when this ships is in that state, so reading NULL
    as a mismatch would drop every live session once, at upgrade, and would look
    like the check working.

    The grace is one turn wide, and that is what the last assertion is for. The
    same call records the resolution it just let through, so the conversation is
    governed from its next turn onward. Without that write the exemption would
    never end, and the conversations it exempted permanently would be exactly the
    long-lived ones this check exists for.
    """
    store = OperatorStore(tmp_path / "state.db")
    cid = (await store.create_conversation())["id"]
    await store.set_provider_session_id(cid, "session-from-before")
    assert (await store.get_conversation(cid))["resolvedModel"] is None

    kept = await store.claim_resolved_pair(cid, provider="claude_code", model="sonnet")

    assert kept == "session-from-before"
    conversation = await store.get_conversation(cid)
    assert conversation["providerSessionId"] == "session-from-before"
    assert (conversation["resolvedProvider"], conversation["resolvedModel"]) == (
        "claude_code",
        "sonnet",
    )
    # Governed from here: the next turn on a different pair drops the session.
    assert await store.claim_resolved_pair(cid, provider="claude_code", model="opus") is None


@pytest.mark.asyncio
async def test_the_resolved_columns_are_added_to_a_preexisting_store(tmp_path):
    """Same additive-migration contract the provider and effort columns carry."""
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
    cid = (await store.create_conversation())["id"]
    assert await store.claim_resolved_pair(cid, provider="codex", model="gpt-5.3-codex") is None
    conversation = await store.get_conversation(cid)
    assert conversation["resolvedProvider"] == "codex"
    assert conversation["resolvedModel"] == "gpt-5.3-codex"
