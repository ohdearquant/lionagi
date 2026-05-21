---
name: status
description: >
  Dashboard for all sub-lambdas: task queues, build health, blockers, idle/stuck state.
  Suggest when: "status", "dashboard", "what's happening", "who's free", "who's blocked",
  or before delegating work.
allowed-tools: [Bash, Read, Glob, Grep, mcp__khive__next, mcp__khive__list, mcp__khive__inbox]
---

# Status — Sub-Lambda Dashboard

Overview of all 5 sub-lambdas: health, tasks, activity, blockers.

## When to Use

- Starting a session as λ:khive to understand current state
- Before delegating work — who's idle, who's blocked
- "status", "dashboard", "what's happening", "who's free"
- After completing a cross-cutting task to verify all layers are green

## Workflow

### 1. Task Queue Status

```python
mcp__khive__next(assignee="lambda:foundation", limit=10)
mcp__khive__next(assignee="lambda:platform", limit=10)
mcp__khive__next(assignee="lambda:features", limit=10)
mcp__khive__next(assignee="lambda:apps", limit=10)
mcp__khive__next(assignee="lambda:products", limit=10)

# Unassigned tasks (need delegation)
mcp__khive__list(type="work", status="inbox", limit=10)
mcp__khive__list(type="work", status="next", limit=10)
```

### 2. Build Health

```bash
# Quick workspace compile check
cargo check --workspace 2>&1 | tail -5

# Count warnings per layer
cargo check --workspace 2>&1 | grep "warning:" | \
  sed 's/.*--> //' | cut -d/ -f1 | sort | uniq -c | sort -rn
```

### 3. Recent Git Activity

```bash
# What changed recently, by layer
git log --oneline -10 --stat | head -40

# Changes per layer in last N commits
echo "=== Recent changes by layer ==="
git log --oneline -20 -- foundation/ | wc -l | xargs echo "foundation:"
git log --oneline -20 -- platform/ | wc -l | xargs echo "platform:"
git log --oneline -20 -- features/ | wc -l | xargs echo "features:"
git log --oneline -20 -- apps/ | wc -l | xargs echo "apps:"
git log --oneline -20 -- products/ | wc -l | xargs echo "products:"
```

### 4. Test Health (optional, slower)

```bash
# Quick test counts per layer
cargo test --workspace --no-run 2>&1 | grep "Compiling\|test result" | tail -20
```

### 5. Inbox & Discussions

```python
# Check for unread messages
mcp__khive__inbox(status="unread", limit=20)

# Active discussions
mcp__khive__list(type="comm", channel="forum", limit=20)
```

### 6. Identify Blockers

Look for:
- Tasks stuck in `in_progress` for too long
- Cross-lambda dependencies (feature waiting on platform)
- Build failures blocking downstream work
- Unread inbox messages that might contain blockers

### Report

```
λ:khive Status Dashboard

  BUILD:  cargo check ✓ (15 warnings — 12 in ui, 3 in deploy)

  LAYER STATUS:
  ┌─────────────┬──────────┬────────┬─────────┬──────────┐
  │ Lambda      │ Tasks    │ Active │ Blocked │ Last Δ   │
  ├─────────────┼──────────┼────────┼─────────┼──────────┤
  │ foundation  │ 0 next   │ 0      │ 0       │ 2d ago   │
  │ platform    │ 1 next   │ 0      │ 0       │ 2d ago   │
  │ features    │ 0 next   │ 0      │ 0       │ 2d ago   │
  │ apps        │ 1 next   │ 0      │ 0       │ 2d ago   │
  │ products    │ 0 next   │ 0      │ 0       │ 2d ago   │
  └─────────────┴──────────┴────────┴─────────┴──────────┘

  INBOX: 0 unread
  DISCUSSIONS: 6 topics (0 pending response)

  AVAILABLE: foundation, features (idle, no tasks)
  BLOCKED: none
  ACTION NEEDED: 2 unassigned tasks in inbox
```

## Dashboard Interpretation

| State | Meaning | Action |
|-------|---------|--------|
| Lambda idle, no tasks | Available for new work | dispatch via `li o flow` or assign through `mcp__khive__assign` |
| Lambda has tasks, none active | Work queued but not started | Check priority |
| Lambda active, no blockers | Working normally | Let it run |
| Lambda blocked | Waiting on another lambda | Resolve dependency |
| Build failing | Layer broken | `/blame` + fix |
| Many warnings | Tech debt accumulating | Create cleanup task |

## Important Rules

- Build health is the first thing to check — if it's red, nothing else matters
- Idle lambdas are opportunities, not problems
- Cross-lambda blockers need immediate attention
- Warning counts trend upward if not actively managed
- Run this at the start of every orchestration session
