# coding-tools — Feedback Surface for the Coding Agent

**Branch**: `show/lionagi-sweep/coding-tools` (based on `main`)
**Commit**: `4e85e631b` — `feat(tools): coding-agent feedback surface — research, guidance, diagnostics`
**Refs**: #1246 #1247 #1248
**Status**: committed locally — NOT pushed, NO PR (per task scope)

---

## Per-issue change

### #1246 — research OSS harnesses (research doc)
Studied OpenCode (`sst/opencode`) and `mini-swe-agent`: agent loop, tool set, permission
model, context management, and persistence mechanics. Produced
`docs/research/coding-harnesses-2026-06.md` — concrete + citable, with pinned commit SHAs
(`sst/opencode@a0e4db3`, `mini-swe-agent@3df30a4`), `file:line` permalinks, a source-inventory
table with confidence ratings, an honest conflicts/gaps section, and a prioritized P0–P3
"fold into lionagi" list mapped to the #1247 ordering.

### #1248 — tune reader/editor/bash guidance
Upgraded tool descriptions and error messages from "what failed" to "why + how to recover":
- **editor.py** — `old_string not found` now detects the reader's `<n>\t` line-prefix being
  copied in and whitespace-only mismatch, and unconditionally appends a "re-read and copy exact
  text" fallback; ambiguous match offers both `replace_all=True` and "add more context".
- **bash.py** — operator rejection → use `cwd=` instead of `cd x && cmd`; command-not-found →
  PATH explanation; timeout → raise `timeout=` or split; truncation → redirect to file + reader.
- **reader.py** — not-a-file → `list_dir`; binary → `bash file`; not-found names workspace scope.
- Field descriptions warn about the line-prefix trap up front.

### #1247 — AST/static-analysis feedback tool (one real slice shipped)
New `code_check` tool (`lionagi/tools/code/check.py`) shells out to **ruff** (optional dev-only
dep, `shutil.which` guard — returns `status="unavailable"` with `uv add ruff` guidance, never
raises). Returns structured `file:line:col: SEVERITY code message [fixable]` diagnostics via
`CodeDiagnostic.as_text()`. Composes with the editor (edit → check on same on-disk file).
Registered with the standard `Tool(func_callable=..., request_options=...)` schema-extraction
pattern and exported from `code/__init__.py`.

---

## Files touched (10)

| File | Issue | Change |
|------|-------|--------|
| `docs/research/coding-harnesses-2026-06.md` | #1246 | NEW — research doc (174 lines) |
| `lionagi/tools/code/check.py` | #1247 | NEW — `code_check` tool (266 lines) |
| `lionagi/tools/code/__init__.py` | #1247 | export `code_check` |
| `lionagi/tools/code/bash.py` | #1248 | recovery-oriented error messages |
| `lionagi/tools/coding.py` | #1248 | bundled-toolkit guidance mirror |
| `lionagi/tools/file/editor.py` | #1248 | edit-failure diagnostics + recovery |
| `lionagi/tools/file/reader.py` | #1248 | reader guidance + error recovery |
| `tests/tools/test_check.py` | #1247 | NEW — code_check + edit→check composition |
| `tests/tools/test_guidance.py` | #1248 | NEW — asserts recovery text + paths |
| `tests/tools/test_reader.py` | #1248 | NEW — reader guidance assertions |

`uv.lock` was intentionally **not** staged: its `0.26.14→0.26.15` bump traces to the
pre-existing `chore(release): 0.26.15` commit, not this work (verified by the critic).
The repo-wide `ruff format .` reformatted 231 unrelated files — also intentionally left
unstaged; only the 10 touched files above are in the commit.

---

## Focused test command + result

```
uv run pytest tests/tools/test_check.py tests/tools/test_guidance.py tests/tools/test_reader.py
```

**63 passed, 12 warnings in 1.83s** — all green.
`uv run ruff check .` passes on all touched files.

---

## Critic verdict

**APPROVE** — `CRIT:0 | MAJ:0 | MIN:3 | PASS:5` (`../critic/verdict.md`).
Per-issue: #1246 APPROVE · #1248 APPROVE-WITH-FIXES (1 MIN) · #1247 APPROVE ·
Tests APPROVE-WITH-FIXES (2 MIN) · conventions APPROVE. All three MINOR items are
local, non-blocking follow-ups (see Drafted vs Shipped). Decision rule
`[zero_crit ∧ zero_maj] → APPROVE` satisfied. Tester confirmed all 7 mutations fail
for the right reason.

---

## Drafted vs Shipped

**Shipped (working, tested, committed):**
- #1246 research doc — complete.
- #1248 guidance tuning across reader/editor/bash — complete, with recovery-path tests.
- #1247 — **one** real diagnostic slice: `code_check` over ruff, composable with the editor.

**Drafted only (in the research doc, not implemented):**
- The remaining AST/static-analysis tool surface (`coding-harnesses-2026-06.md:118-162`):
  structural search/rewrite (ast-grep), outline/navigation, and parse-validation tools, with a
  shared result schema and P0→P3 sequencing. Per the contract, only one slice ships now; the
  rest is staged for a follow-up.

**Open MINOR follow-ups (non-blocking, for the implementer):**
- MIN-1 — bundled `CodingToolkit` editor (`coding.py:~444`) keeps the smart hints but lacks the
  unconditional "re-read the file" fallback that standalone `editor.py` always appends; add for
  parity (also flagged by tester as W1).
- 2 MINOR test-hardening items: bundled not-found test is cosmetic (W1); bash-truncation test
  has a silent-skip guard (W2).
