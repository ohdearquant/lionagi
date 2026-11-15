"""Private observations have explicit owners and detached, secret-safe snapshots."""

import gc
import json
import weakref

import pytest

from lionagi.service._api_telemetry import (
    InputDecision,
    _record_response_usage,
    activate_request,
    capture_request,
    collect_api_turn,
    is_collecting,
    mark_dispatch,
    record_guard_refusal,
    record_selected_attempt,
    record_served_model,
)
from lionagi.service.accounts._pricing import CURRENT_REVISION
from lionhub.serve.cli._api_usage import APIUsageCollector


def _decision(**updates):
    fields = dict(
        kind="checked",
        counted=12,
        scale=2.0,
        estimate=24,
        cap=100,
        prompt_chars=31,
        provider="anthropic",
        model="requested-model",
    )
    return InputDecision(**(fields | updates))


def test_served_model_uses_the_recorded_request_owner():
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    with collect_api_turn(collector, 1):
        first = capture_request(_decision(), api_call_id="event-a")
        second = capture_request(_decision(), api_call_id="event-b")

    with activate_request(second):
        record_served_model(first, "served-first")
    with activate_request(first):
        record_served_model(second, "served-second")
        record_served_model(None, "unowned-response")

    snapshot = collector.close()
    record_served_model(first, "late-response")
    assert collector.snapshot() == snapshot
    assert [
        (row["model"], row["model_semantics"], row["served_model"]) for row in snapshot["requests"]
    ] == [
        ("requested-model", "requested", "served-first"),
        ("requested-model", "requested", "served-second"),
    ]


@pytest.mark.parametrize(
    ("request_tier", "response_tier", "expected_cost"),
    [
        pytest.param(None, None, None, id="omitted-tiers"),
        pytest.param(None, "standard", 64_500, id="omitted-request-standard-response"),
        pytest.param("standard_only", None, 64_500, id="standard-request"),
        pytest.param("auto", "standard", 64_500, id="standard-response"),
        pytest.param("standard_only", "priority", None, id="priority-response"),
        pytest.param("auto", None, None, id="unreported-auto"),
        pytest.param("priority", None, None, id="unreported-priority"),
    ],
)
def test_anthropic_usage_cost_accounts_for_service_tier(request_tier, response_tier, expected_cost):
    model = "claude-sonnet-5-5"
    payload = {
        "model": model,
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hello"}],
    }
    usage = {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5,
        "cache_creation": {"ephemeral_5m_input_tokens": 5},
    }
    if request_tier is not None:
        payload["service_tier"] = request_tier
    if response_tier is not None:
        usage["service_tier"] = response_tier

    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    with collect_api_turn(collector, 1):
        frame = capture_request(_decision(model=model), api_call_id="event-a")
    _record_response_usage(
        frame, usage, provider="anthropic", endpoint="messages", served_model=model, payload=payload
    )

    snapshot = collector.snapshot()
    (row,) = snapshot["requests"]
    assert (row["input_tokens"], row["output_tokens"]) == (10, 3)
    assert snapshot["totals"] == {"input_tokens": 10, "output_tokens": 3}
    assert row["estimated_cost_nusd"] == snapshot["estimated_cost_nusd"] == expected_cost
    assert snapshot["price_table_version"] == (
        CURRENT_REVISION.id if expected_cost is not None else None
    )


def test_detached_versioned_record_dedupes_actual_identities_and_seals():
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    with collect_api_turn(collector, 1):
        frame = capture_request(_decision(), api_call_id="event-a")
        mark_dispatch(frame)
        with activate_request(frame):
            record_selected_attempt(
                attempt_id="attempt-a", provider="anthropic", account_label="primary"
            )
            record_selected_attempt(
                attempt_id="attempt-a", provider="anthropic", account_label="primary"
            )
            record_selected_attempt(
                attempt_id="attempt-b", provider="anthropic", account_label="primary"
            )
        record_guard_refusal(call_id="tool-a")
        record_guard_refusal(call_id="tool-a")
    with collect_api_turn(collector, 2):
        capture_request(_decision(kind="refused"), api_call_id=None)
        record_guard_refusal(call_id="tool-b")
    snapshot = collector.snapshot()
    assert snapshot["schema_version"] == 1
    assert snapshot["run_id"] == "run-a"
    assert snapshot["branch_id"] == "branch-a"
    assert snapshot["guard_refusal_count"] == 2
    assert snapshot["guard_refusal_coverage"] == "act_policy_denial_v1"
    assert snapshot["account_labels"] == [{"provider": "anthropic", "label": "primary"}]
    first, refused = snapshot["requests"]
    assert first["input_decision"] == dict(
        kind="checked", estimate=24, cap=100, counted=12, scale=2.0
    )
    assert first["model_semantics"] == "requested"
    assert first["dispatch_started"] is True
    assert [a["attempt_id"] for a in first["selected_attempts"]] == ["attempt-a", "attempt-b"]
    assert refused["request_index"] == 2
    assert refused["turn_index"] == 2
    assert refused["dispatch_started"] is False
    snapshot["requests"][0]["selected_attempts"].clear()
    assert len(collector.snapshot()["requests"][0]["selected_attempts"]) == 2
    sealed = collector.close()
    with collect_api_turn(collector, 3), activate_request(frame):
        assert not is_collecting()
        assert capture_request(_decision(), api_call_id="late") is None
        mark_dispatch(frame)
        record_selected_attempt(attempt_id="late", provider="anthropic", account_label="late")
        record_guard_refusal(call_id="late")
    assert collector.snapshot() == sealed
    sealed["requests"].clear()
    assert len(collector.close()["requests"]) == 2


def test_missing_request_frame_shadows_outer_owner_and_never_uses_turn_fallback():
    a = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    b = APIUsageCollector(run_id="run-b", branch_id="branch-b")
    with collect_api_turn(a, 1):
        frame = capture_request(_decision(), api_call_id="event-a")
    with collect_api_turn(b, 1):
        record_selected_attempt(attempt_id="no-request", provider="anthropic", account_label="b")
        with activate_request(frame):
            with activate_request(None):
                record_selected_attempt(
                    attempt_id="unowned", provider="anthropic", account_label="b"
                )
            record_selected_attempt(attempt_id="owned-a", provider="anthropic", account_label="a")
        record_guard_refusal(call_id="tool-b")
    assert [x["attempt_id"] for x in a.snapshot()["requests"][0]["selected_attempts"]] == [
        "owned-a"
    ]
    assert b.snapshot()["requests"] == []
    assert a.snapshot()["guard_refusal_count"] == 0
    assert b.snapshot()["guard_refusal_count"] == 1


def test_request_frame_does_not_keep_completed_run_collector_alive():
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    ref = weakref.ref(collector)
    with collect_api_turn(collector, 1):
        frame = capture_request(_decision(), api_call_id="event-a")
    del collector
    gc.collect()
    assert ref() is None
    with activate_request(frame):
        mark_dispatch(frame)
        record_selected_attempt(attempt_id="late", provider="anthropic", account_label="primary")


def test_nested_turn_scope_restores_owner_after_exception():
    a = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    b = APIUsageCollector(run_id="run-b", branch_id="branch-b")
    with collect_api_turn(a, 1):
        with pytest.raises(RuntimeError), collect_api_turn(b, 2):
            capture_request(_decision(), api_call_id="b")
            raise RuntimeError("stop inner scope")
        capture_request(_decision(), api_call_id="a")
    assert not is_collecting()
    assert a.snapshot()["requests"][0]["api_call_id"] == "a"
    assert b.snapshot()["requests"][0]["api_call_id"] == "b"


def test_identifier_projection_drops_secret_prefixes_and_arbitrary_text():
    collector = APIUsageCollector(run_id="run-a", branch_id="branch-a")
    with collect_api_turn(collector, 1):
        frame = capture_request(
            _decision(provider="Bearer-secret-provider", model="sk-secret-model"),
            api_call_id="event-a",
        )
        with activate_request(frame):
            record_selected_attempt(
                attempt_id="attempt-a", provider="anthropic", account_label="secret-token"
            )
            record_selected_attempt(
                attempt_id="attempt-b",
                provider="anthropic",
                account_label="prompt text with spaces",
            )
    row = collector.snapshot()["requests"][0]
    assert row["provider"] is None and row["model"] is None
    assert [x["account_label"] for x in row["selected_attempts"]] == [None, None]
    assert collector.snapshot()["account_labels"] == []
    encoded = json.dumps(collector.snapshot())
    for secret in (
        "Bearer-secret-provider",
        "sk-secret-model",
        "secret-token",
        "prompt text with spaces",
    ):
        assert secret not in encoded


def test_failed_internal_sink_is_observational():
    class BrokenSink:
        closed = False

        def record_request(self, **kwargs):
            raise RuntimeError("observation failed")

        def record_guard_refusal(self, **kwargs):
            raise RuntimeError("observation failed")

    sink = BrokenSink()
    with collect_api_turn(sink, 1):
        assert capture_request(_decision(), api_call_id="event-a") is None
        record_guard_refusal(call_id="tool-a")

    class BrokenDispatchSink:
        closed = False

        def record_request(self, **kwargs):
            return 1

        def mark_dispatch(self, request_index):
            raise RuntimeError("observation failed")

        def record_selected_attempt(self, request_index, **kwargs):
            raise RuntimeError("observation failed")

    sink = BrokenDispatchSink()
    with collect_api_turn(sink, 1):
        frame = capture_request(_decision(), api_call_id="event-a")
        mark_dispatch(frame)
        with activate_request(frame):
            record_selected_attempt(
                attempt_id="attempt-a", provider="anthropic", account_label="primary"
            )
