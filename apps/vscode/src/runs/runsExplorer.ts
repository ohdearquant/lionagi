import * as vscode from "vscode";
import type { StudioDeps } from "../extension.js";
import type { Run } from "../api/types.js";
import { StudioApiError } from "../api/client.js";
import {
  RunItem,
  ProjectGroupItem,
  LoadMoreItem,
  isTerminal,
  toMillis,
} from "./runItem.js";
import { RunDetailPanel } from "./runDetailPanel.js";

const POLL_INTERVAL_MS = 4_000;
const PAGE_SIZE = 50;

type RunNode = ProjectGroupItem | RunItem | LoadMoreItem;

function newestFirst(runs: Run[]): Run[] {
  return [...runs].sort((a, b) => {
    const ta = toMillis(a.started_at ?? a.created_at) ?? 0;
    const tb = toMillis(b.started_at ?? b.created_at) ?? 0;
    return tb - ta;
  });
}

class RunsProvider implements vscode.TreeDataProvider<RunNode> {
  private _authErrorShown = false;
  // How many pages each project group has loaded (grows on "Load more").
  private readonly _depth = new Map<string, number>();
  // Latest runs fetched per group — drives active-run detection between renders.
  private readonly _runsByGroup = new Map<string, Run[]>();
  // Current group node instances, so "Load more" can refresh one group in place.
  private readonly _groupItems = new Map<string, ProjectGroupItem>();
  private readonly _onDidChangeTreeData =
    new vscode.EventEmitter<RunNode | undefined | null | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  constructor(private readonly deps: StudioDeps) {}

  refresh(): void {
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: RunNode): vscode.TreeItem {
    return element;
  }

  async getChildren(element?: RunNode): Promise<RunNode[]> {
    if (element instanceof RunItem || element instanceof LoadMoreItem) {
      return [];
    }
    if (!this.deps.backend.isRunning()) {
      this._reset();
      return [];
    }
    if (element instanceof ProjectGroupItem) {
      return this._loadGroup(element);
    }
    return this._loadGroups();
  }

  // Root: cheap per-project counts only, no runs loaded. Most-recent project expands.
  private async _loadGroups(): Promise<RunNode[]> {
    try {
      const { projects, total } = await this.deps.client.listProjectGroups();
      this._authErrorShown = false;
      void vscode.commands.executeCommand(
        "setContext",
        "lionStudio.hasRuns",
        total > 0
      );
      this._groupItems.clear();
      return projects.map((g, i) => {
        const item = new ProjectGroupItem(g, i === 0);
        this._groupItems.set(item.key, item);
        return item;
      });
    } catch (err) {
      this._handleError(err);
      return [];
    }
  }

  // A group's children: its loaded runs (re-fetched fresh each render) + a Load more leaf.
  private async _loadGroup(item: ProjectGroupItem): Promise<RunNode[]> {
    const perPage = (this._depth.get(item.key) ?? 1) * PAGE_SIZE;
    const filter =
      item.group.project === null
        ? { project_null: true }
        : { project: item.group.project };
    try {
      const page = await this.deps.client.listRuns({
        ...filter,
        page: 1,
        per_page: perPage,
      });
      const runs = newestFirst(page.runs);
      this._runsByGroup.set(item.key, runs);
      const nodes: RunNode[] = runs.map((r) => new RunItem(r));
      if (page.has_next) {
        nodes.push(new LoadMoreItem(item.key, runs.length, item.group.count));
      }
      return nodes;
    } catch (err) {
      this._handleError(err);
      return [];
    }
  }

  // Grow a group by one page and re-render just that group.
  loadMore(key: string): void {
    this._depth.set(key, (this._depth.get(key) ?? 1) + 1);
    this._onDidChangeTreeData.fire(this._groupItems.get(key));
  }

  hasActiveRuns(): boolean {
    for (const runs of this._runsByGroup.values()) {
      if (runs.some((r) => !isTerminal(r))) {
        return true;
      }
    }
    return false;
  }

  private _reset(): void {
    this._depth.clear();
    this._runsByGroup.clear();
    this._groupItems.clear();
  }

  private _handleError(err: unknown): void {
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
  }
}

export function registerRunsExplorer(
  context: vscode.ExtensionContext,
  deps: StudioDeps
): void {
  const provider = new RunsProvider(deps);

  const treeView = vscode.window.createTreeView("lionStudio.runs", {
    treeDataProvider: provider,
    showCollapseAll: true,
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

  const loadMoreCmd = vscode.commands.registerCommand(
    "lionStudio.loadMoreRuns",
    (key: string) => {
      provider.loadMore(key);
    }
  );

  context.subscriptions.push(
    treeView,
    refreshCmd,
    openRunCmd,
    loadMoreCmd,
    stateListener,
    visibilityListener,
    dataChangeListener,
    { dispose: stopPolling }
  );
}
