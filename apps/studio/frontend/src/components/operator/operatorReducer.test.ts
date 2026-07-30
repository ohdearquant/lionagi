import { describe, expect, it } from "vitest";
import type { OperatorConversation, OperatorFrame } from "@/lib/types";
import {
  initialOperatorState,
  mergeOperatorFrames,
  operatorReducer,
  pendingOperatorProposals,
} from "./operatorReducer";

const conversation: OperatorConversation = {
  id: "conversation-1",
  status: "active",
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
});
