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
  | "still-pausing"
  /** No command exists that would carry this verb out, so the control is
   * shown and refused rather than offered and unable to deliver. See
   * COMMAND_TYPES_BY_VERB for which verbs are backed. */
  | "no-executable-path";

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

/** Whether any command exists that would actually carry this verb out. When
 * none does, the control is refused before a conversation is ever opened —
 * the propose step sends a natural-language instruction, so an unbacked verb
 * does not fail cleanly, it comes back as some other command. Refusing early
 * is what keeps that substitution from ever reaching a confirm dialog. */
export function hasExecutablePath(verb: ControlVerb): boolean {
  return COMMAND_TYPES_BY_VERB[verb].size > 0;
}

/** Layers the surface-wide fact that a verb has no backing command on top of
 * whatever the run-state machine below decided.
 *
 * Deliberately separate from those state machines rather than folded into
 * them. Two reasons. It keeps every run-state rule reachable and tested
 * instead of shadowed by a refusal that currently fires first for every input.
 * And it makes this refusal WIN over the run-state reasons, which is what the
 * reader needs: "The run is not paused" on a resume button implies resume will
 * work once the run pauses, and that is not true of a verb nothing can carry
 * out. A verb the state machine did not offer at all stays unoffered — this
 * disables controls, it never adds one.
 *
 * The moment a backing command's type is added to COMMAND_TYPES_BY_VERB this
 * becomes a no-op and the specific run-state reason resurfaces on its own. */
export function applyExecutablePath(verb: ControlVerb, state: ControlState): ControlState {
  if (!state.offered || hasExecutablePath(verb)) return state;
  return offeredState(true, "no-executable-path");
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

/** Command types that legitimately satisfy each control verb.
 *
 * Every set is empty, and that is a finding rather than a placeholder. The
 * operator's mutating command set is prefill_schedule, launch_playbook,
 * cancel_run, resume_run and rename_session; none of them pauses a run,
 * releases a pause gate, or delivers a steering message. `resume_run` is the
 * trap: it carries command type "resume" and its own docstring says it is a
 * distinct operation from un-pausing a paused run, so it matches this verb by
 * name while doing something else.
 *
 * A control command therefore rides a natural-language instruction to a model
 * that has no tool for it, and the nearest available match to "stop this run"
 * is cancel_run. Until a backing command exists, every proposal returned for a
 * control verb is the wrong command, and refusing it is the only correct
 * outcome. Adding the backing command means adding its type here, and the
 * checks below start passing without further change. */
const COMMAND_TYPES_BY_VERB: Record<ControlVerb, ReadonlySet<string>> = {
  pause: new Set(),
  resume: new Set(),
  message: new Set(),
};

/** A returned proposal does not match the control that was requested. Thrown
 * before anything is confirmed, so the run is never mutated. */
export class ControlProposalMismatch extends Error {
  constructor(
    readonly verb: ControlVerb,
    readonly proposalCommandType: string,
    reason: string,
  ) {
    super(reason);
    this.name = "ControlProposalMismatch";
  }
}

/** The run a proposal would actually act on, or null when it names none.
 * `target.id` is the resource the store resolved; `command.session_id` is what
 * the command itself carries. Either identifies the run, so a proposal is
 * bound when one of them matches and neither names a different run. */
function proposalRunIds(proposal: OperatorCommandProposal): string[] {
  const ids: string[] = [];
  if (proposal.target?.id) ids.push(proposal.target.id);
  const fromCommand = proposal.command?.session_id;
  if (typeof fromCommand === "string" && fromCommand) ids.push(fromCommand);
  return ids;
}

/** Checks a returned proposal against the control that asked for it, throwing
 * rather than returning a boolean so no caller can proceed by ignoring the
 * result. The proposal arrives from a model round-trip, so neither its command
 * nor its target is guaranteed to be what was requested — binding them here is
 * what keeps "confirm pause" from executing some other mutation. */
export function assertProposalSatisfies(
  verb: ControlVerb,
  runId: string,
  proposal: OperatorCommandProposal,
  /** The command types that satisfy `verb`. Defaults to the table above, which
   * is empty for every verb today — meaning every call refuses at the first
   * check, and a test asserting any of the later refusals would pass no matter
   * what this function did with run ids. Passing a set explicitly is what keeps
   * each rule here separately falsifiable, and is what a backing command's own
   * tests use before its type is added to the table. */
  allowed: ReadonlySet<string> = COMMAND_TYPES_BY_VERB[verb],
): void {
  const commandType = proposal.commandType ?? "";
  if (!allowed.has(commandType)) {
    throw new ControlProposalMismatch(
      verb,
      commandType,
      `Refusing to confirm: asked to ${verb} this run, but the proposed command is "${commandType || "unknown"}".`,
    );
  }
  const runIds = proposalRunIds(proposal);
  if (runIds.length === 0) {
    throw new ControlProposalMismatch(
      verb,
      commandType,
      "Refusing to confirm: the proposed command does not name the run it would act on.",
    );
  }
  const other = runIds.find((id) => id !== runId);
  if (other) {
    throw new ControlProposalMismatch(
      verb,
      commandType,
      `Refusing to confirm: the proposed command targets run ${other}, not this run.`,
    );
  }
}

/** Throws unless the confirm call reported the command as actually applied.
 *
 * The status field is load-bearing: the API reports "failed", "conflict" and
 * "expired" in a 200 body rather than by raising, so a caller that only
 * catches thrown errors reports every one of those as an accepted control.
 * "executing" is not success either — the command has not landed yet, and
 * treating it as accepted is what makes a control look applied while it is
 * still in flight. */
export function assertCommandApplied(verb: ControlVerb, result: OperatorProposalResult): void {
  if (result.status === "succeeded") return;
  const detail = result.error?.message ?? result.status;
  throw new Error(`The ${verb} command was not applied: ${detail}`);
}

/** Confirms a control proposal after binding it to the verb and run that were
 * requested, and after reading the result the confirm call returns. Both
 * checks throw rather than returning a value a caller can ignore. */
export async function confirmRunControl(
  verb: ControlVerb,
  runId: string,
  conversationId: string,
  proposal: OperatorCommandProposal,
): Promise<OperatorProposalResult> {
  assertProposalSatisfies(verb, runId, proposal);
  const result = await confirmOperatorProposal(
    conversationId,
    proposal.id,
    proposal.commandHash,
    proposal.target?.version ?? null,
  );
  assertCommandApplied(verb, result);
  return result;
}
