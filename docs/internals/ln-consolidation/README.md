# Shared utilities and native boundaries

`lionagi.ln` supplies shared helpers for compatible utility operations. These references describe which helpers callers use and where a native operation retains a different admission, representation, exception or resource-lifetime contract.

| Area | Reference |
| --- | --- |
| Clocks and identifiers | [Clock callers](clocks.md) |
| Files and processes | [Filesystem and process boundaries](files-and-processes.md) |
| Imports, copying and hashing | [Import and representation contracts](imports-copying-hashing.md) |
| Protocol utilities | [Types, sentinels and collections](protocols.md) |
| JSON | [Decoder and serialization boundaries](json.md) |
| Concurrency | [Worker, delay and task ownership](concurrency.md) |

Hook settings and built-in class lookup use the public module-only loader. Compatible worker and delay calls use `ln.concurrency`, and PID identity uses the shared start-time comparator. Native operations remain where those helpers would change caller-visible behavior; each area names the relevant functions and explains the boundary.

Ruff TID251 prevents direct `importlib.import_module`, `asyncio.sleep`, `anyio.sleep` and `anyio.to_thread.run_sync` from returning to the eight migrated import/concurrency modules. Its negated per-file rule limits enforcement to those modules; the retained native boundaries elsewhere remain valid.
