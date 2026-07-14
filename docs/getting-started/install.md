# Install LionAGI

LionAGI requires Python 3.10 or newer. Install the CLI in an isolated tool
environment, or add LionAGI to a Python project.

## CLI installation

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install lionagi
li --version
```

With `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install lionagi
li --version
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`.

## Python project installation

Create a project and add LionAGI as a dependency:

```bash
mkdir lionagi-quickstart
cd lionagi-quickstart
uv init --bare
uv add lionagi
uv run python -c "import lionagi; print(lionagi.__version__)"
```

The printed version is the success check. The equivalent `pip` command inside
an activated environment is `python -m pip install lionagi`.

## Choose one provider mode

CLI-backed providers run the provider's own coding-agent executable. They use
that executable's login and do not need an API key in LionAGI.

### Codex CLI

```bash
npm install -g @openai/codex
codex login
codex --version
```

Use the LionAGI model alias `codex`.

### Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude login
claude --version
```

Use the LionAGI model alias `claude`.

API-backed providers are most useful from Python. Export the matching key
before running your program:

```bash
export OPENAI_API_KEY="..."
```

Other supported keys include `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`. An API
key does not sign the `codex` or `claude` CLI into its subscription-backed
session; those CLIs still use their own login commands.

## Run the preflight

```bash
li doctor
```

Healthy core checks use a check mark. A warning that the Studio daemon is not
running is expected until you start Studio; it does not prevent `li agent` or
`li o flow` from running. A failed import, dependency, or `~/.lionagi` write
check must be fixed before continuing.

For machine-readable diagnostics:

```bash
li doctor --json
```

## If installation fails

- If `li` is not found after `uv tool install`, ensure uv's tool bin directory
  is on `PATH` with `uv tool update-shell`, then open a new terminal.
- If a provider command is missing, rerun `codex --version` or
  `claude --version` directly. LionAGI cannot start a CLI-backed agent until
  that executable works on its own.
- If Python cannot import LionAGI, run it through the environment that owns the
  dependency: `uv run python ...` or activate `.venv` first.

Next, choose the [CLI quickstart](first-flow.md) or the
[Python quickstart](python.md).
