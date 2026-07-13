# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""KhiveInjectionProvider: the reference ContextProvider (ADR-0008) — recalls/composes
from khive over the existing MCP transport; see docs/internals/runtime.md."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi.protocols.messages.instruction import Instruction
    from lionagi.session.branch import Branch

__all__ = (
    "RecallPolicy",
    "ComposePolicy",
    "WritebackPolicy",
    "KhiveInjectionPolicy",
    "KhiveInjectionProvider",
)

logger = logging.getLogger(__name__)

# Same stdio MCP server shape the khive CLI itself exposes; callers with a
# non-default install pass their own mcp_config.
_DEFAULT_MCP_CONFIG = {"command": "kkernel", "args": ["mcp"]}
_VALID_CADENCE = ("first_turn", "every_turn")


@dataclass(frozen=True)
class RecallPolicy:
    limit: int = 5
    min_score: float = 0.4
    max_tokens: int = 800


@dataclass(frozen=True)
class ComposePolicy:
    enabled: bool = False
    max_tokens: int = 2000


@dataclass(frozen=True)
class WritebackPolicy:
    enabled: bool = False
    salience_cap: float = 0.4
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class KhiveInjectionPolicy:
    """Policy block controlling pre-turn khive injection (ADR-0008); see
    docs/internals/runtime.md for the namespace isolation contract."""

    profile_id: str
    enabled: bool = True
    snapshot_id: str | None = None
    namespace: str | None = None
    recall: RecallPolicy = field(default_factory=RecallPolicy)
    compose: ComposePolicy = field(default_factory=ComposePolicy)
    cadence: str = "first_turn"
    writeback: WritebackPolicy = field(default_factory=WritebackPolicy)

    def __post_init__(self):
        if not self.profile_id:
            raise ValueError(
                "KhiveInjectionPolicy.profile_id is required — a catch-all default "
                "profile mis-attributes feedback events."
            )
        if self.cadence not in _VALID_CADENCE:
            raise ValueError(f"cadence must be one of {_VALID_CADENCE}, got {self.cadence!r}")


async def _call_khive(ops: str, mcp_config: dict) -> Any:
    """One MCP round-trip to khive's `request` tool. Lazy import: the mcp/fastmcp
    transport is only touched here, never at module load."""
    from lionagi.service.connections.mcp_wrapper import MCPConnectionPool, MCPSecurityConfig

    security = MCPSecurityConfig(allow_commands=True, allow_urls=True)
    client = await MCPConnectionPool.get_client(mcp_config, security=security)
    result = await client.call_tool("request", {"ops": ops})
    return _unwrap(result)


def _unwrap(result: Any) -> Any:
    """MCP tool results carry a `.content` list of text blocks; khive's own
    payload is JSON-encoded text, so decode it back into data."""
    content = getattr(result, "content", None)
    if isinstance(content, list) and len(content) == 1:
        item = content[0]
        text = getattr(item, "text", None)
        if text is not None:
            return _maybe_json(text)
    if isinstance(result, str):
        return _maybe_json(result)
    return result


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _first_op_result(khive_response: Any) -> Any:
    if not isinstance(khive_response, dict):
        return None
    results = khive_response.get("results") or []
    if not results or not isinstance(results[0], dict):
        return None
    return results[0].get("result")


def _first_result_id(khive_response: Any) -> str | None:
    op_result = _first_op_result(khive_response)
    if isinstance(op_result, list) and op_result and isinstance(op_result[0], dict):
        rid = op_result[0].get("id")
        return str(rid) if rid else None
    return None


def _render_recall(khive_response: Any) -> str | None:
    op_result = _first_op_result(khive_response)
    if not op_result:
        return None
    lines = ["# khive recall"]
    for item in op_result:
        if isinstance(item, dict):
            lines.append(f"- {item.get('content', item)}")
        else:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _render_compose(khive_response: Any) -> str | None:
    op_result = _first_op_result(khive_response)
    if not op_result:
        return None
    return f"# khive compose\n{op_result}"


def _ns_kwarg(namespace: str | None) -> str:
    """The ``, namespace="..."`` ops fragment when a namespace is pinned, else ""."""
    return f", namespace={json.dumps(namespace)}" if namespace else ""


def _truncate(text: str, max_tokens: int | None) -> str:
    """None = uncapped; a non-positive cap is a hard zero budget and suppresses
    the text entirely."""
    if not text or max_tokens is None:
        return text
    if max_tokens <= 0:
        return ""

    from lionagi.service.token_calculator import TokenCalculator

    if TokenCalculator.tokenize(text) <= max_tokens:
        return text

    lo, hi, best = 0, len(text), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid]
        if TokenCalculator.tokenize(candidate) <= max_tokens:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _extract_writeback_pairs(action_responses: list) -> list[dict]:
    """FIFO-pair each tool error with the next response that resolves it.
    Deliberately rule-based: no LLM summarization on the writeback path."""
    pairs: list[dict] = []
    pending: list[dict] = []
    for resp in action_responses:
        function = getattr(resp, "function", None)
        output = getattr(resp, "output", None)
        is_error = isinstance(output, dict) and "error" in output
        if is_error:
            pending.append({"function": function, "error": output.get("error")})
            continue
        if pending:
            err = pending.pop(0)
            pairs.append(
                {
                    "function": err["function"],
                    "error": err["error"],
                    "resolved_by": function,
                    "resolution_output": output,
                }
            )
    return pairs


class KhiveInjectionProvider:
    """Pre-turn ContextProvider (ADR-0008): recall + optional compose against khive.
    See docs/internals/runtime.md for the provide()/writeback() contract and failure-swallowing behavior."""

    def __init__(self, policy: KhiveInjectionPolicy, mcp_config: dict | None = None):
        if policy.snapshot_id is not None:
            raise ValueError(
                "KhiveInjectionProvider cannot honor policy.snapshot_id yet — the khive "
                "recall/compose surface has no snapshot parameter. policy.namespace "
                "pinning is the supported isolation mechanism; set snapshot_id=None "
                "and use namespace instead."
            )
        self.policy = policy
        self.name = f"khive_injection:{policy.profile_id}"
        self._mcp_config = dict(mcp_config or _DEFAULT_MCP_CONFIG)

    async def provide(self, branch: Branch, instruction: Instruction) -> str | None:
        if not self.policy.enabled:
            return None
        if not self._should_fire(branch):
            return None

        query = self._build_query(branch, instruction)

        try:
            blocks = [b for b in (await self._recall(query), await self._maybe_compose(query)) if b]
        except Exception:
            logger.warning(
                "KhiveInjectionProvider transport failure; degrading to no injection this turn",
                exc_info=True,
            )
            return None

        if not blocks:
            return None

        text = "\n".join(blocks)
        cap = self.policy.recall.max_tokens + (
            self.policy.compose.max_tokens if self.policy.compose.enabled else 0
        )
        return _truncate(text, cap)

    async def writeback(self, branch: Branch, action_responses: list) -> None:
        wb = self.policy.writeback
        if not wb.enabled:
            return

        pairs = _extract_writeback_pairs(action_responses)
        if not pairs:
            return

        role = getattr(branch, "name", None) or "agent"
        tags = list(wb.tags) or [f"agent:{role}"]
        ns = _ns_kwarg(self.policy.namespace)
        for pair in pairs:
            content = (
                f"tool '{pair['function']}' failed ({pair['error']!r}); resolved by "
                f"'{pair['resolved_by']}', output={pair['resolution_output']!r}"
            )
            ops = (
                f"memory.remember(content={json.dumps(content)}, "
                f"salience={wb.salience_cap}, tags={json.dumps(tags)}{ns})"
            )
            try:
                await _call_khive(ops, self._mcp_config)
            except Exception:
                logger.warning("KhiveInjectionProvider writeback failed; skipping", exc_info=True)
                return

    def _should_fire(self, branch: Branch) -> bool:
        if self.policy.cadence == "every_turn":
            return True
        return branch.msgs.last_response is None

    def _build_query(self, branch: Branch, instruction: Instruction) -> str:
        task_text = ""
        if instruction is not None:
            try:
                rendered = instruction.rendered
                task_text = rendered if isinstance(rendered, str) else str(rendered)
            except Exception:
                task_text = ""
        task_text = task_text[:400]
        role = getattr(branch, "name", None) or "agent"
        return f"role={role} task={task_text}"

    async def _recall(self, query: str) -> str | None:
        rp = self.policy.recall
        ns = _ns_kwarg(self.policy.namespace)
        ops = (
            f"memory.recall(query={json.dumps(query)}, limit={rp.limit}, "
            f"min_score={rp.min_score}{ns})"
        )
        result = await _call_khive(ops, self._mcp_config)

        first_id = _first_result_id(result)
        if first_id:
            fb_ops = (
                f"brain.auto_feedback(query={json.dumps(query)}, "
                f"results={json.dumps([{'id': first_id}])}, "
                f"served_by_profile_id={json.dumps(self.policy.profile_id)}{ns})"
            )
            try:
                await _call_khive(fb_ops, self._mcp_config)
            except Exception:
                logger.warning("KhiveInjectionProvider auto_feedback failed", exc_info=True)

        return _render_recall(result)

    async def _maybe_compose(self, query: str) -> str | None:
        if not self.policy.compose.enabled:
            return None
        cp = self.policy.compose
        ns = _ns_kwarg(self.policy.namespace)
        ops = f"knowledge.compose(query={json.dumps(query)}, max_tokens={cp.max_tokens}{ns})"
        result = await _call_khive(ops, self._mcp_config)
        return _render_compose(result)
