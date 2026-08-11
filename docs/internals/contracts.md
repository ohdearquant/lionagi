# Public-surface contract tests (`tests/contracts/`)

This covers the "V0 behavior-preservation gate" implemented by
`tests/contracts/_capture.py` and `tests/contracts/test_public_surfaces.py`. Its job
is to catch an observable public surface changing by accident during refactors —
HTTP routes, OpenAPI shape, the CLI's argparse tree, MCP tool projections,
machine-mode output classification, and the set of public imports.

## How the gate works

Before any consolidation edit, each of these surfaces was captured once by calling
the functions in `_capture.py` against the pre-edit worktree, and the result was
frozen as JSON under `tests/contracts/data/`. `test_public_surfaces.py` calls the
same functions again against the live code and diffs the fresh capture against the
frozen baseline, field for field. A diff means an observable surface changed.

A diff is never routine, but it is not always wrong. There are two cases:

- **Unintended delta** — an edit changed observable behavior it was supposed to
  preserve. Fix the code, not the baseline. This is the case the gate exists for,
  and the common one.
- **Intended delta** — a change deliberately adds to or alters the public surface,
  and the baseline is now a stale description of it. Record the new baseline.
  Refusing to record an intended change doesn't preserve anything — it just leaves
  the gate red until someone refreshes the baseline anyway, usually with less care
  than the procedure below asks for.

A capture bug, where the baseline never described the code correctly in the first
place, is fixed the same way as an intended delta.

## Recording an intended delta

All three steps are required, because a wholesale baseline refresh is exactly how
an unrelated regression gets laundered in unnoticed:

1. Regenerate to a scratch file and diff it against the committed baseline *before*
   installing it. Read that diff as the change's own claim about its surface, and
   confirm every line in it belongs to the intended change — this is the step that
   catches a second, unnoticed delta riding along.
2. Say in the commit message what moved and what did not: the counts that changed,
   and the ones that stayed put.
3. Where a count is also asserted as a literal in `test_public_surfaces.py`, update
   it by hand and mutation-probe it (restore the old value, confirm the test goes
   red, restore the new one). Those literals are a second, independent lock on
   facts the JSON also carries — they stay hand-typed rather than derived from the
   baseline, which would collapse two checks into one.

## Waiver: redacting host-volatile output

Two fields carry inherent host-state volatility and are excluded from the
byte-for-byte comparison: the `agent status` specialized-CLI case includes a live
session UUID and elapsed timers, and machine-mode `monitor`/`agent` cases report
live run state. Their exit codes and envelope shape are still compared; their
literal stdout is not.

More generally, every case captured by `SPECIALIZED_CASES` / `MACHINE_CASES` in
`_capture.py` is redacted by default. A case's literal stdout/stderr may only be
committed to this public repository if its argv is listed in
`_COMMITTABLE_SPECIALIZED_ARGV` / `_COMMITTABLE_MACHINE_ARGV` in
`test_public_surfaces.py`, each entry stating why that argv's output is safe:
static argparse usage/help/error text, derived from this repo's own source,
carrying no session/host/run state. This is a population rule enforced against the
live set of cases, not a fixed list someone has to remember to grow —
`test_new_case_defaults_closed_without_declaration` in `test_public_surfaces.py`
checks that a new case defaults to closed (redacted) until someone adds a reasoned
entry to the allowlist.

Two mechanisms in `_capture.py` back the redaction:

- `differential_capture()` runs a given argv three times — once under the ambient
  environment, once under a deliberately different `HOME`/`TMPDIR`/`USER` and
  working directory, and once more after a wall-clock gap crossing a one-second
  boundary. Output that reads anything from the environment, the current
  directory, or the clock necessarily differs across the three runs; genuinely
  static argparse text does not. This checks the property that actually matters
  (does the output depend on the machine at all) instead of guessing at what a
  leaked value looks like.
- `known_machine_identity()` closes the gap `differential_capture` can't: a value
  that's constant on one machine but still identifying (a hostname baked into a
  banner line, for instance) won't vary between two runs on the same box. It
  redacts a small set of literal identifying values — hostname, real username,
  home directory, this checkout's path — directly, rather than pattern-matching.
