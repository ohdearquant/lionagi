# iModel construction: routing a model spec to a provider, and back

`iModel` (`lionagi/service/imodel.py`) is the wrapper a caller builds once per
provider connection: it owns rate limiting, hooks, and streaming on top of an
`Endpoint`. Most of what a caller needs is in its constructor and its
`copy()`/`to_dict()`/`from_dict()` round trip, and both hide real decisions
about how a plain model string becomes a routed request.

## Effort-suffix routing

Callers can write a model name like `"gpt-5.6-luna-high"` and have the
trailing `-high` stripped and routed to whichever kwarg the provider uses for
reasoning effort (`effort`, `reasoning_effort`, `thinking`, ...). This
happens once, in `iModel.__init__`, so every construction site gets it for
free without repeating the parsing.

The suffix only strips when two conditions both hold: the resolved provider
actually has an effort kwarg (`PROVIDER_EFFORT_KWARG`, in
`lionagi/service/providers.py`), and the model name is written in lionagi's
own `provider/model-effort` grammar rather than borrowed whole from another
vendor's catalogue. The second check is `split_effort_suffix()`, and its rule
is a single fact: lionagi's grammar spends its one `/` on the provider, so if
the model *name* (the part after any `/`) still contains a `/`, it isn't in
that grammar — it's a literal vendor id, reached for instance through a CLI
provider's own config profile naming its own `model_provider`. Those ids end
in whatever the vendor chose, and a trailing word that happens to spell an
effort level (`"low"`, `"high"`, ...) is part of the id, not a suffix to
strip. Splitting it anyway would produce a model nobody serves. An explicit
effort kwarg passed by the caller always wins over anything inferred from the
suffix.

Two providers clamp the extracted effort further, because their model
catalogues don't support every level lionagi recognizes:

- **Claude** (`_clamp_claude_effort`): only the Opus line, from Opus 4.7
  onward, accepts `xhigh` — everything else clamps to `high`. There is no
  `ultra` tier at all; `ultra` always clamps to `max`. The set of models
  that accept `xhigh` is an explicit allow-list of exact strings (both the
  bare alias and the `claude-` prefixed form, since callers pass either), so
  a new Opus release silently loses `xhigh` — the request still succeeds,
  one tier lower, with nothing in the result saying so — until its name is
  added to `_CLAUDE_XHIGH_MODELS`.
- **Codex** (`_clamp_codex_effort`): reasoning-effort ceilings are
  model-dependent, sourced from the CLI's own live model list
  (`_CODEX_ULTRA_MODELS`, `_CODEX_MAX_ONLY_MODELS`,
  `_CODEX_XHIGH_CEILING_MODELS`). Unrecognized (future) models pass through
  unclamped rather than being rejected.
- **Gemini** (`_clamp_gemini_effort`): the CLI has no effort kwarg at all —
  effort is baked into the `--model` name as `Low`/`Medium`/`High`, and the
  Pro variant has no `Medium` tier (`Medium` promotes to `High` for Pro).

## Runtime-state adoption

Some endpoints (CLI/agentic ones) carry runtime-only state — a spawned
child's environment, an `on_spawn` callback — that must never be written to
disk (see `RUNTIME_STATE_NAMES` below). When `iModel.__init__` receives an
already-constructed `Endpoint` instance, it calls
`endpoint.adopt_runtime_state(kwargs)` to place any runtime values the caller
passed at construction time — for an endpoint constructed inline (not via an
existing `Endpoint`), those values flow through the normal config path
instead, so this adoption step only fires for the "endpoint already built"
case, which otherwise misses the window where runtime kwargs are normally
lifted out of config. If the endpoint has nowhere to put a given runtime
value (a plain, non-CLI endpoint has no runtime state at all —
`Endpoint.drain_runtime_state()` is a no-op for it), `iModel.__init__` raises
`TypeError` rather than dropping the value silently. Silently discarding it
would hand a spawned child a default environment while the caller believes it
configured one — indistinguishable from a working setup until something the
child needed turns out to be missing.

## Copy and runtime state

`iModel.copy()` builds a new `iModel` with a fresh id but the same
configuration. Before the deep copy, it calls
`self.endpoint.drain_runtime_state()`. This ordering matters: a runtime value
that arrived after construction (through `update()`, for instance) is still
sitting in `config.kwargs` at copy time, and a naive deep copy would take it
along — duplicating a child's environment into the copy, and rebinding a
callback that was meant for one receiver onto the copy's receiver as well,
leaving the *original* caller's supervisor hearing nothing from the new
copy's legs. Draining first empties `config.kwargs` of anything runtime-only,
so the deep copy carries nothing live, and `copy_runtime_state_to()` then
transfers the live objects onto the new endpoint explicitly, once.

## Runtime state across serialization channels

`EndpointConfig` (`lionagi/service/connections/endpoint_config.py`) has to
keep `RUNTIME_STATE_NAMES` (`env`, `on_spawn`) out of anything written down —
these are a child process's environment (commonly holding credentials) and a
callback whose representation carries whatever object it's bound to — while
still making their *presence* visible to a developer debugging a live
session. The config exposes three different read channels, and each needs
its own exclusion because none of them share code:

- **`model_dump()` / `to_dict()`** (via the `kwargs` field serializer,
  `_serialize_kwargs`): excludes `RUNTIME_STATE_NAMES` from `kwargs`
  entirely. This is the channel `Endpoint.to_dict()`, `iModel.to_dict()`, and
  every persisted run snapshot go through, and it has to be excluded *here*
  — at the field serializer — rather than relying on endpoints to drain
  their runtime state before dumping, because `EndpointConfig` is public:
  `model_dump()` is directly callable by any caller, `update()` can put a
  runtime value back into `kwargs` after an endpoint already drained it
  once, and `iModel.from_dict()` assigns a freshly hydrated config over a
  drained one. Excluding at the one channel every dump has to pass through
  makes the answer independent of who's asking, rather than depending on
  whether some upstream drain step happened to run first.
- **`dict(config)` / `list(config)`** (`__iter__`): Pydantic's
  `BaseModel.__iter__` yields raw `__dict__` values directly and does not run
  field serializers, so it needs the identical exclusion applied separately,
  or `json.dumps(dict(config), default=str)` — an ordinary way to write a
  config to a log — would leak runtime state the field serializer already
  hides from `model_dump()`.
- **`repr(config)`** (`__repr_args__`): deliberately does the *opposite* —
  it reports that a runtime key is set (`"<env: set, not shown>"`) without
  showing its content. Both other channels omit the key outright, because a
  dump is something a config gets rebuilt from, and a stand-in string would
  hydrate back as a real value of the wrong type. `repr` is never rebuilt
  from, so it's free to say what it's withholding — which is exactly what a
  developer staring at a traceback or a debugger needs, when they're asking
  why an environment variable wasn't applied.
