# ADR-0063: Project attribution cascade

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: cli-surface
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0026

## Context

CLI-created sessions need a stable project label for filtering and provenance. The
label is not an authorization boundary and cannot be required: commands also run in
unconfigured directories, non-Git directories, deleted mirror source paths, and
checkouts whose origin cannot be read.

**P1 — Repository identity has several plausible sources.** A checkout may declare a
name in `.lionagi/config.toml`, a user may override it in global settings, or Git may
provide only an origin URL. Those sources need deterministic precedence.

**P2 — Detection must normally be best-effort.** Failure to read optional TOML, YAML,
or Git metadata should not prevent an agent or flow from running. “No attribution” is a
valid outcome.

**P3 — The result needs provenance, not just a label.** `config_toml`,
`global_override`, `git_remote`, and explicit input do not carry equal intent. A stored
source tag lets later readers distinguish declaration from inference.

**P4 — Existing runs must not be relabeled by the current directory.** A session is a
durable record. Once its project pair is written, observation surfaces query that
stored value; re-running detection later is only a way to select a project-scoped view,
not permission to overwrite history.

**P5 — Imported transcripts and scheduled actions need adaptations.** Mirror must
assign even an unconfigured or now-missing source directory, while schedule creation
needs only a validated project label in its request body. Those uses share the detector
but do not share its exact output contract.

The current implementation has two known path-boundary weaknesses. Configuration is
searched before the Git root is found, so a file above a nested repository can win.
Path overrides use raw string prefixes, so `/work/repo` also matches
`/work/repository`.

| Concern | Decision |
|---|---|
| Attribution value | D1: the detector returns a nullable `(project, source)` pair; explicit input adds source `explicit`. |
| Precedence | D2: config, remote override, path override, Git slug, then unassigned; first successful source wins. |
| Source parsing | D3: TOML, YAML, Git root, and origin parsing have exact best-effort rules. |
| Persistence | D4: fresh durable sessions snapshot the pair; resumed branches reuse their existing session attribution. |
| Specialized consumers | D5: mirror adds cwd fallbacks and schedule creation transmits only a validated label. |

This ADR deliberately does **not** decide:

- Access control, repository trust, or authorization; a project label is descriptive
  provenance only.
- Project editing and registry presentation in Studio; those surfaces consume the
  stored fields but own their mutation policy.
- Working-directory resolution when a schedule fires; the scheduling-control-plane
  area owns that behavior.
- A general workspace hierarchy above repositories; the current ancestor walk is
  recorded as behavior and its correction remains a delta.
- Run outcome identity or artifact storage; ADR-0064 owns those records.

## Decision

### D1 — Attribution is a nullable pair with source provenance

The detector contract is the actual Python signature:

```python
# lionagi/cli/_project.py
def detect_project(
    cwd: Path | None = None,
) -> tuple[str | None, str | None]: ...
```

For this function, the closed return vocabulary is:

```python
DetectorSource = Literal[
    "config_toml",
    "global_override",
    "git_remote",
] | None

DetectorResult = tuple[str | None, DetectorSource]
```

The execution-boundary resolver extends it:

```python
# lionagi/cli/_runs.py
def _resolve_project(
    project: str | None,
) -> tuple[str | None, str | None]:
    if project:
        return project, "explicit"
    return detect_project()
```

The stored session columns are nullable text:

```sql
-- lionagi/state/schema.sql
project         TEXT,
project_source  TEXT
```

**Exact semantics**:

- `cwd=None` uses `Path.cwd()` at call time.
- `(None, None)` means no source resolved. No placeholder project is invented by the
  detector.
- An explicit project is recognized by truthiness. An empty string is not stored as
  `explicit`; it falls through to detection.
- Detector-produced project names are strings, but no identifier grammar is enforced
  in `_project.py` itself.
- `project_source` is meaningful only with a project. `StateDB.set_session_provenance`
  writes the two together when `project is not None`.
- The database column is not constrained to the detector vocabulary. Other ingestion
  paths currently use `cwd_dir`, `cwd_missing`, `studio`, or `manual`; D5 records the
  CLI mirror additions. The closed set above applies to `detect_project`, not to every
  StateDB writer.

**Why this way.** A nullable pair represents both absence and evidence source without
requiring a registry lookup. Storing only the label would make a repository declaration
indistinguishable from a Git-derived guess; requiring a label would reject legitimate
unconfigured work.

### D2 — Implicit detection uses a fixed first-success cascade

The shipped cascade is:

```text
1. Walk cwd -> filesystem root for the first `.lionagi/config.toml` file.
   If it yields a truthy `[project].name`, return (name, "config_toml").
2. Find the nearest Git root with `git rev-parse --show-toplevel`.
3. If a Git root exists, derive remote_slug from origin.
4. Read `~/.lionagi/settings.yaml` project_overrides:
   a. exact remote_slug key first;
   b. then the first absolute-looking key for which str(cwd).startswith(key).
   On either hit return (str(value), "global_override").
5. If remote_slug exists, return (remote_slug, "git_remote").
6. Return (None, None).
```

Configuration shapes are:

```toml
# <ancestor>/.lionagi/config.toml
[project]
name = "project-name"
```

```yaml
# ~/.lionagi/settings.yaml
project_overrides:
  organization/repository: local-label
  /absolute/path/prefix: path-label
```

**Exact semantics**:

- Config discovery enumerates `[cwd, *cwd.parents]`; it is not bounded by the Git root
  and does not resolve or normalize `cwd` first.
- The first existing config file shadows every higher ancestor. If it is unreadable,
  malformed, lacks `[project].name`, or has a falsey name, `_from_config_toml` stops the
  walk and the overall cascade proceeds to overrides/Git; it does not continue looking
  for another config file above it.
- A truthy non-string TOML name is converted with `str`.
- Git is not consulted at all when config attribution succeeds.
- A remote override always precedes a path override.
- Path overrides follow YAML mapping insertion order. There is no longest-prefix sort.
- A path key participates only when `key.startswith("/")`; relative keys are ignored by
  the path branch, though they may still match a remote slug exactly.
- Prefix matching is textual, case-sensitive, and not component-aware.
- A missing override file, YAML read/parse exception, missing `project_overrides`, or a
  non-mapping `project_overrides` value degrades to the next source.
- A syntactically valid settings document whose **top level** is not a mapping is a
  current exception hole: `settings.get(...)` can raise because that access is outside
  the YAML load `try`. Callers such as mirror and schedule defensively suppress it, but
  `detect_project` itself does not. This is shipped behavior, not the intended
  best-effort ideal.

**Why this way.** Repository-local declaration has the strongest proximity and travels
with a checkout. Global overrides support forks and paths that cannot be modified.
Remote slugs give a zero-configuration final inference. First-success evaluation keeps
the explanation singular and cheap.

### D3 — File and Git parsing are bounded and deliberately narrow

The source helpers are:

```python
# lionagi/cli/_project.py
def _from_config_toml(cwd: Path) -> tuple[str | None, str | None]: ...
def _read_project_from_toml(path: Path) -> str | None: ...
def _from_global_overrides(
    cwd: Path,
    remote_slug: str | None,
) -> tuple[str | None, str | None]: ...
def _git_remote_slug(git_root: Path) -> str | None: ...
def _parse_remote_url(url: str) -> str | None: ...

# lionagi/_paths.py
def _find_git_root(cwd: Path) -> Path | None: ...
```

**TOML semantics**:

- Python 3.11+ uses `tomllib` on a binary file. If that import is unavailable, the
  declared `toml` dependency reads the file in text mode.
- Any exception in import, open, parse, or shape handling returns `None`.
- Only `[project].name` is consumed. Other project keys do not affect attribution.

**YAML semantics**:

- `yaml.safe_load(f) or {}` is used; read and parse exceptions return no override.
- `project_overrides` must be a mapping. Values are converted with `str`, so a matched
  null value becomes the literal project label `"None"` rather than an unassigned
  result.

**Git semantics**:

- `_find_git_root` runs `git rev-parse --show-toplevel` in `cwd`; nonzero exit or any
  exception returns `None`.
- `_git_remote_slug` runs `git remote get-url origin` in the resolved Git root; nonzero
  exit or any exception returns `None`.
- Each subprocess has a 5-second timeout. The reason for bounding a CLI metadata probe
  is clear—detection must not hang execution—but the source records no rationale for
  the exact value 5. A no-config Git path can therefore spend up to two such sequential
  probe bounds.
- Remote URLs have trailing `/` and a final `.git` removed.
- SCP-like forms such as `git@host:org/repo` return their final two path components.
- Other forms, including HTTPS and `ssh://`, are split on `/` and also return the final
  two components when at least two exist.
- Parsing does not validate host, organization, repository characters, or the URL
  scheme. It is a slug extractor, not a URL validator.

**Why this way.** The detector needs only a stable display/filter label and should not
take a network dependency. Fixed-argv local Git commands and narrow file parsing provide
that evidence with bounded latency. Full remote URL normalization would add policy that
the current consumers do not need.

### D4 — Fresh sessions snapshot attribution; resume reuses the record

Agent persistence and orchestration persistence resolve the pair before
`StateDB.create_session`:

```python
# lionagi/cli/_runs.py
async def setup_agent_persist(
    branch: Branch,
    *,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract: dict | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
) -> dict | None: ...
```

```python
# lionagi/cli/orchestrate/_orchestration.py
async def setup_orchestration_persist(
    session: Any,
    *,
    invocation_kind: str | None = None,
    playbook_name: str | None = None,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract: dict | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
    branches: list[Any] | None = None,
    extra_node_metadata: dict | None = None,
) -> dict | None: ...
```

Both creation paths write:

```python
{
    "project": resolved_project,
    "project_source": resolved_source,
}
```

**Exact semantics**:

- A new agent branch creates a new session row and snapshots attribution with the other
  execution provenance.
- When `setup_agent_persist` finds an existing Branch row, it reuses that row's
  `session_id` and progression. It does not re-run `_resolve_project` or update the
  stored pair.
- A new orchestration Session snapshots its explicit or detected attribution once at
  session creation.
- `StateDB.create_session` registers any truthy project in the `projects` table after
  inserting the session. If source is absent there, the registry fallback is
  `git_remote`; the session row itself retains the supplied nullable source.
- Registry conflict handling always refreshes timestamps and replaces the registry
  source only when the incoming source is `config_toml` or `global_override`. It does not
  rewrite existing session attribution.
- Status without an explicit id detects the current directory's project only to scope a
  “latest session” query against stored `sessions.project`.
- Monitor derives a project for show repositories and compares it with the requested
  filter; its detector results are cached by repository path for the process.

**Why this way.** Attribution describes the execution context at record creation. If a
later checkout rename, override edit, or current-directory change rewrote old rows,
filters and provenance would become time-dependent. Reusing the Branch-linked session
also preserves conversation identity, though ADR-0064 records the resulting execution-
outcome limitation.

### D5 — Mirror and schedule creation adapt the detector to their needs

Mirror resolves a non-null pair:

```python
# lionagi/cli/mirror.py
def _fallback_project(cwd: str) -> tuple[str, str]: ...
def _resolve_project_for_mirror(cwd: str) -> tuple[str, str]: ...
```

The additional mirror sources are:

| Condition | Result |
|---|---|
| Detector resolves a project | Detector `(project, source)` |
| Detector misses and `Path(cwd).is_dir()` | `(Path(cwd).name, "cwd_dir")` |
| Detector raises or source directory no longer exists | `("others", "cwd_missing")`, unless a still-existing directory enables `cwd_dir` |

Schedule create follows a different projection in `lionagi/studio/cli.py`:

```text
explicit --project -> action_project exactly as supplied
no --project        -> best-effort detect_project(Path.cwd())[0]
detected label      -> validate as scheduler identifier, then include action_project
miss/exception/invalid identifier -> omit action_project; creation continues
```

Only the label enters the schedule request body; the detector source is discarded.

**Exact semantics**:

- Mirror catches every detector exception before applying its fallback.
- An existing directory's final path component becomes the fallback project name; it
  is not normalized or registered through `_project.py`.
- A missing mirror directory is grouped under the literal project `others`.
- Explicit schedule project input bypasses detection.
- Auto-detected schedule input is validated with the scheduler's identifier rule;
  validation failure is suppressed with the rest of the best-effort block.
- These adaptations do not change `detect_project`'s closed source vocabulary.

**Why this way.** Mirror needs every imported transcript placed in a visible bucket,
even after its original directory disappears. Schedule creation needs a safe lookup key
for later working-directory resolution, not the session provenance pair. Sharing the
detector but adapting its miss behavior keeps those requirements explicit.

## Consequences

- Owned repositories can carry a label with the checkout, while global overrides cover
  forks and directories that cannot be modified.
- Unconfigured live work remains usable and is stored as unassigned; mirrored work is
  instead grouped by cwd or `others`.
- The source tag distinguishes explicit, declared, overridden, inferred, and mirror-
  fallback attribution.
- Detection is normally non-fatal, but the top-level YAML shape hole means the helper
  itself does not yet satisfy that property for every valid YAML document.
- Repository-local config currently crosses nested Git boundaries, and textual path
  prefixes can misattribute siblings with a common string prefix.
- Snapshotting makes an attribution error durable until an explicit correction path
  changes the row; it also prevents later environmental drift from silently relabeling
  history.
- Reversing precedence would be a compatibility change for filters and registry entries.
  Changing path matching is localized but needs nested-repository and sibling-prefix
  characterization tests. Replacing the stored pair would require a data migration.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Constrain `.lionagi/config.toml` discovery to the nearest Git repository boundary; acceptance: nested-repository tests prove that configuration above the nearest Git root is ignored while configuration inside that root is selected. | S | (filled at issue-open time) |
| 2 | Replace string-prefix path overrides with path-component-aware containment; acceptance: an override for `/work/repo` matches descendants but does not match `/work/repository`. | S | (filled at issue-open time) |

## Alternatives considered

### Central project registry as the primary detector

A registry could give every directory an explicit centrally-managed label and remove
Git parsing. It would buy uniform editing and lookup. It lost because it becomes a
second inventory that can drift from repositories and makes an otherwise local checkout
depend on prior registration. The current `projects` table is a derived index, not the
source of detection truth.

### Git-remote-only attribution

Using only origin would be simple and portable across checkouts. It lost because local
and non-Git work have no origin, forks may need a shared logical label, and operators
need an explicit repository-owned name that does not change with remote configuration.

### Require explicit `--project`

Requiring a label would remove ambiguity and all filesystem probing. It lost because it
adds friction to every normal invocation and makes existing unconfigured automation
fail. Explicit input remains the strongest execution-boundary override when supplied.

### Let enclosing config intentionally define a workspace hierarchy

The current unbounded ancestor walk can be interpreted as an enclosing workspace label.
That would buy shared attribution across nested repositories. It was not selected because
the policy is implicit and can silently override a nested repository's own identity. The
delta chooses the narrower repository boundary; a broader hierarchy requires an explicit
future decision and representation.

### Longest path-prefix override

Sorting path overrides by length would make the most specific textual prefix win. It
would improve overlapping rules but still mis-handle component boundaries and symlinks.
Component-aware containment is the smaller correct target, so a textual longest-prefix
rule was not chosen.

### Re-detect on every read and overwrite stale attribution

This would automatically follow repository moves and settings edits. It lost because it
would rewrite historical provenance with current environment state and make filters
non-repeatable. Reads may use current detection to choose a scope, but stored executions
remain snapshots.

## Notes

The detector's best-effort policy is implemented by local catches in its source readers,
not by one outer exception boundary. Callers that require non-failure, notably mirror and
schedule creation, therefore add their own catch around the complete detector.
