# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Drive a real ``python -m lionagi.mcp`` over stdio, the way a client does.

Every other module under ``tests/mcp/`` exercises the Python objects beneath
the transport, so a defect in the wire shape itself (a key read from the op
rather than from ``args``, a schema only the projector sees) is invisible to
all of them. This starts the server as a subprocess and speaks JSON-RPC to
it in handshake order: ``initialize``, ``notifications/initialized``, then
``tools/call`` -- the server refuses work before the handshake completes.

Every wait is bounded, and the child is terminated on every exit path
including an exception mid-conversation, so a server that stops answering
fails the waiting test instead of hanging the suite.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from typing import Any

__all__ = ("StdioMCPClient", "TransportError", "tool_payload")

# Generous enough for interpreter start plus the import of the CLI registry the
# catalog is projected from, short enough that a wedged server fails the run.
START_TIMEOUT = 90.0
CALL_TIMEOUT = 60.0
STOP_TIMEOUT = 10.0

PROTOCOL_VERSION = "2025-06-18"


class TransportError(RuntimeError):
    """The transport itself failed: no start, no answer, or a closed pipe."""


class StdioMCPClient:
    """A minimal JSON-RPC client over one server subprocess.

    Reads run on a background thread so that a silent server costs a timeout
    rather than a blocked interpreter.
    """

    def __init__(self, *, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self._id = 0
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: list[str] = []
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "lionagi.mcp"],
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for thread in self._readers:
            thread.start()

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _read_stdout(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except ValueError:
                # Not a JSON-RPC frame; keep it as diagnostics for a failure.
                self._stderr.append(f"[non-json stdout] {line}")

    def _read_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip())

    @property
    def stderr(self) -> list[str]:
        return list(self._stderr)

    def _diagnostics(self) -> str:
        tail = self._stderr[-20:]
        return f"exit={self._proc.poll()} stderr_tail={tail}"

    def _send(self, message: dict[str, Any]) -> None:
        if self._proc.poll() is not None:
            raise TransportError(f"server already exited; {self._diagnostics()}")
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise TransportError(f"server closed stdin: {exc}; {self._diagnostics()}") from exc

    def _await_id(self, want: int, timeout: float) -> dict[str, Any]:
        import time

        deadline = time.monotonic() + timeout
        pending: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError(
                        f"no reply to request id {want} within {timeout}s; {self._diagnostics()}"
                    )
                try:
                    message = self._inbox.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    if self._proc.poll() is not None:
                        raise TransportError(
                            f"server exited while awaiting id {want}; {self._diagnostics()}"
                        ) from None
                    continue
                if message.get("id") == want:
                    return message
                # A notification or an out-of-order reply: keep it for the
                # caller that is waiting on it.
                pending.append(message)
        finally:
            for message in pending:
                self._inbox.put(message)

    # ── protocol ─────────────────────────────────────────────────────────────

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self._await_id(rid, CALL_TIMEOUT if timeout is None else timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> dict[str, Any]:
        reply = self.call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "lionagi-tests", "version": "0"},
            },
            timeout=START_TIMEOUT,
        )
        if "error" in reply:
            raise TransportError(f"initialize failed: {reply['error']}; {self._diagnostics()}")
        self.notify("notifications/initialized")
        return reply["result"]

    def list_tools(self) -> list[dict[str, Any]]:
        reply = self.call("tools/list")
        if "error" in reply:
            raise TransportError(f"tools/list failed: {reply['error']}")
        return reply["result"]["tools"]

    def request(
        self,
        *,
        ops: list[dict[str, Any]] | None = None,
        help: Any = None,  # noqa: A002 - the surface's own parameter name
        timeout: float | None = None,
    ) -> Any:
        """One ``request`` tool call, unwrapped to the verb surface's own JSON."""
        arguments: dict[str, Any] = {}
        if ops is not None:
            arguments["ops"] = ops
        if help is not None:
            arguments["help"] = help
        reply = self.call(
            "tools/call", {"name": "request", "arguments": arguments}, timeout=timeout
        )
        if "error" in reply:
            raise TransportError(f"tools/call transport error: {reply['error']}")
        return tool_payload(reply["result"])

    def op(self, name: str, args: dict[str, Any] | None = None, **op_fields: Any) -> dict[str, Any]:
        """One op, returned as the single result entry the surface answers with."""
        payload = self.request(ops=[{"op": name, "args": args or {}, **op_fields}])
        entries = payload.get("ops") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or len(entries) != 1:
            raise TransportError(f"unexpected response envelope: {payload!r}")
        return entries[0]

    def close(self) -> None:
        """Terminate the child, whatever state it is in."""
        proc = self._proc
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=STOP_TIMEOUT)
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def __enter__(self) -> StdioMCPClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def tool_payload(result: dict[str, Any]) -> Any:
    """The verb surface's JSON out of an MCP ``tools/call`` result."""
    if "structuredContent" in result:
        return result["structuredContent"]
    for block in result.get("content") or []:
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except ValueError:
                return {"text": block["text"]}
    return result
