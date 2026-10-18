# Agent Workbench

An [OpenEnv](https://github.com/meta-pytorch/OpenEnv) environment for training tool-using agents. Each episode is
one short work task: a few files in a private workspace, a question to answer or a bug to fix, and a small fixed
tool set. There are 40 tasks, five in each of eight domains: finance, industrial, math, media, office, science,
security and software.

The image is built from this directory and published as `ghcr.io/ohdearquant/agent-workbench`
(`.github/workflows/agent-workbench-image.yml`).

## What it trains

Habits that carry over to unfamiliar agent tasks:

- **One exact JSON action per turn.** Every action is `{"tool": <name>, "args": {...}}`. A refused call
  (unknown tool, unexpected argument, a path outside the workspace, a blank answer) comes back with the reason
  and does not end the episode; three refusals in a row do.
- **Read before answering.** Inputs are files in the workspace (CSV tables, logs, notes, a subtitle file, a small
  Python module). The instruction says which ones matter; some files hold distractors.
- **Compute instead of guessing.** `run_python` runs a script in the workspace, so totals, rates, hashes and
  counts can be computed exactly.
- **Verify before finishing.** `check` says whether a candidate answer is right, and nothing more (at most
  twice per episode); code tasks have `run_tests`.
- **Exact answer formats.** Each instruction states the format: a number with N decimals, a name, a sorted
  comma-separated list, a timestamp as written in the file.

## Tools

| Tool | Arguments | Effect |
|---|---|---|
| `list_files` | none | list the workspace |
| `read_file` | `path`, optional `start`, `end` | up to 60 numbered lines |
| `run_python` | `code` | run a script in the workspace (10 s limit) |
| `write_file` | `path`, `content` | create or replace a file |
| `run_tests` | none | code tasks: run the visible tests as the task gave them |
| `check` | `answer` | correct or not correct, no reason (twice per episode) |
| `submit` | `answer` | final answer; ends the episode |

An episode allows 20 tool calls. `finish` is not offered to the agent: a trainer sends it to close an episode that
did not end by itself, for example after a reply that was not a valid action.

## Reward

Only the final observation carries a reward, in [0, 1]:

```text
reward = 0.7 x correctness
       + 0.1 if the agent read a file or ran code before submitting
       + 0.1 if it verified what it submitted: check confirmed that exact answer, or, for
         code tasks, the visible tests passed after its last change to the files and the
         test run left solution.py as it was
       + 0.1 if it made no refused calls
```

Listing the files does not count as reading them: reset already shows the listing.

An episode that ends without `submit` scores 0. Correctness is computed from the task's own data at grading time;
no answer is stored anywhere.

- Numbers: 1.0 when the value is within the stated rounding and the answer is the number alone; 0.5 when the value
  is right but wrapped in other text.
- Names, addresses and timestamps: 1.0 for the value alone (case-insensitive); 0 for anything else, since
  crediting text that merely contains the value would reward listing every candidate.
- Lists: 1.0 for the exact sorted list; 0.8 for the right items in another order; half the share found when
  some are missing and none is wrong; 0 for a list with any item that does not belong, since crediting overlap
  would reward listing every candidate.
- Code: every visible test must pass and every further case the original code passed must still pass; the
  credit is then the share of the further cases the original code fails that the fixed function passes, each
  in its own process. The further cases are not the visible tests, so code that special-cases the visible
  inputs earns nothing. Submitting the code unchanged, or with an edit that fixes nothing, earns 0.
- Headline (media): 1.0 when every rule in the brief holds; 0 when the headline is over the length limit;
  otherwise partial credit only once the required words are present.

Tasks are generated from their ids with a seeded generator, so every reset of a task is identical.

## Sandbox

In the image the server runs as root, and each episode runs its agent code under a uid of its own with the
system interpreter, in a workspace the episode creates and removes. Concurrent episodes cannot read each other's
workspaces, and a call's cleanup kills only its own episode's processes. When an episode ends, its processes
are killed, the files its uid left in `/tmp`, `/var/tmp` and `/dev/shm` are removed with its workspace, and its
uid goes to the back of the rotation. The server's code and its virtual environment are not readable from agent
code. Paths given to the file tools are relative, stay inside the
workspace and may not pass through a symlink; they read and write regular files only, and the listing neither
follows nor shows symlinks.

Outside the image (the server not running as root) there is no separate uid: agent code runs as the server's
user, and a process that starts its own session can outlive its call. Train against the image.

## Run it

```sh
cd apps/agent_workbench
uv sync --extra dev
uv run pytest
uv run uvicorn server.app:app --host 127.0.0.1 --port 8000
```

`tests/test_tasks.py` re-derives every expected answer from the task files with an independent solver, and
`tests/test_environment.py` plays whole episodes, including the refusal, budget and sandbox paths.

## License

The files generated from the OpenEnv environment template (`server/app.py`, `client.py`, `models.py`, the two
`__init__.py` files, `server/Dockerfile` and `pyproject.toml`) keep the template's BSD-3-Clause license. The rest
of this directory is Apache-2.0, like the repository.
