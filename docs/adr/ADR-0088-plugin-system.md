# ADR-0088: Plugin system (directory-bundle manifest with lazy activation)

- **Status**: Accepted
- **Kind**: Aspirational
- **Area**: substrates
- **Date**: 2026-07-12
- **Relations**: builds on ADR-0047 (hook mechanism scopes) and ADR-0048 (external
  hook contract — a plugin's hook block is delivered through it, including its D7
  trust gate); none superseded

## Context

LionAGI already has six independent extension seams, each with a different shape,
discovery timing, and trust model:

1. **Agent profiles** — markdown + YAML frontmatter under `.lionagi/agents/`
   (project) and `~/.lionagi/agents/` (global), discovered by directory scan via
   `find_lionagi_dirs()`, first match wins. Data-only, no install step. The most
   mature "drop a file, it works" seam.
2. **Playbooks** — `~/.lionagi/playbooks/*.playbook.yaml`, discovered by glob at
   `li play` time. Same drop-a-file shape, but **global-only** (no project-local
   `.lionagi/playbooks/`, no `find_lionagi_dirs()` cascade), and the discovery is
   split across two sites: a pre-argparse CLI branch resolves the playbook name
   and rewrites argv, and the downstream flow subcommand resolves the file again.
3. **Tools** — imperative runtime registration (`branch.register_tools`,
   `ActionManager.register_mcp_server`). No declarative form, no discovery.
4. **Hooks** — a name→callable registry (`lionagi/hooks/loader.py:_REGISTRY`,
   last-writer-wins), plus the settings-driven tool-hook shape, plus (with ADR-0048)
   external command hooks.
5. **Providers/endpoints** — `@register_endpoint` self-registration at import time,
   with a hardcoded built-in module list (`_import_all_providers`) walked once on
   first `match_endpoint`. The decorator already works for any module that gets
   imported; what is missing is only *who imports third-party modules*.
6. **Casts roles/modes** — an explicitly closed built-in set; packs overlay behavior
   on existing roles but cannot add roles.

There is no unit that groups these into one installable, listable, versionable,
trustable thing. The practical consequences:

- **P1 — no distribution unit.** Sharing "my agent profile + its guard hook + the
  playbook that drives it + the provider it needs" means a README with four manual
  copy steps into three directories plus an import-side-effect arrangement for the
  provider. There is nothing to hand a user.
- **P2 — no discovery for code capabilities.** A third-party provider endpoint
  works if and only if something imports its module before first
  `match_endpoint()`. Nothing owns that import today; users get "provider not
  found" with a working package installed.
- **P3 — no inventory or lifecycle.** Nothing answers "what extensions are active
  in this project?" — extensions are invisible until they fire. There is no
  disable-without-delete, no version-compatibility check, and no uninstall other
  than hunting files.
- **P4 — no trust boundary.** Every existing seam either runs nothing external
  (data-only seams) or runs whatever is present (import-side-effect providers,
  last-writer-wins hook registry). A bundle of third-party capability needs an
  explicit approval step before its code executes in-process.
- **P5 — a plugin concept already half-exists, for the wrong system.** Studio ships
  a read-only viewer for *Claude Code* plugin bundles (directory + manifest +
  `skills/`/`agents/` subdirectories), and the repo ships one such bundle. The
  directory-bundle shape is proven in this codebase; what does not exist is a
  bundle that extends *LionAGI's own runtime*.

Hard constraint from the architecture: **import-time O(1)**. `lionagi/__init__.py`
is lazy by design; plugin discovery must not import plugin code, and must not even
run at `import lionagi` at all.

| Concern | Decision |
|---------|----------|
| What a plugin is | D1: a directory bundle with a `plugin.yaml` manifest under `.lionagi/plugins/` |
| Manifest schema | D2: declarative capability manifest; pure data, no code references executed at parse |
| Activation timing | D3: manifest-only discovery on first registry miss; code import deferred to capability first-use |
| Pluggable surfaces (v1) | D4: tools, external hooks, agent profiles, playbooks, providers |
| Trust | D5: content-pinned trust record (manifest + every declared capability file) required before any plugin code, command, profile, or playbook is used |
| Collisions | D6: built-ins always win; plugin-vs-plugin same-name is a hard load error |
| Lifecycle | D7: `li plugin list/info/trust/enable/disable`; uninstall is directory removal |
| Python-package plugins | D8 (DEFERRED): `lionagi.plugins` entry-points group resolving to the same manifest |

Out of scope, each deliberately:

- **CLI subcommands as plugin capability** — the argparse tree is statically built
  with pre-parse interception branches; a lazy subcommand registry is real
  architectural work on `cli/main.py` that should not ride a plugin ADR. Revisit
  once the manifest shape has proven itself.
- **Casts roles/modes** — closed by an explicit recorded design decision
  (`casts/pattern.py`); packs remain the extension surface for cast behavior. A
  plugin may ship a pack file; it may not add roles.
- **Schedule `action_kind` values** — dispatch is string-compared across several
  studio/scheduler sites with no registry; making that pluggable requires the
  registry to exist first (its own change, in scheduling-control-plane).
- **Sandboxed execution of plugin code** — `SandboxSession` provides git-worktree
  *filesystem* isolation, not process or capability isolation; advertising it as a
  plugin security boundary would be false. Trust (D5) is the boundary in this
  design; process-level isolation is a possible future hardening with its own ADR.
- **A remote marketplace/index** — distribution beyond "a directory you obtained"
  is a product decision, not runtime architecture.

## Decision

### D1 — A plugin is a directory bundle with a manifest

```text
.lionagi/plugins/<name>/
  plugin.yaml            # the manifest (D2) — required
  tools/                 # python modules for declared tools
  hooks/                 # hook commands (executables) referenced by the manifest
  agents/<n>.md          # agent profiles, same format as .lionagi/agents/
  playbooks/*.playbook.yaml
  providers/             # python modules using @register_endpoint
  packs/*.yaml           # casts pack overlays
```

- Discovery scans `<dir>/plugins/*/plugin.yaml` for each directory yielded by
  `find_lionagi_dirs()` — project `.lionagi/` first, then `~/.lionagi/`, the exact
  precedence agent profiles already use. No new path convention.
- A directory without `plugin.yaml` is ignored (not an error — it may be a work in
  progress). A `plugin.yaml` that fails schema validation is a load-time diagnostic
  naming the file and field, and the plugin is excluded (never partially loaded:
  half a plugin is harder to reason about than none).
- The bundle is self-contained: manifest references are relative paths inside the
  bundle; a manifest entry that points outside its own directory
  (path-traversal-checked with the existing `has_traversal` helper) is a validation
  error. This is what makes "uninstall = delete the directory" true (D7).
- Why directory-bundle first: it matches the two proven discovery seams in this
  codebase (agent profiles, playbooks) and the Studio-side CC-bundle reader; it
  needs no packaging knowledge from plugin authors; and it can carry data-only
  bundles (profiles + playbooks + packs) that have no importable Python at all —
  the case a pip-package-only design cannot express cleanly.

### D2 — Manifest schema: declarative, pure data

```yaml
# plugin.yaml
name: web-research            # ^[a-z0-9][a-z0-9-]{0,31}$ — doubles as namespace
version: "0.3.0"              # plugin's own version, informational
description: Web research toolkit
lionagi: ">=0.30,<1.0"        # PEP 440 specifier set — compatibility gate

capabilities:
  tools:
    - name: web_search        # registered tool name
      target: tools/search.py:web_search    # module-path:callable inside the bundle
    - name: fetch_page
      target: tools/fetch.py:fetch_page
  hooks_external:             # ADR-0048 D6 shape, verbatim
    PreToolUse:
      - matcher: "web_search|fetch_page"
        hooks:
          - type: command
            command: ["hooks/rate_guard"]   # relative to bundle root
  agents: [agents/researcher.md]
  playbooks: [playbooks/deep-research.playbook.yaml]
  providers:
    - module: providers/searchapi.py        # imported lazily; self-registers
  packs: [packs/research.yaml]
```

Exact semantics:

- Parsing the manifest imports nothing and executes nothing: `target` and `module`
  are strings, resolved only per D3. This is the same deferred-reference idea as the
  provider config layer's `LazyType` (`"module:Class"` strings resolved on demand),
  extended to bundle-relative paths.
- `lionagi:` is checked at discovery against the installed version; out-of-range
  plugins are listed by `li plugin list` as `incompatible` with the failing
  specifier, and none of their capabilities load. Skip-with-visible-reason rather
  than hard error: an incompatible plugin must not break the session that merely
  walked past it.
- `name` is the namespace for everything the plugin registers (see D6). Version
  ranges between *plugins* are not resolved — there is no inter-plugin dependency
  field in v1; a plugin that needs Python packages states them in its own docs or
  ships as D8 later. Building a dependency resolver for directory bundles
  duplicates pip badly; declining it keeps the loader honest about what it is.
- Unknown top-level or capability keys are load-time errors (catch typos like
  `playbook:` early), except keys prefixed `x-` which are reserved for
  user/vendor annotation and ignored.

### D3 — Lazy activation: manifests at first need, code at first use

Two-stage laziness, mirroring the endpoint registry's `_ensure_loaded` pattern:

- **Stage 1 — discovery (cheap, data-only).** The plugin registry scans and parses
  manifests the first time any consumer asks it anything. Consumers and their
  trigger points:
  - `ActionManager` tool-name miss → "does a trusted, enabled plugin declare this
    tool?"
  - provider resolution: **`EndpointRegistry.match()` never misses today** — on no
    registered match it silently falls through to a generic OpenAI-compatible
    endpoint, so there is no existing miss event to hook. This decision therefore
    requires a small, named refactor of the registry: an interception point after
    "no registered entry matched" and **before** the generic fallback, which
    consults the plugin registry, imports any declared provider module for that
    provider name (the module self-registers at import time via the existing
    decorator, so importing before the fallback is sufficient — no registry
    reload), re-runs the match once, and only then falls back. Without this
    interception a plugin provider would silently receive the generic endpoint
    instead of its own — a wrong-answer failure, not an error.
  - agent-profile resolution miss → plugin `agents/` entries join the search list
    after project and global profiles.
  - playbooks: discovery today is global-only and split across a pre-argparse CLI
    branch (name resolution + argv rewrite) and the downstream flow resolver.
    Plugin-playbook support therefore lands **with** a unification of playbook
    discovery onto `find_lionagi_dirs()` (project, global, then plugin dirs), and
    both resolution sites consult the unified scan. A `<plugin>/<name>` token (D6)
    is passed through the argv rewrite as an opaque playbook identifier — the
    rewrite branch must not split on `/` — and resolved only at the unified scan.
  - session-bus construction (`build_session_bus`) and agent-factory hook wiring →
    plugin `hooks_external` blocks join the ADR-0048 loader input.
  - `li plugin *` commands → full scan, eagerly.
- **Stage 2 — activation (code, per capability).** A declared `target`/`module` is
  imported only when that specific capability is actually invoked or matched —
  never as a side effect of discovery, listing, or an unrelated capability of the
  same plugin firing.
- `import lionagi` alone triggers neither stage. Nothing at module import time
  touches the plugin registry; the import-time O(1) invariant holds by
  construction, not by discipline.
- Failure cases: a `target` whose file is missing or whose callable name is absent
  raises at first use with a diagnostic carrying plugin name + manifest path (not
  an ImportError three frames deep); a provider module that raises on import is
  reported once, cached as failed, and does not retry per call. The failed-import
  cache is **new infrastructure** — the built-in provider auto-loader swallows
  ImportError per module with no memory of the failure, so the plugin loader
  builds its own (a per-plugin `{target: error}` map held by the plugin registry),
  it does not extend an existing one.

### D4 — v1 pluggable surfaces

Exactly five: **tools, external hooks, agent profiles, playbooks, providers** —
plus pack files as data. The selection rule is: every v1 surface extends a seam
that already exists (registration call, directory scan, or registration decorator);
no v1 surface requires building a new registry inside a subsystem that lacks one.
Tools/profiles/playbooks/packs are the low-lift group (existing dynamic seams);
providers are included despite being medium-lift because they are the single
highest-value plugin case (a new LLM backend without a lionagi PR) and the missing
piece is precisely what a manifest supplies: the name of the module to import.
CLI subcommands and schedule kinds are excluded by that same rule (their registries
do not exist), and roles by recorded design decision — see out-of-scope.

### D5 — Trust: nothing executes before an explicit trust record

- A plugin arrives untrusted. Untrusted means: fully visible in `li plugin
  list`/`info` (rendered from manifest data only), and completely inert — no code
  import, no command execution, no profile/playbook exposure (a poisoned system
  prompt is an attack, not just code).
- `li plugin trust <name>` shows what the plugin declares and records trust into
  `~/.lionagi/settings.yaml` under `trusted_plugins: {<name>: {manifest: <hash>,
  targets: {<path>: <hash>, …}}}`. The display contract is **complete and
  non-skippable**: every hook command's full argv (no truncation, no counts-only
  summary), every `target`/`module` path, and every profile/playbook file are
  rendered before the approval prompt — a bundle carrying many hook commands
  cannot bury one in an elided display. User-level, not project-level: a repo must
  not be able to self-trust the plugin it carries by committing a settings line —
  the human on the machine approves.
- The trust record pins **content, not just declaration**, for **every declared
  capability file** — executable *and* consumed-as-instructions alike: `sha256` of
  the canonical-JSON manifest, plus `sha256` of every tool `target` file, provider
  `module` file, hook binary, **agent profile file, playbook file, and pack data
  file** the manifest declares. Any change to the manifest or to any declared file
  reverts the plugin to `changed — re-trust required` and it stops loading —
  the changed capability is not exposed again before re-approval. Two narrower
  scopes were considered and rejected:
  - *Manifest-only pinning* is incoherent: it would argv-pin the subprocess hooks
    (the lower-privilege surface) while letting the in-process Python behind fixed
    `target` strings — which runs with the host's full privileges on the shared
    event loop — swap freely without re-approval.
  - *Executables-only pinning* repeats the same mistake one layer up: this ADR
    itself classifies a poisoned profile prompt as an attack (first bullet), and a
    profile or playbook is injected into a session's instruction stream — content
    the model acts on. Leaving those files unhashed means a bundle can pass trust
    with a benign `agents/researcher.md`, then swap in attacker instructions
    without touching any pinned hash. The displayed-then-mutable gap is exactly
    the attack the display contract exists to prevent, so everything the display
    contract shows, the trust record pins.
  The set of hashed files is exactly the declared set, so the cost is O(declared
  capabilities) at trust time and at each load-time verification. The one remaining
  unpinned category is bundle files the manifest does **not** declare (auxiliary
  data or docs a declared module might read at runtime) — that is the honest,
  bounded limitation of this design, stated rather than hidden: the boundary pins
  every capability LionAGI itself loads into a session or execution path, not every
  byte a trusted execution may choose to consume.
- Plugin hook commands additionally pass through ADR-0048 D7 (they are the
  "plugin-bundled" tier there); trusting a plugin records their argv hashes in the
  same step — defensible precisely because the display contract above already put
  every argv in front of the approver — and an edited hook argv still re-pends on
  its own through the hook-level gate.
- Trusted plugin code runs in-process with the host's privileges. The ADR says so
  plainly; see out-of-scope for why no sandbox is claimed.

### D6 — Collisions: built-ins win; peers hard-fail

- A plugin capability must never silently change what a stock name does — that is
  a supply-chain attack shape, not a customization feature. The built-in stays
  authoritative and the plugin capability is rejected with a diagnostic. **When**
  that check runs is surface-dependent, because for the code surfaces the set of
  built-in names is itself only knowable by loading code, which D3 forbids at
  discovery:
  - **Data-only surfaces** (agent profiles, playbooks, packs): checked at
    discovery — the built-in/shipped set is enumerable from data.
  - **Providers**: checked at the activation trigger (the D3 match
    interception), where the built-in registry has necessarily been loaded
    anyway. A plugin provider name shadowing a built-in provider match is
    rejected there, first use, with the diagnostic.
  - **Tools**: there is no global built-in tool set today — tool registries are
    per-`ActionManager`, populated imperatively. "Built-in" for tools therefore
    means: a plugin tool may never replace a name already present in the
    consuming manager's registry (whatever put it there — factory wiring, user
    code, another plugin), checked at the point the plugin tool would be
    registered/matched. No abstract shipped-tool list is claimed, because none
    exists.
- Two **enabled plugins** declaring the same capability name is a hard error at
  discovery, naming both plugins and the surface (the Studio route-registry
  precedent: duplicates are `ValueError`, not last-writer-wins). Resolution is
  human: disable one. Silent shadowing by scan order (the agent-profile precedent)
  was rejected for cross-plugin conflicts because scan order is not something a
  user meaningfully chose.
- A plugin colliding with a **user's own local file** (project agent profile with
  the same name, e.g.) resolves to the user's file, with a logged shadow warning —
  the user's explicit local file is the nearest intent.
- Namespacing keeps collisions rare: playbooks and agent profiles from plugins are
  addressable as `<plugin>/<name>` (`li play web-research/deep-research`); bare
  names resolve only when unambiguous. Tool names are global by necessity (the
  model calls them by bare name), which is exactly why the two rules above are
  strict.

### D7 — Lifecycle and CLI

```text
li plugin list                 # name, version, state: active|disabled|untrusted|changed|incompatible
li plugin info <name>          # manifest render: capabilities, trust state, paths
li plugin trust <name>         # inventory display + hash record (D5)
li plugin enable|disable <name>  # flips enabled: false in ~/.lionagi/settings.yaml plugins block
```

- Disable is a settings flag, not a file mutation inside the bundle — the bundle
  stays pristine and diffable against its source.
- Uninstall is `rm -r` of the bundle directory (or removing the package, under D8).
  Trust records for absent plugins are inert and garbage-collected by `li plugin
  list` when noticed.
- No `li plugin install <url>` in v1 — acquisition is out of scope (see
  out-of-scope: marketplace); the loader consumes directories however they arrived
  (git clone, copy, checkout of a monorepo path).

### D8 — DEFERRED: Python-package plugins via entry points

The design, recorded in full for the follow-up:

- A pip-installable package declares
  `[project.entry-points."lionagi.plugins"] <name> = "pkg.module:get_manifest"`;
  the callable returns the D2 manifest as a dict, with `target`/`module` references
  resolved as import paths instead of bundle-relative files.
- Discovery adds `importlib.metadata.entry_points(group="lionagi.plugins")` to
  Stage 1 — names only; the entry point is **loaded** (which imports the package)
  only on trust + first use, keeping Stage 1 data-only for the common path.
- Trust (D5) applies identically — installation via pip is not approval.
- Deferred because it adds a second resolution path (import-path vs bundle-relative)
  to every capability loader before the first path has users; the manifest schema
  is designed now so nothing about D2 changes when this lands.

## Consequences

- "Install" becomes: obtain a directory, `li plugin trust <name>`. One approval
  step, full inventory shown, everything listable afterward — P1/P3/P4 close.
- Providers gain their missing discovery half: the manifest names the module,
  `match_endpoint` miss triggers the import, the existing decorator does the rest —
  P2 closes with no change to the registration mechanism itself.
- Contributors must know the two-stage laziness rule when adding any new
  registry/consumer: ask the plugin registry on miss, never import plugin code in
  discovery. A consumer that eagerly imports plugin modules re-breaks the
  import-time invariant in a way no test on `import lionagi` alone will catch
  unless the invariant test asserts zero plugin imports too (it must).
- Failure surface moves earlier and louder: manifest typos, collisions, and
  incompatible ranges all surface at discovery with plugin-named diagnostics,
  instead of as deep-stack ImportErrors at call time.
- The trust boundary pins every declared capability file (manifest, target files,
  hook binaries, profiles, playbooks, packs), so a bundle edit behind a trusted
  manifest stops the plugin loading until re-approved — including a swap of
  instruction-bearing prompt files. Undeclared bundle files remain unpinned —
  stated in D5 as the design's honest limit.
- Built-in-collision detection for the code surfaces (providers, tools) happens at
  activation, not discovery — deferred by construction, because knowing the
  built-in set requires the loads D3 forbids at discovery. A colliding plugin
  provider therefore surfaces its rejection diagnostic at first use of that
  provider name, not at `li plugin trust` time; maintainers should expect the
  later timing when triaging reports.
- Studio's CC-bundle viewer and this system stay separate concepts sharing a
  directory idiom. A repository may ship one directory that is both (a
  `.claude-plugin/plugin.json` for CC and a `plugin.yaml` for LionAGI, side by
  side); neither reader consumes the other's manifest, so neither product's format
  evolution can break the other — at the cost that dual-target bundles maintain two
  small manifests, which is the cheaper side of the trade.
- Reversal cost: D1–D4 are additive (delete the loader and the miss-hooks; no core
  path bends around them). D6's namespacing (`<plugin>/<name>`) is the piece users
  will have embedded in playbook references and scripts — the hardest to walk back;
  it is also the piece least likely to need reversal.

## Alternatives considered

- **Python entry-points as the primary (or only) mechanism** — pip handles
  versioning, dependency resolution, uninstall. Lost as primary: it cannot express
  data-only bundles without forcing a Python package around three YAML files; it
  puts a packaging toolchain between a user and "share my agent setup"; and
  discovery-by-installation means `pip install` alone injects capability — a worse
  trust posture than an inert directory. Retained as D8 because code-heavy plugins
  (providers with dependencies) genuinely want pip underneath.
- **Adopt Claude Code's plugin format as LionAGI's own** (one `plugin.json` read by
  both products) — one manifest, and Studio's reader half-exists. Rejected: the
  format is owned and versioned by another product for a different host (its
  capability sections — skills, LSP servers, monitors — map to Claude Code
  concepts, not to `Branch`/`ActionManager`/`match_endpoint`), so LionAGI's runtime
  needs would either bend to a foreign schema's evolution or fork it silently —
  worse than two explicit manifests side by side.
- **No plugin concept — document the six seams and let users script them** — zero
  new code. Rejected: leaves P2 (nobody owns provider imports) and P4 (no trust
  step) genuinely unsolved; those are not documentation problems.
- **Last-writer-wins or scan-order shadowing for all collisions** (the existing
  hook-registry and agent-profile precedents) — simplest loader. Rejected for
  plugin-vs-plugin and plugin-vs-built-in because both are exactly the silent
  capability-substitution shape a plugin system must not import; retained only for
  user-local-file-wins, where the shadowed thing is the user's own explicit intent.
- **Per-plugin process isolation / sandboxing for tool execution** — a real
  security boundary instead of trust. Deferred, not rejected: LionAGI tools are
  in-process async callables sharing the event loop; pushing plugin tools out of
  process changes the `Tool` execution contract (serialization, latency, streaming)
  for all callers and deserves its own ADR with measurements. Trust-then-in-process
  matches the risk profile of the v1 audience (users installing bundles they
  obtained deliberately).

## Notes

- Numbering: this record's area is `substrates`, whose number block (0090-0095) is
  exhausted; the number is allocated from the adjacent free gap. The `Area` header
  is authoritative for classification — the block ranges are an allocation
  convenience, and recent records have already outgrown strict block-area
  correspondence.
- Naming: "plugin" here always means a LionAGI runtime extension bundle.
  Studio's existing plugin routes render *Claude Code* bundles; the two appear
  together only in the marketplace directory of this repository, which may carry
  dual manifests per the Consequences note.
- The `x-` manifest-key escape hatch exists so downstream tooling can annotate
  bundles (provenance stamps, registry metadata) without schema violations.

## Implementation status (2026-07-13)

As of 2026-07-13, this ADR is materially further along than ADR-0048, with the
bundle format, manifest schema, two-stage laziness, and trust boundary fully
implemented and tested, and three of five declared consumer surfaces wired
end-to-end.

**Implemented and tested:**

- **D1, bundle shape and discovery.** The directory-bundle layout under
  `.lionagi/plugins/<name>/plugin.yaml`, discovery via `find_lionagi_dirs()` in
  project-then-global order, missing-manifest-is-ignored, schema-failure-is-
  excluded-not-partial, and the path-traversal check on every bundle-relative
  reference are all implemented and covered by dedicated tests.
- **D2, manifest schema.** The `name` pattern, the `version`/`lionagi:`
  compatibility-specifier gate, the six capability fields (tools,
  `hooks_external`, agents, playbooks, providers, packs), the
  parse-imports-nothing guarantee, and the unknown-key-errors-except-`x-`-
  prefix rule are all implemented and tested.
- **D3, two-stage laziness.** Manifest discovery stays data-only; code import
  is deferred to first use of a specific capability; `import lionagi` alone
  triggers neither stage, verified by a dedicated import-laziness test. Three
  of the five named consumer trigger points are wired end-to-end with tests:
  the `ActionManager` tool-name-miss path, the endpoint registry's
  match-miss interception before the generic OpenAI-compatible fallback, and
  agent-profile resolution miss. A per-plugin failed-import cache (distinct
  from the built-in provider loader's memoryless swallow-and-continue
  behavior) is implemented and tested.
- **D5, trust.** An untrusted plugin is fully inert across every consumer
  path; the `li plugin trust` display contract renders every declared
  capability (tool targets, hook argv, agent/playbook/pack files) without
  truncation before approval; the trust hash pins the manifest plus every
  declared file, not only executables, which is exercised by a test proving
  non-executable files are pinned too; and trust records are written to
  user-level settings, never project-level. This is the most rigorously
  implemented and tested section of either ADR-0048 or ADR-0088.
- **D6, collisions (partial) and D7, lifecycle.** Two enabled plugins
  declaring the same tool name raise a hard collision error; a plugin colliding
  with a user's own local agent-profile file resolves to the user's file with
  a logged warning; and the full `li plugin list/info/trust/enable/disable`
  command set is implemented, with disable implemented as a settings flag that
  leaves the bundle directory untouched, matching the ADR's stated design.
  Namespacing (`<plugin>/<name>`) is implemented and tested for agent profiles.
  Trust records for a plugin whose bundle directory has been removed are
  garbage-collected on `li plugin list` (`gc_trust_records`), printing which
  entries were pruned and why; a plugin later reappearing under the same name
  is not resurrected from the removed record and must be re-trusted.

**Known gaps:**

- **Playbook discovery is not unified onto `find_lionagi_dirs()`.** Playbook
  resolution still globs only the global `~/.lionagi/playbooks/` directory and
  has no awareness of project-local or plugin-provided playbooks. The path
  validator on the playbook-name argument rejects any component containing a
  `/`, which is exactly the shape of the `<plugin>/<name>` token this ADR
  specifies for referencing a plugin-provided playbook — so today that token
  is rejected outright, not merely left unresolved. This is the one consumer
  trigger point named in D3 that has not been started, and it also leaves the
  playbook half of D6's namespacing commitment unimplemented (the agent-profile
  half is done).
- **The `hooks_external` capability has no runtime consumer.** A plugin's
  `hooks_external` block is parsed, path-validated, and content-hashed for
  trust, and rendered in `li plugin info`/`trust` output, but nothing joins it
  into the session hook bus or agent-factory hook wiring. This is a direct
  consequence of ADR-0048's external-hook execution layer not existing yet
  (see that ADR's own implementation-status annex): a plugin's declared hooks
  have nowhere to attach until that layer lands.
- One smaller, contained gap: providers and tools have no explicit,
  user-facing diagnostic when a plugin capability collides with an
  already-registered built-in — the safety property holds today only because
  the plugin-consultation branch is structurally unreachable once a built-in
  match succeeds, not because a rejection path was exercised and reported.
