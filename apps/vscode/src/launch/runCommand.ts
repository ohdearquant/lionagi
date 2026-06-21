import * as path from "path";
import * as vscode from "vscode";
import { StudioApiError } from "../api/client.js";
import type { StudioDeps } from "../extension.js";
import { rememberLaunch } from "./launchStore.js";
import { openLaunchStreamPanel } from "./launchStreamPanel.js";
import { RunTreePanel } from "../runs/runTreePanel.js";

const POLL_INTERVAL_MS = 1_000;
const POLL_TIMEOUT_MS = 30_000;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function handleLaunchError(err: unknown): void {
  if (err instanceof StudioApiError) {
    if (err.status === 429) {
      void vscode.window.showErrorMessage(
        "Too many concurrent launches, try again shortly."
      );
    } else if (err.status === 422) {
      void vscode.window.showErrorMessage(
        `Validation error: ${err.detail}`
      );
    } else {
      void vscode.window.showErrorMessage(
        `Launch failed (${err.status}): ${err.detail}`
      );
    }
    return;
  }
  const msg = err instanceof Error ? err.message : String(err);
  void vscode.window.showErrorMessage(`Launch failed: ${msg}`);
}

/** Poll GET /api/invocations/{id} until a child session id appears or timeout. */
async function pollForSessionId(
  deps: StudioDeps,
  invocationId: string,
  signal: AbortSignal
): Promise<string | undefined> {
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  while (Date.now() < deadline && !signal.aborted) {
    await delay(POLL_INTERVAL_MS);
    if (signal.aborted) {
      break;
    }
    try {
      const inv = await deps.client.getInvocation(invocationId);
      const first = inv.sessions[0];
      if (first?.id) {
        return first.id;
      }
    } catch {
      // transient — keep polling
    }
  }
  return undefined;
}

async function collectAgentParams(): Promise<
  | { action_model: string; action_prompt: string; action_project?: string }
  | undefined
> {
  const model = await vscode.window.showInputBox({
    title: "Den: Run Agent — Model",
    prompt: "Model string to use.",
    placeHolder: "openai/gpt-4.1-mini",
    ignoreFocusOut: true,
  });
  if (model === undefined) {
    return undefined;
  }

  const prompt = await vscode.window.showInputBox({
    title: "Den: Run Agent — Prompt",
    prompt: "Instruction for the agent. Required.",
    placeHolder: "e.g. Summarize the repo README",
    validateInput(v) {
      return v.trim() ? null : "Prompt cannot be empty.";
    },
    ignoreFocusOut: true,
  });
  if (prompt === undefined) {
    return undefined;
  }

  const project = await vscode.window.showInputBox({
    title: "Den: Run Agent — Project (optional)",
    prompt: "Project label. Leave blank to skip.",
    placeHolder: "e.g. ohdearquant/lionagi",
    ignoreFocusOut: true,
  });
  if (project === undefined) {
    return undefined;
  }

  return {
    action_model: model.trim() || "openai/gpt-4.1-mini",
    action_prompt: prompt,
    action_project: project.trim() || undefined,
  };
}

async function collectFlowParams(): Promise<
  | { action_model: string; action_prompt: string; action_project?: string }
  | undefined
> {
  const model = await vscode.window.showInputBox({
    title: "Den: Run Flow — Model",
    prompt: "Model string for the orchestrated flow.",
    placeHolder: "openai/gpt-4.1-mini",
    ignoreFocusOut: true,
  });
  if (model === undefined) {
    return undefined;
  }

  const prompt = await vscode.window.showInputBox({
    title: "Den: Run Flow — Objective",
    prompt: "Goal for the orchestrated flow. The orchestrator plans a DAG from it. Required.",
    placeHolder: "e.g. Plan and implement the feature across the codebase",
    validateInput(v) {
      return v.trim() ? null : "Objective cannot be empty.";
    },
    ignoreFocusOut: true,
  });
  if (prompt === undefined) {
    return undefined;
  }

  const project = await vscode.window.showInputBox({
    title: "Den: Run Flow — Project (optional)",
    prompt: "Project label. Leave blank to skip.",
    placeHolder: "e.g. ohdearquant/lionagi",
    ignoreFocusOut: true,
  });
  if (project === undefined) {
    return undefined;
  }

  return {
    action_model: model.trim() || "openai/gpt-4.1-mini",
    action_prompt: prompt,
    action_project: project.trim() || undefined,
  };
}

/** Shared post-launch tail: refresh, poll for session, open panel, offer tree. */
async function attachLaunchedRun(
  context: vscode.ExtensionContext,
  deps: StudioDeps,
  invocationId: string,
  panelTitle: string
): Promise<void> {
  void vscode.commands.executeCommand("den.refreshRuns");

  // 4. Poll the invocation until a child session id appears.
  const pollAbort = new AbortController();
  let sessionId: string | undefined;

  try {
    sessionId = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "Waiting for run to start…",
        cancellable: true,
      },
      async (_progress, token) => {
        token.onCancellationRequested(() => pollAbort.abort());
        return pollForSessionId(deps, invocationId, pollAbort.signal);
      }
    );
  } catch {
    // poll threw; leave sessionId undefined
  }

  if (!sessionId) {
    void vscode.window.showInformationMessage(
      "Launched. Open it from the Runs view once it appears."
    );
    return;
  }

  // 5. Attach the streaming panel.
  openLaunchStreamPanel(context, sessionId, panelTitle, deps);
  void vscode.commands.executeCommand("den.refreshRuns");

  // 6. Non-intrusive offer to open the run tree alongside the stream panel.
  void vscode.window.showInformationMessage("Run started.", "View Run Tree").then((choice) => {
    if (choice === "View Run Tree") {
      RunTreePanel.open(context, deps, sessionId, panelTitle);
    }
  });
}

/** Read YAML from the active editor or from a file picker. */
async function collectFlowYaml(): Promise<{ yaml: string; title: string } | undefined> {
  const editor = vscode.window.activeTextEditor;
  if (editor) {
    const text = editor.document.getText().trim();
    if (text) {
      const fsPath = editor.document.uri.fsPath;
      const title = fsPath ? path.basename(fsPath) : "Flow from YAML";
      return { yaml: text, title };
    }
  }

  const uris = await vscode.window.showOpenDialog({
    canSelectMany: false,
    filters: { YAML: ["yaml", "yml"], "All files": ["*"] },
    title: "Den: Run Flow from YAML — pick a flow spec",
  });
  if (uris && uris[0]) {
    const bytes = await vscode.workspace.fs.readFile(uris[0]);
    const yaml = Buffer.from(bytes).toString("utf8").trim();
    if (yaml) {
      return { yaml, title: path.basename(uris[0].fsPath) };
    }
  }

  void vscode.window.showInformationMessage(
    "Open a flow YAML file in the editor, or pick one, to run it."
  );
  return undefined;
}

/** Collect model + optional project for a flow_yaml launch (no prompt — YAML is the spec). */
async function collectFlowYamlParams(): Promise<
  { action_model: string; action_project?: string } | undefined
> {
  const model = await vscode.window.showInputBox({
    title: "Den: Run Flow from YAML — Model",
    prompt: "Model string for the flow.",
    placeHolder: "openai/gpt-4.1-mini",
    ignoreFocusOut: true,
  });
  if (model === undefined) {
    return undefined;
  }

  const project = await vscode.window.showInputBox({
    title: "Den: Run Flow from YAML — Project (optional)",
    prompt: "Project label. Leave blank to skip.",
    placeHolder: "e.g. ohdearquant/lionagi",
    ignoreFocusOut: true,
  });
  if (project === undefined) {
    return undefined;
  }

  return {
    action_model: model.trim() || "openai/gpt-4.1-mini",
    action_project: project.trim() || undefined,
  };
}

export function registerRunCommand(
  context: vscode.ExtensionContext,
  deps: StudioDeps
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("den.run", async () => {
      // 1. Ensure backend is running.
      if (!deps.backend.isRunning()) {
        const choice = await vscode.window.showWarningMessage(
          "Den backend is not running. Start it?",
          "Start"
        );
        if (choice !== "Start") {
          return;
        }
        await deps.backend.start();
        if (!deps.backend.isRunning()) {
          void vscode.window.showErrorMessage(
            "Backend failed to start. Check the Den output channel."
          );
          return;
        }
      }

      // 2. Choose kind.
      const kind = await vscode.window.showQuickPick(["Agent", "Flow"], {
        title: "Den: Run",
        placeHolder: "What do you want to launch?",
        ignoreFocusOut: true,
      });
      if (!kind) {
        return;
      }

      // 3. Collect kind-specific params and build the launch request.
      let invocationId: string;
      let panelTitle: string;

      if (kind === "Agent") {
        const params = await collectAgentParams();
        if (!params) {
          return;
        }
        panelTitle = params.action_prompt;
        const req = {
          action_kind: "agent" as const,
          action_model: params.action_model,
          action_prompt: params.action_prompt,
          action_project: params.action_project,
        };
        try {
          const result = await vscode.window.withProgress(
            {
              location: vscode.ProgressLocation.Notification,
              title: "Launching agent…",
              cancellable: false,
            },
            () => deps.client.launch(req)
          );
          invocationId = result.invocation_id;
          rememberLaunch(result.invocation_id, req);
        } catch (err) {
          handleLaunchError(err);
          return;
        }
      } else {
        const params = await collectFlowParams();
        if (!params) {
          return;
        }
        panelTitle = params.action_prompt;
        const req = {
          action_kind: "flow" as const,
          action_model: params.action_model,
          action_prompt: params.action_prompt,
          action_project: params.action_project,
        };
        try {
          const result = await vscode.window.withProgress(
            {
              location: vscode.ProgressLocation.Notification,
              title: "Launching flow…",
              cancellable: false,
            },
            () => deps.client.launch(req)
          );
          invocationId = result.invocation_id;
          rememberLaunch(result.invocation_id, req);
        } catch (err) {
          handleLaunchError(err);
          return;
        }
      }

      await attachLaunchedRun(context, deps, invocationId, panelTitle);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("den.runFlowFromYaml", async () => {
      // 1. Ensure backend is running.
      if (!deps.backend.isRunning()) {
        const choice = await vscode.window.showWarningMessage(
          "Den backend is not running. Start it?",
          "Start"
        );
        if (choice !== "Start") {
          return;
        }
        await deps.backend.start();
        if (!deps.backend.isRunning()) {
          void vscode.window.showErrorMessage(
            "Backend failed to start. Check the Den output channel."
          );
          return;
        }
      }

      // 2. Collect YAML source.
      const src = await collectFlowYaml();
      if (!src) {
        return;
      }

      // 3. Collect model and project.
      const params = await collectFlowYamlParams();
      if (!params) {
        return;
      }

      // 4. Build and launch.
      const req = {
        action_kind: "flow_yaml" as const,
        action_model: params.action_model,
        action_flow_yaml: src.yaml,
        action_project: params.action_project,
      };
      let invocationId: string;
      try {
        const result = await vscode.window.withProgress(
          {
            location: vscode.ProgressLocation.Notification,
            title: "Launching flow from YAML…",
            cancellable: false,
          },
          () => deps.client.launch(req)
        );
        invocationId = result.invocation_id;
        rememberLaunch(result.invocation_id, req);
      } catch (err) {
        handleLaunchError(err);
        return;
      }

      // 5. Attach run panel.
      await attachLaunchedRun(context, deps, invocationId, src.title);
    })
  );
}
