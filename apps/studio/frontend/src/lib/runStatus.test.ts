import { describe, it, expect } from "vitest";
import {
  deriveDisplayStatus,
  deriveVerdict,
  isEffectivelyActive,
  isOrphanedReason,
  isUnsuccessfulTerminal,
} from "./runStatus";

describe("deriveDisplayStatus", () => {
  it("maps a phantom-reaped run to orphaned, not failed — even though raw status is failed", () => {
    const run = {
      status: "failed",
      status_reason_code: "session.orphaned.no_process",
      status_reason_summary: "phantom_reaped",
    };
    expect(deriveDisplayStatus(run)).toBe("orphaned");
  });

  it("treats any phantom-reaped reason_summary as orphaned regardless of the specific reason_code", () => {
    const run = {
      status: "failed",
      status_reason_code: "session.phantom.process_dead",
      status_reason_summary: "phantom_reaped",
    };
    expect(deriveDisplayStatus(run)).toBe("orphaned");
  });

  it("does not orphan a zombie (stale-locks) — that stays a real failure", () => {
    const run = {
      status: "failed",
      status_reason_code: "session.zombie.stale_locks",
      status_reason_summary: "phantom_reaped",
    };
    expect(deriveDisplayStatus(run)).toBe("failed");
  });

  it("a completed run with a request-changes verdict is completed, not failed — the list/detail bug this closes", () => {
    // The verdict itself is a separate axis (see deriveVerdict tests below);
    // this only asserts the STATUS side never gets dragged down by outcome.
    const run = { status: "completed" };
    expect(deriveDisplayStatus(run)).toBe("completed");
  });

  it("a genuine failure stays failed", () => {
    expect(deriveDisplayStatus({ status: "failed" })).toBe("failed");
    expect(deriveDisplayStatus({ status: "error" })).toBe("failed");
  });

  it("maps running aliases to running", () => {
    expect(deriveDisplayStatus({ status: "running" })).toBe("running");
    expect(deriveDisplayStatus({ status: "executing" })).toBe("running");
    expect(deriveDisplayStatus({ status: "director-managed" })).toBe("running");
  });

  it("maps queued aliases to queued", () => {
    expect(deriveDisplayStatus({ status: "queued" })).toBe("queued");
    expect(deriveDisplayStatus({ status: "pending" })).toBe("queued");
  });

  it("maps cancellation aliases to cancelled", () => {
    expect(deriveDisplayStatus({ status: "cancelled" })).toBe("cancelled");
    expect(deriveDisplayStatus({ status: "aborted" })).toBe("cancelled");
    expect(deriveDisplayStatus({ status: "timed_out" })).toBe("cancelled");
  });

  it("is case/whitespace insensitive on the raw status", () => {
    expect(deriveDisplayStatus({ status: "  FAILED  " })).toBe("failed");
  });

  it("falls back to running for an unrecognized status", () => {
    expect(deriveDisplayStatus({ status: "some_new_status" })).toBe("running");
  });
});

describe("isOrphanedReason", () => {
  it("true for phantom_reaped summary", () => {
    expect(isOrphanedReason({ status: "failed", status_reason_summary: "phantom_reaped" })).toBe(
      true,
    );
  });

  it("false when the reason_code is zombie even if summary matches", () => {
    expect(
      isOrphanedReason({
        status: "failed",
        status_reason_code: "session.zombie.stale_locks",
        status_reason_summary: "phantom_reaped",
      }),
    ).toBe(false);
  });

  it("false for an ordinary failure", () => {
    expect(isOrphanedReason({ status: "failed", status_reason_summary: "exit code 1" })).toBe(
      false,
    );
  });
});

describe("deriveVerdict", () => {
  it("normalizes review-engine vocabulary into the closed Verdict union", () => {
    expect(deriveVerdict("APPROVE")).toBe("approve");
    expect(deriveVerdict("APPROVE-WITH-FIXES")).toBe("approve-with-fixes");
    expect(deriveVerdict("REQUEST-CHANGES")).toBe("request-changes");
    expect(deriveVerdict("REQUEST_CHANGES")).toBe("request-changes");
    expect(deriveVerdict("REJECT")).toBe("reject");
  });

  it("renders no verdict for missing or unrecognized input — never fabricates one", () => {
    expect(deriveVerdict(null)).toBe("none");
    expect(deriveVerdict(undefined)).toBe("none");
    expect(deriveVerdict("")).toBe("none");
    expect(deriveVerdict("some random assistant sentence mentioning reject casually")).toBe("none");
  });
});

describe("isEffectivelyActive", () => {
  it("treats stale/orphaned/zombie running rows as inactive", () => {
    expect(isEffectivelyActive({ status: "running", effective_health: "stale" })).toBe(false);
    expect(isEffectivelyActive({ status: "running", effective_health: "orphaned" })).toBe(false);
    expect(isEffectivelyActive({ status: "running", effective_health: "zombie" })).toBe(false);
  });

  it("keeps healthy, idle, and unresponsive running rows active", () => {
    expect(isEffectivelyActive({ status: "running", effective_health: "healthy" })).toBe(true);
    expect(isEffectivelyActive({ status: "running", effective_health: "idle" })).toBe(true);
    // unresponsive means alive but quiet, not dead — stays active.
    expect(isEffectivelyActive({ status: "running", effective_health: "unresponsive" })).toBe(true);
  });

  it("keeps a running row active when effective_health is absent (unknown liveness)", () => {
    expect(isEffectivelyActive({ status: "running" })).toBe(true);
  });

  it("queued rows remain active regardless of effective_health", () => {
    expect(isEffectivelyActive({ status: "queued", effective_health: "stale" })).toBe(true);
  });

  it("non-active display statuses are inactive", () => {
    expect(isEffectivelyActive({ status: "completed" })).toBe(false);
    expect(isEffectivelyActive({ status: "failed" })).toBe(false);
  });
});

describe("isUnsuccessfulTerminal", () => {
  // The case this predicate exists for. A run that blows its deadline arrives
  // as `timed_out` and DISPLAYS as "cancelled", so anything keyed on
  // `status === "failed"` drops its reason — which is how a timed-out run ends
  // up showing a bare "cancelled" while the backend's own explanation goes
  // unread.
  it("covers a timed-out run, which is never spelled 'failed'", () => {
    expect(isUnsuccessfulTerminal({ status: "timed_out" })).toBe(true);
    expect(isUnsuccessfulTerminal({ status: "timeout" })).toBe(true);
    expect(isUnsuccessfulTerminal({ status: "cancelled" })).toBe(true);
    expect(isUnsuccessfulTerminal({ status: "aborted" })).toBe(true);
  });

  it("covers failed and orphaned terminals", () => {
    expect(isUnsuccessfulTerminal({ status: "failed" })).toBe(true);
    expect(isUnsuccessfulTerminal({ status: "error" })).toBe(true);
    expect(
      isUnsuccessfulTerminal({ status: "failed", status_reason_summary: "phantom_reaped" }),
    ).toBe(true);
  });

  it("excludes success and anything still going", () => {
    expect(isUnsuccessfulTerminal({ status: "completed" })).toBe(false);
    expect(isUnsuccessfulTerminal({ status: "success" })).toBe(false);
    expect(isUnsuccessfulTerminal({ status: "running" })).toBe(false);
    expect(isUnsuccessfulTerminal({ status: "queued" })).toBe(false);
    // An unrecognized status derives to "running", so it is not yet terminal.
    expect(isUnsuccessfulTerminal({ status: "some-new-state" })).toBe(false);
  });
});
