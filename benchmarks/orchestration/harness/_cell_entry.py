"""Subprocess entrypoint for one ADR-0089 sandbox-backend prompt-cell trial.

``harness.runner.run_once(backend=...)`` provisions a workspace and then must
genuinely execute the trial through ``SandboxBackend.run_cell()`` rather than
calling ``_run_once_inprocess`` directly next to an unused handle (ADR-0089
§1: the seam has to actually run the thing, not sit beside it). Since
``run_cell()``'s only execution primitive is a subprocess entrypoint, this
script IS that entrypoint: it unpickles the ``(task, config, trial)`` spec
``run_once`` seeded into the workspace, runs the exact same in-process trial
body the no-backend path uses, and pickles the resulting ``RunResult`` back
out for ``run_once`` to collect.

Only reached when a backend is selected; importing this module has no effect
on the default (``backend=None``) in-process path.
"""

from __future__ import annotations

import asyncio
import pickle
import sys
from pathlib import Path

# Run as a plain script (no package context), so bootstrap sys.path the same
# way run.py / test_runner_backend.py do: the "orchestration" dir on the path
# makes ``harness`` importable as a top-level package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.runner import _run_once_inprocess  # noqa: E402


async def _main(in_path: str, out_path: str) -> None:
    # Unpickling our own (task, config, trial) spec, written moments ago by
    # run_once() in the same trial's provisioned workspace — not untrusted
    # input (ADR-0089 §3: a prompt-cell runs no untrusted code).
    task, config, trial = pickle.loads(Path(in_path).read_bytes())  # noqa: S301
    result = await _run_once_inprocess(task, config, trial)
    Path(out_path).write_bytes(pickle.dumps(result))


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1], sys.argv[2]))
