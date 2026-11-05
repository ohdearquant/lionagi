# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Transport-neutral observer concerns: emission, capability extraction, and control — safe to call from any path."""

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
    """Observer-requested clean cancellation — unwinds the stream quietly (distinct from LoopBreak)."""

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason or "stream cancelled by observer")
        self.reason = reason


def _split_list_values(block: dict[str, Any], capabilities: Operable) -> list[dict[str, Any]]:
    """Expand a key holding a non-empty list of objects into one single-key block per element.

    A model asked for one object per key sometimes puts several under one key
    (``{"issue_found": [{...}, {...}]}``). Each element is validated on its own as the
    key's model, so its field rules apply per element. Only keys whose spec is a pydantic
    model are expanded; every other key stays together in the first block, and a block with
    no such key comes back unchanged.
    """
    from pydantic import BaseModel

    split_keys: list[str] = []
    for key, value in block.items():
        spec = capabilities.get(key, None)
        base = getattr(spec, "base_type", None)
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
            and isinstance(base, type)
            and issubclass(base, BaseModel)
        ):
            split_keys.append(key)
    if not split_keys:
        return [block]
    rest = {k: v for k, v in block.items() if k not in split_keys}
    out: list[dict[str, Any]] = [rest] if rest else []
    for key in split_keys:
        out.extend({key: item} for item in block[key])
    return out


def attempt_extract(text: str, capabilities: Operable) -> tuple[list[Any], list[Any], list[Any]]:
    """Parse fenced JSON capability blocks from an assistant message; returns (bundles, violations, rejects).
    Keys ⊆ grant → validated bundle; keys outside grant → violation; schema failure → reject."""
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
    expanded: list[Any] = []
    for block in blocks:
        if isinstance(block, dict) and block and set(block).issubset(allowed):
            expanded.extend(_split_list_values(block, capabilities))
        else:
            expanded.append(block)
    for block in expanded:
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
    """Emit a branch message onto the session bus; extracts capability bundles from AssistantResponse when a grant is set. No-op with no observer."""
    if getattr(branch, "_observer", None) is None:
        return
    from lionagi.protocols.messages import AssistantResponse
    from lionagi.session.signal import (
        MessageAdded,
        Signal,
        StructuredOutput,
    )

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
    """Honor a pending observer directive: BREAK → LoopBreak, CANCEL → StopStream, CONTINUE → no-op."""
    ctrl = branch.poll_control()
    if ctrl is None or ctrl.directive is LoopDirective.CONTINUE:
        return
    if ctrl.directive is LoopDirective.BREAK:
        raise LoopBreak(ctrl.reason)
    if ctrl.directive is LoopDirective.CANCEL:
        raise StopStream(ctrl.reason)
