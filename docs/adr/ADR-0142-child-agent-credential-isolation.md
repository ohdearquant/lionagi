# ADR-0142: Child-agent credential isolation from the OS keychain and credential helpers

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started. File containment for Claude Code children (the
  `child_sandbox` settings key) is delivered separately, by #4311, and is the starting point this
  record builds on; nothing in D1-D6 is implemented.
- **Area**: service-providers
- **Date**: 2026-11-13
- **Origin**: #4306, the end state for child-agent file-read containment (its second candidate,
  credentials out of the child's reach). Maintainer-initiated hardening; no user request is behind it.
- **Relations**: extends ADR-0030 (the agentic provider-adapter boundary gains a credential
  isolation obligation); complements ADR-0126 (provider credential custody in the launching
  process). The two meet in the child's environment: D2 keeps the account keys and dotenv values
  ADR-0126 holds in the launching process out of every child, and with that stated ADR-0126 itself
  is unchanged

## Context

lionagi launches child agents on provider CLIs (Claude Code, Codex, Gemini) and runs an API-model
shell tool of its own. A child's tools run commands chosen by a model, often after reading text the
user did not write. Whatever the launching user can reach, those commands can reach.

File containment confines a Claude Code child's files: Claude Code's own sandbox, configured by
lionagi, refuses Bash reads of a list of credential locations, refuses the Read tool the same
locations, and limits Bash writes to the working directory and toolchain caches. It covers files
only. This record decides the next layer: credentials the operating system holds for the user in its
credential store (the macOS keychain; the Secret Service on Linux), the credential helpers and agents
that hand those credentials out (the GitHub CLI as git's helper, `ssh-agent`, credential caches), and
the provider's own login.

Problems, observed on macOS with Claude Code 2.1.323:

- **P1. The keychain answers inside the file sandbox.** With file containment on, a child's Bash runs
  `security find-generic-password -w` on a test item and gets the secret. The store is reached
  through a system service over IPC, not by reading a file, so a file rule cannot cover it.
- **P2. Hiding a helper's configuration does not stop the helper.** With the GitHub CLI's
  configuration directory denied, a child that writes its own configuration directory naming the
  account (account names are public) and points `GH_CONFIG_DIR` at it gets the token from
  `gh auth token`, and `git credential fill` returns it as git's password, because git's helper for
  the forge is the GitHub CLI.
- **P3. The provider's own login lives in the same store.** Claude Code started inside an outer OS
  sandbox that denies the keychain services does not start a turn ("Not logged in"). A boundary
  around the whole CLI breaks the provider before it protects anything.
- **P4. OS sandboxes do not nest.** Inside an outer `sandbox-exec` profile a second one fails to
  apply, so the CLI's own sandbox cannot run under an outer one.
- **P5. The provider's own sandbox allows the store.** The profile Claude Code generates for a Bash
  command (read from the child's own process arguments) is deny-by-default with a Mach allow-list,
  and that list includes `com.apple.SecurityServer`, `com.apple.securityd.xpc` and
  `com.apple.coreservices.launchservicesd`. No setting lionagi has found removes an entry.

| Concern | Decision |
|---------|----------|
| Where the provider authenticates | D1: in the provider CLI's own process, with its normal login, outside the tool boundary |
| Where tools run | D2: every command a child starts runs inside a deny-by-default OS profile lionagi owns, no weaker than file containment on any axis, from an environment lionagi builds by allow-list and never inherits |
| How a CLI's tools reach that boundary | D3: through an integration point that routes every command; every path that does not start a command is named and confined by other means, or the provider is unavailable |
| Claude Code integration | D4: the shell-command prefix, Claude Code's own sandbox off, in-process file and fetch tools off until qualified, no settings source but lionagi's, an empty shell start-up directory; not qualified until acceptance row 13 passes |
| Support and failure | D5: a separate, requested capability per provider and platform; it refuses when unqualified and never falls back silently |
| Credentialed work | D6: runs in the launching process, never inside a tree, configuration or cache the child could write |

## D1. The provider authenticates in its own process

The provider CLI keeps its supported login (a keychain item, a configuration file, or a token it
manages) and reads it in its own process, which is not inside the tool boundary. lionagi does not
copy that login into the tool environment, arguments, files, inherited descriptors, or results, and
D2 denies tools any inspection of, or signal to, processes outside their own sandbox, the provider
process included.

Why: P3 shows that moving authentication inside the boundary breaks the provider. An
environment-carried login (a setup token or an API key) is the other way to keep the provider working
under an outer sandbox; it puts the credential in the environment of the process the tools descend
from, so it is safe only if every route strips it before a command starts. D1 keeps the credential
out of the environment altogether and needs only the inspection denial, which D2 requires in any
case, together with D2's rule that the child's environment is built by allow-list, so that no other
credential the launching process holds arrives by inheritance.

## D2. Tools run inside a deny-by-default profile lionagi owns

Every command a child starts runs under an OS profile that lionagi writes and applies. The profile
denies by default and allows what a build, test or edit needs, in the manner of the provider's own
profile, with these differences and requirements:

- **Credential services.** The Mach allow-list excludes the credential-store services
  (`com.apple.SecurityServer`, `com.apple.securityd`, `com.apple.securityd.xpc`,
  `com.apple.security.agent`) and the services that hand credentials or run work outside the
  sandbox: token and Kerberos services, the pasteboard, LaunchServices, AppleEvents and launchd job
  submission.
- **Credential agents.** No connection to any local socket by path, wherever it lives (the working
  directory and caches included), except sockets lionagi names for the launch; this covers
  `ssh-agent`, git credential caches and `gpg-agent` whether or not their environment variables are
  set. Those variables (`SSH_AUTH_SOCK` and the like) are not passed either.
- **Environment.** The provider child's environment is built, never inherited. lionagi starts it
  from a list of variable names it states per provider (for Claude Code: the user and home
  identity, `PATH`, locale, terminal, temporary directory, and the proxy and certificate-bundle
  variables the provider needs to reach its service, a proxy address carrying a user name or
  password refusing the launch), copies each listed name from the launching process, and adds the
  values this record sets (the snapshot shell and the empty start-up directory of D4, the
  self-update switch). A profile may
  name further variables; naming one is the user's statement that its value is not a credential.
  Three things never enter, under any name, whatever a list says: a value lionagi resolved as an
  account credential under ADR-0126, a value lionagi loaded from a dotenv file, and a variable that
  would switch the provider to an environment-carried login (for Claude Code, `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`; D1 already rejects that login), which is
  removed even when a profile names it. Every tool, hook and the shell's environment snapshot
  descend from this environment, so they carry nothing else. An MCP server lionagi starts gets the
  same environment plus the variables its own configuration entry sets, which reach that server's
  process only; D2's process rule keeps tools out of it.
- **Other processes.** No process inspection of, or signal to, any process outside the command's own
  sandbox instance.
- **Files.** Reads of the configured credential locations are denied (the list the `child_sandbox`
  key of #4311 resolves: built-in defaults plus global additions, minus a profile's named exemptions).
  Writes are limited to the working directory and the configured caches, minus the files git, a
  shell or the provider later executes or obeys there, and the provider's own configuration and
  instruction files in the working directory (for Claude Code, `.claude/`, `.mcp.json`, `CLAUDE.md`
  and `CLAUDE.local.md`), so that a command cannot plant what the provider loads on a later turn.
  For git that is the repository's git directory as a whole, not a list of files in it: the `.git`
  entry of the working directory cannot be renamed, removed or replaced, whether it is a directory
  or a gitfile; no `.git` entry can be created anywhere else in the working directory or the
  caches; and inside the git directory, `config`, `config.worktree`, `hooks/`, `info/` and
  `modules/` are not writable. When `.git` is a directory, refs, objects and the index stay
  writable, so the child can commit. When `.git` is a gitfile (a linked worktree, the usual case for
  a child), the directory it names, which holds that worktree's HEAD and index, and the repository's
  shared refs and objects all stay outside the write set, so the child cannot commit. In both cases
  the change is taken from the working tree, and what the child wrote reaches the launching process
  only as D6 allows. The profile, the launchers that apply it, the empty start-up directory of
  D4, and the provider CLI's installed files are never writable from inside, wherever they live, the
  configured caches included. What the child writes into the configured caches is its output like
  anything else it writes: the launching process never runs it with the withheld credentials (D6),
  and a later build of the user's that uses those caches is the user's call (D6). The rules must hold on the
  resolved path, so that a symlink in the working directory does not widen them.
- **Network.** No wider than under file containment. `sandbox-exec` filters by local port, not by
  domain, and turning the provider's sandbox off also turns off the local proxy it filters through;
  outbound traffic is therefore allowed only to a filtering proxy lionagi runs with the domain
  policy of file containment (the `child_sandbox` settings of #4311). Until that proxy exists, outbound network from tools is denied.

The rule for every axis: the profile is no weaker than file containment's, so requesting credential
isolation never loosens anything that file containment holds.

The boundary is applied before the command runs and is inherited by everything the command starts.
A nested provider CLI started from a tool therefore cannot read the store, and with the provider's
login files on the read-deny list and no login in the tool environment (D1) it has no login at all:
it fails to start a turn, which is the intended outcome.

The file rules move into this profile because P4 rules out keeping the provider's sandbox underneath
it.

## D3. A provider qualifies only if every path is accounted for

A provider integration qualifies for credential isolation only if each of the following holds:

- every command its tools, hooks or extensions start is routed through the D2 boundary, or that
  path is turned off;
- every built-in tool that runs inside the provider process (file read, write and edit, search,
  web fetch) is turned off, unless a measured acceptance row shows the provider's own rules hold it
  to D2's read-deny list, write set and network policy under the launch's permission mode, symlinks
  included;
- every command the provider starts on its own behalf outside the router (status lines, credential
  helpers, environment set-up, and any git command it runs for its own context, which reads the
  working directory's git configuration) is turned off or runs with nothing from the user's or the working
  directory's configuration, and its output carries no credential into the tools;
- every instruction, memory or extension file the provider loads is accounted for, from the working
  directory, every directory above it, and the user's own configuration: the instruction files it
  reads into context and the files they import by path, in any path form (for Claude Code,
  `CLAUDE.md` and `CLAUDE.local.md` files, rules and memory files, and `@path` imports, which can be
  absolute, home-relative or reached through a symlink, inside any of these or inside a skill or
  command), and the skills, commands and agent definitions it loads together with what their
  headers declare (for Claude Code, under `.claude/` and the user's `~/.claude/`: inline commands a
  skill or command runs while it is expanded, tool lists, and hooks). Each is either not loaded, or
  loaded only through a read that D2's file rules hold; none of them can start a command outside the
  D2 boundary, and none can enable a tool the launch turned off. A request in a file's prose that
  the model run a command is not a bypass: the command it leads to is routed like any other;
- every MCP server the launch starts runs inside a D2 profile instance of its own: the provider starts
  a local (stdio) server by running the command in its configuration, so lionagi writes each such
  server's command as a server launcher, which applies the profile and runs the original command
  with its arguments unchanged (not through a shell), under a profile that also denies writes to the
  working directory. A server reached by URL (HTTP or SSE) has no command to wrap, and the provider
  process would connect to it unconfined, so it is not started in this mode; nor is any server the
  provider would add from a source lionagi does not pass.

A provider that cannot meet this is unavailable in this mode. lionagi does not approximate it with a
wrapper around the whole CLI (P3) or with a settings file the working directory can affect.

## D4. Claude Code: the shell-command prefix

Claude Code runs each Bash command, and each hook command, through the program named by
`CLAUDE_CODE_SHELL_PREFIX` when that variable is set, passing the whole shell command as one
argument. lionagi sets it to a launcher that applies the D2 profile and runs the command with the
shell:

```text
<prefix> "<shell command>"   →   sandbox-exec -f <lionagi profile> /bin/zsh -c "<shell command>"
```

and starts the child with Claude Code's own sandbox off (`{"sandbox": {"enabled": false}}` in the one
`--settings` value), because the two cannot nest (P4). Alongside the prefix:

- In-process file tools. Claude Code's permission rules deny listed paths; nothing lionagi has
  measured shows them confining a tool to an allow-list under a non-prompting permission mode, which
  is what D2's write set needs. Write, Edit and NotebookEdit are therefore turned off in this mode and edits go through the confined
  shell; Read, search and web fetch are turned off unless an acceptance row qualifies them (D3).
- Settings. The child loads no settings source but lionagi's own `--settings` value (no user,
  project or local settings), so no user- or project-defined status line, credential helper or hook
  runs outside the prefix; hooks the launch needs are passed in that value and routed. MCP servers
  are limited to the local servers lionagi passes (`--strict-mcp-config`), each started through the
  server launcher of D3; a URL server in what lionagi would pass refuses the launch.
- Instruction and extension files. Limiting settings sources does not stop Claude Code reading
  `CLAUDE.md` files, their imports, memory, skills, commands and agent definitions, which it does in
  its own process, outside the prefix. lionagi has not yet measured a launch configuration that
  turns these loads off or holds them to D2's file rules, so this is the open part of D3 for Claude
  Code: until acceptance row 13 passes for a named configuration, Claude Code does not qualify.
- Shell set-up. Every routed Bash command sources an environment snapshot that Claude Code made by
  running the user's shell start-up files once, outside the prefix (that command never reached the
  prefix in the measured run). zsh is the one supported shell for this mode. lionagi sets `SHELL` to
  `/bin/zsh` and `ZDOTDIR` to an empty directory of its own in the child's environment, so the
  snapshot reads no start-up file of the user's (zsh reads the user's `.zshenv`, `.zprofile`,
  `.zshrc` and `.zlogin` from `ZDOTDIR` when it is set; the system files under `/etc` are not
  writable by the user). No other shell's start-up files run: `BASH_ENV` and `ENV` are not passed,
  and a profile that names `SHELL`, `ZDOTDIR`, `BASH_ENV` or `ENV` refuses the launch. Which shell
  a provider version builds its snapshot with, and that it takes it from `SHELL`, is part of that
  version's qualification (row 14); a version that builds it with another shell, or ignores `SHELL`,
  is outside the qualified set and refuses the launch. Row 15 tests this.

Measured on macOS with Claude Code 2.1.323 in one single-turn run, with a test keychain item and a
test file. The probe profile was `(allow default)` with only the four credential-store Mach services
and one test directory denied; it carried no write, network or process rules, and the child had no
permission rules. The run loaded the user's own settings, whose hooks account for the hook commands
in the routing row. It shows that prefix routing applies a profile to every Bash and hook command and
that the store and file denials hold there; it does not show the D2 profile.

| Probe | Result | Control |
|---|---|---|
| child Bash: `security find-generic-password -s <test item> -w` | the search fails (`One or more parameters passed to a function were not valid`, then `could not be found`, exit 44); no secret in the output | the same command outside the child, run after the child exited, returned the item |
| child Bash: `cat <test file under a denied directory>` | `Operation not permitted` | the file was readable outside the profile |
| child Bash: `echo hello && git --version` | runs | - |
| provider login | the child completed its turn | the launching environment carried none of the provider's credential variables (checked by name, outside the child); under an outer keychain denial the same CLI was not logged in (P3) |
| command routing | 28 invocations reached the prefix: 3 Bash commands and 25 hook commands, each as one argument | the count shows what reached the prefix, not that nothing bypassed it |

A denied keychain query reports "item not found"; only the first line (`parameters ... not valid`)
distinguishes it from a missing item, so every probe of this boundary needs the outside control.

## D5. A requested capability that refuses rather than degrades

Credential isolation is its own capability, requested per launch or per profile and qualified per
provider, provider version and platform. A launch that requests it on an unqualified combination
refuses before the provider starts (so before any turn, hook, tool or MCP server), naming the
combination. It never falls back to file containment while
keeping the claim. File containment stays the default for Claude Code children and keeps its stated
limits.

| Platform | Position |
|---|---|
| macOS | prefix routing measured for one provider (D4); the D2 profile is not yet written or measured, and Claude Code is not qualified until acceptance row 13 passes |
| Linux | unavailable until measured: the candidate is bubblewrap with no route to the session bus that serves the Secret Service, verified by endpoint, not by removing one environment variable |
| WSL2 | unavailable until the Linux checks and the Windows interoperability routes are measured |
| native Windows | unavailable: no backend |

## D6. Credentialed work never runs on what the child wrote

Tools that need the store (a signed commit, an authenticated push or fetch, a registry pull, the
GitHub CLI) do not work inside the child. That work runs in the launching process, which treats
everything the child wrote as untrusted data:

- **Inputs first.** Authenticated fetches and dependency installs happen before the child starts,
  into locations the child can read and cannot write where the toolchain allows it. A dependency
  directory the toolchain keeps inside the working directory (`node_modules`, `.venv`) is writable
  by the child and is therefore treated like the rest of what it wrote: nothing in it runs in the
  launching process, and a later install or build from it is the user's call (below).
- **Extracting the result.** The launching process produces the child's change without running
  anything from the child's tree: no git command that reads that tree's configuration, attributes or
  hooks (it reads the tree with its own configuration only), and no file copy that follows a symlink.
  Symlinks, submodule changes and changes under the repository's git directory are refused, not
  applied. The change is the working tree compared with the commit the child started from; commits
  and refs the child made are not read, so a file reaches the change only as a working-tree file,
  under the same refusals.
- **Applying and publishing.** The change is applied in a tree of the launching process's own, and
  the commit and push there run with hooks disabled, with no hook manager, filter driver or build
  step from the change executed, and with the launching process's own configuration.
- **What remains the user's call.** A change that edits a build script, hook manager configuration,
  dependency manifest, or a provider's configuration or instruction files (for Claude Code,
  `.claude/`, `.mcp.json`, `CLAUDE.md`), which D2 does not let the child write and which reach the
  change only through a path D2 does not cover, is published as data; any later build or install of it runs under the same
  isolation as the child, never with the withheld credentials. The same holds for what the child
  left in the configured caches and in dependency directories inside the working directory.

Otherwise the child plants a git hook, a `core.fsmonitor`, filter or `credential.helper` setting, a
symlink to a key, or a build script, and the unconfined process runs or publishes it with the
credentials D2 withheld.

## Acceptance (before Status moves to Accepted)

Each row runs with test items, test files and test agents only; no step lists or searches a user's
credential store, and no row reads a real credential. Test items live where real ones do (the
default keychain, a test host entry for the GitHub CLI), never in a separate file the file rules
could block, so that a refusal measures the service denial and not a file denial. Every refusal row
carries a control showing the same request succeeds outside the boundary in the same run, so that a
broken probe cannot read as a denial.

1. Provider login and renewal work across several turns, including a token refresh, while the tools
   are confined; the login source is recorded.
2. Direct store access is refused: `security`, and a small client calling the Security framework for
   the same test item.
3. The P2 helper path is refused: a tool-written `GH_CONFIG_DIR` naming a test host whose token is a
   test item, with `gh auth token` and with `git credential fill` through the GitHub CLI helper.
4. Delegation is refused: asking launchd, LaunchServices or AppleEvents to run a command outside the
   sandbox, each with a control showing the same delegation works outside.
5. Credential agents are refused by socket path, not by a missing variable: a test `ssh-agent` and
   `gpg-agent` reached at their socket paths, a git credential cache socket under the configured
   caches, the pasteboard, and the token and Kerberos services.
6. Processes are out of reach. A test secret planted in the launching process under an arbitrary
   variable name, and another in a dotenv file lionagi loads, together with a provider login
   variable set in the launching process, reach no tool, no hook, no environment snapshot and no MCP
   server, and the provider still uses its own login; a control launch outside the mode shows the
   planted secret does reach a tool. No credential is in inherited descriptors, and process
   inspection of and signals to the provider process and to MCP servers lionagi started are refused.
7. Every path is accounted for: Bash, background commands and hooks reach the prefix, shown by a
   marker each path writes only when it runs inside the profile; a test local MCP server lionagi
   passes checks from its own process that the test store item, a denied test file and a write to
   the working directory are each refused, with the same checks succeeding from the same server
   started outside the profile, and the provider's MCP tool call to it succeeds; a `.mcp.json` in the
   working directory, a server in the user's provider configuration and a URL server are each
   planted and none is started; every in-process tool the launch leaves on passes its own
   qualification row, symlinks included; every other path is shown to be off.
8. No settings source but lionagi's own loads: a user settings file and a project settings file that
   each define a status line, a credential helper and a hook produce no command outside the prefix
   and cannot turn the prefix off or widen the profile. A repository configuration with a
   `core.fsmonitor` command and a hook, planted before launch, runs neither from any git command the
   provider starts on its own behalf.
9. The profile is no weaker than file containment: the file containment acceptance for file reads
   and writes, and its network refusal checks, pass unchanged under it, from a nested shell and a
   background descendant; writes to the repository's git configuration and hooks, and to the
   provider's configuration and instruction files in the working directory, are refused. The git
   directory holds as a whole: the child tries to rename and to remove `.git`, to replace it with a
   gitfile naming a directory inside the working directory, to create a `.git` in a subdirectory,
   and to write a `core.fsmonitor` setting into `config`, `config.worktree` and `info/`, each
   refused; a git command then run outside the profile in the working directory and in that
   subdirectory starts no planted command; and a commit the child makes succeeds when `.git` is a
   directory and is refused when it is a gitfile, with the working-tree change extracted in both
   cases (D6). While
   outbound network is denied, reaching the allowed domains is a stated loss, not a check; turning on
   the filtering proxy requires the whole file containment network acceptance to pass through it.
10. A nested provider CLI started from a tool fails to start a turn.
11. D6 holds against a child that plants each of: a git hook, repository configuration, an attributes
    filter, a tracked hook manager configuration, a symlink to a test key, a submodule change, a
    build script, and a commit holding a provider configuration file the working tree does not hold
    (for Claude Code, a `.claude/` settings file). None runs in the launching process, the symlink
    and submodule are refused, and the committed file is not in the change.
12. A missing or failing backend refuses the launch; nothing runs outside the profile on a retry.
13. Instruction and extension files are accounted for (D3). Planted in the working directory, in a
    directory above it, and in the user's provider configuration: an instruction file with `@path`
    imports of a denied test file in relative, absolute, home-relative and symlinked form; a skill
    and a command, each carrying the same imports, an inline command run at expansion, a hook in its
    header and a tool list naming a tool the launch turned off; and an agent definition with that
    tool list and hook. Each item is invoked. The run shows no read of a denied file, no write outside
    the D2 set, no fetch, no command outside the prefix, and no call of the tool the launch turned off;
    a control run outside the mode shows each planted item does load.
14. A provider version outside the qualified set refuses the launch with a message naming the version
    found and the qualified ones (see Consequences), tested with one version lower and one higher than
    every qualified version, so that a minimum check fails the row; the refusal comes before any turn,
    hook or MCP server starts. A write from inside a child to the provider CLI's installed files is
    refused.
15. The shell set-up carries nothing from the user's start-up files (D4). Planted in the user's
    home: zsh's `.zshenv`, `.zprofile` and `.zshrc`, bash's `.bashrc` and `.bash_profile`, and a
    file named by `BASH_ENV` in the launching process, each reading the denied test file and
    exporting a marker variable. With the launching process's `SHELL` set to zsh and then to bash,
    no tool and no hook sees a marker, the denied file is not read, and the snapshot is shown to be
    built by `/bin/zsh` with the empty start-up directory; a child write of `.zshenv` into that
    directory is refused; a profile naming `SHELL` or `BASH_ENV` refuses the launch; a control launch outside the mode shows the planted export does reach a
    tool.

## Consequences

- The provider's built-in Bash sandbox is replaced, not stacked, for children launched in this mode.
  lionagi then owns a deny-by-default profile and its correctness on every supported OS version.
- `sandbox-exec` is a deprecated macOS interface that still ships; a replacement backend is a
  separate decision when it stops shipping.
- Hooks run inside the profile with the tools' write set (D4 routes them). A hook that needs the
  store, or writes state outside that set, stops working in this mode; widening the write set for a
  hook widens it for the model's commands too, so such hooks are turned off instead.
- Credentialed operations move to the launching process under D6, which costs a hand-off step for
  every commit, push or authenticated fetch. A child cannot sign its own commits; its work arrives as
  an unsigned change the launching process commits.
- Qualification is per provider version, and provider CLIs update themselves. lionagi probes rather
  than pins: it does not install or hold back the provider CLI, it reads the version at every launch
  that requests this mode, and it refuses any version that is not in the qualified set, which is a
  list of exact versions, never a minimum. At each launch lionagi resolves the provider command to its
  real path (through any symlink), reads the version from that path and starts that same path, and
  sets the provider's own switch that turns off its self-update for the child (for Claude Code,
  `DISABLE_AUTOUPDATER=1`); D2 keeps the installed files out of the child's write set, so a child
  cannot change what a later launch probes. After an update the mode therefore refuses until the new
  version passes the acceptance list and is added to the set; file containment, which states its
  own minimum version, keeps working meanwhile. The cost is a requalification run per provider
  update, and an operator who wants the mode without interruption holds the CLI's own updates back.
- MCP servers in this mode lose capability too. A server has no outbound network, no writes to the
  working directory and no connection to a local socket unless lionagi names that socket for the
  launch, and only local servers start. A server that bridges to a local service or daemon, or one
  reached by URL, stops working until its socket or host is named and qualified.
- A tool, script or test that read a variable from the user's shell environment sees it only if
  the provider's list or the profile names it, so some builds need a profile entry.
- Claude Code in this mode edits through its shell only, and has no web fetch and no outbound
  network until lionagi's filtering proxy exists. That is a real loss of capability, and the reason
  this is a requested mode rather than the default.

## Alternatives considered

- **An OS sandbox around the whole CLI, with an environment-carried login.** Blocks the store for
  everything (P1). Rejected because P3 forces the login into the environment every tool descends
  from (see D1), and P4 forces the file rules out of the provider's sandbox anyway.
- **A separate OS user for the whole CLI.** Kernel user separation keeps the human user's keychain,
  helpers, agents and launchd domain out of reach without a hand-written profile. Rejected as a
  whole-CLI design because the provider and its tools would share that user: the tools reach the
  provider's login and process, against D1, and nothing stops one child reading another's work.
- **A separate OS user for the tool processes only.** The provider runs as the human user and the
  prefix runs each command as a second user. This keeps D1 and gives D2's process and launchd
  separation from the kernel. Its costs: an administrator creates the user and the delegation rule
  that lets the launcher switch to it, and the working directory is shared across users. Kept as a
  candidate D2 backend; the acceptance list applies to it unchanged.
- **Moving valuable items out of the store's reach, or restricting items to named applications.**
  P2 is the counterexample: the application allowed to read an item (the GitHub CLI) can be run by
  the tools. A separately locked store depends on machine state for the whole run. Kept as optional
  hardening, not as the mechanism.
- **Low-value reachable credentials.** Short-lived, narrowly scoped tokens reduce the damage of a
  retrieval but do not prevent it. Kept as defence in depth.
- **Asking the provider's sandbox to deny the store.** Not available (P5). If a provider adds that
  setting, D3 lets it qualify without the prefix.

## Open questions

- Which integration points Codex and Gemini expose for routing every command (D3).
- Whether the Security framework or a delegation service reaches the store through a name D2 does
  not list (acceptance rows 2 and 4 answer it).
- Whether any of Claude Code's in-process tools can be qualified (D3), which would restore editing,
  reading and fetching through them in this mode.
- The filtering proxy D2's network rule needs, and whether it is shared with other providers.
- The read allow-list half of the end state (issue #4306) is a separate decision; this record covers
  the credential store, helpers, agents and the provider login only.
