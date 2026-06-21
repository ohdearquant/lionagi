import * as vscode from "vscode";
import * as path from "path";
import * as crypto from "crypto";
import type { StudioDeps } from "../extension.js";
import type { Run, StudioEvent, InvocationDetail } from "../api/types.js";
import { streamSession } from "../api/sse.js";
import { getAuthToken } from "../config.js";
import { isTerminal, mergeRunDetail, runId } from "./runItem.js";

// A single reusable run-detail panel, re-targeted as you click different runs —
// clicking never spawns a second panel.
let currentPanel: RunDetailPanel | undefined;

export class RunDetailPanel {
  private readonly panel: vscode.WebviewPanel;
  private ac: AbortController;

  private constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly deps: StudioDeps,
    private run: Run,
    column: vscode.ViewColumn
  ) {
    const title = run.name ?? run.playbook_name ?? run.agent_name ?? runId(run).slice(0, 8);

    this.panel = vscode.window.createWebviewPanel(
      "den.runDetail",
      `Run: ${title}`,
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
      if (currentPanel === this) {
        currentPanel = undefined;
      }
    });

    void this.initialize();
  }

  static open(
    context: vscode.ExtensionContext,
    deps: StudioDeps,
    run: Run
  ): RunDetailPanel {
    if (currentPanel) {
      // Reuse the one panel — clicking a different run re-targets it in place.
      currentPanel.retarget(run);
      return currentPanel;
    }
    const inst = new RunDetailPanel(context, deps, run, pickDetailColumn());
    currentPanel = inst;
    return inst;
  }

  /** Re-point the existing panel at another run without opening a new one. */
  private retarget(run: Run): void {
    if (runId(run) === runId(this.run)) {
      this.panel.reveal(undefined, true); // same run — just bring it forward
      return;
    }
    this.ac.abort();
    this.ac = new AbortController();
    this.run = run;
    this.panel.title = `Run: ${
      run.name ?? run.playbook_name ?? run.agent_name ?? runId(run).slice(0, 8)
    }`;
    this.panel.reveal(undefined, true);
    void this.initialize();
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
    const id = runId(this.run);
    if (!id) {
      this.postMessage({ type: "error", message: "Run has no stable identifier." });
      return;
    }
    try {
      const run = await this.deps.client.getRun(id);
      // Merge, don't replace: a partial detail response must not erase list-row
      // fields the banner logic below depends on (invocation_id especially).
      this.run = mergeRunDetail(this.run, run);

      // Refresh header with final metadata.
      this.postMessage({ type: "meta", run: this.run });

      // Fetch the reason only for non-success terminal runs — skip the call for
      // green runs so a succeeded run never even loads a (red-toned) banner.
      const terminalStatus = (this.run.status ?? "").toLowerCase();
      const isNonSuccess =
        terminalStatus !== "succeeded" && terminalStatus !== "completed";
      if (isNonSuccess && this.run.invocation_id) {
        try {
          const inv = await this.deps.client.getInvocation(this.run.invocation_id);
          const reasonMsg = reasonBannerMessage(
            this.run.status,
            this.run.invocation_id,
            inv
          );
          if (reasonMsg) {
            this.postMessage(reasonMsg);
          }
        } catch {
          // reason is best-effort; never block the log on it
        }
      }

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
    const id = runId(this.run);
    if (!id) {
      this.postMessage({ type: "error", message: "Run has no stable identifier." });
      return;
    }
    try {
      await streamSession(
        this.deps.backend.baseUrl,
        id,
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
      run.name ?? run.playbook_name ?? run.agent_name ?? runId(run).slice(0, 8);

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

/** Payload posted to the webview to render the non-success reason banner. */
export interface ReasonBannerMessage {
  type: "reason";
  code: string | null;
  summary: string | null;
  evidenceRefs: Array<Record<string, unknown>> | null;
}

// A non-success terminal run whose invocation carries a reason → banner payload;
// succeeded/completed (or no reason at all) → null, so a green run never shows a red banner.
export function reasonBannerMessage(
  status: string | null | undefined,
  invocationId: string | null | undefined,
  inv:
    | Pick<
        InvocationDetail,
        "status_reason_code" | "status_reason_summary" | "status_evidence_refs"
      >
    | null
    | undefined
): ReasonBannerMessage | null {
  const terminalStatus = (status ?? "").toLowerCase();
  const isNonSuccess =
    terminalStatus !== "succeeded" && terminalStatus !== "completed";
  if (!isNonSuccess || !invocationId || !inv) {
    return null;
  }
  if (!inv.status_reason_summary && !inv.status_reason_code) {
    return null;
  }
  return {
    type: "reason",
    code: inv.status_reason_code,
    summary: inv.status_reason_summary,
    evidenceRefs: inv.status_evidence_refs,
  };
}

/**
 * Where the first detail panel opens: split Beside when the editor shows a single
 * group, otherwise reuse the active group so we never keep adding splits.
 */
function pickDetailColumn(): vscode.ViewColumn {
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
