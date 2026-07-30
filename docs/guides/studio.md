# Studio, Operator, and schedules

Lion Studio is an operational UI backed by a local daemon. Operator
conversations, Runs, and schedules use that daemon as their durable source, so
keep it running while work is active.

## Install the Studio dependencies

In a project environment:

```bash
uv add "lionagi[studio]"
```

Or with an activated `pip` environment:

```bash
python -m pip install "lionagi[studio]"
```

## Prepare Claude Code for Operator

Operator uses the locally installed Claude Code CLI by default. The
`lionagi[studio]` extra installs Studio, but it does not install Claude Code or
authenticate a Claude account.

Install and authenticate Claude Code before sending the first Operator
instruction:

```bash
npm install -g @anthropic-ai/claude-code
claude --version
claude auth login
```

Claude Code can also authenticate when you launch `claude` interactively. Its
existing CLI login is used directly; no Anthropic API key is required in
LionAGI for a subscription-backed login. See the
[Claude Code setup guide](https://code.claude.com/docs/en/setup) for supported
installation and enterprise authentication options.

## Start Studio

The default starts the local API daemon on port 8765 and opens the hosted web
client at `https://lion-studio.khive.ai`:

```bash
li studio
```

The hosted page is the frontend; it connects to the local daemon at
`http://127.0.0.1:8765`. Keep the command running. Use `--no-open` when you do
not want LionAGI to open a browser.

Other shipped modes are:

```bash
li studio --docker       # bundled frontend and backend through Docker
li studio --no-frontend  # local API only
li studio --dev          # source checkout with frontend hot reload
```

In a second terminal, confirm daemon reachability:

```bash
li doctor
```

The `studio_daemon` check should now be healthy.

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

Leaving the card unanswered leaves Operator waiting; it is not silently
approved.

### Follow work into Fleet and Runs

Every Operator turn creates a normal Studio run. Use **Open run** in the
conversation to inspect its status, messages, tool results, and activity in
Fleet's Runs view.

Run detail includes **Continue this run** for every live or terminal run that
still has a persisted branch:

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

The always-visible **CLI escape hatch** shows and copies the equivalent command:

```bash
li agent -r '<branch-id>' --prompt '<follow-up instruction>'
```

For a multi-branch run, select a branch first. When no branch is available, the
placeholder remains visible but copying is disabled.

## Verify the real local Operator path

Automated tests cannot establish that a local Claude Code installation is
present and authenticated. Before relying on Operator on a new machine or
shipping a release, exercise the real path in a disposable project:

1. Run `claude --version`, then authenticate with `claude auth login` if
   needed.
2. Start Studio with `li studio` and open Operator.
3. Send `Reply with exactly: operator-ready` and confirm text arrives
   incrementally, the turn completes, and **Open run** reaches the matching
   run.
4. Reload Studio and confirm the conversation and completed turn return.
5. Ask Operator to create a harmless temporary file. Choose **Deny** and
   confirm the file was not created. Ask again, choose **Allow**, and confirm
   the file was created; then remove it.
6. Start a longer inspection, choose **Stop**, and confirm the conversation
   reports **Turn stopped** and the linked run reaches a cancelled state.
7. From that run, use **Continue this run** to send a follow-up and confirm the
   new activity appears.

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

## Create a schedule

Create a daily CLI-agent action:

```bash
li schedule create daily-review \
  --cron "0 9 * * *" \
  --action-kind agent \
  --model codex \
  --cwd . \
  --prompt "Review the repository and summarize the highest-risk change."
```

The command prints `Created:` followed by the schedule ID and name. Save the ID:

```bash
SCHEDULE_ID=<id-from-create>
```

Inspect and trigger it without waiting for the next cron tick:

```bash
li schedule get "$SCHEDULE_ID"
li schedule trigger "$SCHEDULE_ID"
li schedule runs "$SCHEDULE_ID"
```

`trigger` prints a schedule-run ID when the daemon accepts the fire. Wait for
that run in a script with:

```bash
li monitor run <schedule-run-id> --max-wait 900
```

The wait command exits when the schedule run reaches a terminal status and
maps that status to its process exit code. By default it follows
`on_success`/`on_fail` chain children; add `--no-chain` to wait only for the
literal run ID.

## Other trigger and safety options

The live schedule API supports:

- `--interval SECONDS` for fixed intervals.
- `--trigger-type github --github-repo OWNER/NAME --github-filter JSON` for
  polled GitHub events.
- `--threshold-config JSON` for metric-threshold alerts evaluated on the
  schedule's cadence.
- `--once` or `--max-runs N` to bound the number of fires.
- `--max-cost-usd` and `--max-tokens` to stop future fires after a cumulative
  budget is reached.
- `li schedule limits` to inspect the daemon-wide concurrent-fire cap.

## Expected state

- The schedule definition appears in `li schedule list` and Studio.
- Each fire creates a schedule-run row visible through
  `li schedule runs ID`.
- The action creates its normal session, run state, and artifacts.
- Disabling a schedule preserves its history; deleting removes the definition.

If a schedule command reports a connection failure, start `li studio` or set
`LIONAGI_STUDIO_URL` to the daemon you intend to use. If a trigger is accepted
but no work completes, inspect `li schedule runs ID`, then use
`li monitor run RUN_ID` and `li doctor`.

Next, return to [durable operations](durable-operations.md) for live control and
checkpoint recovery.
