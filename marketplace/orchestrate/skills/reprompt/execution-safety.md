# Execution Safety

## Blocking Foreground Only — NEVER Background Agents

**FORBIDDEN**:
- `run_in_background: true` for any agent
- Launching agents without waiting for completion
- Fire-and-forget agent patterns

**WHY**: Background agents cause catastrophic coordination failures:
- All agents stepping on each other's work
- Git conflicts everywhere
- Context explosion when checking on all of them

**REQUIRED**: Blocking batches with max foreground limit:

```python
# CORRECT: Blocking batch of max 4 foreground agents
for batch in chunk(agents, max_size=4):
    results = await parallel_blocking(batch)  # WAIT for all to complete
    validate_results(results)                  # GATE before next batch

# WRONG: Fire-and-forget chaos
for agent in agents:
    spawn_background(agent)  # NEVER DO THIS
```

### Max Foreground Batch Size

```
Default: --max-foreground=4
Absolute max: 8 (only with explicit justification)

NEVER launch >8 agents simultaneously
ALWAYS wait for batch completion before next batch
```

## NEVER Read Raw Console Outputs

**FORBIDDEN**: Reading full console outputs from agent tasks

**WHY**: Console outputs can be 4-5k+ lines, causing instant context crash.

**REQUIRED**: Agents must summarize their own outputs:

```kpp
# CORRECT: Agent provides summary
from: α[tester]
to: λ
sts: ok
summary: "47 tests passed, 2 skipped, 0 failed"
quality: {test: 100, lint: ok}

# WRONG: Returning raw console dump (crashes λ's context)
```

**If raw output needed**: Write to workspace file, reference by path only:

```kpp
out:
  - {file: "runlog.txt", desc: "Full test output (2847 lines)", summary: "47 pass, 0 fail"}
```

**If λ MUST inspect output**:

```bash
tail -50 runlog.txt            # Last 50 lines only
grep "FAIL\|ERROR" runlog.txt  # Only failures
grep -c "passed" runlog.txt    # Just count
```

## λ Handles All Git Flows

Git operations are λ's exclusive responsibility:

| Operation             | Who    | Why                                         |
| --------------------- | ------ | ------------------------------------------- |
| `git add`             | λ only | Prevents conflicting staging                |
| `git commit`          | λ only | Ensures atomic commits with proper messages |
| `git push`            | λ only | Prevents race conditions                    |
| `git merge`           | λ only | Requires orchestration context              |
| `git checkout/branch` | λ only | Agents work on assigned scope only          |

**Agents MUST NOT** run any git commands.
**Agents MUST** report modified files in status report, let λ handle git.

```kpp
from: α[implementer]
to: λ
sts: ok
modified: [src/lib.rs, src/types.rs, tests/integration.rs]
ready_for_commit: true
```
