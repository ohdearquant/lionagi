import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EventEmitter as NodeEventEmitter } from "events";
import * as fs from "fs";
import * as path from "path";
import { BackendManager } from "../src/backend/lifecycle.js";
import {
  __resetVscodeMock,
  window as mockWindow,
  workspace as mockWorkspace,
} from "./mocks/vscode.js";

// child_process.spawn and fs.existsSync are module-namespace exports, which
// vi.spyOn cannot redefine ("Cannot redefine property"). Mock the modules
// instead; vi.hoisted lifts these fns above the hoisted vi.mock factories so the
// factories can reference them. existsSync defaults to false (nothing on disk).
const { spawnMock, existsSyncMock } = vi.hoisted(() => ({
  spawnMock: vi.fn(),
  existsSyncMock: vi.fn(() => false),
}));
vi.mock("child_process", async (importOriginal) => ({
  ...(await importOriginal<typeof import("child_process")>()),
  spawn: spawnMock,
}));
vi.mock("fs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("fs")>()),
  existsSync: existsSyncMock,
}));

// Drives probeHealth / _pollHealth: when `healthy` is true, GET /health
// resolves to {status:"ok"}; otherwise the fetch rejects (backend unreachable).
let healthy = false;

// A configured den.url puts start() on the attach-only path: it probes and never
// spawns a child, so the real state machine is exercised without child_process.
function makeManager(): BackendManager {
  return new BackendManager(
    () => "", // pythonPath
    () => "http://127.0.0.1:8765", // configuredUrl
    () => 8765, // port
    () => "127.0.0.1", // host
    () => "" // token
  );
}

describe("BackendManager resilience", () => {
  let bm: BackendManager | undefined;

  beforeEach(() => {
    __resetVscodeMock();
    healthy = false;
    vi.stubGlobal("fetch", async () => {
      if (!healthy) {
        throw new Error("ECONNREFUSED");
      }
      return {
        ok: true,
        json: async () => ({ status: "ok" }),
      } as unknown as Response;
    });
  });

  afterEach(() => {
    bm?.dispose();
    bm = undefined;
    vi.unstubAllGlobals();
  });

  it("attaches to a healthy configured backend", async () => {
    healthy = true;
    bm = makeManager();
    await bm.start();
    expect(bm.state).toBe("running");
  });

  it("errors when the configured backend is unreachable", async () => {
    healthy = false;
    bm = makeManager();
    await bm.start();
    expect(bm.state).toBe("error");
  });

  it("self-heals error → running when the backend becomes reachable", async () => {
    bm = makeManager();
    await bm.start();
    expect(bm.state).toBe("error");

    healthy = true;
    await bm.reconcile();
    expect(bm.state).toBe("running");
  });

  it("condemns running → error only after two consecutive missed probes", async () => {
    healthy = true;
    bm = makeManager();
    await bm.start();
    expect(bm.state).toBe("running");

    healthy = false;
    await bm.reconcile();
    expect(bm.state).toBe("running"); // one miss tolerated (hysteresis)
    await bm.reconcile();
    expect(bm.state).toBe("error"); // second consecutive miss condemns
  });

  it("does not resurrect an intentionally stopped backend", async () => {
    healthy = true;
    bm = makeManager();
    await bm.start();
    bm.stop();
    expect(bm.state).toBe("stopped");

    await bm.reconcile(); // backend is healthy, but the stop was deliberate
    expect(bm.state).toBe("stopped");
  });

  it("a Stop during an in-flight start() wins (no resurrection to running)", async () => {
    // fetch resolves healthy, but stop() runs synchronously while start() is
    // parked on the probe's await — the resumed start() must see itself
    // superseded (epoch bumped) and not write "running" over the user's Stop.
    healthy = true;
    bm = makeManager();
    const starting = bm.start(); // parks at `await probeHealth`
    bm.stop(); // bumps epoch, sets "stopped", before the probe resolves
    await starting;
    expect(bm.state).toBe("stopped");
  });
});

// A spawned child, faked: one-shot processes (the `-c` import probe, the `uv
// sync` repair) emit `exit` on a microtask; the long-lived server stays alive so
// _pollHealth attaches via the health fetch. kill() emits exit so _killChild's
// SIGKILL timer clears and teardown is clean.
class FakeProc extends NodeEventEmitter {
  stdout = new NodeEventEmitter();
  stderr = new NodeEventEmitter();
  exitCode: number | null = null;
  signalCode: string | null = null;
  constructor(opts: { exit?: number | null; longLived?: boolean }) {
    super();
    if (!opts.longLived) {
      queueMicrotask(() => {
        this.exitCode = opts.exit ?? 0;
        this.emit("exit", this.exitCode);
      });
    }
  }
  kill(_signal?: string): boolean {
    if (this.exitCode === null) {
      queueMicrotask(() => this.emit("exit", null));
    }
    return true;
  }
}

describe("BackendManager spawn pre-flight", () => {
  let bm: BackendManager | undefined;
  // Drives the discovery + health-poll fetch: false until a server is spawned.
  let healthy = false;

  // Spawn router: dispatch by argv so the test controls each spawned process
  // independently. Returns a call counter for assertions.
  function installRouter(opts: {
    probeExits: number[]; // exit code per `-c` import probe, in order
    syncExit?: number; // exit code for the `uv sync` repair
    serverHealthy?: boolean; // flip the backend healthy when the server spawns
  }): { probes: number; sync: number; server: number } {
    const calls = { probes: 0, sync: 0, server: 0 };
    spawnMock.mockImplementation((_cmd: string, args?: readonly string[]) => {
      const argv = args ?? [];
      if (argv[0] === "-c") {
        calls.probes++;
        return new FakeProc({ exit: opts.probeExits.shift() ?? 1 });
      }
      if (argv[0] === "sync") {
        calls.sync++;
        return new FakeProc({ exit: opts.syncExit ?? 0 });
      }
      if (argv[0] === "-m") {
        calls.server++;
        if (opts.serverHealthy) {
          healthy = true;
        }
        return new FakeProc({ longLived: true });
      }
      return new FakeProc({ exit: 0 });
    });
    return calls;
  }

  // Empty configuredUrl puts start() on the spawn path (not attach-only).
  function makeSpawnManager(pythonPath = ""): BackendManager {
    return new BackendManager(
      () => pythonPath,
      () => "", // no den.url → spawn path
      () => 8765,
      () => "127.0.0.1",
      () => ""
    );
  }

  // Make resolveSpawnSpec land on the existing-.venv case (2) with a uv repair:
  // a workspace folder, a present .venv python + pyproject.toml, and a uv binary.
  function stubUvWorkspaceVenv(): void {
    const ws = "/ws";
    (mockWorkspace as { workspaceFolders?: unknown }).workspaceFolders = [
      { uri: { fsPath: ws } },
    ];
    const venvPython = path.join(ws, ".venv", "bin", "python");
    existsSyncMock.mockImplementation((p: fs.PathLike) => {
      const s = String(p);
      return (
        s === venvPython ||
        s === path.join(ws, "pyproject.toml") ||
        s.endsWith(`${path.sep}uv`)
      );
    });
  }

  beforeEach(() => {
    __resetVscodeMock();
    healthy = false;
    spawnMock.mockReset();
    existsSyncMock.mockReset();
    existsSyncMock.mockReturnValue(false);
    vi.stubGlobal("fetch", async () => {
      if (!healthy) {
        throw new Error("ECONNREFUSED");
      }
      return {
        ok: true,
        json: async () => ({ status: "ok" }),
      } as unknown as Response;
    });
  });

  afterEach(() => {
    bm?.dispose();
    bm = undefined;
    vi.unstubAllGlobals();
    delete (mockWorkspace as { workspaceFolders?: unknown }).workspaceFolders;
  });

  it("a pre-flight failure errors with guidance and never spawns the server", async () => {
    // System python3 (no workspace, no .venv) that cannot import the studio
    // server: the import probe exits non-zero, there is no repair, so Den must
    // error fast without paying for a doomed `-m lionagi.studio` spawn.
    const calls = installRouter({ probeExits: [1] });
    bm = makeSpawnManager("");
    await bm.start();
    expect(bm.state).toBe("error");
    expect(calls.server).toBe(0);
    expect(mockWindow.showErrorMessage).toHaveBeenCalled();
  });

  it("a pre-flight pass proceeds to spawn and reaches running", async () => {
    const calls = installRouter({ probeExits: [0], serverHealthy: true });
    bm = makeSpawnManager("");
    await bm.start();
    expect(bm.state).toBe("running");
    expect(calls.server).toBe(1);
  });

  it("repairs a studio-less workspace .venv in place, then runs", async () => {
    stubUvWorkspaceVenv();
    mockWindow.withProgress.mockImplementation(
      (_opts: unknown, task: (p: unknown, t: unknown) => Promise<unknown>) =>
        task(undefined, undefined)
    );
    // First import probe fails (studio missing) → uv sync repairs → second probe
    // passes → server spawns.
    const calls = installRouter({
      probeExits: [1, 0],
      syncExit: 0,
      serverHealthy: true,
    });
    bm = makeSpawnManager("");
    await bm.start();
    expect(bm.state).toBe("running");
    expect(calls.sync).toBe(1);
    expect(calls.server).toBe(1);
  });

  it("a failed repair still errors without spawning the server", async () => {
    stubUvWorkspaceVenv();
    mockWindow.withProgress.mockImplementation(
      (_opts: unknown, task: (p: unknown, t: unknown) => Promise<unknown>) =>
        task(undefined, undefined)
    );
    const calls = installRouter({ probeExits: [1], syncExit: 1 });
    bm = makeSpawnManager("");
    await bm.start();
    expect(bm.state).toBe("error");
    expect(calls.sync).toBe(1);
    expect(calls.server).toBe(0);
    expect(mockWindow.showErrorMessage).toHaveBeenCalled();
  });
});
