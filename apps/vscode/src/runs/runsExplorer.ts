import * as vscode from "vscode";
import type { StudioDeps } from "../extension.js";
import type { Run } from "../api/types.js";
import { StudioApiError } from "../api/client.js";
import { RunItem, isTerminal, toMillis } from "./runItem.js";
import { RunDetailPanel } from "./runDetailPanel.js";

const POLL_INTERVAL_MS = 4_000;

class RunsProvider implements vscode.TreeDataProvider<RunItem> {
  private _runs: Run[] = [];
  private _authErrorShown = false;
  private readonly _onDidChangeTreeData =
    new vscode.EventEmitter<RunItem | undefined | null | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  constructor(private readonly deps: StudioDeps) {}

  refresh(): void {
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: RunItem): RunItem {
    return element;
  }

  async getChildren(): Promise<RunItem[]> {
    if (!this.deps.backend.isRunning()) {
      this._runs = [];
      return [];
    }
    try {
      const page = await this.deps.client.listRuns({ per_page: 50 });
      const sorted = [...page.runs].sort((a, b) => {
        const ta = toMillis(a.started_at ?? a.created_at) ?? 0;
        const tb = toMillis(b.started_at ?? b.created_at) ?? 0;
        return tb - ta;
      });
      this._runs = sorted;
      this._authErrorShown = false;
      void vscode.commands.executeCommand(
        "setContext",
        "lionStudio.hasRuns",
        sorted.length > 0
      );
      return sorted.map((r) => new RunItem(r));
    } catch (err) {
      this._runs = [];
      if (
        err instanceof StudioApiError &&
        err.status === 401 &&
        !this._authErrorShown
      ) {
        this._authErrorShown = true;
        void vscode.window.showErrorMessage(
          "Lion Studio: authentication failed — check the lionStudio.authToken setting."
        );
      }
      return [];
    }
  }

  hasActiveRuns(): boolean {
    return this._runs.some((r) => !isTerminal(r));
  }

  getRuns(): Run[] {
    return this._runs;
  }
}

export function registerRunsExplorer(
  context: vscode.ExtensionContext,
  deps: StudioDeps
): void {
  const provider = new RunsProvider(deps);

  const treeView = vscode.window.createTreeView("lionStudio.runs", {
    treeDataProvider: provider,
    showCollapseAll: false,
  });

  let pollTimer: ReturnType<typeof setInterval> | undefined;

  function startPolling(): void {
    if (pollTimer !== undefined) {
      return;
    }
    pollTimer = setInterval(() => {
      if (!treeView.visible) {
        stopPolling();
        return;
      }
      provider.refresh();
      if (!provider.hasActiveRuns()) {
        stopPolling();
      }
    }, POLL_INTERVAL_MS);
  }

  function stopPolling(): void {
    if (pollTimer !== undefined) {
      clearInterval(pollTimer);
      pollTimer = undefined;
    }
  }

  function onRefresh(): void {
    provider.refresh();
    if (treeView.visible && deps.backend.isRunning()) {
      // Delay checking for active runs until after the data loads.
      setTimeout(() => {
        if (provider.hasActiveRuns()) {
          startPolling();
        }
      }, 500);
    }
  }

  // Auto-refresh when backend transitions to running.
  const stateListener = deps.backend.onDidChangeState((state) => {
    if (state === "running") {
      onRefresh();
    } else {
      stopPolling();
      provider.refresh();
    }
  });

  // Start/stop polling based on view visibility.
  const visibilityListener = treeView.onDidChangeVisibility(({ visible }) => {
    if (visible) {
      provider.refresh();
      if (deps.backend.isRunning() && provider.hasActiveRuns()) {
        startPolling();
      }
    } else {
      stopPolling();
    }
  });

  // After each tree data change, re-evaluate whether polling is needed.
  const dataChangeListener = provider.onDidChangeTreeData(() => {
    if (
      treeView.visible &&
      deps.backend.isRunning() &&
      provider.hasActiveRuns()
    ) {
      startPolling();
    } else if (!provider.hasActiveRuns()) {
      stopPolling();
    }
  });

  const refreshCmd = vscode.commands.registerCommand(
    "lionStudio.refreshRuns",
    () => {
      onRefresh();
    }
  );

  const openRunCmd = vscode.commands.registerCommand(
    "lionStudio.openRun",
    (run: Run) => {
      RunDetailPanel.open(context, deps, run);
    }
  );

  context.subscriptions.push(
    treeView,
    refreshCmd,
    openRunCmd,
    stateListener,
    visibilityListener,
    dataChangeListener,
    { dispose: stopPolling }
  );
}
