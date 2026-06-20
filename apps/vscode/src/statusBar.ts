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
        item.text = "$(circle-slash) Den";
        item.tooltip = "Den: not running. Click to start.";
        item.command = "den.startBackend";
        break;
      case "starting":
        item.text = "$(loading~spin) Den…";
        item.tooltip = "Den: starting…";
        item.command = undefined;
        break;
      case "running":
        item.text = "$(check) Den";
        item.tooltip = "Den: running. Click to open panel.";
        item.command = "den.refreshRuns";
        break;
      case "error":
        item.text = "$(error) Den";
        item.tooltip = "Den: error. Click to retry.";
        item.command = "den.startBackend";
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
