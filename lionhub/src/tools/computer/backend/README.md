# lionhub.tools.computer.backend

The sandbox backends that `lionhub.tools.computer` actions run on: one module per place a command can execute (a local worktree, a docker container, a hosted sandbox). It is not the computer workspace itself (blobs, trees, actions), which sits one level up.

Final import path: `lionhub.tools.computer.backend`. The existing backend module `lionagi.tools.sandbox_backend` keeps working through 1.x as the same module object, with a `DeprecationWarning` naming the new path; 2.0 removes it. The core reaches this package only lazily, never at import time.

| Slot | What it holds | Source |
| --- | --- | --- |
| `__init__.py` | the `SandboxBackend` protocol and the backend lookup by name | `lionagi/tools/sandbox_backend.py` |
| `local.py` | the backend that runs in a local git worktree (`LocalWorktreeBackend`) | `lionagi/tools/sandbox_backend.py` |
| `daytona.py` | the Daytona hosted-sandbox backend (`DaytonaBackend`, `DaytonaSandbox`) | `lionagi/tools/daytona.py`, `lionagi/tools/sandbox_backend.py`, `lion1/hub/tools/daytona.py` |
| `docker.py` | a box in a docker container, one `docker exec` per command | `lion1/hub/tools/docker.py`; the workspace design's command runner (docker mode) |
| `e2b.py` | a backend on E2B hosted sandboxes | new; no existing code |
| `fly.py` | a backend on Fly Machines | new; no existing code |

The layout decision creates `e2b.py` and `fly.py` together with their first code, not as empty modules.

## Who uses it

No module on the development line imports `lionagi/tools/sandbox_backend.py` by name today; `lionagi/tools/daytona.py` is imported only by it. The computer actions (`execer.py`) will select a backend by name through the lookup in `__init__.py`.

## Status

`local.py`, `daytona.py` and the protocol move next, with the tools move; `docker.py` is new code from a concept; `e2b.py` and `fly.py` are new code.
