import * as vscode from "vscode";
import type { BackendManager, BackendState } from "./backend/lifecycle.js";

export function registerStatusBar(
  context: vscode.ExtensionContext,
  backend: BackendManager
): void {
  const item = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100
  );

  function update(state: BackendState): void {
    switch (state) {
      case "stopped":
        item.text = "$(circle-slash) Lion Studio";
        item.tooltip = "Lion Studio: not running. Click to start.";
        item.command = "lionStudio.startBackend";
        break;
      case "starting":
        item.text = "$(loading~spin) Lion Studio…";
        item.tooltip = "Lion Studio: starting…";
        item.command = undefined;
        break;
      case "running":
        item.text = "$(check) Lion Studio";
        item.tooltip = "Lion Studio: running. Click to open panel.";
        item.command = "lionStudio.refreshRuns";
        break;
      case "error":
        item.text = "$(error) Lion Studio";
        item.tooltip = "Lion Studio: error. Click to retry.";
        item.command = "lionStudio.startBackend";
        break;
    }
  }

  update(backend.state);
  item.show();

  context.subscriptions.push(
    backend.onDidChangeState((state) => update(state)),
    item
  );
}
