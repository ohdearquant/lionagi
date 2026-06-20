import * as vscode from "vscode";

function cfg(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration("den");
}

export function getUrl(): string {
  return cfg().get<string>("url", "");
}

export function getPythonPath(): string {
  return cfg().get<string>("pythonPath", "");
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
