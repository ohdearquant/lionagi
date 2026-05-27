# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.runtime.cost — CostEntry, PricingTable, CostLedger."""

from __future__ import annotations

import threading
import time

import pytest

from lionagi.runtime.cost import (
    BudgetExceededError,
    CostEntry,
    CostLedger,
    PricingTable,
)

# ===========================================================================
# PricingTable — default rates
# ===========================================================================


class TestPricingTableDefaults:
    def test_default_table_has_openai_models(self) -> None:
        table = PricingTable()
        assert "gpt-4.1-mini" in table.known_models()
        assert "gpt-4.1" in table.known_models()

    def test_default_table_has_anthropic_models(self) -> None:
        table = PricingTable()
        assert "claude-sonnet-4-5-20250514" in table.known_models()
        assert "claude-opus-4-20250514" in table.known_models()

    def test_known_models_sorted(self) -> None:
        table = PricingTable()
        models = table.known_models()
        assert models == sorted(models)


# ===========================================================================
# PricingTable — compute_cost accuracy
# ===========================================================================


class TestPricingTableComputeCost:
    def test_zero_tokens_gives_zero_cost(self) -> None:
        table = PricingTable()
        cost = table.compute_cost("gpt-4.1-mini", 0, 0)
        assert cost == pytest.approx(0.0)

    def test_gpt_4o_mini_known_rate(self) -> None:
        # gpt-4o-mini: input 0.000150/1k, output 0.000600/1k
        table = PricingTable()
        cost = table.compute_cost("gpt-4o-mini", 1000, 1000)
        expected = (1000 * 0.000150 + 1000 * 0.000600) / 1000.0
        assert cost == pytest.approx(expected, rel=1e-9)

    def test_gpt_4_1_mini_rate(self) -> None:
        # gpt-4.1-mini: input 0.000400/1k, output 0.001600/1k
        table = PricingTable()
        cost = table.compute_cost("gpt-4.1-mini", 500, 300)
        expected = (500 * 0.000400 + 300 * 0.001600) / 1000.0
        assert cost == pytest.approx(expected, rel=1e-9)

    def test_input_and_output_weighted_separately(self) -> None:
        table = PricingTable({"my-model": (1.0, 2.0)})
        # 100 input tokens: 100 * 1.0 / 1000 = 0.1
        # 200 output tokens: 200 * 2.0 / 1000 = 0.4
        cost = table.compute_cost("my-model", 100, 200)
        assert cost == pytest.approx(0.5, rel=1e-9)

    def test_unknown_model_raises_key_error(self) -> None:
        table = PricingTable()
        with pytest.raises(KeyError):
            table.compute_cost("nonexistent-model-xyz", 100, 100)


# ===========================================================================
# PricingTable — custom rate registration
# ===========================================================================


class TestPricingTableRegistration:
    def test_register_new_model(self) -> None:
        table = PricingTable()
        table.register_rate("brand-new-model", 0.001, 0.003)
        assert "brand-new-model" in table.known_models()

    def test_registered_rate_used_in_compute(self) -> None:
        table = PricingTable()
        table.register_rate("custom-v1", 2.0, 4.0)
        cost = table.compute_cost("custom-v1", 1000, 500)
        expected = (1000 * 2.0 + 500 * 4.0) / 1000.0
        assert cost == pytest.approx(expected, rel=1e-9)

    def test_update_existing_rate(self) -> None:
        table = PricingTable()
        original_cost = table.compute_cost("gpt-4.1-mini", 1000, 1000)
        table.register_rate("gpt-4.1-mini", 9.99, 9.99)
        updated_cost = table.compute_cost("gpt-4.1-mini", 1000, 1000)
        assert updated_cost != pytest.approx(original_cost)
        assert updated_cost == pytest.approx((1000 * 9.99 + 1000 * 9.99) / 1000.0, rel=1e-9)

    def test_custom_rates_dict_replaces_defaults(self) -> None:
        table = PricingTable({"only-model": (0.5, 1.0)})
        assert table.known_models() == ["only-model"]
        with pytest.raises(KeyError):
            table.compute_cost("gpt-4.1-mini", 100, 100)


# ===========================================================================
# CostEntry — immutability
# ===========================================================================


class TestCostEntryImmutability:
    def test_cost_entry_is_frozen(self) -> None:
        """CostEntry must be immutable (Pydantic frozen model)."""
        entry = CostEntry(
            entry_id="abc123",
            model_id="gpt-4.1-mini",
            provider="openai",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_usd=0.0001,
            timestamp=time.time(),
            operation_id=None,
            session_id=None,
            metadata=None,
        )
        with pytest.raises(Exception):
            entry.cost_usd = 999.0  # type: ignore[misc]

    def test_cost_entry_fields_accessible(self) -> None:
        ts = time.time()
        entry = CostEntry(
            entry_id="id1",
            model_id="gpt-4o",
            provider="openai",
            input_tokens=200,
            output_tokens=100,
            total_tokens=300,
            cost_usd=0.005,
            timestamp=ts,
            operation_id="op-1",
            session_id="sess-1",
            metadata={"retry": 0},
        )
        assert entry.entry_id == "id1"
        assert entry.model_id == "gpt-4o"
        assert entry.input_tokens == 200
        assert entry.output_tokens == 100
        assert entry.total_tokens == 300
        assert entry.cost_usd == pytest.approx(0.005)
        assert entry.timestamp == ts
        assert entry.operation_id == "op-1"
        assert entry.session_id == "sess-1"
        assert entry.metadata == {"retry": 0}


# ===========================================================================
# CostLedger — basic recording
# ===========================================================================


class TestCostLedgerRecord:
    def test_record_returns_cost_entry(self) -> None:
        ledger = CostLedger()
        entry = ledger.record("gpt-4.1-mini", 500, 300, provider="openai")
        assert isinstance(entry, CostEntry)
        assert entry.model_id == "gpt-4.1-mini"
        assert entry.provider == "openai"
        assert entry.input_tokens == 500
        assert entry.output_tokens == 300
        assert entry.total_tokens == 800

    def test_record_entry_id_is_unique(self) -> None:
        ledger = CostLedger()
        e1 = ledger.record("gpt-4.1-mini", 100, 50)
        e2 = ledger.record("gpt-4.1-mini", 100, 50)
        assert e1.entry_id != e2.entry_id

    def test_record_timestamp_is_recent(self) -> None:
        before = time.time()
        ledger = CostLedger()
        entry = ledger.record("gpt-4.1-mini", 100, 50)
        after = time.time()
        assert before <= entry.timestamp <= after

    def test_record_optional_fields_propagated(self) -> None:
        ledger = CostLedger()
        entry = ledger.record(
            "gpt-4.1-mini",
            200,
            100,
            operation_id="op-xyz",
            session_id="sess-42",
            metadata={"foo": "bar"},
        )
        assert entry.operation_id == "op-xyz"
        assert entry.session_id == "sess-42"
        assert entry.metadata == {"foo": "bar"}

    def test_record_unknown_model_raises_key_error(self) -> None:
        ledger = CostLedger()
        with pytest.raises(KeyError):
            ledger.record("no-such-model", 100, 100)


# ===========================================================================
# CostLedger — total_cost accumulation
# ===========================================================================


class TestCostLedgerTotalCost:
    def test_empty_ledger_total_is_zero(self) -> None:
        ledger = CostLedger()
        assert ledger.total_cost() == pytest.approx(0.0)

    def test_total_cost_accumulates(self) -> None:
        ledger = CostLedger()
        e1 = ledger.record("gpt-4.1-mini", 1000, 500)
        e2 = ledger.record("gpt-4.1-mini", 2000, 1000)
        assert ledger.total_cost() == pytest.approx(e1.cost_usd + e2.cost_usd, rel=1e-9)

    def test_total_tokens_accumulates(self) -> None:
        ledger = CostLedger()
        ledger.record("gpt-4.1-mini", 300, 200)
        ledger.record("gpt-4.1-mini", 100, 50)
        assert ledger.total_tokens() == 650


# ===========================================================================
# CostLedger — budget enforcement
# ===========================================================================


class TestCostLedgerBudget:
    def test_under_budget_does_not_raise(self) -> None:
        ledger = CostLedger(budget_usd=1.0)
        # gpt-4.1-mini at tiny token counts should be well under $1
        ledger.record("gpt-4.1-mini", 100, 50)
        assert not ledger.is_over_budget()

    def test_over_budget_raises_budget_exceeded_error(self) -> None:
        # Use a tiny budget so any call exceeds it
        ledger = CostLedger(budget_usd=0.0)
        with pytest.raises(BudgetExceededError) as exc_info:
            ledger.record("gpt-4.1-mini", 10000, 10000)
        assert exc_info.value.budget_usd == 0.0
        assert exc_info.value.total_cost > 0.0

    def test_budget_error_contains_overspend_details(self) -> None:
        ledger = CostLedger(budget_usd=0.000001)  # $0.000001 budget
        with pytest.raises(BudgetExceededError) as exc_info:
            ledger.record("gpt-4.1", 1000, 1000)
        err = exc_info.value
        assert err.total_cost > err.budget_usd

    def test_no_budget_never_raises(self) -> None:
        ledger = CostLedger()  # no budget
        # Record many expensive calls
        for _ in range(10):
            ledger.record("gpt-4.1", 10000, 10000)
        assert not ledger.is_over_budget()

    def test_remaining_budget_no_budget_returns_none(self) -> None:
        ledger = CostLedger()
        assert ledger.remaining_budget() is None

    def test_remaining_budget_decreases_with_each_call(self) -> None:
        ledger = CostLedger(budget_usd=1.0)
        ledger.record("gpt-4.1-mini", 100, 50)
        remaining = ledger.remaining_budget()
        assert remaining is not None
        assert remaining < 1.0
        assert remaining == pytest.approx(1.0 - ledger.total_cost(), rel=1e-9)

    def test_is_over_budget_false_when_no_budget(self) -> None:
        ledger = CostLedger()
        assert not ledger.is_over_budget()


# ===========================================================================
# CostLedger — entries filtering
# ===========================================================================


class TestCostLedgerEntries:
    def test_entries_returns_all_without_filters(self) -> None:
        ledger = CostLedger()
        ledger.record("gpt-4.1-mini", 100, 50)
        ledger.record("gpt-4o-mini", 200, 100)
        assert len(ledger.entries()) == 2

    def test_entries_filter_by_model_id(self) -> None:
        ledger = CostLedger()
        ledger.record("gpt-4.1-mini", 100, 50)
        ledger.record("gpt-4o-mini", 200, 100)
        ledger.record("gpt-4.1-mini", 300, 150)

        mini_entries = ledger.entries(model_id="gpt-4.1-mini")
        assert len(mini_entries) == 2
        assert all(e.model_id == "gpt-4.1-mini" for e in mini_entries)

    def test_entries_filter_by_session_id(self) -> None:
        ledger = CostLedger()
        ledger.record("gpt-4.1-mini", 100, 50, session_id="sess-A")
        ledger.record("gpt-4.1-mini", 200, 100, session_id="sess-B")
        ledger.record("gpt-4.1-mini", 300, 150, session_id="sess-A")

        sess_a = ledger.entries(session_id="sess-A")
        assert len(sess_a) == 2
        assert all(e.session_id == "sess-A" for e in sess_a)

    def test_entries_filter_by_model_and_session(self) -> None:
        ledger = CostLedger()
        ledger.record("gpt-4.1-mini", 100, 50, session_id="sess-A")
        ledger.record("gpt-4o-mini", 200, 100, session_id="sess-A")
        ledger.record("gpt-4.1-mini", 300, 150, session_id="sess-B")

        filtered = ledger.entries(model_id="gpt-4.1-mini", session_id="sess-A")
        assert len(filtered) == 1
        assert filtered[0].model_id == "gpt-4.1-mini"
        assert filtered[0].session_id == "sess-A"

    def test_entries_no_match_returns_empty_list(self) -> None:
        ledger = CostLedger()
        ledger.record("gpt-4.1-mini", 100, 50)
        assert ledger.entries(model_id="gpt-4o-mini") == []


# ===========================================================================
# CostLedger — summary breakdown
# ===========================================================================


class TestCostLedgerSummary:
    def test_summary_empty_ledger(self) -> None:
        ledger = CostLedger()
        s = ledger.summary()
        assert s["total_cost"] == pytest.approx(0.0)
        assert s["total_tokens"] == 0
        assert s["by_model"] == {}

    def test_summary_by_model_keys(self) -> None:
        ledger = CostLedger()
        ledger.record("gpt-4.1-mini", 100, 50)
        ledger.record("gpt-4o-mini", 200, 100)
        s = ledger.summary()
        assert "gpt-4.1-mini" in s["by_model"]
        assert "gpt-4o-mini" in s["by_model"]

    def test_summary_by_model_cost_matches_entries(self) -> None:
        ledger = CostLedger()
        e1 = ledger.record("gpt-4.1-mini", 500, 300)
        e2 = ledger.record("gpt-4.1-mini", 200, 100)
        s = ledger.summary()
        expected = e1.cost_usd + e2.cost_usd
        assert s["by_model"]["gpt-4.1-mini"]["cost"] == pytest.approx(expected, rel=1e-9)

    def test_summary_by_model_calls_count(self) -> None:
        ledger = CostLedger()
        for _ in range(3):
            ledger.record("gpt-4.1-mini", 100, 50)
        ledger.record("gpt-4o-mini", 100, 50)
        s = ledger.summary()
        assert s["by_model"]["gpt-4.1-mini"]["calls"] == 3
        assert s["by_model"]["gpt-4o-mini"]["calls"] == 1

    def test_summary_total_cost_matches_sum_of_models(self) -> None:
        ledger = CostLedger()
        ledger.record("gpt-4.1-mini", 300, 200)
        ledger.record("gpt-4o-mini", 500, 400)
        s = ledger.summary()
        model_total = sum(m["cost"] for m in s["by_model"].values())
        assert s["total_cost"] == pytest.approx(model_total, rel=1e-9)


# ===========================================================================
# CostLedger — thread safety
# ===========================================================================


class TestCostLedgerThreadSafety:
    def test_concurrent_recording_is_thread_safe(self) -> None:
        """Multiple threads recording simultaneously should not lose entries."""
        ledger = CostLedger()
        num_threads = 20
        calls_per_thread = 50
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(calls_per_thread):
                    ledger.record("gpt-4.1-mini", 10, 5)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        expected_count = num_threads * calls_per_thread
        assert len(ledger.entries()) == expected_count
        assert ledger.total_tokens() == expected_count * 15
