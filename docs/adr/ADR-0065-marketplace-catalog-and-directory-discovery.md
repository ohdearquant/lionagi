# ADR-0065: Marketplace catalog and directory discovery

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: cli-surface
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0003, v0-0007

## Context

The repository publishes one official marketplace catalog in
`.claude-plugin/marketplace.json`. Its current inventory contains one `orchestrate`
plugin sourced from `marketplace/orchestrate`; the earlier four-plugin catalog is not
present.

**P1 — Publication and capability inventory are different questions.** The root
manifest answers which repository bundles are official. Skills and agent profiles are
files that can change independently inside a published bundle.

**P2 — Plugin metadata is not a second capability registry.** The current
`marketplace/orchestrate/.claude-plugin/plugin.json` carries package name, description,
version, attribution, links, license, and keywords. It has no `skills` or `agents`
arrays. Requiring such arrays would duplicate the directory tree.

**P3 — Studio needs a safe, current projection of both sources.** Studio reads official
source paths from the root manifest, scans each plugin directory, and exposes summary
and detail payloads. A malformed or escaping catalog path must not become arbitrary
filesystem access.

**P4 — Installed third-party plugins are discoverable but not official inventory.**
They live under a user cache with marketplace, plugin, and version directories. Studio
lists them as a separate source and does not write them into the repository manifest.

**P5 — Filesystem defects must degrade locally.** Missing directories, unreadable
Markdown, malformed/missing JSON metadata, and absent optional hook/MCP/readme files
should omit or default that field rather than make the entire plugin list unavailable.

| Concern | Decision |
|---|---|
| Official publication boundary | D1: `.claude-plugin/marketplace.json` is the sole official bundle inventory. |
| Bundle contract | D2: `plugin.json` supplies metadata; `skills/` and `agents/` supply capability inventory. |
| Catalog path safety | D3: official source paths must be relative, traversal-free, and contained under the repository root. |
| Directory scanning | D4: deterministic filesystem scans derive skill/agent summaries and optional capability flags. |
| Installed plugin source | D5: third-party cache discovery is appended separately, with lexicographic latest-version selection and no deduplication. |
| Studio projection | D6: list/detail/content endpoints expose fixed dictionary shapes assembled from those sources. |

This ADR deliberately does **not** decide:

- Plugin installation, activation, or update behavior; this is catalog and discovery
  only.
- Skill or agent execution semantics; discovery exposes their metadata/content but does
  not run them.
- A future split of `orchestrate` into additional installable bundles; the current
  catalog is recorded as shipped.
- Marketplace README generation; consistency with the manifest and directory scan is a
  retrospective delta.
- General filesystem publication outside plugin roots; D3 deliberately bounds official
  sources to the repository.

## Decision

### D1 — The root marketplace manifest is the official bundle inventory

The inventory-bearing portion of the current file is:

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "lionagi",
  "version": "1.0.0",
  "plugins": [
    {
      "name": "orchestrate",
      "source": "./marketplace/orchestrate",
      "description": "Multi-agent orchestration for lionagi: plan DAG workflows, author playbooks, run multi-play shows with quality gates."
    }
  ]
}
```

Catalog-level descriptive and attribution metadata is present in the file but does not
participate in Studio's plugin iteration. The discovery contract consumes only
`plugins[*].name`, `source`, and `description`.

The loader is:

```python
# lionagi/studio/services/plugins.py
def _iter_marketplace_plugins() -> list[tuple[Path, str, str]]: ...
```

Each tuple is `(resolved_plugin_dir, manifest_name, manifest_description)`.

**Exact semantics**:

- The primary manifest path is `<repo-root>/.claude-plugin/marketplace.json`.
- If that path is missing and the resolved marketplace directory has a parent manifest,
  the service uses that fallback.
- A missing manifest returns an empty official list.
- JSON read failure or a falsey decoded value returns an empty official list through the
  shared `_read_json` helper.
- A syntactically valid non-mapping JSON value is not rejected by `_read_json`; the
  subsequent `manifest.get(...)` access can raise. The checked-in manifest is a mapping,
  but runtime degradation is not total for every JSON shape.
- Entries are visited in manifest order.
- An entry with an empty/missing name is skipped.
- An empty, unsafe, missing, or non-existing source path is skipped.
- The loader does not scan arbitrary `marketplace/` children looking for additional
  official bundles.
- The present official inventory is exactly one bundle, `orchestrate`; installed cache
  copies with the same plugin name do not alter that statement.

**Why this way.** Publication is an intentional act. An explicit catalog prevents an
experimental or incomplete directory from becoming official merely because it exists.
The single manifest also gives external installers one stable inventory source.

### D2 — Plugin metadata and capability files have separate authorities

The current official bundle tree relevant to discovery is (support files omitted — per-role
`agents/<name>/*.md` reference docs and per-skill supporting files exist on disk but are invisible
to discovery, which globs only direct `agents/*.md` and each skill's `SKILL.md`):

```text
marketplace/orchestrate/
├── .claude-plugin/
│   └── plugin.json
├── README.md
├── agents/
│   ├── critic.md
│   └── orchestrator.md
└── skills/
    ├── debug/SKILL.md
    ├── orchestrate/SKILL.md
    ├── playbook/SKILL.md
    ├── pr-review/SKILL.md
    ├── review/SKILL.md
    ├── security-review/SKILL.md
    ├── show/SKILL.md
    ├── summarize/SKILL.md
    └── tdd/SKILL.md
```

The plugin metadata file's discovery-relevant fields are:

```json
{
  "name": "orchestrate",
  "description": "Multi-agent orchestration for lionagi: plan DAG workflows, author playbooks, run multi-play shows with quality gates, and monitor execution in Lion Studio.",
  "version": "1.0.0",
  "license": "Apache-2.0",
  "keywords": [
    "multi-agent",
    "orchestration",
    "dag",
    "playbook",
    "show",
    "lionagi"
  ]
}
```

`plugin.json` does not enumerate skills or agents. The current directory-derived
inventory is nine skills and two agents.

**Exact semantics**:

- Summary `name`, `description`, and `version` prefer truthy `plugin.json` values over
  manifest/cache fallback values.
- As with the root manifest, a syntactically valid non-mapping `plugin.json` can raise at
  `.get`; missing, unreadable, or malformed JSON instead becomes `{}`.
- Missing/falsey version defaults to the string `0.0.0`.
- Skill and agent counts are recomputed from directory scans each time a summary is
  built.
- `hooks/hooks.json` existence sets `has_hooks`; the file need not parse for the summary
  flag to be true.
- `.mcp.json` existence sets `has_mcp`. If absent, a truthy `plugin.json.mcpServers`
  also sets it.
- The current official bundle has neither hooks nor MCP configuration, so both flags
  are false.
- Files outside the direct `skills/<directory>` and `agents/*.md` patterns are support
  material, not separately enumerated capabilities.

**Why this way.** Package metadata changes at release cadence, while capability files
are the executable/document content itself. Using the directory as inventory removes a
second list that would need to change every time a skill or agent file is added,
renamed, or removed.

### D3 — Official source resolution is contained under the repository root

The path contract is:

```python
# lionagi/studio/services/plugins.py
def _resolve_marketplace_source(source_rel: str) -> Path | None: ...
```

Resolution applies:

```text
source_rel must be non-empty
and Path(source_rel) must be relative
and must not contain traversal
and resolve(repo_root / source_rel) must remain under resolve(repo_root)
```

**Exact semantics**:

- Absolute paths return `None`.
- Parent traversal returns `None` before resolution.
- A symlink that resolves outside the repository returns `None` at the containment
  check.
- `OSError` or `ValueError` during resolution/containment returns `None`.
- A safely resolved path is returned even if it does not exist; the caller then requires
  `plugin_dir.exists()` before adding the official entry.
- Containment is against the repository root, not only `MARKETPLACE_DIR`. The current
  catalog still points under `marketplace/`, but the safety contract allows another
  repository-contained source directory.

For named content lookup, the service additionally calls:

```python
safe_path_join(plugin_dir / "skills", skill_name)
safe_path_join(plugin_dir / "agents", agent_name)
```

before constructing the requested child path. A traversal attempt therefore raises
through the path-safety helper rather than reading outside the plugin root.

**Why this way.** The marketplace manifest is data read from disk and cannot be trusted
as an unrestricted path map. Repository containment permits flexible in-repository
layout while blocking absolute, traversal, and symlink escape paths.

### D4 — Skills and agents are discovered by deterministic directory scans

The scanners are:

```python
# lionagi/studio/services/plugins.py
def _scan_skills(plugin_dir: Path) -> list[dict[str, Any]]: ...
def _scan_agents(plugin_dir: Path) -> list[dict[str, Any]]: ...
```

Skill summary shape:

```python
{
    "name": str,
    "description": str,
}
```

Agent summary has the same shape.

**Skill scan semantics**:

- Missing `skills/` returns `[]`.
- Direct entries are sorted by filesystem name; non-directories are ignored.
- The Markdown candidate order is `SKILL.md`, `<directory-name>.md`, then the first
  `*.md` yielded by the directory glob.
- A directory with no candidate Markdown is omitted.
- An unreadable candidate is omitted.
- Frontmatter is parsed. Name is truthy frontmatter `name` or the directory name;
  description is truthy frontmatter `description` or `""`, converted to string and
  stripped.
- Malformed/absent frontmatter behavior follows the shared frontmatter parser; the
  scanner does not reject an otherwise readable file for missing fields.

**Agent scan semantics**:

- Missing `agents/` returns `[]`.
- Only direct `agents/*.md` files are considered, sorted by path.
- An unreadable file is omitted.
- Name is always the file stem. Agent frontmatter `name` is not consulted.
- Description is frontmatter `description` or `""`, converted to string and stripped.
- The current `critic.md` and `orchestrator.md` have no description field, so Studio
  reports empty descriptions for both even though their bodies describe their roles.

Full content lookup has additional shapes:

```python
# get_plugin_skill(...)
{
    "name": str,
    "description": str,
    "path": str,
    "allowed_tools": list[Any],
    "content": str,
}

# get_plugin_agent(...)
{
    "name": str,
    "description": str,
    "path": str,
    "content": str,
}
```

For a skill, scalar `allowed-tools` is normalized to a one-element list, an existing list
is retained, and absence becomes `[]`. `content` is the post-frontmatter body. Agent
content uses the file stem after removing an optional `.md` suffix from the request.

**Why this way.** Sorted direct-child scans are deterministic and make the file tree the
source of truth. Lenient metadata fallback keeps a partially documented capability
visible while unreadable or misplaced files do not create stale phantom entries.

### D5 — Installed third-party discovery is separate and append-only

The installed-cache contract is:

```python
# lionagi/studio/services/plugins.py
THIRDPARTY_DIR = Path.home() / ".claude" / "plugins" / "cache"

def _iter_thirdparty_plugins(
) -> list[tuple[Path, str, str, str]]: ...
```

The expected layout is:

```text
~/.claude/plugins/cache/
└── <marketplace_name>/
    └── <plugin_name>/
        └── <version>/
            ├── .claude-plugin/plugin.json
            ├── skills/
            └── agents/
```

Each tuple is `(plugin_dir, name, description, marketplace_name)`.

**Exact semantics**:

- A missing cache root returns `[]`.
- Marketplace and plugin directories are iterated in sorted order; non-directories are
  ignored.
- Direct version subdirectories are sorted lexicographically, and the final entry is
  selected. This is not semantic-version comparison.
- A plugin directory with no version subdirectory is skipped.
- Name and description prefer plugin metadata, with the cache plugin-directory name and
  empty string as fallbacks.
- `list_plugins` emits official summaries first, then third-party summaries. It does not
  deduplicate equal names.
- `get_plugin`, skill lookup, and agent lookup search official entries first, then
  third-party entries. An official plugin therefore shadows an installed plugin with the
  same name for detail/content lookup even though both may appear in the list.
- The third-party tuple's marketplace name becomes the summary `source`; official
  summaries use the literal source `marketplace`.
- Cache shape is trusted structurally: an unexpected directory tree may be projected as
  a plugin with metadata defaults. It still remains separate from official inventory.

**Why this way.** Installed plugins are useful to Studio users, but local cache contents
must not redefine what the repository publishes. Appending sources preserves visibility;
official-first lookup gives the root manifest deterministic precedence.

### D6 — Studio exposes summary, detail, and content payloads

The internal builders are:

```python
def _plugin_summary(
    plugin_dir: Path,
    name: str,
    description: str,
    source: str,
) -> dict[str, Any]: ...

def _plugin_detail(
    plugin_dir: Path,
    name: str,
    description: str,
    source: str,
) -> dict[str, Any]: ...

def list_plugins() -> list[dict[str, Any]]: ...
def get_plugin(name: str) -> dict[str, Any] | None: ...
def get_plugin_skill(plugin_name: str, skill_name: str) -> dict[str, Any] | None: ...
def get_plugin_agent(plugin_name: str, agent_name: str) -> dict[str, Any] | None: ...
```

Summary payload:

```python
{
    "name": str,
    "description": str,
    "version": str,
    "source": str,
    "skill_count": int,
    "agent_count": int,
    "has_hooks": bool,
    "has_mcp": bool,
    "path": str,
}
```

Detail payload extends the summary:

```python
{
    **summary,
    "skills": list[{"name": str, "description": str}],
    "agents": list[{"name": str, "description": str}],
    "hooks": Any | None,
    "mcp": Any | None,
    "readme": str | None,
}
```

The HTTP projection is:

| Route | Success | Miss |
|---|---|---|
| `GET /plugins` | `{"plugins": list_plugins()}` | Empty list is valid |
| `GET /plugins/{name}` | Detail object | 404 `Plugin <name> not found` |
| `GET /plugins/{plugin_name}/skills/{skill_name}` | Skill content object | 404 |
| `GET /plugins/{plugin_name}/agents/{agent_name}` | Agent content object | 404 |

Filesystem work is moved to a worker thread with `anyio.to_thread.run_sync` before the
async route returns.

**Exact optional-file semantics**:

- Detail reads `hooks/hooks.json` only when it exists; read/parse failure follows the
  shared JSON helper and yields its falsey/default result.
- MCP detail prefers `.mcp.json`; only when absent does it fall back to
  `plugin.json.mcpServers`; a falsey value becomes `None`.
- README is `README.md` text when readable, otherwise `None`.
- Response paths pass through `public_path`, so the service exposes a bounded display
  path rather than an arbitrary absolute host path.
- Summary scanning and detail scanning are separate calls; a concurrent directory
  change can make counts and arrays differ within one detail build. No snapshot lock is
  taken.

**Why this way.** Small plain dictionaries keep the Studio service independent of plugin
execution code. Summary payloads are cheap for lists; details and full content are read
only when requested. Thread offload prevents synchronous filesystem access from blocking
the async server loop.

## Consequences

- Adding or removing a skill/agent file changes discovery without a second manifest
  edit.
- The official publication boundary remains explicit even when other directories or
  installed cache entries exist.
- Consumers that read only `plugin.json` cannot obtain capability inventory; they must
  follow the directory convention or call Studio discovery.
- Missing or unreadable capability files are omitted rather than represented by stale
  arrays. This favors current readable content over diagnostic completeness.
- Official and installed plugins with the same name can both appear in lists, while
  official-first detail lookup returns only the official one.
- Lexicographic version selection is simple but can choose a different directory than
  semantic-version ordering.
- Reversing the publication boundary would be high cost because arbitrary directories
  could become official. Changing scan fallback order is moderate and can change names
  or counts. Adding explicit arrays would require dual-source reconciliation or a
  migration away from directory authority.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Add a marketplace consistency check that validates every official source path, matches each catalog name to its plugin metadata, and confirms directory-discovered skills and agents are readable; acceptance: catalog or layout drift fails the focused test. | S | (filled at issue-open time) |
| 2 | Generate or validate the marketplace README inventory from the root manifest and directory scan; acceptance: the documented official plugins and capability lists cannot disagree with repository discovery. | S | (filled at issue-open time) |

## Alternatives considered

### Explicit `skills` and `agents` arrays in plugin.json

Arrays would make inventory available without filesystem scanning and could validate
intentional ordering. They lost because they duplicate the executable directory
inventory and require two edits for every capability change. Drift would produce an
unanswerable question about whether the manifest or file is authoritative.

### Treat every marketplace subdirectory as official

Directory enumeration would remove the root manifest and make new bundles zero-config.
It lost because mere presence is not publication intent: incomplete, experimental, or
support directories could become official. The explicit catalog is the required
boundary.

### Put all metadata in the root manifest

One file could describe bundle version, hooks, MCP, skills, and agents. It would make
catalog reads self-contained. It lost because bundle-local metadata would be separated
from the installed unit and the root file would repeat both `plugin.json` and directory
content.

### Merge official and installed entries by plugin name

Deduplication would simplify Studio lists. It lost because equal names can represent
different sources or versions, and silently choosing one would hide installed state.
The current design shows both and makes official-first detail precedence explicit.

### Semantic-version selection in the installed cache

Parsing versions would choose `10.0.0` over `9.0.0` correctly and reject non-version
directories. It was not selected in the shipped implementation because the cache scan
uses no packaging dependency and accepts hash-like version directories. Lexicographic
latest is simple but must not be described as semantic latest.

### Fail the complete plugin list on one malformed entry

Strict failure would surface catalog and file defects immediately. It lost for the
runtime surface because one unreadable optional file should not hide every healthy
plugin. The consistency-test delta is the correct place for strict repository
validation; runtime discovery remains locally degrading.

### Split the current bundle into multiple hypothetical plugins

A finer catalog could install review, orchestration, and development methods
independently. It is deferred because the repository currently ships one coherent
installable bundle and no separate publication contract exists. The ADR records current
architecture rather than inventing products not present in source.

## Notes

The old multi-plugin inventory is historical. Current truth is the one-entry root
manifest plus the capability files reachable beneath that entry's source directory.
