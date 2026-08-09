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

Or spawn a researcher through the plugin's MCP server, `mcp__plugin_orchestrate_lion__request`.
Ask what it can run first — a catalog request (`help`) is a separate call from any request
that carries `ops`:

```json
{"help": true}
```

That catalog is the authority on what exists; the orchestration verbs used here are
`agent.submit`, `flow.submit`, `fanout.submit`, `play.submit`, `job.status`, `job.output`,
`job.list`, `job.wait`, `job.kill`, `profile.list` and `profile.show`. Submit the research
task with `agent.submit`. Agent profile names are installation-specific, so call
`profile.list` before setting `agent`, or identify a model through the positional `query`
argument as this example does. Every call to the tool carries `ops` as an array of
`{op, args}` objects, and every `*.submit` op additionally carries the
`schema_fingerprint` its `help` reply returned, as a **sibling of `args`** rather than a key
inside it. Without it the op is refused with `stale_schema` and no run starts; put it inside
`args` and it is not read at all, so the same refusal repeats and the failure looks
idempotent:

```json
{
  "ops": [
    {
      "op": "agent.submit",
      "args": {
        "prompt": "Research this error: [paste error]. Find root cause and solutions for [tool/library version].",
        "query": ["claude"]
      },
      "schema_fingerprint": "<from help='agent.submit'>"
    }
  ]
}
```

If you're working inside a lionagi checkout, the CLI equivalent is:

```bash
li agent claude \
  --prompt "Research this error: [paste error]. Find root cause and solutions for [tool/library version]."
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

If research doesn't yield a clear solution, spawn parallel diagnostic agents with
`fanout.submit`:

```json
{
  "ops": [
    {
      "op": "fanout.submit",
      "args": {
        "query": ["claude"],
        "prompt": "Diagnose: [error]. Codebase: [path]. Find root cause and propose fix.",
        "num_workers": 2
      },
      "schema_fingerprint": "<from help='fanout.submit'>"
    }
  ]
}
```

Or spawn a single focused analyst with `agent.submit`:

```json
{
  "ops": [
    {
      "op": "agent.submit",
      "args": {
        "prompt": "Context: [paste relevant error messages and code]\n\nResearch findings so far:\n- [finding 1]\n- [finding 2]\n\nAnalyze:\n1. What is the root cause?\n2. What are possible solutions?\n3. What are the tradeoffs?",
        "query": ["claude"]
      },
      "schema_fingerprint": "<from help='agent.submit'>"
    }
  ]
}
```

Track a submitted job with `job.wait` (call targeted help if you need a job verb's exact
schema — `job.wait` takes a list):

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<id returned by the submit call above>"]}}]}
```

Check the op's `ok` and the result's `all_terminal`; repeat while the run is pending. Then
read the result with `job.output`:

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<id returned by the submit call above>"}}]}
```

If you're working inside a lionagi checkout, the CLI equivalents are `li o fanout` and
`li agent`:

```bash
li o fanout claude -n 2 \
  "Diagnose: [error]. Codebase: [path]. Find root cause and propose fix."
```

The prompt is positional and `-n` sets the worker count. `--workers` is a different flag: it
takes a comma-separated list of model specs, so `--workers 2` would ask for a model named `2`.

```bash
li agent claude --prompt "
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

Use `profile.list` to discover which named profiles the server can resolve. If a suitable
profile exists, pass its returned name as `agent`; otherwise pass a model as the first entry
in `query`. Inside a checkout, the equivalent profile form is `li agent -a <profile-name>`.

| Problem Type             | MCP call                                                   | CLI equivalent (lionagi checkout only) |
|--------------------------|------------------------------------------------------------|-----------------------------------------|
| Unknown error root cause | `agent.submit` with a discovered analysis profile or model | `li agent -a <profile-name>` or `li agent <model>` |
| Need more information    | `agent.submit` with a discovered research profile or model | `li agent -a <profile-name>` or `li agent <model>` |
| Parallel hypothesis test | `fanout.submit` with `num_workers: 2-3`                    | `li o fanout` with 2-3 workers |
| Implementation approach  | `agent.submit` with a discovered coding profile or model   | `li agent -a <profile-name>` or `li agent <model>` |
| Verify proposed solution | `agent.submit` with a discovered review profile or model   | `li agent -a <profile-name>` or `li agent <model>` |

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

The run this fix came from is already recorded: `job.output` returns its console tail and
artifact list by run id (see `help=true` for the exact argument name). Inside a lionagi
checkout, the persisted run record and artifacts also sit under
`~/.lionagi/runs/{run_id}/`.
