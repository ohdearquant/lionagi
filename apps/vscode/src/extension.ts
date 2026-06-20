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
  studioBaseUrl,
} from "./config.js";
import { registerAgentTrigger } from "./launch/agentTrigger.js";
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
    () => studioBaseUrl(),
    () => getPythonPath(),
    () => getUrl(),
    () => getPort(),
    () => getHost(),
    () => getAuthToken()
  );
  backend = bm;
  context.subscriptions.push(bm);

  const client = new StudioClient(
    () => studioBaseUrl(),
    () => getAuthToken() || undefined
  );

  const deps: StudioDeps = { client, backend: bm };

  registerStatusBar(context, bm);
  registerRunsExplorer(context, deps);
  registerAgentTrigger(context, deps);

  context.subscriptions.push(
    vscode.commands.registerCommand("lionStudio.startBackend", () => {
      void bm.start();
    }),
    vscode.commands.registerCommand("lionStudio.stopBackend", () => {
      bm.stop();
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
