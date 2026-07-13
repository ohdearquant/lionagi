# Troubleshooting

## `ImportError` — lionagi lazy imports

lionagi uses `__getattr__`-based lazy loading. Use public exports, not sub-package paths:

```python
# ✓ Works — public export surface
from lionagi import Branch, iModel, Builder, HookRegistry

# ✗ Imports the module object, not the callable
from lionagi.operations.communicate import communicate

# ✓ Works — full module path to the .py file
from lionagi.operations.communicate.communicate import communicate
from lionagi.operations.run.run import run_and_collect
```

Always import top-level names via `import lionagi as li` or `from lionagi import <Name>`.

## `RuntimeError` — event loop in Jupyter

```python
# ✗ Fails in Jupyter — already inside a running event loop
asyncio.run(branch.operate(...))

# ✓ Use await directly in Jupyter cells
result = await branch.operate(instruction="...")
```

Or install `nest_asyncio` before running:

```python
import nest_asyncio
nest_asyncio.apply()
asyncio.run(branch.operate(...))
```

## CLI: run-id not found

```text
error: run not found: 20260420T103404-abc123
```

Run artifacts live under `~/.lionagi/runs/{run_id}/`. Check what exists:

```bash
ls ~/.lionagi/runs/
li o flow --resume <run-or-session-id>
```

Flow resume accepts a run, session, or invocation ID. To resume one agent
conversation instead, choose a snapshot filename under
`~/.lionagi/runs/<run-id>/branches/` and pass that **branch ID** to
`li agent --resume <branch-id>`.

## CLI: `--background` output not visible

`--background` is a flow option. It requires `--save`, detaches the flow into a
subprocess, and prints a monitorable session ID. Follow the log in the save
directory:

```bash
li o flow codex "Audit this repository" --background --save ./audit-out
tail -f ./audit-out/flow.log
```

## `stream_persist` JSONL behavior

When `stream_persist=True`, chunks write to `{persist_dir}/{branch_id}.buffer.jsonl`.
Each line is `{"content": "...chunk..."}`.

Default `persist_dir` is `~/.lionagi/logs/runs` when not set.

The return value of `operate()` / `run()` is the **complete accumulated text** (or parsed
`BaseModel`), not the JSONL path.

## Parse validation returns `None` or raw string

`operate()` defaults to `handle_validation="return_value"` — parse failures silently return
the raw string. To diagnose:

1. Check `handle_validation` — set `"raise"` to surface the exact error.
2. Confirm `response_format` is a Pydantic `BaseModel` subclass (not a dataclass).
3. Enable fuzzy matching — `fuzzy_match=True` tolerates key name variations.
4. Lower `similarity_threshold` — try `0.75` for noisy model output (default `0.85`).

```python
result = await branch.operate(
    instruction="Extract entity",
    response_format=EntityModel,
    handle_validation="raise",   # surface the real error
)
```

## Rate limit errors from provider

```text
RateLimitError: 429 Too Many Requests
```

lionagi's rate limiter is proactive — configure it to queue before hitting the limit:

```python
model = li.iModel(
    model="gpt-4o",
    limit_requests=60,       # stay under provider RPM
    limit_tokens=80_000,     # stay under provider TPM
    capacity_refresh_time=60,
)
```

## `AttributeError` — branch property access

Branch properties like `messages`, `tools`, `logs` are read-only piles, not lists.
Use pile access patterns:

```python
# ✗ list operations don't apply
branch.messages[0]          # index access on Pile uses UUID, not int position

# ✓ iterate or convert
for msg in branch.messages:
    print(msg.content)

df = branch.to_df()         # convert to DataFrame for tabular access
```

Next: [Migration guide](../migration/0.22.5-to-0.22.6.md)
