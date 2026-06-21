import * as child_process from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

export type BackendState = "stopped" | "starting" | "running" | "error";

interface SpawnSpec {
  command: string;
  args: string[];
  cwd?: string;
}

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function resolveUv(): string | undefined {
  const candidates = [
    path.join(os.homedir(), ".local", "bin", "uv"),
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
  ];
  return candidates.find((p) => fs.existsSync(p));
}

/** Resolve the Python interpreter or uv run spec for spawning the backend. */
function resolveSpawnSpec(explicitPython: string): SpawnSpec {
  const ws = workspaceRoot();

  // 1. Explicitly configured path.
  if (explicitPython.trim() !== "") {
    return { command: explicitPython, args: ["-m", "lionagi.studio"] };
  }

  // 2. Workspace venv.
  if (ws) {
    const unix = path.join(ws, ".venv", "bin", "python");
    const win = path.join(ws, ".venv", "Scripts", "python.exe");
    if (fs.existsSync(unix)) {
      return { command: unix, args: ["-m", "lionagi.studio"] };
    }
    if (fs.existsSync(win)) {
      return { command: win, args: ["-m", "lionagi.studio"] };
    }
  }

  // 3. uv run (if uv and pyproject.toml are present).
  if (ws) {
    const uv = resolveUv();
    if (uv && fs.existsSync(path.join(ws, "pyproject.toml"))) {
      return {
        command: uv,
        args: ["run", "python", "-m", "lionagi.studio"],
        cwd: ws,
      };
    }
  }

  // 4. System python3 fallback.
  return { command: "python3", args: ["-m", "lionagi.studio"] };
}

/** Single-shot health probe with a short timeout. Returns true on success. */
async function probeHealth(url: string): Promise<boolean> {
  try {
    const res = await fetch(`${url}/health`, {
      signal: AbortSignal.timeout(1500),
    });
    if (!res.ok) {
      return false;
    }
    const body = (await res.json()) as { status?: string };
    return body.status === "ok";
  } catch {
    return false;
  }
}

export class BackendManager implements vscode.Disposable {
  private _state: BackendState = "stopped";
  private _child: child_process.ChildProcess | undefined;
  private _output: vscode.OutputChannel;
  private _pollTimer: ReturnType<typeof setTimeout> | undefined;
  private _effectiveBaseUrl: string | undefined;

  private readonly _onDidChangeState =
    new vscode.EventEmitter<BackendState>();
  readonly onDidChangeState = this._onDidChangeState.event;

  constructor(
    private readonly getPythonPath: () => string,
    private readonly getConfiguredUrl: () => string,
    private readonly getPort: () => number,
    private readonly getHost: () => string,
    private readonly getToken: () => string
  ) {
    this._output = vscode.window.createOutputChannel("Den");
  }

  get state(): BackendState {
    return this._state;
  }

  /** The live base URL (attached or spawned). Falls back to config-derived default. */
  get baseUrl(): string {
    if (this._effectiveBaseUrl) {
      return this._effectiveBaseUrl;
    }
    const configured = this.getConfiguredUrl().trim().replace(/\/$/, "");
    if (configured) {
      return configured;
    }
    return `http://${this.getHost()}:${this.getPort()}`;
  }

  isRunning(): boolean {
    return this._state === "running";
  }

  private setState(s: BackendState): void {
    if (this._state !== s) {
      this._state = s;
      this._onDidChangeState.fire(s);
    }
  }

  async start(): Promise<void> {
    if (this._state === "running" || this._state === "starting") {
      return;
    }

    this.setState("starting");

    // --- A. Discovery phase ---
    const host = this.getHost();
    const port = this.getPort();
    const configuredUrl = this.getConfiguredUrl().trim().replace(/\/$/, "");
    const defaultUrl = `http://${host}:${port}`;

    // When den.url is explicitly configured, attach-only: probe that URL and
    // surface an error if it is unreachable rather than silently falling back
    // to spawning a local backend that the user did not ask for.
    if (configuredUrl) {
      const found = await probeHealth(configuredUrl);
      if (found) {
        this._effectiveBaseUrl = configuredUrl;
        this.setState("running");
        this._output.appendLine(
          `[lifecycle] attached to existing backend at ${configuredUrl}`
        );
        return;
      }
      this._output.appendLine(
        `[lifecycle] configured backend at ${configuredUrl} is unreachable`
      );
      this.setState("error");
      void vscode.window.showErrorMessage(
        `Den: cannot reach configured backend at ${configuredUrl}. ` +
          `Check the den.url setting or clear it to let Den manage a local backend.`
      );
      return;
    }

    // No configured URL: probe the default local address before spawning.
    if (await probeHealth(defaultUrl)) {
      this._effectiveBaseUrl = defaultUrl;
      this.setState("running");
      this._output.appendLine(
        `[lifecycle] attached to existing backend at ${defaultUrl}`
      );
      return;
    }

    // --- B. Spawn phase ---
    const spec = resolveSpawnSpec(this.getPythonPath());
    this._effectiveBaseUrl = defaultUrl;

    this._output.appendLine(
      `[lifecycle] spawning via ${spec.command} ${spec.args.join(" ")}`
    );

    const token = this.getToken();
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      LIONAGI_STUDIO_PORT: String(port),
      LIONAGI_STUDIO_HOST: host,
    };
    if (token) {
      env["LIONAGI_STUDIO_AUTH_TOKEN"] = token;
    }

    let spawnFailed = false;
    let earlyExit = false;

    const child = child_process.spawn(spec.command, spec.args, {
      env,
      cwd: spec.cwd,
      stdio: ["ignore", "pipe", "pipe"],
    });

    this._child = child;

    child.stdout?.on("data", (data: Buffer) => {
      this._output.append(data.toString());
    });
    child.stderr?.on("data", (data: Buffer) => {
      this._output.append(data.toString());
    });

    child.on("error", (err) => {
      this._output.appendLine(`[lifecycle] spawn error: ${err.message}`);
      spawnFailed = true;
      this.setState("error");
      void vscode.window.showErrorMessage(
        `Den: failed to start backend — ${err.message}. ` +
          `Check the den.pythonPath setting.`
      );
    });

    child.on("exit", (code) => {
      this._output.appendLine(`[lifecycle] exited with code ${code}`);
      if (this._state === "starting" && code !== null && code !== 0) {
        earlyExit = true;
        spawnFailed = true;
        this.setState("error");
        void vscode.window.showErrorMessage(
          `Den: backend exited (code ${code}) before becoming healthy. ` +
            `The Python at ${spec.command} may be missing the studio server — ` +
            `set den.pythonPath, or install it with 'pip install "lionagi[studio]"'. ` +
            `See the Den output channel.`
        );
      } else if (this._state !== "stopped") {
        this.setState("error");
      }
    });

    const ok = await this._pollHealth(30_000, () => spawnFailed);
    if (spawnFailed || earlyExit) {
      // error state already set above via the child event handlers
    } else if (!ok) {
      // Health-check timed out: kill the child so it does not linger as an
      // orphan, then transition to error so a retry spawns a fresh process
      // rather than racing the still-running unhealthy one.
      if (this._child) {
        const child = this._child;
        this._child = undefined;
        child.kill();
        const killTimer = setTimeout(() => {
          if (child.exitCode === null && child.signalCode === null) {
            child.kill("SIGKILL");
          }
        }, 3_000);
        child.once("exit", () => clearTimeout(killTimer));
      }
      this.setState("error");
    } else {
      this.setState("running");
    }
  }

  stop(): void {
    if (this._pollTimer !== undefined) {
      clearTimeout(this._pollTimer);
      this._pollTimer = undefined;
    }
    if (this._child) {
      const child = this._child;
      this._child = undefined;
      child.kill();
      // Escalate to SIGKILL if the process does not exit within 3s.
      const killTimer = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) {
          child.kill("SIGKILL");
        }
      }, 3_000);
      child.once("exit", () => clearTimeout(killTimer));
    }
    this._effectiveBaseUrl = undefined;
    this.setState("stopped");
  }

  /** Poll GET /health until it returns true, timeout elapses, or shouldAbort() is true. */
  private async _pollHealth(
    timeoutMs: number,
    shouldAbort?: () => boolean
  ): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    const interval = 1_000;

    while (Date.now() < deadline) {
      if (shouldAbort?.()) {
        return false;
      }
      try {
        const res = await fetch(`${this.baseUrl}/health`);
        if (res.ok) {
          const body = (await res.json()) as { status?: string };
          if (body.status === "ok") {
            return true;
          }
        }
      } catch {
        // backend not up yet
      }
      await new Promise<void>((resolve) =>
        setTimeout(resolve, interval)
      );
    }
    this._output.appendLine("[lifecycle] health-check timed out");
    return false;
  }

  dispose(): void {
    this.stop();
    this._onDidChangeState.dispose();
    this._output.dispose();
  }
}
