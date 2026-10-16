# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin Amendment 1 of ADR-0120 against the code and records it cites.

The amendment decides a wake policy for an observing subscriber. Two kinds of
claim in it are load-bearing and both are checked here: what the tree does today
(so the record cannot describe a path as absent that exists, or as present that
does not), and how the amendment relates to the accepted records it names (so it
cannot borrow a contract another record does not own).

The code claims are checked at the exact line the record cites, and the record's
own citations are checked to match. A test loose enough to survive a moved line
would let the record drift from the tree while staying green, which is the defect
class this file exists to catch.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/adr/ADR-0120-interception-observation-and-durable-delivery-planes.md"
ADR_DIR = ROOT / "docs/adr"


def _text() -> str:
    return RECORD.read_text(encoding="utf-8")


def _flat() -> str:
    """The record with runs of whitespace collapsed, so a phrase is matched
    across the record's own line wrapping rather than against one physical line."""
    return " ".join(_text().split())


def _line(relpath: str, number: int) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8").splitlines()[number - 1]


def _span(relpath: str, start: int, end: int) -> str:
    return "\n".join((ROOT / relpath).read_text(encoding="utf-8").splitlines()[start - 1 : end])


def _amendment() -> str:
    """The record from Amendment 1's own heading on, so a pin here cannot be
    satisfied by the Status line's mention of the amendment near the top."""
    text = _text()
    return text[text.index("## Amendment 1") :]


def _amendment_flat() -> str:
    return " ".join(_amendment().split())


# -- the code the amendment claims exists ------------------------------------


def test_in_process_spawn_paths_are_present_where_named() -> None:
    assert "run.spawn" in _span("lionagi/engines/hypothesis.py", 890, 961)
    assert "run.spawn" in _line("lionagi/engines/review.py", 615)
    assert "run.spawn" in _line("lionagi/engines/research.py", 187)
    assert "run.spawn" in _line("lionagi/engines/research.py", 191)


def test_flow_bus_spawn_and_escalation_paths_are_present() -> None:
    assert "observe(self.spawn_type, self._on_bus_spawn)" in _line(
        "lionagi/operations/flow.py", 1113
    )
    assert "observe(EscalationRequest, self._on_bus_escalation)" in _line(
        "lionagi/operations/flow.py", 1114
    )
    assert "def _inject_request" in _line("lionagi/operations/flow.py", 1389)


def test_observer_gate_still_applies_before_routes_and_subscribers() -> None:
    body = _span("lionagi/session/observer.py", 339, 348)
    assert "allowed = True" in body
    assert "except Exception" in body
    assert "allowed = False" in body
    assert "if not allowed:" in body
    assert "return []" in body


def test_review_verdict_has_an_emit_site_into_the_session_observer() -> None:
    assert "class ReviewVerdict(Verdict)" in _line("lionagi/engines/review.py", 280)
    assert "await run.emit(final)" in _line("lionagi/engines/review.py", 841)


def test_schedule_reader_position_is_the_github_poll_cursor() -> None:
    assert "github_cursor" in _line("lionagi/state/schema.sql", 463)


def test_outbox_row_defaults_and_attempt_bound_anchors() -> None:
    assert "DEFAULT_MAX_ATTEMPTS" in _line("lionagi/dispatch/outbox.py", 47)
    assert "ack_required: bool = False" in _line("lionagi/dispatch/outbox.py", 178)
    assert "max_attempts: int = DEFAULT_MAX_ATTEMPTS" in _line("lionagi/dispatch/outbox.py", 179)


def test_team_round_batches_pending_targets_per_completion() -> None:
    assert "pending_targets = frozenset(state.pending_targets) | frozenset(exchange_pending)" in (
        _line("lionagi/cli/orchestrate/_orchestration.py", 1378)
    )
    assert "def build_round_operations" in _line("lionagi/cli/orchestrate/_orchestration.py", 1414)
    assert "def _on_team_op_complete" in _line("lionagi/cli/orchestrate/flow.py", 1865)


# -- what the amendment now says ---------------------------------------------


def test_amendment_claims_no_durable_wake_path_not_a_missing_wake() -> None:
    text = _text()
    flat = _amendment_flat()
    assert "No durable wake path exists" in flat
    assert "mints a woken run" not in text
    assert "in-process spawns inside a running flow" in flat


def test_durable_subscriber_source_is_a_confirmed_session_signals_row() -> None:
    flat = _flat()
    assert "a `session_signals` row the persistence sink has confirmed" in flat
    assert "never the in-memory emit" in flat
    assert "reaches `best_effort` subscribers only" in flat
    assert "lionagi/state/schema.sql:797-801" in flat
    assert "lionagi/state/db.py:5569-5577" in flat
    assert "lionagi/state/db.py:5592-5607" in flat
    assert "lionagi/session/observer.py:270" in flat
    assert "the cursor is a position in that sequence, read forward with `after_seq`" in flat
    assert "is not a committed row and is no durable subscriber's source" in flat


def test_closing_batch_triggers_and_bound_are_stated() -> None:
    flat = _flat()
    assert "the first of two triggers" in flat
    assert "`RunEnd` or `RunFailed`" in flat
    assert "lionagi/session/branch.py:441-469" in flat
    assert "lionagi/operations/run/run.py:777-788" in flat
    assert "`unbind_db_persistence`" in flat
    assert "lionagi/session/observer.py:542-559" in flat
    assert "today flushes no pending wake state" in flat
    assert "the amendment adds that flush" in flat
    assert "keeps its tail pending for the life of the session" in flat
    assert "no timeout and no new setting" in flat
    assert "no execution attempt object" in flat
    assert "one admitted run, the unit ADR-0123 D1 names" in flat
    assert "lionagi/session/signal.py:163" in flat
    assert "lionagi/cli/_runs.py:1269" in flat
    assert "lionagi/studio/services/workflow_run.py:105" in flat


def test_settlement_is_an_implicit_wake_outside_the_cap() -> None:
    text = _text()
    flat = _flat()
    assert "whether or not the subscriber declared `settled`" not in text
    assert "settlement is an implicit wake for every observing" in flat
    assert "the implicit wake does not count toward `max_wakes_per_attempt`" in flat


def test_amendment_names_the_emit_site_it_kept_in_row_seven() -> None:
    text = _text()
    flat = _flat()
    assert "emitted only as a capability bundle" not in text
    assert "lionagi/engines/review.py:280`, `:841" in flat
    row_seven = next(line for line in text.splitlines() if line.startswith("| 7 |"))
    assert "budget" in row_seven
    assert "`Verdict` cast outside" in row_seven


def test_phase_two_is_a_target_and_the_gate_is_cited_where_it_still_runs() -> None:
    text = _text()
    flat = _flat()
    assert "Phase 2 already makes" not in text
    assert "Phase 2's target is to make `SessionObservation` signal-only" in flat
    assert "lionagi/session/observer.py:339-348" in flat
    assert "lionagi/session/observer.py:343-344`) and then returns at `:348" in flat
    assert "lionagi/session/observer.py:235-236" not in flat


def test_cursor_advances_in_the_state_transaction_and_borrows_only_dedup_practice() -> None:
    text = _text()
    flat = _flat()
    assert "consumed-cursor shape of ADR-0125" not in flat
    assert "ADR-0125 (Accepted) D2 step 7" in flat
    assert "is cited only for the idempotent identity per delivered input" in flat
    assert (
        "the cursor advances in the same state transaction that records the woken run's terminal"
        in flat
    )
    assert "joins the durable delivery contract ADR-0059 owns" not in text
    assert "reuses only ADR-0059's `dedup_key` practice" in flat


def test_batch_growth_is_bounded_by_attempt_length_not_declaration() -> None:
    flat = _flat()
    assert "bounded by declaration rather than by attempt" not in flat
    assert "The batch grows with attempt length; only the number of woken runs is bounded" in flat


def test_delivery_default_matches_the_outbox_tier_it_cites() -> None:
    flat = _flat()
    assert "the row completes at transport success" in flat
    assert "docs/adr/ADR-0059-durable-dispatch-outbox.md:391`, `:609" in flat
    assert "`enqueue_dispatch` inserts a row unconditionally" in flat
    assert "acknowledgement is the opt-in (`:178`)" in flat
    assert "the only per-call bound is `max_attempts` (`:179`)" in flat


def test_d1_gains_a_durable_observing_subscriber_row() -> None:
    text = _text()
    flat = _flat()
    assert "The D1 vocabulary table gains no row" not in text
    assert "D1 gains one row" in flat
    assert "| `ObservingSubscriber` |" in text


def test_d3_body_carries_the_delivery_clause_the_amendment_points_at() -> None:
    flat = _flat()
    assert "**D3 gains a delivery clause.**" in flat
    d3 = " ".join(_text()[_text().index("### D3 —") : _text().index("### D4 —")].split())
    assert "is a delivery, not a handler call" in d3
    assert "its failure changes delivery state rather than the observed outcome" in d3


def test_code_today_names_the_batch_reader_and_schedule_cursor_for_what_they_are() -> None:
    flat = _flat()
    assert "lionagi/cli/orchestrate/_orchestration.py:1378" in flat
    assert "lionagi/state/schema.sql:463" in flat
    assert "lionagi/state/db.py:3850-3852" in flat
    assert "the store advances it only forward, so a stale write cannot move it back" in flat
    assert "ADR-0070, Accepted" in flat
    assert "per-completion batch of targets, not a per-subscriber position" in flat
    assert "lionagi/cli/orchestrate/flow.py:1865-1910" in flat
    assert "It is one position per schedule, not per observing subscriber" in flat


def test_overflow_rule_and_row_three_fixture() -> None:
    text = _text()
    flat = _flat()
    assert "closing batch" in flat
    assert (
        "The closing batch is unconditional for a subscriber with undelivered admitted facts"
        in flat
    )
    assert "A held wake counts toward `max_wakes_per_attempt` only once it is" in flat
    assert (
        "The closing batch waits for a queue slot like any held wake and is never dropped for depth"
        in flat
    )
    row_three = next(line for line in text.splitlines() if line.startswith("| 3 |"))
    assert "at most four runs" in row_three
    assert "every fact delivered exactly once across the batches" in row_three


def test_consequences_stay_true_to_the_emit_sites_and_the_batch_growth() -> None:
    flat = _flat()
    assert "no fact raises `budget`, and `verdict` is raised on the review path" in flat


def test_the_team_round_grouping_is_scoped_to_admitted_signals() -> None:
    flat = _flat()
    assert "over the subscription-admitted Signals it sees" in flat


def test_wake_identity_includes_the_fact_range_everywhere() -> None:
    text = _text()
    decision = text[text.index("**Decision.**") : text.index("### What this changes")]
    assert "identity `(attempt, boundary, subject, subscriber, fact range)`" in " ".join(
        decision.split()
    )
    assert "`(attempt, boundary, subject, subscriber, fact range)`" in _flat()
    row_five = next(line for line in text.splitlines() if line.startswith("| 5 |"))
    assert "`(attempt, boundary, subject, subscriber, fact range)`" in row_five


def test_alternatives_state_their_own_reasons() -> None:
    text = _text()
    assert "D1 and D3 already reject" not in text
    assert "Kept rejected" not in text
    alternatives = text[text.index("### Alternatives considered") :]
    assert "**Observers as ordinary nodes with dependencies.** Rejected" in alternatives
    assert "settle window with an in-window veto" in alternatives
    assert "second in-memory bus" in alternatives
    assert "whose semantics begin from an already-committed source fact" in alternatives


def test_external_records_are_cited_for_what_they_own_and_marked_proposed() -> None:
    amendment = _amendment_flat()
    assert "**Status**" not in amendment
    assert _amendment().startswith("## Amendment 1 (2026-10-16)")
    assert "fixes the unit that cost should follow" not in amendment
    assert "ADR-0123 (Proposed) D1 names the unit the wake follows" in amendment
    assert "ADR-0114 (Proposed) D3" in amendment


def test_amendment_uses_the_records_own_word_and_defaults_to_best_effort() -> None:
    text = _text()
    flat = _amendment_flat()
    assert "falsifier" not in text
    assert "third gate arm" in flat
    assert "delivery: ObservanceDelivery = ObservanceDelivery.BEST_EFFORT" in text
    assert "`best_effort` is the default" in flat
    assert "`retryable` is the subscriber's opt-in" in flat
    assert "max_attempts: int = 3" in text
    assert "over D3's `SignalFilter` subscription conditions" in flat
