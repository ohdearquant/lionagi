# `AgentSpec` and `create_agent()`

```python
from lionagi.agent import AgentSpec, create_agent, PermissionPolicy
from lionagi.agent.hooks import guard_destructive, guard_paths, log_tool_use
```

`AgentSpec` captures what an agent needs — role/identity, model, tools, hooks, permissions,
emission grants — in a single serializable object. `create_agent()` wires it into a
ready-to-use `Branch`.

---

## `AgentSpec`

```python
@dataclass
class AgentSpec(HooksMixin)
```

Source: `lionagi/agent/spec.py`

An `AgentSpec` pairs a `Profile` (role + modes identity) with runtime concerns. Build one with
`AgentSpec.compose(role, ...)` or the `AgentSpec.coding()` preset rather than constructing the
dataclass directly.

### Fields

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `profile` | `Profile` | — | Role + modes identity (set by `compose()`) |
| `model` | `str \| None` | `None` | Model spec: `"provider/model"` or bare alias |
| `effort` | `str \| None` | `None` | Override effort level (e.g. `"high"`, `"xhigh"`) |
| `tools` | `tuple[str, ...]` | `()` | Tool presets to register: `"coding"`, `"reader"`, `"editor"`, `"bash"`, `"search"` |
| `permissions` | `PermissionPolicy \| None` | `None` | Permission rules; see `PermissionPolicy` |
| `grant_emissions` | `bool` | `True` | Grant the role's declared capability-emission models |
| `emits` | `tuple \| None` | `None` | Override *what* is granted; `None` uses the role contract |
| `pack` | `str \| Pack \| None` | `"default"` | Policy pack for the role-policy prompt block |
| `lion_system` | `bool` | `True` | Prepend the lionagi system preamble to the system prompt |
| `extra_prompt` | `str \| None` | `None` | Extra literal prompt text (set via `system_prompt=`) |
| `hook_handlers` | `dict[str, list[Callable]]` | `{}` | Phase-keyed hooks (`"pre:bash"`, `"post:*"`, `"error:editor"`) |
| `cwd` | `str \| None` | `None` | Working directory for tools and MCP discovery |
| `yolo` | `bool` | `False` | Auto-approve all tool calls (pass-through to provider kwargs) |
| `mcp_servers` | `list[str] \| None` | `None` | MCP server names to load from `.mcp.json` |
| `mcp_config_path` | `str \| None` | `None` | Explicit path to `.mcp.json` (overrides auto-discovery) |

`mcp_servers` and `mcp_config_path` are not `compose()` keyword arguments — set them as
attributes after building the spec:

```python
spec = AgentSpec.coding()
spec.mcp_servers = ["khive"]
spec.mcp_config_path = "/path/to/project/.mcp.json"
```

### Hook methods

Inherited from `HooksMixin`:

```python
spec.pre("bash", handler)       # register a pre-hook for the bash tool
spec.post("editor", handler)    # register a post-hook for the editor tool
spec.on_error("*", handler)     # register an error hook for all tools
```

- `pre` hooks: `async (tool_name: str, action: str, args: dict) -> dict | None`
  Return a modified `args` dict to rewrite the call, or raise `PermissionError` to block.
- `post` hooks: `async (tool_name: str, action: str, args: dict, result: dict) -> dict | None`
  Return a modified `result` dict, or `None` to pass through unchanged.
- Tool name `"*"` matches all tools.

### `AgentSpec.compose()`

```python
@classmethod
def compose(
    cls,
    role: Any,
    *,
    modes: list[Any] | None = None,
    model: str | None = None,
    effort: str | None = None,
    tools: tuple[str, ...] | list[str] = (),
    permissions: Any = None,
    pack: str | Pack | None = "default",
    grant_emissions: bool = True,
    emits: tuple | None = None,
    system_prompt: str | None = None,
    cwd: str | None = None,
    yolo: bool = False,
) -> AgentSpec
```

Build an `AgentSpec` from a role name/object plus optional overrides. `permissions` accepts a
`PermissionPolicy`, a plain dict (YAML-shaped), or a preset name (`"safe"`, `"read_only"`,
`"allow_all"`, `"deny_all"`). `system_prompt` becomes the spec's `extra_prompt`.

```python
spec = AgentSpec.compose("implementer", tools=["coding"], model="openai/gpt-4.1")
```

### `AgentSpec.coding()`

```python
@classmethod
def coding(
    cls,
    *,
    model: str | None = None,
    effort: str | None = "high",
    system_prompt: str | None = None,
    cwd: str | None = None,
    secure: bool = True,
    **kwargs,
) -> AgentSpec
```

Preset for a coding agent — `implementer` role + `tools=["coding"]` (reader, editor, bash,
search, context, subagent). Extra `**kwargs` flow through to `compose()`.

By default (`secure=True`) it wires two guards:

- `guard_destructive` as a pre-hook on `bash` — blocks destructive shell commands
  (`rm -rf`, force-push, etc.).
- `guard_paths` as a pre-hook on `reader` and `editor` — restricts file access to the
  workspace root (`cwd` if provided, else `Path.cwd()` at call time).

Set `secure=False` to disable these defaults and manage hooks manually.

```python
spec = AgentSpec.coding(model="openai/gpt-4.1", cwd="/path/to/project")
```

### `AgentSpec.from_yaml()`

```python
@classmethod
def from_yaml(cls, path: str | Path) -> AgentSpec
```

Load a spec from a YAML file. Hook callables are code-only and are not serialized.

```yaml
# example .lionagi/agents/coder/coder.yaml
role: implementer
model: openai/gpt-4.1
effort: high
tools: [coding]
system_prompt: |
  You are a coding agent...
permissions:
  mode: rules
  allow:
    reader: ["*"]
    search: ["*"]
    bash: ["git *", "cargo *", "uv *"]
  deny:
    bash: ["rm -rf *", "sudo *"]
```

### `AgentSpec.to_yaml()`

```python
def to_yaml(self, path: str | Path) -> None
```

Save spec fields to YAML. `hook_handlers` (callables) are omitted.

---

## `create_agent()`

```python
async def create_agent(
    config: AgentSpec,
    *,
    load_settings: bool = True,
    project_dir: str | None = None,
    trust_project_settings: bool = False,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
    chat_model: Any = None,
    log_config: Any = None,
) -> Branch
```

Source: `lionagi/agent/factory.py`

Creates a fully configured `Branch` from an `AgentSpec`. Wires: settings → hooks →
system prompt → model → tools → MCP → emissions.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `config` | `AgentSpec` | — | Agent specification |
| `load_settings` | `bool` | `True` | Load hooks from `~/.lionagi/settings.yaml` |
| `project_dir` | `str \| None` | `None` | Project root for settings resolution; auto-detected if `None` |
| `trust_project_settings` | `bool` | `False` | Also load `.lionagi/settings.yaml` from the project dir |
| `trusted_hook_modules` | `set[str] \| None` | `None` | Python modules allowed for import-based hooks; defaults to `{"lionagi.agent.hooks"}` |
| `chat_model` | `iModel \| None` | `None` | Prebuilt model to use verbatim; skips `spec.model` parsing |
| `log_config` | `DataLoggerConfig \| dict \| None` | `None` | Logging config forwarded to the `Branch` |

Returns a `Branch` ready for use with all tools registered and hooks attached.

```python
spec = AgentSpec.coding(model="openai/gpt-4.1")
branch = await create_agent(spec)
response = await branch.chat("Refactor the auth module")
```

**Settings loading order** (project-local wins):

1. `~/.lionagi/settings.yaml` — always loaded when `load_settings=True`
2. `.lionagi/settings.yaml` — loaded only when `trust_project_settings=True`

---

## `PermissionPolicy`

```python
@dataclass
class PermissionPolicy
```

Source: `lionagi/agent/permissions.py`

Per-tool allow/deny/escalate rules evaluated before each tool call. Three modes:

| Mode | Behavior |
|------|----------|
| `"allow_all"` | All tool calls permitted (default) |
| `"deny_all"` | All tool calls blocked |
| `"rules"` | Check deny → allow → escalate lists; default deny if no rule matches |

### Fields

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `mode` | `str` | `"allow_all"` | `"allow_all"` \| `"deny_all"` \| `"rules"` |
| `allow` | `dict[str, list[str]]` | `{}` | Tool → list of fnmatch patterns that permit the call |
| `deny` | `dict[str, list[str]]` | `{}` | Tool → list of fnmatch patterns that block the call |
| `escalate` | `dict[str, list[str]]` | `{}` | Tool → list of patterns that trigger `on_escalate` |
| `on_escalate` | `Callable \| None` | `None` | Async callable invoked on escalation; return `True` to allow, a `dict` to rewrite args |

Tool names in `allow`/`deny`/`escalate` are normalized: `"bash_tool"` → `"bash"`, etc.
`"*"` as a tool key applies to all tools.

### Preset class methods

```python
PermissionPolicy.allow_all()   # mode="allow_all"
PermissionPolicy.deny_all()    # mode="deny_all"

# reader + search allowed; editor + bash denied
PermissionPolicy.read_only()

# reader + editor + search allowed; dangerous bash commands denied; other bash → escalate
PermissionPolicy.safe()
```

### `from_dict()`

```python
@classmethod
def from_dict(cls, data: dict) -> PermissionPolicy
```

Build from a plain dict (e.g. loaded from YAML):

```python
policy = PermissionPolicy.from_dict({
    "mode": "rules",
    "allow": {"reader": ["*"], "bash": ["git *", "uv *"]},
    "deny": {"bash": ["rm *", "sudo *"]},
})
```

### Pattern matching

For the `bash` tool, patterns are matched against the command string.
For `editor` and `reader`, patterns are matched against the file path.
Shell control operators (`;`, `&&`, `||`, `|`, backticks, `$()`, redirects) in bash commands
are blocked unconditionally before pattern matching — they cannot be allow-listed.

### Using with `AgentSpec`

```python
# Preset name (resolved by compose())
spec = AgentSpec.compose("implementer", tools=["coding"], permissions="safe")

# Dict form (round-trips through YAML)
spec.permissions = PermissionPolicy.from_dict({
    "mode": "rules",
    "allow": {"reader": ["*"], "bash": ["git *"]},
    "deny": {"bash": ["rm *"]},
})

# Object form (code-only)
spec.permissions = PermissionPolicy.safe()
```

---

## Built-in hooks

Source: `lionagi/agent/hooks.py`

### `guard_destructive`

```python
async def guard_destructive(tool_name: str, action: str, args: dict) -> dict | None
```

Pre-hook for `bash`. Raises `PermissionError` when the command matches a destructive pattern:
`rm -rf`, `git push --force`, `git reset --hard`, `git clean -fd`, `DROP TABLE`,
`DROP DATABASE`, `TRUNCATE TABLE`, `mkfs`, `dd if=`, writes to `/dev/sd*`.

```python
spec.pre("bash", guard_destructive)
```

### `guard_paths()`

```python
def guard_paths(
    allowed_paths: list[str] | None = None,
    denied_paths: list[str] | None = None,
) -> Callable
```

Factory that returns a pre-hook restricting file access by path. Applied to `reader` and `editor`.

- `allowed_paths`: if set, any path outside these roots raises `PermissionError`. A relative
  path resolves against the first allowed root, and the resulting candidate — like any
  absolute path — is accepted if it resolves under any configured root.
- `denied_paths`: patterns (absolute paths, filenames, or substrings) that are always blocked.

Workspace containment delegates to `lionagi.libs.path_safety.resolve_workspace_path`: a direct
symlink (even one whose target is inside the workspace) is refused before it is followed, and a
fixed set of protected basenames (`.env`, `.netrc`, SSH private keys, `.htpasswd`) is denied even
when no `denied_paths` are supplied, matched case-insensitively so a spelling like `.ENV` is
denied on case-insensitive filesystems too. With no `allowed_paths` configured, path access is
deny-only — the process working directory is never treated as an implicit allowlist — but the
same symlink refusal and protected-basename floor still apply before `denied_paths` is checked.

This guard validates a pathname at check time; it does not hold a descriptor across the check
and the tool's later I/O. A racing filesystem mutation between the check and that I/O (for
example, swapping a validated regular file for a symlink) is outside this guard's threat model —
it assumes a cooperative filesystem, not an adversarial one under concurrent write access.

```python
spec.pre("reader", guard_paths(allowed_paths=["/path/to/project/"]))
spec.pre("editor", guard_paths(denied_paths=[".env", "*.key"]))
```

### `log_tool_use`

```python
async def log_tool_use(tool_name: str, action: str, args: dict, result: dict) -> dict | None
```

Post-hook for any tool. Logs `tool=<name> action=<action> success=<bool>` at `INFO` level
via the standard `logging` module. Returns `None` (does not modify result).

```python
spec.post("*", log_tool_use)
```

### `auto_format_python`

```python
async def auto_format_python(tool_name: str, action: str, args: dict, result: dict) -> dict | None
```

Post-hook for `editor`. Runs `ruff format <file_path>` on successfully edited `.py` files.

```python
spec.post("editor", auto_format_python)
```

---

Next: [`SandboxSession`](sandbox.md) — isolated worktree execution
