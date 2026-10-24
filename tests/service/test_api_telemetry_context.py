"""Private observations have explicit owners and detached, secret-safe snapshots."""

import gc
import json
import weakref

import pytest

from lionagi.cli._api_usage import APIUsageCollector
from lionagi.service._api_telemetry import (
    InputDecision,
    activate_request,
    capture_request,
    collect_api_turn,
    is_collecting,
    mark_dispatch,
    record_guard_refusal,
    record_selected_attempt,
)


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
