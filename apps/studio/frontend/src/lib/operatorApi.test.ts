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
  streamOperatorConversation,
  submitOperatorTurn,
  updateOperatorConversation,
} from "./api";
import type { OperatorFrame, OperatorTruncationPayload } from "./types";

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

// The durable retention notice the backend writes when a turn reaches its
// frame count or payload-byte budget. Both reasons share this envelope.
function truncationFrame(
  sequence: number,
  reason: "frames_per_turn" | "turn_payload_bytes" = "frames_per_turn",
): OperatorFrame<OperatorTruncationPayload> {
  return {
    version: 1,
    conversationId: "conversation-1",
    requestId: "request-1",
    sequence,
    type: "truncation",
    payload: {
      reason,
      limits: {
        maxFramesPerTurn: 2000,
        maxTurnPayloadBytes: 8388608,
        maxFramePayloadBytes: 65536,
      },
      elidedFrames: 3,
      elidedBytes: 4096,
      elidedFrameTypes: { text: 2, tool_result: 1 },
      firstElidedAt: 1_700_000_000,
      lastElidedAt: 1_700_000_010,
      message:
        "This turn reached its durable retention limit; frames after this point were not stored and are counted here.",
    },
    createdAt: 1_700_000_010,
  };
}

function doneFrame(sequence: number): OperatorFrame {
  return {
    version: 1,
    conversationId: "conversation-1",
    requestId: "request-1",
    sequence,
    type: "done",
    payload: { outcome: "completed", lastSequence: sequence },
    createdAt: sequence,
  };
}

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
        // The pinned selection survives normalization (absent here, so null):
        // dropping these fields is what reset the composer to "Default" on
        // every page refresh.
        provider: null,
        providerModel: null,
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

  it("keeps a retained truncation notice and the later terminal frame readable through paginated history", async () => {
    // The notice is a stored frame of its own: it can land on a later page than
    // the text before it, and the turn's `done` frame follows it.
    const truncation = truncationFrame(2001, "turn_payload_bytes");
    const done = doneFrame(2002);
    const pages: Record<string, unknown[]> = {
      "0": Array.from({ length: 1000 }, (_, index) => frame(index + 1)),
      "1000": Array.from({ length: 1000 }, (_, index) => frame(1001 + index)),
      "2000": [truncation, done],
    };
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        calls.push(url);
        const after = new URL(url, "http://studio.test").searchParams.get("after_sequence") ?? "0";
        return Promise.resolve(
          json({
            conversation: {
              id: "conversation-1",
              status: "active",
              nextSequence: 2003,
              activeRequestId: null,
            },
            frames: pages[after],
          }),
        );
      }),
    );

    const snapshot = await getOperatorConversation("conversation-1");

    expect(calls).toHaveLength(3);
    expect(snapshot.frames).toHaveLength(2002);
    expect(snapshot.frames.at(-2)).toMatchObject({
      type: "truncation",
      sequence: 2001,
      payload: { reason: "turn_payload_bytes", elidedFrames: 3 },
    });
    expect(snapshot.frames.at(-1)).toMatchObject({ type: "done", sequence: 2002 });
  });

  it("still rejects a stored frame type the protocol does not define, and a malformed notice", async () => {
    for (const frameValue of [
      { ...frame(1), type: "future_type" },
      { ...truncationFrame(1), payload: { reason: "frames_per_turn" } },
      { ...truncationFrame(1), payload: { ...truncationFrame(1).payload, elidedFrames: -1 } },
    ]) {
      vi.stubGlobal(
        "fetch",
        vi.fn(() =>
          Promise.resolve(
            json({
              conversation: { id: "conversation-1", status: "active" },
              frames: [frameValue],
            }),
          ),
        ),
      );
      await expect(getOperatorConversation("conversation-1")).rejects.toThrow(
        "Operator history contains an unsupported protocol frame.",
      );
    }
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

describe("Operator stream cursor", () => {
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("holds the cursor over a handler that threw, then advances past one that did not", async () => {
    vi.useFakeTimers();
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        urls.push(url);
        return Promise.resolve(
          new Response(`data: ${JSON.stringify(frame(4))}\n\n`, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          }),
        );
      }),
    );

    let deliveries = 0;
    const close = streamOperatorConversation("conversation-1", 0, {
      onFrame: () => {
        deliveries += 1;
        if (deliveries === 1) throw new Error("frame handler failed on this frame");
      },
    });

    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(750);
    await vi.advanceTimersByTimeAsync(750);
    close();

    // Frame 4 is offered twice because the first handler never took it, and the
    // third attempt has moved past it, which is what says the cursor advances
    // at all — a cursor that simply never moved would satisfy the repeat alone.
    expect(
      urls.map((url) => new URL(url, "http://studio.test").searchParams.get("after_sequence")),
    ).toEqual(["0", "0", "4"]);
  });

  it("delivers a retention notice and keeps consuming through to the later done frame", async () => {
    vi.useFakeTimers();
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        urls.push(url);
        const after = new URL(url, "http://studio.test").searchParams.get("after_sequence");
        // First connection: text, the retention notice, then the turn's done.
        // A reconnect must never be needed for a valid stream.
        const body = (after ?? "0") === "0"
          ? [frame(1), truncationFrame(2), doneFrame(3)]
              .map((value) => `data: ${JSON.stringify(value)}\n\n`)
              .join("")
          : "";
        // The daemon keeps the subscription open after the terminal frame.
        // A body that closes would only exercise the reconnect loop.
        const stream = new ReadableStream({
          start(controller) {
            if (body) controller.enqueue(new TextEncoder().encode(body));
          },
        });
        return Promise.resolve(
          new Response(stream, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          }),
        );
      }),
    );

    const delivered: OperatorFrame[] = [];
    const errors: { message: string; fatal: boolean }[] = [];
    const close = streamOperatorConversation("conversation-1", 0, {
      onFrame: (value) => delivered.push(value),
      onError: (error, fatal) => errors.push({ message: error.message, fatal }),
    });

    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(10_000);
    close();

    expect(errors).toEqual([]);
    expect(delivered.map((value) => [value.type, value.sequence])).toEqual([
      ["text", 1],
      ["truncation", 2],
      ["done", 3],
    ]);
    // The cursor advanced past the notice, so a reconnect would resume after
    // the terminal frame rather than replay the whole turn.
    expect(
      urls.map((url) => new URL(url, "http://studio.test").searchParams.get("after_sequence")),
    ).toEqual(["0"]);
  });

  it("abandons a silent pre-restart stream and replays from the durable cursor", async () => {
    vi.useFakeTimers();
    const urls: string[] = [];
    const signals: AbortSignal[] = [];
    const delivered: OperatorFrame[] = [];
    const errors: { message: string; fatal: boolean }[] = [];
    let attempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        attempts += 1;
        urls.push(url);
        signals.push(init?.signal as AbortSignal);
        if (attempts === 1) {
          // This is the failure shape seen through the dev proxy when daemon A
          // dies: the upstream produces neither EOF nor the five-second
          // heartbeat, so reader.read() otherwise waits forever while a turn
          // submitted to daemon B is already durable.
          return Promise.resolve(
            new Response(new ReadableStream({ start() {} }), {
              status: 200,
              headers: { "content-type": "text/event-stream" },
            }),
          );
        }
        return Promise.resolve(
          new Response(`data: ${JSON.stringify(frame(5))}\n\n`, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          }),
        );
      }),
    );

    const close = streamOperatorConversation("conversation-1", 4, {
      onFrame: (value) => delivered.push(value),
      onError: (error, fatal) => errors.push({ message: error.message, fatal }),
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(urls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(signals[0].aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(750);
    close();

    expect(delivered.map((value) => value.sequence)).toEqual([5]);
    expect(errors).toEqual([
      expect.objectContaining({ fatal: false, message: expect.stringContaining("timed out") }),
    ]);
    expect(
      urls
        .slice(0, 2)
        .map((url) => new URL(url, "http://studio.test").searchParams.get("after_sequence")),
    ).toEqual(["4", "4"]);
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

  it("accepts the retention notice for both limits and refuses one without its counts", () => {
    for (const reason of ["frames_per_turn", "turn_payload_bytes"] as const) {
      expect(isOperatorFrame(truncationFrame(1, reason))).toBe(true);
    }
    const notice = truncationFrame(1);
    expect(isOperatorFrame({ ...notice, payload: { ...notice.payload, elidedFrames: "1" } })).toBe(
      false,
    );
    expect(isOperatorFrame({ ...notice, payload: { ...notice.payload, limits: null } })).toBe(
      false,
    );
    expect(isOperatorFrame({ ...notice, payload: { ...notice.payload, reason: "other" } })).toBe(
      false,
    );
    expect(
      isOperatorFrame({
        ...notice,
        payload: { ...notice.payload, elidedFrameTypes: undefined },
      }),
    ).toBe(false);
    expect(
      isOperatorFrame({
        ...notice,
        payload: { ...notice.payload, firstElidedAt: Number.NaN },
      }),
    ).toBe(false);
  });
});
