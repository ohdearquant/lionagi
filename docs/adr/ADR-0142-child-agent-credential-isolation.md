# ADR-0142: Child-agent credential isolation from the OS keychain and credential helpers

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started. File containment for Claude Code children (the
  `child_sandbox` settings key) is shipped separately and is the starting point this record
  builds on; nothing in D1-D6 is implemented.
- **Area**: service-providers
- **Date**: 2026-11-13
- **Relations**: extends ADR-0030 (the agentic provider-adapter boundary gains a credential
  isolation obligation); complements ADR-0126 (provider credential custody in the launching
  process, which this record leaves unchanged)

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
| Where tools run | D2: every command a child starts runs inside a deny-by-default OS profile lionagi owns, no weaker than file containment on any axis |
| How a CLI's tools reach that boundary | D3: through an integration point that routes every command; every path that does not start a command is named and confined by other means, or the provider is unavailable |
| Claude Code integration | D4: the shell-command prefix, Claude Code's own sandbox off, in-process tools confined by permission rules, unrouted command paths turned off |
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
case.

## D2. Tools run inside a deny-by-default profile lionagi owns

Every command a child starts runs under an OS profile that lionagi writes and applies. The profile
denies by default and allows what a build, test or edit needs, in the manner of the provider's own
profile, with these differences and requirements:

- **Credential services.** The Mach allow-list excludes the credential-store services
  (`com.apple.SecurityServer`, `com.apple.securityd`, `com.apple.securityd.xpc`,
  `com.apple.security.agent`) and the services that hand credentials or run work outside the
  sandbox: token and Kerberos services, the pasteboard, LaunchServices, AppleEvents and launchd job
  submission.
- **Credential agents.** No connection to a local socket outside the working directory and the
  configured caches, which covers `ssh-agent`, git credential caches and `gpg-agent`; their
  environment variables (`SSH_AUTH_SOCK` and the like) are not passed.
- **Other processes.** No process inspection of, or signal to, any process outside the sandbox.
- **Files.** Reads of the configured credential locations are denied (the list the `child_sandbox`
  key resolves today: built-in defaults plus global additions, minus a profile's named exemptions).
  Writes are limited to the working directory and the configured caches; the profile and the
  launcher that applies it are never writable from inside.
- **Network.** No wider than under file containment.

The rule for every axis: the profile is no weaker than file containment's, so requesting credential
isolation never loosens anything that file containment holds.

The boundary is applied before the command runs and is inherited by everything the command starts.
A nested provider CLI started from a tool therefore cannot read the store; a nested CLI whose login is
a file or an environment variable is covered only by the file rules and by D1's rule that no login
enters the tool environment.

The file rules move into this profile because P4 rules out keeping the provider's sandbox underneath
it.

## D3. A provider qualifies only if every path is accounted for

A provider integration qualifies for credential isolation only if each of the following holds:

- every command its tools, hooks or extensions start is routed through the D2 boundary, or that
  path is turned off;
- every built-in tool that runs inside the provider process (file read, write and edit, search,
  web fetch) is confined by the provider's permission rules to the same read-deny list and write set
  as D2, or is turned off;
- every command the provider starts on its own behalf outside the router (status lines, credential
  helpers, shell set-up) is either turned off or reads nothing the child could have written.

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

- The Read tool keeps the `permissions.deny` `Read(...)` rules; Write, Edit and NotebookEdit get
  rules limiting them to D2's write set, so an in-process tool cannot plant a file (a shell start-up
  file, a launch agent, git configuration, the profile itself) that something unconfined later runs.
  Search tools are covered by the same read rules or turned off.
- Paths that do not go through the prefix are turned off: MCP servers except those lionagi starts
  through the same launcher (`--strict-mcp-config`), and settings-defined helper commands (status
  line, API-key and cloud-credential helpers), by loading no settings source the working directory
  can affect.
- Every routed Bash command sources a shell snapshot that Claude Code made itself, and the command
  that made it never reached the prefix in the measured run. That snapshot reflects the user's own
  shell start-up files, which is a further reason the write rules above deny those files to the
  child.

Measured on macOS with Claude Code 2.1.323 in one single-turn run, with a test keychain item and a
test file. The probe profile was `(allow default)` with only the four credential-store Mach services
and one test directory denied; it carried no write, network or process rules, and the child had no
permission rules. It shows that prefix routing applies a profile to every Bash and hook command and
that the store and file denials hold there; it does not show the D2 profile.

| Probe | Result | Control |
|---|---|---|
| child Bash: `security find-generic-password -s <test item> -w` | the search fails (`One or more parameters passed to a function were not valid`, then `could not be found`, exit 44); no secret in the output | the same command outside the child returned the item after the run |
| child Bash: `cat <test file under a denied directory>` | `Operation not permitted` | the file was readable outside the profile |
| child Bash: `echo hello && git --version` | runs | - |
| provider login | the child completed its turn; no provider credential variable was in its environment | under an outer keychain denial the same CLI was not logged in (P3) |
| command routing | 28 prefix invocations: 3 Bash commands and 25 hook commands, each as one argument | - |

A denied keychain query reports "item not found"; only the first line (`parameters ... not valid`)
distinguishes it from a missing item, so every probe of this boundary needs the outside control.

## D5. A requested capability that refuses rather than degrades

Credential isolation is its own capability, requested per launch or per profile and qualified per
provider, provider version and platform. A launch that requests it on an unqualified combination
refuses before any tool runs, naming the combination. It never falls back to file containment while
keeping the claim. File containment stays the default for Claude Code children and keeps its stated
limits.

| Platform | Position |
|---|---|
| macOS | prefix routing measured for one provider (D4); the D2 profile is not yet written or measured |
| Linux | unavailable until measured: the candidate is bubblewrap with no route to the session bus that serves the Secret Service, verified by endpoint, not by removing one environment variable |
| WSL2 | unavailable until the Linux checks and the Windows interoperability routes are measured |
| native Windows | unavailable: no backend |

## D6. Credentialed work never runs on what the child wrote

Tools that need the store (a signed commit, an authenticated push or registry pull, the GitHub CLI)
do not work inside the child. That work runs in the launching process, and it never runs inside the
child's working tree, with git configuration or hooks from that tree, or with a cache the child could
write: the launching process reads the child's result as data (a diff, an archive, a file list) and
applies it in a tree and environment of its own. Otherwise the child plants a git hook, a
`core.fsmonitor` or `credential.helper` setting, or a poisoned cache entry, and the unconfined process
runs it with the credentials D2 withheld.

## Acceptance (before Status moves to Accepted)

Each row runs with test items, test files and test agents only; no step lists or searches a user's
credential store. Every refusal row carries a control showing the same request succeeds outside the
boundary in the same run, so that a broken probe cannot read as a denial.

1. Provider login and renewal work across several turns, including a token refresh, while the tools
   are confined; the login source is recorded.
2. Direct store access is refused: `security`, and a small client calling the Security framework for
   the same item.
3. The P2 helper path is refused: a tool-written `GH_CONFIG_DIR` with `gh auth token`, and
   `git credential fill` through the GitHub CLI helper.
4. Delegation is refused: asking launchd, LaunchServices or AppleEvents to run a command outside the
   sandbox, each with a control showing the same delegation works outside.
5. Credential agents are refused: a test `ssh-agent` key, a git credential cache entry, the
   pasteboard.
6. The parent is out of reach: no credential in the tools' environment or inherited descriptors, and
   process inspection of and signals to the provider process are refused.
7. Every path is accounted for: Bash, background commands and hooks reach the prefix; each in-process
   tool is held to the read and write rules (including writes to shell start-up files, launch agents,
   git configuration and the profile); each unrouted path is shown to be off.
8. A project settings file in the working directory cannot turn the prefix off, re-enable a
   forbidden tool or path, or widen the profile.
9. The profile is no weaker than file containment: the file containment acceptance (file reads,
   writes, network) passes unchanged under it, including from a nested shell, a background
   descendant and a nested provider CLI.
10. D6 holds: a git hook, repository configuration and cache entry planted by the child do not run
    when the launching process applies the child's result.
11. A missing or failing backend refuses the launch; nothing runs outside the profile on a retry.

## Consequences

- The provider's built-in Bash sandbox is replaced, not stacked, for children launched in this mode.
  lionagi then owns a deny-by-default profile and its correctness on every supported OS version.
- `sandbox-exec` is a deprecated macOS interface that still ships; a replacement backend is a
  separate decision when it stops shipping.
- Hooks run inside the profile with the tools' write set (D4 routes them). A hook that needs the
  store, or writes state outside that set, stops working in this mode; widening the write set for a
  hook widens it for the model's commands too, so such hooks are turned off instead.
- Credentialed operations move to the launching process under D6, which costs a hand-off step for
  every commit, push or authenticated fetch.

## Alternatives considered

- **An OS sandbox around the whole CLI, with an environment-carried login.** Blocks the store for
  everything (P1). Rejected because P3 forces the login into the environment every tool descends
  from (see D1), and P4 forces the file rules out of the provider's sandbox anyway.
- **A separate OS user for the whole CLI.** Kernel user separation keeps the human user's keychain,
  helpers, agents, launchd domain and processes out of reach without a hand-written profile. Its
  costs: an administrator creates and maintains the user, the workspace is shared across users, the
  agent's own provider login lives in that user's store where its tools can reach it, and nothing
  stops one child reading another's work. Kept as a candidate backend for D2 rather than rejected; the
  acceptance list applies to it unchanged, and the first backend to pass it on a platform qualifies
  there.
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
- The read allow-list half of the end state (issue #4306) is a separate decision; this record covers
  the credential store, helpers, agents and the provider login only.
