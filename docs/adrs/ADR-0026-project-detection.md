# ADR-0026: Project Detection for Session Organization

**Status**: Accepted
**Date**: 2026-05-23

## Context

Lion Studio's Runs page lists all sessions in a flat reverse-chronological list. As the session
count grows across multiple repositories (lionagi, ag2, khive-oss, khive-cloud, etc.), the list
becomes unusable — there is no way to scope sessions to a specific project.

The triggering need: Ocean works across 5–10 repos daily and wants sessions grouped by named
project in Studio, with GitHub integration potential. The question is how to detect which
project a session belongs to at creation time.

Two design patterns were evaluated by agent deliberation:

1. **Centralized registry** (`~/.lionagi/projects.yaml`): a single file mapping project names to
   git remotes and directory paths. Detection checks remotes/paths against the registry.
2. **Per-repo config** (`.lionagi/config.toml` in each repo): each repo declares its own project
   identity. Detection walks up from cwd to find the config file.

## Decision

Per-repo config with global fallback. Three new artifacts:

### 1. `.lionagi/config.toml` (committed, per-repo)

Static identity declaration — safe to commit, no secrets. Separate from `.lionagi/settings.yaml`
which may contain hooks, model keys, and sensitive local config.

```toml
[project]
name = "lionagi"
github = "ohdearquant/lionagi"
```

### 2. Global fallback in `~/.lionagi/settings.yaml`

For repos the user doesn't control (e.g., a fork of ag2 where committing `.lionagi/config.toml`
is not practical):

```yaml
project_overrides:
  "ag2ai/ag2": "ag2"
  "/Users/lion/forks/ag2": "ag2"
```

Keys can be `org/repo` (matched against git remote) or absolute directory paths (prefix-matched
against cwd).

### 3. Detection cascade (at session creation)

1. Walk up from cwd to git root → read `.lionagi/config.toml` → use `[project].name`
2. If absent → load global `~/.lionagi/settings.yaml` → check `project_overrides` for git
   remote match (`org/repo`), then cwd prefix match
3. If absent → parse git remote URL → derive `org/repo` as fallback project name
4. If non-git → `null` (shown as "Unassigned" in Studio)

### 4. Schema changes

Two new columns on `sessions`:
- `project TEXT` — the resolved project name
- `project_source TEXT` — detection method: `config_toml`, `global_override`, `git_remote`, or `null`

`project_source` lets Studio surface detection confidence and flag low-confidence assignments.

### 5. Implementation module

New module `lionagi/cli/_project.py`:
- `detect_project(cwd: Path | None = None) -> tuple[str | None, str | None]`
  Returns `(project_name, project_source)`.
- Called by all three `create_session` sites: `cli/agent.py`, `cli/orchestrate/_orchestration.py`,
  and `cli/state.py` (import path).

## Consequences

**Positive**
- Fresh clone of any owned repo auto-detects project (zero config if `.lionagi/config.toml` committed)
- Blast radius of missing config bounded to one repo, not global
- Monorepos handled naturally via nested `.lionagi/config.toml` at subdirectory level
- Aligns with existing settings merge pattern (project-level files override global)
- `project_source` column enables Studio to distinguish high-confidence from inferred labels

**Negative**
- Repos the user doesn't control require manual `project_overrides` in global settings
- Non-git directories always land in "Unassigned" — no auto-detection path
- New config file (`.lionagi/config.toml`) alongside existing `.lionagi/settings.yaml` —
  two files in `.lionagi/` with different commit policies

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Centralized `~/.lionagi/projects.yaml` | Global registry drifts from reality; git remote renames cause silent mis-categorization; all projects break if file missing on fresh machine; new infrastructure with no precedent in codebase |
| `pyproject.toml [tool.lionagi]` | Can't add to repos the user doesn't control; pollutes upstream repos |
| Extend `.lionagi/settings.yaml` with `project` key | settings.yaml is gitignored/local (may contain secrets); project identity should travel with the repo |

## References

- Agent deliberation: 3×suggester (Simplify, Risk, System perspectives)
- Existing settings merge: `lionagi/agent/settings.py` `load_settings()`
- Session creation sites: `cli/agent.py:292`, `cli/orchestrate/_orchestration.py:558`, `cli/state.py:237`
