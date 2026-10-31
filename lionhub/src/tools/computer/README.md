# lionhub.tools.computer

A content-addressed workspace for tool runs: files live as immutable blobs, a directory is a tree value, and running a command is an action that takes a tree and returns a new tree. It is not the editor, reader or bash tool the core ships, and it is not an orchestration layer; it only provides the workspace and the actions over it.

Final import path: `lionhub.tools.computer`. Nothing here exists in `lionagi` today, so there is no old import path to keep and no `DeprecationWarning` shim applies. The core would reach this package only lazily, never at import time. This package is new code written from a content-addressed workspace design, not a move.

| Slot | What it holds | Source |
| --- | --- | --- |
| `__init__.py` | package marker and public exports | new; no existing code |
| `blobs.py` | immutable, deduplicated blob store; bytes in, content hash out | new; the workspace design's `blobs` |
| `image.py` | names the container image a command runs in | new; no existing code (the workspace design has no image module; only its command runner takes an image argument) |
| `trees.py` | trees as values: an immutable canonical list of entries stored as a blob; every write returns a new tree id | new; the workspace design's `trees` |
| `execer.py` | a command as an action: tree in, argv, closed environment, result tree out; keyed by its inputs and cached | new; the workspace design's `execer` |
| `fs.py` | path views over a tree id: numbered read, exact-string edit, glob, grep; writes return a new tree id | new; the workspace design's `fs` |
| `gitio.py` | git as an import and export adapter over trees | new; the workspace design's `gitio` |
| `tree.py` | not settled: the draft has both `trees.py` and `tree.py`; the nearest concept is three-way merge of trees | new; the workspace design's three-way tree merge (candidate) |
| `shell.py` | bash and python inside a sandbox, every command under a timeout | `lion1/hub/tools/shell.py`; `lionagi/tools/code/bash.py` |
| `backend/` | sandbox backends the actions run on; see [backend/README.md](backend/README.md) | `lionagi/tools/sandbox_backend.py` |

## Who uses it

No caller today. It will be offered as tools through `lionhub.tools`, and the coding engine under `lionhub/src/lionhub/orchestration/engines/` is the expected consumer.

## Status

New code, written as its own change after the tools move; it is not part of any move.
