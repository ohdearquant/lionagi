# Scheduler engine — design rationale

Notes preserved from `lionagi/studio/scheduler/engine.py` docstrings, kept as
a document rather than as inline essays.

## `_reserve_max_runs_budget`: why `inflight` is read before the await

`inflight` is read BEFORE the awaited `count_schedule_runs()` call, not
after. `release()` is deliberately lock-free — a claim must still release
from a cancelled/failing `_fire()`'s `finally` without depending on the
reservation lock, which would otherwise reintroduce cancellation-unsafe
lock-acquire-in-`finally` hazards — so a concurrent fire's claim can be
released by another task while this call is suspended awaiting the DB.

If `inflight` were read *after* that await (an intermediate design that was
tried), a fire that both persists its occurrence row and releases its claim
entirely within this call's await window would vanish from both the
persisted count (read too early, before the write) and the in-flight
snapshot (read too late, after the release) — the exact gap that
adversarial concurrency testing exploited.

Reading `inflight` first captures that other fire's claim before it can
disappear: the persisted count may still be stale, but the in-flight
snapshot backstops it, so the sum can only ever over-count (a spurious
refusal, safe and self-correcting on the next tick) — never under-count (an
actual overshoot).

## `_fire_inner`: the delivery contract's three windows

DELIVERY CONTRACT — at-least-once up to confirmed process launch,
at-most-once past it. Three windows:

1. Before the occurrence transaction commits, a crash leaves nothing
   durable, so a restart fires fresh — never a duplicate.
2. Between commit and `spawn_and_wait()` confirming launch (`on_launched`
   stamping `dispatched_at`), the row is durable but undispatched;
   `_recover_undispatched_fires()` finds it on startup and re-fires via
   `supersedes_run_id`, which routes the occurrence insert through
   `tombstone_and_replace_schedule_run()` to tombstone the orphan and insert
   the replacement atomically (its CAS also requires `dispatched_at IS
   NULL`, so a launch that gets confirmed in the race against recovery wins
   and the tombstone is a no-op).
3. Once `dispatched_at` is confirmed, the process genuinely exists and is
   never re-fired — the row is resolved by the stale-run reaper or its own
   terminal write.

This boundary is deliberate: a duplicate real-world side effect is worse
than one unretried outcome.
