import * as vscode from "vscode";

function cfg(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration("lionStudio");
}

export function getUrl(): string {
  return cfg().get<string>("url", "");
}

export function getPythonPath(): string {
  return cfg().get<string>("pythonPath", "python3");
}

export function getPort(): number {
  return cfg().get<number>("port", 8765);
}

export function getHost(): string {
  return cfg().get<string>("host", "127.0.0.1");
}

export function getAutoStart(): boolean {
  return cfg().get<boolean>("autoStart", true);
}

export function getAuthToken(): string {
  return cfg().get<string>("authToken", "");
}

/** Returns the base URL: configured attach URL or http://{host}:{port}. */
export function studioBaseUrl(): string {
  const configured = getUrl().trim();
  if (configured) {
    return configured.replace(/\/$/, "");
  }
  return `http://${getHost()}:${getPort()}`;
}
