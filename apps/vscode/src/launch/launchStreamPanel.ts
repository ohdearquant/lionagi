import * as crypto from "crypto";
import * as path from "path";
import * as vscode from "vscode";
import { streamSession } from "../api/sse.js";
import type { StudioEvent } from "../api/types.js";
import type { StudioDeps } from "../extension.js";
import { getAuthToken } from "../config.js";

function generateNonce(): string {
  return crypto.randomBytes(16).toString("base64");
}

function buildHtml(
  webview: vscode.Webview,
  extensionUri: vscode.Uri,
  nonce: string,
  prompt: string
): string {
  const cssUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, "media", "launchStream.css")
  );
  const jsUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, "media", "launchStream.js")
  );
  const csp = [
    `default-src 'none'`,
    `style-src ${webview.cspSource} 'unsafe-inline'`,
    `script-src 'nonce-${nonce}'`,
    `font-src ${webview.cspSource}`,
    `img-src ${webview.cspSource} data:`,
  ].join("; ");

  const escapedPrompt = prompt
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="${csp}" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="${cssUri}" />
  <title>Lion Agent</title>
</head>
<body>
  <div id="header">
    <div id="prompt-text">${escapedPrompt}</div>
    <div id="status-badge" class="launching">launching</div>
  </div>
  <div id="log"></div>
  <script nonce="${nonce}" src="${jsUri}"></script>
</body>
</html>`;
}

/** Open (or reveal) a streaming webview panel for an agent run. */
export function openLaunchStreamPanel(
  context: vscode.ExtensionContext,
  sessionId: string,
  prompt: string,
  deps: StudioDeps
): void {
  const panel = vscode.window.createWebviewPanel(
    "lionStudio.agentStream",
    truncateTitle(prompt),
    vscode.ViewColumn.Beside,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [
        vscode.Uri.joinPath(context.extensionUri, "media"),
      ],
    }
  );

  const nonce = generateNonce();
  panel.webview.html = buildHtml(
    panel.webview,
    context.extensionUri,
    nonce,
    prompt
  );

  const ac = new AbortController();
  panel.onDidDispose(() => {
    ac.abort();
  });

  const post = (msg: Record<string, unknown>) => {
    void panel.webview.postMessage(msg);
  };

  post({ kind: "status", label: "streaming", cls: "streaming" });
  post({ kind: "meta", text: `Session: ${sessionId}` });

  void streamSession(
    deps.backend.baseUrl,
    sessionId,
    getAuthToken() || undefined,
    (ev: StudioEvent) => {
      post({ kind: "event", event: ev });
    },
    ac.signal
  ).catch((err: unknown) => {
    if (ac.signal.aborted) {
      return;
    }
    const msg =
      err instanceof Error ? err.message : String(err);
    post({ kind: "error", text: msg });
  });
}

function truncateTitle(prompt: string, max = 40): string {
  const first = prompt.split("\n")[0].trim();
  if (first.length <= max) {
    return first;
  }
  return first.slice(0, max - 1) + "…";
}
