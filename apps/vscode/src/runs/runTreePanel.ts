import * as vscode from "vscode";
import * as path from "path";
import * as crypto from "crypto";
import type { StudioDeps } from "../extension.js";
import type { SignalRow, SignalStreamEvent } from "../api/types.js";
import { streamSignals } from "../api/signals.js";
import { getAuthToken } from "../config.js";
import {
  createRunTreeState,
  applySignalRow,
  toForest,
} from "./runTreeModel.js";

// A single reusable run-tree panel, re-targeted as you click different runs —
// clicking never spawns a second panel.
let currentTreePanel: RunTreePanel | undefined;

export class RunTreePanel {
  private readonly panel: vscode.WebviewPanel;
  private ac: AbortController;
  private sessionId: string;
  private title: string;

  private constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly deps: StudioDeps,
    sessionId: string,
    title: string,
    column: vscode.ViewColumn
  ) {
    this.sessionId = sessionId;
    this.title = title;

    this.panel = vscode.window.createWebviewPanel(
      "den.runTree",
      `Tree: ${title}`,
      { viewColumn: column, preserveFocus: true },
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.file(path.join(context.extensionPath, "media")),
        ],
      }
    );

    this.ac = new AbortController();

    this.panel.onDidDispose(() => {
      this.ac.abort();
      if (currentTreePanel === this) {
        currentTreePanel = undefined;
      }
    });

    void this.initialize();
  }

  static open(
    context: vscode.ExtensionContext,
    deps: StudioDeps,
    sessionId: string,
    title: string
  ): RunTreePanel {
    if (currentTreePanel) {
      // Reuse the one panel — clicking a different run re-targets it in place.
      currentTreePanel.retarget(sessionId, title);
      return currentTreePanel;
    }
    const inst = new RunTreePanel(context, deps, sessionId, title, pickTreeColumn());
    currentTreePanel = inst;
    return inst;
  }

  /** Re-point the existing panel at another session without opening a new one. */
  private retarget(sessionId: string, title: string): void {
    if (sessionId === this.sessionId) {
      this.panel.reveal(undefined, true);
      return;
    }
    this.ac.abort();
    this.ac = new AbortController();
    this.sessionId = sessionId;
    this.title = title;
    this.panel.title = `Tree: ${title}`;
    this.panel.reveal(undefined, true);
    void this.initialize();
  }

  private async initialize(): Promise<void> {
    this.panel.webview.html = this.buildHtml();
    await this.stream();
  }

  private async stream(): Promise<void> {
    // Capture this run's controller. A mid-stream retarget() swaps this.ac, so
    // the loop must check the controller it started with — otherwise a dropped
    // connection would reconnect the old session against the new controller.
    const ac = this.ac;
    const MAX_RETRIES = 3;

    for (let attempt = 0; ; attempt++) {
      // Fresh state each attempt: the signals endpoint replays from seq 0, so a
      // reconnect rebuilds the snapshot rather than appending onto a stale one.
      const state = createRunTreeState();
      // Coalesce rapid-fire initial replay rows: schedule a single flush rather
      // than posting on every row during the replay phase.
      let flushScheduled = false;
      let flushTimer: ReturnType<typeof setTimeout> | undefined;

      const flush = (): void => {
        flushScheduled = false;
        flushTimer = undefined;
        this.postMessage({
          type: "snapshot",
          forest: toForest(state),
          runState: state.runState,
          usage: state.usage,
        });
      };

      const scheduleFlush = (): void => {
        if (!flushScheduled) {
          flushScheduled = true;
          flushTimer = setTimeout(flush, 50);
        }
      };

      try {
        await streamSignals(
          this.deps.backend.baseUrl,
          this.sessionId,
          getAuthToken() || undefined,
          (e: SignalStreamEvent) => {
            if ("type" in e) {
              if (e.type === "heartbeat") {
                return;
              }
              if (e.type === "done") {
                // Flush any pending snapshot before signalling done.
                if (flushScheduled) {
                  if (flushTimer) {
                    clearTimeout(flushTimer);
                  }
                  flush();
                }
                this.postMessage({ type: "done" });
                return;
              }
            }
            // Data row: has seq and kind fields.
            applySignalRow(state, e as SignalRow);
            scheduleFlush();
          },
          ac.signal
        );
        // streamSignals only resolves after a `done` event — a clean finish.
        return;
      } catch (err) {
        if (flushTimer) {
          clearTimeout(flushTimer);
        }
        if (ac.signal.aborted) {
          return;
        }
        if (attempt >= MAX_RETRIES) {
          this.postMessage({
            type: "error",
            message: err instanceof Error ? err.message : String(err),
          });
          return;
        }
        // Transient drop (EOF before done): back off (1s, 2s, 4s) and reconnect.
        const delayMs = Math.min(4000, 1000 * 2 ** attempt);
        if (!(await this.abortableDelay(delayMs, ac.signal))) {
          return;
        }
      }
    }
  }

  /** Resolve true after ms, or false immediately if the signal aborts first. */
  private abortableDelay(ms: number, signal: AbortSignal): Promise<boolean> {
    return new Promise((resolve) => {
      if (signal.aborted) {
        resolve(false);
        return;
      }
      let timer: ReturnType<typeof setTimeout>;
      const onAbort = (): void => {
        clearTimeout(timer);
        resolve(false);
      };
      timer = setTimeout(() => {
        signal.removeEventListener("abort", onAbort);
        resolve(true);
      }, ms);
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  private postMessage(msg: unknown): void {
    void this.panel.webview.postMessage(msg);
  }

  private buildHtml(): string {
    const nonce = crypto.randomBytes(16).toString("hex");
    const webview = this.panel.webview;

    const cssUri = webview.asWebviewUri(
      vscode.Uri.file(path.join(this.context.extensionPath, "media", "runTree.css"))
    );
    const jsUri = webview.asWebviewUri(
      vscode.Uri.file(path.join(this.context.extensionPath, "media", "runTree.js"))
    );

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src ${webview.cspSource} 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${cssUri}">
  <title>${esc(this.title)}</title>
</head>
<body>
  <div class="header" id="header">
    <div class="header__title">
      <span class="status-dot status-dot--pending" id="statusDot"></span>
      <h1 id="runTitle">${esc(this.title)}</h1>
      <span class="badge badge--status badge--pending" id="statusBadge">pending</span>
    </div>
    <div class="header__meta" id="usageLine" style="display:none"></div>
  </div>

  <div class="tree-container" id="tree">
    <div class="tree__empty" id="emptyState" style="display:none">
      single run — no sub-nodes
    </div>
  </div>

  <div class="footer" id="footer">
    <span id="footerStatus">Connecting…</span>
  </div>

  <script nonce="${nonce}" src="${jsUri}"></script>
</body>
</html>`;
  }
}

function pickTreeColumn(): vscode.ViewColumn {
  const groups = vscode.window.tabGroups?.all.length ?? 1;
  return groups <= 1 ? vscode.ViewColumn.Beside : vscode.ViewColumn.Active;
}

function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
