# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run-bound, serialized outcomes for independent notification subscriptions.

The recorder is inert until a caller binds it to a run and explicitly records
an observed attempt. It does not deliver notifications or acknowledge events.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from typing import Literal

from lionagi.cli._runs import RunDir

logger = logging.getLogger(__name__)


class DurableRunRecorder:
    """Serialize outcomes from the run and global subscriptions of one run.

    The composition root owns exactly one recorder for the run. Different
    recorders or processes are not coordinated by this instance's lock.
    """

    def __init__(self, run: RunDir) -> None:
        self._run = run
        self._lock = threading.Lock()
        self._subscriptions: dict[str, dict] = {}

    def _capture_stderr(self, subscription: str, text: str) -> str:
        self._run.state_root.mkdir(parents=True, exist_ok=True)
        name = "notify_stderr.global.log" if subscription == "global" else "notify_stderr.log"
        path = self._run.state_root / name
        fd, temporary = tempfile.mkstemp(dir=self._run.state_root, prefix=f".{name}.")
        try:
            stream = os.fdopen(fd, "w")
            fd = -1  # The stream now owns the descriptor.
            with stream:
                stream.write(text)
            # Replace the directory entry, never truncate an existing link target.
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        finally:
            if fd >= 0:
                os.close(fd)
        return str(path)

    def record(
        self,
        subscription: Literal["run", "global"],
        *,
        ok: bool,
        exit_code: int | None,
        stderr_text: str | None,
        reason: str | None,
    ) -> str | None:
        """Record an observed exec result or rejection without changing delivery."""
        if subscription not in ("run", "global"):
            logger.warning("Could not record notification outcome: invalid subscription")
            return None
        with self._lock:
            stderr_path = None
            if stderr_text:
                try:
                    stderr_path = self._capture_stderr(subscription, stderr_text)
                except Exception:  # noqa: BLE001 -- bookkeeping cannot change delivery
                    logger.warning("Could not capture notification stderr")
            entry = {"ok": ok, "exit_code": exit_code, "stderr_path": stderr_path}
            if reason is not None:
                entry["reason"] = reason
            self._subscriptions[subscription] = entry
            selected = next(
                (
                    self._subscriptions[key]
                    for key in ("run", "global")
                    if key in self._subscriptions and not self._subscriptions[key]["ok"]
                ),
                self._subscriptions.get("run", self._subscriptions.get("global")),
            )
            payload = {
                "ok": all(item["ok"] for item in self._subscriptions.values()),
                "exit_code": selected["exit_code"],
                "stderr_path": selected["stderr_path"],
                "subscriptions": dict(self._subscriptions),
            }
            if "reason" in selected:
                payload["reason"] = selected["reason"]
            try:
                self._run.write_notify_outcome(payload)
            except Exception:  # noqa: BLE001 -- bookkeeping cannot change delivery
                logger.warning("Could not record notification outcome")
            return stderr_path
