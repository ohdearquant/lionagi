/**
 * Shared per-test-configurable mock of the `vscode` module.
 * Named exports map to vscode.window, vscode.commands, etc. under `import * as vscode`.
 * Call __resetVscodeMock() in beforeEach to clear call history and re-apply defaults.
 */
import { vi } from "vitest";

// ---------------------------------------------------------------------------
// VS Code tree-view primitives used by RunItem / ProjectGroupItem / ActiveGroupItem
// ---------------------------------------------------------------------------

export const TreeItemCollapsibleState = {
  None: 0,
  Collapsed: 1,
  Expanded: 2,
} as const;

export class TreeItem {
  label?: string;
  description?: string;
  iconPath?: unknown;
  contextValue?: string;
  tooltip?: unknown;
  command?: unknown;
  id?: string;
  collapsibleState?: number;
  constructor(label: string, collapsibleState?: number) {
    this.label = label;
    this.collapsibleState = collapsibleState;
  }
}

export class ThemeIcon {
  constructor(public id: string, public color?: unknown) {}
}

export class ThemeColor {
  constructor(public id: string) {}
}

export class MarkdownString {
  private _value = "";
  public supportHtml = false;
  public isTrusted = false;
  constructor(value = "", _supportThemeIcons = false) {
    this._value = value;
  }
  appendMarkdown(text: string): this {
    this._value += text;
    return this;
  }
  toString(): string {
    return this._value;
  }
}

// Command registry: maps command id → callback registered via registerCommand.
export const _commandRegistry = new Map<string, (...args: unknown[]) => unknown>();

export function __getCommand(id: string): ((...args: unknown[]) => unknown) | undefined {
  return _commandRegistry.get(id);
}

export function __resetVscodeMock(): void {
  _commandRegistry.clear();
  // Reset all vi.fn() mocks to their default implementations.
  window.showInputBox.mockReset();
  window.showQuickPick.mockReset();
  window.showInformationMessage.mockReset();
  window.showWarningMessage.mockReset();
  window.showErrorMessage.mockReset();
  window.showOpenDialog.mockReset();
  window.withProgress.mockReset();
  window.createTreeView.mockReset();
  commands.executeCommand.mockReset();
  workspace.fs.readFile.mockReset();

  // Sensible defaults
  window.showInformationMessage.mockResolvedValue(undefined);
  window.showWarningMessage.mockResolvedValue(undefined);
  window.showErrorMessage.mockResolvedValue(undefined);
  commands.executeCommand.mockResolvedValue(undefined);

  // Default: createTreeView returns a stub that doesn't start polling (visible: false).
  window.createTreeView.mockReturnValue({
    visible: false,
    onDidChangeVisibility: () => ({ dispose() {} }),
    dispose() {},
  });
}

// Minimal EventEmitter that matches vscode.EventEmitter<T> shape.
export class EventEmitter<T> {
  private _listeners: Array<(e: T) => void> = [];
  readonly event = (listener: (e: T) => void) => {
    this._listeners.push(listener);
    return { dispose: () => { this._listeners = this._listeners.filter((l) => l !== listener); } };
  };
  fire(data: T): void {
    for (const l of this._listeners) l(data);
  }
  dispose(): void {
    this._listeners = [];
  }
}

// Minimal Uri stub.
export const Uri = {
  parse: (s: string) => ({ toString: () => s }),
  file: (s: string) => ({ fsPath: s, toString: () => s }),
};

// ProgressLocation enum (only Notification used in tested paths).
export const ProgressLocation = {
  Notification: 15,
  SourceControl: 1,
  Window: 10,
} as const;

export const window = {
  showInputBox: vi.fn<[options?: unknown], Promise<string | undefined>>(),
  showQuickPick: vi.fn<[items: unknown, options?: unknown], Promise<string | undefined>>(),
  showInformationMessage: vi.fn<[message: string, ...rest: unknown[]], Promise<string | undefined>>(),
  showWarningMessage: vi.fn<[message: string, ...rest: unknown[]], Promise<string | undefined>>(),
  showErrorMessage: vi.fn<[message: string, ...rest: unknown[]], Promise<string | undefined>>(),
  showOpenDialog: vi.fn<[options?: unknown], Promise<unknown[] | undefined>>(),
  withProgress: vi.fn<[opts: unknown, task: (progress: unknown, token: unknown) => Promise<unknown>], Promise<unknown>>(),
  createTreeView: vi.fn<[viewId: string, opts: unknown], unknown>(),
  // Plain stub (not vi.fn) so __resetVscodeMock never wipes its implementation —
  // BackendManager grabs the output channel once in its constructor.
  createOutputChannel: (name: string) => ({
    name,
    append() {},
    appendLine() {},
    clear() {},
    show() {},
    hide() {},
    replace() {},
    dispose() {},
  }),
  activeTextEditor: undefined as
    | { document: { getText(): string; uri: { fsPath: string } } }
    | undefined,
};

export const commands = {
  registerCommand: vi.fn(
    (id: string, cb: (...args: unknown[]) => unknown) => {
      _commandRegistry.set(id, cb);
      return { dispose() { _commandRegistry.delete(id); } };
    }
  ),
  executeCommand: vi.fn<[command: string, ...rest: unknown[]], Promise<unknown>>(),
};

export const workspace = {
  fs: {
    readFile: vi.fn<[uri: unknown], Promise<Uint8Array>>(),
  },
};
