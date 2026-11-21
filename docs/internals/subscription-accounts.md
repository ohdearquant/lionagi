# Subscription accounts for CLI agents

`lionhub.agent.subscription_accounts` lets one machine run CLI legs on several subscription
logins at once. A Claude Code account is a login token. A Codex account is a Codex home
directory, with its own `auth.json` and `config.toml`.

## The requirement

The feature asks for:

1. A custom Codex home that an agent definition can set.
2. Multi-account management, where a Claude Code account is a token and a Codex account is a home.
3. Reading a token from the macOS keychain.
4. Reading a token from a `.env` file.

Every decision below that goes beyond those four points is marked **(design choice)**.

## Where accounts are listed

All accounts live in one directory, `LIONAGI_LOGINS_DIR`, which defaults to `<LIONAGI_HOME>/logins`.

- The setting is read from the process environment first, then from `<LIONAGI_HOME>/.env`.
- **(design choice)** A working directory's own `.env` is not read for this setting. A checkout under review therefore cannot point a leg at accounts it chose.

The directory holds two kinds of entry:

| Entry | Provider | Credential |
| --- | --- | --- |
| `claude-<name>` (a file) | Claude Code | One token, read from the file and stripped of surrounding whitespace. |
| `[claude_code.<name>]` in `accounts.toml` | Claude Code | Exactly one of `keychain = { service = "...", account = "..." }` or `dotenv = { file = "/abs/path/.env", var = "NAME" }`. |
| `[codex.<name>]` in `accounts.toml` | Codex | `home = "/abs/path"`. |

**(design choice)** One name may serve both providers: a `claude-work` file and a `[codex.work]` table can coexist. A name defined twice for the same provider (a file and a table) is refused.

A `claude-<name>` file holds a token, so the directory is as private as any login store. `accounts.toml` holds no secret, and no config, profile or run record ever holds a token:

- A keychain row names the item, not its value.
- A `.env` row names the file and the variable, not the value.
- A token is read only when a child is spawned.

A sandboxed Claude Code child is denied reading every sign-in the directory keeps or names: the directory itself, each `.env` file a row reads, and each Codex home as a whole. Each is denied as written and at the path it resolves to, every link on the way included, because the sandbox matches the path a read resolves to. A Codex home's `config.toml` can hold provider keys and MCP server `env` values besides the sign-in in its `auth.json`, and a Claude Code child has no use for any of it. An `accounts.toml` that cannot be read refuses the child's sandbox policy, because its sign-ins could not be named.

**Limit: a keychain row is readable by a sandboxed child.** A keychain item is not a file, so the deny list cannot cover it. This process reads the item with `/usr/bin/security` and no prompt, which means the item's access list admits that binary. Claude Code's sandbox lets a child run the same binary and reach the keychain service, so the child can read any keychain row's token the same way. Measured on macOS with Claude Code 2.1.328 and the sandbox settings this package generates: a throwaway item was readable from the child. In the same child, a read of a listed Codex home was refused by the operating system. Until the child's sandbox cuts its reach to the keychain service, a token that must stay out of a child's reach belongs in a token file or a `.env` row, not a keychain row. Cutting that reach may also stop Claude Code reading its own sign-in from the keychain, so it is left for its own change.

## Choosing an account

| Where | Key | Precedence |
| --- | --- | --- |
| `li agent --account <name>` | a name | Wins over the profile. |
| Agent profile frontmatter | `account: <name>` | Used when no flag is given. |
| Agent profile frontmatter | `codex_home: /abs/path` | Used when no flag is given. A Codex home named directly, with no entry in the directory. |

The profile keys also apply to a flow or fan-out orchestrator started with that profile, to the workers it clones from itself, and to each worker spawned with a profile that sets them. A bare worker has no profile, so it gets no account.

A profile that sets both `account` and `codex_home` is refused. So is a malformed name, or a `codex_home` that is relative or starts with `~`. **(design choice)** Each of these is refused when the profile is read, rather than read as unset: an unset account runs the leg on the machine's own login, which is the wrong account rather than a smaller setting.

Account names are lowercase letters, digits, `_` and `-`, and start with a letter or a digit.

## What the child gets

- **Claude Code.**
  - `CLAUDE_CODE_OAUTH_TOKEN` is set to the account's token.
  - The variables that would outrank it are removed from the child's environment:
    - an API key or bearer token: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`;
    - a credential handed another way: `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`, `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`, `CLAUDE_CODE_SESSION_ACCESS_TOKEN`;
    - a backend switch: `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_GATEWAY`, `CLAUDE_CODE_USE_MANTLE`, `CLAUDE_CODE_USE_ANTHROPIC_AWS`, `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD`.
  - The configured secret lookup is told to skip those names, so it cannot put one back. It fills only a name the child lacks, so it never replaces the token.
  - All other variables pass through as before, `ANTHROPIC_BASE_URL` included. An `apiKeyHelper` in a Claude Code settings file is not an environment variable and is not removed here.
- **Codex.**
  - `CODEX_HOME` is set to the home's real path, so a symlinked name and its target are the same home.
  - The home must be absolute as written and must be an existing directory.
  - The variables by which Codex signs in from the environment, `OPENAI_API_KEY`, `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN`, are removed, and the secret lookup skips them, so the home's own sign-in decides the account.

This process makes its own codex reads from the child's home, never from its own:

- the codex config profile a model name refers to;
- the default reasoning effort derived from `config.toml`;
- the generated profile that carries MCP servers' `env` and `http_headers`, which is written into the child's home where codex looks for `-p <name>`.

Containment acts on the chosen home. `contain_account_integrations` turns off the connectors of the account signed in there. Which parts of the home a leg uses depends on how it starts:

| Part of the home | `li agent` leg | Flow or fan-out worker |
| --- | --- | --- |
| Sign-in (`auth.json`) | Used. | Used. |
| `config.toml` | Loaded, unless the leg runs with an empty MCP server set (`--no-mcp-config`, or a role with `mcp: none`), which sets `ignore_user_config`. | Skipped: `ignore_user_config` is always set. Its sandbox settings and its hook and project trust records are passed on the command line instead. |
| Hooks (`hooks.json`) | Run as codex runs them; with `ignore_user_config` set, as on a worker. | Run where the skipped `config.toml` trusts them. A `config.toml` that declares hooks itself refuses the run. |
| Sessions | Written under the home. | Written under the home. |

So a home's `config.toml` (its model profiles, MCP servers and default effort) does not apply to a worker.

For a codex agent, `create_agent(agent_env={"CODEX_HOME": ...})` follows the same rule. For any other agent `CODEX_HOME` is an ordinary variable, passed through unchecked. `HOME` stays refused for a codex agent, because it moves every tool's configuration, not only codex's.

## Records

`run.json` and the saved branch record:

- `account` and `account_provider` for a named account, and `codex_home` too when the account is a Codex one;
- `codex_home` and `account_provider` for a home named directly.

No token is recorded.

A resume runs on the account the branch last ran on.

- **Codex. (design choice)** A codex session lives under the home it ran in, so a resume runs in that home. A resume may name another account only if that account's home is the same one; naming an account on another home is refused, and so is resuming on an account that has been pointed at another home since the branch ran on it. A branch that ran on no named account and no home is not moved onto one.
- **Claude Code.** A resume may name a different account, because the transcript is kept on this machine, not under the login, so a session can move off an account that has run out.
- **Either provider.** The saved branch keeps the account it started on and appends each account it moves to, so a later resume without `--account` stays on the latest. The run's `run.json` lists them in order as `account_history`, with `null` for this machine's own login. The run prints one warning line saying the account changed.
- A resume that names an account for a model that takes none, such as the Gemini CLI, is refused.

## Refusals

There is no fallback, to another account or to the machine's own login. **(design choice)** Each of the following refuses the launch, naming the entry, file, variable or keychain item:

- an account name with no entry for the agent's provider;
- an account on a provider that takes none (an API model, the Gemini CLI);
- an unreadable or malformed `accounts.toml`;
- an empty or all-whitespace token, a missing or dangling token file, a missing `.env` file, a `.env` file that does not set the variable;
- a Codex home that is gone.

The keychain is read with `/usr/bin/security find-generic-password -s <service> -a <account> -w` and a 10-second limit. Each of the following refuses, naming the item:

- the item is missing (exit 44);
- any other non-zero exit, such as a locked keychain;
- output that is not text;
- no answer within the limit, as when an access prompt waits;
- the platform is not macOS.

The tool's own output is never logged.

## Not in this cut

- **(design choice)** An account is never chosen automatically by remaining quota. Recording which account each leg ran on comes first.
- A leg started by the leg-host daemon runs on its role's account, but the launch request cannot name one. The daemon's own `LIONAGI_LOGINS_DIR` and `LIONAGI_HOME` settings are not passed to the leg, so the leg finds the accounts directory as an unconfigured process does: from `~/.lionagi/.env`, else `~/.lionagi/logins`.
