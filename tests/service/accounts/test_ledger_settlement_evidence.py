# SPDX-License-Identifier: Apache-2.0
"""Stored evidence validation and settlement invariants."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest

from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._pricing import (
    ModelPrice,
    PriceRevision,
    PriceTier,
    price_usage,
    quote_ceiling,
)
from lionagi.service.accounts._types import (
    AccountRoutingError,
    AccountSpec,
    CompleteUsage,
    PricingInputs,
    ProvenNotSent,
    Quote,
    Rate,
    RejectionZero,
    RouterErrorCode,
    Scope,
    StreamUsageEstimate,
    UnknownOutcome,
    UsageCounts,
    UsageRates,
)

NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)


async def reserved(path: Path):
    ledger = CredentialLedger(path, clock=lambda: NOW)
    attempt = uuid4()
    await ledger.reserve(
        Scope("branch", uuid4(), "anthropic"),
        attempt,
        Quote("model-a", 10, "price-a", "a" * 64),
        (AccountSpec("alpha", "ALPHA_KEY", 100),),
    )
    return ledger, attempt


def priced_revision(max_output: int = 2048, max_input: int = 10_000):
    rate = Fraction(1, 1000)
    tier = PriceTier(None, rate, rate, rate, rate, rate)
    return PriceRevision(
        "price-evidence",
        "2026-01-01",
        (),
        {"model-a": ModelPrice(max_input, max_output, (tier,))},
        {None: Fraction(1, 1)},
    )


async def priced_reservation(
    path: Path, *, max_output: int = 2048, max_input: int = 10_000, output_bound: int = 1024
):
    revision = priced_revision(max_output, max_input)
    quote = quote_ceiling(
        revision,
        json.dumps(
            {
                "model": "model-a",
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": output_bound,
                "service_tier": "standard_only",
            }
        ).encode(),
    )
    ledger = CredentialLedger(path, clock=lambda: NOW, price_revisions={revision.id: revision})
    attempt = uuid4()
    await ledger.reserve(
        Scope("branch", uuid4(), "anthropic"),
        attempt,
        quote,
        (AccountSpec("alpha", "ALPHA_KEY", 100_000),),
    )
    return ledger, attempt, quote, revision


def estimate(quote, revision, reason: str, output_tokens: int = 0):
    counts = UsageCounts(0, 0, 0, 0, output_tokens)
    usage = {
        "input_tokens": 0,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": 0,
    }
    priced = price_usage(revision, frozenset(), "model-a", usage)
    assert priced is not None
    margin_tokens = max(1024, (output_tokens + 9) // 10)
    numerator = margin_tokens * quote.pricing_inputs.output_rate_max.numerator
    denominator = quote.pricing_inputs.output_rate_max.denominator
    margin_nusd = (numerator + denominator - 1) // denominator
    rate = Rate(1, 1000)
    rates = UsageRates(rate, rate, rate, rate, rate)
    return StreamUsageEstimate(
        reason,
        "model-a",
        counts,
        None,
        quote.pricing_inputs,
        "reservation",
        rates,
        priced.cost_nusd,
        margin_tokens,
        margin_nusd,
        priced.cost_nusd + margin_nusd,
    )


def stored_row(path: Path, attempt):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM attempt WHERE id=?", (str(attempt),)).fetchone()


def write_envelope(path: Path, attempt, value: str):
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE attempt SET settlement_evidence_json=? WHERE id=?", (value, str(attempt))
        )


def create_pre_evidence_ledger(path: Path):
    """Create a version one database using the attempt DDL from before evidence columns."""
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE account (
                provider TEXT NOT NULL, name TEXT NOT NULL, key_env TEXT NOT NULL,
                block_reason TEXT, PRIMARY KEY(provider, name)
            );
            CREATE TABLE month (
                provider TEXT NOT NULL, name TEXT NOT NULL, month_utc TEXT NOT NULL,
                budget_nusd INTEGER NOT NULL CHECK(typeof(budget_nusd)='integer' AND budget_nusd > 0),
                PRIMARY KEY(provider, name, month_utc),
                FOREIGN KEY(provider, name) REFERENCES account(provider, name)
            );
            CREATE TABLE binding (
                scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, provider TEXT NOT NULL,
                account TEXT NOT NULL, PRIMARY KEY(scope_kind, scope_id, provider),
                FOREIGN KEY(provider, account) REFERENCES account(provider, name)
            );
            CREATE TABLE attempt (
                id TEXT PRIMARY KEY, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
                provider TEXT NOT NULL, account TEXT NOT NULL, month_utc TEXT NOT NULL,
                ceiling_nusd INTEGER NOT NULL CHECK(typeof(ceiling_nusd)='integer' AND ceiling_nusd > 0),
                price_revision TEXT NOT NULL, requested_model TEXT NOT NULL,
                request_digest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('open','settled')),
                cost_nusd INTEGER, usage_json TEXT, request_id TEXT, outcome TEXT,
                created_at TEXT NOT NULL, settled_at TEXT, provider_error_type TEXT, rejection_class TEXT,
                CHECK((state='settled' AND typeof(cost_nusd)='integer' AND cost_nusd >= 0)
                  OR (state='open' AND cost_nusd IS NULL)),
                FOREIGN KEY(provider, account) REFERENCES account(provider, name),
                FOREIGN KEY(provider, account, month_utc) REFERENCES month(provider, name, month_utc)
            );
            CREATE TABLE adjustment (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, attempt_id TEXT, provider TEXT NOT NULL,
                account TEXT NOT NULL, month_utc TEXT, delta_nusd INTEGER NOT NULL,
                source_ref TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX attempt_by_account_month ON attempt(provider, account, month_utc, state);
            CREATE INDEX adjustment_by_account_month ON adjustment(provider, account, month_utc);
            CREATE TABLE warm_prefix (
                provider TEXT NOT NULL, account TEXT NOT NULL, cert TEXT NOT NULL,
                stable_tokens INTEGER, expires_at TEXT NOT NULL,
                PRIMARY KEY(provider, account, cert),
                FOREIGN KEY(provider, account) REFERENCES account(provider, name)
            );
            CREATE INDEX warm_prefix_cert ON warm_prefix(provider, cert);
            CREATE INDEX warm_prefix_expiry ON warm_prefix(provider, expires_at);
            PRAGMA user_version=1;
            """
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.pop("evidence"),
        lambda x: x.update(extra=True),
        lambda x: x.update(schema_version=2),
        lambda x: x.update(rule_revision="other"),
        lambda x: x["evidence"].update(cost_nusd=1.5),
        lambda x: x["evidence"]["counts"].update(input_tokens=True),
        lambda x: x.update(basis="unknown"),
        lambda x: x.update(origin="unknown"),
        lambda x: x.update(source_ref="evidence:runtime-origin"),
        lambda x: x["evidence"].update(extra=True),
    ],
    ids=(
        "missing-key",
        "extra-key",
        "version",
        "rule-revision",
        "float-amount",
        "boolean-count",
        "unknown-basis",
        "unknown-origin",
        "runtime-source-ref",
        "unknown-evidence-field",
    ),
)
async def test_malformed_stored_envelope_refuses_without_changing_attempt(tmp_path, mutate):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt, quote, revision = await priced_reservation(path)
    await ledger.settle(attempt, estimate(quote, revision, "cancelled"))
    with sqlite3.connect(path) as db:
        serialized = db.execute(
            "SELECT settlement_evidence_json FROM attempt WHERE id=?", (str(attempt),)
        ).fetchone()[0]
    value = json.loads(serialized)
    mutate(value)
    write_envelope(path, attempt, json.dumps(value))
    before = stored_row(path, attempt)
    with pytest.raises(AccountRoutingError) as error:
        await ledger.settle(attempt, UnknownOutcome("transport_error"), http_status=502)
    assert error.value.code is RouterErrorCode.LEDGER_UNAVAILABLE
    assert stored_row(path, attempt) == before


@pytest.mark.anyio
async def test_pre_evidence_version_one_schema_migrates_from_its_original_ddl(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    create_pre_evidence_ledger(path)
    await CredentialLedger(path, clock=lambda: NOW).status("anthropic")
    with sqlite3.connect(path) as db:
        columns = {row[1]: row[2] for row in db.execute('PRAGMA table_info("attempt")')}
        version = db.execute("PRAGMA user_version").fetchone()[0]
    assert columns["settlement_evidence_json"] == "TEXT"
    assert columns["http_status"] == "INTEGER"
    assert version == 1


@pytest.mark.anyio
async def test_rejection_repeat_uses_full_evidence_and_keeps_the_stored_class(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt = await reserved(path)
    await ledger.settle(
        attempt, UnknownOutcome("request_rejected", "invalid_request_error", "other")
    )
    zero = RejectionZero(400, "invalid_request_error", "prompt_too_long", "request_rejected")
    assert await ledger.settle(attempt, zero) == "settled"
    first = stored_row(path, attempt)
    assert await ledger.settle(attempt, zero) == "already_settled"
    assert stored_row(path, attempt) == first
    assert (
        await ledger.settle(
            attempt,
            RejectionZero(400, "invalid_request_error", "image_too_large", "request_rejected"),
        )
        == "already_settled"
    )
    assert stored_row(path, attempt) == first


@pytest.mark.anyio
async def test_a_different_rejection_envelope_conflicts_after_committing_block_and_warmth_clear(
    tmp_path,
):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt = await reserved(path)
    await ledger.settle(
        attempt, RejectionZero(400, "invalid_request_error", "other", "request_rejected")
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO warm_prefix(provider, account, cert, stable_tokens, expires_at) VALUES (?, ?, ?, ?, ?)",
            ("anthropic", "alpha", "cert", 1, (NOW + timedelta(hours=1)).isoformat()),
        )
    before = stored_row(path, attempt)
    with pytest.raises(AccountRoutingError) as error:
        await ledger.settle(attempt, RejectionZero(529, "overloaded_error", None, "provider_error"))
    assert error.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert stored_row(path, attempt) == before
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT block_reason FROM account WHERE provider=? AND name=?", ("anthropic", "alpha")
        ).fetchone() == ("settlement_conflict",)
        assert db.execute(
            "SELECT count(*) FROM warm_prefix WHERE provider=? AND account=?",
            ("anthropic", "alpha"),
        ).fetchone() == (0,)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 529])
async def test_conflicting_open_reasons_block_commit_and_preserve_attempt(tmp_path, status):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt = await reserved(path)
    await ledger.settle(attempt, UnknownOutcome("cancelled"))
    before = stored_row(path, attempt)
    evidence = (
        RejectionZero(400, "invalid_request_error", "other", "request_rejected")
        if status == 400
        else RejectionZero(529, "overloaded_error", None, "provider_error")
    )
    with pytest.raises(AccountRoutingError) as error:
        await ledger.settle(attempt, evidence)
    assert error.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert stored_row(path, attempt) == before
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT block_reason FROM account WHERE provider=? AND name=?", ("anthropic", "alpha")
        ).fetchone() == ("settlement_conflict",)


@pytest.mark.anyio
async def test_a_400_zero_conflicts_with_a_different_recorded_rejection_type(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt = await reserved(path)
    await ledger.settle(attempt, UnknownOutcome("request_rejected", "authentication_error"))
    before = stored_row(path, attempt)
    with pytest.raises(AccountRoutingError) as error:
        await ledger.settle(
            attempt,
            RejectionZero(400, "invalid_request_error", "other", "request_rejected"),
        )
    assert error.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert stored_row(path, attempt) == before


@pytest.mark.anyio
async def test_stream_reason_must_match_the_recorded_ending(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt, quote, revision = await priced_reservation(path)
    await ledger.settle(attempt, UnknownOutcome("cancelled"))
    before = stored_row(path, attempt)
    with pytest.raises(AccountRoutingError) as error:
        await ledger.settle(attempt, estimate(quote, revision, "stream_closed"))
    assert error.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert stored_row(path, attempt) == before


@pytest.mark.anyio
async def test_reason_only_write_after_settlement_returns_already_settled(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt = await reserved(path)
    await ledger.settle(
        attempt,
        RejectionZero(400, "invalid_request_error", "other", "request_rejected"),
    )
    before = stored_row(path, attempt)
    assert await ledger.settle(attempt, UnknownOutcome("cancelled")) == "already_settled"
    assert stored_row(path, attempt) == before


@pytest.mark.anyio
@pytest.mark.parametrize("closure", ["complete", "not_sent", "reconcile"])
async def test_old_closures_preserve_the_quote_envelope_byte_for_byte(tmp_path, closure):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt, _, _ = await priced_reservation(path)
    with sqlite3.connect(path) as db:
        before = db.execute(
            "SELECT settlement_evidence_json FROM attempt WHERE id=?", (str(attempt),)
        ).fetchone()[0]
    if closure == "complete":
        await ledger.settle(attempt, CompleteUsage("model-a", 0, 0, 0, 0, 0, 0))
    elif closure == "not_sent":
        await ledger.settle(attempt, ProvenNotSent())
    else:
        await ledger.reconcile(uuid4(), attempt, 0, "existing-source")
    with sqlite3.connect(path) as db:
        after = db.execute(
            "SELECT settlement_evidence_json FROM attempt WHERE id=?", (str(attempt),)
        ).fetchone()[0]
    assert after == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reason", "keeps_warmth"),
    [
        ("transport_error", False),
        ("stream_truncated", False),
        ("stream_error", False),
        ("cancelled", True),
        ("stream_closed", True),
    ],
)
async def test_stream_estimates_follow_reason_warmth_rules(tmp_path, reason, keeps_warmth):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt, quote, revision = await priced_reservation(path)
    expiry = (NOW + timedelta(hours=1)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO warm_prefix(provider, account, cert, stable_tokens, expires_at) VALUES (?, ?, ?, ?, ?)",
            ("anthropic", "alpha", "cert", 12, expiry),
        )
    with sqlite3.connect(path) as db:
        warm_before = db.execute(
            "SELECT stable_tokens, expires_at FROM warm_prefix WHERE account='alpha'"
        ).fetchall()
    assert await ledger.settle(attempt, UnknownOutcome(reason)) == "still_open"
    assert await ledger.settle(attempt, estimate(quote, revision, reason)) == "settled"
    with sqlite3.connect(path) as db:
        count = db.execute(
            "SELECT count(*) FROM warm_prefix WHERE provider=? AND account=?",
            ("anthropic", "alpha"),
        ).fetchone()[0]
        warm_after = db.execute(
            "SELECT stable_tokens, expires_at FROM warm_prefix WHERE account='alpha'"
        ).fetchall()
        envelope = json.loads(
            db.execute(
                "SELECT settlement_evidence_json FROM attempt WHERE id=?", (str(attempt),)
            ).fetchone()[0]
        )
    assert (count == 1) is keeps_warmth
    assert warm_after == (warm_before if keeps_warmth else [])
    assert envelope["first_failure"] == {
        "reason": reason,
        "provider_error_type": None,
        "rejection_class": None,
    }


@pytest.mark.anyio
async def test_stream_duplicate_compares_evidence_and_preserves_the_attempt_on_conflict(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt, quote, revision = await priced_reservation(path)
    await ledger.settle(attempt, UnknownOutcome("cancelled"))
    original = estimate(quote, revision, "cancelled")
    assert await ledger.settle(attempt, original) == "settled"
    before = stored_row(path, attempt)
    changed = replace(original, request_id="other-request")
    with pytest.raises(AccountRoutingError) as error:
        await ledger.settle(attempt, changed)
    assert error.value.code is RouterErrorCode.SETTLEMENT_CONFLICT
    assert stored_row(path, attempt) == before


@pytest.mark.anyio
async def test_identical_stream_repeat_is_a_noop(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt, quote, revision = await priced_reservation(path)
    await ledger.settle(attempt, UnknownOutcome("cancelled"))
    value = estimate(quote, revision, "cancelled")
    assert await ledger.settle(attempt, value) == "settled"
    before = stored_row(path, attempt)
    assert await ledger.settle(attempt, value) == "already_settled"
    assert stored_row(path, attempt) == before


@pytest.mark.anyio
async def test_stream_estimate_recomputes_cost_before_writing(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt, quote, revision = await priced_reservation(path)
    estimate_value = estimate(quote, revision, "stream_closed")
    object.__setattr__(estimate_value, "cost_nusd", estimate_value.cost_nusd + 1)
    before = stored_row(path, attempt)
    with pytest.raises(ValueError, match="recomputed"):
        await ledger.settle(attempt, estimate_value)
    assert stored_row(path, attempt) == before


@pytest.mark.anyio
async def test_estimate_above_reservation_ceiling_is_recorded_and_blocks(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt, quote, revision = await priced_reservation(path, max_input=1, output_bound=1)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO warm_prefix(provider, account, cert, stable_tokens, expires_at) VALUES (?, ?, ?, ?, ?)",
            ("anthropic", "alpha", "cert", 1, (NOW + timedelta(hours=1)).isoformat()),
        )
    value = estimate(quote, revision, "transport_error", output_tokens=1024)
    assert value.cost_nusd > quote.ceiling_nusd
    await ledger.settle(attempt, value)
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT cost_nusd, outcome FROM attempt WHERE id=?", (str(attempt),)
        ).fetchone()
        block = db.execute(
            "SELECT block_reason FROM account WHERE provider=? AND name=?",
            ("anthropic", "alpha"),
        ).fetchone()
    assert row == (value.cost_nusd, "stream_usage_estimate")
    assert block == ("ceiling_exceeded",)
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT count(*) FROM warm_prefix WHERE account='alpha'").fetchone()[0] == 0
        )
        envelope = json.loads(
            db.execute(
                "SELECT settlement_evidence_json FROM attempt WHERE id=?", (str(attempt),)
            ).fetchone()[0]
        )
    assert envelope["first_failure"] == {
        "reason": None,
        "provider_error_type": None,
        "rejection_class": None,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "refusal",
    ["revision", "model", "bound", "feature_selector", "tier_selector", "rate", "ceiling"],
)
async def test_reserve_pricing_refusals_leave_no_row(tmp_path, refusal):
    path = tmp_path / "ledger.sqlite3"
    revision = priced_revision(max_output=100)
    quote = quote_ceiling(
        revision,
        b'{"model":"model-a","messages":[{"role":"user","content":"x"}],"max_tokens":10,"service_tier":"standard_only"}',
    )
    ledger = CredentialLedger(
        path,
        clock=lambda: NOW,
        price_revisions={} if refusal == "revision" else {revision.id: revision},
    )
    if refusal == "model":
        quote = Quote(
            "unknown-model",
            quote.ceiling_nusd,
            quote.price_revision,
            quote.request_digest,
            quote.pricing_inputs,
        )
    elif refusal == "bound":
        inputs = PricingInputs(
            101, (), None, "messages_standard_v1", "max_applicable_at_reserve", Rate(1, 1000)
        )
        quote = Quote(
            quote.requested_model,
            quote.ceiling_nusd,
            quote.price_revision,
            quote.request_digest,
            inputs,
        )
    elif refusal in ("feature_selector", "tier_selector"):
        inputs = quote.pricing_inputs
        field = "feature_profile" if refusal == "feature_selector" else "tier_selector"
        object.__setattr__(inputs, field, "unsupported")
    elif refusal == "rate":
        inputs = quote.pricing_inputs
        quote = Quote(
            quote.requested_model,
            quote.ceiling_nusd,
            quote.price_revision,
            quote.request_digest,
            PricingInputs(
                inputs.output_bound,
                inputs.cache_ttls,
                inputs.single_ttl,
                inputs.feature_profile,
                inputs.tier_selector,
                Rate(1, 500),
            ),
        )
    elif refusal == "ceiling":
        quote = Quote(
            quote.requested_model,
            quote.ceiling_nusd + 1,
            quote.price_revision,
            quote.request_digest,
            quote.pricing_inputs,
        )
    error = AccountRoutingError
    expected = RouterErrorCode.UNSUPPORTED_PRICING if refusal in ("revision", "model") else None
    with pytest.raises(error if expected else ValueError) as caught:
        await ledger.reserve(
            Scope("branch", uuid4(), "anthropic"),
            uuid4(),
            quote,
            (AccountSpec("alpha", "ALPHA_KEY", 100_000),),
        )
    if expected:
        assert caught.value.code is expected
    assert not path.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("reason", [None, "request_rejected"])
async def test_rejection_closure_retains_the_pre_settlement_snapshot(tmp_path, reason):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt = await reserved(path)
    if reason is not None:
        await ledger.settle(attempt, UnknownOutcome(reason, "invalid_request_error", "other"))
    await ledger.settle(
        attempt, RejectionZero(400, "invalid_request_error", "prompt_too_long", "request_rejected")
    )
    with sqlite3.connect(path) as db:
        value = db.execute(
            "SELECT settlement_evidence_json FROM attempt WHERE id=?", (str(attempt),)
        ).fetchone()[0]
    first = json.loads(value)["first_failure"]
    expected = (
        {"reason": None, "provider_error_type": None, "rejection_class": None}
        if reason is None
        else {
            "reason": "request_rejected",
            "provider_error_type": "invalid_request_error",
            "rejection_class": "other",
        }
    )
    assert first == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status, prior_reason",
    [(400, "request_rejected"), (529, "provider_error"), (529, None)],
)
async def test_rejection_zero_clears_warmth_and_preserves_529_null_columns(
    tmp_path, status, prior_reason
):
    path = tmp_path / "ledger.sqlite3"
    ledger, attempt = await reserved(path)
    if prior_reason is not None:
        await ledger.settle(attempt, UnknownOutcome(prior_reason))
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO warm_prefix(provider, account, cert, stable_tokens, expires_at) VALUES (?, ?, ?, ?, ?)",
            ("anthropic", "alpha", "cert", 12, (NOW + timedelta(hours=1)).isoformat()),
        )
    evidence = (
        RejectionZero(400, "invalid_request_error", "other", "request_rejected")
        if status == 400
        else RejectionZero(529, "overloaded_error", None, "provider_error")
    )
    await ledger.settle(attempt, evidence)
    with sqlite3.connect(path) as db:
        fields = db.execute(
            "SELECT provider_error_type, rejection_class FROM attempt WHERE id=?", (str(attempt),)
        ).fetchone()
        warmth = db.execute("SELECT count(*) FROM warm_prefix WHERE account='alpha'").fetchone()[0]
    assert warmth == 0
    if status == 529:
        assert fields == (None, None)
