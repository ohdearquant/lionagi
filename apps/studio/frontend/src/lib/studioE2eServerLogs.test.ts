import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { killProcess, killServersOnExit, recordOutput } from "../../e2e/global-setup";

// A real child process, not a stub: the ordering under test depends on the
// child's own `close` firing after `exit`. The script prints and exits within a
// second and starts no server -- it stands in for a port-retry attempt that
// already died, which is the case killProcess has to finish logging.
const FAST_CHILD = "process.stdout.write('child output line\\n'); process.exit(0);";

const tmpDirs: string[] = [];

function makeLogDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "e2e-logs-"));
  tmpDirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

describe("killProcess log ordering", () => {
  it("resolves only after the child has closed and its log has flushed", async () => {
    const logDir = makeLogDir();
    const proc = spawn(process.execPath, ["-e", FAST_CHILD], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    const events: string[] = [];
    recordOutput(proc, "studio-daemon", logDir);
    // Capture `close` before any await: it can fire in the same tick as
    // `exit`, and the exit line is written from `recordOutput`'s own close
    // handler. Wait only for `exit` -- at that point the child has died but
    // its exit line is not guaranteed to be on disk, which is the state a
    // port retry leaves the failed attempt in when it calls killProcess.
    proc.once("close", () => events.push("close"));
    await new Promise<void>((resolve) => proc.once("exit", () => resolve()));

    await killProcess(proc);
    events.push("killProcess resolved");

    expect(events).toEqual(["close", "killProcess resolved"]);
    const log = fs.readFileSync(path.join(logDir, "studio-daemon.log"), "utf8");
    expect(log).toContain("child output line");
    expect(log).toMatch(/exited code=/);
  });
});

describe("killServersOnExit", () => {
  it("appends a line and kills a still-running server's group", () => {
    const logDir = makeLogDir();
    // A pid far above the OS maximum: its process group cannot exist, so the
    // SIGKILL is a no-op rather than a real kill of this test's process.
    const started = [
      { proc: { pid: 2_000_000, exitCode: null, signalCode: null }, name: "studio-daemon" },
    ];

    killServersOnExit(started as never, logDir);

    const log = fs.readFileSync(path.join(logDir, "studio-daemon.log"), "utf8");
    expect(log).toMatch(/runner exiting before teardown; killing process group 2000000/);
  });

  it("does nothing once normal teardown has stopped the servers", () => {
    const logDir = makeLogDir();
    const stopped = [
      { proc: { pid: 2_000_000, exitCode: 0, signalCode: null }, name: "studio-daemon" },
      // A server SIGKILLed by the 10s fallback has no exit code, only a signal;
      // it is already gone and must not be logged or signalled again.
      { proc: { pid: 2_000_001, exitCode: null, signalCode: "SIGKILL" }, name: "vite-preview" },
    ];

    killServersOnExit(stopped as never, logDir);

    expect(fs.existsSync(path.join(logDir, "studio-daemon.log"))).toBe(false);
    expect(fs.existsSync(path.join(logDir, "vite-preview.log"))).toBe(false);
  });
});
