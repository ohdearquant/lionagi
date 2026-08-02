import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  acknowledgeOperatorEffect,
  consumeOperatorSse,
  decideOperatorProposal,
  forkOperatorConversation,
  getOperatorConversation,
  isOperatorFrame,
  listOperatorConversations,
  submitOperatorTurn,
  updateOperatorConversation,
} from "./api";
import type { OperatorFrame } from "./types";

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function frame(sequence: number): OperatorFrame {
  return {
    version: 1,
    conversationId: "conversation-1",
    requestId: "request-1",
    sequence,
    type: "text",
    payload: { content: String(sequence), format: "plain", role: "assistant" },
    createdAt: sequence,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Operator API v1", () => {
  it("normalizes the daemon-backed conversation index", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          json({
            conversations: [
              {
                id: "conversation-new",
                title: "Release check",
                status: "active",
                next_sequence: 4,
                active_request_id: null,
                created_at: 10,
                updated_at: 20,
              },
            ],
          }),
        ),
      ),
    );

    await expect(listOperatorConversations()).resolves.toEqual([
      {
        id: "conversation-new",
        project: null,
        title: "Release check",
        status: "active",
        pinned: false,
        nextSequence: 4,
        activeRequestId: null,
        createdAt: 10,
        updatedAt: 20,
      },
    ]);
  });

  it("requests the active/archived/all filter as a query param", async () => {
    let url = "";
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string) => {
        url = input;
        return Promise.resolve(json({ conversations: [] }));
      }),
    );

    await listOperatorConversations({ status: "archived" });
    expect(url).toContain("status=archived");
  });

  it("PATCHes only the fields included in a conversation rename/pin/archive", async () => {
    let captured: RequestInit | undefined;
    let method = "";
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        captured = init;
        method = String(init?.method);
        return Promise.resolve(
          json({
            conversation: {
              id: "conversation-1",
              title: "Renamed",
              status: "active",
              pinned: true,
            },
          }),
        );
      }),
    );

    const updated = await updateOperatorConversation("conversation-1", {
      title: "Renamed",
      pinned: true,
    });

    expect(method).toBe("PATCH");
    expect(JSON.parse(String(captured?.body))).toEqual({ title: "Renamed", pinned: true });
    expect(updated).toMatchObject({ id: "conversation-1", title: "Renamed", pinned: true });
  });

  it("surfaces a 404 when renaming a conversation that does not exist", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          json({ detail: { code: "not_found", message: "Operator conversation not found" } }, 404),
        ),
      ),
    );

    await expect(
      updateOperatorConversation("missing-conversation", { title: "New title" }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      code: "not_found",
    } satisfies Partial<ApiError>);
  });

  it("surfaces a 422 when a rename exceeds the title field limit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          json({ detail: [{ msg: "String should have at most 512 characters" }] }, 422),
        ),
      ),
    );

    await expect(
      updateOperatorConversation("conversation-1", { title: "x".repeat(513) }),
    ).rejects.toMatchObject({ name: "ApiError", status: 422 } satisfies Partial<ApiError>);
  });

  it("forks a conversation and normalizes the returned conversation and frames", async () => {
    let url = "";
    let captured: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string, init?: RequestInit) => {
        url = input;
        captured = init;
        return Promise.resolve(
          json({
            conversation: {
              id: "conversation-fork",
              title: "Source (fork)",
              status: "active",
              pinned: false,
            },
            frames: [frame(1)],
          }),
        );
      }),
    );

    const snapshot = await forkOperatorConversation("conversation-1", { upToSequence: 4 });

    expect(url).toContain("/conversations/conversation-1/fork");
    expect(JSON.parse(String(captured?.body))).toEqual({ upToSequence: 4 });
    expect(snapshot.conversation.id).toBe("conversation-fork");
    expect(snapshot.frames).toHaveLength(1);
  });

  it("surfaces a 404 when forking a conversation that does not exist", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          json({ detail: { code: "not_found", message: "Operator conversation not found" } }, 404),
        ),
      ),
    );

    await expect(forkOperatorConversation("missing-conversation")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      code: "not_found",
    } satisfies Partial<ApiError>);
  });

  it("pages every retained frame instead of truncating history at 1000", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        calls.push(url);
        const after = Number(new URL(url, "http://studio.test").searchParams.get("after_sequence"));
        const frames =
          after === 0
            ? Array.from({ length: 1000 }, (_, index) => frame(index + 1))
            : [frame(1001)];
        return Promise.resolve(
          json({
            conversation: {
              id: "conversation-1",
              status: "active",
              nextSequence: 1002,
              activeRequestId: null,
            },
            frames,
          }),
        );
      }),
    );

    const snapshot = await getOperatorConversation("conversation-1");

    expect(snapshot.frames).toHaveLength(1001);
    expect(snapshot.frames.at(-1)?.sequence).toBe(1001);
    expect(calls).toHaveLength(2);
    expect(calls[0]).toContain("after_sequence=0");
    expect(calls[1]).toContain("after_sequence=1000");
  });

  it("serializes the turn concurrency cursor with the ADR field name", async () => {
    let captured: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        captured = init;
        return Promise.resolve(
          json(
            {
              conversationId: "conversation-1",
              requestId: "request-1",
              acceptedSequence: 8,
            },
            202,
          ),
        );
      }),
    );

    await submitOperatorTurn("conversation-1", {
      instruction: "Investigate the failure",
      context: {
        space: "mission",
        route: "/",
        selection: null,
        filters: {},
      },
      expectedLastSequence: 7,
    });

    expect(JSON.parse(String(captured?.body))).toMatchObject({
      instruction: "Investigate the failure",
      expected_last_sequence: 7,
    });
  });

  it("preserves structured stale-context details on a 409", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          json(
            {
              detail: {
                code: "stale_context",
                message: "Conversation advanced",
                retryable: true,
                details: { latestSequence: 9 },
              },
            },
            409,
          ),
        ),
      ),
    );

    const rejected = submitOperatorTurn("conversation-1", {
      instruction: "Continue",
      context: { space: "mission", route: "/", selection: null, filters: {} },
      expectedLastSequence: 7,
    });

    await expect(rejected).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      code: "stale_context",
      retryable: true,
      message: "Conversation advanced",
    } satisfies Partial<ApiError>);
  });

  it("posts an explicit allow/deny proposal decision with the exact command hash", async () => {
    let url = "";
    let captured: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string, init?: RequestInit) => {
        url = input;
        captured = init;
        return Promise.resolve(
          json({ proposalId: "proposal-1", status: "executing", result: null }),
        );
      }),
    );

    await decideOperatorProposal(
      "conversation-1",
      "proposal-1",
      "deny",
      "sha256",
      "playbook-fingerprint",
    );

    expect(url).toContain("/proposals/proposal-1/decision");
    expect(JSON.parse(String(captured?.body))).toEqual({
      decision: "deny",
      expectedCommandHash: "sha256",
      expectedTargetVersion: "playbook-fingerprint",
    });
  });

  it("acknowledges a validated client effect with the resulting route", async () => {
    let url = "";
    let captured: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string, init?: RequestInit) => {
        url = input;
        captured = init;
        return Promise.resolve(json({ effectId: "effect-1", status: "applied" }));
      }),
    );

    await acknowledgeOperatorEffect("conversation-1", "effect-1", {
      status: "applied",
      clientRoute: "/fleet?s=run-1",
    });

    expect(url).toContain("/effects/effect-1/ack");
    expect(JSON.parse(String(captured?.body))).toEqual({
      status: "applied",
      clientRoute: "/fleet?s=run-1",
    });
  });
});

describe("Operator SSE framing", () => {
  it("retains a fragmented tail and consumes LF plus CRLF records", () => {
    const first = consumeOperatorSse('data: {"a":1}\r\n\r\ndata: {"b"');
    expect(first.data).toEqual(['{"a":1}']);
    const second = consumeOperatorSse(`${first.rest}:2}\n\n`);
    expect(second.data).toEqual(['{"b":2}']);
    expect(second.rest).toBe("");
  });

  it("joins multi-line data and rejects unknown protocol versions or frame types", () => {
    const consumed = consumeOperatorSse("event: ignored\ndata: hello\ndata: world\n\n");
    expect(consumed.data).toEqual(["hello\nworld"]);
    expect(isOperatorFrame(frame(1))).toBe(true);
    expect(isOperatorFrame({ ...frame(1), version: 2 })).toBe(false);
    expect(isOperatorFrame({ ...frame(1), type: "future_type" })).toBe(false);
  });
});
