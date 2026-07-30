# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Every Operator route that touches conversation state refuses an unauthenticated caller.

The failing configuration is the one with NO Studio bearer configured: the
app-wide middleware deliberately lets every request through in that mode, so a
route with no per-request authorization of its own is reachable by anyone who
can open a socket. These tests exercise that mode directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.store import OperatorStore

# Anything but 127.0.0.1/localhost is rejected by the Host-header guard with a
# 400 before auth runs, which would make every assertion below measure the
# wrong thing.
BASE_URL = "http://127.0.0.1:8765"


def _patch_state_db(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import lionagi.cli._runs as runs_mod
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(sessions_mod, "_DB", str(path))
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", path.parent / "runs")


class _IdleEngine:
    async def _stream(self, _turn):
        return
        yield  # pragma: no cover

    def stream(self, turn):
        return self._stream(turn)


@pytest.fixture()
async def no_credential_operator(tmp_path, monkeypatch):
    """A live app plus coordinator with BOTH credential variables cleared."""
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing
    from lionagi.studio.security import clear_captured_studio_credentials

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    clear_captured_studio_credentials()
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)

    app = create_app()
    assert app.state.studio_auth_token is None
    assert app.state.studio_operator_credential_origin is None
    assert "LIONAGI_STUDIO_AUTH_TOKEN" not in os.environ
    assert "LIONAGI_STUDIO_HUMAN_TOKEN" not in os.environ

    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_IdleEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    await coordinator.startup()
    try:
        yield app, coordinator
    finally:
        await coordinator.shutdown()
        clear_captured_studio_credentials()


def _client(app):
    httpx = pytest.importorskip("httpx")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


@pytest.mark.asyncio
async def test_no_token_mode_refuses_unauthenticated_create(no_credential_operator):
    app, coordinator = no_credential_operator
    before = await coordinator.store.list_conversations(limit=500)

    async with _client(app) as client:
        created = await client.post(
            "/api/operator/conversations", json={"title": "should not exist"}
        )

    assert created.status_code == 403, created.text
    assert "credential" in str(created.json()["detail"])
    # The refusal has to be a refusal, not a 403 issued after the write.
    after = await coordinator.store.list_conversations(limit=500)
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_no_token_mode_refuses_unauthenticated_delete(no_credential_operator):
    app, coordinator = no_credential_operator
    cid = (await coordinator.create_conversation(title="keep me"))["conversation"]["id"]

    async with _client(app) as client:
        deleted = await client.delete(f"/api/operator/conversations/{cid}")

    assert deleted.status_code == 403, deleted.text
    # The conversation survived: the reported end-to-end exploit deleted it and
    # a follow-up read returned 404.
    assert (await coordinator.store.get_conversation(cid))["id"] == cid


@pytest.mark.asyncio
async def test_no_token_mode_refuses_unauthenticated_cancel(no_credential_operator):
    app, coordinator = no_credential_operator
    cid = (await coordinator.create_conversation(title="cancel target"))["conversation"]["id"]
    accepted = await coordinator.store.submit_turn(
        cid,
        instruction="in flight",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    request_id = accepted["requestId"]

    async with _client(app) as client:
        cancelled = await client.post(
            f"/api/operator/conversations/{cid}/requests/{request_id}/cancel",
            headers={"content-type": "application/json"},
        )

    assert cancelled.status_code == 403, cancelled.text
    conversation = await coordinator.store.get_conversation(cid)
    assert conversation["activeRequestId"] == request_id


@pytest.mark.asyncio
async def test_no_token_mode_refuses_every_conversation_state_route(no_credential_operator):
    """The whole surface, not just the three routes named in the report."""
    app, coordinator = no_credential_operator
    cid = (await coordinator.create_conversation(title="surface"))["conversation"]["id"]

    async with _client(app) as client:
        responses = {
            "list": await client.get("/api/operator/conversations"),
            "snapshot": await client.get(f"/api/operator/conversations/{cid}"),
            "create": await client.post("/api/operator/conversations", json={}),
            "delete": await client.delete(f"/api/operator/conversations/{cid}"),
            "stream": await client.get(f"/api/operator/conversations/{cid}/stream"),
            "submit": await client.post(
                f"/api/operator/conversations/{cid}/turns",
                json={
                    "instruction": "no",
                    "context": {"space": "mission", "route": "/", "filters": {}},
                    "expectedLastSequence": 0,
                },
            ),
            "cancel": await client.post(
                f"/api/operator/conversations/{cid}/requests/r1/cancel",
                headers={"content-type": "application/json"},
            ),
            # Bodies are valid on purpose: FastAPI validates before the handler
            # runs, so an invalid body would score a 422 and prove nothing.
            "ack": await client.post(
                f"/api/operator/conversations/{cid}/effects/e1/ack",
                json={"status": "applied"},
            ),
            "confirm": await client.post(
                f"/api/operator/conversations/{cid}/proposals/p1/confirm",
                json={"expectedCommandHash": "a" * 64},
            ),
            "decision": await client.post(
                f"/api/operator/conversations/{cid}/proposals/p1/decision",
                json={"decision": "allow", "expectedCommandHash": "a" * 64},
            ),
        }

    assert {name: r.status_code for name, r in responses.items()} == {
        name: 403 for name in responses
    }


@pytest.mark.asyncio
async def test_bearer_configured_but_absent_header_is_refused_and_correct_header_works(
    tmp_path, monkeypatch
):
    """The generated-credential mode still works, and a wrong bearer does not."""
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing
    from lionagi.studio.security import (
        capture_studio_credentials,
        clear_captured_studio_credentials,
    )

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    clear_captured_studio_credentials()
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)
    token = capture_studio_credentials(generate_human=True)
    assert token

    app = create_app()
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_IdleEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="guarded"))["conversation"]["id"]

    async with _client(app) as client:
        assert (await client.post("/api/operator/conversations", json={})).status_code == 401
        assert (await client.delete(f"/api/operator/conversations/{cid}")).status_code == 401
        wrong = await client.post(
            "/api/operator/conversations",
            json={},
            headers={"authorization": "Bearer not-the-token"},
        )
        assert wrong.status_code == 401
        good = await client.post(
            "/api/operator/conversations",
            json={"title": "authorized"},
            headers={"authorization": f"Bearer {token}"},
        )
        assert good.status_code == 200
        removed = await client.delete(
            f"/api/operator/conversations/{cid}",
            headers={"authorization": f"Bearer {token}"},
        )
        assert removed.status_code == 200

    await coordinator.shutdown()
    clear_captured_studio_credentials()


@pytest.mark.asyncio
async def test_environment_credential_cannot_authorize_conversation_state(tmp_path, monkeypatch):
    """An environment-derived bearer opens the API but not Operator state.

    Its value is recoverable by process inspection, so it is not the human
    boundary — the same rule submit already applied, now applied everywhere.
    """
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing
    from lionagi.studio.security import clear_captured_studio_credentials

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    clear_captured_studio_credentials()
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "environment-secret")
    monkeypatch.delenv("LIONAGI_STUDIO_HUMAN_TOKEN", raising=False)

    app = create_app()
    assert app.state.studio_operator_credential_origin == "environment"
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_IdleEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    await coordinator.startup()
    cid = (await coordinator.create_conversation(title="env"))["conversation"]["id"]

    headers = {"authorization": "Bearer environment-secret"}
    async with _client(app) as client:
        # The app-wide bearer gate is satisfied — these 403s come from the route.
        assert (await client.get("/openapi.json", headers=headers)).status_code == 200
        assert (
            await client.post("/api/operator/conversations", json={}, headers=headers)
        ).status_code == 403
        assert (
            await client.delete(f"/api/operator/conversations/{cid}", headers=headers)
        ).status_code == 403

    assert (await coordinator.store.get_conversation(cid))["id"] == cid
    await coordinator.shutdown()
    clear_captured_studio_credentials()


def test_studio_bearer_middleware_uses_constant_time_compare():
    """The app-wide gate routes its bearer check through `compare_digest`.

    This is a structural check, not a timing measurement: it asserts the
    comparison the middleware performs, and that the comparator behaves on the
    edge cases `!=` used to absorb. A wall-clock timing assay on an in-process
    ASGI app would not discriminate.
    """
    import inspect

    from lionagi.studio import app as app_module

    # A non-ASCII header is a mismatch, not a TypeError escaping as a 500.
    assert app_module._bearer_matches("Bearer ÿ", "secret") is False
    assert app_module._bearer_matches("Bearer secret", "secret") is True
    assert app_module._bearer_matches("", "secret") is False
    assert "hmac.compare_digest" in inspect.getsource(app_module._bearer_matches)

    source = inspect.getsource(app_module.create_app)
    assert "_bearer_matches(presented, token)" in source
    assert 'request.headers.get("authorization") != f"Bearer {token}"' not in source
