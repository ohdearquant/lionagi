# PR Review: #1939

## Verdict

REQUEST_CHANGES

The default pack path is correct and the prompt-size increase is modest, but the implementation does not preserve the PR's advertised-subset-of-accepted invariant for all supported custom packs.

## Finding

| Severity | Location | Issue | Suggested fix | Confidence |
|---|---|---|---|---|
| MEDIUM | `lionagi/cli/orchestrate/_orchestration.py:119` | `mode_roster()` renders raw `cfg.modes_allow` entries as modes the role “accepts,” while `resolve_modes()` separately rejects entries that `Mode.load()` does not recognize. `Pack.from_file()` does not validate mode names. A custom `Pack(name="custom", configs={"critic": RoleConfig(modes_allow=("not_a_mode",))})` therefore produces `critic accepts only not_a_mode`, but `resolve_modes("critic", ["not_a_mode"], pack)` returns `[]`. The planner guidance can still lie whenever `env.pack` differs from the default and contains an unknown allowlist entry. | Establish one shared accepted-set invariant: either reject unknown `modes_allow` entries when loading a pack, or render only names recognized by the mode catalog (with explicit wording when a non-empty allowlist admits no valid modes). Add a custom-pack regression test that derives every advertised entry and verifies `resolve_modes(role, [mode], same_pack) == [mode]`. | High |

## Scope checks

- Reviewed all 3 changed files and the complete one-commit diff against `main` (`f2935a2..5bae3b2`). `git diff --check` passed.
- `flow.py:1569` is the only production call site of `mode_roster`; the other references are its definition and the two new tests. The new optional parameter is backward-compatible, and `None` still resolves through `role_config()` to the packaged default pack.
- For flow execution, planner guidance and enforcement receive the identical `env.pack` object: `mode_roster(env.pack)` at `flow.py:1569` and `resolve_modes(..., env.pack)` at `flow.py:1636`, `flow.py:1651`, and `flow.py:1654`. For valid mode names, all 7 allowlisted entries in the current default pack were advertised and accepted.
- The custom-pack failure above comes from a second enforcement step, mode existence validation, not from selecting different pack objects.

## Prompt size

With the full current default pack, only `analyst` and `critic` are restricted. The new restrictions clause is 209 characters / approximately 43 `cl100k_base` tokens. The full mode roster is 565 characters / approximately 121 tokens, and the new clause is about 3.5% of the combined role-plus-mode guidance by character count. This is reasonable for planner guidance.

## Tests and gates

- `uv run pytest tests/cli/orchestrate/ -q`: passed (exit 0; 284 tests collected).
- `uv run ruff check` on all 3 changed files: passed.
- `uv run ruff format --check` on all 3 changed files: passed.
- GitHub lint, pyright, docs, frontend, Studio, marketplace, VS Code, benchmark, and Vercel checks passed at review time; Python 3.10 and 3.14 jobs were still pending.
- The new tests do not actually pin the stated coherence property. `test_surfaces_role_allowlists` checks rendered text only for `analyst`, while `test_allowlists_match_enforcement` checks execution only for `critic` and never calls `mode_roster()` or passes a custom pack. They would fail on a future removal/rename of `analyst` or `critic`, but they can pass when another restricted role is omitted, when the new `pack` argument is ignored, or under the demonstrated unknown-mode custom pack.

Domain utility: LOW — the composed planner/contract domains reinforced the need for a single explicit accepted-set invariant, but repository code and executable probes supplied the decisive evidence.

REQUEST_CHANGES
