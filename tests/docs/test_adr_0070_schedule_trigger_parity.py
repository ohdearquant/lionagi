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
_HEADING = re.compile(r"#{1,6} ")


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
    # Stop at the next section: unbounded, an assertion meant for D2 passes on
    # text from D3 or later, and the section could be empty without failing.
    # Fenced blocks are skipped, since D2 quotes SQL and Python whose comment
    # lines start with a hash in column one and read as headings otherwise.
    kept: list[str] = []
    fenced = False
    for line in parts[1].splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and _HEADING.match(line):
            break
        kept.append(line)
    body = "\n".join(kept)
    assert body.strip(), "D2 is empty; the assertions below would pass on nothing."
    return " ".join(body.split())


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


def test_the_d2_reader_stops_at_the_next_section() -> None:
    """Without a bound, every assertion below could be satisfied by another section."""
    adr = ADR_PATH.read_text(encoding="utf-8")
    d2 = _d2_section(adr)

    d3_only = "The run and invocation rows are created by"
    assert d3_only in " ".join(adr.split()), "the control text moved; pick another D3 line"
    assert d3_only not in d2


def test_adr_documents_where_a_skipped_one_shot_is_stopped() -> None:
    """The max-run gate is documented with its limit, and with what covers it.

    The reservation only guards a fire that actually ran: a missed fire is
    recorded as `skipped`, which it does not count, so the budget stays
    unspent. What stops the instant coming back is the re-apply rule, not the
    reservation, and the two must not be conflated back together.
    """
    d2 = _d2_section(ADR_PATH.read_text(encoding="utf-8"))

    assert "`skipped` is not a counted status" in d2
    assert "writes `next_fire_at` only when the declared instant is still in the future" in d2
    assert "Moving the instant forward is a genuine reschedule" in d2
    # And that the obvious remedy stays ruled out, since the same status carries
    # capacity and overlap skips that are supposed to be retried.
    assert "Counting `skipped` rows would not have worked" in d2


def test_adr_fire_gate_order_includes_the_shipped_rate_limit() -> None:
    adr = " ".join(ADR_PATH.read_text(encoding="utf-8").split())

    assert "budget, rate-limit, run-count, and global-slot gates" in adr
    assert "→ cumulative token/cost gate → rolling rate-limit reservation → max_runs" in adr
    assert "Manual fire:** applies budget, rate-limit, max-run, and global-slot checks" in adr
    assert "tells the caller to retry after the configured window advances" in adr
