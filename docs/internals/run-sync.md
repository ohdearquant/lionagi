# Running synchronous callables in worker threads

`lionagi.ln.concurrency.run_sync` runs a callable in an AnyIO worker thread and
returns its result. Positional arguments and ordinary keyword arguments are
passed to the callable. Exceptions propagate to the waiting caller. Context
variables are copied into the worker; changes to those variables in the worker
do not update the caller's context. Argument objects are not deep-copied.

```python
from pathlib import Path

from lionagi.ln.concurrency import CapacityLimiter, run_sync

text = await run_sync(Path("notes.txt").read_text, encoding="utf-8")
limiter = CapacityLimiter(4)
result = await run_sync(blocking_function, "input", limiter=limiter)
```

The keyword-only controls belong to the offload:

`abandon_on_cancel` accepts only `bool` values. Other values raise `TypeError`
before a worker starts; they are not coerced through their truth value.

- `abandon_on_cancel=False` keeps AnyIO's default shielding of the active worker
  wait from cancellation by an enclosing AnyIO cancel scope. Waiting for a
  capacity token can still be cancelled before the callable starts.
- `abandon_on_cancel=True` lets a cancelled caller stop waiting. The worker
  thread continues, and its later result or exception is ignored by that
  caller. This option does not stop the function or undo its effects.
- `limiter=None` uses AnyIO's default thread limiter. Supplying an
  `lionagi.ln.concurrency.CapacityLimiter` uses its underlying limiter instead of the default.
  Native AnyIO limiter objects are not part of this wrapper's public contract.

These cancellation rules are AnyIO cancel-scope behavior; they do not promise
the same shielding for arbitrary direct `asyncio.Task.cancel()` calls. A
limiter is also not a hard bound on every still-running abandoned worker on
asyncio: abandoning a wait releases its token while that worker may still run.
On Trio, the token stays borrowed until the worker finishes.

## Callable keyword names

Direct `run_sync` calls reserve `abandon_on_cancel` and `limiter`. To supply
either name to the callable itself, prebind it with `functools.partial`:

```python
from functools import partial

bound = partial(
    blocking_function,
    limiter="a business argument",
    abandon_on_cancel="another business argument",
)
result = await run_sync(bound, "input")
```

Other keywords, including `cancellable`, remain callable arguments.
`run_sync` does not expose AnyIO's deprecated `cancellable` control alias.

Batch processing through `alcall`/`bcall` and synchronous handlers returned
by `get_handler` prebind their target arguments before offloading. Their
callable keyword arguments therefore keep their existing meaning, including
names that match offload controls. Async targets continue to receive those
arguments directly. Set offload controls through a direct `run_sync` call
when that policy is needed; passing them as batch or handler business
arguments does not change cancellation or thread admission.
