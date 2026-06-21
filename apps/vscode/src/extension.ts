import * as vscode from "vscode";
import { StudioClient } from "./api/client.js";
import { BackendManager } from "./backend/lifecycle.js";
import {
  getAuthToken,
  getAutoStart,
  getHost,
  getPythonPath,
  getPort,
  getUrl,
} from "./config.js";
import { registerAgentTrigger } from "./launch/agentTrigger.js";
import { registerRunCommand } from "./launch/runCommand.js";
import { registerRunsExplorer } from "./runs/runsExplorer.js";
import { registerStatusBar } from "./statusBar.js";

/** Shared dependencies injected into every feature module. */
export interface StudioDeps {
  client: StudioClient;
  backend: BackendManager;
}

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

  const client = new StudioClient(
    () => bm.baseUrl,
    () => getAuthToken() || undefined
  );

  const deps: StudioDeps = { client, backend: bm };

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
  registerRunsExplorer(context, deps);
  registerAgentTrigger(context, deps);
  registerRunCommand(context, deps);

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
