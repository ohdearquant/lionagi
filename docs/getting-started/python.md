# Python Quickstart

Use the Python API when LionAGI is part of your application. This path uses an
API provider rather than a subscription-backed coding CLI.

## 1. Create a project

```bash
mkdir lionagi-python-start
cd lionagi-python-start
uv init --bare
uv add lionagi
export OPENAI_API_KEY="..."
```

Confirm that the key exists in this shell without printing it:

```bash
test -n "$OPENAI_API_KEY" && echo "OPENAI_API_KEY is configured"
```

## 2. Record one chat turn

Save this as `main.py`:

```python
import asyncio

from lionagi import Branch, iModel


async def main() -> None:
    async with iModel(model="openai/gpt-5.4") as model:
        branch = Branch(
            chat_model=model,
            system="You are a concise technical guide.",
        )
        reply = await branch.communicate(
            "Explain dependency-aware execution in one paragraph."
        )
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("The provider returned no text")
        print(reply)


asyncio.run(main())
```

Run it:

```bash
uv run python main.py
```

`Branch.communicate()` adds both sides of the turn to the branch's message
history. A non-empty paragraph is the success evidence.

If authentication fails, confirm the key is exported in the same shell and
that the model's provider prefix matches the key. `OPENAI_API_KEY` is for
`openai/...`; it does not authenticate the Codex CLI.

## 3. Request typed output

Replace `main.py` with this complete example to call `operate()` on the same
branch after the recorded chat turn:

```python
import asyncio

from lionagi import Branch, iModel
from pydantic import BaseModel


class ReviewPlan(BaseModel):
    first_step: str
    checks: list[str]


async def main() -> None:
    async with iModel(model="openai/gpt-5.4") as model:
        branch = Branch(chat_model=model)
        await branch.communicate(
            "We are reviewing an async Python module for reliability."
        )
        plan = await branch.operate(
            instruction="Create a small review plan for that module.",
            response_format=ReviewPlan,
        )

        if not isinstance(plan, ReviewPlan):
            raise RuntimeError(
                f"Expected ReviewPlan, received {type(plan).__name__}"
            )

        print(plan.model_dump_json(indent=2))


asyncio.run(main())
```

Run it again with `uv run python main.py`. `Branch.operate()` adds the turn and
returns the validated response model when the provider satisfies the schema.

When you register tools, use `operate(actions=True, tools=...)` or `ReAct()` so
the model can actually invoke them. `communicate()` intentionally does not
invoke tools.

## Context and cleanup

- Reuse one `Branch` when later turns should see earlier messages.
- Start a new `Branch` for independent work. For a single reset turn,
  `communicate(..., clear_messages=True)` clears the existing history first.
- Keep the `iModel` async context manager shown above. It stops the model
  executor on exit.
- Python branches live in your process. The CLI's automatic
  `~/.lionagi/runs/` persistence is not implied when you construct a `Branch`
  directly.

## Next step

Use [`Session` and `Builder`](../api/flow.md) when your application owns a DAG.
Use the [CLI orchestration guide](../guides/orchestration.md) when you want an
orchestrator model to plan and run the graph from the terminal.
