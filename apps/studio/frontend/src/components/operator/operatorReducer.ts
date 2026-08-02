import type { OperatorConversation, OperatorFrame, OperatorProposalPayload } from "@/lib/types";

export type OperatorLoadState = "idle" | "loading" | "ready" | "error";
export type OperatorConnectionState = "idle" | "connecting" | "open" | "reconnecting" | "error";

export interface OperatorState {
  conversation: OperatorConversation | null;
  frames: OperatorFrame[];
  lastSequence: number;
  activeRequestId: string | null;
  loadState: OperatorLoadState;
  connectionState: OperatorConnectionState;
  error: string | null;
}

export const initialOperatorState: OperatorState = {
  conversation: null,
  frames: [],
  lastSequence: 0,
  activeRequestId: null,
  loadState: "idle",
  connectionState: "idle",
  error: null,
};

export type OperatorAction =
  | { type: "LOAD_START" }
  | {
      type: "LOAD_SUCCESS";
      conversation: OperatorConversation;
      frames: OperatorFrame[];
    }
  | { type: "LOAD_ERROR"; error: string }
  | { type: "APPEND_FRAMES"; frames: OperatorFrame[] }
  | { type: "TURN_ACCEPTED"; requestId: string }
  | { type: "CONNECTION"; state: OperatorConnectionState; error?: string }
  | { type: "UPDATE_CONVERSATION"; conversation: OperatorConversation }
  | { type: "RESET" };

/**
 * Frames retained in browser memory for one conversation. A long turn would
 * otherwise grow the array — and the DOM — without limit, and every append
 * re-sorts the whole thing.
 */
export const MAX_RETAINED_FRAMES = 2_000;

/**
 * Drop the oldest frames past the cap, except unresolved proposals.
 *
 * An unresolved proposal is a live permission prompt the daemon is still waiting
 * on. Evicting one by age would take Allow/Deny off the screen with no way back,
 * which is a worse failure than an unbounded array.
 */
function evictOldestFrames(frames: OperatorFrame[]): OperatorFrame[] {
  if (frames.length <= MAX_RETAINED_FRAMES) return frames;
  const unresolved = new Set(
    pendingOperatorProposals(frames)
      .filter((proposal) => !proposal.resolved)
      .map((proposal) => proposal.frame.sequence),
  );
  const retained = frames.slice(-MAX_RETAINED_FRAMES);
  const rescued = frames
    .slice(0, frames.length - MAX_RETAINED_FRAMES)
    .filter((frame) => unresolved.has(frame.sequence));
  return rescued.length === 0 ? retained : [...rescued, ...retained];
}

export function mergeOperatorFrames(
  current: OperatorFrame[],
  incoming: OperatorFrame[],
  conversationId?: string,
): OperatorFrame[] {
  if (incoming.length === 0) return current;
  const bySequence = new Map<number, OperatorFrame>();
  for (const frame of current) bySequence.set(frame.sequence, frame);
  for (const frame of incoming) {
    if (conversationId && frame.conversationId !== conversationId) continue;
    if (!bySequence.has(frame.sequence)) bySequence.set(frame.sequence, frame);
  }
  return evictOldestFrames([...bySequence.values()].sort((a, b) => a.sequence - b.sequence));
}

function activeRequestAfterFrames(
  activeRequestId: string | null,
  frames: OperatorFrame[],
): string | null {
  if (!activeRequestId) return null;
  // A terminal `error` frame ends the request just as `done` does; leaving the id
  // set there strands the composer disabled until the operator reloads.
  return frames.some(
    (frame) =>
      frame.requestId === activeRequestId && (frame.type === "done" || frame.type === "error"),
  )
    ? null
    : activeRequestId;
}

export function operatorReducer(state: OperatorState, action: OperatorAction): OperatorState {
  switch (action.type) {
    case "LOAD_START":
      return {
        ...state,
        loadState: "loading",
        connectionState: "idle",
        error: null,
      };
    case "LOAD_SUCCESS": {
      const frames = mergeOperatorFrames([], action.frames, action.conversation.id);
      const lastSequence = frames.at(-1)?.sequence ?? 0;
      return {
        conversation: action.conversation,
        frames,
        lastSequence,
        activeRequestId: activeRequestAfterFrames(
          action.conversation.activeRequestId ?? null,
          frames,
        ),
        loadState: "ready",
        connectionState: "connecting",
        error: null,
      };
    }
    case "LOAD_ERROR":
      return {
        ...state,
        loadState: "error",
        connectionState: "error",
        error: action.error,
      };
    case "APPEND_FRAMES": {
      const frames = mergeOperatorFrames(state.frames, action.frames, state.conversation?.id);
      return {
        ...state,
        frames,
        lastSequence: Math.max(state.lastSequence, frames.at(-1)?.sequence ?? 0),
        activeRequestId: activeRequestAfterFrames(state.activeRequestId, frames),
      };
    }
    case "TURN_ACCEPTED":
      return {
        ...state,
        // A fast provider can persist and stream `done` before the 202 submit
        // response reaches the browser. Never let that later acceptance race
        // resurrect a request the durable frame log already closed.
        activeRequestId: activeRequestAfterFrames(action.requestId, state.frames),
        error: null,
      };
    case "CONNECTION":
      return {
        ...state,
        connectionState: action.state,
        error: action.error ?? (action.state === "open" ? null : state.error),
      };
    case "UPDATE_CONVERSATION":
      // A rename/pin/archive elsewhere (the conversation list) should not
      // resurrect a conversation the operator has since navigated away from.
      if (!state.conversation || state.conversation.id !== action.conversation.id) return state;
      return { ...state, conversation: action.conversation };
    case "RESET":
      return initialOperatorState;
  }
}

export interface PendingOperatorProposal {
  frame: OperatorFrame<OperatorProposalPayload>;
  resolved: boolean;
}

export function pendingOperatorProposals(frames: OperatorFrame[]): PendingOperatorProposal[] {
  const resolutions = new Map<string, string>();
  for (const frame of frames) {
    if (frame.type !== "confirmation") continue;
    const payload = frame.payload as { proposalId?: unknown; state?: unknown };
    if (typeof payload.proposalId === "string" && typeof payload.state === "string") {
      resolutions.set(payload.proposalId, payload.state);
    }
  }

  return frames
    .filter((frame): frame is OperatorFrame<OperatorProposalPayload> => frame.type === "proposal")
    .map((frame) => {
      const state = resolutions.get(frame.payload.proposal.id);
      return {
        frame,
        resolved:
          state === "confirmed" ||
          state === "denied" ||
          state === "cancelled" ||
          state === "expired" ||
          state === "executed",
      };
    });
}
