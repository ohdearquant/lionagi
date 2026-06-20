import * as child_process from "child_process";
import * as vscode from "vscode";

export type BackendState = "stopped" | "starting" | "running" | "error";

export class BackendManager implements vscode.Disposable {
  private _state: BackendState = "stopped";
  private _child: child_process.ChildProcess | undefined;
  private _output: vscode.OutputChannel;
  private _pollTimer: ReturnType<typeof setTimeout> | undefined;

  private readonly _onDidChangeState =
    new vscode.EventEmitter<BackendState>();
  readonly onDidChangeState = this._onDidChangeState.event;

  constructor(
    private readonly getBaseUrl: () => string,
    private readonly getPythonPath: () => string,
    private readonly getConfiguredUrl: () => string,
    private readonly getPort: () => number,
    private readonly getHost: () => string,
    private readonly getToken: () => string
  ) {
    this._output = vscode.window.createOutputChannel("Lion Studio");
  }

  get state(): BackendState {
    return this._state;
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

    // ATTACH mode: configured URL set — do not spawn, just health-check.
    if (this.getConfiguredUrl().trim()) {
      this.setState("starting");
      const ok = await this._pollHealth(30_000);
      this.setState(ok ? "running" : "error");
      return;
    }

    // SPAWN mode.
    this.setState("starting");

    const pythonPath = this.getPythonPath();
    const port = this.getPort();
    const host = this.getHost();
    const token = this.getToken();

    const env: NodeJS.ProcessEnv = {
      ...process.env,
      LIONAGI_STUDIO_PORT: String(port),
      LIONAGI_STUDIO_HOST: host,
    };
    if (token) {
      env["LIONAGI_STUDIO_AUTH_TOKEN"] = token;
    }

    this._output.appendLine(
      `[lifecycle] spawning: ${pythonPath} -m lionagi.studio (port=${port}, host=${host})`
    );

    const child = child_process.spawn(
      pythonPath,
      ["-m", "lionagi.studio"],
      { env, stdio: ["ignore", "pipe", "pipe"] }
    );

    this._child = child;

    child.stdout?.on("data", (data: Buffer) => {
      this._output.append(data.toString());
    });
    child.stderr?.on("data", (data: Buffer) => {
      this._output.append(data.toString());
    });
    child.on("error", (err) => {
      this._output.appendLine(`[lifecycle] spawn error: ${err.message}`);
      this.setState("error");
    });
    child.on("exit", (code) => {
      this._output.appendLine(`[lifecycle] exited with code ${code}`);
      if (this._state !== "stopped") {
        this.setState("error");
      }
    });

    const ok = await this._pollHealth(30_000);
    if (!ok && this._state !== "error") {
      this.setState("error");
    } else if (ok) {
      this.setState("running");
    }
  }

  stop(): void {
    if (this._pollTimer !== undefined) {
      clearTimeout(this._pollTimer);
      this._pollTimer = undefined;
    }
    if (this._child) {
      this._child.kill();
      this._child = undefined;
    }
    this.setState("stopped");
  }

  /** Poll GET /health until it returns true or the timeout elapses. */
  private async _pollHealth(timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    const interval = 1_000;

    while (Date.now() < deadline) {
      try {
        const res = await fetch(`${this.getBaseUrl()}/health`);
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
