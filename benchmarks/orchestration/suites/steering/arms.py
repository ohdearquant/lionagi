"""Arm switch — the control that isolates rendering (ADR-0088 measurement design)."""

from __future__ import annotations

import contextlib
import importlib
import time
from enum import Enum

from .fixture import STEER_TEXT

# operations/__init__.py's `from .flow import flow` shadows the `flow` submodule
# name with the `flow` function; importlib.import_module reaches the real module.
_flow_module = importlib.import_module("lionagi.operations.flow")


class Arm(str, Enum):
    NO_STEER = "arm0_no_steer"
    STEER_BURIED = "arm1_steer_buried"
    STEER_RENDERED = "arm2_steer_rendered"
    STEER_AS_OP = "arm3_steer_as_op"


BUILT_ARMS = (Arm.NO_STEER, Arm.STEER_BURIED, Arm.STEER_RENDERED)

ARM3_STUB_NOTE = (
    "Arm 3 (steer as op / Mode B) is not implemented in this harness. ADR-0088 "
    "slice 1's implementation fences forbid building op-mode injection ('MAY NOT "
    "build B (op-mode inject) in slice 1'); Mode B stays unbuilt until this harness's "
    "provider-by-arm table clears the pre-registered gate on Arm 2 vs Arm 1."
)


def make_steer_entry(text: str = STEER_TEXT) -> dict:
    """Synthetic operator message, matching the poller's entry shape (cli/orchestrate/flow.py)."""
    return {"ts": time.time(), "text": text}


@contextlib.contextmanager
def suppress_operator_render():
    """Arm 1 control: monkeypatch the module-level render hook to a no-op so the steer stays buried."""
    original = _flow_module._render_operator_messages

    def _noop(operation, context):
        return None

    _flow_module._render_operator_messages = _noop
    try:
        yield
    finally:
        _flow_module._render_operator_messages = original
