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
`sandbox_merge()`. Discarding the sandbox removes the worktree and branch with no trace
(on success — see the partial-cleanup notes below for what a failed step reports).

Why worktrees instead of temp dirs:

- The agent sees the real repo (shared git objects, same file history) — not a copy.
- Changes are a proper git branch: reviewable with `git diff`, mergeable with `git merge --no-ff`.
- `sandbox_discard()` removes both the worktree and branch. The two steps are
  independent and may fail individually (e.g. a locked worktree); the result
  reports each step's outcome, and the session stays active on partial failure.

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
| `is_active` | `bool` | `True` until `sandbox_merge()` or `sandbox_discard()` completes both of their cleanup steps |
| `base_sha` | `str` | Commit SHA `base_branch` pointed at when the sandbox was created |

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
`base_branch`. Records `base_sha` (the commit `base_branch` pointed at) at creation time.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `repo_root` | `str` | — | Absolute path to the git repository root |
| `base_branch` | `str \| None` | `None` | Branch to fork from; defaults to the branch currently checked out at `repo_root` |
| `name` | `str \| None` | `None` | Branch/directory name; auto-generated (`sandbox-<8hex>`) if `None` |

Returns a `SandboxSession`. Raises `RuntimeError` if `repo_root` is in a detached HEAD state
(there is no branch name to fork from or merge back into) or if `git worktree add` fails.

```python
session = await create_sandbox("/path/to/project")
# session.worktree_path → "/path/to/project/.worktrees/sandbox-a1b2c3d4"
# session.branch_name   → "sandbox-a1b2c3d4"
# session.base_branch   → "main"
# session.base_sha      → "a1b2c3d4e5f6..." (40-char SHA of main's tip at creation)
```

---

### `sandbox_diff()`

```python
async def sandbox_diff(session: SandboxSession) -> dict
```

Read a diff summary of everything changed in the worktree — **without staging or otherwise
mutating the index**. Tracked changes come from `git diff HEAD`; untracked files (including
files inside untracked directories) are enumerated with `git ls-files --others
--exclude-standard -z` and each diffed individually against `/dev/null` via `git diff
--no-index`. The worktree's index is left exactly as the caller left it — calling `sandbox_diff`
never stages anything, so it's safe to call repeatedly while iterating.

Returns a dict:

| Key | Type | Notes |
|-----|------|-------|
| `files_changed` | `list[str]` | Relative paths of changed files (tracked + untracked, including nested untracked files) |
| `stat` | `str` | Combined `git diff HEAD --stat` plus per-file untracked stats |
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
async def sandbox_merge(session: SandboxSession, *, allow_protected: bool = False) -> dict
```

Stage and commit any remaining changes, then merge the sandbox branch into `base_branch` via
`git merge --no-ff`. Cleans up the worktree and branch on success.

Before merging, `sandbox_merge` refuses (returns `{"success": False, "error": ...}`, never
raises) in three cases:

1. **`repo_root` is in a detached HEAD state.** There is no branch ref for the merge to land
   on, so a `HEAD == HEAD` comparison would otherwise merge into a detached commit with nothing
   pointing at the result afterward.
2. **`repo_root` isn't checked out on the sandbox's recorded `base_branch`.** No auto-checkout —
   the caller must be on the exact branch the sandbox was forked from.
3. **`base_branch` is a protected name** (`main`, `master`, or anything starting with
   `release`) **and `allow_protected` wasn't passed.** Pass `allow_protected=True` to merge into
   it explicitly.

Returns a dict:

| Key | Type | Notes |
|-----|------|-------|
| `success` | `bool` | `True` if the merge completed without conflicts |
| `merged` | `bool` | `True` when successful |
| `worktree_removed` | `bool` | Whether the worktree directory was removed |
| `branch_deleted` | `bool` | Whether the sandbox branch was deleted |
| `errors` | `list[str]` | Any non-fatal errors from cleanup steps |
| `error` | `str` | Refusal or merge-conflict detail (only when `success=False`) |

```python
result = await sandbox_merge(session)
if not result["success"]:
    print("Merge refused or failed:", result["error"])

# Merging into a protected branch (main/master/release*) needs an explicit opt-in:
result = await sandbox_merge(session, allow_protected=True)
```

`session.is_active` is only flipped to `False` once **both** the worktree removal and the
branch deletion actually succeed. On a partial cleanup failure (e.g. a locked worktree),
`is_active` stays `True` and `worktree_removed`/`branch_deleted` report the real per-step
outcome — treat a merge result with `success=True` but `worktree_removed=False` or
`branch_deleted=False` as "merged, cleanup incomplete," not fully done.

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

Worktree removal and branch deletion are independent steps and either can fail on its own (a
locked worktree blocks removal; a branch checked out elsewhere blocks deletion). `is_active`
only becomes `False` once **both** succeed — check `worktree_removed`/`branch_deleted`
individually rather than assuming the call always fully cleans up. On the `CodingToolkit`
sandbox tool facade (below), a partial failure here is surfaced as `success: False` and the
tool keeps its internal session handle so a caller can inspect or retry instead of losing
track of the sandbox.

Retries are state-aware: a resource that is already absent (e.g. the worktree was removed by
an earlier partial attempt) counts as cleaned up, so a later `sandbox_discard()` completes the
remaining step and releases the session instead of failing forever on the step that already
succeeded. `sandbox_diff()` raises and `sandbox_commit()` returns an error when the session's
worktree no longer exists, rather than reporting an empty diff as success.

The `CodingToolkit` sandbox tool's `merge` action always calls `sandbox_merge` with the
toolkit's own `sandbox_allow_protected` setting — the LLM-facing request schema has no
`allow_protected` field, so an agent can never opt itself into merging into a protected
branch. `sandbox_allow_protected` is a `CodingToolkit(...)` constructor argument (default
`False`): an operator-level trust decision made by whoever composes the agent in code, e.g.
`CodingToolkit(workspace_root=repo, tools=["sandbox"], sandbox_allow_protected=True)` for a
job that's always meant to merge into `main`.

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
session = await create_sandbox("/path/to/project")

# 2. Run an agent confined to the worktree
spec = AgentSpec.coding(cwd=session.worktree_path)
branch = await create_agent(spec)
await branch.chat("Refactor the auth module into separate files")

# 3. Review changes
diff = await sandbox_diff(session)
print(diff["stat"])
print(f"Changed files: {diff['files_changed']}")

# 4a. Accept — commit and merge back (base_branch here is "main", a protected
#     name, so the merge needs an explicit opt-in)
await sandbox_commit(session, "refactor: split auth module")
result = await sandbox_merge(session, allow_protected=True)

# 4b. Reject — discard all changes, no trace
# await sandbox_discard(session)
```

---

Next: [`AgentSpec` and `create_agent()`](agent-config.md)
