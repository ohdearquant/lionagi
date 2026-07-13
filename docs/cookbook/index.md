# Cookbook

Runnable scenarios. Each ends with a `Next:` pointer to the next one.

| Scenario | Command | What it covers |
|----------|---------|----------------|
| [Codebase audit](codebase-audit.md) | `li o fanout` | Parallel workers, independent findings |
| [Research synthesis](research-synthesis.md) | `li o fanout --with-synthesis` | Fan-out + consolidation |
| [Multi-model pipeline](multi-model-pipeline.md) | `li o flow` | DAG with dependency edges |
| [Team coordination](team-coordination.md) | `li team` + `--team-attach` | Mid-flow messaging |
| [Resumable background](resumable-background.md) | `li o flow --background` | Detach, monitor, resume |

Next: [Codebase audit](codebase-audit.md) — the simplest pattern.
