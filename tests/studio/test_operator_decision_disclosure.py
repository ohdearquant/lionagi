# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Operator decision surfaces say who can actually decide in open mode.

With no ``LIONAGI_STUDIO_AUTH_TOKEN`` the daemon admits any request that carries
no browser provenance headers, so any local process that can reach the listener
can allow or deny a pending Operator proposal. Several texts described that
decision as an authenticated human decision, which reads as isolation the daemon
does not provide. These pin the corrected contract on the surfaces a reader
meets: the two decision operations' OpenAPI descriptions, the no-token startup
warning, the guide and internals reference, and the Operator module texts that
describe the decision path and the automatically allowed tools.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

# The clause each surface must carry, from the contract sentence about who can
# decide with no token.
ANY_LOCAL_CALLER_CLAUSE = "any process that can reach the daemon's listener"
OTHER_LOCAL_ACCOUNT_CLAUSE = "including programs run by other local accounts"
SAFEGUARD_CLAUSE = "a workflow safeguard, not isolation from local programs"
SHARED_MACHINE_CLAUSE = "set a token on a shared machine"
DECISION_CLAUSES = (ANY_LOCAL_CALLER_CLAUSE, OTHER_LOCAL_ACCOUNT_CLAUSE)
FULL_CONTRACT_CLAUSES = (*DECISION_CLAUSES, SAFEGUARD_CLAUSE, SHARED_MACHINE_CLAUSE)
# The third sentence, about tools that can call the daemon back.
HTTP_TOOL_CLAUSE = "can make HTTP requests to the daemon"
SELF_APPROVAL_CLAUSE = "approve its own proposals"

# Both Operator proposal-decision routes: allow and deny are the same admission.
_DECISION_PATHS = (
    "/api/operator/conversations/{conversation_id}/proposals/{proposal_id}/confirm",
    "/api/operator/conversations/{conversation_id}/proposals/{proposal_id}/decision",
)


def _normalized(text: str) -> str:
    """One whitespace-normalized line, so a clause is matched across a wrap."""
    return " ".join(text.split())


def _make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    import lionagi.studio.app as app_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", tmp_path / "state.db")
    app = app_mod.create_app()
    return TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765")


@pytest.fixture()
def make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)

    def _factory() -> TestClient:
        return _make_client(monkeypatch, tmp_path)

    return _factory


def test_the_decision_operations_disclose_who_can_decide_without_a_token(make_client):
    """The rendered API description of both decision routes carries the
    contract, so a caller reading the schema is not told a human authenticated."""
    client = make_client()
    document = client.get("/openapi.json")
    assert document.status_code == 200
    paths = document.json()["paths"]

    for path in _DECISION_PATHS:
        operation = paths[path]["post"]
        description = _normalized(operation.get("description") or "")
        for clause in DECISION_CLAUSES:
            assert clause in description, (
                f"{path} description does not state the no-token contract: missing {clause!r}"
            )


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "0.0.0.0"])
def test_the_no_token_startup_warning_states_that_any_local_caller_can_decide(
    monkeypatch: pytest.MonkeyPatch, caplog, bind_host: str
) -> None:
    """Whichever no-token warning the bind host selects, it states who can
    decide a pending proposal: any process that can reach the listener."""
    import lionagi.studio.app as app_mod

    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)

    with caplog.at_level("WARNING", logger=app_mod.__name__):
        app_mod._emit_startup_warnings()

    warning = _normalized(caplog.text)
    for clause in FULL_CONTRACT_CLAUSES:
        assert clause in warning, f"no-token startup warning on host {bind_host} omits: {clause!r}"


def test_the_operator_modules_do_not_call_the_decision_authenticated() -> None:
    """The module texts describe what the daemon does: the decision is recorded
    by the decision routes, which in open mode admit any local caller."""
    from lionagi.studio.operator import application_mcp, permission_mcp

    for module in (application_mcp, permission_mcp):
        doc = _normalized(module.__doc__ or "")
        assert "authenticated human-decision" not in doc, f"{module.__name__} still claims it"
        for clause in DECISION_CLAUSES:
            assert clause in doc, f"{module.__name__} omits: {clause!r}"


def test_the_extra_tool_config_text_warns_against_auto_allowing_an_http_tool() -> None:
    """The text that tells a reader how automatically allowed tools are
    configured, and the docstring of the function that reads that config, both
    carry the warning about a tool that can call the daemon back."""
    from lionagi.studio.operator import engine

    texts = {
        "_operator_extra_mcp docstring": _normalized(engine._operator_extra_mcp.__doc__ or ""),
        "build_operator_branch allowed-tools text": _normalized(
            inspect.getsource(engine.build_operator_branch)
        ),
    }
    for where, text in texts.items():
        for clause in (HTTP_TOOL_CLAUSE, SELF_APPROVAL_CLAUSE):
            assert clause in text, f"{where} omits: {clause!r}"


@pytest.mark.parametrize(
    "page",
    [
        "docs/guides/studio-getting-started.md",
        "docs/internals/studio.md",
    ],
)
def test_the_published_pages_state_the_no_token_decision_contract(page: str) -> None:
    """The page that describes running without a token states the contract, so
    a reader of the guide or the reference is not left with the other version."""
    text = _normalized((ROOT / page).read_text())
    for clause in (*FULL_CONTRACT_CLAUSES, HTTP_TOOL_CLAUSE, SELF_APPROVAL_CLAUSE):
        assert clause in text, f"{page} omits: {clause!r}"
