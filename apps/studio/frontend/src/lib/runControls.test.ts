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

    await confirmRunControl(result.conversationId, result.proposal);
    expect(api.confirmOperatorProposal).toHaveBeenCalledWith("conv-1", "prop-1", "hash-1", null);
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
