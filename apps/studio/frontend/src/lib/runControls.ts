/**
 * Run control commands (pause / resume-gate / steer) for the run detail view.
 *
 * Mirrors the consumer-kind table in lionagi/cli/orchestrate/_control.py
 * (`_CONSUMER_KINDS_BY_VERB`): a flow or play run drains all three verbs; an
 * agent run drains only `message` (a steer lands as a warm continuation at
 * the next turn boundary — there is no pause seam inside a single
 * `operate()` call). Commands are never sent directly: they ride the
 * ADR-0083 operator conversation → proposal → confirm path unchanged, so
 * every control command gets the same audit trail as any other operator
 * command.
 */
import {
  confirmOperatorProposal,
  createOperatorConversation,
  submitOperatorTurn,
  streamOperatorConversation,
} from "@/lib/api";
import type {
  OperatorCommandProposal,
  OperatorContextSnapshot,
  OperatorDonePayload,
  OperatorErrorPayload,
  OperatorProposalPayload,
  OperatorProposalResult,
} from "@/lib/types";

export type ControlKind = "flow" | "play" | "agent";
export type ControlVerb = "pause" | "resume" | "message";

const CONSUMER_KINDS_BY_VERB: Record<ControlVerb, ReadonlySet<ControlKind>> = {
  pause: new Set(["flow", "play"]),
  resume: new Set(["flow", "play"]),
  message: new Set(["flow", "play", "agent"]),
};

/** session.invocation_kind → a kind the control poller recognizes, or null
 * for a kind this ADR does not cover (e.g. show-play, fanout, a mirrored
 * import) — the server enqueues nothing for those, so no control surface is
 * offered rather than offering one that would be refused. */
export function controlKindFor(invocationKind: string | null | undefined): ControlKind | null {
  return invocationKind === "flow" || invocationKind === "play" || invocationKind === "agent"
    ? invocationKind
    : null;
}

export type ControlReasonCode =
  | "run-terminal"
  | "agent-no-pause-seam"
  | "already-pause-requested"
  | "not-paused"
  | "still-pausing";

export interface ControlState {
  /** Whether the control renders at all. A kind this ADR does not cover
   * (controlKindFor returned null, or resume/pause is not in the verb's
   * consumer-kind set for this kind) is not offered — false here means
   * "render nothing," never "render disabled." */
  offered: boolean;
  /** Offered but not clickable right now, with reasonCode explaining why.
   * Per D4, an agent run's pause control is offered=true, disabled=true —
   * shown, not hidden, so the engine constraint reads as deliberate. */
  disabled: boolean;
  reasonCode: ControlReasonCode | null;
}

function offeredState(
  disabled: boolean,
  reasonCode: ControlReasonCode | null = null,
): ControlState {
  return { offered: true, disabled, reasonCode };
}

const NOT_OFFERED: ControlState = { offered: false, disabled: true, reasonCode: null };

export type PausePhase = "idle" | "pausing" | "paused";

/** Pause is soft: a requested pause does not become "paused" until every
 * operation already admitted has finished. `runningCount` is the same
 * progress-counts.running the graph and progress bar already read, so this
 * can never disagree with what the canvas shows. */
export function derivePausePhase(pauseRequested: boolean, runningCount: number): PausePhase {
  if (!pauseRequested) return "idle";
  return runningCount > 0 ? "pausing" : "paused";
}

export function pauseControlState(
  kind: ControlKind,
  runTerminal: boolean,
  pausePhase: PausePhase,
): ControlState {
  if (!CONSUMER_KINDS_BY_VERB.pause.has(kind)) {
    // Only "agent" falls here among the three recognized kinds — this is the
    // shown-and-disabled refusal D4 requires, not an omission.
    return offeredState(true, "agent-no-pause-seam");
  }
  if (runTerminal) return offeredState(true, "run-terminal");
  if (pausePhase !== "idle") return offeredState(true, "already-pause-requested");
  return offeredState(false);
}

export function resumeControlState(
  kind: ControlKind,
  runTerminal: boolean,
  pausePhase: PausePhase,
): ControlState {
  if (!CONSUMER_KINDS_BY_VERB.resume.has(kind)) return NOT_OFFERED;
  if (runTerminal) return offeredState(true, "run-terminal");
  if (pausePhase === "paused") return offeredState(false);
  if (pausePhase === "pausing") return offeredState(true, "still-pausing");
  return offeredState(true, "not-paused");
}

export function steerControlState(kind: ControlKind, runTerminal: boolean): ControlState {
  if (!CONSUMER_KINDS_BY_VERB.message.has(kind)) return NOT_OFFERED;
  if (runTerminal) return offeredState(true, "run-terminal");
  return offeredState(false);
}

/** Deterministic instruction text sent as the operator turn — the run id is
 * spelled out in the text itself so the operator model never has to
 * disambiguate "this run" from page context alone. */
export function controlInstructionText(
  kind: ControlKind,
  verb: ControlVerb,
  runId: string,
  message?: string,
): string {
  const label = kind === "agent" ? "agent run" : `${kind} run`;
  if (verb === "pause") {
    return `Pause the ${label} ${runId}. Let in-flight operations finish; do not start anything new.`;
  }
  if (verb === "resume") {
    return `Resume the ${label} ${runId} by releasing its pause gate.`;
  }
  return `Deliver this message to the ${label} ${runId} as a steering continuation at the next turn boundary: ${(message ?? "").trim()}`;
}

function controlContext(runId: string): OperatorContextSnapshot {
  return {
    space: "history",
    route: `/history?s=${encodeURIComponent(runId)}`,
    selection: { s: runId },
    filters: { s: runId },
  };
}

export interface RunControlProposal {
  conversationId: string;
  proposal: OperatorCommandProposal;
}

/** Waits for the turn just submitted to produce a `proposal` frame. Rejects
 * on an `error` frame, on a `done` frame with no proposal (nothing to
 * confirm), or after `timeoutMs` with no signal at all. */
function waitForProposal(
  conversationId: string,
  afterSequence: number,
  timeoutMs = 30_000,
): Promise<OperatorCommandProposal> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      close();
      reject(new Error("Timed out waiting for a command proposal."));
    }, timeoutMs);
    const close = streamOperatorConversation(conversationId, Math.max(0, afterSequence - 1), {
      onFrame: (frame) => {
        if (settled) return;
        if (frame.type === "proposal") {
          settled = true;
          clearTimeout(timer);
          close();
          resolve((frame.payload as OperatorProposalPayload).proposal);
        } else if (frame.type === "error") {
          settled = true;
          clearTimeout(timer);
          close();
          reject(new Error((frame.payload as OperatorErrorPayload).error.message));
        } else if (frame.type === "done") {
          const outcome = (frame.payload as OperatorDonePayload).outcome;
          if (outcome !== "completed") {
            settled = true;
            clearTimeout(timer);
            close();
            reject(new Error(`Command turn ended without a proposal (${outcome}).`));
          }
        }
      },
    });
  });
}

/** Submits a control command through a fresh operator conversation and
 * returns the proposal it produced — not yet applied. The caller confirms
 * (confirmRunControl) or lets it expire; this never applies a command on its
 * own, matching ADR-0083's propose-then-confirm safety contract. */
export async function proposeRunControl(
  runId: string,
  kind: ControlKind,
  verb: ControlVerb,
  options?: { message?: string },
): Promise<RunControlProposal> {
  const conversation = await createOperatorConversation({
    title: `${verb} · ${runId.slice(0, 8)}`,
  });
  const accepted = await submitOperatorTurn(conversation.id, {
    instruction: controlInstructionText(kind, verb, runId, options?.message),
    context: controlContext(runId),
    expectedLastSequence: 0,
  });
  const proposal = await waitForProposal(conversation.id, accepted.acceptedSequence);
  return { conversationId: conversation.id, proposal };
}

export async function confirmRunControl(
  conversationId: string,
  proposal: OperatorCommandProposal,
): Promise<OperatorProposalResult> {
  return confirmOperatorProposal(
    conversationId,
    proposal.id,
    proposal.commandHash,
    proposal.target?.version ?? null,
  );
}
