# ADR-0137: Failed rounds and the output limit on API-model legs

- **Status**: Accepted (2026-10-25, #3763)
- **Kind**: Aspirational
- **Implementation-status**: implemented: D1 to D4; the cost measurement has run and keeps D1's
  limit (Consequences, Cost)
- **Area**: operations
- **Date**: 2026-10-25
- **Relations**: extends ADR-0041 Amendment 1 (API-model `li agent` legs) and ADR-0130 Amendment 1
  (the fold resumes of the same loop, which D2 counts apart from its own); read with ADR-0132
  (LNDL rounds, which already repair an empty round inside their round budget and are unchanged)

## Context

An API-model `li agent` leg (ADR-0041 Amendment 1) runs lionagi's own tool loop:
`_run_api_turn` calls `_api_loop_pass`, which runs `Branch.ReAct(tools=True)`, or LNDL rounds when
the profile selects `loop: lndl` (ADR-0132). ReAct ends its loop when a round's analysis does not
ask for an extension, then sends an answer turn, and the leg returns the answer as the run's
result. Two defects, each found in Claude Haiku 5.5 agent runs, ended a run as completed with its
work undone, and two costs sat behind them.

- **P1: a round spent on thinking ends the work.** The leg builds its model with no output limit,
  so an Anthropic request carries the provider default, `ANTHROPIC_DEFAULT_MAX_TOKENS = 8192`
  (`lionagi/providers/anthropic/messages.py`), thinking included. Claude Haiku 5.5 thinks by
  default, and a response spent the whole limit on thinking: `stop_reason` `max_tokens`, one
  `thinking` block, no text and no tool call. ReAct reads `extension_needed` only from a parsed
  analysis (`_need_extension` in `lionagi/operations/ReAct/ReAct.py` returns False for anything
  else), so the empty reply ended the loop. The answer turn after it was cut off the same way, the
  empty text became the answer, and the run read completed.
- **P2: a fence inside a JSON string ends the block.** A reply put its actions in a ```` ```json ````
  block whose argument text held a fenced block of its own, as a file written through the editor
  tool does. `extract_json` read the block up to the first ```` ``` ```` it met, which was inside a
  JSON string, so the reply did not parse, none of its actions ran, and the run ended completed
  after the answer turn.
- **P3: an empty reply is "repaired" by retries.** When a reply does not parse, `parse()` sends up
  to four reformat requests (the default `retry_attempts=3` after the first). A reformat request
  carries the conversation and asks the model to put the reply into the schema. With no reply text
  there is nothing to reformat, so what comes back is a new answer written from the conversation:
  a retry under a repair's name, with the same output limit. In the recorded run behind P1, 13
  requests spent 85,551 output tokens, and the 8 reformat requests sent for the two empty replies
  spent 65,536 of them, each cut off at 8,192 like the reply it was sent for.
- **P4: the output limit is the only bound on thinking.** The leg sends no effort setting and no
  `thinking` parameter; `CreateMessageRequest` (`extra="forbid"`) has neither field. On this route
  the request's `max_tokens` is the one per-request bound on how much a thinking model spends.

| Concern | Decision |
|---------|----------|
| How much an Anthropic leg's request may spend | D1: an Anthropic API-model leg whose model names no output limit sends `max_tokens=32768` |
| A pass that ends on a cut-off or unreadable round | D2: it is continued with a prompt that names what failed, at most twice, then the turn fails with `FailedRoundError` |
| Reading a ```` ```json ```` block that holds a fence | D3: `extract_json` first reads blocks whose fences stand on lines of their own |
| A reply with no text | D4: `parse()` sends no reformat request for it |

Out of scope:

- **The LNDL loop.** With `lacts_with_out=False` an LNDL round that produced nothing returns
  `Retry(_NOTHING_RAN)` and is repaired inside the round budget, and an exhausted budget raises
  `LNDLError` (`lionagi/operations/lndl_middle/lndl_middle.py`), which fails the run. It already
  meets D2's rule, so D2 applies to the ReAct loop only.
- **ReAct itself.** `Branch.ReAct` keeps its contract for every other caller; the leg reads what
  a pass returned and decides. Changing ReAct to treat an unparsed analysis as a request for
  another round is an alternative below.
- **Bounding thinking.** Sending an effort setting or a `thinking` parameter needs request fields
  the provider model does not have; whether it should replace D1 is measured first (Consequences,
  Cost).
- **Other providers' limits.** D1 changes only Anthropic requests; D2 reads the cut-off shape of
  the Anthropic Messages, OpenAI chat completions and OpenAI Responses replies.

## Decision

### D1: an Anthropic leg sends a 32,768-token output limit

```python
# lionagi/cli/agent.py
_API_MAX_OUTPUT_TOKENS = 32_768
```

`_install_api_input_cap`, which already isolates each API model of the leg to put its per-model
settings on (ADR-0130 Amendment 1), also sets `max_tokens` on the endpoint config of an Anthropic
model whose config names none:

- provider `anthropic` and no `max_tokens` in the endpoint config: the isolated copy gets
  `max_tokens=32768`;
- a model that names its own `max_tokens` keeps it;
- any other provider is unchanged.

The limit assumes a model that accepts at least 32,768 output tokens, as every current Claude
model does; a model with a smaller cap names its own `max_tokens` in its config and keeps it.

It applies on a new leg and on a resumed one, as the per-model settings it sits beside do.

Why this number: the two cut-off responses of P1 took 39.7 and 40.5 seconds for 8,192 tokens,
about 200 a second. 32,768 tokens at that rate take about 160 seconds, inside the endpoint's
600-second request timeout, so the larger limit does not turn a cut-off into a timeout. The
provider registry builds the Anthropic endpoint's config with that timeout (the
`EndpointConfig` field's own default is 300), both request paths of the endpoint, direct and
routed, hand it to the HTTP client as its `httpx.Timeout`, and a test pins the 600 seconds on a
leg's model. Four times the old limit leaves a thinking model room to think and then act, which
is what the cut-off responses did not have.

### D2: a failed round is continued, then fails the turn

```python
# lionagi/cli/agent.py
_API_ROUND_RESUMES = 2

def _failed_round(branch, before: set, rounds: list) -> tuple[str, str] | None: ...
def _stopped_at_output_limit_with_nothing(response: Any) -> bool: ...

# lionagi/_errors.py
class FailedRoundError(LionError):
    default_status_code = 502
    # details: {"continued": n}
```

`_api_loop_pass` runs ReAct with `return_analysis=True`, keeps each round's analysis and the answer
in the caller's `rounds` list, and returns what ReAct returns without it. After each pass with no
input-cap refusal, `_run_api_turn` asks `_failed_round` whether the pass ended on a failed round:

- **Cut off with nothing.** One of the last two assistant responses the pass added to the branch,
  the round that ended the loop and the answer turn, stopped at its output limit with no text and
  no tool call. `_stopped_at_output_limit_with_nothing` reads the raw provider response:
  - Anthropic Messages: `stop_reason == "max_tokens"` and no `tool_use` block and no `text` block
    with non-blank text; thinking blocks do not count;
  - OpenAI chat completions: `finish_reason == "length"`, blank content and no `tool_calls`;
  - OpenAI Responses: `status == "incomplete"` with reason `max_output_tokens`, and no
    `function_call` item and no message part with text;
  - any other shape, a list of responses included, reads False.
- **Did not parse.** The analysis of the round that ended the loop is an `UnparsedResponse`: a
  reply the parse repairs could not turn into the schema. The reason names the schema's own error
  (truncated at 800 characters) or says no JSON object could be read. That error can repeat text
  of the reply and of the tool results it quoted, and the reason goes into the continuation prompt
  and into `run.json`'s error; the same text is already in the branch history every request sends,
  so the copy adds no exposure.
- **No answer text.** The answer the pass returned has no text. When the answer turn's reply does
  not parse, `ReActStream` ends with that reply's text (its `except Exception`), so an answer turn
  that wrote nothing, cut off or not, ends the pass with empty text.

Then:

- no failed round: the turn ends as before;
- a failed round, fewer than `_API_ROUND_RESUMES` continuations so far: a new ReAct pass runs on
  the same branch, with its own `max_turns`, and with a prompt in place of the instruction that
  says what failed: the output limit (keep the reasoning short, then act or answer), the parse
  failure with its reason (send the actions again in the required format), or an answer with no
  text (continue, then give the answer). The task instruction stays in the history;
- a failed round after `_API_ROUND_RESUMES` continuations: the turn raises `FailedRoundError`
  ("3 passes of the tool loop in a row ended on a failed round: ..."), with
  `details={"continued": 2}`.

Continuations are counted apart from ADR-0130's fold resumes: a pass refused at the input cap is
folded and resumed under that record's own count, and its refusal, not a failed round, is what
the turn raises if the fold cannot get under the cap. The run's status is failed and `run.json`'s
error carries the reason through the same exception path as a cap refusal, and `li agent` exits
with the failed status without a traceback (`run_agent` catches `FailedRoundError` beside
`InputTokenCapError`).

The text a run returns is unchanged: the answer's `answer` field when it has one, otherwise the
answer itself, as `Branch.ReAct` returns without `return_analysis`, and `_run_api_turn` reads it
as before.

Why: a run that reads completed after its work stopped is worse than a failed one, because its
answer is taken as finished work. A cut-off or unreadable round is a transient failure of one
response, so the run gets the chance to continue, with what went wrong said in the prompt. One
that keeps failing ends the way a refusal at the cap does. Two continuations follow the two fold
resumes ADR-0130 allows.

### D3: a fenced block is read between fences on lines of their own

```python
# lionagi/ln/fuzzy/_extract_json.py
_JSON_FENCED_BLOCK_PATTERN = re.compile(
    r"^[ \t]*(`{3,})json[ \t]*\r?\n(.*?)\r?\n[ \t]*\1`*[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
_JSON_BLOCK_PATTERN = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
```

When the direct parse fails, `extract_json` first reads each ```` ```json ```` block whose opening
and closing fences stand on lines of their own, after any indentation, the closing fence at least
as long as the opening one, and beside them, in its place, every ```` ```json ```` span up to
the next fence (the earlier pattern) that lies outside those blocks. When none of these parses,
every such span is read, as before.

- A JSON string holds its newlines as escapes, so a fence inside one never starts a line and no
  longer ends the block, compact or indented JSON alike.
- A block in a list item, indented four spaces or more, is read beside a block at the top level.
- A block written on one line (```` ```json {...} ``` ````) or closed on its last line of JSON
  still parses through the earlier pattern, alone or beside blocks on lines of their own, in the
  order the text holds them.
- Several blocks are each read with their own inner fences.
- One object is returned alone when one was found. A block on lines of its own counts as found
  whether it parses or not, as every span does in text without such a block; a span beside the
  blocks counts only when it parses. A stray span that is not JSON, such as ```` ```json``` ````
  named in prose, leaves the block's object, where the earlier pattern returned a list holding
  it, so a caller validating one object reads it.

Every caller of `extract_json` gains this. It changes only text whose direct parse failed and
which holds a fenced block.

### D4: an empty reply gets no reformat request

```python
# lionagi/operations/parse/parse.py, parse()
if not str(text or "").strip():
    empty = ExtractionError("the response has no text to parse")
    attempts.record_failure(empty)
    raise empty
```

`parse()` sends no reformat request for a reply with no text, or only whitespace, and goes
straight to its `handle_validation` outcome: an `ExtractionError` under `"raise"`, `None` under
`"return_none"`, or an `UnparsedResponse` of kind `extraction` under `"return_value"`. The
outcome reports the empty text, whatever the first attempt made of it: a schema with a required
field rejects the mapping an empty reply reads as, and without the recorded failure the outcome
would be a `SchemaRejectedError` and an `UnparsedResponse` of kind `validation`. A `parse()` with
no response format returns what its first attempt read, `None`, before this check, as it did
before this record.

Why: a reformat request with no reply text is a retry (P3). On the agent route D2's continuation
is that retry, named in its prompt and bounded. Any other caller of `parse()` now gets the empty
reply's outcome where it got up to four retries. A refusal at the input cap still raises as
before.

## Tests that prove it

- `tests/cli/test_agent_api_provider.py`: an Anthropic leg sends the output limit; a model that
  names its own limit keeps it; a round spent on thinking is continued and the turn finishes; an
  answer that keeps stopping at the output limit fails the turn; a reply that does not parse is
  continued with the reason; an answer with no text is continued, and one that keeps having none
  fails the turn; an Anthropic leg's request waits at most the endpoint's 600-second timeout; an
  LNDL round cut off with nothing is repaired inside its rounds; the
  actions in a reply whose text holds a fenced block run; `li agent` exits failed on a failed round
  without a traceback; the cut-off reading of each provider shape (15 cases).
- `tests/operations/test_parse_failure_diagnostics.py`: no reformat request for an empty or blank
  reply; an extraction error for one under `handle_validation="raise"`; the same outcome for a
  schema whose required field the first attempt rejects.
- `tests/libs/fuzzy/test_extract_json.py`: a fence inside a JSON string, compact and indented; an
  indented, longer or space-followed closing fence and CRLF line ends; an indented block beside a
  top-level one; a block on one line beside fenced blocks, read in its place; a block whose fences
  share its lines; several blocks, each with its own inner fence.

Each decision's code was switched off in turn and the suite run against it: the failed-round
check, the output limit, the line-anchored pattern, its indentation, the empty-reply rule and the
CLI exit clause each turn at least one of these tests red.

## Run-contract impact

- A turn that used to end completed with an empty or unexecuted answer now ends failed, with
  `run.json`'s error naming the reason and the failed exit status. No field is added or renamed.
- `FailedRoundError` is a new public error in `lionagi._errors`.
- An Anthropic leg's requests carry `max_tokens=32768` where they carried the provider default.

## Consequences

- A leg on a thinking model no longer reports finished work it did not do; a run that cannot get
  past a cut-off or an unreadable reply says so in its status.
- Every caller of `extract_json` reads a fenced block that holds a fence, and every caller of
  `parse()` stops paying for reformat requests of an empty reply.
- D4 is a behavior change on the retry path for every caller of `parse()`, through `operate()`
  and `ReAct()` too, and library users have no continuation like D2's. An empty reply used to get
  up to four reformat requests, and one of them could write a usable answer from the
  conversation; it now gets its `handle_validation` outcome at once, so a caller who wants that
  reach back retries the call itself on the outcome: the `ExtractionError` under `"raise"`, or the
  `UnparsedResponse` of kind `extraction` under `"return_value"`.
- A contributor changing ReAct's return with `return_analysis=True` must keep the answer last and
  the round that ended the loop before it, which `_failed_round` reads.

### Cost

A cut-off response now runs to 32,768 tokens before it ends, so a pass that keeps cutting off
spends up to that on its loop-ending round and again on its answer, for each of up to three
passes. In wall time the worst case is those six responses at about 160 seconds each, some 16
minutes before `FailedRoundError`; each request waits at most the endpoint's 600 seconds (D1), and
the leg's `--timeout` bounds the whole turn when it is given, while `li agent` sets none by default.
Before this record the same pass spent 8,192 on each, plus up to four reformat requests after each,
which D4 removes.

This record was accepted after the cost was measured rather than argued. On the agent tasks of
a benchmark, run with the limit at 8,192 and at 32,768, it recorded output tokens and dollars per
run and the task pass rate for each. A third arm bounded thinking through the request instead
(Anthropic's `output_config` effort, or `thinking` disabled at an effort that allows it) at the
old limit. If the bounded request keeps Claude Haiku 5.5 under 8,192 at the same pass rate, a
further decision bounds thinking and D1's limit is revisited.

Result, from one run of each of 26 tasks per arm on Claude Haiku 5.5, each task checking a design
record against the code it describes, with the same code, profile and tasks in every arm and each
run scored against a recorded reference answer for its task; all 78 runs completed:

| Arm | Output tokens | Dollars | Conclusion matches the reference | Reference files also cited |
| --- | ---: | ---: | ---: | ---: |
| 8,192 | 537,589 | 0.70 | 15 of 26 | 0.28 |
| 32,768 | 514,318 | 0.69 | 20 of 26 | 0.33 |
| 8,192, effort low | 284,042 | 0.44 | 15 of 26 | 0.23 |

- Paired per task, 32,768 cited more of the reference's files than 8,192 by 0.044 (standard
  error 0.022) and more than effort low by 0.092 (0.022).
- One run at 8,192 stopped at the limit twice with no text, was continued once (D2) and finished.
  No run at 32,768 stopped at it, and its largest single response was 10,615 tokens, which 8,192
  would have cut.
- So the larger limit cost no more than 8,192 and lost nothing measured. Effort low cut the cost
  by about a third and every response stayed under 8,192, with fewer matching conclusions and fewer of
  the reference's files cited, so it does not meet the condition above. D1's limit stays.
- Limits: one run per task, and the share of cited files moved by about 0.12 between runs of one
  task in earlier runs of this benchmark; the reference reaches the same conclusion on 24 of the
  26 tasks, so the conclusion count mostly shows how often a run reaches that one.

Reversing a decision: D1 is one constant; D2 removes a check and a constant and restores the
completed status of a failed pass; D3 and D4 each restore one pattern or one branch.

## Alternatives considered

### Bound thinking instead of raising the limit

Sending an effort setting or `thinking` disabled would keep a thinking model's responses short
at the old limit and the old price. It needs request fields `CreateMessageRequest` does not have,
and a lower effort can lower the quality of the work the leg is run for. It is not argued here: it
is the third arm of the cost measurement, and its result decides between it and D1.

### Raise the limit only

A larger limit makes a cut-off rarer and does not remove it, and P2's unreadable reply has
nothing to do with the limit. Without D2 a cut-off at any limit still ends the run completed.

### Treat an unparsed analysis as a request for another round inside ReAct

This would fix P1 and P2 for every caller of `Branch.ReAct`, and change the loop's contract for
all of them: a caller that reads an unparsed reply as the end today would loop instead. The leg
owns its retry policy, its prompts and its failure status, so the decision sits there.

### Fail the turn at the first failed round

Simpler, and it turns every transient cut-off into a failed run. A continuation that says what
went wrong recovered the cut-off case in the scripted runs, at the cost of one more pass.

### Keep reformat requests for an empty reply

They are retries that spend the full limit each (P3), unnamed and unbounded by the leg. D2's
continuation does the same job with a prompt that says why.

### Take the last fence, or scan the JSON string-aware

Taking the last ```` ``` ```` as the end of the block would merge several blocks into one. A
scanner that follows JSON strings from the opening fence would read the block exactly, with more
code than a line-anchored pattern that already cannot stop inside a JSON string.

## Notes

The same decisions were first made, measured on the recorded runs, and tested on the agent route
of a sibling runtime that shares this loop; this record carries them to this package with its own
tests.
