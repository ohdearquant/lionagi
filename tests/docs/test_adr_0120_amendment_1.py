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

import ast
import re
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


def _tree(relpath: str) -> ast.Module:
    return ast.parse((ROOT / relpath).read_text(encoding="utf-8"))


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    found = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name), None
    )
    assert found is not None, f"class {name} is not in the module the amendment cites"
    return found


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = next(
        (
            n
            for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )
    assert found is not None, f"{cls.name}.{name} is not in the class the amendment cites"
    return found


def _amendment() -> str:
    """The record from Amendment 1's own heading on, so a pin here cannot be
    satisfied by the Status line's mention of the amendment near the top."""
    text = _text()
    return text[text.index("## Amendment 1") :]


def _amendment_flat() -> str:
    return " ".join(_amendment().split())


# -- the code the amendment claims exists ------------------------------------


def test_in_process_spawn_paths_are_present_where_named() -> None:
    assert "run.spawn" in _span("lionhub/src/lionhub/orchestration/engines/hypothesis.py", 890, 961)
    assert "run.spawn" in _line("lionhub/src/lionhub/orchestration/engines/review.py", 711)
    assert "run.spawn" in _line("lionhub/src/lionhub/orchestration/engines/research.py", 187)
    assert "run.spawn" in _line("lionhub/src/lionhub/orchestration/engines/research.py", 191)


def test_flow_bus_spawn_and_escalation_paths_are_present() -> None:
    assert "observe(self.spawn_type, self._on_bus_spawn)" in _line(
        "lionagi/operations/flow.py", 1164
    )
    assert "observe(EscalationRequest, self._on_bus_escalation)" in _line(
        "lionagi/operations/flow.py", 1165
    )
    assert "def _inject_request" in _line("lionagi/operations/flow.py", 1457)


def test_observer_gate_still_applies_before_routes_and_subscribers() -> None:
    body = _span("lionagi/session/observer.py", 365, 374)
    assert "allowed = True" in body
    assert "except Exception" in body
    assert "allowed = False" in body
    assert "if not allowed:" in body
    assert "return []" in body


REVIEW = "lionhub/src/lionhub/orchestration/engines/review.py"
ENGINE = "lionhub/src/lionhub/orchestration/engines/engine.py"


def test_review_verdict_has_an_emit_site_into_the_session_observer() -> None:
    review = _tree(REVIEW)
    verdict = _class(review, "ReviewVerdict")
    assert [b.id for b in verdict.bases if isinstance(b, ast.Name)] == ["Verdict"]
    body = _method(_class(review, "ReviewEngine"), "_verdict").body

    def is_emit_of_final(stmt: ast.stmt) -> bool:
        call = (
            stmt.value.value
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await)
            else None
        )
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "emit"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "run"
            and [a.id for a in call.args if isinstance(a, ast.Name)] == ["final"]
        )

    def is_rule_into_final(stmt: ast.stmt) -> bool:
        return (
            isinstance(stmt, ast.Assign)
            and [t.id for t in stmt.targets if isinstance(t, ast.Name)] == ["final"]
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "_rule"
        )

    # The verdict the synthesis rules produced is emitted at the method's own level,
    # after `final = self._rule(...)`; the shared-session refusal emits from inside a branch.
    ruled = next(i for i, stmt in enumerate(body) if is_rule_into_final(stmt))
    assert any(is_emit_of_final(stmt) for stmt in body[ruled + 1 :])


def test_budget_exhaustion_is_an_engine_notification_not_a_session_signal() -> None:
    notifier = _method(_class(_tree(ENGINE), "EngineRun"), "_notify_budget_once")
    notified = [
        n.args[0].value
        for n in ast.walk(notifier)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "notify"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    ]
    assert notified == ["budget_exhausted"]


def test_schedule_reader_position_is_the_github_poll_cursor() -> None:
    assert "github_cursor" in _line("lionagi/state/schema.sql", 487)


def test_outbox_row_defaults_and_attempt_bound_anchors() -> None:
    assert "DEFAULT_MAX_ATTEMPTS" in _line(
        "lionhub/src/lionhub/orchestration/dispatch/outbox.py", 48
    )
    assert "ack_required: bool = False" in _line(
        "lionhub/src/lionhub/orchestration/dispatch/outbox.py", 166
    )
    assert "max_attempts: int = DEFAULT_MAX_ATTEMPTS" in _line(
        "lionhub/src/lionhub/orchestration/dispatch/outbox.py", 167
    )


def test_team_round_batches_pending_targets_per_completion() -> None:
    assert "pending_targets = frozenset(state.pending_targets) | frozenset(exchange_pending)" in (
        _line("lionagi/cli/orchestrate/_orchestration.py", 1597)
    )
    assert "def build_round_operations" in _line("lionagi/cli/orchestrate/_orchestration.py", 1633)
    assert "def _on_team_op_complete" in _line("lionagi/cli/orchestrate/flow.py", 2089)


def test_signal_rows_and_schedule_cursor_citations_point_at_their_code() -> None:
    assert '"github_cursor" = CASE WHEN github_cursor IS NULL' in _line("lionagi/state/db.py", 5923)
    assert "SELECT COALESCE(MAX(seq), 0) FROM session_signals" in _line("lionagi/state/db.py", 7767)
    assert "async def get_session_signals_after(" in _line("lionagi/state/db.py", 7792)
    assert "limit: int = 500," in _line("lionagi/state/db.py", 7797)


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
    assert "lionagi/state/schema.sql:828-832" in flat
    assert "lionagi/state/db.py:7767-7777" in flat
    assert "lionagi/state/db.py:7792-7809" in flat
    assert "the cursor is a position in that sequence, read forward with `after_seq`" in flat
    assert "is not a committed row and is no durable subscriber's source" in flat
    assert "whose forward read defaults to 500 rows (`:7797`)" in flat
    assert "`emitter_role`, versioned payload type" in flat
    assert "filter-required data without destructive truncation; every page is read" in flat
    assert "read through a frozen closure cutoff" in flat
    assert "`persist_dropped` is therefore binding-local dropped-plus-in-flight" in flat
    assert "zero does not prove" in flat


def test_closing_batch_triggers_and_bound_are_stated() -> None:
    flat = _flat()
    assert "Settlement is the exit of the invocation owning this subscription" in flat
    assert "A nested operation's `RunEnd` or `RunFailed` does not settle its parent" in flat
    assert "lionagi/session/branch.py:479-507" in flat
    assert "lionagi/operations/run/run.py:835-846" in flat
    assert "`unbind_db_persistence`" in flat
    assert "lionagi/session/observer.py:597-614" in flat
    assert "observer unbinding is cleanup, not settlement" in flat
    assert "keeps its tail pending for the life of the session" in flat
    assert "no timeout and no new setting" in flat
    assert "no execution attempt object" in flat
    assert "one admitted run, the unit ADR-0123 D1 names" in flat
    assert "lionagi/session/signal.py:184" in flat
    # No flow- or invocation-terminal signal exists today, and the closure path
    # is named as a prerequisite rather than described as present.
    assert "No flow- or" in flat
    assert "invocation-terminal session Signal exists today" in flat
    assert "the bus carries `RunEnd` per operation" in flat
    assert "lionagi/state/lifecycle/callbacks.py:85" in flat
    assert "lionhub/src/lionhub/orchestration/engines/flow_signals.py:148-154" in flat
    assert "so the owner-scoped finalizer that records closure is" in flat
    assert "a prerequisite delta below" in flat
    assert "the writes it tracked are known to have finished" in flat
    assert "lionagi/cli/_runs.py:1719" in flat
    assert "lionagi/studio/services/workflow_run.py:110" in flat


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
    assert (
        "the `ReviewVerdict` class and the `await run.emit(final)` in `ReviewEngine._verdict`, "
        "both in `lionhub/src/lionhub/orchestration/engines/review.py`" in flat
    )
    assert (
        "`EngineRun._notify_budget_once` in `lionhub/src/lionhub/orchestration/engines/engine.py`"
        in flat
    )
    # A line number in either citation goes stale on the next edit above it and nothing checks it.
    first = flat.index("the `ReviewVerdict` class and the `await run.emit(final)`")
    second = (
        "`EngineRun._notify_budget_once` in `lionhub/src/lionhub/orchestration/engines/engine.py`"
    )
    cited = flat[first : flat.index(second) + len(second)]
    assert re.search(r"engines/(?:review|engine)\.py:\d", cited) is None
    row_seven = next(line for line in text.splitlines() if line.startswith("| 7 |"))
    assert "budget" in row_seven
    assert "`Verdict` cast outside" in row_seven


def test_phase_two_is_a_target_and_the_gate_is_cited_where_it_still_runs() -> None:
    text = _text()
    flat = _flat()
    assert "Phase 2 already makes" not in text
    assert "Phase 2's target is to make `SessionObservation` signal-only" in flat
    assert "lionagi/session/observer.py:365-374" in flat
    assert "lionagi/session/observer.py:369-370`) and then returns at `:374" in flat
    assert "lionagi/session/observer.py:235-236" not in flat


def test_cursor_advances_in_the_state_transaction_and_borrows_only_dedup_practice() -> None:
    text = _text()
    flat = _flat()
    assert "consumed-cursor shape of ADR-0125" not in flat
    assert "ADR-0125 (Accepted) D2 step 7" in flat
    assert "is cited only for the idempotent identity per delivered input" in flat
    assert "a guarded transaction records the batch's success or" in flat
    assert "durable dead-letter and advances the cursor in the same state transaction" in flat
    assert "A failure retains the" in flat
    assert "batch and the cursor, and a dead-lettered batch stays re-queueable" in flat
    assert "joins the durable delivery contract ADR-0059 owns" not in text
    assert "reuses only ADR-0059's `dedup_key` practice" in flat


def test_batch_growth_is_bounded_by_attempt_length_not_declaration() -> None:
    flat = _flat()
    assert "bounded by declaration rather than by attempt" not in flat
    assert "The batch grows with attempt length; only the number of woken runs is bounded" in flat


def test_delivery_default_matches_the_outbox_tier_it_cites() -> None:
    flat = _flat()
    # Retryable is the default; ADR-0059's transport-success completion is the
    # contrast, not the model.
    assert "`retryable` is the default for" in flat
    assert "`ObservancePolicy`: it persists batches and retries failed consumer executions" in flat
    assert "completes at transport success without consumer acknowledgement" in flat
    assert "`best_effort` is an explicit volatile option with no durable cursor" in flat
    assert "docs/adr/ADR-0059-durable-dispatch-outbox.md:391`, `:609" in flat
    assert "`enqueue_dispatch` inserts a row unconditionally" in flat
    assert "acknowledgement is the opt-in (`:166`)" in flat
    assert "the only per-call bound is `max_attempts` (`:167`)" in flat


def test_d1_gains_a_durable_observing_subscriber_row() -> None:
    text = _text()
    flat = _flat()
    assert "The D1 vocabulary table gains no row" not in text
    assert "D1 gains one row" in flat
    assert "| `ObservingSubscriber` |" in text


def test_d3_body_carries_the_delivery_clause_the_amendment_points_at() -> None:
    flat = _flat()
    assert "**D3 gains a delivery clause.**" in flat
    assert "whose durable cursor and batch belong to its `retryable` class only" in flat
    d3 = " ".join(_text()[_text().index("### D3 —") : _text().index("### D4 —")].split())
    assert "is a delivery, not a handler call" in d3
    assert "its failure changes delivery state rather than the observed outcome" in d3


def test_code_today_names_the_batch_reader_and_schedule_cursor_for_what_they_are() -> None:
    flat = _flat()
    assert "lionagi/cli/orchestrate/_orchestration.py:1597" in flat
    assert "lionagi/state/schema.sql:487" in flat
    assert "lionagi/state/db.py:5923-5925" in flat
    assert (
        "The poller's writes advance it only forward, so a stale poll cannot move it back" in flat
    )
    assert (
        "a direct `update_schedule` or `apply_schedule_set` write sets it to the value given"
        in flat
    )
    assert "ADR-0070, Accepted" in flat
    assert "per-completion batch of targets, not a per-subscriber position" in flat
    assert "lionagi/cli/orchestrate/flow.py:2089-2134" in flat
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
    # A retryable held or closing wake coalesces; a best_effort wake on a full
    # queue is dropped and counted.
    assert "a held or closing `retryable` wake is coalesced into the pending batch" in flat
    assert "A full queue drops a `best_effort`" in flat
    assert "wake and counts it, which is that class's declared loss" in flat
    assert "`best_effort` drops and counts queue overflow" in flat
    row_three = next(line for line in text.splitlines() if line.startswith("| 3 |"))
    assert "at most four runs" in row_three
    assert "each admitted stored fact belongs to exactly one logical batch" in row_three
    assert "delivery allowed to repeat" in row_three


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


def test_amendment_uses_the_records_own_word_and_defaults_to_retryable() -> None:
    text = _text()
    flat = _amendment_flat()
    assert "falsifier" not in text
    assert "third gate arm" in flat
    assert "delivery: ObservanceDelivery = ObservanceDelivery.RETRYABLE" in text
    assert "`best_effort` is the default" not in flat
    assert "`retryable` is the subscriber's opt-in" not in flat
    assert "max_attempts: int = 3" in text
    assert "over D3's `SignalFilter` subscription conditions" in flat
    assert "Omitted, `delivery` selects `retryable`" in flat
    assert "fails explicitly rather than being silently downgraded" in flat


def test_d1_row_scopes_durability_to_the_retryable_class() -> None:
    text = _text()
    row = next(line for line in text.splitlines() if line.startswith("| `ObservingSubscriber` |"))
    assert "process subscriber behind a declared wake policy" in row
    assert "only its `retryable` delivery class has a durable cursor" in row
    assert row.rstrip().endswith("| retryable only |")


def test_failure_advances_the_cursor_only_on_success_or_a_durable_dead_letter() -> None:
    text = _text()
    flat = _flat()
    assert "A failed execution is not terminal delivery" in flat
    assert "`max_attempts` counts total automatic execution attempts, including the first" in flat
    assert "Failure retains the batch and cursor until retry succeeds" in flat
    assert "a dead-letter row is re-queueable regardless of cursor position" in flat
    assert "lionhub/src/lionhub/orchestration/dispatch/outbox.py:48`, `:506-523`, `:579-609" in flat
    row_four = next(line for line in text.splitlines() if line.startswith("| 4 |"))
    assert "advanced only on a batch's success or its durable dead-letter" in row_four
    assert "re-queueable irrespective of cursor position" in row_four
    consequences = text[text.index("### Consequences of this amendment") :]
    assert "A failure retains the" in consequences
    # the exactly-once claim and the unconditional first-run identity promise are gone
    assert "every fact delivered exactly once" not in text
    assert "answers with the first run's identity rather than executing twice" not in text
    assert "each admitted stored fact belongs to exactly one logical batch" in flat
    assert "belongs to one logical batch; delivery may repeat" in flat
    assert "exactly-once external effects require destination support" in flat


def test_source_capture_and_replay_is_a_prerequisite_delta_row() -> None:
    text = _text()
    flat = _flat()
    assert (
        "Durable delivery covers committed `session_signals` rows, not every emitted fact" in flat
    )
    assert "Registration requires a bound usable sink and replayable filters" in flat
    row_nine = next(line for line in text.splitlines() if line.startswith("| 9 |"))
    assert "capture and replay every fact a durable subscriber's filter can need" in row_nine
    assert "`emitter_role`, a versioned payload type" in row_nine
    assert "read every page through a frozen closure cutoff" in row_nine
    # the sanitizer's losses are stated rather than hidden behind the drop count
    assert "The sanitizer drops `emitter_role`" in flat
    assert "losing payload type" in flat
    assert "skips writes for the window" in flat
    assert "not a durable gap ledger" in flat


def test_d9_introduces_durable_delivery_as_proposed_and_splits_the_amendment() -> None:
    text = _text()
    d9 = text[text.index("### D9 —") : text.index("### What the code does today")]
    assert "This record decides the wake" in " ".join(d9.split())
    assert "durable delivery stays proposed behind the prerequisite delta rows" in " ".join(
        d9.split()
    )
    flat = _flat()
    assert "No second fact bus is introduced; durable batch records belong to `state/`" in flat
    assert "Wake caps bound logical batches; execution attempts have a separate bound" in flat
    assert "policy adds no second bus and no second record of its own" not in flat


def test_amendment_citations_match_the_head_line_ranges() -> None:
    flat = _flat()
    assert "idempotency/acknowledgement identity (`:493-498`)" in flat
    assert "terminal delivery (`:563`)" in flat
    assert "already-committed source fact (`:493-498`)" in flat
    assert "durable delivery state (`:493-498`)" in flat
    assert "ADR-0125:495-499" in flat
    assert "lionagi/session/branch.py:479-507`, `:1210-1263" in flat
    assert "branch.py:477-505" not in flat
    assert "`:1197-1250" not in flat
    assert ":487-494" not in flat
    assert "(`:559`)" not in flat
    assert "`:1102-1115" not in flat


def test_closing_waits_for_a_slot_without_moving_the_previous_wording() -> None:
    text = _text()
    assert "The closing batch waits for a queue slot like any held wake" not in text
    amendment = _amendment()
    assert "observer unbinding is cleanup, not" in " ".join(amendment.split())


def test_dispatch_happens_inside_emit_and_row_one_scopes_durability() -> None:
    flat = _flat()
    assert "handler runs inside `emit`, once per fact it matches" in flat
    assert "dispatch per matched fact inside `emit`" in flat
    assert "handler runs inline, once per fact it matches" not in flat
    assert "dispatch per matched fact, inline, with" not in flat
    row_one = next(line for line in _text().splitlines() if line.startswith("| 1 |"))
    assert "Only the `retryable` delivery class has a durable cursor and batch" in row_one
    assert "the `best_effort` subscriber woken with neither" in row_one


def test_owner_scoped_finalizer_is_a_prerequisite_delta_row() -> None:
    row_eight = next(line for line in _text().splitlines() if line.startswith("| 8 |"))
    assert "Prerequisite: add the owner-scoped finalizer and durable closure marker" in row_eight
    assert "a nested operation's `RunEnd` does not settle its parent" in row_eight
    assert "a restart recovers durable closure work" in row_eight
