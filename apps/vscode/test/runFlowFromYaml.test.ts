/**
 * Tests for den.runFlowFromYaml command (src/launch/runCommand.ts).
 * Drives the REAL registerRunCommand and invokes the captured callback.
 * Uses fake timers to blow past the 30 s poll so the post-launch panel
 * path (openLaunchStreamPanel) is never reached, keeping the test pure.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  window,
  commands,
  ProgressLocation,
  __resetVscodeMock,
  __getCommand,
} from "./mocks/vscode.js";
import { registerRunCommand } from "../src/launch/runCommand.js";
import type { StudioDeps } from "../src/extension.js";

// Minimal token with an onCancellationRequested no-op (avoids TypeError inside pollForSessionId).
const fakeToken = {
  isCancellationRequested: false,
  onCancellationRequested: () => ({ dispose() {} }),
};
const fakeProgress = { report: vi.fn() };

beforeEach(() => {
  __resetVscodeMock();

  // withProgress: just run the thunk synchronously, passing fakeProgress + fakeToken.
  window.withProgress.mockImplementation(
    (_opts: unknown, thunk: (p: unknown, t: unknown) => Promise<unknown>) =>
      thunk(fakeProgress, fakeToken)
  );
});

afterEach(() => {
  vi.useRealTimers();
});

function makeContext() {
  return { subscriptions: { push(..._args: unknown[]) {} } };
}

function makeDeps(launchMock: ReturnType<typeof vi.fn>, getInvocationMock: ReturnType<typeof vi.fn>) {
  return {
    client: {
      launch: launchMock,
      getInvocation: getInvocationMock,
      listRuns: vi.fn(),
      listProjectGroups: vi.fn(),
      getRun: vi.fn(),
      cancelLaunch: vi.fn(),
    },
    backend: {
      isRunning: vi.fn().mockReturnValue(true),
      onDidChangeState: vi.fn().mockReturnValue({ dispose() {} }),
      start: vi.fn(),
    },
  } as unknown as StudioDeps;
}

describe("den.runFlowFromYaml", () => {
  it("calls client.launch with correct flow_yaml payload including trimmed editor content", async () => {
    vi.useFakeTimers();

    // Editor has a YAML with trailing newline — action_flow_yaml must be trimmed.
    window.activeTextEditor = {
      document: {
        getText: () => "prompt: say hi\n",
        uri: { fsPath: "/tmp/flow.yaml" },
      },
    };

    // showInputBox returns model then project (blank = undefined).
    window.showInputBox
      .mockResolvedValueOnce("openai/gpt-4.1-mini")
      .mockResolvedValueOnce("");

    const launchMock = vi.fn().mockResolvedValue({
      invocation_id: "inv-yaml-1",
      action_kind: "flow_yaml",
    });
    // getInvocation always returns no sessions → pollForSessionId times out → early return path.
    const getInvocationMock = vi.fn().mockResolvedValue({
      id: "inv-yaml-1",
      skill: null,
      status: "running",
      sessions: [],
    });

    const deps = makeDeps(launchMock, getInvocationMock);

    registerRunCommand(
      makeContext() as unknown as import("vscode").ExtensionContext,
      deps
    );

    const handler = __getCommand("den.runFlowFromYaml");
    expect(handler).toBeDefined();

    // Start the command. It will await inside pollForSessionId after launch completes.
    const commandPromise = handler!();

    // The launch withProgress runs synchronously (our mock). After launch the code
    // enters attachLaunchedRun → a second withProgress wrapping pollForSessionId.
    // Advance 31 s to blow past the 30 000 ms poll timeout.
    await vi.advanceTimersByTimeAsync(31_000);

    await commandPromise;

    // The critical assertion: launch called once, with exactly the right shape.
    expect(launchMock).toHaveBeenCalledTimes(1);
    expect(launchMock).toHaveBeenCalledWith({
      action_kind: "flow_yaml",
      action_model: "openai/gpt-4.1-mini",
      action_flow_yaml: "prompt: say hi",   // trimmed — no trailing newline
      action_project: undefined,
    });
  });
});
