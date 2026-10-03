---
adr: ADR-0004
status: draft
liveness: operating (the refusal of a mixed response and the DeepSeek markup read in the backend are pinned by tests)
date: "2026-09-23"
area: language
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0002-the-run|ADR-0002]]"
tags:
  - adr
  - runtime
  - lndl
---

# ADR-0004: The language

## Context

The model writes prose, and somewhere in the prose are the things it asks for. The runtime must read
those out without a provider's tool-call wire format, and the values it reads are multi-line, carry
quotes, and are shaped like JSON, because that is what a model writes when it means a value. This
record fixes the grammar of a message, how leniently it is read, and what an unreadable message
costs.

A message is what the model wrote in a turn ([[ADR-0002-the-run|ADR-0002]]); its commands go to
dispatch ([[ADR-0005-command-handling|ADR-0005]]) and its reading errors to the notification
([[ADR-0006-the-notification|ADR-0006]]). `DO`, `if`, `DEF` and the semantic operators of the
symbolic LNDL design are not part of this runtime; the program form is
[[ADR-0008-the-program|ADR-0008]]. Source: `src/lion1/lndl.py` and the reading half of `Actor.run`
in `src/lion1/actor.py`; a provider's own formats are read in the backend
([[ADR-0009-backends|ADR-0009]]).

## Definitions

- **LNDL**: the notation the model writes inside its prose to make requests: three constructs;
  everything else is text.
- **value declaration**: `<lvar>`: names a value under an alias; with `spec.field` before the alias
  it fills one output field.
- **command**: a request dispatched to a handler: a `<lact>`, naming one Spec with its arguments, or
  `OUT{}`, lowered to the `out` command once [[ADR-0003-the-bounds#^d3|ADR-0003/D3]] lands; today
  the loop assembles it itself.
- **`OUT{}`**: the declaration of done: names the aliases and literals that fill the output's
  fields.
- **alias**: the name the model gives a value or a command's result.
- **pointer**: `*alias`: a reference to a value or a result, dereferenced when the command executes;
  it stands where a value stands, never as a key or a set member.
- **heredoc**: `field=<<TAG` in a call's arguments, then the lines verbatim, then a closing line
  that starts with `TAG`; the second form of a string argument.
- **reading**: one attempt to parse a call's body.
- **repair**: what a reading past the first supplied; escaping a line break inside a string literal
  changes nothing the model would rewrite, so the model is not told of it; it is only counted.
- **native tool call**: a provider's structured call in the response, emitted although no tool
  schema was sent.
- **program**: the same requests written as a script; a profile option
  ([[ADR-0008-the-program|ADR-0008]]).

## Assumptions

| #  | statement                                                                             | source                                                                                                                                    | if false                                |
| -- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| A1 | A model trained on JSON function calling writes strings the JSON way.                 | 51 of the 52 unreadable messages by shape: 21 native tool calls, 16 quotes or line breaks in a literal, 7 bare shell, 4 values written as `<lact>`, 3 OUT errors | C2 buys nothing; the tax is elsewhere   |
| A2 | A slip at a call's closers is the call the model meant, short of its closers.         | all 7 closer slips in the residual set finished with `finish_reason=stop`                                                                | C3's repairs execute wrong calls        |

## Claims

### C1: Three constructs; everything else is prose _(enforced: mechanical)_ ^c1

- **Subject**: every message, its value declarations included.
- **Violated when**: the parser acts on text outside the three forms, or a form is read in a way the
  guidance does not state.

```
<lvar alias>value</lvar>                        name a value
<lvar spec.field alias>value</lvar>              name a value that fills one output field
<lact alias>name(field=..., x=*other)</lact>     ask for something to happen
OUT{spec: [alias, ...], other: literal}          finish with typed output
```

Bare names inside `OUT{}` are aliases; anything else is a literal; a one-field output takes a
literal, or one bare result in a list; any output takes a dict literal of its fields. Tags are
siblings: a tag quoted whole in a value stays text; else the first closer ends the value. An alias
used twice in one turn keeps the first use and names the second.

A string's contents are never read for tags, `OUT{}` or heredocs: one pass finds the constructs in
order and each owns its span, so `OUT{answer: "<lact a>x()</lact>"}` is a string. A quoted string in
a dict literal means the string: a field that takes text gets it as written, and only a field that
cannot reads it as a literal.

### C2: A heredoc body is verbatim, and its closing line is recognised in four positions _(enforced: mechanical)_ ^c2

- **Subject**: every `<lact>` argument.
- **Violated when**: text between the opening and closing lines is interpreted beyond its line
  endings, or a closing line in one of the four legal positions is not recognised.

The closing line starts with `TAG` and holds nothing else but, optionally, a comma, a closing
parenthesis, the next `name=`, or the call's closing tag; a longer name is not `TAG`. A CRLF body
reads as its lines, LF-ended. A heredoc never closed ends at its call's first closing tag. An
opening in prose or in a string literal is text; a closing line right after the opening is an empty
body.

Heredocs are lifted before anything else in the call is read, so a script, a shell line or a code
block needs no escaping. The guidance names this as the first form for a multi-line or quote-bearing
value; a value in its own `<lvar>` passed by pointer is the second.

### C3: A call is read leniently, in a fixed order, and every repair is named _(enforced: mechanical)_ ^c3

- **Subject**: every `<lact>` body.
- **Violated when**: a reading outside the order is tried, a repaired reading is executed without
  its note reaching the notification, or a non-call body is dropped.

Readings, first to parse wins: as written; line breaks inside string literals escaped, triple-quoted
literals untouched (quotes and braces inside them included, in `OUT{}` as in a call); the call's
missing closers supplied; a missing comma between arguments supplied, looking back across line
breaks. A repair's note, naming what was supplied and inviting a rewrite, rides the next
notification's `not executed:` list under `turn`, though the repaired call ran
([[ADR-0006-the-notification#^c2|ADR-0006/C2]]).

A `<lact>` whose body is not call-shaped is kept as an `<lvar>` under its alias and the model is
told. A call still unreadable names the heredoc route. Measured on the recorded unreadable messages:
replay of 55, 5 left; live unreadable rate 5.5% before, 0.9% after, with 25 repairs named in the
run.

### C4: A provider's native tool call is read back as the text the model wrote, then dispatched _(enforced: mechanical)_ ^c4

- **Subject**: every backend response.
- **Violated when**: a tool schema is forwarded to a provider, a native tool call carrying LNDL is
  refused, or a message carrying the loop's own frame is executed.

No backend forwards a tool schema. A native call in a response with empty content is read, not
retried; beside non-empty content the message is refused as a retry naming both channels, so nothing
runs on a message read by halves. A call named `lact...` or `lvar...` is the tag the model wrote,
rebuilt with its arguments; any other becomes `<lact auto_hash>name(...)</lact>`, dispatched like a
typed one (ADR-0005). DeepSeek's markup around a tag reads as the tag; a swallowed closer is closed.

A message containing `<system turn=`, in any case or spacing after `system`, is refused whole, even
inside backticks, naming its line. Only the loop writes that frame; its results were never observed
(9 of 50 thinking-off runs, half their output).

### C5: A message with no LNDL is legal and told _(enforced: mechanical)_ ^c5

- **Subject**: every message without a construct.
- **Violated when**: such a message ends the run, or a call written inside `<lvar>` runs.

The run continues with an idle note; a second idle turn adds a literal example; a stray command tag
is named as not a call. A value tag holding `name(...)` for a known command runs nothing and is
named as such.

## Decisions

### D1: The heredoc form and the reading order in `lndl.py` ^d1

Serves C2 and C3. `_lift_heredocs` runs first; `_readings` yields the four readings with the repairs
each applied, by name; `parse_call_noting` returns the name, the arguments and those names; `parse`
counts every name for the run and appends one note naming each repair to the program's problems. The
line break escaped in a string is counted and carries no note.

- **Landing evidence**: `tests/test_lndl.py`; the replay of the recorded unreadable messages;
  `test_a_call_needing_a_comma_and_its_closers_names_both_repairs`.

### D2: Native tool calls are rebuilt at the backend boundary ^d2

Serves C4. The backend adapts a provider's output before `parse` reads it, so `src/lion1/lndl.py`
reads LNDL and knows no provider. `tool_calls_text` is applied by the OpenRouter backend when the
message content is empty ([[ADR-0009-backends#^d1|ADR-0009/D1]]). `normalize`, which reads DeepSeek
markup and closes a swallowed closer, lives in `hub/backend/markup.py` and runs in that backend. A
mixed response returns a `Reply` carrying `refused`: a retry, its reason kept on the turn's entry so
a resumed run declares nothing from it.

- **Landing evidence**: `tests/test_openrouter.py`:
  `test_a_native_tool_call_answer_is_the_models_turn_as_text_and_is_not_retried`,
  `test_a_native_tool_call_beside_text_refuses_the_turn_whole_and_the_model_is_told`,
  `test_the_backend_rewrites_native_markup_and_a_swallowed_closer_before_the_parser`; the markup
  cases in `tests/test_harness_markup.py`.

### D3: The guidance text names the heredoc first ^d3

Serves C2 and C5. `guidance()` shows the three constructs, one example with a pointer, the heredoc
form, the pointer rule, and the turn rule; it is the second part of every system prompt
([[ADR-0001-the-actor#^d2|ADR-0001/D2]]).

- **Landing evidence**: `test_guidance_names_the_runs_outputs_in_the_out_example`.

### D4: `_read` names the frame's line, and the guidance says the alias rules the run keeps ^d4

Serves C1 and C4. `_read` in `src/lion1/actor.py` drops format characters (Unicode `Cf`) and raises
on the first `<system turn=` anywhere, in any case or spacing after `system`, its `<` also read as
`&lt;`. It names the line number, a lone CR breaking a line too, and the line from the frame on, and
tells the model to call it the notice and never write its tag. The guidance says a command's alias
is unique for the whole run and a value declared again rebinds, the rules the actor keeps.

- **Landing evidence**: `tests/test_actor.py`:
  `test_a_frame_anywhere_a_mention_included_is_refused_naming_its_line`;
  `tests/test_lndl_guidance.py`. Controls: a backtick exemption, an off-by-one line, the refusal
  without its word on the notice, the old alias wording, a case-sensitive match, exact spacing, no
  `&lt;`, keeping `Cf` characters, and counting only `\n` each fail them.

## Alternatives

| approach                                          | rejected because                                                                                 |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Failing every unreadable message                     | a turn lost each time, and the text was complete short of its closers (A2)                      |
| A raw-body form where the tag body is the value   | a command needs a name and fields to validate; a non-call body becomes a value instead (C3)      |
| Native tool calling as the channel                | a provider's wire format, and no native form for a pointer from one call into another in the same message (ADR-0005) |
| Substituting aliases into arguments               | reversed: pointers dereference at execution, so a value is never repeated in the text            |
| Matching the tag's exact bytes                    | `<SYSTEM turn=`, `&lt;system turn=` and a zero-width space inside `system` each passed it      |
| Sparing the tag inside an inline code span        | a frame whose opener alone is in backticks is then read and its commands run; a rule on syntax cannot tell a mention from a frame made to look like one |
| Python string literals inside the tags            | fought the model's JSON training on every multi-line or quoted value: 52 of 952 messages unreadable on a 50-instance SWE-bench run |

## Consequences

- **S1**: The syntax tax is under 1% of messages; the remaining misses on the bench are time budget
  and declared-done-wrong ([[ADR-0012-the-bench|ADR-0012]], the bench).
- **S2**: A repaired reading can be wrong; the model is told and can rewrite, and the record keeps
  the raw message.
- **S3**: The context layer is barely exercised on short tasks: 109 `<lvar>`, 6 notes and 9 context
  commands in 952 turns. Long tasks are where it is measured.
- **S4**: A turn that only mentions the tag is refused like a frame; the refusal tells the model to
  call it the notice. The check reads the spellings a model copying the frame writes; one built to
  evade it, a lookalike letter or a numeric entity, passes. A turn stating invented results without
  the frame is refused by nothing; the guidance says a turn states none
  ([[ADR-0018-chat-in-a-box#^c17|ADR-0018/C17]]).
