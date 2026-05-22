# li play

Shorthand for running a named playbook.

## Synopsis

```
li play <NAME> [playbook-args...]
li play list
li play <NAME> --help
```

## Description

`li play NAME` is sugar for `li o flow -p NAME`. The argv is rewritten before argparse runs, so all flags from [`li o flow`](orchestrate.md#flow) are available.

Playbooks live at `~/.lionagi/playbooks/<NAME>.playbook.yaml`.

## Forms

| Form | Behavior |
|------|----------|
| `li play NAME [args...]` | Runs the playbook. Rewrites to `li o flow -p NAME [args...]`. |
| `li play list` | Lists `*.playbook.yaml` names in `~/.lionagi/playbooks/`. |
| `li play NAME --help` | Prints the playbook's description, declared args, and usage line. Does not execute. |

`NAME` must not start with `-` and must not contain path separators.

## Dynamic flags

Playbooks may declare typed arguments in their `args:` block. These become CLI flags injected before argparse runs:

- `type: bool` → bare flag (`--flag` sets it `true`)
- `type: str / int / float` → flag with a value (`--flag VALUE`)
- If a flag name collides with a built-in `li o flow` flag it is silently skipped.

If the playbook has no `args:` block but has `argument-hint:`, the hint string is used in the usage line only; no flags are injected.

## Examples

```bash
# List installed playbooks
li play list

# Run a playbook with no declared args
li play codex-review

# Run a playbook with declared args
li play release-prep --version 2.1.0 --target main

# Inspect without running
li play release-prep --help

# Pass flow flags through (all li o flow flags work)
li play codex-review --dry-run --save ./out/
```

## Playbook YAML format

```yaml
name: release-prep
description: "Prepare a release: changelog, version bump, and tag."
args:
  version:
    type: str
    help: "Semver string, e.g. 2.1.0"
  target:
    type: str
    default: main
    help: "Branch to release from"
prompt: |
  Prepare release {version} from branch {target}.
  Update CHANGELOG, bump version files, create release tag.
```

The `prompt` field supports `{placeholder}` interpolation from declared args. A positional `prompt` argument (from the command line or a `-f` file) overrides the playbook's `prompt` field.

## Storage

Playbooks are resolved from `~/.lionagi/playbooks/<NAME>.playbook.yaml`. The name passed to `li play` must match the filename stem exactly (case-sensitive, no extension).
