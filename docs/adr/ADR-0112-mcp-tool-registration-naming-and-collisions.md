# ADR-0112: MCP tool registration naming and collision behavior

- **Status**: Accepted
- **Kind**: Aspirational
- **Area**: actions-tools
- **Date**: 2026-08-08
- **Relations**: extends ADR-0011

## Context

lionagi ships an MCP server whose entire surface is a single tool named `request`, and
lionagi's own MCP client cannot register it alongside any peer that made the same choice.
One of the two silently gets nothing. That is the shortest statement of the problem, and
the rest of this section is how it happens.

`ActionManager.register_mcp_server()` discovers the tools an MCP server advertises and
registers each one as an ordinary `Tool` on the branch. ADR-0011 D3 fixed the registry
as "keyed by provider-visible function name", and fixed that for an MCP tool the single
key of `mcp_config` becomes that name. It did not decide what the key should be, so the
implementation took the only name available at the time: the bare tool name the server
returned from `list_tools()`.

That was correct while a branch talked to one server. It stops being correct the moment
a branch loads two, and `load_mcp_config()` / `load_mcp_tools()` both take a list of
server names and iterate it.

### P1 — Two servers exposing the same tool name silently collapse to one

`register_mcp_server` registers each discovered tool with
`self.register_tool(tool_obj, update=update)`, where `update` defaults to `False`. Per
ADR-0011, a duplicate name in that mode raises `ValueError`. The raise is then caught
and logged (`lionagi/protocols/action/manager.py`, the discovery branch's
`except Exception`), the tool is skipped, and the loop continues. A server whose every
tool name was already taken therefore returns an empty list from a call that did not
fail.

Measured on a two-server configuration where each server exposes its whole surface as a
single tool named `request`, with the servers labeled `alpha` and `beta` here:

```text
[alpha alone]  registry=['request']   load_mcp_config -> {'alpha': ['request']}
[beta  alone]  registry=['request']   load_mcp_config -> {'beta':  ['request']}
[both]         registry=['request']   load_mcp_config -> {'alpha': ['request'], 'beta': []}

surviving entry: {"request": {"server": "alpha", "_original_tool_name": "request"}}
WARNING:root:Failed to register tool request: Tool request is already registered.
```

The branch is missing an entire capability surface. Nothing raised, and the one tool
that exists routes every call to whichever server won.

### P2 — Which server wins is decided by iteration order, not by anything a caller chose

`load_mcp_config` iterates `server_names`, defaulting to the order the config file
declared. So the surviving server is the first-declared one, and moving two lines in a
JSON file changes which half of a branch's capability exists. No caller can reason about
that, and no caller is told which one it got.

This is worst for detection rather than merely bad. A configuration is typically written
with the most-used server first, so the collision costs the *rarely* used surface, and
the failure waits for whichever code path needed the loser.

### P3 — An aggregate tool count cannot detect any of this

The natural guard — assert that tools registered — passes in the failing case. In the
measurement above the registry holds exactly one tool with one server and exactly one
tool with two, and one tool is the *correct* answer for a legitimate single-dispatch
server. Only the per-server breakdown separates the cases, and only because the loser
appears as an empty list.

An assertion on the total is computed over the wrong population. The number is right;
the population is not the one the question was about.

### P4 — The registered name is configuration-dependent even with no collision

A tool the server calls `request` registers as `request` when it is alone. If the fix
for P1 were to qualify names only when they clash, the same tool would register under a
different name as soon as an unrelated second server joined the config. Prompts,
allow-lists, and `tool_names=` arguments that name tools would then be correct or broken
depending on what else happens to be configured — a coupling between two servers that
have nothing to do with each other.

### P5 — The registered name diverges from the spelling every MCP client uses

MCP clients — including the vendor CLIs lionagi drives as providers — expose discovered
tools as `mcp__{server}__{tool}`. lionagi's native registry exposes `{tool}`. So an
instruction written for a CLI-backed branch names a tool the native registry does not
have, and the reverse. The resulting error is "Tool `mcp__alpha__request` is not
registered", which reads as the server being unavailable rather than as a spelling
difference, and costs a debugging session each time.

This matters more as more work moves onto the native loop, because the two paths are
meant to be interchangeable compute for the same instructions.

### P6 — The failure is announced where `lionagi.*` filtering cannot see it

The two diagnostics on this path call the root `logging.warning(...)` rather than the
module's `logger`. A consumer that configures logging for `lionagi.*` — the ordinary
thing to do — sees nothing at all. Combined with a return value that reports the loser
as `[]`, there is no channel by which the collision announces itself.

### P7 — `request_options` is prefixed at one level and read at another

`register_mcp_server` re-keys caller-supplied `request_options` to `{server_name}_{key}`,
then looks entries up by the bare `tool_name`. The lookup never matches, so caller
options are dropped. The prefix is also redundant: `load_mcp_tools` already takes
`request_options_map: dict[str, dict[str, type]]` whose *outer* key is the server, so the
inner dict is server-scoped before it arrives.

Latent rather than live: no in-package caller passes `request_options` today, so nothing
currently reaches it. It is in this ADR because a naming decision that settles the tool
key and leaves this half inconsistent has created a second, quieter version of the same
bug.

### P8 — Bare-name registration across servers is a known attack shape, not only a bug

The MCP specification guarantees tool-name uniqueness **within** a server. It does not
guarantee it across servers, and a client that merges several servers into one flat
registry keyed on bare names is relying on a property the protocol never promised
(`modelcontextprotocol/modelcontextprotocol`, server-tools page, commit `eb0c4e0`).

Published security work treats the consequence as its own class. Invariant Labs described
cross-server tool-name shadowing alongside the "rug pull", where a server changes an
already-approved tool definition after approval. HiddenLayer separately documented
tool-name typosquatting and duplicate-name replacement in MCP clients they tested — their
results are specific to those clients and are not a claim about this one.

What is measured here is lionagi's own behavior. Three shapes were measured, two decided
by a caller-supplied flag and one by the type of the argument. This is an enumeration of
what was measured and not a proof that there is no fourth:

- With `update=False` — the default, and what every in-package caller gets — the
  **first-registered** server keeps the name and a later server silently registers
  nothing. That is P1.
- With `update=True` — a public parameter on both `load_mcp_config` and
  `load_mcp_tools` — `register_tool` skips its duplicate guard entirely and replaces the
  entry, so a **later-declared** server silently displaces an already-registered tool of
  the same name. Calls a caller believes are going to the first server go to the last one.
- **With a one-entry MCP dict, regardless of the flag.** `ActionManager.__contains__`
  (`manager.py:67-74`) has arms for `Tool`, `str` and callable and returns `False`
  otherwise. A dict is a member of `FuncTool`, so `tool in self` at `:77` is `False` for
  a dict even when that name is registered, the `update=False` guard passes, and `:106`
  overwrites. The displacement is the same as the `update=True` case and it needs no
  flag. ADR-0011 lines 330-332 record this behavior; it is re-verified at this head.

The second and third are the shadowing shape. Both are reachable without any private API:
`update=True` is documented on two public functions, and the dict form is reachable
through `Branch.register_tools([{...}])` (`branch.py:586` → `:581`). All three are decided
by config ordering or argument type rather than by anything a caller expressed, and none
announces itself.

**Scope of the third, stated honestly.** A caller grep across the package finds **zero**
in-package call sites passing a dict to `register_tool` — the seven callers pass a
variable bound to a `Tool` or a callable. So the dict shape is a latent property reachable
through the public API rather than a live defect in current internal use. That is the same
standing this document already grants the `update=True` shape.

This ADR does not claim to make MCP tool loading secure — descriptions and schemas are
still server-supplied text forwarded to a model, which is a separate problem this document
does not touch. It removes one specific mechanism, and it does not remove all three shapes
by the same means: D1 makes the names unable to coincide, which closes the first two, and
the dict shape needs its own one-line fix because it is a hole in the duplicate guard
rather than a naming collision (delta #8).

### Why this is not a niche configuration

lionagi itself ships an MCP server whose entire surface is one tool named `request`
(`lionagi/mcp/server.py`, `@mcp.tool async def request(...)`), a shape ADR-0066 chose
deliberately: one tool, generated per-verb schemas, `help` for discovery. It is a good
design and it is being adopted elsewhere for the same reasons. Single-dispatch surfaces
converge on the same small vocabulary — `request`, `query`, `search`, `call` — precisely
because that vocabulary is the right one.

So collision probability rises with the number of well-designed servers a branch loads.
lionagi's own MCP server cannot currently be registered alongside any peer that made the
same choice, in lionagi's own client registry.

| Concern | Decision |
|---------|----------|
| Registered name for MCP-derived tools | D1: always `mcp__{server}__{tool}`, whether or not anything collides. |
| Name sent on the wire | D2: unchanged — the server's own tool name, carried in `_original_tool_name`. |
| Partial registration | D3: per server, registered count must equal advertised count, or the load raises. |
| Diagnostics and return shape | D4: module logger; the return distinguishes "advertised nothing" from "failed to register". |
| `request_options` keying | D5: keyed by the server's own tool name inside the already-server-scoped map; one helper derives the write and the read. |
| Name constraints | D6: the qualified name is validated at registration against the strictest provider constraint; over-length fails, never truncates. |

This ADR does **not** decide:

- The naming any *server* advertises, including lionagi's own — ADR-0066 owns that
  surface and it is not changing.
- Admission and permission policy for MCP tools — that is `validate_mcp_tool_admission`
  and the security config in `lionagi/service/connections/mcp_wrapper.py`.
- Whether CLI-provider allow-lists are rewritten to match. `ClaudeCodeRequest.mcp_tools`
  is a legacy field, excluded and unused in CLI argument construction; `--allowedTools`
  spellings are passed through by the caller. D1 makes the two vocabularies agree, which
  is the point, but nothing in the provider path is changed here.
- Invocation, hooks, event lifecycle, or history ordering — ADR-0012.
- The trust status of server-supplied tool descriptions and schemas. Those are forwarded
  into model-visible metadata and are a separate problem with a separate remedy; D1
  closes the name-occupancy mechanism in P8 and nothing else about it.

## Decision

### D1 — MCP-derived tools register under `mcp__{server}__{tool}`, always

**The contract.** In `register_mcp_server`, the key of the one-entry `mcp_config`
dictionary — which ADR-0011 fixed as the registered, provider-visible function name —
becomes the qualified name:

```python
def qualified_mcp_name(server_name: str, tool_name: str) -> str:
    """The registry/provider-visible name for a tool discovered on an MCP server."""
    return f"mcp__{server_name}__{tool_name}"
```

The name matters and is not `mcp_tool_name`. That identifier is already bound to a `str`
on this exact path — a local in `_validate_prebuilt_mcp_tool_admission`
(`manager.py:114`, read at `:117` and `:132`) and a parameter of
`is_synthetic_mcp_wrapper_schema` (`mcp_wrapper.py:1423`). A module-level function of that
name would be shadowed inside the one function that most needs it, and a later call from
there would raise `TypeError: 'str' object is not callable` at runtime and only at
runtime, on the admission path. Rename the helper rather than the locals: the locals are
correct, since what they hold really is the tool's name on the wire.

Both construction paths use it — the discovery branch and the metadata-free
`tool_names=` shortcut — so a caller who names tools explicitly and a caller who
discovers them get the same registry.

**Exact semantics.**

- Applies unconditionally. A single-server configuration gets qualified names too. The
  name a tool registers under is a function of that server and that tool alone, never of
  what else is configured (this is the whole of P4, and the reason D1 is not
  "qualify on collision").
- `tool_names=["request"]` names the *server's* tool. The caller passes the wire name
  and receives a registry entry under the qualified name. The argument is not the
  registry key and never was — it is the discovery filter.
- `Branch.acts.registry` keys, `get_tool_schema(tools=[...])` lookups, and the
  `function` field of every provider schema all carry the qualified name.
- `register_mcp_server` returns the **qualified** names, and so `load_mcp_config`'s
  `dict[server, list[str]]` carries qualified names in its values. The return value is
  what a caller uses to address a tool, so it speaks the registry's vocabulary. The wire
  names are not returned; they are recoverable from each `Tool`'s `_original_tool_name`.

That last point is load-bearing rather than a formality. The one in-package consumer of
the returned mapping, in `lionagi/agent/factory.py`, feeds those names straight back into
the registry to attach the spec's hook chain:

```python
for tool_names in loaded.values():
    for tool_name in tool_names:
        tool = branch.acts.registry.get(tool_name)
        if tool is not None:
            _attach_hooks(tool, spec, tool_name)
```

Returning wire names would make every lookup miss, `tool` would be `None` for every MCP
tool, and the loop would attach nothing — with no error, because the `is not None` guard
treats a miss as an ordinary skip. Every MCP tool on every agent built by the factory
would quietly lose its permission and logging hooks. The vocabularies must match, and the
one that must win is the registry's.

Directly-held MCP clients are unaffected, because they never consult the registry.
`lionagi/tools/khive_injection.py` calls `client.call_tool("request", ...)` against a
client object; that is a wire name on a wire call and D2 leaves it exactly as it is.
- Two servers advertising the same tool name no longer collide, because their qualified
  names differ.
- A collision after qualification means either two servers configured under one name
  (impossible within a single config mapping) or a locally-registered function tool
  already occupying an `mcp__*__*` name. Both are genuine anomalies and are handled by
  D3 rather than skipped.

**Why this way.** Four properties decided it.

*The protocol only ever guaranteed per-server uniqueness.* Qualifying by server is not a
lionagi convention layered on top of MCP; it restores the scope the spec actually defines.
A bare-name registry silently widens a per-server guarantee into a global one, and P8 is
what that costs.

*It is the convention that already exists.* MCP clients expose `mcp__{server}__{tool}`.
Adopting it means one spelling works whether an instruction runs on the native loop or
on a vendor CLI, which is the direct answer to P5, and it means a model that has seen
MCP tools elsewhere recognizes the shape.

*The separator is unambiguous.* With a single underscore, server `foo` + tool `bar_baz`
and server `foo_bar` + tool `baz` produce the same string. A double underscore is rare
enough inside real tool names that the decomposition stays readable, and D6 rejects the
pathological case rather than letting it collide quietly.

*It costs one dict key.* See D2 — the indirection that makes this cheap is already in
the code.

### D2 — The wire name is unchanged, and the mechanism already exists

**The contract.** `register_mcp_server` already writes the server's own tool name into
the config it stores:

```python
config_with_metadata = dict(server_config)
config_with_metadata["_original_tool_name"] = tool_name
mcp_config = {registry_name: config_with_metadata}
```

and the invoker already prefers it:

```python
# lionagi/service/connections/mcp_wrapper.py
actual_tool_name = mcp_config.get("_original_tool_name", tool_name)
```

**Exact semantics.**

- `_original_tool_name` is set on both construction paths today, unconditionally
  (`manager.py:361`, `:399`). It is currently a no-op because it is always equal to the
  registry key.
- It has **two** readers, not one. The invoker above (`mcp_wrapper.py:1840`) is the
  routing reader. The second is `_validate_prebuilt_mcp_tool_admission`
  (`manager.py:115`), on the security path, with its own fallback to the config key.
  Its behavior under D1 is already correct — it prefers the wire name, which is what
  admission should be checking — but a reader auditing that path should not have to
  discover the field's second site for themselves.
- After D1 the two names differ, and the existing fallback (`, tool_name`) is never taken
  for an MCP-derived tool. The fallback stays for hand-built `mcp_config` values.
- No change to the invoker, the connection pool, or any transport.

**Why this way.** This is the fact that makes D1 a small change rather than a risky one.
The registered name and the wire name were separated when this path was written; the
separation has simply never been used. Renaming is therefore a change to what a *model*
is shown, not to what a *server* is sent. Any design that could not preserve the wire
name would have had to negotiate with every server, which is not a thing a client gets
to do.

**And that is exactly why it needs its own test.** "The indirection already exists" is a
statement about shape, not about behavior: `_original_tool_name` has been written on every
tool and read by the invoker while the two names were always equal, so the passthrough has
never once been exercised with a value that differs. Code that has never run its
interesting case is untested code wearing a reassuring shape.

The failure it hides is also badly placed. A collision test proves the registry is fixed
and says nothing about the wire. If D1 prefixes and D2's passthrough is subtly wrong, the
server receives `mcp__alpha__request`, rejects it as an unknown tool, and the error surfaces
as a fault in the *server* — the one place a reader will not look, because the client change
is the thing that just landed. One assertion prevents that entire investigation: in a single
call, the name arriving at the server equals the bare `request` while the registry key is the
qualified form. Two different strings, one call, one test.

### D3 — Registration is complete or it fails

**The contract.** `register_mcp_server` knows the advertised count — it is
`len(await client.list_tools())` — and returns the registered names. The postcondition
is equality:

```python
tools = await client.list_tools()
advertised: set[str] = {t.name for t in tools}
registered_wire_names: set[str] = set()
# ... each successful registration adds the server's own name for the tool
if registered_wire_names != advertised:
    missing = sorted(advertised - registered_wire_names)
    raise RuntimeError(
        f"MCP server {server_name!r} advertised {len(advertised)} tool(s) but "
        f"{len(registered_wire_names)} registered; missing: {missing}"
    )
```

The comparison is over the **wire** names, because that is the vocabulary
`list_tools()` speaks. `registered_tools` — the qualified names the function returns per
D1 — is a parallel list, not the thing compared.

**Exact semantics.**

- The predicate is **registered == advertised**, per server. It is deliberately not
  "registered > 0": a server that legitimately advertises no tools (a resources-only
  server) is not an error, and a non-empty check would fail it while still passing every
  case where a server advertises three tools and registers one.
- Server-set selection precedes acquisition. When a caller supplies an already-resolved
  set (including the explicit no-config case), that selection is authoritative:
  factory-side native discovery is skipped, so a server outside the set never reaches
  `MCPConnectionPool.get_client` and is neither connected nor recorded as an empty
  registration. D3 applies only after a server has been selected for registration.
- A server advertising zero tools registers zero and succeeds.
- `PermissionError` continues to propagate unchanged — an admission denial is already
  fail-closed and is not a shortfall.
- The pre-validation loops that check every descriptor before mutating the registry stay
  as they are. They already guarantee that an admission denial leaves the registry
  untouched; D3 covers the *other* failure, where admission passed and registration did
  not.
- In `load_mcp_config`, a raising server is reported and re-raised rather than recorded
  as `[]`. Callers that want partial success ask for it explicitly rather than receiving
  it by default.
- **The raise happens in the earliest process that can still return it to the caller.**
  This is a placement requirement, not a detail. A predicate that raises correctly but
  inside a spawned child process produces a submit that returns success while the child is
  already dead, leaving the reason only in a console log — the caller sees a running job
  with half its tools and no signal. Config loading happens in the submitting process, so
  the check has a sound place to live and there is no reason to defer it. An acceptance
  criterion of "the predicate raises" is satisfied by the broken arrangement; the criterion
  is "the predicate raises where the caller can still receive it."

**Why this way.** P3 is the reason this is a postcondition inside the function rather
than advice to callers. The information needed to detect the failure — what the server
said it had — exists only here, for one line, and is discarded. Every guard a caller
could write downstream is computed over a population that no longer distinguishes the
cases. A check belongs where the evidence is.

The counting is over tool *names* advertised versus registered, not over the return
list's length alone, so that a future path registering a tool twice cannot satisfy the
count with a duplicate.

### D4 — Diagnostics go to the module logger, and the return value is unambiguous

**The contract.** The two `logging.warning(...)` calls on this path become
`logger.warning(...)` on the existing module logger. `load_mcp_config`'s return type is
unchanged (`dict[str, list[str]]`) — after D3 an empty list means the server advertised
nothing, which is now its only meaning.

**Exact semantics.**

- Schema-extraction failure for one tool stays a warning: the tool still registers, with
  `tool_schema=None`, which is a degradation and not a loss.
- Registration failure is no longer a warning at all — it raises, per D3.
- `[]` in the returned mapping means "this server advertises no tools", full stop.

**Why this way.** P6. A diagnostic on the root logger is invisible to the standard way of
configuring logging for this package, so the one channel that could have announced the
collision was closed by a two-character difference. Fixing the sink matters less than D3
does — a raise does not need a log to be noticed — but leaving a root-logger call on a
path this ADR is rewriting would preserve the defect for the next failure that arrives
here.

### D5 — `request_options` is keyed by the server's own tool name, once

**The contract.**

```python
async def load_mcp_tools(
    config_path: str | None = None,
    server_names: list[str] | None = None,
    request_options_map: dict[str, dict[str, type]] | None = None,   # {server: {tool: type}}
    update: bool = False,
    mcp_security: "MCPSecurityConfig | None" = None,
) -> list[Tool]: ...
```

The inner mapping is keyed by the tool name **as the server advertises it**. The
`{server_name}_{key}` re-keying loop in `register_mcp_server` is removed, and the lookup
reads the same key it was given.

**Exact semantics.**

- `request_options_map` is already server-scoped by its outer key, so the inner key needs
  no server component. The removed loop was prefixing at a level that was already
  disambiguated.
- Callers who pass `request_options` directly to `register_mcp_server` pass the same
  shape: `{tool_name: type}`, bare.
- A key matching no advertised tool is a caller error and is reported at registration
  rather than dropped, on the same principle as D3: options silently not applied are
  indistinguishable from options that had no effect.
- The registered name is derived by `qualified_mcp_name()` in exactly one place, and the
  options lookup uses the wire name in exactly one place. Neither is reconstructed by
  string-building at a second site.

**Why this way.** P7. The existing pair is a worked example of the failure this whole
ADR is about: two halves of one convention, written independently, that disagree. The
remedy is not a matching prefix on the read side — it is that only one of the two sites
gets to decide the spelling.

### D6 — The qualified name is validated at registration

**The contract.** Provider function-name constraints are the binding limit, and the
package already encodes one: `lionagi/providers/anthropic/messages.py` declares a tool
name as `min_length=1, max_length=64, pattern="^[a-zA-Z0-9_-]+$"`. A qualified name is
checked against that same rule when it is constructed.

Other providers are not assumed to be looser. The rule is applied uniformly rather than
per-provider, because a tool is registered once and may be sent to any model the branch
later uses — a per-provider check would pass at registration and fail at the call.

**Exact semantics.**

- Over-length (`len(qualified) > 64`) raises at registration, naming the server, the
  tool, and the resulting length. It does not truncate: a truncated name collides with
  its own siblings, which is the defect this ADR exists to remove, reintroduced in a form
  that is harder to see.
- A character outside the allowed set raises with the same information. Servers with
  such names are already unusable on those providers; failing at registration reports it
  once, at the place with the context to explain it, rather than as a provider rejection
  mid-run.
- The remedy named in the error is to load that server under a shorter alias — the config
  key is the caller's to choose, and it is the component the caller controls.
- `mcp__` (5) + `__` (2) leaves 57 characters for server and tool together. For
  reference, the motivating case — `mcp__` + a 5-character server + `__` + `request` —
  is 19.

**Why this way.** Qualification makes names longer, so it introduces a limit that bare
names never approached. A limit discovered at provider-call time surfaces as an opaque
400 several layers from the cause. The check is cheap and it happens once per tool per
load.

## Consequences

**A branch can load more than one MCP server and get all of them.** This is the point,
and today it is not true for any pair of single-dispatch servers.

**Every MCP tool name changes.** This is a breaking change to what models are shown and
to what callers may name. A prompt saying "call the tool named `request`" stops working;
it becomes `mcp__alpha__request`. There is no compatibility flag, deliberately — a flag
that restores bare names restores order-dependent silent capability loss, and a defect
kept behind a default is a defect the next reader has to rediscover. The migration is a
release note and a mechanical rename, and the package is pre-1.0.

**The cost falls outside the package, not inside it.** No in-package code addresses an
MCP tool by bare registry name: the one direct use of the name `request`
(`lionagi/tools/khive_injection.py`) is a wire call on a held client and is unaffected,
the `tool_names=` shortcut has no callers, and the factory's hook loop consumes whatever
vocabulary the loader returns. So the change is carried by users' prompts and allow-lists
rather than by internal churn — which is also the reason the change is worth making now
rather than later, when there are more of both.

**Instructions become portable between the native loop and CLI providers.** One spelling
is valid on both. This is the consequence that pays for the rename.

**A misconfigured server now fails loudly at load.** Callers who were getting a partly
populated branch and did not know it will start seeing errors. That is the correct
direction, and it will look like a regression to anyone whose configuration has been
quietly broken.

**One name-occupancy mechanism closes for two of P8's three shapes.** After D1 no server
can take a name another server needs, so neither the default `update=False` capability
loss nor the `update=True` displacement remains reachable through tool naming. The dict
shape is **not** closed by D1 and does not need to be closed by naming: it is a hole in
the duplicate guard, where `__contains__` is annotated `FuncToolRef` (which includes
`dict`) and silently answers `False` for one of the types its own signature admits.
Delta #8 closes it with the missing arm. Leaving D1 to carry all three would have been
the wrong mechanism for the third, and the ADR previously claimed a completeness it did
not have.

This is a narrowing, not a security property: server-supplied descriptions and schemas
still reach the model unchanged, and D3's fail-closed behavior matters here — a partially
registered server was previously indistinguishable from a fully registered one.

**Relation to ADR-0011 delta #2, stated rather than left silent.** 0011 delta #2 moves MCP
configuration, discovery, namespacing and pool lifecycle into a service-owned factory, and
its acceptance has three clauses: `protocols.action` carries no service-layer import,
remote identities are collision-free, and per-tool request models resolve by canonical
identity without key mutation or silent fallback. The disposition of that delta under this
ADR is **narrowed, not discharged**:

- The identity clause is discharged by D1, at the registry layer.
- The request-options clause is discharged by D5, at the registry layer.
- The **dependency-direction clause is untouched and remains open in full**, and it is
  larger than a single site. `manager.py` imports from
  `lionagi.service.connections.mcp_wrapper` at **eight** places (`:10`, `:93`, `:109`,
  `:325`, `:343`, `:377`, `:445`, `:489`) — one under `TYPE_CHECKING` and seven lazily
  inside function bodies. The lazy form defers the import; it does not remove the
  dependency, which is what 0011's clause is about. The service-factory design remains
  the standing answer for that clause, and its cost is eight call sites rather than the
  one this document's own reasoning would have suggested.

D1 and D5 are not placeholders and should not be re-implemented in a service factory
later. What a service factory would still buy is the import direction, and that is the
part of 0011 delta #2 this document does not do.

**Contributors must know one rule**: for an MCP-derived tool, the registry name and the
wire name are different, the first comes from `qualified_mcp_name()`, and the second lives in
`_original_tool_name`. Any new code that reconstructs either by string-building has
reintroduced P7.

**New failure mode:** a server loaded under a long config key can now fail at
registration where it previously succeeded (D6). The error names the remedy.

**Reversal cost.** D1 is one function and its two call sites, plus tests — reversible in
an afternoon, at the cost of reinstating P1-P5. D3 is a postcondition; reversing it
restores silent partial registration. D2 and D6 have no independent existence without
D1. D5 is independent of the rest and worth keeping under any outcome.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Add `qualified_mcp_name()` and use it for the `mcp_config` key on both construction paths in `register_mcp_server`; assert `_original_tool_name` carries the wire name. Acceptance: two servers each advertising `request` both register, and each routes to its own server. | S | #2921 |
| 2 | Add the advertised-vs-registered postcondition to `register_mcp_server`; stop recording a raising server as `[]` in `load_mcp_config`. Acceptance: a server advertising N tools that registers fewer raises and names the missing ones; a server advertising zero succeeds; and the raise reaches the **submitting** process — asserted by a caller that receives the error as a return, not by reading a child's console. | S | #2921 |
| 2b | Assert the wire name is unchanged under qualification: one call where the name delivered to the server is bare `request` while the registry key is `mcp__{server}__request`. Acceptance: the test fails if `_original_tool_name` passthrough is removed or inverted. | XS | #2921 |
| 3 | Move the two root-`logging` calls on this path to the module logger. Acceptance: a consumer configuring only `lionagi.*` sees the schema-extraction warning. | XS | #2921 |
| 4 | Remove the `{server_name}_` re-keying of `request_options`; key the lookup by the advertised tool name; report keys matching no advertised tool. Acceptance: a caller-supplied `request_options` entry reaches the constructed `Tool`. | S | #2921 |
| 5 | Validate the qualified name against `^[a-zA-Z0-9_-]{1,64}$` at construction, with an error naming server, tool, length, and the alias remedy. Acceptance: an over-long server key fails at load, not at provider call. | XS | #2921 |
| 6 | Regression test: a two-server configuration where both advertise the same tool name, asserting per-server registration and that each qualified name dispatches to its own server. Acceptance: the test fails on the pre-change code. | S | #2921 |
| 7 | Make the hook-attachment loop in `agent/factory.py` loud when a returned name is absent from the registry, instead of skipping on `if tool is not None`. Acceptance: a name in the returned mapping that the registry does not hold raises rather than silently leaving that tool without its spec hooks. | XS | #2921 |
| 8 | Add the missing `dict` arm to `ActionManager.__contains__`, so the duplicate guard covers every type its `FuncToolRef` annotation admits. This is P8's third shape and D1 does not close it. Acceptance: `register_tool({name: cfg}, update=False)` against an already-registered `name` raises instead of overwriting, and the same call with `update=True` still replaces — the second arm is what proves the fix did not simply make registration stricter for everyone. | XS | #2921 |

## Alternatives considered

**Keep bare names, first registration wins (status quo).** Buys nothing, and is the
behavior described in P1-P6. It survived this long because the single-server case works
and the multi-server case fails in the one direction that produces no error. Rejected on
the measurement: a whole capability surface disappears from a call that returns
successfully.

**Qualify only on collision.** Would have bought a much smaller migration: every existing
single-server prompt keeps working, and only genuinely ambiguous names change. Rejected
because the name a tool gets would then depend on what *else* is configured (P4). A
prompt naming `request` would work until an unrelated second server was added to the
config, and the resulting break would appear to have nothing to do with the change that
caused it. Configuration-dependent names also make `tool_names=` and any allow-list
un-writable in advance. The migration saving is real; it buys a coupling that never goes
away.

**`{server}_{tool}` as the separator.** This is the convention already present in the
file, in the `request_options` re-keying, so it had a consistency argument. Rejected on
two grounds. It is ambiguous — `foo` + `bar_baz` and `foo_bar` + `baz` produce one
string — and the consistency it matches is with an internal plumbing key that D5 removes,
not with anything model-facing. Losing to `mcp__` also costs nothing, since `mcp__` is
what other clients already emit.

**Register the qualified name plus a bare alias when unambiguous.** Would have bought
backward compatibility for existing prompts while fixing collisions. Rejected because the
alias's *existence* is configuration-dependent, so it reproduces P4 in a softer form: the
bare name works until a second server appears, then vanishes. It also doubles registry
entries, so every count over the registry — including any guard written against D3 —
would need to know which names are aliases.

**Let the caller supply a prefix per server.** Would have bought flexibility for callers
with existing name conventions. Rejected as a decision the library should make once: an
optional convention is not a convention, and every caller that skipped the option would
be back at P1. The config key already gives the caller the only control that matters
(D6's remedy), without making the default unpredictable.

**Ask colliding servers to rename their tools.** The cheapest fix if it were available:
no client change at all. Rejected because it is not the client's call. For a server whose
single-tool dispatch surface is a published wire contract, the tool name is the interface
— every existing client, document, and stored instruction names it — and renaming it to
work around a limitation in one client's registry is a breaking change imposed on the
wrong party. It also does not scale: the vocabulary these surfaces converge on is small
because it is the right vocabulary, so each new well-designed server raises collision
probability and this remedy asks one more maintainer to accept a worse name.

**DEFERRED — decompose the qualified name back to `(server, tool)` for display.** Tools
in traces and Studio views will render as `mcp__alpha__request`, which is correct but
noisy. A display-time split on `__` would show server and tool separately. Deferred
rather than decided because it is a presentation choice owned by the surfaces that render
tool calls, and because the split is only safe under D6's charset rule — worth revisiting
if the rendering proves to be a real irritant.

## Notes

The `mcp__{server}__{tool}` convention is not invented here. It is what MCP clients
already expose to models, and D1's main claim is that a library whose own agents run on
both native loops and vendor CLIs should not maintain a second spelling for the same
tool.

`_original_tool_name` predates this ADR and is why D1 is cheap. Whoever added it
separated the registered name from the wire name before there was a reason to; this ADR
supplies the reason.
