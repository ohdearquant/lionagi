# `SandboxSession`

```python
from lionagi.tools.sandbox import (
    create_sandbox,
    sandbox_diff,
    sandbox_commit,
    sandbox_merge,
    sandbox_discard,
)
```

Source: `lionagi/tools/sandbox.py`

`SandboxSession` wraps a git worktree for isolated, reversible code changes. An agent edits
files inside the worktree branch; those changes never touch the base branch until an explicit
`sandbox_merge()`. Discarding the sandbox removes the worktree and branch with no trace.

Why worktrees instead of temp dirs:

- The agent sees the real repo (shared git objects, same file history) — not a copy.
- Changes are a proper git branch: reviewable with `git diff`, mergeable with `git merge --no-ff`.
- `sandbox_discard()` removes both the worktree and branch atomically.

---

## `SandboxSession`

```python
@dataclass
class SandboxSession
```

Returned by `create_sandbox()`. Do not construct directly.

### Fields

| Field | Type | Notes |
|-------|------|-------|
| `worktree_path` | `str` | Absolute path to the worktree directory (`<repo>/.worktrees/<branch_name>/`) |
| `branch_name` | `str` | Name of the sandbox git branch |
| `base_branch` | `str` | Branch the sandbox was forked from |
| `repo_root` | `str` | Absolute path to the repository root |
| `is_active` | `bool` | `True` until `sandbox_merge()` or `sandbox_discard()` completes |

---

## Functions

### `create_sandbox()`

```python
async def create_sandbox(
    repo_root: str,
    base_branch: str | None = None,
    name: str | None = None,
) -> SandboxSession
```

Create a git worktree at `<repo_root>/.worktrees/<name>/` on a new branch forked from
`base_branch`.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `repo_root` | `str` | — | Absolute path to the git repository root |
| `base_branch` | `str \| None` | `None` | Branch to fork from; defaults to current HEAD branch |
| `name` | `str \| None` | `None` | Branch/directory name; auto-generated (`sandbox-<8hex>`) if `None` |

Returns a `SandboxSession`. Raises `RuntimeError` if `git worktree add` fails.

```python
session = await create_sandbox("/Users/me/project")
# session.worktree_path → "/Users/me/project/.worktrees/sandbox-a1b2c3d4"
# session.branch_name   → "sandbox-a1b2c3d4"
# session.base_branch   → "main"
```

---

### `sandbox_diff()`

```python
async def sandbox_diff(session: SandboxSession) -> dict
```

Stage all changes in the worktree (`git add -A`) and return a diff summary.

Returns a dict:

| Key | Type | Notes |
|-----|------|-------|
| `files_changed` | `list[str]` | Relative paths of changed files |
| `stat` | `str` | `git diff --cached --stat` output |
| `patch` | `str` | Unified diff patch (truncated to 10 000 chars if larger) |
| `patch_truncated` | `bool` | `True` if the patch was truncated |
| `full_patch_chars` | `int` | Total patch length in characters before truncation |

```python
diff = await sandbox_diff(session)
print(diff["stat"])
# output:
#  auth/session.py | 42 +++++-----
#  auth/utils.py   |  8 +-
#  2 files changed, 28 insertions(+), 22 deletions(-)
```

---

### `sandbox_commit()`

```python
async def sandbox_commit(session: SandboxSession, message: str) -> dict
```

Stage all changes (`git add -A`) and commit them inside the worktree branch.

| Param | Type | Notes |
|-------|------|-------|
| `session` | `SandboxSession` | Active sandbox session |
| `message` | `str` | Commit message |

Returns a dict:

| Key | Type | Notes |
|-----|------|-------|
| `success` | `bool` | `True` on commit or when there is nothing to commit |
| `commit` | `str` | SHA of the new commit (only when a commit was made) |
| `message` | `str` | Commit message or `"Nothing to commit"` |
| `error` | `str` | Error detail (only when `success=False`) |

```python
result = await sandbox_commit(session, "refactor: split auth into separate module")
# result → {"success": True, "commit": "a1b2c3d...", "message": "refactor: ..."}
```

---

### `sandbox_merge()`

```python
async def sandbox_merge(session: SandboxSession) -> dict
```

Stage and commit any remaining changes, then merge the sandbox branch into `base_branch` via
`git merge --no-ff`. Cleans up the worktree and branch on success.

Returns a dict:

| Key | Type | Notes |
|-----|------|-------|
| `success` | `bool` | `True` if the merge completed without conflicts |
| `merged` | `bool` | `True` when successful |
| `worktree_removed` | `bool` | Whether the worktree directory was removed |
| `branch_deleted` | `bool` | Whether the sandbox branch was deleted |
| `errors` | `list[str]` | Any non-fatal errors from cleanup steps |
| `error` | `str` | Merge conflict detail (only when `success=False`) |

```python
result = await sandbox_merge(session)
if not result["success"]:
    print("Merge conflict:", result["error"])
```

After a successful merge, `session.is_active` should be treated as `False` — the worktree
no longer exists.

---

### `sandbox_discard()`

```python
async def sandbox_discard(session: SandboxSession) -> dict
```

Remove the worktree and delete the sandbox branch. All changes are discarded; the base branch
is unchanged.

Returns a dict:

| Key | Type | Notes |
|-----|------|-------|
| `worktree_removed` | `bool` | Whether the worktree directory was removed |
| `branch_deleted` | `bool` | Whether the sandbox branch was deleted |
| `errors` | `list[str]` | Any non-fatal errors during cleanup |

```python
await sandbox_discard(session)
# worktree and branch are gone; base branch is untouched
```

---

## Full lifecycle example

```python
from lionagi.agent import AgentSpec, create_agent
from lionagi.tools.sandbox import (
    create_sandbox,
    sandbox_diff,
    sandbox_commit,
    sandbox_merge,
    sandbox_discard,
)

# 1. Create sandbox forked from current branch
session = await create_sandbox("/Users/me/project")

# 2. Run an agent confined to the worktree
spec = AgentSpec.coding(cwd=session.worktree_path)
branch = await create_agent(spec)
await branch.chat("Refactor the auth module into separate files")

# 3. Review changes
diff = await sandbox_diff(session)
print(diff["stat"])
print(f"Changed files: {diff['files_changed']}")

# 4a. Accept — commit and merge back
await sandbox_commit(session, "refactor: split auth module")
result = await sandbox_merge(session)

# 4b. Reject — discard all changes, no trace
# await sandbox_discard(session)
```

---

Next: [`AgentSpec` and `create_agent()`](agent-config.md)
