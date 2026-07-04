import { spawn, type ChildProcessByStdio } from "node:child_process";
import type { Readable } from "node:stream";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { FullConfig } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// e2e/ -> frontend -> studio -> apps -> repo root
const REPO_ROOT = path.resolve(__dirname, "../../../..");
const FRONTEND_ROOT = path.resolve(__dirname, "..");

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
  proc: ChildProcessByStdio<null, Readable, Readable>,
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
      buffer += chunk.toString();
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
 * Terminate *proc* and everything it spawned. Both the daemon and the
 * preview server are started with `detached: true` (their own process
 * group), because `uv run` and `npx` are themselves wrapper processes that
 * may or may not exec() into the real interpreter -- signaling only the
 * immediate child can leave a grandchild (uvicorn, vite) running as an
 * orphan. `-pid` signals the whole group instead of just the one process.
 */
function killProcess(proc: ChildProcessByStdio<null, Readable, Readable>): Promise<void> {
  return new Promise((resolve) => {
    if (proc.exitCode !== null || proc.signalCode !== null || !proc.pid) {
      resolve();
      return;
    }
    const pid = proc.pid;
    const timer = setTimeout(() => {
      try {
        process.kill(-pid, "SIGKILL");
      } catch {
        // group already gone
      }
    }, 10_000);
    proc.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    try {
      process.kill(-pid, "SIGTERM");
    } catch {
      proc.kill("SIGTERM");
    }
  });
}

/**
 * Brings up a seeded Lion Studio daemon (Python, temp dir + freshly seeded
 * db -- see tests/e2e_studio/) and a `vite preview` static server proxying
 * /api to it, both on dynamically-allocated free ports so concurrent runs on
 * this machine never collide. Returns a teardown closure that tears both
 * down and lets the daemon clean up its own temp dir.
 */
export default async function globalSetup(_config: FullConfig) {
  const apiPort = await getFreePort();
  const previewPort = await getFreePort();

  const daemon = spawn(
    "uv",
    ["run", "python", "-m", "tests.e2e_studio.run_seeded_daemon", "--port", String(apiPort)],
    { cwd: REPO_ROOT, stdio: ["ignore", "pipe", "pipe"] },
  );

  await waitForOutput(daemon, (line) => line.includes("studio-e2e-daemon-ready"), {
    timeoutMs: 45_000,
    label: "seeded studio daemon",
  });
  await waitForHttp(`http://127.0.0.1:${apiPort}/health`, 10_000);

  const preview = spawn(
    "npx",
    ["vite", "preview", "--host", "127.0.0.1", "--port", String(previewPort), "--strictPort"],
    {
      cwd: FRONTEND_ROOT,
      env: { ...process.env, STUDIO_E2E_API_PORT: String(apiPort) },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  await waitForOutput(preview, (line) => /Local:\s*http/i.test(line), {
    timeoutMs: 30_000,
    label: "vite preview",
  });

  const baseURL = `http://127.0.0.1:${previewPort}`;
  await waitForHttp(baseURL, 15_000);
  process.env.E2E_BASE_URL = baseURL;

  return async function globalTeardown() {
    await killProcess(preview);
    await killProcess(daemon);
  };
}
