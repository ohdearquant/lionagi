# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Keep ADR-0070's trigger vocabulary aligned with the persisted schema."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR_PATH = ROOT / "docs/adr/ADR-0070-studio-scheduling-and-dispatch-delivery.md"
SCHEMA_PATH = ROOT / "lionagi/state/schema.sql"
EXPECTED_TRIGGERS = ("cron", "interval", "github_poll", "at")
_TRIGGER_CHECK = re.compile(r"CHECK\(trigger_type IN \((?P<values>[^)]+)\)\)")


def _trigger_values(path: Path) -> tuple[str, ...]:
    matches = _TRIGGER_CHECK.findall(path.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"expected one trigger_type CHECK in {path}"
    return tuple(value.strip().strip("'") for value in matches[0].split(","))


def test_adr_trigger_vocabulary_matches_the_persisted_schema() -> None:
    assert _trigger_values(SCHEMA_PATH) == EXPECTED_TRIGGERS
    assert _trigger_values(ADR_PATH) == EXPECTED_TRIGGERS


_D2_HEADING = "### D2 — Four trigger types pass explicit fire gates"


def _d2_section(adr: str) -> str:
    """Return D2's normalized body, or fail saying why rather than IndexError.

    Splitting on a heading that is not there yields a one-element list, so the
    unguarded form dies on an index lookup and reports nothing about the ADR.
    A rename is a legitimate edit; it just has to be made here too.
    """
    parts = adr.split(_D2_HEADING, 1)
    assert len(parts) == 2, (
        f"ADR-0070 no longer contains the heading {_D2_HEADING!r}. This test pins "
        "the contract documented beneath it, so a renamed section needs the "
        "heading updated here rather than the assertions dropped."
    )
    return " ".join(parts[1].split())


def test_adr_documents_the_at_trigger_one_shot_contract() -> None:
    adr = ADR_PATH.read_text(encoding="utf-8")
    normalized = " ".join(adr.split())
    d2 = _d2_section(adr)

    assert "Schedules persist `cron`, `interval`, `github_poll`, or `at` triggers." in normalized
    assert "A manual fire is an operation over an existing schedule, not a fifth trigger type." in (
        normalized
    )
    assert "D2: Persist four trigger types" in normalized
    assert "Top-level fire admission order for cron/interval/at is:" in normalized
    assert "stores its resolved due instant in `next_fire_at`" in d2
    assert "has no subsequent occurrence after firing" in d2
    assert "forces `max_runs = 1`" in d2
    assert "a future instant for recurring triggers, or terminal `NULL` for `at`" in d2
    assert (
        "a process crash after the terminal reservation but before the recovery row lands "
        "loses the single fire"
    ) in d2
    assert "outside the accepted `at` recovery crash window above" in d2


def test_adr_records_that_a_skipped_one_shot_can_be_resurrected() -> None:
    """The max-run gate is documented with its gap, not as a complete guard.

    The ADR previously said the max-run reservation was the guard against a
    re-apply resurrecting a one-shot. It only guards a fire that actually ran:
    a missed fire is recorded as `skipped`, which the reservation does not
    count, so the budget stays unspent and the instant comes back. Pinning the
    limit here keeps the stronger claim from returning as a tidy-up.
    """
    d2 = _d2_section(ADR_PATH.read_text(encoding="utf-8"))

    assert "`skipped` is not a counted status" in d2
    assert "This is current behavior, not an intended contract." in d2
    # And that the obvious remedy is ruled out, since the same status carries
    # capacity and overlap skips that are supposed to be retried.
    assert "counting `skipped` rows" in d2


def test_adr_fire_gate_order_includes_the_shipped_rate_limit() -> None:
    adr = " ".join(ADR_PATH.read_text(encoding="utf-8").split())

    assert "budget, rate-limit, run-count, and global-slot gates" in adr
    assert "→ cumulative token/cost gate → rolling rate-limit reservation → max_runs" in adr
    assert "Manual fire:** applies budget, rate-limit, max-run, and global-slot checks" in adr
    assert "tells the caller to retry after the configured window advances" in adr
