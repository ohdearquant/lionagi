# ADR-0140: Provider discovery and composition through the 1.x transition

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: service-providers
- **Date**: 2026-10-31
- **Relations**: supplies ADR-0136 D6's providers contract; requires ADR-0136 Amendment 3; specifies finite exceptions to ADR-0122 D1, D2 and D5; preserves ADR-0088's directory-plugin trust and collision rules. ADR-0088 D8 remains deferred.

## Context

The maintainer chose to retain automatic provider selection throughout 1.x and remove the compatibility paths in 2.0. A fresh process must continue to construct `iModel(provider="openai")` without importing the hub first or registering providers. The one `lionagi` wheel continues to contain both import roots. This does not make a lazy core-to-hub import comply with ADR-0122's layer law; it requires an explicit, expiring exception.

Today endpoint discovery walks a fixed module list and relies on decorators mutating a global registry. Token-budget lookup has another module table and suppresses import errors. The package move first rewrites their 46 provider-module strings to the hub root without changing behavior. These two interim authorities remain only until the discovery contract lands after the move and before 1.0; 1.0 cannot release with any of those 46 edges remaining. A single manifest import is useful only if reading that manifest does not import endpoint implementations or publish registrations.

## Decision

### D1. One automatic source in 1.x; explicit composition in 2.0

After the package move and before 1.0, introduce core-owned contract types in `lionagi.service.connections.provider_catalog`. Introduce one compatibility loader in `lionagi.service.connections._provider_compat`. Only that loader names `lionhub.providers.manifest`; no provider-specific module paths or model tables enter the loader or contract module.

The hub exports `get_provider_manifest()` from `lionhub.providers.manifest`. Calling it returns immutable declarations and imports no endpoint implementations. Importing the manifest itself does not discover plugins, read configuration or credentials, create clients, start processes, or mutate any registry. Provider-specific module names and factories belong to the hub.

On the first provider-selection or context-window request that has no explicitly supplied catalog, the 1.x facade lazily obtains and validates this manifest. Both endpoint resolution and token-budget lookup consume that same catalog. Remove `_import_all_providers`, its fixed list, `_PROVIDER_MODULES`, and the independent token-budget provider cache. The scripted test provider currently lives in `lionagi.testing._endpoint`, not in the providers package, and stays there during the package move. The contract change converts its registration into an explicitly selected core test fragment while preserving its `LIONAGI_CHAT_PROVIDER=scripted` activation gate; no scripted implementation moves to the hub.

Ordinary `import lionagi`, importing the registry, and importing the four relocated core modules by their canonical core paths must not load the hub. Their old paths import the moved parent packages and therefore load the hub after the broad compatibility row lands. Merely listing declarations may load the manifest on explicit diagnostic access, but must not load endpoint implementations. Selecting one endpoint loads only its factory's necessary implementation closure, not every provider. The catalog's declared default endpoint and ordering preserve current successful matching, endpoint aliases, provider aliases, case folding, and the existing single-endpoint matching behavior.

Expose `compose_provider_catalog(fragments)` in the core contract module and an optional keyword-only `provider_catalog` at the `iModel` selection boundary. Thread that catalog through endpoint resolution and token-budget lookup; an already supplied endpoint continues to bypass provider discovery. These are additive 1.x APIs. An explicit empty catalog is distinct from omission and must never trigger the automatic source. Reject simultaneous conflicting endpoint and catalog selection rather than guessing which owns the model.

An explicitly supplied catalog or endpoint bypasses automatic hub loading entirely. In 2.0 delete `_provider_compat` and every automatic core-to-hub path. A composition root imports the hub manifest and passes a composed catalog or endpoint to the core. Installing the hub alone does not activate it. The core receives a registry or port, never a provider module path. Old `lionagi.providers.*` imports cease to exist under ADR-0136 D4.

### D2. Versioned declarations, factories and state ownership

Contract version 1 consists of:

| Declaration | Required content |
|---|---|
| Manifest | `contract_version=1`, stable source identity, ordered provider fragments |
| Provider fragment | `contract_version=1`, canonical provider key, provider aliases, ordered endpoint declarations, default endpoint, immutable context-window mapping or explicit unknown metadata |
| Endpoint declaration | endpoint key, endpoint aliases, API/agentic type, callable factory, declared dependency requirements, settings specification, diagnostic identifier |
| Dependency requirement | distribution name, top-level import roots, supported version constraint where needed, and a validated installation hint appropriate to the installed distribution |
| Activation result | endpoint implementing the core port and matching the declaration; no registry mutation |

These are declarations, not additional positional members in `ProviderConfig` enums. Existing config enums and public constants may project this authority, but must not maintain divergent copies. Provider and endpoint identity are validated before publication. Reject missing, non-integer, Boolean, or unsupported contract versions with `ProviderContractVersionError(ValueError)`, carrying source, expected versions and observed value. Define `ProviderContractVersionError` and `ProviderActivationError` in `lionagi._provider_errors`; the contract module imports these core-owned types. Manifest and fragments must agree. Version mismatch never becomes a missing-extra message, an empty catalog, or generic fallback. Validate all declaration versions before calling any factory. Do not infer a contract version from the distribution version.

Composition produces an immutable catalog snapshot. Factory results and failures are cached separately per composition and endpoint; no process-global mutable activation state is shared between explicitly composed applications. Concurrent selection of the same endpoint performs one activation. Recursive activation of that endpoint raises `ProviderActivationError` with a cycle reason. Publish successful state atomically; a failed factory publishes no endpoint. Failure is stable within a composition; an explicit new composition is the retry boundary.

Built-in decorators become declaration-only in the contract change after the move: they may attach metadata to classes but must not call global registration or resolve optional request types merely by importing a config enum. Factories resolve the selected request type and construct the endpoint on demand. `Endpoint` continues to receive equivalent `_ENDPOINT_META` or an explicit config, preserving its constructor behavior.

The existing public `register_endpoint` API and directory-plugin import registration remain a **1.x compatibility exception**. Keep that imperative path separate from built-in manifest declarations. The default facade may retain one process-default registry for old callers. Capture plugin contributions under activation provenance and validate them before publishing; a failed activation must leave no partial entries or aliases. Do not discover built-ins by inspecting that global registry or copying its entries into a fragment. Existing direct registration remains usable for nonconflicting keys.

This explicitly defers full ADR-0122 D2 rules 1, 2, 4 and 6 compliance for the old imperative facade: plugin imports still register, and old calls still use default composition. Built-in declarations and new explicit compositions obey those rules immediately. In 2.0 remove automatic global publication: adapters return fragments and roots compose them. Existing directory-plugin trust checks remain mandatory; this record does not adopt wheel entry points or authorize plugins merely because a wheel is installed. All diagnostic consumers use the catalog's availability projection, satisfying D2 rule 5 without a second capability table.

### D3. Keys and duplicates

The hub manifest reserves its built-in canonical provider names and aliases before any implementation loads, including providers whose optional dependencies are absent. Provider registration keys retain trimmed lowercase normalization; lookup retains its current lowercase-only behavior, so this change does not silently accept padded names.

Within a provider, endpoint keys and endpoint aliases share one namespace. A provider may declare multiple distinct endpoints. Different providers may use the same endpoint name. Two fragments claiming the same provider identity, two endpoints claiming the same endpoint identity, or conflicting provider aliases are composition errors identifying both owners. Reusing the identical validated snapshot is idempotent; repeating declarations inside an input is still an error. No import-order or last-writer precedence is allowed.

A plugin claiming a built-in provider name or alias is rejected with a diagnostic naming the plugin, key and built-in owner; the built-in stays authoritative. Reject the colliding contribution even when the built-in cannot activate. This rejection does not prevent an unrelated built-in from resolving. Two plugins claiming the same canonical provider, provider alias, or endpoint identity produce a hard collision error before their conflicting entries are published. Explicit application contributions also cannot silently override reserved built-in keys; an application needing different ownership composes a catalog without that built-in source.

Ownership is recorded as source identity plus plugin target, not inferred from `cls.__module__`. The existing plugin loader's module-name comparison must not relabel a built-in factory result, a re-exported class, or another plugin's entry. Preserve trust revalidation and revocation of already-activated plugin endpoints. The legacy directory manifest names modules rather than provider keys, so first miss may still activate all active declared provider targets; this bounded exception is not described as per-provider discovery. That activation runs only behind ADR-0088 D5's trust gate: for each target, `PluginRegistry.activate_target` re-reads the plugin, requires it to be trusted, enabled and compatible at that call, and compares the target file's bytes with the hash in its trust record before the module is compiled or run, so a miss never executes untrusted, disabled, incompatible or changed plugin code. Diagnose collisions when those keys become known and publish atomically.

### D4. Absence, defects and the public boundary

Provider selection during `iModel` construction is the public failure boundary. Validate the selected endpoint's declared runtime prerequisites there, including prerequisites otherwise first needed by its request path. No network request, executable launch, credential lookup, or availability probe requiring credentials is necessary for this validation. Invalid caller configuration and existing domain errors retain their types.

| Condition | Required result |
|---|---|
| Complete 1.x wheel, supported provider and dependencies | Existing constructor call selects the same endpoint without caller registration or prior hub import. |
| Hub physically absent from a 1.x installation | `MissingFeatureError(ImportError)` with `feature=providers`, requested provider, missing distribution `lionagi`, and guidance to repair the matching `lionagi` installation. This is an incomplete bundled installation, not an optional hub extra. |
| Declared optional dependency absent | `MissingFeatureError` naming selected provider/endpoint, declared distribution and exact supported install hint. Other providers remain usable. |
| Internal import failure, syntax error or factory defect | `ProviderActivationError(RuntimeError)` with provider, endpoint, source, reason `activation_failed`, and original exception chained. Do not advise installing an extra. |
| Unsupported contract | `ProviderContractVersionError`, before activation. |
| Unknown provider after permitted plugin discovery | Existing `ProviderNotFoundError`, with registered/declaration keys and composition guidance. |
| 2.0, no supplied provider catalog, hub distribution not installed | `MissingFeatureError` at construction, `feature=providers`, missing distribution `lionhub`, and guidance to install a compatible hub and explicitly compose its manifest. |
| 2.0, no supplied provider catalog, hub distribution installed | `ProviderCompositionError(ValueError)` naming the missing provider catalog and requiring explicit composition; never report a missing distribution. Define this error in `lionagi._provider_errors`. |
| 2.0 explicit catalog lacks the requested key | `ProviderNotFoundError`; do not suggest that installation automatically changes an explicit catalog. |

Introduce the common `MissingFeatureError(ImportError)` specified by ADR-0122 in `lionagi._feature_errors`; it is not implemented today. Preserve its specified fields and add provider, endpoint, reason and source for the shared diagnostic projection. The 1.x automatic source declares the top-level import requirement `lionhub`, supplied by distribution `lionagi`. At 2.0, after that source is deleted, the public diagnostic requirement names distribution `lionhub`. A missing `lionhub` root in 1.x therefore reports repair of the bundled `lionagi` installation. At the 2.0 public failure boundary only, inspect installed distribution metadata to distinguish an absent hub distribution from an omitted catalog; do not import the hub or select an adapter. This narrow diagnostic exception to ADR-0122 D5 does not permit runtime discovery or replace explicit composition. Absence translation is allowed only for a declared missing top-level dependency. Translate `ModuleNotFoundError` only when its `name` exactly matches a declared top-level import root. A missing internal hub module or symbol is a defect even if Python raises `ModuleNotFoundError`. In 1.x, absent hub detection applies only to the automatic source; in 2.0, distribution absence is classified only on the missing-catalog failure path. Supplied endpoints and explicit catalogs remain usable without the hub.

An explicitly requested generic OpenAI-compatible endpoint remains supported. Preserve the existing deprecated `base_url` opt-in warning for genuinely unknown providers in 1.x. Neither route may hide a failed declared provider, missing bundled manifest, invalid contract, or rejected collision. Preserve existing successful known-provider endpoint fallback behavior; distinguish an endpoint not declared by a healthy provider from a declared endpoint whose activation failed. In 2.0 generic compatibility requires explicit caller selection.

Diagnostics expose `declared`, `selected`, `available`, `reason`, source, contract version, requested/canonical key, and declared missing requirements. Before activation, availability may be `unknown`; listing must not activate providers to turn it into a Boolean. Optional explicit activation diagnostics use the same loader. Cache and report failures without repeated warning floods. Secret values, request bodies and environment values never enter these projections. Original exception objects remain available as chained causes; public diagnostics must not dump arbitrary exception text containing secrets.

### D5. Context-window metadata

Each provider fragment owns its immutable context-window mapping. Hub metadata modules may hold existing constants but must not import endpoint implementations. Remove provider-specific context tables and provider module strings from core token budgeting. The selected endpoint's explicit `context_window` remains highest precedence.

For a healthy catalog preserve the existing algorithm: provider-specific metadata first, then mappings in the declared legacy order, then the existing 128,000 estimate. Within a mapping the current algorithm uses longest **substring** match, despite its prefix-oriented name; do not change that matching rule as part of relocation. Validate declared window sizes as positive integers, excluding Booleans. Unknown model metadata may use the documented estimate, reported as `reason=unknown_context_window`. A missing manifest, incompatible contract or failed metadata load must raise the corresponding typed error, not masquerade as an unknown model and return 128,000. Metadata-only queries do not activate endpoint implementations; known metadata remains readable when an endpoint dependency is absent. Endpoint selection still fails for that absent dependency.

### D6. Other edges and relocation

Use the finite interim-edge inventory in ADR-0136 Amendment 3. Keep `_codex_profile` with the providers. The four lower modules have these canonical homes before the provider package moves:

| Old module | New module |
|---|---|
| `lionagi.providers._provider_errors` | `lionagi._provider_errors` |
| `lionagi.providers._secret_resolution` | `lionagi.service.accounts._secret_resolution` |
| `lionagi.providers._subagent_admission` | `lionagi.service._subagent_admission` |
| `lionagi.providers.openai._chat_schemas` | `lionagi.service.connections._chat_schemas` |

These relocations preserve definitions and behavior. The shared chat schema contains existing OpenAI model validation and warning-filter behavior; its temporary residency does not authorize new provider-specific catalogs in core or certify import purity. Separate later cleanup must preserve schema compatibility and move provider policy behind supplied contracts before declaring ADR-0122 fully implemented. Admission and secret resolution remain explicitly invoked services, not pure DTOs. Importing them must not perform admission or secret lookup.

Add four specific compatibility rows and then the broad `lionagi.providers -> lionhub.providers` row. Longest-prefix matching must preserve the four core destinations after the broad row lands. Extend compatibility tests for individual module rows and nested old prefixes; preserve identity, warning attribution, reload, patches and old pickles. With the bundled hub present, direct old descendant imports must work both before and after the parent package relocation. D3's finder must run before `PathFinder` and intercept old descendant names even below already-aliased parent packages, then return the canonical core module object for each specific row. In a deliberately hub-absent 1.x environment, canonical core imports work; an old descendant path may fail while resolving its moved parent. Do not claim that longest-prefix resolution alone makes that parent import independent of the hub.

Pin existing logger names, including literals already used by CLI providers. Rewrite the five shared-schema `LazyType` references when the schema moves, and all remaining provider references when the package moves. `LazyType.resolve()` splits `module:Class` before importing; canonical repository strings must not depend on the compatibility finder. Keep external old strings usable through 1.x under D3's normal parent-import requirements.

### D7. Implementation sequence and acceptance

1. Establish individual-module compatibility tests and relocate the four lower modules, one change each, with their callers, logger literals, table rows and CHANGELOG entries. Preserve settings lookup through `lionagi._settings` with `include_project=False` and its failure handling. Make the four module-top `_codex_profile` imports in `agent/factory.py` lazy in a separate preparatory change; the other listed factory references are already lazy.
2. Move `lionagi/providers` completely to `lionhub/src/lionhub/providers` with one broad compatibility row. The implementation diff is renames, import rewrites, path docstrings, pinned logger names, and in-place root rewrites of the 34 `_import_all_providers` module-list strings in `registry.py` and 12 `_PROVIDER_MODULES` strings in `token_budget.py`; no behavior change. The move change carries C2's exact inventory test and fresh-process tests of every existing `lionagi.providers.*` import and `iModel(provider=...)` for every built-in provider key, with supported prerequisites and no earlier `lionhub` import. Carry ADR-0136 D6 items 1–6 as amended by Amendment 3.
3. After the package move and before 1.0, implement D1–D5 in separate contract changes: the catalog, declaration-only built-ins, legacy plugin publication safeguards, shared context metadata, lazy factories, and the common `MissingFeatureError` specified by ADR-0122 plus the other errors defined in D2 and D4. Write the manifest directly at `lionhub.providers.manifest` and `_provider_compat` against that final path. Delete the 46 interim strings and both independent lists, convert the scripted provider to a core fragment, and prove parity and the failure matrix below. A release inventory test blocks 1.0 while any of those 46 interim edges remains.
4. At 2.0 remove automatic selection, legacy publication and all old import aliases. Prove explicit isolated composition and both missing-distribution and missing-catalog results before the distribution split.

Required verification uses fresh non-editable wheel environments outside the source tree, with no network provider calls. The move checks preserve existing behavior; the contract checks below apply after D7 step 3:

- Present hub: assert no hub modules after base import; then the unchanged `iModel(provider="openai")` call selects the expected class. No earlier hub import or registration is allowed. Repeat every built-in provider key, API, agentic, alias, plugin and scripted cases with their declared prerequisites.
- Physically absent hub: canonical core modules and injected test endpoints work; the unchanged automatic constructor raises the specified 1.x error. Repeat at 2.0 with the hub distribution absent and installed but uncomposed, asserting the distinct missing-distribution and missing-catalog errors. Blocking imports alone is not the sole absence test.
- Missing optional dependency, internal `ImportError`, missing internal symbol, syntax error, invalid version and factory failure remain distinct; no generic endpoint or default context estimate masks them. A failed endpoint does not break unrelated providers.
- Compare endpoint keys, aliases, default choices, request-schema identity and context-window outputs with characterized pre-change results. Test module-cache warm and cold orders, concurrent selection, failed activation followed by fresh composition, and recursive activation.
- Test built-in/plugin collisions with unloaded and failed built-ins; peer plugin collisions; partial-registration rollback; helper-defined/re-exported classes; trust revocation; and two explicitly composed applications with disjoint catalogs and failure caches.
- Verify all ADR-0136 compatibility requirements, updated schema-import checks, source-boundary fixtures, and exact file-level exceptions. Search persisted names and report the population actually inspected; source inspection is not a search of user data. Apply D5's class tests wherever applicable.

**MAY:** add provider-neutral declaration, composition, validation and diagnostic machinery; keep the enumerated 1.x compatibility paths. **MAY NOT:** introduce entry points, new provider tables in core or retention of the 46 interim strings at 1.0, broad error swallowing, new blanket package exemptions, implicit fallback on declared-provider failure, or additional optional dependencies in the base install. **Verify by:** the wheel matrix and invariants above, plus ADR-0136's per-change evidence.

## Alternatives considered

- **Wheel entry points:** useful for separately distributed extensions, but expands deferred ADR-0088 D8 and trust/version policy without solving the transitional core dependency. Not adopted.
- **Contract before the move:** avoids rewriting 46 strings only to delete them, but delays the behavior-preserving package move behind a substantial refactor. Rejected in favor of the maintainer's move-first order.
- **Keep rewritten module lists permanently:** preserves duplicated discovery and error behavior. Rejected; Amendment 3 permits them only between the move and the contract change before 1.0.
- **One data-only table in an exempt core file:** centralizes strings but leaves the built-in catalog owned by core and confuses compatibility mappings with provider policy. Rejected.
- **Require explicit registration immediately:** conforms to the final layer law but breaks the required 1.x constructor behavior. Reserved for 2.0 composition.
- **Keep context windows in core:** avoids a metadata accessor but makes every provider catalog change a core edit. Rejected.
- **Use the finder for repository LazyType strings or rename loggers now:** leaves internal code depending on expiring paths or breaks logging configuration. Rejected.

## Consequences and risks

The automatic loader is a deliberate transitional inversion with a deletion release. The manifest adds one shared source of provider identity and metadata; it does not by itself eliminate the legacy global registry. The subsequent contract changes are larger than an import rewrite and require parity evidence. The move-first order costs a temporary rewrite of 46 strings and a larger interim inventory. Failure reporting becomes more precise and may expose defects previously hidden by import suppression. That change must be documented with the contract implementation before 1.0.

The four relocated modules reduce upward imports but do not prove all their contents are provider-neutral. Full ADR-0122 compliance still requires removal of the listed runtime implementation imports, legacy global publication and residual provider policy in core. The 2.0 release may not claim those obligations complete from file motion alone.
