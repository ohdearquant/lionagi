import * as vscode from "vscode";
import { StudioApiError } from "../api/client.js";
import type { Run } from "../api/types.js";
import type { StudioDeps } from "../extension.js";
import { openLaunchStreamPanel } from "./launchStreamPanel.js";

const POLL_INTERVAL_MS = 1_000;
const POLL_TIMEOUT_MS = 30_000;

async function pollForRun(
  deps: StudioDeps,
  invocationId: string,
  signal: AbortSignal
): Promise<Run | undefined> {
  const deadline = Date.now() + POLL_TIMEOUT_MS;

  while (Date.now() < deadline && !signal.aborted) {
    await delay(POLL_INTERVAL_MS);
    if (signal.aborted) {
      break;
    }
    try {
      const page = await deps.client.listRuns({ per_page: 50 });
      const match = page.runs.find((r) => r.invocation_id === invocationId);
      if (match) {
        return match;
      }
    } catch {
      // transient — keep polling
    }
  }
  return undefined;
}

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

export function registerAgentTrigger(
  context: vscode.ExtensionContext,
  deps: StudioDeps
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("den.runAgent", async () => {
      // 1. Ensure backend is running
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

      // 2. Collect prompt (required)
      const actionPrompt = await vscode.window.showInputBox({
        title: "Den: Run Agent",
        prompt:
          "Enter the instruction or task for the agent. Press Enter to launch.",
        placeHolder:
          "e.g. Summarize the current file and list any TODO comments",
        validateInput(value) {
          return value.trim() ? null : "Prompt cannot be empty.";
        },
        ignoreFocusOut: true,
      });

      if (actionPrompt === undefined) {
        // cancelled
        return;
      }

      // 3. Collect optional agent name
      const agentNameInput = await vscode.window.showInputBox({
        title: "Den: Agent name (optional)",
        prompt:
          "Specify a named agent profile, or leave blank to use the default.",
        placeHolder: "e.g. reviewer (leave blank to skip)",
        ignoreFocusOut: true,
      });

      if (agentNameInput === undefined) {
        // cancelled
        return;
      }

      const actionAgent = agentNameInput.trim() || undefined;

      // 4. Launch with progress indicator
      let invocationId: string;
      try {
        const result = await vscode.window.withProgress(
          {
            location: vscode.ProgressLocation.Notification,
            title: "Launching agent…",
            cancellable: false,
          },
          () =>
            deps.client.launch({
              action_kind: "agent",
              action_prompt: actionPrompt,
              action_agent: actionAgent,
            })
        );
        invocationId = result.invocation_id;
      } catch (err) {
        handleLaunchError(err);
        return;
      }

      // Refresh the Runs tree regardless of what happens next
      void vscode.commands.executeCommand("den.refreshRuns");

      // 5. Poll until the run record appears, then stream it
      const pollAbort = new AbortController();
      let run: Run | undefined;

      try {
        run = await vscode.window.withProgress(
          {
            location: vscode.ProgressLocation.Notification,
            title: "Waiting for run to start…",
            cancellable: true,
          },
          async (_progress, token) => {
            token.onCancellationRequested(() => pollAbort.abort());
            return pollForRun(deps, invocationId, pollAbort.signal);
          }
        );
      } catch {
        // poll itself threw; surface nothing — run stays undefined
      }

      if (!run) {
        void vscode.window.showInformationMessage(
          "Agent launched. Open it from the Runs view once it appears."
        );
        return;
      }

      // 6. Open the streaming panel
      openLaunchStreamPanel(context, run.run_id, actionPrompt, deps);

      // Refresh again now that we have a run_id confirmed
      void vscode.commands.executeCommand("den.refreshRuns");
    })
  );
}
