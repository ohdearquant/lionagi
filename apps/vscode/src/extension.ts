import * as vscode from "vscode";
import { BackendManager } from "./backend/lifecycle.js";
import {
  getAuthToken,
  getAutoStart,
  getHost,
  getPythonPath,
  getPort,
  getUrl,
} from "./config.js";
import { registerStatusBar } from "./statusBar.js";

let backend: BackendManager | undefined;

export function activate(context: vscode.ExtensionContext): void {
  const bm = new BackendManager(
    () => getPythonPath(),
    () => getUrl(),
    () => getPort(),
    () => getHost(),
    () => getAuthToken()
  );
  backend = bm;
  context.subscriptions.push(bm);

  // Set initial context key and track all state changes.
  void vscode.commands.executeCommand(
    "setContext",
    "den.backendState",
    bm.state
  );
  context.subscriptions.push(
    bm.onDidChangeState((state) => {
      void vscode.commands.executeCommand(
        "setContext",
        "den.backendState",
        state
      );
    })
  );

  registerStatusBar(context, bm);

  context.subscriptions.push(
    vscode.commands.registerCommand("den.startBackend", () => {
      void bm.start();
    }),
    vscode.commands.registerCommand("den.stopBackend", () => {
      bm.stop();
    }),
    vscode.commands.registerCommand("den.starOnGitHub", () => {
      void vscode.env.openExternal(
        vscode.Uri.parse("https://github.com/ohdearquant/lionagi")
      );
    })
  );

  if (getAutoStart()) {
    void bm.start();
  }
}

export function deactivate(): void {
  backend?.dispose();
  backend = undefined;
}
