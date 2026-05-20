# Anti-Patterns (Hard Stops)

| Name               | Description                                          | Why It's Bad                              |
| ------------------ | ---------------------------------------------------- | ----------------------------------------- |
| **The Bureaucrat** | 5-phase plan with α[architect]+α[critic] for CSS fix | C < 0.3 doesn't need a village            |
| **The Zombie**     | Execute Phase 2 when Phase 1 failed                  | Stop and pivot, don't cascade failures    |
| **The Coward**     | P_SEQ when P_PAR would save 50% time                 | Be bold when independence is verified     |
| **The Ghost**      | Spawn agents without economic justification          | "Just in case" wastes tokens              |
| **The Vagrant**    | Use raw/generic agents without roles                 | FORBIDDEN — always use roled agents       |
| **The Amnesiac**   | No workspace artifacts, no evidence                  | Future λ can't trace decisions            |
| **The Optimist**   | "Days" as time estimate                              | AI-scale is minutes. Decompose more.      |
| **The Anarchist**  | Launch 30 background agents at once                  | Pure chaos, git conflicts, nothing done   |
| **The Glutton**    | Read 5000-line console output into context           | Instant context crash, orchestration dead |
| **The Rogue**      | Agents running git commands directly                 | Conflicts, race conditions, broken state  |

## Critical Rules

1. **Roled Agents Only**: NEVER spawn generic/raw agents. Always use `α[role]` from roster.
2. **Opus Only**: ALL agents MUST use Opus model. Never Sonnet, never Haiku for agents.
3. **Domain Composition**: Every roled agent MUST compose domains before executing.
4. **Blocking Batches Only**: NEVER use background agents. Max 4 foreground, wait for completion.
5. **λ Owns Git**: Only λ runs git commands. Agents modify files only, report changes.
6. **Protect Context**: NEVER read raw console outputs. Use tail/head/grep, or have agents summarize.
7. **AI-Scale Time**: Estimate in minutes (5m, 15m, 30m). If "days", decompose further.
8. **Sync or Die**: `checklist.md` and task tracking must agree.
9. **Evidence First**: Never mark a gate complete without log, hash, or test result.
10. **Cross-Cut Early**: Check consistency between phases, not just at the end.
11. **Independence Before Parallelism**: Verify before upgrading to P_PAR.
