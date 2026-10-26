# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Selected-account receipts come from real reservations after key resolution."""

import json

import pytest

from lionagi.cli._api_usage import APIUsageCollector
from lionagi.service._api_telemetry import collect_api_turn
from lionagi.service.accounts._types import AccountRoutingError
from lionagi.service.resilience import RetryConfig
from tests.service.accounts import test_routed_endpoint as routed


@pytest.fixture(autouse=True)
def reset_router():
    yield
    routed._cleanup_router()


@pytest.mark.asyncio
async def test_real_retry_attempts_belong_to_one_constructed_request(monkeypatch, tmp_path):
    path = routed._install_accounts(monkeypatch, tmp_path)
    model, transport = routed._model(monkeypatch, [routed.StubResponse(429), routed._response()])
    model.endpoint.retry_config = RetryConfig(max_retries=1, base_delay=0.01, jitter=False)
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")

    with collect_api_turn(collector, 1):
        result = await model.invoke(**routed.REQUEST)

    assert result.response["model"] == routed.MODEL
    assert len(transport.calls) == 2
    ledger = routed._attempts(path)
    usage = collector.close()
    (row,) = usage["requests"]
    assert row["dispatch_started"] is True
    assert row["selected_attempts"] == [
        {"attempt_id": item[0], "provider": "anthropic", "account_label": item[1]}
        for item in ledger
    ]
    assert len(ledger) == 2 and ledger[0][0] != ledger[1][0]
    assert usage["account_labels"] == [{"provider": "anthropic", "label": "primary"}]
    assert routed.CANARY not in json.dumps(usage)
    assert routed.KEY_NAME not in json.dumps(usage)


@pytest.mark.asyncio
async def test_failed_key_resolution_has_no_selected_attempt(monkeypatch, tmp_path):
    path = routed._install_accounts(monkeypatch, tmp_path, key="bad")
    model, transport = routed._model(monkeypatch, [])
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")

    with collect_api_turn(collector, 1):
        result = await model.invoke(**routed.REQUEST)

    assert isinstance(result.execution.error, AccountRoutingError)
    (row,) = collector.close()["requests"]
    assert row["dispatch_started"] is True
    assert row["selected_attempts"] == []
    assert transport.calls == []
    assert len(routed._attempts(path)) == 1


@pytest.mark.asyncio
async def test_actual_key_overlap_suppresses_an_otherwise_valid_account_label(
    monkeypatch, tmp_path
):
    label = "primary-account"
    routed._install_accounts(
        monkeypatch,
        tmp_path,
        accounts=[{"name": label, "key_env": routed.KEY_NAME, "budget_usd": "20"}],
        key=label,
    )
    model, transport = routed._model(monkeypatch, [routed._response()])
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    with collect_api_turn(collector, 1):
        await model.invoke(**routed.REQUEST)

    usage = collector.close()
    (row,) = usage["requests"]
    (attempt,) = row["selected_attempts"]
    assert attempt["provider"] == "anthropic"
    assert attempt["account_label"] is None
    assert usage["account_labels"] == []
    assert label not in json.dumps(usage)
    assert transport.calls[0]["headers"]["x-api-key"] == label


@pytest.mark.asyncio
async def test_direct_endpoint_selection_does_not_borrow_an_ambient_turn(monkeypatch, tmp_path):
    routed._install_accounts(monkeypatch, tmp_path)
    model, transport = routed._model(monkeypatch, [routed._response()])
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    with collect_api_turn(collector, 1):
        await model.endpoint.call(routed.REQUEST)
    assert len(transport.calls) == 1
    assert collector.close()["requests"] == []


@pytest.mark.asyncio
async def test_selected_attempt_sink_failure_preserves_real_response(monkeypatch, tmp_path):
    routed._install_accounts(monkeypatch, tmp_path)
    model, transport = routed._model(monkeypatch, [routed._response()])
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")

    def broken(*args, **kwargs):
        raise RuntimeError("observation unavailable")

    monkeypatch.setattr(collector, "record_selected_attempt", broken)
    with collect_api_turn(collector, 1):
        result = await model.invoke(**routed.REQUEST)
    assert result.response["model"] == routed.MODEL
    assert len(transport.calls) == 1
