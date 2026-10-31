# lionhub.lndl

LNDL, the Lion Notation Definition Language: a structured output format for model responses, in which tags let a model mix natural reasoning with structured data. The package holds the lexer, parser, assembler, diagnostics and the system prompt that teach a model the format. It is not the generic response parser of the core (`lionagi/operations/parse/`) and it does not run models; it reads and builds the text a model produced.

Final import path: `lionhub.lndl`. The old path `lionagi.lndl` keeps working through 1.x as the same module object, with a `DeprecationWarning` naming the new path, and old pickled `lionagi.lndl.*` paths keep loading. The nine LNDL names exported lazily from the root `lionagi` package stay lazy compatibility names until 2.0. A plain `import lionagi` loads nothing from this package.

| Slot | What it holds | Source |
| --- | --- | --- |
| `__init__.py` | the public names: `assemble`, `Parser`, `Lexer`, `LNDLError`, `LNDL_SYSTEM_PROMPT`, `extract_lndl_blocks`, round outcomes and the trace types | `lionagi/lndl/__init__.py` |

The draft reserves only the package marker. The modules that sit beside it on the development line have no slot yet and move with the package: `lionagi/lndl/ast.py`, `assembler.py`, `diagnostics.py`, `errors.py`, `extract.py`, `lexer.py`, `normalize.py`, `parser.py`, `prompt.py`, `round_outcome.py`, `types.py` and `_parse_function_call.py`. The LNDL Middle (`lionagi/operations/lndl_middle/lndl_middle.py`) moves to `lionhub.lndl.middle`.

## Who uses it

- `lionagi/__init__.py`: the lazy root exports of LNDL names.
- `lionagi/operations/parse/parse.py`: does not import this package; it takes fenced blocks from the core extractor `lionagi/libs/schema/extract_code_block.py`, which LNDL's own `extract.py` is built on.
- `lionagi/operations/lndl_middle/lndl_middle.py`: the opt-in LNDL Middle; it imports the LNDL names and moves with the language.
- `lionagi/cli/agent.py`: builds the Middle with `build_lndl_middle`.

## Status

Moves in the third wave, after `providers`, `tools`, `agent`, `casts` and the external hooks (`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md`, Amendment 1, A3 and A5). The fence-extraction seam is already in the core: `parse.py` uses `lionagi/libs/schema/extract_code_block.py`. The code is still at `lionagi/lndl/`.
