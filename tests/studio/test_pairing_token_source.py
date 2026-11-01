# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""POST /api/studio/pair says whether the bearer it returns is the launch bearer
or a configured token, so a page can keep only the one that stops working when
the daemon restarts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod

_PAIR = "/api/studio/pair"
_HOSTED = "https://lion-studio.khive.ai"
_HEADERS = {"Host": "127.0.0.1:8765", "Origin": _HOSTED}
_CONFIGURED = "configured-operator-token"


@pytest.fixture()
def fresh_launch(monkeypatch):
    """A process with no launch bearer and no codes yet."""
    import lionagi.studio.launch_access as launch_access

    monkeypatch.setattr(launch_access, "_bearer", None)
    monkeypatch.setattr(launch_access, "_codes", {})
    monkeypatch.setattr(launch_access, "_capabilities", {}, raising=False)
    monkeypatch.setattr(launch_access, "_launch_codes", {}, raising=False)
    monkeypatch.setattr(launch_access, "_launch_origins", set())
    return launch_access


@pytest.fixture(autouse=True)
def _no_token(monkeypatch, fresh_launch):
    for name in ("LIONAGI_STUDIO_AUTH_TOKEN", "CORS_ORIGINS", "LIONAGI_STUDIO_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Factory: a client on a fresh app whose state database lives in tmp_path."""
    import lionagi.studio.app as app_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", tmp_path / "state.db")

    def _factory() -> TestClient:
        return TestClient(
            app_mod.create_app(),
            raise_server_exceptions=False,
            base_url="http://127.0.0.1:8765",
        )

    return _factory


@pytest.mark.integration
class TestPairTokenSource:
    """The answer names where its token came from."""

    def test_no_configured_token_answers_the_launch_bearer_as_launch(
        self, make_client, fresh_launch
    ):
        client = make_client()
        code = fresh_launch.mint_pairing_code()

        response = client.post(_PAIR, json={"code": code}, headers=_HEADERS)
        body = response.json()

        assert response.status_code == 200
        assert body["token"] == fresh_launch.launch_bearer()
        assert body["token_source"] == "launch"

    def test_a_configured_token_is_answered_as_configured(
        self, make_client, fresh_launch, monkeypatch
    ):
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", _CONFIGURED)
        client = make_client()
        code = fresh_launch.mint_pairing_code()

        response = client.post(_PAIR, json={"code": code}, headers=_HEADERS)
        body = response.json()

        assert response.status_code == 200
        assert body["token"] == _CONFIGURED
        assert body["token_source"] == "configured"

    def test_an_empty_configured_token_counts_as_none(self, make_client, fresh_launch, monkeypatch):
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "")
        client = make_client()
        code = fresh_launch.mint_pairing_code()

        response = client.post(_PAIR, json={"code": code}, headers=_HEADERS)
        body = response.json()

        assert response.status_code == 200
        assert body["token"] == fresh_launch.launch_bearer()
        assert body["token_source"] == "launch"
