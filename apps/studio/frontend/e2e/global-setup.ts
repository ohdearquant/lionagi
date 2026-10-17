import { spawn, type ChildProcessByStdio } from "node:child_process";
import fs from "node:fs";
import type { Readable } from "node:stream";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { FullConfig } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// e2e/ -> frontend -> studio -> apps -> repo root
const REPO_ROOT = path.resolve(__dirname, "../../../..");
const FRONTEND_ROOT = path.resolve(__dirname, "..");
// Each server's output for the run, so a failed run shows why a server stopped
// answering: `studio-daemon.log` (the `uv run` wrapper's stdio),
// `studio-daemon-child.log` (the daemon itself), and `vite-preview.log`. CI
// uploads this directory when the job fails. The name is also spelled in
// .gitignore and in the CI upload path; this constant is its origin here, so a
// rename cannot leave a copy behind in this file.
const LOG_DIR_NAME = "e2e-logs";
const LOG_DIR = path.join(FRONTEND_ROOT, LOG_DIR_NAME);
// The daemon child writes its own output (uvicorn's log, and the Python stack
// of every thread on a native crash) to this file. Setup names it through
// CHILD_LOG_ENV so it lands in LOG_DIR and outlives the wrapper's temp dir,
// which the wrapper deletes on stop.
const CHILD_LOG_PATH = path.join(LOG_DIR, "studio-daemon-child.log");
const CHILD_LOG_ENV = "LIONAGI_E2E_STUDIO_CHILD_LOG";

type Proc = ChildProcessByStdio<null, Readable, Readable>;

interface RunningServer {
  proc: Proc;
  name: string;
}

/**
 * Per-process output log state. `childClosed` records the child's `close`
 * (when `recordOutput` writes the exit line) and `written` the log stream's
 * own `finish`, so a caller can wait for the exit line to reach the file
 * without ending the stream a second time.
 */
interface OutputLog {
  stream: fs.WriteStream;
  childClosed: boolean;
  written: boolean;
}

const outputLogs = new Map<Proc, OutputLog>();

// Vite colors its CLI output; naive substring/regex matching on raw stdout
// breaks on the embedded ANSI escape codes, so every buffer is stripped
// before it is matched against.
const ANSI_PATTERN = /\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g;
function stripAnsi(text: string): string {
  return text.replace(ANSI_PATTERN, "");
}

const PORT_IN_USE_PATTERN = /EADDRINUSE|already in use/i;

/** Discard the previous run's logs and recreate the directory, once, before any spawn. */
function resetLogDir(): void {
  fs.rmSync(LOG_DIR, { recursive: true, force: true });
  fs.mkdirSync(LOG_DIR, { recursive: true });
}

/**
 * Append everything *proc* writes to `<logDir>/<name>.log`, with its start and
 * its exit code or signal. Attempts within one run append, so a port retry
 * keeps the failed attempt's lines. The listeners stay for the life of the
 * process, so output after the readiness line is kept too; exit is recorded on
 * `close`, which fires after both pipes have drained.
 *
 * `logDir` already exists here (`resetLogDir` runs first), and a stream error
 * is reported and dropped rather than thrown, so a full or unwritable log
 * directory cannot abort setup with a server already spawned and untracked.
 */
export function recordOutput(proc: Proc, name: string, logDir: string = LOG_DIR): Proc {
  const log = fs.createWriteStream(path.join(logDir, `${name}.log`), { flags: "a" });
  const state: OutputLog = { stream: log, childClosed: false, written: false };
  outputLogs.set(proc, state);
  log.on("close", () => {
    state.written = true;
  });
  log.on("error", (err) => {
    console.error(`e2e log ${name}: ${String(err)}`);
  });
  log.write(`${new Date().toISOString()} started pid=${proc.pid}\n`);
  proc.stdout.on("data", (chunk: Buffer) => log.write(chunk));
  proc.stderr.on("data", (chunk: Buffer) => log.write(chunk));
  proc.on("close", (code, signal) => {
    state.childClosed = true;
    log.end(`${new Date().toISOString()} exited code=${code} signal=${signal}\n`);
  });
  return proc;
}

/** Bind to port 0 and read back the OS-assigned free port. */
function getFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (address && typeof address === "object") {
        const port = address.port;
        server.close(() => resolve(port));
      } else {
        server.close(() => reject(new Error("failed to allocate a free port")));
      }
    });
  });
}

function waitForOutput(
  proc: Proc,
  matcher: (line: string) => boolean,
  opts: { timeoutMs: number; label: string },
): Promise<void> {
  return new Promise((resolve, reject) => {
    let buffer = "";
    let settled = false;

    const timer = setTimeout(() => {
      finish(
        new Error(`${opts.label} did not become ready within ${opts.timeoutMs}ms:\n${buffer}`),
      );
    }, opts.timeoutMs);

    function finish(err?: Error) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      proc.stdout.off("data", onData);
      proc.stderr.off("data", onData);
      proc.off("exit", onExit);
      if (err) reject(err);
      else resolve();
    }

    function onData(chunk: Buffer) {
      buffer += stripAnsi(chunk.toString());
      if (buffer.split("\n").some(matcher)) finish();
    }

    function onExit(code: number | null) {
      finish(new Error(`${opts.label} exited early (code=${code}):\n${buffer}`));
    }

    proc.stdout.on("data", onData);
    proc.stderr.on("data", onData);
    proc.on("exit", onExit);
  });
}

async function waitForHttp(url: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch (err) {
      lastError = err;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`timed out waiting for ${url}: ${String(lastError)}`);
}

/**
 * Resolve once the log stream has finished, so every byte `recordOutput` wrote
 * (including the exit line on the child's `close`) is on disk. The stream ends
 * itself in that `close` handler, so this only waits -- it never ends the
 * stream again. A bounded fallback keeps a wedged filesystem from stalling
 * teardown.
 */
function flushLog(proc: Proc | undefined, timeoutMs = 2_000): Promise<void> {
  const state = proc && outputLogs.get(proc);
  if (!state || state.written) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, timeoutMs);
    state.stream.once("close", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

/**
 * Resolve once the child has closed (so `recordOutput` has written its exit
 * line) and the log stream has flushed. `close` -- not `exit` -- is the signal
 * that both pipes have drained, so a `killProcess` that resolved on `exit`
 * could leave the exit line off disk when the caller spawned the next attempt.
 * The waits are bounded: after a SIGKILL the child is gone, and a second that
 * its `close` has still not fired must not hold teardown open.
 */
function waitForClose(proc: Proc, timeoutMs: number): Promise<void> {
  // `exitCode`/`signalCode` are set on `exit`, which can fire before `close`;
  // only the log's own `childClosed` flag means `recordOutput` has written the
  // exit line. A child that already closed resolves immediately, so a repeat
  // call cannot wait for an event that will never come again.
  if (outputLogs.get(proc)?.childClosed) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      proc.off("close", onClose);
      resolve();
    }, timeoutMs);
    function onClose() {
      clearTimeout(timer);
      resolve();
    }
    proc.once("close", onClose);
  });
}

/**
 * Terminate *proc* and everything it spawned, resolving only after the child's
 * exit line has been written to its log. Both the daemon and the preview server
 * are started with `detached: true` (their own process group), because `uv run`
 * and `npx` are themselves wrapper processes that may or may not exec() into
 * the real interpreter -- signaling only the immediate child can leave a
 * grandchild (uvicorn, vite) running as an orphan. `-pid` signals the whole
 * group instead of just the one process.
 *
 * The 10s SIGKILL fallback is kept for a child that ignores SIGTERM; after it
 * fires the child still has to reach `close` for its log to be complete, but
 * that wait is bounded so a group that never closes cannot stall teardown.
 */
export async function killProcess(proc: Proc | undefined): Promise<void> {
  if (!proc) return;
  if (proc.exitCode === null && proc.signalCode === null && proc.pid) {
    const pid = proc.pid;
    const killed = new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        killProcessGroup(pid);
        resolve();
      }, 10_000);
      proc.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
    try {
      process.kill(-pid, "SIGTERM");
    } catch {
      proc.kill("SIGTERM");
    }
    await killed;
  }
  await waitForClose(proc, 2_000);
  await flushLog(proc);
}

interface StartResult {
  proc: Proc;
  port: number;
}

/** The servers this run started, for the exit handler above. */
const servers: RunningServer[] = [];

/** Record a spawned server so `killServersOnExit` can reach it by name and pid. */
function trackServer(proc: Proc, name: string): Proc {
  servers.push({ proc, name });
  return proc;
}

/** Signal a whole process group, ignoring a group that is already gone. */
function killProcessGroup(pgid: number): void {
  try {
    process.kill(-pgid, "SIGKILL");
  } catch {
    // group already gone
  }
}

/**
 * Run from `process.once("exit")` for the life of the run: when Playwright's
 * `globalTimeout` ends the process, `globalTeardown` never runs, so the servers
 * are still up and their group would be killed with nothing written to say why.
 * For each server still running this appends one line synchronously
 * (`fs.appendFileSync`, because an async write does not land during `exit`) and
 * SIGKILLs its process group. Once normal teardown has stopped the servers,
 * every entry is already exited and nothing here fires.
 */
export function killServersOnExit(started: RunningServer[], logDir: string = LOG_DIR): void {
  for (const { proc, name } of started) {
    if (proc.exitCode !== null || proc.signalCode !== null) continue;
    const pgid = proc.pid;
    if (!pgid) continue;
    try {
      fs.appendFileSync(
        path.join(logDir, `${name}.log`),
        `${new Date().toISOString()} runner exiting before teardown; killing process group ${pgid}\n`,
      );
    } catch {
      // the log directory is gone; the kill below still matters
    }
    killProcessGroup(pgid);
  }
}

/**
 * Spawns via *spawnFn(port)*, waits for *ready*, and retries on a fresh free
 * port if the child reports the port was already taken -- getFreePort()'s
 * allocate-then-close leaves a window where a concurrent run on this
 * machine (several worktrees/agents commonly run at once here) can grab the
 * same port first.
 */
async function startOnFreePort(
  spawnFn: (port: number) => Proc,
  ready: (proc: Proc) => Promise<void>,
  label: string,
  maxAttempts = 5,
): Promise<StartResult> {
  let lastErr: unknown;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const port = await getFreePort();
    const proc = spawnFn(port);
    try {
      await ready(proc);
      return { proc, port };
    } catch (err) {
      await killProcess(proc);
      const message = err instanceof Error ? err.message : String(err);
      lastErr = err;
      if (!PORT_IN_USE_PATTERN.test(message) || attempt === maxAttempts) {
        throw err;
      }
      console.warn(
        `${label}: port ${port} was taken by another process, retrying (attempt ${attempt}/${maxAttempts})`,
      );
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(`${label}: exhausted retries`);
}

/**
 * Brings up a seeded Lion Studio daemon (Python, temp dir + freshly seeded
 * db -- see tests/e2e_studio/) and a `vite preview` static server proxying
 * /api to it, both on dynamically-allocated free ports so concurrent runs on
 * this machine never collide. Returns a teardown closure that tears both
 * down and lets the daemon clean up its own temp dir. Any failure partway
 * through setup tears down whatever already started before rethrowing, so a
 * broken run never leaks a daemon/preview process or a seeded temp dir.
 */
export default async function globalSetup(_config: FullConfig) {
  let daemon: Proc | undefined;
  let preview: Proc | undefined;

  // Before any server is spawned: a filesystem failure here must not leave a
  // detached daemon or preview running with no teardown handle.
  resetLogDir();
  // One handler for the whole setup, so Playwright's global timeout -- which
  // ends the process without running globalTeardown -- still records that the
  // run's servers were killed and kills them. Nothing here fires once normal
  // teardown has stopped them.
  process.once("exit", () => killServersOnExit(servers));

  try {
    const daemonResult = await startOnFreePort(
      (port) =>
        trackServer(
          recordOutput(
            spawn(
              "uv",
              ["run", "python", "-m", "tests.e2e_studio.run_seeded_daemon", "--port", String(port)],
              {
                cwd: REPO_ROOT,
                // PYTHONUNBUFFERED keeps what the daemon printed before it stopped;
                // PYTHONFAULTHANDLER prints the Python stack of every thread to
                // stderr on a native crash. CHILD_LOG_ENV sends that stderr, plus
                // uvicorn's own log, into LOG_DIR instead of the wrapper's temp
                // dir, which the wrapper deletes on stop.
                env: {
                  ...process.env,
                  PYTHONFAULTHANDLER: "1",
                  PYTHONUNBUFFERED: "1",
                  [CHILD_LOG_ENV]: CHILD_LOG_PATH,
                },
                stdio: ["ignore", "pipe", "pipe"],
                detached: true,
              },
            ),
            "studio-daemon",
          ),
        ),
      (proc) =>
        waitForOutput(proc, (line) => line.includes("studio-e2e-daemon-ready"), {
          timeoutMs: 45_000,
          label: "seeded studio daemon",
        }),
      "seeded studio daemon",
    );
    daemon = daemonResult.proc;
    const apiPort = daemonResult.port;
    await waitForHttp(`http://127.0.0.1:${apiPort}/health`, 10_000);

    const previewResult = await startOnFreePort(
      (port) =>
        trackServer(
          recordOutput(
            spawn(
              "npx",
              ["vite", "preview", "--host", "127.0.0.1", "--port", String(port), "--strictPort"],
              {
                cwd: FRONTEND_ROOT,
                env: { ...process.env, STUDIO_E2E_API_PORT: String(apiPort) },
                stdio: ["ignore", "pipe", "pipe"],
                detached: true,
              },
            ),
            "vite-preview",
          ),
        ),
      (proc) =>
        waitForOutput(proc, (line) => /Local:\s*http/i.test(line), {
          timeoutMs: 30_000,
          label: "vite preview",
        }),
      "vite preview",
    );
    preview = previewResult.proc;
    const previewPort = previewResult.port;

    const baseURL = `http://127.0.0.1:${previewPort}`;
    await waitForHttp(baseURL, 15_000);
    process.env.E2E_BASE_URL = baseURL;

    return async function globalTeardown() {
      await killProcess(preview);
      await killProcess(daemon);
    };
  } catch (err) {
    await killProcess(preview);
    await killProcess(daemon);
    throw err;
  }
}
