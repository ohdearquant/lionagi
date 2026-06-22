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
  /** One-time dependency install to run (with progress) before spawning. */
  provision?: { command: string; args: string[]; cwd: string };
  /**
   * Verify the studio server imports before spawning. Set for interpreters
   * whose provisioning Den does not control (an explicit den.pythonPath, an
   * already-present .venv, or system python3); not set when `provision` already
   * guarantees the deps.
   */
  preflight?: boolean;
  /**
   * Repair step to run if the preflight import check fails and Den can fix this
   * interpreter in place (a uv-managed workspace .venv that is missing the
   * studio extra). Same sync the source-checkout path uses.
   */
  repair?: { command: string; args: string[]; cwd: string };
}

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function resolveUv(): string | undefined {
  const exe = process.platform === "win32" ? "uv.exe" : "uv";
  const pathDirs = (process.env.PATH ?? "").split(path.delimiter);
  const knownDirs = [
    path.join(os.homedir(), ".local", "bin"),
    path.join(os.homedir(), ".cargo", "bin"),
    "/usr/local/bin",
    "/opt/homebrew/bin",
  ];
  for (const dir of [...pathDirs, ...knownDirs]) {
    if (dir && fs.existsSync(path.join(dir, exe))) {
      return path.join(dir, exe);
    }
  }
  return undefined;
}

/** Resolve the Python interpreter (and any provisioning step) for the backend. */
function resolveSpawnSpec(explicitPython: string): SpawnSpec {
  const ws = workspaceRoot();
  const venvPython = ws
    ? process.platform === "win32"
      ? path.join(ws, ".venv", "Scripts", "python.exe")
      : path.join(ws, ".venv", "bin", "python")
    : undefined;

  // 1. Explicitly configured path.
  if (explicitPython.trim() !== "") {
    return {
      command: explicitPython,
      args: ["-m", "lionagi.studio"],
      preflight: true,
    };
  }

  // 2. Existing workspace venv (assumed provisioned). Pre-flight it: a .venv the
  //    user created without the studio extra would otherwise spawn-and-fail. If
  //    this is a uv-managed project, offer an in-place repair (same sync as the
  //    source-checkout path) so a studio-less .venv self-heals.
  if (venvPython && fs.existsSync(venvPython)) {
    const spec: SpawnSpec = {
      command: venvPython,
      args: ["-m", "lionagi.studio"],
      preflight: true,
    };
    if (ws && fs.existsSync(path.join(ws, "pyproject.toml"))) {
      const uv = resolveUv();
      if (uv) {
        spec.repair = {
          command: uv,
          args: ["sync", "--extra", "studio", "--no-dev"],
          cwd: ws,
        };
      }
    }
    return spec;
  }

  // 3. uv-managed source checkout (e.g. Codespaces): sync the studio extra
  //    into .venv first, then run that interpreter. Zero-config from source.
  //    --no-dev skips the dev group (sphinx, mkdocs, pyarrow, jupyter, the
  //    pytest suite, profilers) the studio server never imports — the studio
  //    extra itself is four light deps, so this is most of the first-run wait.
  //    uv re-adds the dev group on demand if the user later runs the test suite.
  if (ws && venvPython && fs.existsSync(path.join(ws, "pyproject.toml"))) {
    const uv = resolveUv();
    if (uv) {
      return {
        command: venvPython,
        args: ["-m", "lionagi.studio"],
        cwd: ws,
        provision: {
          command: uv,
          args: ["sync", "--extra", "studio", "--no-dev"],
          cwd: ws,
        },
      };
    }
  }

  // 4. System python3 fallback (expects `pip install "lionagi[studio]"`).
  return { command: "python3", args: ["-m", "lionagi.studio"], preflight: true };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Health probe with optional retries. Tolerant by default so a transient
 * backend slowdown is not mistaken for "no backend" — which would otherwise
 * trigger a doomed respawn against the still-bound port (EADDRINUSE).
 */
async function probeHealth(
  url: string,
  opts: { timeoutMs?: number; retries?: number } = {}
): Promise<boolean> {
  const timeoutMs = opts.timeoutMs ?? 1500;
  const retries = opts.retries ?? 0;
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await fetch(`${url}/health`, {
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (res.ok) {
        const body = (await res.json()) as { status?: string };
        if (body.status === "ok") {
          return true;
        }
      }
    } catch {
      // not reachable on this attempt
    }
    if (attempt >= retries) {
      return false;
    }
    await delay(250);
  }
}

// How often the supervisor reconciles Den's state against a live health probe.
const SUPERVISE_INTERVAL_MS = 8_000;

export class BackendManager implements vscode.Disposable {
  private _state: BackendState = "stopped";
  private _child: child_process.ChildProcess | undefined;
  private _output: vscode.OutputChannel;
  private _effectiveBaseUrl: string | undefined;
  private _supervisorTimer: ReturnType<typeof setInterval> | undefined;
  private _reconciling = false;
  private _missedHealthChecks = 0;
  // Bumped by every start()/stop(); a start() that finds its captured value
  // stale was superseded mid-flight and must not write state. Guards the
  // "user clicks Stop while start() is parked on an await" resurrection race.
  private _epoch = 0;
  // True when attached to a backend Den did not spawn (so stop() can't kill it).
  private _attachedUnmanaged = false;

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
    if (s === "running") {
      this._missedHealthChecks = 0;
    }
    if (this._state !== s) {
      this._state = s;
      this._onDidChangeState.fire(s);
    }
  }

  async start(): Promise<void> {
    if (this._state === "running" || this._state === "starting") {
      return;
    }

    // Claim this start generation. Any later start()/stop() bumps _epoch; a
    // stale generation must not write state after its awaits resolve.
    const epoch = ++this._epoch;
    this.setState("starting");
    // Supervise from the first start so even a failed start self-recovers when
    // the backend becomes reachable, without the manual status-bar click.
    this._startSupervisor();

    // --- A. Discovery phase ---
    const host = this.getHost();
    const port = this.getPort();
    const configuredUrl = this.getConfiguredUrl().trim().replace(/\/$/, "");
    const defaultUrl = `http://${host}:${port}`;

    // When den.url is explicitly configured, attach-only: probe that URL and
    // surface an error if it is unreachable rather than silently falling back
    // to spawning a local backend that the user did not ask for.
    if (configuredUrl) {
      const found = await probeHealth(configuredUrl, {
        timeoutMs: 2500,
        retries: 2,
      });
      if (this._superseded(epoch)) {
        return;
      }
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
    // Tolerant retry so a slow-but-alive backend is attached, not respawned.
    const alreadyServing = await probeHealth(defaultUrl, {
      timeoutMs: 2500,
      retries: 2,
    });
    if (this._superseded(epoch)) {
      return;
    }
    if (alreadyServing) {
      this._effectiveBaseUrl = defaultUrl;
      this._attachedUnmanaged = true;
      this.setState("running");
      this._output.appendLine(
        `[lifecycle] attached to existing backend at ${defaultUrl}`
      );
      return;
    }

    // --- B. Spawn phase ---
    const spec = resolveSpawnSpec(this.getPythonPath());
    this._effectiveBaseUrl = defaultUrl;

    // First run on a source checkout installs the studio backend deps so Den
    // works zero-config; later starts find the synced .venv and skip this.
    if (spec.provision) {
      const provisioned = await this._provision(spec.provision);
      if (this._superseded(epoch)) {
        return;
      }
      if (!provisioned) {
        this.setState("error");
        void vscode.window.showErrorMessage(
          "Den: could not prepare the studio backend automatically. " +
            "See the Den output channel, or set den.pythonPath to a Python with " +
            '"lionagi[studio]" installed.'
        );
        return;
      }
    }

    // Pre-flight: for an interpreter whose provisioning Den does not control,
    // verify the studio server is importable before spawning. A doomed spawn
    // otherwise costs a process exit plus a multi-second orphan probe before it
    // can error; this fails fast with an actionable message — and self-heals a
    // studio-less workspace .venv in place when uv can repair it.
    if (spec.preflight) {
      let importable = await this._canImportStudio(spec.command);
      if (this._superseded(epoch)) {
        return;
      }
      if (!importable && spec.repair) {
        this._output.appendLine(
          `[lifecycle] studio server not importable — repairing via ` +
            `${spec.repair.command} ${spec.repair.args.join(" ")}`
        );
        const repaired = await this._provision(spec.repair);
        if (this._superseded(epoch)) {
          return;
        }
        if (repaired) {
          importable = await this._canImportStudio(spec.command);
          if (this._superseded(epoch)) {
            return;
          }
        }
      }
      if (!importable) {
        this.setState("error");
        void vscode.window.showErrorMessage(
          `Den: the studio server is not installed for ${spec.command}. ` +
            `Install it with 'pip install "lionagi[studio]"', set den.pythonPath ` +
            `to a Python that has it, or set den.url to attach to a running backend.`
        );
        return;
      }
    }

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
    let spawnErr: string | undefined;
    let exitCode: number | null = null;

    const child = child_process.spawn(spec.command, spec.args, {
      env,
      cwd: spec.cwd,
      stdio: ["ignore", "pipe", "pipe"],
    });

    this._child = child;
    this._attachedUnmanaged = false;

    child.stdout?.on("data", (data: Buffer) => {
      this._output.append(data.toString());
    });
    child.stderr?.on("data", (data: Buffer) => {
      this._output.append(data.toString());
    });

    child.on("error", (err) => {
      if (child !== this._child) {
        return; // stale child from a superseded start()
      }
      this._output.appendLine(`[lifecycle] spawn error: ${err.message}`);
      spawnFailed = true;
      spawnErr = err.message;
    });

    child.on("exit", (code) => {
      if (child !== this._child) {
        return; // stale child from a superseded start()
      }
      this._output.appendLine(`[lifecycle] exited with code ${code}`);
      if (code !== 0) {
        // Non-zero exit during start — commonly EADDRINUSE when another backend
        // already holds the port. Record it; the tail decides whether to attach
        // to that existing backend or surface a real error.
        spawnFailed = true;
        exitCode = code;
      }
      // The child died after we were already running. The process we own is
      // gone, so count it as one confirmed miss: a dead port then condemns on
      // the very next probe (fast), while an orphan still serving the port
      // resets the counter and keeps us running rather than flapping to "error".
      if (this._state === "running") {
        this._child = undefined;
        this._missedHealthChecks = 1;
        void this.reconcile();
      }
    });

    const ok = await this._pollHealth(
      30_000,
      () => spawnFailed || this._superseded(epoch)
    );
    if (this._superseded(epoch)) {
      // Superseded mid-spawn (a Stop or a newer start ran). Reap only the child
      // WE spawned — never whatever the current generation now owns.
      if (this._child === child) {
        this._child = undefined;
      }
      this._killChild(child);
      return;
    }
    if (ok) {
      this.setState("running");
      return;
    }

    // The spawn did not become healthy. Before erroring, check whether a
    // backend is already serving the port: the spawn may have lost an
    // EADDRINUSE race against an orphan from a previous session (or a
    // manually-started backend). If one is healthy, attach instead of flapping
    // to "error". Reap our own losing/unhealthy child first so it cannot linger.
    this._reapChild();
    const orphanHealthy = await probeHealth(
      this._effectiveBaseUrl ?? defaultUrl,
      { timeoutMs: 2500, retries: 2 }
    );
    if (this._superseded(epoch)) {
      return;
    }
    if (orphanHealthy) {
      this._attachedUnmanaged = true;
      this._output.appendLine(
        "[lifecycle] spawn did not win the port, but a backend is healthy — attached"
      );
      this.setState("running");
      return;
    }

    this.setState("error");
    if (spawnErr) {
      void vscode.window.showErrorMessage(
        `Den: failed to start backend — ${spawnErr}. ` +
          `Check the den.pythonPath setting.`
      );
    } else if (exitCode !== null) {
      void vscode.window.showErrorMessage(
        `Den: backend exited (code ${exitCode}) before becoming healthy. ` +
          `The Python at ${spec.command} may be missing the studio server — ` +
          `set den.pythonPath, or install it with 'pip install "lionagi[studio]"'. ` +
          `See the Den output channel.`
      );
    } else {
      void vscode.window.showErrorMessage(
        "Den: backend did not become healthy in time. See the Den output channel."
      );
    }
  }

  /** True if a newer start()/stop() has superseded this start() generation. */
  private _superseded(epoch: number): boolean {
    return epoch !== this._epoch;
  }

  /** Install backend deps (uv sync) with a progress notification before spawning. */
  private async _provision(p: {
    command: string;
    args: string[];
    cwd: string;
  }): Promise<boolean> {
    this._output.appendLine(
      `[lifecycle] provisioning backend: ${p.command} ${p.args.join(" ")}`
    );
    const ok = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title:
          "Den: preparing the studio backend (first run installs dependencies)…",
      },
      () =>
        new Promise<boolean>((resolve) => {
          const proc = child_process.spawn(p.command, p.args, {
            cwd: p.cwd,
            stdio: ["ignore", "pipe", "pipe"],
          });
          proc.stdout?.on("data", (d: Buffer) =>
            this._output.append(d.toString())
          );
          proc.stderr?.on("data", (d: Buffer) =>
            this._output.append(d.toString())
          );
          const timer = setTimeout(() => {
            proc.kill();
            resolve(false);
          }, 180_000);
          proc.on("error", (err) => {
            clearTimeout(timer);
            this._output.appendLine(
              `[lifecycle] provision error: ${err.message}`
            );
            resolve(false);
          });
          proc.on("exit", (code) => {
            clearTimeout(timer);
            resolve(code === 0);
          });
        })
    );
    return ok;
  }

  /**
   * Whether `python` can import the studio server. Uses importlib.util.find_spec
   * (resolves the module spec without executing it) so the happy path does not
   * pay the full studio import cost twice — once here and again in the spawned
   * `-m lionagi.studio`. Resolves false on a missing dep, a non-runnable
   * interpreter (spawn error), or a probe that overruns 5s.
   */
  private _canImportStudio(python: string): Promise<boolean> {
    const code =
      "import importlib.util as u, sys; " +
      "sys.exit(0 if u.find_spec('lionagi') and u.find_spec('fastapi') " +
      "and u.find_spec('uvicorn') else 1)";
    return new Promise<boolean>((resolve) => {
      let done = false;
      const finish = (ok: boolean) => {
        if (done) {
          return;
        }
        done = true;
        clearTimeout(timer);
        resolve(ok);
      };
      const probe = child_process.spawn(python, ["-c", code], {
        stdio: "ignore",
      });
      const timer = setTimeout(() => {
        probe.kill();
        finish(false);
      }, 5_000);
      probe.on("error", () => finish(false));
      probe.on("exit", (exitCode) => finish(exitCode === 0));
    });
  }

  stop(): void {
    // Invalidate any in-flight start() so its awaits cannot resurrect state.
    this._epoch++;
    if (this._attachedUnmanaged) {
      // We attached to a backend we did not spawn — there is no child to kill,
      // so it keeps serving the port. Say so rather than implying it stopped.
      this._output.appendLine(
        `[lifecycle] detached from ${this.baseUrl} (not started by Den; it is still running)`
      );
      this._attachedUnmanaged = false;
    }
    this._reapChild();
    this._effectiveBaseUrl = undefined;
    this.setState("stopped");
  }

  /** Detach and terminate the child we currently own, if any. */
  private _reapChild(): void {
    const child = this._child;
    if (!child) {
      return;
    }
    this._child = undefined;
    this._killChild(child);
  }

  /** Terminate a specific child process, escalating to SIGKILL if it lingers. */
  private _killChild(child: child_process.ChildProcess): void {
    child.kill();
    const killTimer = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill("SIGKILL");
      }
    }, 3_000);
    child.once("exit", () => clearTimeout(killTimer));
  }

  private _startSupervisor(): void {
    if (this._supervisorTimer !== undefined) {
      return;
    }
    this._supervisorTimer = setInterval(() => {
      void this.reconcile();
    }, SUPERVISE_INTERVAL_MS);
  }

  /**
   * Reconcile Den's tracked state against a live health probe. Recovers from a
   * spurious "error" — the backend is actually reachable, e.g. a spawned helper
   * died while an orphan kept serving the port — without the manual status-bar
   * click, and conversely marks "error" when a backend that was running stops
   * responding. Hysteresis (two consecutive misses) avoids flapping on a single
   * slow probe. Intentional stops and in-flight starts are left alone.
   */
  async reconcile(): Promise<void> {
    if (this._reconciling) {
      return;
    }
    const s = this._state;
    if (s === "stopped" || s === "starting") {
      return;
    }
    this._reconciling = true;
    try {
      const healthy = await probeHealth(this.baseUrl, {
        timeoutMs: 2500,
        retries: 1,
      });
      if (healthy) {
        this._missedHealthChecks = 0;
        if (this._state === "error") {
          this._output.appendLine(
            "[lifecycle] backend reachable again — recovered to running"
          );
          this.setState("running");
        }
        return;
      }
      if (this._state === "running") {
        this._missedHealthChecks += 1;
        if (this._missedHealthChecks >= 2) {
          this._output.appendLine(
            "[lifecycle] backend stopped responding — marking error"
          );
          this.setState("error");
        }
      }
    } finally {
      this._reconciling = false;
    }
  }

  /** Poll GET /health until it returns true, timeout elapses, or shouldAbort() is true. */
  private async _pollHealth(
    timeoutMs: number,
    shouldAbort?: () => boolean
  ): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    // Tight cadence so Den attaches within a few hundred ms of the spawned
    // backend going live, instead of waiting up to a full second on the gap.
    const interval = 400;

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
    if (this._supervisorTimer !== undefined) {
      clearInterval(this._supervisorTimer);
      this._supervisorTimer = undefined;
    }
    this.stop();
    this._onDidChangeState.dispose();
    this._output.dispose();
  }
}
