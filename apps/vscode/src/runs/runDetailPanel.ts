import * as vscode from "vscode";
import * as path from "path";
import * as crypto from "crypto";
import type { StudioDeps } from "../extension.js";
import type { Run, StudioEvent } from "../api/types.js";
import { streamSession } from "../api/sse.js";
import { studioBaseUrl, getAuthToken } from "../config.js";
import { isTerminal } from "./runItem.js";

// One panel per run_id.
const openPanels = new Map<string, RunDetailPanel>();

export class RunDetailPanel {
  private readonly panel: vscode.WebviewPanel;
  private readonly ac: AbortController;

  private constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly deps: StudioDeps,
    private run: Run
  ) {
    const title = run.name ?? run.playbook_name ?? run.agent_name ?? run.run_id.slice(0, 8);

    this.panel = vscode.window.createWebviewPanel(
      "lionStudio.runDetail",
      `Run: ${title}`,
      vscode.ViewColumn.Beside,
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
      openPanels.delete(run.run_id);
    });

    void this.initialize();
  }

  static open(
    context: vscode.ExtensionContext,
    deps: StudioDeps,
    run: Run
  ): RunDetailPanel {
    const existing = openPanels.get(run.run_id);
    if (existing) {
      existing.panel.reveal(vscode.ViewColumn.Beside);
      return existing;
    }
    const inst = new RunDetailPanel(context, deps, run);
    openPanels.set(run.run_id, inst);
    return inst;
  }

  private async initialize(): Promise<void> {
    this.panel.webview.html = this.buildHtml();

    if (isTerminal(this.run)) {
      await this.loadTerminal();
    } else {
      await this.streamLive();
    }
  }

  private async loadTerminal(): Promise<void> {
    try {
      const run = await this.deps.client.getRun(this.run.run_id);
      this.run = run;

      // Refresh header with final metadata.
      this.postMessage({ type: "meta", run });

      // Backend returns steps[].messages[] — flatten into individual message events.
      const extended = run as Run & {
        steps?: Array<{
          step?: string;
          status?: string;
          messages?: Array<Record<string, unknown>>;
          timestamp?: string;
        }>;
        branches?: unknown[];
      };

      const messages = (extended.steps ?? []).flatMap(
        (s) => s.messages ?? []
      );

      if (messages.length === 0) {
        this.postMessage({ type: "empty" });
      } else {
        for (const msg of messages) {
          this.postMessage({ type: "event", event: msg });
        }
        this.postMessage({ type: "done" });
      }
    } catch (err) {
      this.postMessage({
        type: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private async streamLive(): Promise<void> {
    try {
      await streamSession(
        studioBaseUrl(),
        this.run.run_id,
        getAuthToken() || undefined,
        (e: StudioEvent) => {
          if (e.type === "heartbeat") {
            return;
          }
          if (e.type === "done") {
            this.postMessage({ type: "done" });
            return;
          }
          this.postMessage({ type: "event", event: e });
        },
        this.ac.signal
      );
    } catch (err) {
      if (this.ac.signal.aborted) {
        return;
      }
      this.postMessage({
        type: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private postMessage(msg: unknown): void {
    void this.panel.webview.postMessage(msg);
  }

  private buildHtml(): string {
    const nonce = crypto.randomBytes(16).toString("hex");
    const webview = this.panel.webview;

    const cssUri = webview.asWebviewUri(
      vscode.Uri.file(path.join(this.context.extensionPath, "media", "runDetail.css"))
    );
    const jsUri = webview.asWebviewUri(
      vscode.Uri.file(path.join(this.context.extensionPath, "media", "runDetail.js"))
    );

    const run = this.run;
    const title =
      run.name ?? run.playbook_name ?? run.agent_name ?? run.run_id.slice(0, 8);

    const statusClass = statusCssClass(run.status);
    const modelBadge = run.model
      ? `<span class="badge">${esc(run.model)}</span>`
      : "";
    const providerBadge = run.provider
      ? `<span class="badge">${esc(run.provider)}</span>`
      : "";
    const kindBadge = run.invocation_kind
      ? `<span class="badge badge--kind">${esc(run.invocation_kind)}</span>`
      : "";
    const projectBadge = run.project
      ? `<span class="badge badge--project">${esc(run.project)}</span>`
      : "";

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src ${webview.cspSource} 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${cssUri}">
  <title>${esc(title)}</title>
</head>
<body>
  <div class="header" id="header">
    <div class="header__title">
      <span class="status-dot status-dot--${statusClass}" id="statusDot"></span>
      <h1 id="runTitle">${esc(title)}</h1>
      <span class="badge badge--status badge--${statusClass}" id="statusBadge">${esc(run.status ?? "unknown")}</span>
    </div>
    <div class="header__meta" id="headerMeta">
      ${kindBadge}${modelBadge}${providerBadge}${projectBadge}
      <span class="meta-item" id="branchCount">branches: ${run.branch_count ?? 0}</span>
      <span class="meta-item" id="msgCount">messages: ${run.message_count ?? 0}</span>
    </div>
  </div>

  <div class="log-container" id="log">
    <div class="log__empty" id="emptyState" style="display:none">
      No messages recorded for this run.
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

function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function statusCssClass(status: string | null | undefined): string {
  const s = (status ?? "").toLowerCase();
  if (s === "running" || s === "active" || s === "starting") {
    return "running";
  }
  if (s === "succeeded" || s === "completed") {
    return "success";
  }
  if (s === "failed" || s === "error") {
    return "error";
  }
  if (s === "cancelled") {
    return "cancelled";
  }
  if (s === "queued" || s === "pending") {
    return "pending";
  }
  return "unknown";
}
