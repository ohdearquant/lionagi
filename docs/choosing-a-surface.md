# Choose the Lightest Surface

Start with the shape of the task. Move to a heavier surface only when the task
needs its coordination or operational guarantees.

| Task shape | Use | Why |
|------------|-----|-----|
| One terminal task | `li agent MODEL "prompt"` | One agent, no planning turn |
| Continue prior terminal work | `li agent -c` or `li agent -r BRANCH_ID` | Reuses saved conversation state |
| Independent perspectives | `li o fanout` | Parallel workers, optional synthesis |
| Work with dependencies | `li o flow` | Plans and executes a dependency-aware graph |
| The same planned flow repeatedly | `li play NAME` | Named, parameterized, versionable playbook |
| A shipped domain pipeline | `li engine run KIND` | Prebuilt coding, research, review, planning, or hypothesis engine |
| Run later or repeatedly | `li schedule create` | Cron, interval, GitHub, and threshold triggers through Studio |
| Operate runs visually | `li studio` | Hosted UI connected to the local daemon |
| One recorded API-model turn in code | `Branch.communicate()` | Stateful chat without tool invocation |
| Typed or tool-aware work in code | `Branch.operate()` | Structured output and, with `actions=True`, tools |
| An application-owned DAG | `Builder` + `Session.flow()` | Your code owns graph construction and execution |

## A quick decision path

1. If the task belongs inside your application, use Python. Choose
   `communicate()` for chat and `operate()` for structured or tool-aware work.
2. If the task is in the terminal and one agent can finish it, use `li agent`.
3. If subtasks are independent, use fan-out.
4. If any subtask consumes another's result, use flow.
5. If you run that flow repeatedly, promote it to a playbook.
6. Add Studio or a schedule only when the run needs an operational UI, a
   trigger, or unattended execution.

## Cost of each step up

- `li agent` makes one agent turn and starts immediately.
- Fan-out adds a decomposition turn before workers run.
- Flow adds planning and dependency management. Preview it with `--dry-run`
  and cap growth with `--max-ops`.
- A playbook improves repeatability, not first-run latency; it still uses the
  flow execution path.
- Schedules require the Studio daemon to be running when a trigger fires.

## Common choices

### Inspect a repository once

```bash
li agent codex "Identify the highest-risk module and explain why." --cwd .
```

### Compare independent reviews

```bash
li o fanout codex "Review this repository." --cwd . -n 3 --with-synthesis
```

### Plan dependent work safely

```bash
li o flow codex "Audit, fix, and verify this package." --cwd . --max-ops 6 --dry-run
```

### Build typed application behavior

```python
result = await branch.operate(
    instruction="Extract the risks from this report.",
    response_format=RiskReport,
)
```

Next, follow the [orchestration progression](guides/orchestration.md), the
[durable operations guide](guides/durable-operations.md), or the
[Studio and schedules guide](guides/studio.md).
