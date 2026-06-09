# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Transport-neutral observer concerns: emission, capability extraction, control.

These used to live inside ``operations/run/run.py`` — the CLI-streaming Middle —
which meant only CLI agents emitted signals onto the session bus or honored
observer control directives. API agents (operate/ReAct/communicate over an HTTP
chat model) reached none of it. That asymmetry is the bug: observer-powered
orchestration (capability-routed flow, reactive SpawnRequest, governance
control) must be transport-agnostic.

So the logic lives here and is applied at transport-neutral seams:
  - ``emit_message`` runs as a ``MessageManager.on_message_added`` hook → every
    message any path adds emits its signal uniformly (CLI stream, API turn, act).
  - ``check_control`` is polled at turn boundaries (operate / ReAct loop) AND
    between stream chunks (run), at whatever granularity each transport allows
    (API calls are atomic → turn-granular; CLI streams → chunk-granular).

Nothing here assumes a transport. ``emit_message`` / ``check_control`` are no-ops
when the branch has no observer / no pending control, so they are safe to call
unconditionally from every path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from lionagi.session.control import LoopBreak, LoopDirective

if TYPE_CHECKING:
    from lionagi.ln.types import Operable
    from lionagi.protocols.messages.message import RoledMessage
    from lionagi.session.branch import Branch

logger = logging.getLogger(__name__)


class StopStream(Exception):  # noqa: N818
    """Internal sentinel for observer-requested clean cancellation.

    Raised when an observer sets a CANCEL directive. Distinct from ``LoopBreak``
    (a hard stop surfaced as ``RunFailed``): ``StopStream`` unwinds the current
    stream/loop quietly so the operation returns what it has.
    """

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason or "stream cancelled by observer")
        self.reason = reason


def attempt_extract(text: str, capabilities: Operable) -> tuple[list[Any], list[Any], list[Any]]:
    """Parse capability emissions out of an assistant message.

    A capability is a named typed field (a ``Spec``); ``capabilities`` is the
    ``Operable`` of names the agent is allowed to produce. We pull every fenced
    ````json`` block out of ``text`` (fuzzy-tolerant; the injected prompt asks
    the model to fence its emissions) — a single message may carry several
    blocks; un-fenced JSON embedded in prose is *not* extracted — and per block,
    per Ocean's rule, require ``set(keys) ⊆ capabilities.allowed()``:

    - no capability keys → ordinary prose/JSON, skipped;
    - keys ⊆ grant → validated via ``create_model`` into a bundle (a dynamic
      model with one field per present capability);
    - any key outside the grant → an *illegal* emission (the agent reaching
      past its capabilities): not honored, recorded as a ``CapabilityViolation``;
    - keys ⊆ grant but schema validation fails → recorded as an
      ``EmissionRejected`` so repair loops can re-prompt instead of the work
      silently vanishing.

    Returns ``(bundles, violations, rejects)`` — all lists, since one response
    may carry several blocks.
    """
    if not text or not isinstance(text, str):
        return [], [], []
    from lionagi.ln.fuzzy._extract_json import extract_json
    from lionagi.session.capabilities import CapabilityViolation, EmissionRejected

    try:
        data = extract_json(text, fuzzy_parse=True, return_one_if_single=False)
    except Exception:
        return [], [], []
    blocks = data if isinstance(data, list) else [data]

    allowed = capabilities.allowed()
    bundles: list[Any] = []
    violations: list[Any] = []
    rejects: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict) or not block:
            continue
        keys = set(block.keys())
        if keys.isdisjoint(allowed):
            continue  # not a capability emission
        if not keys <= allowed:
            logger.warning(
                "Illegal capability emission: keys %s outside grant %s",
                keys - allowed,
                allowed,
            )
            violations.append(
                CapabilityViolation(
                    offending=sorted(keys - allowed),
                    allowed=sorted(allowed),
                    block=block,
                )
            )
            continue
        model = capabilities.create_model(include=keys)
        try:
            bundles.append(model.model_validate(block))
        except Exception as e:
            logger.debug("Capability block failed validation, skipped: %s", e)
            rejects.append(EmissionRejected(error=str(e), block=block))
            continue
    return bundles, violations, rejects


async def emit_message(branch: Branch, msg: RoledMessage) -> None:
    """Raise a branch message onto the session bus as a typed Signal.

    AssistantResponse → extract the capability bundle (when a grant is set) and
    emit it as one StructuredOutput; filters fan out by named field, so one
    response can satisfy several observers. ActionRequest/ActionResponse →
    tool-use / tool-result signals. No-op when the branch has no observer or the
    message is not an emittable type — so it is safe as a universal
    ``on_message_added`` hook.
    """
    if getattr(branch, "_observer", None) is None:
        return
    from lionagi.protocols.messages import AssistantResponse
    from lionagi.session.signal import (
        MessageAdded,
        Signal,
        StructuredOutput,
    )

    # Every message — system, instruction, assistant, action — lands on the bus
    # as a MessageAdded so the Flow is a complete record and observers can watch
    # the whole stream by payload type: ``observe(ActionRequest)`` /
    # ``observe(System)`` fire off the MessageAdded envelope's unwrapped ``data``
    # (signal.py: a TypeFilter matches any Signal whose ``data`` is that type).
    # The StructuredOutput below is an additional, finer event for the capability
    # subset — there is no separate per-message-type signal, which would
    # double-fire data-type observers that already match MessageAdded.
    await branch.emit(MessageAdded(data=msg))

    if isinstance(msg, AssistantResponse):
        capabilities = getattr(branch, "_capabilities", None)
        if capabilities is not None:
            bundles, violations, rejects = attempt_extract(msg.response, capabilities)
            role_name: str | None = getattr(capabilities, "name", None)
            if bundles:
                # Emit all bundles concurrently — a slow handler on one bundle
                # must not serialize the others.
                from lionagi.ln.concurrency import gather as _gather

                await _gather(
                    *(
                        branch.emit(StructuredOutput(data=b, emitter_role=role_name))
                        for b in bundles
                    )
                )
            # Over-grant attempts become observable governance events, not
            # silent drops — session.observe(CapabilityViolation) can react.
            for violation in violations:
                await branch.emit(Signal(data=violation))
            # In-grant blocks that failed schema validation become observable
            # repair events — session.observe(EmissionRejected) can re-prompt.
            for reject in rejects:
                reject.branch_name = getattr(branch, "name", "") or ""
                await branch.emit(Signal(data=reject))


def check_control(branch: Branch) -> None:
    """Honor a pending observer control directive at a loop/stream boundary.

    No-op on CONTINUE / no directive. BREAK → ``LoopBreak`` (hard stop surfaced
    as RunFailed). CANCEL → ``StopStream`` (quiet unwind). Call this at turn
    boundaries in operate/ReAct and between chunks in run.
    """
    ctrl = branch.poll_control()
    if ctrl is None or ctrl.directive is LoopDirective.CONTINUE:
        return
    if ctrl.directive is LoopDirective.BREAK:
        raise LoopBreak(ctrl.reason)
    if ctrl.directive is LoopDirective.CANCEL:
        raise StopStream(ctrl.reason)
