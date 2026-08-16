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
  /** Nothing is running that would drain a control for this session. A
   * mirrored or imported agent run carries invocation_kind "agent" like a live
   * one, but no lionagi runner owns it, so the server refuses every control
   * queued against it. Offered and disabled rather than hidden, on the same
   * reasoning as agent-no-pause-seam: the limit is a property of this run, and
   * a reader who sees no control at all cannot tell that from a missing
   * feature. */
  | "no-live-consumer"
  /** No command exists that would carry this verb out, so the control is
   * shown and refused rather than offered and unable to deliver. See
   * COMMAND_TYPES_BY_VERB for which verbs are backed. */
  | "no-executable-path"
  /** The run carries no project, so no control against it can be authorized.
   * The server scopes a control by the project of the conversation it was
   * proposed in, and a run with no project of its own gives that conversation
   * nothing to be scoped to. Shown disabled rather than hidden, like the other
   * property-of-this-run refusals. */
  | "no-project-scope";

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
 * can never disagree with what the canvas shows.
 *
 * `null` means that count is UNKNOWN, which is not the same as zero and must
 * not collapse into it. A run with no authored graph has nothing for the
 * progress counter to count, so it would hand over zero and settle straight
 * to "paused" while its operations are still in flight — offering Resume
 * before the pause gate has drained anything. Unknown holds at "pausing"
 * instead, the phase that claims nothing about what has finished.
 *
 * `pauseEstablished` says the gate is already installed as far as the server
 * is concerned, rather than requested here a moment ago. It changes only the
 * unknown-count case, and it has to: holding an established gate at "pausing"
 * keeps Resume disabled for as long as the count stays unknown, which on a run
 * with no authored graph is permanently — the reader is left with a paused run
 * and no way to release it, which is the state this argument exists to avoid.
 * A fresh request keeps the cautious reading, since there the unknown really
 * is unknown. */
export function derivePausePhase(
  pauseRequested: boolean,
  runningCount: number | null,
  pauseEstablished = false,
): PausePhase {
  if (!pauseRequested) return "idle";
  if (runningCount === null) return pauseEstablished ? "paused" : "pausing";
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

/** Whether any control verb at all has a backing command.
 *
 * When none does, the run detail renders no control section rather than a
 * section in which every control is disabled. A surface whose every verb
 * refuses reads as a broken feature, while its absence reads as one not built
 * yet, and only the second is true. The per-verb refusal above stays exactly
 * as it is: it is what protects the mixed state, where some verbs are backed
 * and others are not.
 *
 * The verb list comes from the registry itself rather than a second literal,
 * so the two can never disagree, and the section reappears on its own the
 * moment any command type lands. */
export function hasAnyExecutablePath(): boolean {
  return (Object.keys(COMMAND_TYPES_BY_VERB) as ControlVerb[]).some(hasExecutablePath);
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

/** Whether the server would admit any control for this run at all.
 *
 * Mirrors _admission_refusal (studio/operator/run_control.py), which admits
 * the raw status "running" and answers "not_running" for every other value.
 * Written as that positive test rather than as "not one of the statuses we
 * call terminal", because the display mapping this used to read folds every
 * status it does not recognize into "running" on purpose — sensible for a
 * status chip, wrong for a gate. A status it did not know therefore arrived on
 * the ENABLED side and rendered a control the server then rejected;
 * `completed_empty` is the one that exists today, and the next one added would
 * do the same. The positive form has no unknown side to fail open on.
 *
 * Reads the raw status rather than the display status for the same reason: the
 * server's admission reads the sessions row, so anything derived in between is
 * a second opinion this gate cannot afford. A missing status is not "running",
 * which is what the server would conclude too. */
export function runAdmitsControls(status: string | null | undefined): boolean {
  return status === "running";
}

/** Layers the run's own lack of a project onto whatever the state machines
 * decided, on the same reasoning as applyExecutablePath: the refusal has to
 * win over the run-state reasons, since "The run is not paused" on a control
 * that could never be authorized tells the reader to wait for something that
 * will not help.
 *
 * A verb the state machine did not offer stays unoffered — this disables
 * controls, it never adds one. */
export function applyProjectScope(
  project: string | null | undefined,
  state: ControlState,
): ControlState {
  if (!state.offered || (typeof project === "string" && project.length > 0)) return state;
  return offeredState(true, "no-project-scope");
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

/** `hasControlConsumer` mirrors session_has_control_consumer (studio/operator/
 * run_control.py), which is what the server's own admission asks. It is a
 * required argument rather than an optional one so a caller cannot omit the
 * question by accident: the failure it guards against is a steer that is
 * offered, clicked, and then refused with "no_consumer", and that failure is
 * invisible until someone clicks. Callers pass a strict boolean, so a response
 * that never carried the field disables the control instead of assuming it. */
export function steerControlState(
  kind: ControlKind,
  runTerminal: boolean,
  hasControlConsumer: boolean,
): ControlState {
  if (!CONSUMER_KINDS_BY_VERB.message.has(kind)) return NOT_OFFERED;
  if (runTerminal) return offeredState(true, "run-terminal");
  if (!hasControlConsumer) return offeredState(true, "no-live-consumer");
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

function controlContext(runId: string, project?: string | null): OperatorContextSnapshot {
  return {
    project,
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
          // Reaching `done` at all means no proposal arrived: a proposal
          // frame settles this promise in the branch above. The outcome only
          // changes the wording, never whether this is a failure — a turn
          // that completed normally and proposed nothing leaves just as
          // little to confirm as one that errored. Waiting the outcome out
          // reported it as a 30-second stall instead of the refusal it is.
          const outcome = (frame.payload as OperatorDonePayload).outcome;
          settled = true;
          clearTimeout(timer);
          close();
          reject(new Error(`Command turn ended without a proposal (${outcome}).`));
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
  options?: { message?: string; project?: string | null },
): Promise<RunControlProposal> {
  // The project rides on the conversation, not only on the turn context. A
  // turn's context is request-body data the server stores as sent, so a
  // control authorized from it would be authorized by its own caller. The
  // conversation's project is written once here and has no update route, which
  // is what the server's ownership check reads.
  const conversation = await createOperatorConversation({
    project: options?.project,
    title: `${verb} · ${runId.slice(0, 8)}`,
  });
  const accepted = await submitOperatorTurn(conversation.id, {
    instruction: controlInstructionText(kind, verb, runId, options?.message),
    context: controlContext(runId, options?.project),
    expectedLastSequence: 0,
  });
  const proposal = await waitForProposal(conversation.id, accepted.acceptedSequence);
  return { conversationId: conversation.id, proposal };
}

/** Command types that legitimately satisfy each control verb.
 *
 * Gate-release deliberately does not reuse the checkpoint-resume command type
 * (`resume`): that launches another invocation, while `release_run_pause`
 * releases the live run's existing pause gate. Keeping the types distinct lets
 * the confirmation guard reject a model substituting one operation for the
 * other even though both are described as "resume" in natural language. */
const COMMAND_TYPES_BY_VERB: Record<ControlVerb, ReadonlySet<string>> = {
  pause: new Set(["pause_run"]),
  resume: new Set(["release_run_pause"]),
  message: new Set(["steer_run"]),
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
  /** The command types that satisfy `verb`. Defaults to the production table;
   * tests may pass a set explicitly to keep every mismatch rule independently
   * falsifiable without changing the registered command surface. */
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
