# Examples

One idea per file, read in the order of the table: two notebooks open with the run and the language
the model speaks in it, six scripts take one mechanism each, a third notebook hands work from one
actor to another, and the last script puts a real model behind the same loop. Everything but the
last runs offline, with no key: where a model is needed, its turns are written in advance.

    uv run python examples/<file>.py

The notebooks run in Jupyter from this repository's environment and are stored with their outputs
cleared. `tests/test_examples.py` runs every script here: the scripted ones to their end, the live one
with no key and no CLI, where it names what is missing and stops.

| file | pattern | the idea |
|---|---|---|
| `notebooks/01_the_run.ipynb` | Specs, handlers and a profile on one actor; four scripted turns; the record printed | Specs are what the model may say; handlers act; the record remembers; the model points at a value instead of repeating it |
| `notebooks/02_lndl.ipynb` | what `parse` accepts, repairs and refuses; the heredoc; native tool-call markup; the guidance | the model writes three constructs in its prose, and a slip the parser repairs is named back to it |
| `examples/tools_and_gates.py` | a tool library registered on an actor; profiles as subsets; privileges; the bus as the trace | two gates: the prompt is generated from the profile's subset, and privilege is checked at dispatch |
| `examples/notes_and_context.py` | notes that outlive a run; hiding and summarizing the view; pointers to hidden entries | big picture in notes, details behind pointers; the record is never edited, only the view |
| `examples/watch_the_artifact.py` | a section reporting the artifact; a stop rule; an accept gate on `OUT{}` | the system watches the thing being worked on, not the model's claims |
| `examples/budgets_and_outcomes.py` | turns, the clock, refusals, the view budget; every outcome constructed | each way a run ends is a value the caller reads, never a string |
| `examples/structured_output.py` | a nested Pydantic output (`emits`), a `Literal`, a field validator, an accept gate; the dict-literal and the field-by-field forms of `OUT{}` | the run ends in the object, held to its types and the system's gate, a value refused is told back as the exact error, and a gate never satisfied ends the run as `Refused` |
| `examples/serve_an_actor.py` | `Actor.serve` with one offer on a free loopback port; the script as its own client: the card, two refused starts, a start, the stream read to its end, the record | a client starts only what the owner offered, and a stream opens with the run's state, so a client that arrives late still learns how the run ended |
| `notebooks/03_two_actors.ipynb` | one actor asking another from inside a handler, both scripted | delegation is one command and one round trip on the record |
| `examples/backends.py` | OpenRouter with DeepSeek pinned, a thinking budget and the stop frame; Codex; Claude Code (`claude`/`session` resume one CLI session per run, `claude-oneshot` is one call per turn); cost and cache per call (live) | a backend is messages in, text out; the loop never depends on native tool calling |

The design records are in `docs/adr`.
