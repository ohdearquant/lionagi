# Shared utility consolidation

This audit records which existing `lionagi.ln` helpers can replace a caller without changing its behavior. Source addresses in the area ledgers refer to baseline `ee5783cc689fbff53afef49f772e6584121b70e6`; moved callers are named by function so they remain identifiable after line shifts.

| Area | Accounting |
| --- | --- |
| Clocks and identifiers | [Clock callers](clocks.md) |
| Files and processes | [Filesystem and process boundaries](files-and-processes.md) |
| Imports, copying and hashing | [Import and representation contracts](imports-copying-hashing.md) |
| Protocol utilities | [Types, sentinels and collections](protocols.md) |
| JSON | [Decoder and serialization boundaries](json.md) |
| Concurrency | [Worker, delay and task ownership](concurrency.md) |

The remaining equivalent substitutions reuse two module loaders, nine worker offloads, five delays and one process start-time comparison. Existing public exports and shared-helper implementations stay unchanged. Native operations with different admission, exception, encoding, task-lifetime or resource-ownership behavior remain explicit in the area ledgers.

Ruff TID251 prevents direct `importlib.import_module`, `asyncio.sleep`, `anyio.sleep` and `anyio.to_thread.run_sync` from returning to the eight migrated import/concurrency modules. Its negated per-file rule limits enforcement to those modules; the retained native boundaries elsewhere remain valid.

Compared with DEV baseline `80d3e33e1a691c33632944e92f279aefef81c77f`, on native Python 3.10 the 576 production modules outside `lionagi/ln/` contain 2,217 standard-library import statements before these changes and 2,215 after them (1,804 and 1,802 excluding `__future__`). This counts each AST `Import` or absolute `ImportFrom` statement once when at least one imported root belongs to `sys.stdlib_module_names`. It includes function-local imports and excludes relative and third-party imports. These statement counts are separate from the caller inventories and from older text-search estimates.

Focused validation includes the new caller contracts and existing tests for class lookup, hook settings, package exports, worker cancellation, admission, lifecycle callbacks, scheduler behavior, monitor snapshots and Studio administration. Exact commands and observed results are recorded in the pull request.
