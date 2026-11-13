# ADR-0142: Child-agent credential isolation from the OS keychain and credential helpers

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started. File containment for Claude Code children (the
  `child_sandbox` settings key) is shipped separately and is the starting point this record
  builds on; nothing in D1-D5 is implemented.
- **Area**: service-providers
- **Date**: 2026-11-13
- **Relations**: extends ADR-0030 (the agentic provider-adapter boundary gains a credential
  isolation obligation); complements ADR-0126 (provider credential custody in the launching
  process, which this record leaves unchanged)

## Context

lionagi launches child agents on provider CLIs (Claude Code, Codex, Gemini) and runs an API-model
shell tool of its own. A child's tools run commands chosen by a model, often after reading text the
user did not write. Whatever the launching user can reach, those commands can reach.

The interim measure confines a Claude Code child's files: Claude Code's own sandbox, configured by
lionagi, refuses Bash reads of a list of credential locations, refuses the Read tool the same
locations, and limits writes to the working directory and toolchain caches. That measure covers
files only. This record decides the next layer: credentials the operating system holds for the user
in its credential store (the macOS keychain; the Secret Service on Linux), and the credential helpers
that read that store on a tool's behalf.

Problems, each observed on macOS with Claude Code 2.1.323:

- **P1. The keychain answers inside the file sandbox.** With the deny list on, a child's Bash runs
  `security find-generic-password -w` on a test item and gets the secret. The store is reached
  through a system service over IPC, not by reading a file, so a file rule cannot cover it.
- **P2. Hiding a helper's configuration does not stop the helper.** Denying the GitHub CLI's
  configuration directory is not enough: a child that writes its own configuration directory naming
  the account (account names are public) and points `GH_CONFIG_DIR` at it gets the token from
  `gh auth token`, and `git credential fill` returns it as git's password, because git's helper for
  the forge is the GitHub CLI.
- **P3. The provider's own login lives in the same store.** Claude Code started inside an outer OS
  sandbox that denies the keychain services does not start a turn ("Not logged in"). A boundary
  around the whole CLI breaks the provider before it protects anything.
- **P4. OS sandboxes do not nest.** Inside an outer `sandbox-exec` profile a second one fails to
  apply, so the CLI's own sandbox cannot run under an outer one, and a design that stacks them
  refuses to start once the CLI is told to fail when its sandbox is unavailable.
- **P5. The provider's built-in sandbox cannot be told to deny the store.** Claude Code's macOS
  sandbox profile allows the keychain service by construction; its settings can add Mach services
  to the allowed set, never remove one (read from the 2.1.323 binary; not a run).

| Concern | Decision |
|---------|----------|
| Where the provider authenticates | D1: in the provider CLI's own process, with its normal login, outside any tool boundary |
| Where tools run | D2: every tool command runs inside an OS boundary lionagi owns, which denies the credential-store services and carries the file rules |
| How a CLI's tools reach that boundary | D3: through a provider integration point that routes every command, or the provider is unavailable in this mode |
| Claude Code integration | D4: the shell-command prefix, with Claude Code's own sandbox off and the Read tool still governed by permission rules |
| Support and failure | D5: a separate, requested capability per provider and platform; it refuses when unqualified and never falls back to file containment silently |

## D1. The provider authenticates in its own process

The provider CLI keeps its supported login (a keychain item, a configuration file, or a token it
manages) and reads it in its own process, which is not inside the tool boundary. lionagi does not
copy that login into the tool environment, arguments, files, inherited descriptors, or results.

Why: P3 shows that moving authentication inside the boundary breaks the provider. The alternative,
an environment-carried login (a setup token or an API key), puts the credential in the process
environment the tools are launched from, where a tool can read it unless every launch path strips
it and no tool can inspect the parent process. That is a larger claim than this record can verify.

## D2. Tools run inside a boundary lionagi owns

Every command a child's tools start runs under an OS profile that lionagi writes and applies:

- denies the credential-store services (on macOS, Mach lookups of `com.apple.SecurityServer`,
  `com.apple.securityd`, `com.apple.securityd.xpc` and `com.apple.security.agent`);
- denies reads of the configured credential locations (the same list the `child_sandbox` key
  resolves today: built-in defaults plus global additions, minus a profile's named exemptions);
- limits writes to the working directory and the configured caches.

The boundary is applied before the command runs and is inherited by everything the command starts,
including a nested provider CLI, which therefore cannot read the store either and fails its own login
rather than borrowing the parent's.

The file rules move into this profile because P4 rules out keeping the provider's sandbox underneath
it.

## D3. A provider qualifies only through an integration point that routes every command

A provider integration qualifies for credential isolation only if lionagi can route every tool
command through the D2 boundary or turn that tool off. Built-in tools that run inside the provider
process (a file-read tool, for example) do not start commands and stay governed by the provider's
own permission rules for files; they cannot query the credential store. Paths that start commands
outside the provider's command runner (local tool servers, plugins, hooks) are either routed or
disabled for the qualification to hold.

A provider that offers no such point is unavailable in this mode. lionagi does not approximate it
with a wrapper around the whole CLI (P3) or with a settings file the working directory can affect.

## D4. Claude Code: the shell-command prefix

Claude Code runs each Bash command, and each user hook command, through the program named by
`CLAUDE_CODE_SHELL_PREFIX` when that variable is set, passing the whole shell command as one
argument. lionagi sets it to a launcher that applies the D2 profile and runs the command with the
shell:

```text
<prefix> "<shell command>"   →   sandbox-exec -f <lionagi profile> /bin/zsh -c "<shell command>"
```

and starts the child with Claude Code's own sandbox off (`{"sandbox": {"enabled": false}}` in the one
`--settings` value), because the two cannot nest (P4). The Read tool keeps the `permissions.deny`
`Read(...)` rules lionagi already renders.

Measured on macOS with Claude Code 2.1.323 in one single-turn run, with a test keychain item and a
test file:

| Probe | Result | Control |
|---|---|---|
| child Bash: `security find-generic-password -s <test item> -w` | the search fails (`One or more parameters passed to a function were not valid`, then `could not be found`, exit 44); no secret in the output | the same command outside the child returned the item right after the run |
| child Bash: `cat <test file under a denied directory>` | `Operation not permitted` | the file was readable outside the profile |
| child Bash: `echo hello && git --version` | runs | - |
| provider login | the child completed its turn | - |
| command routing | 28 prefix invocations: 3 Bash commands and 25 user hook commands, all as one argument | - |

The keychain denial surfaces as "item not found", not as a permission error, so a probe of this
boundary always needs the outside control to tell a denial from a missing item. What this run does
not show is listed under Acceptance.

## D5. A requested capability that refuses rather than degrades

Credential isolation is its own capability, requested per launch or per profile and qualified per
provider, provider version and platform. A launch that requests it on an unqualified combination
refuses before any tool runs, naming the combination. It never falls back to file containment while
keeping the claim. File containment stays the default for Claude Code children and keeps its stated
limits.

| Platform | Position |
|---|---|
| macOS | D2 profile measured for one provider (D4); qualification needs the Acceptance list |
| Linux | unavailable until measured: the candidate is bubblewrap with no route to the session bus that serves the Secret Service, verified by endpoint, not by removing one environment variable |
| WSL2 | unavailable until the Linux checks and the Windows interoperability routes are measured |
| native Windows | unavailable: no backend |

## Acceptance (before Status moves to Accepted)

Each row runs with test items and test files only; no step lists or searches a user's credential
store. Every refusal row carries a control that reads the same test item outside the boundary in the
same run.

1. Provider login and renewal work across several turns while the tools are confined.
2. Direct store access is refused: `security`, and a small client calling the Security framework for
   the same item.
3. The P2 helper path is refused: a tool-written `GH_CONFIG_DIR` with `gh auth token`, and
   `git credential fill` through the GitHub CLI helper.
4. No credential reaches the tools through the parent: environment, inherited descriptors, readable
   temporary files.
5. Every command path is routed: Bash, background commands, hooks, and each local tool server and
   plugin the profile allows; anything unrouted is turned off.
6. A project settings file in the working directory cannot turn the prefix off, re-enable a
   forbidden tool, or widen the profile.
7. The file rules hold as they do under file containment, including user-level `Read(...)` rules,
   from a nested shell, a background descendant and a nested provider CLI.
8. A missing or failing backend refuses the launch; nothing runs outside the profile on a retry.

## Consequences

- The provider's built-in Bash sandbox is replaced, not stacked, for children launched in this mode.
  lionagi then owns the profile text and its correctness on every supported OS version.
- `sandbox-exec` is a deprecated macOS interface that still ships; a replacement backend is a
  separate decision when it stops shipping.
- Tools that need the store (a signed commit, an authenticated registry pull, the GitHub CLI) stop
  working inside the child. Work that needs those credentials runs in the launching process and
  hands results to the child.
- User hooks run inside the profile too (D4 routes them), so a hook that needs the store stops
  working in this mode.

## Alternatives considered

- **An OS sandbox around the whole CLI, with an environment-carried login.** Blocks the store for
  everything (P1), but P3 forces the login into the environment the tools start from, and P4 forces
  the file rules out of the provider's sandbox. Rejected for D1's reason: the credential the boundary
  was meant to protect would sit next to the tools.
- **A separate OS user for the whole CLI.** Strong separation from the first user's home and store,
  but that user's provider login lives in that user's store, reachable by its tools in the same way.
  DEFERRED as a backend for D2: running the tools (not the provider) as a second user is a valid
  realization of the boundary on platforms without a usable sandbox.
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
- Whether the Security framework reaches the store through a service the four names above do not
  cover (acceptance row 2 answers it).
- The read allow-list half of the end state (issue #4306) is a separate decision; this record covers
  the credential store and helpers only.
