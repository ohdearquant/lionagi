# Debug Research Protocol

Detailed methodology for each phase of the debug workflow.

## Phase 1: Research First

**Start with project context** — check project notes and prior session runs:

```bash
# Check recent session notes
ls ~/.lionagi/runs/ | sort -r | head -5

# Search for prior encounters with this error
grep -r "error_keyword" ~/.lionagi/runs/ --include="*.json" -l 2>/dev/null | head -5
```

If a matching debug pattern is found, apply the known fix directly. Skip to Phase 4.

If nothing relevant is found, proceed with web search and codebase exploration:

```bash
# Search the codebase for related patterns
grep -r "error_keyword" /path/to/project/src/ --include="*.py" -n
```

Or spawn a researcher via `li agent`:

```bash
li agent --prompt "Research this error: [paste error]. Find root cause and solutions for [tool/library version]."
```

**Examples of good queries**:
- `"Python ImportError cannot import name X from partially initialized module circular import"`
- `"pytest fixture 'tmp_path' not found conftest.py scope mismatch"`
- `"Node.js ERR_REQUIRE_ESM require() of ES module not supported"`
- `"uv run ModuleNotFoundError package installed but not found in virtual environment"`

**Bad queries**:
- `"ImportError"` (too vague)
- `"tests don't work"` (no specifics)

**Gate**: Run 2-3 focused research queries before attempting any fix.

## Phase 2: Orchestrate Agents

If research doesn't yield a clear solution, spawn parallel diagnostic agents via `li o fanout`:

```bash
li o fanout \
  --prompt "Diagnose: [error]. Codebase: [path]. Find root cause and propose fix." \
  --workers 2
```

Or spawn a single focused analyst:

```bash
li agent --prompt "
Context: [paste relevant error messages and code]

Research findings so far:
- [finding 1]
- [finding 2]

Analyze:
1. What is the root cause?
2. What are possible solutions?
3. What are the tradeoffs?
"
```

### Agent Selection Table

| Problem Type             | Approach                          |
|--------------------------|-----------------------------------|
| Unknown error root cause | `li agent` with analyst role      |
| Need more information    | `li agent` with researcher role   |
| Parallel hypothesis test | `li o fanout` with 2-3 workers    |
| Implementation approach  | `li agent` with implementer role  |
| Verify proposed solution | `li agent` with tester/critic role|

**Gate**: Agent must produce actionable insight, not just restate the problem.

## Phase 3: Escalate if Stuck

If still stuck after Phase 1 and 2, generate a consultation request:

```markdown
## Consultation Request: [Problem Title]

### Context
- Project: [name]
- Tool versions: [list relevant versions]
- Goal: [what we're trying to achieve]

### Problem Statement
[Clear description of the issue]

### Error Output
```
[exact error messages]
```

### Research Conducted
1. [Research query 1] → [Finding]
2. [Research query 2] → [Finding]
3. [Agent analysis] → [Conclusion]

### Attempted Solutions
1. [Attempt 1] → [Result]
2. [Attempt 2] → [Result]

### Hypothesis
Based on research, we believe [hypothesis]. This could be verified by [method].
```

**Gate**: Consultation request must demonstrate exhaustive research before escalating.

## Phase 4: Document the Solution

Once solved, write a brief note so the fix is findable in future sessions:

```bash
# Append to project debug log
cat >> ./notes/debug-log.md << 'EOF'
## [Date] — [Problem Title]
- **Root cause**: [cause]
- **Fix**: [solution]
- **Context**: [tool/library/version]
EOF
```

For lionagi runs, the session transcript in `~/.lionagi/runs/{run_id}/` already captures the
resolution — no extra step needed if the agent solved it during a `li agent` session.
