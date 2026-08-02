import { describe, expect, it } from "vitest";
import type { OperatorConversation, OperatorFrame } from "@/lib/types";
import {
  MAX_RETAINED_FRAMES,
  initialOperatorState,
  mergeOperatorFrames,
  operatorReducer,
  pendingOperatorProposals,
} from "./operatorReducer";

const conversation: OperatorConversation = {
  id: "conversation-1",
  status: "active",
  pinned: false,
  activeRequestId: "request-1",
};

function frame(
  sequence: number,
  type: OperatorFrame["type"] = "text",
  payload: Record<string, unknown> = { content: `${sequence}`, format: "plain" },
): OperatorFrame {
  return {
    version: 1,
    conversationId: conversation.id,
    requestId: "request-1",
    sequence,
    type,
    payload: payload as unknown as OperatorFrame["payload"],
    createdAt: sequence,
  };
}

describe("operatorReducer", () => {
  it("sorts replayed frames and deduplicates redelivery by sequence", () => {
    const merged = mergeOperatorFrames([frame(2)], [frame(1), frame(2), frame(3)]);
    expect(merged.map((item) => item.sequence)).toEqual([1, 2, 3]);
  });

  it("loads daemon history and clears the active request only after done", () => {
    let state = operatorReducer(initialOperatorState, {
      type: "LOAD_SUCCESS",
      conversation,
      frames: [frame(1)],
    });
    expect(state.activeRequestId).toBe("request-1");
    expect(state.lastSequence).toBe(1);

    state = operatorReducer(state, {
      type: "APPEND_FRAMES",
      frames: [
        frame(2, "done", {
          outcome: "completed",
          lastSequence: 2,
        }),
      ],
    });
    expect(state.activeRequestId).toBeNull();
    expect(state.lastSequence).toBe(2);
  });

  it("does not resurrect a request when done streams before turn acceptance", () => {
    const state = operatorReducer(
      {
        ...initialOperatorState,
        conversation,
        frames: [
          frame(1),
          frame(2, "done", {
            outcome: "completed",
            lastSequence: 2,
          }),
        ],
        lastSequence: 2,
      },
      { type: "TURN_ACCEPTED", requestId: "request-1" },
    );

    expect(state.activeRequestId).toBeNull();
  });

  it("ignores a frame from another conversation", () => {
    const foreign = { ...frame(2), conversationId: "conversation-2" };
    const state = operatorReducer(
      {
        ...initialOperatorState,
        conversation,
        frames: [frame(1)],
        lastSequence: 1,
      },
      { type: "APPEND_FRAMES", frames: [foreign] },
    );
    expect(state.frames.map((item) => item.sequence)).toEqual([1]);
  });

  it("marks proposals resolved after a terminal confirmation frame", () => {
    const proposal = frame(1, "proposal", {
      proposal: {
        id: "proposal-1",
        command: {},
        commandHash: "hash",
        risk: "execute",
        summary: "Launch report",
        idempotencyKey: "key",
        expiresAt: 999,
      },
    });
    const confirmation = frame(2, "confirmation", {
      proposalId: "proposal-1",
      state: "executed",
    });

    expect(pendingOperatorProposals([proposal])[0]?.resolved).toBe(false);
    expect(pendingOperatorProposals([proposal, confirmation])[0]?.resolved).toBe(true);
  });

  it("clears the active request on a terminal error frame", () => {
    const state = operatorReducer(
      {
        ...initialOperatorState,
        conversation,
        frames: [frame(1)],
        lastSequence: 1,
        activeRequestId: "request-1",
      },
      {
        type: "APPEND_FRAMES",
        frames: [
          frame(2, "error", {
            error: { code: "engine_failed", message: "stream torn down" },
          }),
        ],
      },
    );

    expect(state.activeRequestId).toBeNull();
  });

  it("caps retained frames and keeps the newest", () => {
    const overflow = MAX_RETAINED_FRAMES + 100;
    const frames = Array.from({ length: overflow }, (_, index) => frame(index + 1));

    const merged = mergeOperatorFrames([], frames, conversation.id);

    expect(merged).toHaveLength(MAX_RETAINED_FRAMES);
    expect(merged.at(0)?.sequence).toBe(overflow - MAX_RETAINED_FRAMES + 1);
    expect(merged.at(-1)?.sequence).toBe(overflow);
  });

  function proposalFrame(sequence: number, id: string): OperatorFrame {
    return frame(sequence, "proposal", {
      proposal: {
        id,
        command: { tool: "Bash", arguments: { command: "rm -rf /etc" } },
        commandHash: "hash",
        risk: "admin",
        summary: "Remove configuration",
        idempotencyKey: `key-${id}`,
        expiresAt: 999,
      },
    });
  }

  it("retains an unresolved proposal that eviction would otherwise drop", () => {
    const overflow = MAX_RETAINED_FRAMES + 100;
    const frames = [
      proposalFrame(1, "proposal-old"),
      ...Array.from({ length: overflow }, (_, index) => frame(index + 2)),
    ];

    const merged = mergeOperatorFrames([], frames, conversation.id);

    // The oldest frame by age, but a live permission prompt: evicting it would
    // take Allow/Deny off the screen while the daemon still waits for a decision.
    expect(merged.at(0)?.sequence).toBe(1);
    expect(pendingOperatorProposals(merged).map((item) => item.frame.payload.proposal.id)).toEqual([
      "proposal-old",
    ]);
    expect(merged.at(-1)?.sequence).toBe(overflow + 1);
  });

  it("evicts an old proposal once it has been resolved", () => {
    const overflow = MAX_RETAINED_FRAMES + 100;
    const frames = [
      proposalFrame(1, "proposal-old"),
      frame(2, "confirmation", { proposalId: "proposal-old", state: "executed" }),
      ...Array.from({ length: overflow }, (_, index) => frame(index + 3)),
    ];

    const merged = mergeOperatorFrames([], frames, conversation.id);

    expect(merged).toHaveLength(MAX_RETAINED_FRAMES);
    expect(merged.some((item) => item.sequence === 1)).toBe(false);
  });

  it.each(["denied", "cancelled"] as const)(
    "marks proposals resolved after a durable %s confirmation",
    (state) => {
      const proposal = frame(1, "proposal", {
        proposal: {
          id: "proposal-1",
          command: { tool: "Bash", arguments: { command: "git status" } },
          commandHash: "hash",
          risk: "execute",
          summary: "Inspect the repository",
          idempotencyKey: "key",
          expiresAt: 999,
        },
      });
      const confirmation = frame(2, "confirmation", {
        proposalId: "proposal-1",
        state,
      });

      expect(pendingOperatorProposals([proposal, confirmation])[0]?.resolved).toBe(true);
    },
  );

  it("applies UPDATE_CONVERSATION to the open conversation", () => {
    const loaded = operatorReducer(initialOperatorState, {
      type: "LOAD_SUCCESS",
      conversation,
      frames: [],
    });
    const renamed = { ...conversation, title: "Renamed", pinned: true };
    const updated = operatorReducer(loaded, {
      type: "UPDATE_CONVERSATION",
      conversation: renamed,
    });
    expect(updated.conversation).toEqual(renamed);
    expect(updated.frames).toBe(loaded.frames);
  });

  it("ignores UPDATE_CONVERSATION for a conversation that is not the open one", () => {
    const loaded = operatorReducer(initialOperatorState, {
      type: "LOAD_SUCCESS",
      conversation,
      frames: [],
    });
    const other = operatorReducer(loaded, {
      type: "UPDATE_CONVERSATION",
      conversation: { ...conversation, id: "conversation-2", title: "Someone else" },
    });
    expect(other).toBe(loaded);
  });
});
