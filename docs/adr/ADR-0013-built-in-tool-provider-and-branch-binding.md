# ADR-0013: Built-in tool provider and Branch binding

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: actions-tools
- **Date**: 2026-07-09
- **Relations**: extends ADR-0011

## Context

ADR-0011 defines the executable `Tool` descriptor and branch registry. Built-in tools
still need a construction boundary: some adapters are reusable objects with no branch
dependency, while others close over a branch, exchange, history, or mutable per-branch
state. Five concrete problems shaped the shipped provider layer.

**P1 — Maintained built-ins need a richer input contract than raw signature
inspection.** The file reader/editor, shell, search, diagnostics, navigation, and
syntax-search adapters pair Pydantic request/response models with async handlers. Their
request models become provider schemas and their handlers enforce workspace containment,
timeouts, caps, and typed failure values (`lionagi/tools/file/reader.py`;
`lionagi/tools/file/editor.py`; `lionagi/tools/code/`).

**P2 — The common marker describes only the stateless case.** `LionTool` requires a
single synchronous `to_tool() -> Tool`. `Branch.register_tools()` recognizes that
marker, instantiates marker classes without arguments, calls `to_tool()`, and registers
the result. The interface has no construction context, multi-tool output, lifecycle, or
clone contract (`lionagi/tools/base.py`; `lionagi/session/branch.py`).

**P3 — Three providers are factories despite inheriting from the marker.**
`ContextTool` needs branch messages and progression. `LionMessenger` needs a branch,
exchange, and roster. `CodingToolkit` builds multiple tools around branch history and
mutable file state. Each exposes `bind(...)` and deliberately raises from `to_tool()`;
generic branch registration therefore cannot consume the declared hierarchy uniformly
(`lionagi/tools/context/context.py`;
`lionagi/tools/communication/messenger.py`; `lionagi/tools/coding.py`).

**P4 — The coding toolkit adds stateful value but duplicates basic adapters.** Its
read-before-edit guard, context-aware invalidation, nudge integration, sandbox session,
and subagent construction are branch-scoped. At the same time it implements reader,
editor, shell, search, diagnostics, navigation, and syntax search again, using shared
request models and selected helpers but returning independently assembled dictionaries.
Defaults and failure shapes can therefore drift from standalone adapters
(`lionagi/tools/coding.py`).

**P5 — Branch cloning copies descriptors, not provider intent.** `Branch.clone()`
creates a new manager and passes it the original registry's `Tool` objects. The
descriptors and callable closures are reused without a scope or rebinding check. A tool
created by `ContextTool.bind()`, `LionMessenger.bind()`, or `CodingToolkit.bind()` can
remain closed over the source branch after registration on the clone
(`lionagi/session/branch.py`).

| Concern | Decision |
|---|---|
| Provider marker | D1: `LionTool` is a minimal one-tool adapter marker, and generic branch registration calls `to_tool()`. |
| Stateless built-ins | D2: maintained standalone adapters cache one Pydantic-backed `Tool` and return typed result dictionaries. |
| Context-bound factories | D3: context and messenger providers expose explicit `bind(...)` methods and reject generic `to_tool()`. |
| Stateful toolkit | D4: `CodingToolkit.bind(branch)` returns a configurable list of branch-bound tools with local state and parallel basic implementations. |
| Clone behavior | D5: branch cloning copies registered `Tool` descriptors by reference and performs no provider rebinding. |

This ADR does **not** decide:

- Function-schema derivation, registry duplicate handling, or remote MCP normalization;
  ADR-0011 owns those contracts.
- Authorization, event status, hooks, and action-message ordering; ADR-0012 owns the
  execution transaction.
- The internal implementation of sandbox or subagent orchestration. This ADR records
  only that those optional tool factories are part of the coding provider's output.
- Resource and prompt graph behavior. Their current co-location with `LionTool` is
  recorded because it affects module cohesion, not because this ADR governs them.

## Decision

### D1 — `LionTool` is a minimal single-tool adapter marker

The complete provider interface is:

```python
# lionagi/tools/base.py
class LionTool(ABC):
    is_lion_system_tool: bool = True
    system_tool_name: str

    @abstractmethod
    def to_tool(self) -> Tool:
        pass
```

The same module also defines graph-resource types unrelated to registration:

```text
lionagi/tools/base.py
├── LionTool
├── ResourceCategory
├── ResourceMeta(BaseModel)
├── Resource(Node)
└── Prompt(Resource)
```

Branch registration is:

```python
# lionagi/session/branch.py
def _register_tool(
    self,
    tools: FuncTool | LionTool,
    update: bool = False,
): ...

def register_tools(
    self,
    tools: FuncTool | list[FuncTool] | LionTool,
    update: bool = False,
): ...
```

**Exact semantics**

- A class object that is a `LionTool` subclass is instantiated with no arguments.
- A `LionTool` instance is converted by calling `to_tool()` synchronously.
- The result is handed to `ActionManager.register_tool()`, so it must be one
  registry-acceptable `Tool`, raw callable, or one-entry MCP dictionary.
- A top-level list is iterated and each element follows the same path. The marker
  contract itself cannot return multiple tools; if `to_tool()` returns a list, manager
  registration rejects it.
- `update` is forwarded to each registry insertion.
- A provider whose constructor requires context cannot be passed as a class to generic
  registration; zero-argument instantiation fails before `to_tool()`.
- A branch-bound provider instance reaches `to_tool()` and receives that provider's
  deliberate `NotImplementedError`.
- `is_lion_system_tool` and `system_tool_name` are conventions used by built-ins; the
  abstract base enforces neither value nor uniqueness.

**Why this way**

The marker made the common stateless case concise: a provider object can own setup and
cache while the branch receives an ordinary `Tool`. It deliberately avoided a larger
factory framework. Once providers required branch context or multiple outputs, the
single-method shape stopped satisfying substitutability but remained the inherited base.

### D2 — Standalone built-ins cache one Pydantic-backed tool

The stateless provider modules are:

```text
lionagi/tools/
├── _subprocess.py
├── file/
│   ├── reader.py       ReaderTool
│   └── editor.py       EditorTool
└── code/
    ├── bash.py         BashTool
    ├── search.py       SearchTool
    ├── check.py        CodeCheckTool
    ├── nav.py          NavTool
    └── ast_search.py   AstSearchTool
```

Every class owns `_tool`, initializes it to `None`, and constructs the descriptor only
on the first `to_tool()` call. Subsequent calls return the same `Tool` object. Each
wrapper is async, reconstructs its request model from `**kwargs`, awaits
`handle_request()`, and returns `response.model_dump()`.

The provider-visible request contracts are:

```python
# reader.py
class ReaderRequest(BaseModel):
    action: ReaderAction                   # read | open | list_dir
    path: str
    offset: int | None = None
    limit: int | None = None
    recursive: bool | None = None
    file_types: list[str] | None = None

class ReaderResponse(BaseModel):
    success: bool
    content: str | None = None
    error: str | None = None

# editor.py
class EditorRequest(BaseModel):
    action: EditorAction                   # write | edit
    file_path: str
    content: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False

class EditorResponse(BaseModel):
    success: bool
    content: str | None = None
    error: str | None = None

# bash.py
class BashRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    timeout: int | None = None              # milliseconds
    cwd: str | None = None

class BashResponse(BaseModel):
    stdout: str = ""
    stderr: str = ""
    return_code: int
    timed_out: bool = False
```

```python
# search.py
class SearchRequest(BaseModel):
    action: SearchAction                    # grep | find
    pattern: str
    path: str | None = "."
    include: str | None = None
    max_results: int | None = 50

class SearchResponse(BaseModel):
    success: bool
    content: str | None = None
    count: int = 0
    error: str | None = None

# check.py
class CodeCheckRequest(BaseModel):
    paths: list[str]
    tool: Literal["ruff"] = "ruff"
    max_diagnostics: int = Field(50, ge=1, le=500)

class CodeCheckResponse(BaseModel):
    status: Literal["ok", "diagnostics", "unavailable", "error"]
    diagnostics: list[CodeDiagnostic] = Field(default_factory=list)
    summary: str = ""
    tool: str = "ruff"

# nav.py
class NavRequest(BaseModel):
    action: str                             # outline | find_definition | find_references
    path: str
    symbol: str | None = None

class NavResponse(BaseModel):
    success: bool
    items: list[NavItem] = Field(default_factory=list)
    error: str | None = None
```

```python
# ast_search.py
class AstSearchRequest(BaseModel):
    pattern: str
    path: str = "."
    lang: Literal[
        "python", "rust", "typescript", "javascript", "go", "c", "cpp"
    ] = "python"
    max_results: int = Field(50, ge=1, le=500)

class AstSearchResponse(BaseModel):
    status: Literal["ok", "matches", "unavailable", "error"]
    matches: list[AstSearchMatch] = Field(default_factory=list)
    summary: str = ""
    total: int = 0
```

The response item shapes used by diagnostics and navigation are:

```python
class CodeDiagnostic(BaseModel):
    file: str
    line: int
    col: int
    end_line: int | None = None
    end_col: int | None = None
    severity: Literal["error", "warning", "info"] = "warning"
    code: str = ""
    message: str
    source: str = "ruff"
    fixable: bool = False

class NavItem(BaseModel):
    kind: str
    name: str
    line: int
    col: int
    signature: str | None = None

class AstSearchMatch(BaseModel):
    file: str
    line: int
    col: int
    text: str
```

**Exact shared semantics**

- Reader, editor, diagnostics, navigation, and syntax-search constructors resolve
  `workspace_root or Path.cwd()` once to an absolute path. Search freezes an explicitly
  supplied root once; `workspace_root=None` leaves standalone search uncontained and
  resolves paths against the current process.
- The shared path resolver rejects a direct symlink, paths escaping the workspace, and
  protected final names such as `.env`, `.netrc`, and private-key filenames
  (`lionagi/libs/path_safety.py`).
- Expected operational failures are returned inside typed response objects rather than
  raised. Pydantic request validation can still raise before the handler returns.
- Known blocking file, subprocess, and AST work is sent through `run_sync()` by the async
  handlers, avoiding the generic inline-sync behavior in ADR-0012 D6.

**Reader semantics and budgets**

- `read` rejects missing/non-files, symlinks, and files whose first 8192 bytes contain a
  NUL. The prefix bounds binary sniffing before the full read; the exact 8192-byte value
  is inherited with no recorded tuning rationale. It decodes UTF-8 with replacement and
  returns `<one-based-line-number>\t<text>` rows.
- Negative offsets clamp to zero. Missing, zero, or negative `limit` becomes 2000 lines.
  The 2000-line window is inherited; no source comment records why that exact value was
  selected.
- `list_dir` uses `recursive` and extension filters, truncating the file list to 1,000.
  The cap bounds returned context; no recorded rationale explains the exact 1,000 value.
- `open` accepts local `.pdf`, `.pptx`, `.docx`, `.html`, and `.htm` files up to
  50 MiB. The size protects document conversion from an unbounded input; the exact
  50 MiB choice has no recorded rationale.
- Remote `open` accepts only `https` URLs whose host is explicitly configured and still
  resolves to a public, SSRF-safe address. The coding toolkit supplies an empty host set,
  so its bound reader rejects all remote URLs.
- Converted text is cached by the original path and read through the same numbered-line
  window. Cache expiry is hard-coded at 300 seconds (documented as five minutes).
  `ReaderTool(cache_ttl=...)` stores the caller value, but `_read_cached()` and
  `_evict_expired()` read the module constant, so the constructor value does not alter
  expiry.
- Missing `docling` returns an actionable `success=False` response; conversion exceptions
  are returned as errors.

**Editor semantics**

- `write` requires non-`None` content, creates parent directories, and overwrites the
  target. The standalone adapter's instruction to read first is advisory; no standalone
  read-state check exists.
- `edit` requires both strings, rejects a missing file, and requires an exact match. Zero
  matches returns whitespace/line-prefix hints. Multiple matches fail unless
  `replace_all=True`.
- The edit write opens the already resolved file with `O_NOFOLLOW` when the platform
  supports it. Success returns a bounded nearby snippet; the 40-character context on
  each side and 200-character fallback are inherited presentation values with no
  recorded rationale.

**Shell and search semantics and budgets**

- Bash uses `shlex.split()`, `shell=False`, and rejects `;`, `&&`, `||`, pipes,
  redirects, newlines, backticks, and `$()` before process launch.
- Bash timeout defaults to 30,000 ms when omitted and clamps supplied values into
  1–300,000 ms; an explicit zero or negative value therefore becomes 1 ms. The default
  targets ordinary commands and the five-minute maximum bounds long-running calls. No
  source note records evidence for the exact values.
- The subprocess starts a new process group and terminates that group on timeout. It
  drains both streams and stores at most 100,000 bytes per stream while continuing to
  drain to avoid child-process deadlock. After a timeout, each drain thread gets a
  one-second join window. The output cap protects model context; the exact byte and join
  values are inherited without recorded tuning evidence.
- Grep and find each use fixed argv with `shell=False` and a fixed 30-second timeout.
  Grep exit codes `0` and `1` are success (`1` is an empty match); every other grep code
  is an error. Find reports a nonzero code as an error only when stderr is non-empty; a
  nonzero code with empty stderr falls through to a successful result.
- Standalone search defaults to 50 results for both actions because the model field and
  handler both use 50. Its field description says find defaults to 100, but that value is
  not the standalone runtime behavior.
- `SearchRequest.max_results` has no numeric constraint. `None` or zero becomes 50;
  a negative value remains truthy and is passed to Python's `lines[:max_results]` slice,
  dropping that many trailing rows rather than raising a validation error.

**Diagnostics and navigation semantics and budgets**

- Code check supports only the `ruff` binary. Missing `ruff` is `unavailable`, findings
  are `diagnostics`, and no findings are `ok`. Ruff gets 30 seconds. An exit code of 2 or
  greater becomes `error` when stdout is empty; non-empty stdout is still parsed and
  classified by its contents. Malformed non-empty JSON becomes `error`.
- Diagnostics are sliced to `max_diagnostics`, default 50 and validated within 1–500.
  The bound limits response noise; no source record justifies the exact ceiling.
- An unexpected Ruff or ast-grep exit includes at most 300 characters of stderr in its
  summary. This is an inherited diagnostic-presentation cap with no recorded rationale.
- Navigation uses the Python standard-library AST for one contained file. Omitting the
  `symbol` argument fails the two symbol actions; a supplied but absent symbol returns a
  successful empty item list. Syntax/read errors become `success=False`.
- `outline` reports class, function, and method nodes. `find_definition` matches classes,
  sync/async functions, and simple name assignments/annotated assignments; it is not an
  import or attribute resolver. `find_references` reports every matching `ast.Name`
  regardless of load/store context and every `ast.Attribute` whose final attribute
  matches, so its results are syntactic occurrences rather than semantic references.
- AST search locates `sg` or `ast-grep`, returns `unavailable` if absent, uses a
  30-second subprocess timeout, and accepts 1–500 results with default 50. It accepts JSON
  arrays or NDJSON and converts zero-based tool lines to one-based response lines.

**Why this way**

Pydantic-backed adapters make model schema and runtime validation share a source, while
typed response dictionaries keep expected operational failure visible to a reasoning
loop. Per-provider caching avoids rebuilding descriptors. The repeated wrapper pattern
is straightforward, but it leaves operational constants distributed across modules and
does not by itself solve branch-bound construction.

### D3 — Context and messenger are explicit branch-bound factories

The context factory contract is:

```python
# lionagi/tools/context/context.py
class ContextAction(str, Enum):
    status = "status"
    get_messages = "get_messages"
    evict = "evict"
    evict_action_results = "evict_action_results"
    restore = "restore"
    compact = "compact"

class ContextRequest(BaseModel):
    action: ContextAction
    start: int | None = None
    end: int | None = None
    keep_last: int | None = None
    summary: str | None = None
    mode: str | None = None
    scope: str | None = None
    auto: bool = False

class ContextTool(LionTool):
    def bind(self, branch: Branch) -> Tool: ...
    def to_tool(self) -> Tool: ...  # raises NotImplementedError
```

`bind()` captures `branch.msgs` and returns one tool whose callable mutates or reports
the branch's active progression.

**Context exact semantics**

- The active progression is copied lazily into `branch.metadata["current_progression"]`
  on the first mutating action; the full message pile and progression remain intact.
- `status` reports active/total/evicted counts, role counts, and an estimated token sum.
- `get_messages` defaults to active scope. Full scope marks every preview active or
  evicted. Previews retain at most 120 characters; the cap is a presentation budget with
  no recorded rationale for the exact value.
- `evict` clamps its start to at least index 1, preserving the system message, and removes
  `[start:end)` from the active progression only.
- `evict_action_results` keeps the most recent five action responses by default. Five is
  a context-retention heuristic stated in the request schema; no measurement is recorded.
- `restore` indexes the full record and reinserts messages in chronological order.
- `get_messages` and `restore` clamp ranges and return successful empty results when the
  clamped range contains nothing. `evict` and `compact` instead return `success=False`
  when their clamped start is not before the end. `evict_action_results` treats zero or
  negative `keep_last` as keeping none.
- `compact` defaults to `tool_io`, collapsing only action request/response messages;
  `mode="all"` collapses the whole selected span except index 0. It inserts one assistant
  summary message and excludes originals only from the active view.
- `mode` and `scope` are unconstrained strings: only the exact `"tool_io"` value selects
  tool-only compaction, so any other mode string follows the all-message branch; only
  exact `scope="all"` selects the full record, and every other scope uses the active
  view.
- Automatic compaction concatenates at most 6,000 source characters before one direct
  model call and returns a normal failure dictionary if the call fails or yields no text.
  The cap bounds the auxiliary prompt; its exact value has no recorded rationale.

The messenger factory contract is:

```python
# lionagi/tools/communication/messenger.py
class MessengerRequest(BaseModel):
    action: MessengerAction                 # send | done | finished | wakeup
    to: str | list[str] | None = None
    content: str | None = None

class LionMessenger(LionTool):
    def __init__(self, exchange: Exchange): ...
    def on(self, event: str, callback): ...
    def bind(
        self,
        branch: Branch,
        roster: dict[str, UUID],
        sender_name: str | None = None,
        channel: str = "team",
    ) -> Tool: ...
    def to_tool(self) -> Tool: ...           # raises NotImplementedError
```

**Messenger exact semantics**

- The bound callable captures the branch id as sender, the roster, channel, exchange,
  and callback dispatcher. Default sender display is the first eight characters of the
  branch id; eight is an inherited display convention with no recorded collision policy.
- `send` requires recipients and content, sends once per known roster name, includes each
  returned message in the branch pile if absent, and returns semicolon-joined text.
  Unknown recipients are per-target text results rather than exceptions.
- `done` and `finished` fire optional synchronous callbacks and return status text.
- `wakeup` uses only the first item when `to` is a list, sends content prefixed with
  `[WAKEUP]`, tracks the message, and fires the callback.
- Unknown actions and missing fields are returned as strings beginning with an error or
  unknown-action message; there is no typed messenger response model.
- Exceptions from `Exchange.send()` or a registered synchronous callback are not
  converted to those strings. They leave the messenger callable and, on the normal
  branch path, become a failed `FunctionCalling` event under ADR-0012 D1.
- `ContextTool.to_tool()` and `LionMessenger.to_tool()` always raise with instructions to
  call `bind(...)`.

**Why this way**

Explicit binding makes captured context visible at construction and keeps ordinary
`Tool` execution unaware of branch internals. It also allowed context and communication
features to ship without changing the `LionTool` base. The cost is that instances claim
an abstract method they intentionally cannot fulfill, and generic registration fails at
runtime rather than expressing required context in the type.

### D4 — `CodingToolkit.bind()` returns a branch-scoped tool set

The toolkit's selection and construction contract is:

```python
# lionagi/tools/coding.py
ALL_CODING_TOOLS = (
    "reader", "editor", "bash", "search", "code_check",
    "code_nav", "ast_search", "context", "sandbox", "subagent",
)

DEFAULT_CODING_TOOLS = (
    "reader", "editor", "bash", "search", "code_check",
    "code_nav", "ast_search", "context",
)

class CodingToolkit(LionTool):
    def __init__(
        self,
        notify: bool = True,
        notify_threshold: float = 0.7,
        notify_max_tokens: int = 200_000,
        workspace_root: str | Path | None = None,
        tools: Sequence[str] | None = None,
        nudge_engine: NudgeEngine | None = None,
        nudge_rules: Sequence[NudgeRule] | None = None,
    ): ...

    def security_pre(self, tool_name: str, handler: Callable) -> CodingToolkit: ...
    def pre(self, tool_name: str, handler: Callable) -> CodingToolkit: ...
    def post(self, tool_name: str, handler: Callable) -> CodingToolkit: ...
    def on_error(self, tool_name: str, handler: Callable) -> CodingToolkit: ...
    def bind(self, branch: Branch) -> list[Tool]: ...
    def to_tool(self) -> Tool: ...            # raises NotImplementedError
```

Sandbox and subagent add their own request models:

```python
class SandboxRequest(BaseModel):
    action: SandboxAction                    # create | diff | commit | merge | discard
    message: str | None = None

class SubagentRequest(BaseModel):
    instruction: str
    permissions: str = "read_only"
    max_turns: int = 20
    cwd: str | None = None
```

`bind()` constructs local functions, pairs each with its canonical request model, attaches
tool-specific preprocessors/postprocessors, filters by `enabled_tools`, and returns a
list in `ALL_CODING_TOOLS` order.

Agent construction treats this provider as a special case rather than sending it
through generic `LionTool.to_tool()` registration: `_register_coding_tools()` constructs
the toolkit, installs configured hooks, calls `toolkit.bind(branch)`, and registers the
returned list (`lionagi/agent/factory.py`).

**Exact branch-scoped semantics**

- **Selection:** omitted `tools` selects the eight defaults. Unknown names raise
  `ValueError` during toolkit construction. Sandbox and subagent are opt-in.
- **Workspace:** the root is resolved once. The bound functions close over that root and
  the supplied branch.
- **File-state population:** a successful filesystem text read records resolved path and
  modification time in `file_state` and marks the path in `read_tracked`. Image reads and
  reads served from the converted-document cache return before that metadata is added.
  A successful write or edit also refreshes `file_state`, but removes the path from
  `read_tracked`.
- **Mutation guard:** an existing-file write and an edit require a `file_state` entry and
  compare the current mtime with the stored value. A new-file write needs no entry. Once
  a toolkit write/edit succeeds, its refreshed entry authorizes a later mutation without
  another read. An external mtime change forces a re-read; failure to `stat()` during the
  guard returns no error and allows the mutation attempt to continue.
- **Context coupling:** after successful eviction or compaction, invalidation examines
  only paths still in `read_tracked` and drops those whose reader response is no longer
  active. Mutation-derived entries have been removed from `read_tracked`, so they remain
  in `file_state` even when the original read evidence leaves active context.
- **Tool hooks:** security/user preprocessors and postprocessors use the same composition
  helpers as agent factory registration. Registered `error` hooks are stored on the
  toolkit but are not attached to the returned `Tool` values in `bind()`.
- **Nudges:** when `notify=True`, `bind()` uses a caller-supplied `nudge_engine` unchanged
  or constructs `NudgeEngine(branch, rules=...)`, stores it as
  `_bound_nudge_engine`, and adds a wildcard post-hook that can add a `system` field to
  dictionary results. Evaluation failure logs a warning and preserves the original
  result.
- **Repeated binding:** the notify hook is appended to the toolkit's persistent
  `_post_hooks` map inside every `bind()` call. Reusing one `CodingToolkit` instance for
  another branch therefore attaches both the new hook and prior bind hooks to the new
  descriptors; those older closures retain their earlier engine and file-state maps.
- **Declared nudge numbers:** `notify_threshold=0.7` and
  `notify_max_tokens=200_000` are stored on the toolkit but not read by `bind()` or
  passed to the constructed nudge engine in this module. They are inherited inactive
  settings here; no runtime budget or rationale can be claimed from them.
- **Sandbox state:** one mutable session slot is captured per bind. A second create is
  rejected until merge or discard. A successful merge clears the slot. A discard that
  returns clears it regardless of the returned success flag; an exception from
  `sandbox_discard()` propagates before the clear and leaves the slot populated.
- **Subagent budget:** `max_turns` defaults to 20 and clamps to 1–50 before becoming
  `max_extensions`. Successful text is truncated to 5,000 characters in the parent
  result. The request text identifies 20/50 as reasoning bounds, and the response cap
  bounds parent-context growth; no measurement for the exact values is recorded.

**Parallel implementation semantics**

- The toolkit imports the standalone request models and selected blocking helpers, so
  provider schemas share field names and constraints.
- The bound reader additionally returns images as base64 data URLs; the standalone reader
  treats NUL-bearing binary content as unsupported. Toolkit document opening allows no
  remote hosts.
- Toolkit editor enforces a `file_state` prerequisite for an existing-file write and an
  edit; the first mutation normally requires a successful text read, while successful
  mutations refresh the state for later calls. Standalone editor only instructs the
  caller to read first.
- Toolkit search defaults to 50 results for grep and 100 for find when the field is
  omitted or null. Standalone search uses 50 for both. Toolkit responses use
  `total_matches`/`total_found` and `shown`; standalone uses `count`.
- Toolkit search shares the unconstrained `SearchRequest`: zero selects its per-action
  default, while a negative limit reaches slicing and can produce a negative `shown`
  count. Its grep handler treats only return code `2` as failure and does not inspect the
  shared helper's `timed_out` flag, whereas standalone grep rejects every code outside
  `{0, 1}` and handles timeout explicitly.
- Toolkit code check calls the shared Ruff helper but does not pass the standalone
  workspace cwd. Toolkit navigation and AST search call the same low-level helpers and
  assemble their own dictionaries.
- Toolkit bash implements the same operator rejection and output cap through shared
  subprocess code, then renames `returncode` to `return_code`. It computes the timeout as
  `timeout or 30000` before clamping, so an explicit zero means 30 seconds rather than the
  standalone adapter's 1 ms; negative values still clamp to 1 ms and values above five
  minutes clamp to 300,000 ms.
- Expected failures remain dictionaries, but not every dictionary has the corresponding
  standalone response model's complete fields or wording.

**Why this way**

Binding once provides a natural home for read state, active-context checks, optional
nudge state, and sandbox lifecycle. Reusing request models retains provider-schema
compatibility. Reimplementing the handlers made stateful behavior easy to add around
each operation, but it created two semantic sources of truth. The accepted delta is to
compose canonical standalone operations and layer only branch state and policy.

### D5 — Branch cloning copies registered descriptors without rebinding

The clone path is:

```python
# lionagi/session/branch.py
def clone(self, sender: ID.Ref = None) -> Branch:
    tools = (
        list(self._action_manager.registry.values())
        if self._action_manager.registry
        else None
    )
    branch_clone = Branch(
        system=<cloned system>,
        user=self.user,
        messages=[msg.clone() for msg in self.msgs.messages],
        tools=tools,
        chat_model=<copied or shared model>,
        parse_model=<copied or shared model>,
        metadata={"clone_from": self},
    )
    ...
    return branch_clone
```

**Exact semantics**

- The clone constructs a new `ActionManager` and registry dictionary.
- The values passed into that manager are the original `Tool` objects. Registration of
  a `Tool` stores it as-is, so source and clone registries point to identical descriptor
  objects and callable objects.
- Stateless cached descriptors are consequently shared too; mutation of processors or
  schema through either reference is visible to both branches.
- A callable returned by `ContextTool.bind(source)` retains the source `branch`, message
  manager, and metadata in its closure after clone registration.
- A messenger callable retains the source branch id and source message pile used by its
  tracking closure.
- Every `CodingToolkit.bind(source)` function retains the source branch, source
  progression, source message manager, local file-state dictionaries, nudge engine, and
  optional sandbox slot.
- The clone operation has no provider identity, scope enum, rebind callback, or
  cloneability check, so it cannot distinguish these descriptors from reusable stateless
  tools.
- Message objects themselves are cloned and recipients are rewritten; this does not
  affect the callable closures already copied.

**Why this way**

Copying registry values preserves the visible tool set and is sufficient for stateless
functions. It also avoids requiring every third-party descriptor to implement cloning.
Once providers captured branch state, descriptor reuse became semantically unsafe. The
current code has no metadata from which clone could infer a repair, so rebinding must be
an explicit provider contract or cloning must reject non-cloneable bindings.

## Consequences

- Maintained standalone tools are easy to construct, test, cache, and register through
  one generic path.
- Pydantic request models give standalone and toolkit variants a shared provider input
  vocabulary.
- Expected file and subprocess failures are model-visible typed values rather than
  exceptions, and known blocking operations are offloaded deliberately.
- `LionTool` is not substitutable across its current subclasses: three implementations
  deliberately raise from the only abstract method.
- Operational caps are dispersed across adapters; several exact values are inherited
  without recorded evidence, and one reader cache setting is exposed but inactive.
- CodingToolkit's parallel handlers already diverge in search defaults, response keys,
  subprocess exit/timeout classification, explicit-zero timeout behavior, image
  behavior, and read-before-edit enforcement.
- CodingToolkit's context invalidation applies only while state remains tagged as
  read-derived; a successful mutation converts it to mutation-derived state that can
  outlive the active reader response.
- Rebinding the same notifying `CodingToolkit` instance accumulates branch-scoped nudge
  closures, so provider instances are single-bind in practice even though the interface
  does not enforce that lifecycle.
- A cloned registry has independent name membership but shared descriptor identity. For
  bound closures, calls through the clone can mutate, message from, or inspect the source
  branch.
- Reversing D1 requires a provider result that can represent one or many tools plus
  explicit construction context. Reversing D5 requires retaining provider provenance at
  registration time; a bare closure cannot be reliably rebound after the fact.
- Contributors must not assume a `LionTool` instance is generically registerable or a
  registered `Tool` is safe to copy across branch scope.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace the partial `LionTool.to_tool()` contract with a provider build contract that returns one or more tools from an explicit context; acceptance requires stateless and branch-bound built-ins to use the same construction path without raising from the advertised interface and to declare whether provider instances are reusable, single-bind, or cloneable. | M | (filled at issue-open time) |
| 2 | Make branch cloning rebuild branch-bound providers for the clone or reject non-cloneable bindings; acceptance requires context, coding, and messenger callables registered on a clone to reference only the clone's state. | S | (filled at issue-open time) |
| 3 | Refactor `CodingToolkit` to compose canonical standalone operations and add only branch-scoped state and policy; acceptance requires response-schema and failure-semantic parity tests for every operation exposed in both modes, plus a tested file-state lifecycle that either requires active read evidence for each mutation or explicitly defines when a prior mutation substitutes for a read. | M | (filled at issue-open time) |
| 4 | Move resource and prompt graph types out of the tool-provider base module; acceptance requires the tool base module to contain only registration and construction abstractions with unchanged public compatibility aliases. | S | (filled at issue-open time) |

## Alternatives considered

### A. Keep `LionTool.to_tool()` as the universal provider interface

This is sufficient for cached stateless adapters and keeps branch registration tiny. It
lost as a universal abstraction because context and messenger need inputs and coding
returns multiple descriptors. Their `NotImplementedError` implementations are direct
evidence that the method cannot express the shipped provider set.

### B. Remove `LionTool` and use ordinary factories

Plain functions returning `Tool | list[Tool]` would remove the broken inheritance and
make branch-bound inputs explicit without a new framework. It remains viable and is the
seed recorded in the original draft. It loses if third-party providers need a stable,
typed construction and clone contract discoverable by generic branch code; that product
requirement decides factory functions versus a provider protocol.

### C. Introduce a context-aware provider protocol

A protocol such as `build(context) -> list[Tool]` could support stateless and bound
providers uniformly and carry clone scope. It was not the organic implementation because
the original built-ins each returned one tool and no branch context was needed. Delta 1
retains this design direction without selecting its final public type prematurely.

### D. Put branch context directly on `Tool`

Adding a `branch` field or generic context object to every descriptor would let one
`to_tool()` path construct bound behavior. It lost because most tools are reusable and
the callable already captures only what it needs; putting session state on the core
descriptor would couple ADR-0011's portable declaration to Branch.

### E. Reuse standalone tools unchanged inside CodingToolkit

This would guarantee response and default parity and remove duplicated subprocess/file
logic. It did not satisfy the initial stateful needs by itself: read-before-edit requires
observing successful reads, context eviction must invalidate that state, and nudges need
post-call information. The retained design is composition plus explicit state/policy
wrappers, not simple list concatenation.

### F. Keep the parallel coding implementations

This bought direct access to branch-local state in each closure and allowed operation-
specific response additions. It lost as the long-term shape because standalone and bound
semantics already differ in defaults, fields, and supported content. Every bug fix must
be reasoned about twice.

### G. Deep-copy tool descriptors during branch clone

Deep-copying would separate mutable schema and processor fields. It lost as a fix for
branch binding because copying a Python closure does not rewrite its captured branch,
message manager, exchange sender id, or nudge engine. The result would look independent
while retaining the same scope bug.

### H. Drop all tools when cloning

This would avoid stale closures and force callers to register a safe set explicitly. It
lost because branch splitting expects visible capabilities to carry forward and existing
tests assert cloned stateless tools remain registered. A scope-aware provider contract
can preserve safe tools and rebuild or reject unsafe ones.

### I. Mark bound tools non-cloneable and reject cloning

This is the smallest safe correction: registration records scope and clone fails loudly
when a provider offers no rebinding function. It gives up transparent branch splitting
for context/coding/messenger users. Delta 2 keeps it as the fallback when a provider
cannot build against the clone.

### J. Keep resource and prompt graph types in `tools.base`

Co-location reduces module count and preserves old imports. It lost on cohesion: those
types do not implement registration or callable construction, so the module name no
longer identifies one responsibility. Delta 4 preserves compatibility aliases while
moving their implementation boundary.

## Notes

Removing `LionTool` in favor of ordinary factories remains a viable alternative to a
provider protocol. The deciding constraint is whether third-party providers need a
stable, typed construction interface for branch context, multiple-tool output, and clone
scope.
