import { describe, expect, it, vi, beforeEach } from "vitest";

describe("lib/runControls — controlKindFor", () => {
  it("recognizes flow, play, and agent", async () => {
    const { controlKindFor } = await import("./runControls");
    expect(controlKindFor("flow")).toBe("flow");
    expect(controlKindFor("play")).toBe("play");
    expect(controlKindFor("agent")).toBe("agent");
  });

  it("returns null for kinds the control poller does not drain (show-play, fanout, null)", async () => {
    const { controlKindFor } = await import("./runControls");
    expect(controlKindFor("show-play")).toBeNull();
    expect(controlKindFor("fanout")).toBeNull();
    expect(controlKindFor(null)).toBeNull();
    expect(controlKindFor(undefined)).toBeNull();
  });
});

describe("lib/runControls — hasAnyExecutablePath", () => {
  // Deliberately unmocked. The run detail's decision to render no control
  // section rests on this being false against the real registry; asserting it
  // only through a mock would prove the wiring and say nothing about the fact.
  it("is false today, because no verb has a backing command yet", async () => {
    const { hasAnyExecutablePath } = await import("./runControls");
    expect(hasAnyExecutablePath()).toBe(false);
  });

  it("agrees with the per-verb answer for every verb, so the two cannot drift", async () => {
    const { hasAnyExecutablePath, hasExecutablePath } = await import("./runControls");
    const verbs = ["pause", "resume", "message"] as const;
    // Guards the aggregate against the case that matters: if it were ever
    // hardcoded rather than derived, this is what would catch it.
    expect(hasAnyExecutablePath()).toBe(verbs.some((v) => hasExecutablePath(v)));
  });
});

describe("lib/runControls — derivePausePhase (the pausing-vs-paused window)", () => {
  it("reads idle when no pause has been requested, regardless of running count", async () => {
    const { derivePausePhase } = await import("./runControls");
    expect(derivePausePhase(false, 3)).toBe("idle");
    expect(derivePausePhase(false, 0)).toBe("idle");
  });

  it("reads pausing while a pause is requested and operations are still in flight", async () => {
    const { derivePausePhase } = await import("./runControls");
    expect(derivePausePhase(true, 1)).toBe("pausing");
    expect(derivePausePhase(true, 4)).toBe("pausing");
  });

  it("crosses from pausing to paused exactly when the running count reaches zero", async () => {
    const { derivePausePhase } = await import("./runControls");
    // This is the window the ADR calls out: the request has been accepted
    // but operations are still finishing. A naive `pauseRequested -> "paused"`
    // implementation would fail this test by reading "paused" while running=1.
    expect(derivePausePhase(true, 1)).toBe("pausing");
    expect(derivePausePhase(true, 0)).toBe("paused");
  });
});

// The state machines below decide from the RUN's state. Whether a verb has a
// command behind it at all is a separate fact about our command surface, and
// applyExecutablePath (tested further down) layers it on top. Keeping them
// apart is what lets these run-state rules stay reachable while every verb is
// unbacked.
describe("lib/runControls — pauseControlState", () => {
  it("is offered and enabled for a running flow with no pause requested", async () => {
    const { pauseControlState } = await import("./runControls");
    expect(pauseControlState("flow", false, "idle")).toEqual({
      offered: true,
      disabled: false,
      reasonCode: null,
    });
  });

  it("is offered and enabled for a running play with no pause requested", async () => {
    const { pauseControlState } = await import("./runControls");
    expect(pauseControlState("play", false, "idle")).toEqual({
      offered: true,
      disabled: false,
      reasonCode: null,
    });
  });

  // D4 / row 8: an agent run cannot be paused, and the control must be SHOWN
  // and DISABLED with the reason stated — never hidden.
  it("is shown and disabled, with the no-pause-seam reason, for an agent run", async () => {
    const { pauseControlState } = await import("./runControls");
    const result = pauseControlState("agent", false, "idle");
    expect(result.offered).toBe(true);
    expect(result.disabled).toBe(true);
    expect(result.reasonCode).toBe("agent-no-pause-seam");
  });

  it("stays shown and disabled for an agent run even on a terminal run (agent reason still applies)", async () => {
    const { pauseControlState } = await import("./runControls");
    const result = pauseControlState("agent", true, "idle");
    expect(result.offered).toBe(true);
    expect(result.disabled).toBe(true);
  });

  it("is disabled with a terminal reason once a flow run has ended", async () => {
    const { pauseControlState } = await import("./runControls");
    const result = pauseControlState("flow", true, "idle");
    expect(result.disabled).toBe(true);
    expect(result.reasonCode).toBe("run-terminal");
  });

  it("is disabled once a pause is already requested (pausing or paused)", async () => {
    const { pauseControlState } = await import("./runControls");
    expect(pauseControlState("flow", false, "pausing").disabled).toBe(true);
    expect(pauseControlState("flow", false, "paused").disabled).toBe(true);
  });
});

describe("lib/runControls — resumeControlState", () => {
  it("is not offered at all for an agent run (resume is not a listed agent capability)", async () => {
    const { resumeControlState } = await import("./runControls");
    expect(resumeControlState("agent", false, "idle").offered).toBe(false);
  });

  it("is offered but disabled with not-paused when a flow is running normally", async () => {
    const { resumeControlState } = await import("./runControls");
    const result = resumeControlState("flow", false, "idle");
    expect(result.offered).toBe(true);
    expect(result.disabled).toBe(true);
    expect(result.reasonCode).toBe("not-paused");
  });

  it("is offered but disabled while still pausing (gate not yet applied)", async () => {
    const { resumeControlState } = await import("./runControls");
    const result = resumeControlState("play", false, "pausing");
    expect(result.disabled).toBe(true);
    expect(result.reasonCode).toBe("still-pausing");
  });

  it("is offered and enabled once fully paused", async () => {
    const { resumeControlState } = await import("./runControls");
    expect(resumeControlState("play", false, "paused")).toEqual({
      offered: true,
      disabled: false,
      reasonCode: null,
    });
  });

  it("is disabled with a terminal reason for a terminal run even if paused", async () => {
    const { resumeControlState } = await import("./runControls");
    const result = resumeControlState("flow", true, "paused");
    expect(result.disabled).toBe(true);
    expect(result.reasonCode).toBe("run-terminal");
  });
});

describe("lib/runControls — steerControlState (row 8: steer offered on an agent run)", () => {
  it("is offered and enabled for flow, play, and agent runs alike", async () => {
    const { steerControlState } = await import("./runControls");
    for (const kind of ["flow", "play", "agent"] as const) {
      expect(steerControlState(kind, false)).toEqual({
        offered: true,
        disabled: false,
        reasonCode: null,
      });
    }
  });

  it("is disabled with a terminal reason once the run has ended", async () => {
    const { steerControlState } = await import("./runControls");
    const result = steerControlState("agent", true);
    expect(result.disabled).toBe(true);
    expect(result.reasonCode).toBe("run-terminal");
  });
});

describe("lib/runControls — hasExecutablePath / applyExecutablePath", () => {
  // Tripwire. Every one of the operator's mutating commands is accounted for
  // (prefill_schedule, launch_playbook, cancel_run, resume_run,
  // rename_session) and none of them pauses a run, releases a pause gate, or
  // delivers a steering message. When a backing command lands, its type goes
  // into COMMAND_TYPES_BY_VERB, this test fails, and whoever added it is
  // pointed straight at the refusals below that have to be revisited.
  it("reports no executable path for any of the three verbs", async () => {
    const { hasExecutablePath } = await import("./runControls");
    expect(hasExecutablePath("pause")).toBe(false);
    expect(hasExecutablePath("resume")).toBe(false);
    expect(hasExecutablePath("message")).toBe(false);
  });

  it("disables an offered control and states the reason as no-executable-path", async () => {
    const { applyExecutablePath } = await import("./runControls");
    expect(
      applyExecutablePath("pause", { offered: true, disabled: false, reasonCode: null }),
    ).toEqual({ offered: true, disabled: true, reasonCode: "no-executable-path" });
  });

  // The run-state reason implies a counterfactual that is not true: "the run
  // is not paused" tells the reader resume will work once it pauses. It will
  // not, so this refusal has to win rather than defer to the more specific
  // reason.
  it("overrides a run-state reason that would imply the control works in another state", async () => {
    const { applyExecutablePath, resumeControlState } = await import("./runControls");
    const runState = resumeControlState("flow", false, "idle");
    expect(runState.reasonCode).toBe("not-paused");
    expect(applyExecutablePath("resume", runState).reasonCode).toBe("no-executable-path");
  });

  it("never turns an unoffered control into an offered one", async () => {
    const { applyExecutablePath, resumeControlState } = await import("./runControls");
    // Resume is not a listed agent capability — it is hidden, and a refusal
    // about our command surface must not surface it.
    expect(resumeControlState("agent", false, "idle").offered).toBe(false);
    expect(applyExecutablePath("resume", resumeControlState("agent", false, "idle")).offered).toBe(
      false,
    );
  });
});

describe("lib/runControls — assertProposalSatisfies binds the proposal to what was asked for", () => {
  const proposalOf = (over: Record<string, unknown> = {}) =>
    ({
      id: "prop-1",
      commandType: "pause",
      command: { session_id: "run-abc123" },
      commandHash: "hash-1",
      risk: "mutate" as const,
      summary: "Pause run-abc123",
      idempotencyKey: "idem-1",
      expiresAt: 0,
      ...over,
    }) as never;

  // Every assertion here passes an explicit allow-set. With the real table
  // empty, the command-type check refuses first for every input, so a test
  // relying on the default would pass without the run-id rules below existing
  // at all.
  const ALLOWS_PAUSE = new Set(["pause"]);

  it("accepts a proposal whose command type is allowed and whose target is this run", async () => {
    const { assertProposalSatisfies } = await import("./runControls");
    expect(() =>
      assertProposalSatisfies(
        "pause",
        "run-abc123",
        proposalOf({ target: { kind: "session", id: "run-abc123", version: "3" } }),
        ALLOWS_PAUSE,
      ),
    ).not.toThrow();
  });

  // The substitution this whole check exists for: the turn asks in prose for
  // a pause, and the nearest tool the operator has is cancel_run.
  it("refuses a cancel proposal returned for a pause request", async () => {
    const { assertProposalSatisfies, ControlProposalMismatch } = await import("./runControls");
    expect(() =>
      assertProposalSatisfies(
        "pause",
        "run-abc123",
        proposalOf({ commandType: "cancel", summary: "Cancel run run-abc123" }),
        ALLOWS_PAUSE,
      ),
    ).toThrow(ControlProposalMismatch);
  });

  it("refuses a proposal carrying no command type at all", async () => {
    const { assertProposalSatisfies } = await import("./runControls");
    // An older server omits commandType from the proposal frame. Unverifiable
    // is not a match.
    expect(() =>
      assertProposalSatisfies(
        "pause",
        "run-abc123",
        proposalOf({ commandType: undefined }),
        ALLOWS_PAUSE,
      ),
    ).toThrow(/unknown/);
  });

  it("refuses a proposal that names no run at all", async () => {
    const { assertProposalSatisfies } = await import("./runControls");
    expect(() =>
      assertProposalSatisfies("pause", "run-abc123", proposalOf({ command: {} }), ALLOWS_PAUSE),
    ).toThrow(/does not name the run/);
  });

  it("refuses a proposal that targets a different run", async () => {
    const { assertProposalSatisfies } = await import("./runControls");
    expect(() =>
      assertProposalSatisfies(
        "pause",
        "run-abc123",
        proposalOf({ command: { session_id: "run-other" } }),
        ALLOWS_PAUSE,
      ),
    ).toThrow(/run-other/);
  });

  it("refuses when target and command name different runs, even if one of them is this run", async () => {
    const { assertProposalSatisfies } = await import("./runControls");
    expect(() =>
      assertProposalSatisfies(
        "pause",
        "run-abc123",
        proposalOf({ target: { kind: "session", id: "run-other", version: "3" } }),
        ALLOWS_PAUSE,
      ),
    ).toThrow(/run-other/);
  });
});

describe("lib/runControls — assertCommandApplied reads the status the confirm call returns", () => {
  it("passes only on succeeded", async () => {
    const { assertCommandApplied } = await import("./runControls");
    expect(() =>
      assertCommandApplied("pause", { proposalId: "p", status: "succeeded" } as never),
    ).not.toThrow();
  });

  // These arrive in a 200 body, not as a raised error, so a caller that only
  // catches throws treats every one of them as an accepted control.
  it.each(["failed", "conflict", "expired"] as const)("throws on %s", async (status) => {
    const { assertCommandApplied } = await import("./runControls");
    expect(() => assertCommandApplied("pause", { proposalId: "p", status } as never)).toThrow(
      /was not applied/,
    );
  });

  // Not a failure, but not success either: the command has not landed, and
  // reporting it as accepted is what makes a control look applied while it is
  // still in flight.
  it("throws on executing, which is in-flight rather than applied", async () => {
    const { assertCommandApplied } = await import("./runControls");
    expect(() =>
      assertCommandApplied("resume", { proposalId: "p", status: "executing" } as never),
    ).toThrow(/was not applied/);
  });

  it("surfaces the server's own error message when it sends one", async () => {
    const { assertCommandApplied } = await import("./runControls");
    expect(() =>
      assertCommandApplied("message", {
        proposalId: "p",
        status: "failed",
        error: { code: "gone", message: "run already exited", retryable: false },
      } as never),
    ).toThrow(/run already exited/);
  });
});

describe("lib/runControls — controlInstructionText", () => {
  it("names the run id explicitly so the operator does not have to disambiguate 'this run'", async () => {
    const { controlInstructionText } = await import("./runControls");
    expect(controlInstructionText("flow", "pause", "run-abc123")).toContain("run-abc123");
    expect(controlInstructionText("play", "resume", "run-abc123")).toContain("run-abc123");
  });

  it("carries the steer message text verbatim", async () => {
    const { controlInstructionText } = await import("./runControls");
    const text = controlInstructionText("agent", "message", "run-abc123", "check the test output");
    expect(text).toContain("check the test output");
    expect(text).toContain("run-abc123");
  });
});

describe("lib/runControls — proposeRunControl / confirmRunControl route through the operator proposal path, not a bespoke endpoint", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("creates a conversation, submits a turn carrying the run id, and resolves with the proposal frame the turn produced", async () => {
    vi.doMock("@/lib/api", () => ({
      createOperatorConversation: vi.fn().mockResolvedValue({ id: "conv-1" }),
      submitOperatorTurn: vi.fn().mockResolvedValue({
        conversationId: "conv-1",
        requestId: "req-1",
        acceptedSequence: 1,
      }),
      streamOperatorConversation: vi.fn((_conversationId, _after, handlers) => {
        queueMicrotask(() =>
          handlers.onFrame({
            version: 1,
            conversationId: "conv-1",
            requestId: "req-1",
            sequence: 2,
            type: "proposal",
            payload: {
              proposal: {
                id: "prop-1",
                command: { verb: "pause" },
                commandHash: "hash-1",
                risk: "mutate",
                summary: "Pause run-abc123",
                idempotencyKey: "idem-1",
                expiresAt: Date.now() + 60_000,
              },
            },
            createdAt: Date.now(),
          }),
        );
        return () => {};
      }),
      confirmOperatorProposal: vi
        .fn()
        .mockResolvedValue({ proposalId: "prop-1", status: "succeeded" }),
    }));

    const { proposeRunControl, confirmRunControl } = await import("./runControls");
    const api = await import("@/lib/api");

    const result = await proposeRunControl("run-abc123", "flow", "pause");

    expect(api.createOperatorConversation).toHaveBeenCalledTimes(1);
    const turnArgs = vi.mocked(api.submitOperatorTurn).mock.calls[0];
    expect(turnArgs[0]).toBe("conv-1");
    expect(turnArgs[1].instruction).toContain("run-abc123");
    expect(result.conversationId).toBe("conv-1");
    expect(result.proposal.id).toBe("prop-1");

    // This proposal carries no commandType, which is what a server predating
    // the frame change sends. Confirming refuses and — the part that matters —
    // never reaches the confirm endpoint, so nothing is applied.
    await expect(
      confirmRunControl("pause", "run-abc123", result.conversationId, result.proposal),
    ).rejects.toThrow(/Refusing to confirm/);
    expect(api.confirmOperatorProposal).not.toHaveBeenCalled();
  });

  it("rejects when the turn ends with an error frame instead of a proposal", async () => {
    vi.doMock("@/lib/api", () => ({
      createOperatorConversation: vi.fn().mockResolvedValue({ id: "conv-2" }),
      submitOperatorTurn: vi.fn().mockResolvedValue({
        conversationId: "conv-2",
        requestId: "req-2",
        acceptedSequence: 1,
      }),
      streamOperatorConversation: vi.fn((_conversationId, _after, handlers) => {
        queueMicrotask(() =>
          handlers.onFrame({
            version: 1,
            conversationId: "conv-2",
            requestId: "req-2",
            sequence: 2,
            type: "error",
            payload: { error: { code: "refused", message: "not allowed", retryable: false } },
            createdAt: Date.now(),
          }),
        );
        return () => {};
      }),
      confirmOperatorProposal: vi.fn(),
    }));

    const { proposeRunControl } = await import("./runControls");
    await expect(
      proposeRunControl("run-xyz", "agent", "message", { message: "hi" }),
    ).rejects.toThrow("not allowed");
  });
});
