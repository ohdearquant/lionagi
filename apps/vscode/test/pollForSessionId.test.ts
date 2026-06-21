/**
 * Tests for pollForSessionId (src/launch/runCommand.ts).
 * Uses vi.useFakeTimers() + vi.advanceTimersByTimeAsync() to drive the
 * interleaved setTimeout-based delay() and awaited API calls.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { pollForSessionId } from "../src/launch/runCommand.js";

// Stub StudioDeps shape for these tests — only `client.getInvocation` is used.
function makeClient(getInvocation: ReturnType<typeof vi.fn>) {
  return {
    getInvocation,
    launch: vi.fn(),
    listRuns: vi.fn(),
    listProjectGroups: vi.fn(),
    getRun: vi.fn(),
    cancelLaunch: vi.fn(),
  };
}

function makeDeps(getInvocation: ReturnType<typeof vi.fn>) {
  return {
    client: makeClient(getInvocation) as unknown as import("../src/extension.js").StudioDeps["client"],
    backend: { isRunning: () => false } as unknown as import("../src/extension.js").StudioDeps["backend"],
  };
}

describe("pollForSessionId", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("resolves with the first session id once it appears (call count binds polling)", async () => {
    vi.useFakeTimers();
    const getInvocation = vi.fn()
      .mockResolvedValueOnce({ id: "inv-1", skill: null, status: "running", sessions: [] })
      .mockResolvedValueOnce({ id: "inv-1", skill: null, status: "running", sessions: [] })
      .mockResolvedValueOnce({ id: "inv-1", skill: null, status: "running", sessions: [{ id: "sess-xyz", name: null, agent_name: null, playbook_name: null, invocation_kind: null, status: "running", model: null, effort: null, started_at: null, ended_at: null, last_message_at: null }] });

    const deps = makeDeps(getInvocation);
    const controller = new AbortController();

    const promise = pollForSessionId(deps as unknown as import("../src/extension.js").StudioDeps, "inv-1", controller.signal);

    // Advance 3 x 1000ms ticks (the Async variant flushes microtasks between ticks).
    await vi.advanceTimersByTimeAsync(3_000);
    const result = await promise;

    expect(result).toBe("sess-xyz");
    // Exactly 3 calls — the loop stops as soon as sessions[0].id is present.
    expect(getInvocation).toHaveBeenCalledTimes(3);
  });

  it("resolves undefined after POLL_TIMEOUT_MS when no session ever appears", async () => {
    vi.useFakeTimers();
    const getInvocation = vi.fn().mockResolvedValue({
      id: "inv-2", skill: null, status: "running", sessions: [],
    });

    const deps = makeDeps(getInvocation);
    const controller = new AbortController();

    const promise = pollForSessionId(deps as unknown as import("../src/extension.js").StudioDeps, "inv-2", controller.signal);

    // Advance past the 30 000 ms timeout.
    await vi.advanceTimersByTimeAsync(31_000);
    const result = await promise;

    expect(result).toBeUndefined();
  });

  it("respects AbortSignal: stops polling after abort and returns undefined", async () => {
    vi.useFakeTimers();
    const getInvocation = vi.fn().mockResolvedValue({
      id: "inv-3", skill: null, status: "running", sessions: [],
    });

    const deps = makeDeps(getInvocation);
    const controller = new AbortController();

    const promise = pollForSessionId(deps as unknown as import("../src/extension.js").StudioDeps, "inv-3", controller.signal);

    // Let exactly one tick fire, then abort.
    await vi.advanceTimersByTimeAsync(1_000);
    const callsAfterOneTick = getInvocation.mock.calls.length;
    controller.abort();

    // Advance further — the loop must not keep calling getInvocation.
    await vi.advanceTimersByTimeAsync(5_000);
    const result = await promise;

    expect(result).toBeUndefined();
    // Call count must not grow after abort.
    expect(getInvocation).toHaveBeenCalledTimes(callsAfterOneTick);
  });

  it("survives a transient rejection and still resolves the session id", async () => {
    vi.useFakeTimers();
    const getInvocation = vi.fn()
      .mockRejectedValueOnce(new Error("transient network error"))
      .mockResolvedValueOnce({ id: "inv-4", skill: null, status: "running", sessions: [{ id: "sess-abc", name: null, agent_name: null, playbook_name: null, invocation_kind: null, status: "running", model: null, effort: null, started_at: null, ended_at: null, last_message_at: null }] });

    const deps = makeDeps(getInvocation);
    const controller = new AbortController();

    const promise = pollForSessionId(deps as unknown as import("../src/extension.js").StudioDeps, "inv-4", controller.signal);

    // Two ticks: first rejects (caught internally), second returns the session.
    await vi.advanceTimersByTimeAsync(2_000);
    const result = await promise;

    expect(result).toBe("sess-abc");
  });
});
