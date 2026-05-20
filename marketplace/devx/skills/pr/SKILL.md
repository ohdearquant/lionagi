---
name: pr
description: >
  GitHub PR creation with auto-push and conventional titles. Suggest when:
  "create PR", "open PR", "push and PR", "submit for review", or branch is
  ready for review.
allowed-tools: [Bash, Read, Glob, Grep]
---

# khive pr

Streamlined PR creation. Push branch, create PR, set metadata — one step.

## When to Use

- User says "create PR", "open PR", "submit PR"
- After a feature branch is ready
- User says "pr" with optional args

## Workflow

### 1. Read Config

Check `.khive/pr.toml`:

```toml
# .khive/pr.toml (optional)
default_base_branch = "main"
default_to_draft = false
default_reviewers = []
default_assignees = []
default_labels = []
auto_push_branch = true
```

### 2. Assess State

```bash
git branch --show-current
git log main..HEAD --oneline
git diff main..HEAD --stat
```

- Ensure we're not on main/master
- Check if branch has commits ahead of base

### 3. Push Branch

If `auto_push_branch = true` (default):
```bash
git push -u origin <current-branch>
```

### 4. Check for Existing PR

```bash
gh pr list --head <current-branch> --json number,url,state
```

If PR exists, report it and optionally open in browser.

### 5. Create PR

Infer title from last conventional commit if not provided:
```bash
gh pr create \
  --title "<inferred or provided title>" \
  --body "$(cat <<'EOF'
## Summary
<bullet points summarizing all commits>

## Test plan
- [ ] Tests pass
- [ ] Linting clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" \
  --base <base_branch>
```

Add metadata if configured:
```bash
gh pr edit <number> --add-reviewer user1 --add-label "feature"
```

### 6. Report

Output PR URL, number, and status.

## Important Rules

- NEVER create PR from main/master
- ALWAYS push branch before creating PR
- Infer title from conventional commits when possible
- Use `gh` CLI (must be authenticated)
- Check for existing PR before creating a new one
