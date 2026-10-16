# Use Operator

Operator is Studio's conversation dock for giving instructions to a local
agent and following its work. First [install Studio and authenticate Claude
Code](studio-getting-started.md#prepare-claude-code-for-operator), then keep
`li studio` running.

## Work from the Operator dock

Open Operator with the speech-bubble button at the top of Studio's left rail.
You can also press <kbd>Cmd</kbd>/<kbd>Ctrl</kbd>+<kbd>J</kbd> or open the
command palette and choose **Toggle Operator**.

Type an instruction and press <kbd>Enter</kbd> to send it.
<kbd>Shift</kbd>+<kbd>Enter</kbd> inserts a new line. Operator streams its
reply and tool activity into the same conversation.

### Durable history

The daemon, not the browser transcript, owns the conversation history. The
browser remembers which conversation is selected, then reloads its full
history from the daemon. Closing and reopening the dock, navigating to another
Studio surface, or reloading the page therefore keeps the conversation
scrollable from where you left it.

Closing the dock does not stop an active turn. Its **Live** or
**Reconnecting** indicator reports the stream state, and missed activity replays
after a reconnect.

### Stop an active turn

While Operator is working, **Stop** appears in the dock header. It requests
cancellation of the current turn and ends that turn as stopped. History and
completed tool results remain visible; Stop does not roll back work that
already finished.

If a permission decision is waiting, **Stop** cancels the whole turn. **Deny**
only rejects the individual proposed operation.

### Decide gated work

When Operator requests a disk write, command, or other gated native operation,
the conversation shows a **Permission required** card and Operator waits.

- **Allow** grants that exact request and lets the turn continue.
- **Deny** blocks that request. Operator receives the denial and may explain,
  revise its plan, or finish without the operation.
- Each later gated operation requires its own decision. An Allow is not a
  blanket approval for the rest of the turn.

Operator waits until you decide or the request expires, currently ten minutes
after creation. Expiry denies the request; it never approves it.

### Follow work into Fleet and Runs

Open the **Fleet** tab beside Overview to find recorded Operator execution and
inspect its status, messages, tool results, and activity. Successive turns can
reuse the conversation's persisted branch and run record. **Open run** appears
in the conversation only when a successful tool result contains a run ID; a
text-only reply has no such control.

Run detail checks whether **Continue this run** is available and explains a
refusal when the required saved state is missing or the run kind is unsupported.
There are two continuation paths.

#### Agent follow-ups

For an agent run with a persisted branch:

1. Enter the follow-up instruction.
2. If the run has more than one branch, select the branch to continue. A
   one-branch run is selected automatically.
3. Choose **Resume**. Studio reports **Follow-up accepted**, links the new
   activity, and refreshes the run.

Completed, failed, cancelled, and timed-out status do not prevent a resume.
The underlying branch snapshot must still exist. If history retention has
removed it, Studio leaves the instruction in place and reports that the run can
no longer be resumed.

For a run whose current leg is still active, Studio accepts one follow-up and
queues it behind that leg. The queued worker waits for the source session's
terminal transition, validates the final branch snapshot, and only then starts
the resumed `li agent` process. Repeating the identical request returns the
same accepted invocation; a different concurrent follow-up for that branch
returns a conflict instead of starting parallel work.

The agent form's **CLI escape hatch** shows and copies the equivalent command:

```bash
li agent -r '<branch-id>' --prompt '<follow-up instruction>'
```

For a multi-branch run, select a branch first. When no branch is available, the
placeholder remains visible but copying is disabled.

#### Checkpoint continuation

For play, flow, and show-play runs, choose **Continue** to replay the saved
checkpoint's plan. There is no instruction field or branch selector: the
checkpoint owns the work to resume. The required checkpoint must still exist
and be readable. Acceptance records the continuation request; a replay failure
is reported in the run's activity rather than guaranteeing completion.

These paths are defined by
`apps/studio/frontend/src/components/history/ResumeRun.tsx` and
`lionagi/studio/services/run_resume.py`. Run-link visibility and conversation
identity follow `components/operator/OperatorPanel.tsx` under that frontend
source directory and `lionagi/studio/operator/engine.py`.

## Verify the real local Operator path

Automated tests cannot establish that a local Claude Code installation is
present and authenticated. Before relying on Operator on a new machine or
shipping a release, exercise the real path in a disposable project:

1. Run `claude --version`, then authenticate with `claude auth login` if
   needed.
2. Start Studio with `li studio` and open Operator.
3. Send `Reply with exactly: operator-ready` and confirm text arrives
   incrementally and the turn completes. Open Fleet to inspect the recorded
   execution; this text-only instruction does not require an **Open run** link.
4. Reload Studio and confirm the conversation and completed turn return.
5. Ask Operator to create a harmless temporary file. Choose **Deny** and
   confirm the file was not created. Ask again, choose **Allow**, and confirm
   the file was created; then remove it.
6. Start a longer inspection, choose **Stop**, and confirm the conversation
   reports **Turn stopped** and inspect the cancelled execution in Fleet.
7. From that agent run, use **Continue this run** to send a follow-up and
   confirm the new activity appears.

This check uses the actual Claude Code executable, account, stream, permission
gate, cancellation path, and durable run store.

## Understand the CI stub boundary

The automated Operator suite uses a deterministic provider stub. It verifies
the conversation workflow, durable replay, streamed activity, Stop and
permission interactions, and Runs linkage without contacting a paid provider.

A green CI run does **not** prove that Claude Code is installed, that its local
login is valid, or that a live Claude Code release can complete the native
permission prompt. The real-local verification above is the release check for
those provider-dependent behaviors.

See [Studio surfaces](../reference/studio-surfaces.md) for the surrounding
pages and [durable operations](durable-operations.md) for CLI control.
