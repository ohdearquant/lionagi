---
adr: ADR-0008
status: draft
liveness: unimplemented
date: "2026-09-23"
area: program
kind: extends
depends_on:
  - "[[ADR-0004-the-language|ADR-0004]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
tags:
  - adr
  - runtime
  - lndl
---

# ADR-0008: The program

## Context

A command whose result the model must read costs a turn today: the model asks, the result lands in
its view, the model reads it and asks again. Finding books similar to one and keeping those over a
threshold takes two model calls, and the candidate list passes through the context twice, as a
result and retyped into the output. A script does it in one pass and hands back only what was asked
for.

The dispatcher is already a dataflow executor: one task per command, a reference to another command
waits for it to settle, a pointer is dereferenced at execution time, and `OUT{}` waits for what it
names ([[ADR-0005-command-handling#^c1|ADR-0005/C1]]).

Missing above it: expressions over values, control flow, a result written to a note instead of the
view, and a reading that fixes small slips without executing a message it could not read.

The grammar exists as a parser with no evaluator and no consumer: the symbolic phase of the `krons`
LNDL package (lexer, Pratt parser, AST with `if`/`elif`/`else` by indent, inline `if`, `return`,
`note["key"]`, `DO`). This record adopts it as the program form, compiled onto the existing
dispatcher. Source: `lionagi/lndl.py`, `Actor._dispatch` and `_perform` in `lionagi/actor.py`,
`krons/lndl/symbolic_*.py` and `krons/lndl/fuzzy.py`.

## Definitions

- **program form**: the message written as a fenced block of statements, blocks by indent; a profile
  option beside the tag form.
- **graph**: what a message compiles to: nodes for commands, values, conditionals and the output;
  edges for the references between them.
- **node**: one unit the dispatcher runs: a command, a value, a conditional holding sub-graphs, or
  the output.
- **control result**: what a block finishes with: Continue, Return(value) or Failed; only Continue
  runs the block's lexical suffix.
- **expression**: a computation over values, evaluated by the runtime when its node executes (C3).
- **repair catalogue**: the closed list of normalisations applied before compilation (C2).
- **consumed result**: a RESULT whose only readers are other nodes or a note assignment, and that
  `OUT{}` does not name.
- **`language`**: the profile's choice, `core` (the tag form) or `program`.

## Assumptions

| #  | statement                                                                                          | source                                                                                        | if false                                                   |
| -- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| A1 | Most unreadable messages are small slips with one obvious repair, not wrong programs.                 | 55 recorded unreadable messages: closers, commas, quotes, line breaks; 5 left after four readings | C2's repair catalogue buys little; the tax is elsewhere    |
| A2 | A model that can write a two-line shell pipeline can write a two-line program in this form.        | assumption; bench messages already chain `*a` into a second command in one turn                 | the program form stays a per-profile option (C6)           |
| A3 | Intermediate results the model never reads cost context and buy nothing.                          | on the bench the view held every RESULT for a turn whether or not the model used it          | C5's curation saves less than the view budget already does |

## Claims

### C1: A message is compiled whole, then run; one that does not compile runs nothing _(enforced: mechanical)_ ^c1

- **Subject**: every message under the program form.
- **Violated when**: a node executes before the whole message compiled, or a message with a syntax
  error executes in part.

The message is read into the graph first. The graph is checked before anything runs: every command
name is in the profile's subset, every reference names a node or a record entry, no cycle over
reference, control and effect edges together, every expression well-formed, no alias collision. A
failed check refuses the message as one retry, naming the line and the reason
([[ADR-0002-the-run#^c1|ADR-0002/C1]]); nothing ran, so nothing is half done.

Whole-message admission is the decision: nothing runs until every line is read and checked, so a
syntax error never leaves the world changed. A graph, not a tree walk, because the dispatcher
schedules by dependency and each node needs an identity. A node that fails at execution leaves the
settled nodes settled: the check refuses, it does not roll back.

### C2: Fuzzy reading is a normalisation pass with a closed catalogue, and every repair is named _(enforced: mechanical)_ ^c2

- **Subject**: every message before compilation.
- **Violated when**: a repair outside the catalogue is applied, an ambiguous repair is applied, a
  repair is applied silently, or a repaired message that still does not compile runs.

Before compilation the reader applies a fixed list of repairs, each recorded: missing closers, a
missing comma, line breaks inside a string, a swallowed closing tag, invented tag shapes (`{lact
x}`, `Note.`), whitespace or case in an `OUT{}` key. Also a command, field or alias name matched to
one known name by string similarity: Jaro-Winkler, a threshold per kind; two candidates within 0.05
is ambiguous.

A repair applies only when it is unique and the repaired message compiles; an ambiguous one refuses
the message naming the candidates. The name repairs start off, because the nearest known name is not
the meant one when that is absent from the subset; D2's measurement turns each on by kind. Every
applied repair rides the next notification ([[ADR-0006-the-notification#^c2|ADR-0006/C2]]).

### C3: Expressions over values are evaluated at execution, not by the model _(enforced: mechanical)_ ^c3

- **Subject**: every argument, assignment and `OUT{}` field under the program form.
- **Violated when**: an expression is evaluated by anything but the runtime's own evaluator, or can
  call anything that is not a command node.

The expression language is the symbolic grammar's: a pointer `*a`, field access `.title`, index
`[0]`, `+ - * /`, comparisons, `and`, `or`, `not`, literals, `note["key"]`, an inline `if c: a;
else: b`. One comprehension form is added, `(x for x in *r if x.score > 0.8)`.

The runtime's own evaluator (never `eval`) computes it when its node executes; a pending reference
waits. Values are data (scalars, strings, lists, dicts, a Spec's fields); access and indexing read,
never call. A type error settles the node `invalid`, naming both. The only loop is the
comprehension; a call is a command node, `DO name(...)` (C6). `note["key"]` is read once per node,
with its version: one expression, one version. A size limit on what it builds is owed.

### C4: A command's result may be assigned to a note, and then it is not in the view _(enforced: mechanical)_ ^c4

- **Subject**: every assignment `note.key = ...` and `note.key.path = ...`.
- **Violated when**: a note assignment renders its value in the view, a note is written before its
  value settled or in a block not reached, or a failed result is written as a note's value.

A note assignment is a node: `note.book1 = {...}` writes when its block reaches it, never hoisted
past a branch or `return` ([[ADR-0007-the-record#^c5|ADR-0007/C5]]); two assignments from one
producer are two writes. `note.book1.similar = search(...)` runs the command; at settle its result
is written there (a dotted path writes into a JSON note) and the RESULT renders as a placeholder,
name and size, never in full. The record keeps the value; the pointer and `context.show` reach it.

A producer that failed, was refused or cancelled writes nothing: the note keeps its value and the
assignment is told not written; a store failing after the producer settled keeps the RESULT and
claims no write.

### C5: The view is curated: consumed intermediates render as placeholders _(enforced: mechanical)_ ^c5

- **Subject**: every RESULT produced by a program.
- **Violated when**: a result consumed only by other nodes or by a note renders in full, or a result
  the program left unconsumed is hidden.

A RESULT that another node consumed, or that a note assignment took, renders as a placeholder in the
turn it lands; a RESULT nothing consumed renders in full as today, and so does what `OUT{}` names.
The model asks to see a consumed value by pointing at it or by `context.show`. This is a rendering
rule, not a record rule: the record holds every value whole
([[ADR-0007-the-record#^c2|ADR-0007/C2]]).

### C6: The program form is a profile option; the three constructs stay the core _(enforced: mechanical)_ ^c6

- **Subject**: every profile.
- **Violated when**: a profile without the option is shown program guidance, or a program message is
  accepted from it.

A program is written in a fenced block, statements by line, blocks by indent:

```
note["book1"] = {title: "…", description: "…", author: "…"}
similar = search_similar(input=*note["book1"].title + " " + *note["book1"].description, candidates=*catalog)
chosen = *similar if *similar != [] else DO search_similar(input=*note["book1"].title, candidates=*catalog)
note["book1"]["similar"] = *chosen
OUT{similar_books: (b for b in *chosen if b.similarity > 0.8)}
```

Prose outside the block is scratch; inside, a `#` line is a comment. A command runs when its block
reaches it ([[ADR-0002-the-run#^c1|ADR-0002/C1]]); one inside an `if` only when its branch is taken;
`return` ends the program with a value for `OUT{}` (C7). A name bound in a branch stays there; one
that escapes is bound outside by the inline `if`. Inside, what is written runs or is refused; a
value costs nothing until dereferenced.

The tag form of [[ADR-0004-the-language#^c1|ADR-0004/C1]] compiles to the same graph under its own
admission: an unknown name or a taken alias refuses that command, not the message
([[ADR-0005-command-handling#^c1|ADR-0005/C1]]); C1's check is the program's. A profile names its
language, `core` (default) or `program`, and sees only its guidance.

### C7: A block finishes with a control result, and only Continue runs its suffix _(enforced: mechanical)_ ^c7

- **Subject**: every block of a program: the top level and each branch.
- **Violated when**: a node runs after its block returned or failed, an untaken branch runs, or a
  refused `return` falls into the suffix it skipped.

Continue runs what follows in the block; Return(value) hands `OUT{}` one candidate and ends the
program through every enclosing block; Failed likewise. A conditional's control result gates every
later node of its block, and a taken `return` inside it propagates outward, so a nested suffix is
gated as the top level is. A refused candidate is a retry ([[ADR-0003-the-bounds#^c4|ADR-0003/C4]]),
never a fall into the skipped suffix. A forward pointer that crosses its own `return` guard is a
cycle over reference and control edges and refuses the message (C1).

## Decisions

### D1: A compiler in `lionagi/lndl/`, its graph handed to the existing dispatcher ^d1

Serves C1, C3, C6 and C7. `read` normalises (C2), the parser builds the AST, and `compile` lowers it
to the graph, an `if` becoming a conditional node whose control result gates its suffix (C7).
`check` validates it over reference, control and effect edges, and the nodes are dispatched as
commands are, the evaluator called at dereference. The consumed mark is a directive dated at the
consumption, on the next notification or the closure ([[ADR-0007-the-record#^d2|ADR-0007/D2]]), so a
resumed chat folds it and recompiles nothing; the record keeps four kinds
([[ADR-0007-the-record#^c1|ADR-0007/C1]]).

- **Landing evidence**: owed: `tests/test_program.py` with the example above resolving in one turn,
  a syntax error refusing the whole message with nothing executed, and each catalogue repair named.

### D2: The repair catalogue is data, and each repair is counted ^d2

Serves C2. A table of repairs with a name, a matcher and a rewrite; the four readings of
[[ADR-0004-the-language#^c3|ADR-0004/C3]] are its first entries. The notification names the repair
as "<line>: read as X; if that is not what you meant, write it again", and the run counts repairs by
name so the bench can say which slips models make.

- **Landing evidence**: owed: the count in the run's usage row; unknown-name refusals with their
  edit distance to the nearest known name, measured before the name repair is turned on.

### D3: Note assignment and view curation in the dispatcher's settle ^d3

Serves C4 and C5. At settle the node's target is read: a note path writes the store. A RESULT counts
as consumed once a reader settled ok or its note was written; the runtime's hide, dated at that
consumption, rides the next notification, or the closure when no turn follows
([[ADR-0006-the-notification#^c1|ADR-0006/C1]]); a later `context.show` stands over it
([[ADR-0007-the-record#^c3|ADR-0007/C3]]). A reader in an untaken branch, or one that failed,
consumes nothing. A RESULT `OUT{}` names is never consumed; the fold renders a consumed RESULT as
any hidden one, a placeholder.

- **Landing evidence**: owed: a test where the consumed result never appears in a backend's messages
  while the pointer and `context.show` still reach it.

## Alternatives

| approach                                          | rejected because                                                                                          |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Interpret the message line by line                   | an interpreter that parses whole first is the same decision; the graph is chosen for scheduling and node identities, not for the preflight |
| Run the readable commands of a broken message        | the same partial state, plus a model that learns half its message ran; a syntax error refuses whole (C1)     |
| A general expression language (Python subset)     | `eval` is a second runtime with its own attack surface; the closed set covers select, join and filter     |
| A model call to filter or summarise results       | a second model on the data path costs a call and reads the whole result; the filter runs in the runtime   |
| `DEF` and `while` now                             | `DEF` is a token the symbolic parser does not yet read; named sub-programs are the next record; no unbounded loop |
| "Only what `OUT{}` names runs" (the scratch rule)  | told three times its edit had not run, a model rewrote the same message; every top-level command runs (C6)  |
| Rebinding a name across branches | not specified here: a name is bound once, and the inline `if` selects between aliases |
| A terminal `OUT{}` as the only exit | a model with its answer inside a branch must copy it out through the inline `if`; the control result costs one edge per block (C7) |
| Making the program form the only form | today's models write the three constructs well and the program form poorly; the profile chooses (C6)      |

## Consequences

- **S1**: A message is a small program; the model call count for select-join-filter work drops to
  one and the intermediate data never enters the context.
- **S2**: A refused message costs a turn as today; the fuzzy pass is expected to make refusals
  rarer, and its counts say by how much.
- **S3**: The symbolic grammar gains what it lacks today: an evaluator, an executor (the dispatcher)
  and a consumer. `DEF` and the remaining semantic operators land in the next record, same profile
  option.
- **S4**: A resumed chat replays programs by folding, as it replays directives; the long-running
  agent's continuous run has a checkpoint ([[ADR-0013-the-agent#^c7|ADR-0013/C7]]), no other run
  does.
- **S5**: The raw message is what the model wrote; the normalised program and the repairs applied
  ride the TEXT entry's `meta`, so the admitted reading can be audited after the catalogue changes.
  A version stamp names a parser and keeps nothing.
