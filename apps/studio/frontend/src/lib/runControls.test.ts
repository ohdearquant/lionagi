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
  // These two are the cases where the run itself has no objection: a live flow
  // or play, no pause requested. They were the enabled state, and they are the
  // exact cases the missing operator command has to speak up in — a live pause
  // button here submits a turn to an operator that has no tool for it, and the
  // proposal that comes back is some other mutation of the run. Enabled again
  // is correct the moment a tool performs the verb, and not before.
  it("is offered but disabled for a running flow, because no operator command pauses", async () => {
    const { pauseControlState } = await import("./runControls");
    expect(pauseControlState("flow", false, "idle")).toEqual({
      offered: true,
      disabled: true,
      reasonCode: "no-operator-command",
    });
  });

  it("is offered but disabled for a running play, for the same reason", async () => {
    const { pauseControlState } = await import("./runControls");
    expect(pauseControlState("play", false, "idle")).toEqual({
      offered: true,
      disabled: true,
      reasonCode: "no-operator-command",
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

  // Fully paused is where resume would go live, so it is where the missing
  // command surfaces. Worth stating plainly: the operator does own a tool named
  // `resume_run`, and it is not this. It launches a fresh invocation rather
  // than releasing a pause gate, which is why an entry for the resume verb
  // cannot simply point at it.
  it("is offered but disabled once fully paused, because no operator command releases a pause gate", async () => {
    const { resumeControlState } = await import("./runControls");
    expect(resumeControlState("play", false, "paused")).toEqual({
      offered: true,
      disabled: true,
      reasonCode: "no-operator-command",
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
  // Steer stays OFFERED on all three kinds, which is what row 8 asks for and
  // what distinguishes this from hiding the control: the capability table still
  // says an agent run can be steered. It is disabled only because nothing
  // carries the message yet.
  it("is offered for flow, play, and agent runs alike, and disabled on all three for want of a command", async () => {
    const { steerControlState } = await import("./runControls");
    for (const kind of ["flow", "play", "agent"] as const) {
      expect(steerControlState(kind, false)).toEqual({
        offered: true,
        disabled: true,
        reasonCode: "no-operator-command",
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

  // NOTE for whoever adds the first operator control tool: the submit-then-wait
  // path in proposeRunControl (including waitForProposal's error and timeout
  // arms) is deliberately unexercised end-to-end right now, because no verb has
  // a command type and every call refuses before it opens a conversation. Add
  // the map entry and this block gets its round-trip test back — that test is
  // owed at the same time as the tool, not later.

  it("refuses every control verb without opening a conversation, because no operator command performs one", async () => {
    const created = vi.fn().mockResolvedValue({ id: "conv-1" });
    const submitted = vi.fn();
    vi.doMock("@/lib/api", () => ({
      createOperatorConversation: created,
      submitOperatorTurn: submitted,
      streamOperatorConversation: vi.fn(),
      confirmOperatorProposal: vi.fn(),
    }));

    const { proposeRunControl } = await import("./runControls");

    for (const verb of ["pause", "resume", "message"] as const) {
      await expect(proposeRunControl("run-abc123", "flow", verb)).rejects.toThrow(
        /No operator command performs/,
      );
    }
    // The refusal has to land before the turn is spent, not after waiting it out.
    expect(created).not.toHaveBeenCalled();
    expect(submitted).not.toHaveBeenCalled();
  });
});

describe("lib/runControls — assertProposalMatches binds a proposal to what was asked for", () => {
  const proposal = (over: Record<string, unknown> = {}) =>
    ({
      id: "prop-1",
      commandType: "pause",
      command: { run_id: "run-abc123" },
      commandHash: "hash-1",
      risk: "mutate" as const,
      summary: "Pause run-abc123",
      idempotencyKey: "idem-1",
      expiresAt: Date.now() + 60_000,
      ...over,
    }) as never;

  it("accepts a proposal whose command type and run both match the request", async () => {
    const { assertProposalMatches } = await import("./runControls");
    expect(() => assertProposalMatches(proposal(), "pause", "run-abc123")).not.toThrow();
  });

  it("refuses a cancel proposal returned for a pause request", async () => {
    // The case this whole check exists for. Cancelling is the nearest thing an
    // operator with no pause tool can reach, the summary it arrives with is
    // truthful, and it names the very run the user was looking at — so nothing
    // except the command type separates it from the click that was made.
    const { assertProposalMatches } = await import("./runControls");
    expect(() =>
      assertProposalMatches(proposal({ commandType: "cancel" }), "pause", "run-abc123"),
    ).toThrow(/Refused a proposal for "cancel" when "pause" was requested/);
  });

  it("refuses a proposal that would act on a different run", async () => {
    const { assertProposalMatches } = await import("./runControls");
    expect(() =>
      assertProposalMatches(proposal({ command: { run_id: "run-other" } }), "pause", "run-abc123"),
    ).toThrow(/targeting a different run/);
  });

  it("refuses a proposal that never names the run it would act on", async () => {
    const { assertProposalMatches } = await import("./runControls");
    expect(() => assertProposalMatches(proposal({ command: {} }), "pause", "run-abc123")).toThrow(
      /does not name the run/,
    );
  });
});

describe("lib/runControls — confirmRunControl treats a non-applied status as a failure", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  const proposal = {
    id: "prop-1",
    commandType: "pause",
    command: { run_id: "run-abc123" },
    commandHash: "hash-1",
    risk: "mutate",
    summary: "Pause run-abc123",
    idempotencyKey: "idem-1",
    expiresAt: Date.now() + 60_000,
  } as never;

  it("returns the result when the command was applied", async () => {
    vi.doMock("@/lib/api", () => ({
      createOperatorConversation: vi.fn(),
      submitOperatorTurn: vi.fn(),
      streamOperatorConversation: vi.fn(),
      confirmOperatorProposal: vi
        .fn()
        .mockResolvedValue({ proposalId: "prop-1", status: "succeeded" }),
    }));
    const { confirmRunControl } = await import("./runControls");
    const api = await import("@/lib/api");

    await expect(confirmRunControl("conv-1", proposal)).resolves.toMatchObject({
      status: "succeeded",
    });
    expect(api.confirmOperatorProposal).toHaveBeenCalledWith("conv-1", "prop-1", "hash-1", null);
  });

  // Each of these resolves rather than rejecting at the API layer, so before
  // this check the caller ran its success path and told the user the run was
  // paused when the command had been refused, lost a race, or timed out.
  it.each(["failed", "conflict", "expired"] as const)(
    "throws instead of reporting success when the confirmation came back %s",
    async (status) => {
      vi.doMock("@/lib/api", () => ({
        createOperatorConversation: vi.fn(),
        submitOperatorTurn: vi.fn(),
        streamOperatorConversation: vi.fn(),
        confirmOperatorProposal: vi.fn().mockResolvedValue({
          proposalId: "prop-1",
          status,
          error: { code: status, message: `the run was not paused (${status})`, retryable: false },
        }),
      }));
      const { confirmRunControl } = await import("./runControls");

      await expect(confirmRunControl("conv-1", proposal)).rejects.toThrow(
        `the run was not paused (${status})`,
      );
    },
  );

  it("still throws when the refusal carries no message to quote", async () => {
    vi.doMock("@/lib/api", () => ({
      createOperatorConversation: vi.fn(),
      submitOperatorTurn: vi.fn(),
      streamOperatorConversation: vi.fn(),
      confirmOperatorProposal: vi
        .fn()
        .mockResolvedValue({ proposalId: "prop-1", status: "expired" }),
    }));
    const { confirmRunControl } = await import("./runControls");

    await expect(confirmRunControl("conv-1", proposal)).rejects.toThrow(
      "The command was not applied (expired).",
    );
  });
});
