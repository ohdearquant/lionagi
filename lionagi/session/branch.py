# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal, overload

from pydantic import BaseModel, JsonValue, PrivateAttr, field_serializer

from lionagi.config import settings
from lionagi.ln import AlcallParams
from lionagi.ln.types import Unset
from lionagi.models.field_model import FieldModel
from lionagi.operations.fields import Instruct
from lionagi.operations.manager import OperationManager
from lionagi.protocols._concepts import Relational
from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.action.tool import FuncTool, Tool, ToolRef
from lionagi.protocols.generic import (
    ID,
    DataLogger,
    DataLoggerConfig,
    Element,
    Log,
    Pile,
    Progression,
)
from lionagi.protocols.memory import InMemoryStore, MemoryStore
from lionagi.protocols.messages import (
    ActionRequest,
    ActionResponse,
    AssistantResponse,
    Instruction,
    MessageManager,
    MessageRole,
    RoledMessage,
    SenderRecipient,
    System,
)
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.manager import iModel, iModelManager
from lionagi.tools.base import LionTool

from .prompts import LION_SYSTEM_MESSAGE

if TYPE_CHECKING:
    from lionagi.operations.operate.operative import Operative
    from lionagi.operations.types import Middle
    from lionagi.protocols.context_providers import ContextProviderRegistry
    from lionagi.session.control import LoopControl, LoopDirective


__all__ = ("Branch",)


def _strip_capability_block(text: str) -> str:
    """Remove a previously-injected capability block (between its markers)."""
    if not text:
        return ""
    from .capabilities import CAP_BEGIN, CAP_END

    start = text.find(CAP_BEGIN)
    if start == -1:
        return text
    end = text.find(CAP_END, start)
    if end == -1:
        # Unbalanced markers — leave intact rather than silently corrupt
        return text
    return (text[:start] + text[end + len(CAP_END) :]).strip()


def _merge_instruct(
    instruct: "Instruct | dict[str, Any] | None",
    instruction=None,
    guidance=None,
    context=None,
) -> dict:
    """Normalize instruct to a dict, overlaying loose instruction fields."""
    if isinstance(instruct, Instruct):
        merged = instruct.to_dict()
    elif instruct:
        merged = dict(instruct)
    else:
        merged = {}
    if instruction is not None:
        merged["instruction"] = instruction
    if guidance is not None:
        merged["guidance"] = guidance
    if context is not None:
        merged["context"] = context
    return merged


class Branch(Element, Relational):
    """Conversation branch: facade over message, action, model, and log managers."""

    user: SenderRecipient | None = None
    name: str | None = None

    _message_manager: MessageManager | None = PrivateAttr(None)
    _action_manager: ActionManager | None = PrivateAttr(None)
    _imodel_manager: iModelManager | None = PrivateAttr(None)
    _log_manager: DataLogger | None = PrivateAttr(None)
    _operation_manager: OperationManager | None = PrivateAttr(None)
    _observer: Any = PrivateAttr(None)
    _hooks: Any = PrivateAttr(None)
    _pending_hook_bus_entries: list = PrivateAttr(default_factory=list)
    _hook_bus_synced_to: Any = PrivateAttr(None)
    _hook_bus_synced_count: int = PrivateAttr(0)
    _hook_bus_registered: list = PrivateAttr(default_factory=list)
    _memory: MemoryStore | None = PrivateAttr(None)
    _owning_session_id: Any = PrivateAttr(None)
    _capabilities: Any = PrivateAttr(None)
    _loop_control: "LoopControl | None" = PrivateAttr(None)
    _signal_tasks: list = PrivateAttr(default_factory=list)
    _context_providers: "ContextProviderRegistry | None" = PrivateAttr(None)
    _last_context_report: ContextVar[Any] = PrivateAttr(
        default_factory=lambda: ContextVar("last_context_report", default=None)
    )
    _last_context_report_fallback: Any = PrivateAttr(None)

    def __init__(
        self,
        *,
        user: "SenderRecipient" = None,
        name: str | None = None,
        messages: Pile[RoledMessage] = None,
        system: System | JsonValue = None,
        system_sender: "SenderRecipient" = None,
        chat_model: iModel | dict | str = None,
        parse_model: iModel | dict | str = None,
        tools: FuncTool | list[FuncTool] = None,
        log_config: DataLoggerConfig | dict = None,
        system_datetime: bool | str = None,
        system_template=None,
        system_template_context: dict = None,
        logs: Pile[Log] = None,
        use_lion_system_message: bool = False,
        memory: MemoryStore | None = None,
        **kwargs,
    ):
        super().__init__(user=user, name=name, **kwargs)

        self._memory = memory

        from lionagi.protocols.messages.manager import MessageManager

        self._message_manager = MessageManager(messages=messages)
        self._message_manager._on_message_added.append(self._schedule_emit)

        if system_template is not None:
            import warnings

            warnings.warn(
                "system_template is deprecated and has no effect. "
                "Template rendering has been removed from the message "
                "system. This parameter will be removed in a future "
                "release.",
                DeprecationWarning,
                stacklevel=2,
            )

        if system_template_context is not None:
            import warnings

            warnings.warn(
                "system_template_context is deprecated and has no "
                "effect. Template rendering has been removed from the "
                "message system. This parameter will be removed in a "
                "future release.",
                DeprecationWarning,
                stacklevel=2,
            )

        if any(
            bool(x)
            for x in [
                system,
                system_datetime,
                use_lion_system_message,
            ]
        ):
            if use_lion_system_message:
                system = f"Developer Prompt: {str(system)}" if system else ""
                system = (LION_SYSTEM_MESSAGE + "\n\n" + system).strip()

            self._message_manager.add_message(
                system=system,
                system_datetime=system_datetime,
                recipient=self.id,
                sender=system_sender or self.user or MessageRole.SYSTEM,
            )

        if not chat_model:
            chat_model = iModel(
                provider=settings.LIONAGI_CHAT_PROVIDER,
                model=settings.LIONAGI_CHAT_MODEL,
            )
        if not parse_model:
            parse_model = chat_model

        if isinstance(chat_model, dict):
            chat_model = iModel.from_dict(chat_model)
        elif isinstance(chat_model, str):
            chat_model = iModel(model=chat_model)

        if isinstance(parse_model, dict):
            parse_model = iModel.from_dict(parse_model)
        elif isinstance(parse_model, str):
            parse_model = iModel(model=parse_model)

        self._imodel_manager = iModelManager(chat=chat_model, parse=parse_model)

        self._action_manager = ActionManager()
        if tools:
            self.register_tools(tools)

        if log_config:
            if isinstance(log_config, dict):
                log_config = DataLoggerConfig(**log_config)
            self._log_manager = DataLogger.from_config(log_config, logs=logs)
        else:
            self._log_manager = DataLogger(**settings.LOG_CONFIG, logs=logs)

        self._operation_manager = OperationManager()

    @property
    def system(self) -> System | None:
        return self._message_manager.system

    @property
    def on_message_added(self) -> list:
        return self._message_manager._on_message_added

    @property
    def msgs(self) -> MessageManager:
        return self._message_manager

    @property
    def acts(self) -> ActionManager:
        return self._action_manager

    @property
    def mdls(self) -> iModelManager:
        return self._imodel_manager

    @property
    def progression(self) -> Progression:
        # metadata['current_progression'] is an agent-managed subset that
        # evicts messages from LLM view without deleting them
        cp = self.metadata.get("current_progression")
        if cp is not None:
            return cp
        return self._message_manager.progression

    @property
    def messages(self) -> Pile[RoledMessage]:
        return self._message_manager.messages

    @property
    def token_budget(self):
        from lionagi.service.token_budget import get_token_budget

        return get_token_budget(self)

    @property
    def logs(self) -> Pile[Log]:
        return self._log_manager.logs

    @property
    def memory(self) -> MemoryStore:
        """This branch's memory store: an explicitly supplied backend, or a
        lazily-created private `InMemoryStore` on first access. Read-only —
        the only way to give a `Branch` its own store is the `memory=`
        constructor parameter."""
        if self._memory is None:
            self._memory = InMemoryStore()
        return self._memory

    @property
    def providers(self) -> "ContextProviderRegistry":
        """This branch's pre-turn ContextProvider registry: lazily created
        on first access. Optional and zero-cost when unused — a branch that
        never touches this property never gathers or renders injections."""
        if self._context_providers is None:
            from lionagi.protocols.context_providers import ContextProviderRegistry

            self._context_providers = ContextProviderRegistry()
        return self._context_providers

    @property
    def last_context_report(self):
        """ProviderReport from this task's latest provider pass, when present.

        Otherwise returns the branch's most recently completed provider pass
        for backward compatibility. Concurrent passes use last-writer semantics
        for that branch-level fallback.

        When the branch has no system message there is no render target, so
        providers are not invoked and the report lists every registered
        provider under ``skipped``.
        """
        task_report = self._last_context_report.get()
        if task_report is not None:
            return task_report
        return self._last_context_report_fallback

    @property
    def chat_model(self) -> iModel:
        return self._imodel_manager.chat

    @chat_model.setter
    def chat_model(self, value: iModel) -> None:
        self._imodel_manager.register_imodel("chat", value)

    @property
    def parse_model(self) -> iModel:
        return self._imodel_manager.parse

    @parse_model.setter
    def parse_model(self, value: iModel) -> None:
        self._imodel_manager.register_imodel("parse", value)

    @property
    def tools(self) -> dict[str, Tool]:
        return self._action_manager.registry

    def get_operation(self, operation: str) -> Callable | None:
        if hasattr(self, operation):
            return getattr(self, operation)
        return self._operation_manager.registry.get(operation)

    async def emit(self, event: Any) -> list[Any]:
        """Emit an event to the session observer. No-op when standalone."""
        if self._observer is None:
            return []
        return await self._observer.emit(event)

    async def _safe_emit(self, event: Any) -> None:
        """Emit a lifecycle event; observer exceptions are logged, never re-raised
        (lifecycle failures must not alter run outcomes)."""
        import logging as _logging

        try:
            await self.emit(event)
        except Exception:
            _logging.getLogger(__name__).exception(
                "branch: observer raised during lifecycle emission of %s; run outcome is preserved",
                type(event).__name__,
            )

    async def authorize(self, action: Any) -> bool:
        """Pre-invoke governance gate. Standalone branches always allow."""
        if self._observer is None:
            return True
        return await self._observer.authorize(action)

    def _origin_filtered_handler(self, handler: Any) -> Any:
        """Wrap a branch-owned bus handler so a shared session bus only
        invokes it for events that originate from this branch.

        Emissions that carry a ``branch_id`` kwarg (e.g. ``UserPromptSubmit``)
        are matched against this branch's id; emissions that don't (e.g.
        ``SessionStart``/``SessionEnd``, which are genuinely session-wide)
        pass through unfiltered. Without this, every branch sharing a
        session's bus would see every other branch's events.
        """
        from lionagi.ln.concurrency import maybe_await

        origin_id = str(self.id)

        async def _filtered(**kwargs: Any) -> Any:
            branch_id = kwargs.get("branch_id")
            if branch_id is not None and branch_id != origin_id:
                return None
            return await maybe_await(handler(**kwargs))

        return _filtered

    def attach_hook_bus(self, bus: Any) -> None:
        """Set this branch's :class:`HookBus` and (re)register any external
        handlers queued for bus attachment.

        A standalone branch built via ``create_agent`` has no bus yet, so
        ``hooks_external`` entries bound to bus-only events (``UserPromptSubmit``,
        ``SessionStart``/``SessionEnd``/``PostToolUseFailure``) cannot attach
        at config time; ``lionagi.agent.factory._wire_external_hooks`` queues
        them onto ``_pending_hook_bus_entries`` instead of dropping them.
        That list is retained for the branch's lifetime, not cleared after
        the first flush, so a branch moved between sessions (``Session.
        remove_branch`` then ``include_branches``/``new_branch`` elsewhere)
        re-registers the same external handlers on its new session's bus
        instead of silently losing them. Re-attaching the same bus is a
        no-op for entries already registered on it -- only the entries
        appended since the last sync onto the current bus are flushed.
        Every seam that gives this branch a bus -- ``Session.include_branches``
        and the lazy ``Session.hooks`` property -- must route the assignment
        through this method so those queued handlers actually attach, rather
        than a configured guard silently never firing.

        Each registered handler is wrapped with an origin-branch filter (see
        ``_origin_filtered_handler``) so a bus shared by multiple branches
        never cross-fires one branch's hook for another branch's event.
        Switching to a genuinely different bus -- or detaching entirely via
        ``attach_hook_bus(None)`` -- first unregisters every wrapper this
        branch put on the old bus, so a reparented or removed branch leaves
        no stale handler behind.
        """
        old_bus = self._hook_bus_synced_to
        if old_bus is not None and old_bus is not bus:
            for point, wrapped in self._hook_bus_registered:
                old_bus.off(point, wrapped)
            self._hook_bus_registered = []
            self._hook_bus_synced_count = 0

        self._hooks = bus
        if bus is None:
            self._hook_bus_synced_to = None
            return
        if bus is not self._hook_bus_synced_to:
            self._hook_bus_synced_to = bus
            self._hook_bus_synced_count = 0
        unsynced = self._pending_hook_bus_entries[self._hook_bus_synced_count :]
        for point, handler in unsynced:
            wrapped = self._origin_filtered_handler(handler)
            bus.on(point, wrapped)
            self._hook_bus_registered.append((point, wrapped))
        self._hook_bus_synced_count = len(self._pending_hook_bus_entries)

    async def _persist_via_bus(self, msg: Any) -> None:
        """on_message_added hook: emit MESSAGE_ADD for ordered persistence."""
        if self._hooks is None:
            return
        from lionagi.hooks.bus import HookPoint

        await self._hooks.emit(HookPoint.MESSAGE_ADD, branch_id=str(self.id), message=msg)

    def _schedule_emit(self, msg: Any) -> None:
        """on_message_added hook: fire-and-forget signal emission."""
        if self._observer is None:
            return
        import asyncio

        from lionagi.operations._observe import emit_message

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._signal_tasks.append(loop.create_task(emit_message(self, msg)))

    async def drain_signals(self) -> None:
        """Await pending background emissions. Called at operation boundaries."""
        if not self._signal_tasks:
            return
        from lionagi.ln.concurrency import gather as _gather

        tasks, self._signal_tasks = self._signal_tasks, []
        await _gather(*tasks, return_exceptions=True)

    async def _observed_run(self, coro: Any) -> Any:
        """Wrap an operation with RunStart/RunEnd lifecycle + signal drain."""
        import time as _time  # noqa: PLC0415

        has_observer = self._observer is not None
        if has_observer:
            from .signal import RunStart

            await self._safe_emit(RunStart())
        _t0 = _time.monotonic()
        # This wrapper owns the lifecycle pair for the operation; suppress the
        # nested run() generator's own pair so one CLI-backed operate() emits
        # exactly one RunStart/RunEnd (same task-scoped mechanism ReAct uses).
        from ._lifecycle_ctx import suppress_lifecycle_var

        _lc_token = suppress_lifecycle_var.set(True)
        try:
            result = await coro
        except BaseException as exc:
            suppress_lifecycle_var.reset(_lc_token)
            await self.drain_signals()
            if has_observer:
                from .signal import RunFailed

                await self._safe_emit(RunFailed(data=exc))
            raise
        suppress_lifecycle_var.reset(_lc_token)
        await self.drain_signals()
        if has_observer:
            from .signal import build_run_end

            duration_ms = (_time.monotonic() - _t0) * 1000.0
            await self._safe_emit(build_run_end(self, duration_ms=duration_ms, result=result))
        return result

    async def emit_and_log(self, event: Any) -> list[Any]:
        """Log an event durably AND emit it onto the reactive bus."""
        self._log_manager.log(event)
        return await self.emit(event)

    def control(self, directive: "LoopDirective", *, reason: str | None = None) -> None:
        """Queue a loop-control directive for the in-flight run."""
        from lionagi.session.control import LoopControl

        self._loop_control = LoopControl(directive, reason)

    def poll_control(self) -> "LoopControl | None":
        """Return and clear any queued directive (one-shot)."""
        ctrl, self._loop_control = self._loop_control, None
        return ctrl

    @property
    def capabilities(self) -> Any:
        return self._capabilities

    @capabilities.setter
    def capabilities(self, operable: Any) -> None:
        self._capabilities = operable

    def grant_capabilities(self, operable: Any, *, prompt: bool = True) -> None:
        """Set the capability grant and optionally inject its prompt block."""
        self._capabilities = operable
        if prompt:
            from .capabilities import CAP_BEGIN, CAP_END, render_capabilities_prompt

            block = f"{CAP_BEGIN}\n{render_capabilities_prompt(operable)}\n{CAP_END}"
            base = _strip_capability_block(self._system_text())
            combined = f"{base.rstrip()}\n\n{block}" if base.strip() else block
            self.msgs.set_system(self.msgs.create_system(system=combined))

    def revoke_capabilities(self) -> None:
        self._capabilities = None
        base = _strip_capability_block(self._system_text())
        if self.msgs.system is not None:
            self.msgs.set_system(self.msgs.create_system(system=base or None))

    def _system_text(self) -> str:
        sys_msg = self.msgs.system
        if sys_msg is None:
            return ""
        return sys_msg.content.system_message or ""

    async def aclone(self, sender: ID.Ref = None) -> "Branch":
        async with self.msgs.messages:
            return self.clone(sender)

    def clone(self, sender: ID.Ref = None) -> "Branch":
        if sender is not None:
            if not ID.is_id(sender):
                raise ValueError(f"Cannot clone Branch: '{sender}' is not a valid sender ID.")
            sender = ID.get_id(sender)

        system = self.msgs.system.clone() if self.msgs.system else None
        tools = (
            list(self._action_manager.registry.values()) if self._action_manager.registry else None
        )
        chat_model = self.chat_model.copy() if self.chat_model.is_cli else self.chat_model
        parse_model = (
            self.parse_model.copy()
            if (self.parse_model is not self.chat_model and self.parse_model.is_cli)
            else self.parse_model
        )

        cloned_messages = []
        old_to_new_id: dict = {}
        for msg in self.msgs.messages:
            cloned = msg.clone()
            old_to_new_id[msg.id] = cloned.id
            cloned_messages.append(cloned)

        branch_clone = Branch(
            system=system,
            user=self.user,
            messages=cloned_messages,
            tools=tools,
            chat_model=chat_model,
            parse_model=parse_model,
            metadata={"clone_from": self},
        )
        for message in branch_clone.msgs.messages:
            message.sender = sender or self.id
            message.recipient = branch_clone.id

        # A source current_progression is an intentional eviction of some
        # durable messages from the active context. Cloned messages get new
        # IDs, so the source's evicted-subset progression has to be remapped
        # onto them - otherwise it's silently dropped and Branch.progression
        # falls back to the clone's complete (uninentionally un-evicted)
        # message set.
        source_progression = self.metadata.get("current_progression")
        if source_progression is not None:
            remapped_order = [
                old_to_new_id[uid] for uid in source_progression.order if uid in old_to_new_id
            ]
            branch_clone.metadata["current_progression"] = Progression(order=remapped_order)

        return branch_clone

    def _register_tool(self, tools: FuncTool | LionTool, update: bool = False):
        if isinstance(tools, type) and issubclass(tools, LionTool):
            tools = tools()
        if isinstance(tools, LionTool):
            tools = tools.to_tool()
        self._action_manager.register_tool(tools, update=update)

    def register_tools(self, tools: FuncTool | list[FuncTool] | LionTool, update: bool = False):
        tools = [tools] if not isinstance(tools, list) else tools
        for tool in tools:
            self._register_tool(tool, update=update)

    @field_serializer("user")
    def _serialize_user(self, v):
        return str(v) if v else None

    def to_df(self, *, progression: Progression = None):
        from lionagi.protocols.generic.pile import Pile
        from lionagi.protocols.messages.base import MESSAGE_FIELDS

        if progression is None:
            progression = self.msgs.progression

        msgs = [self.msgs.messages[i] for i in progression if i in self.msgs.messages]
        p = Pile(collections=msgs)
        return p.to_df(columns=MESSAGE_FIELDS)

    def connect(
        self,
        provider: str = None,
        base_url: str = None,
        endpoint: str | Endpoint = "chat",
        endpoint_params: list[str] | None = None,
        api_key: str = None,
        queue_capacity: int = 100,
        capacity_refresh_time: float = 60,
        interval: float | None = None,
        limit_requests: int = None,
        limit_tokens: int = None,
        invoke_with_endpoint: bool = False,
        imodel: iModel = None,
        name: str = None,
        request_options: type[BaseModel] = None,
        description: str = None,
        update: bool = False,
        **kwargs,
    ):
        if not imodel:
            imodel = iModel(
                provider=provider,
                base_url=base_url,
                endpoint=endpoint,
                endpoint_params=endpoint_params,
                api_key=api_key,
                queue_capacity=queue_capacity,
                capacity_refresh_time=capacity_refresh_time,
                interval=interval,
                limit_requests=limit_requests,
                limit_tokens=limit_tokens,
                invoke_with_endpoint=invoke_with_endpoint,
                **kwargs,
            )

        if not update and name in self.tools:
            raise ValueError(f"Tool with name '{name}' already exists.")

        async def _connect(**kwargs):
            api_call = await imodel.invoke(**kwargs)
            await self.emit_and_log(api_call)
            return api_call.response

        _connect.__name__ = name or imodel.endpoint.name
        if description:
            _connect.__doc__ = description

        tool = Tool(
            func_callable=_connect,
            request_options=request_options or imodel.request_options,
        )
        self._action_manager.register_tools(tool, update=update)

    @field_serializer("metadata")
    def _serialize_metadata_if_clone(self, v):
        if "clone_from" not in v:
            return v
        source = v["clone_from"]
        # A clone restored via from_dict keeps clone_from as the already-serialized
        # dict; re-serializing must be idempotent, not dereference source.id on a dict.
        if isinstance(source, dict):
            return v
        v = dict(v)
        v["clone_from"] = {
            "id": str(source.id),
            "user": str(source.user),
            "created_at": source.created_at,
            "progression": [str(i) for i in source.msgs.progression],
        }
        return v

    def to_dict(
        self,
        mode: Literal["python", "json", "db"] = "python",
        db_meta_key: str | None = None,
        include_request_options: bool = False,
        include_logs: bool = True,
        include_log_config: bool = False,
        include_processor_config: bool = False,
        **kw,
    ) -> dict:
        dict_ = super().to_dict(mode=mode, db_meta_key=db_meta_key, **kw)
        dict_["messages"] = (
            self.messages.to_dict(mode=mode)
            if self.messages
            else {"collections": [], "progression": {"order": []}}
        )
        if include_logs and self.logs:
            dict_["logs"] = self.logs.to_dict(mode=mode)
        if self.system:
            dict_["system"] = self.system.to_dict(mode=mode)
        if include_log_config:
            dict_["log_config"] = self._log_manager._config.model_dump()
        dict_["chat_model"] = self.chat_model.to_dict(
            include_request_options=include_request_options,
            include_processor_config=include_processor_config,
        )
        if self.parse_model is not self.chat_model:
            dict_["parse_model"] = self.parse_model.to_dict(
                include_request_options=include_request_options,
                include_processor_config=include_processor_config,
            )
        return dict_

    @classmethod
    def from_dict(cls, data: dict):
        # Copy first: the pops below would otherwise strip messages, chat_model,
        # and log_config out of the caller's snapshot, so it could not be reused
        # for a retry, comparison, or a second restoration.
        data = dict(data)
        dict_ = {
            "messages": data.pop("messages", Unset),
            "logs": data.pop("logs", Unset),
            "chat_model": data.pop("chat_model", Unset),
            "parse_model": data.pop("parse_model", Unset),
            "system": data.pop("system", Unset),
            "log_config": data.pop("log_config", Unset),
        }

        # System message is already in the serialized messages pile — skip re-adding
        messages_val = dict_.get("messages", Unset)
        system_val = dict_.get("system", Unset)
        if messages_val is not Unset and system_val is not Unset:
            dict_["system"] = Unset

        params = {}
        for k, v in data.items():
            if isinstance(v, dict) and "id" in v:
                params.update(v)
            else:
                params[k] = v

        params.update(dict_)
        return cls(**{k: v for k, v in params.items() if v is not Unset})

    def dump_logs(self, clear: bool = True, persist_path=None):
        self._log_manager.dump(clear=clear, persist_path=persist_path)

    async def adump_logs(self, clear: bool = True, persist_path=None):
        await self._log_manager.adump(clear=clear, persist_path=persist_path)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._log_manager.adump(clear=True)

    @overload
    async def chat(
        self,
        instruction: Instruction | JsonValue = None,
        guidance: JsonValue = None,
        context: JsonValue = None,
        sender: ID.Ref = None,
        recipient: ID.Ref = None,
        request_fields: list[str] | dict[str, JsonValue] = None,
        response_format: type[BaseModel] | BaseModel = None,
        progression: Progression | list[ID[RoledMessage].ID] = None,
        imodel: iModel = None,
        tool_schemas: list[dict] = None,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        plain_content: str = None,
        return_ins_res_message: Literal[False] = False,
        include_token_usage_to_model: bool = False,
        _turn_origin: Any = None,
        **kwargs,
    ) -> str: ...

    @overload
    async def chat(
        self,
        instruction: Instruction | JsonValue = None,
        guidance: JsonValue = None,
        context: JsonValue = None,
        sender: ID.Ref = None,
        recipient: ID.Ref = None,
        request_fields: list[str] | dict[str, JsonValue] = None,
        response_format: type[BaseModel] | BaseModel = None,
        progression: Progression | list[ID[RoledMessage].ID] = None,
        imodel: iModel = None,
        tool_schemas: list[dict] = None,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        plain_content: str = None,
        return_ins_res_message: Literal[True] = ...,
        include_token_usage_to_model: bool = False,
        _turn_origin: Any = None,
        **kwargs,
    ) -> tuple[Instruction, AssistantResponse]: ...

    @overload
    async def chat(
        self,
        instruction: Instruction | JsonValue = None,
        guidance: JsonValue = None,
        context: JsonValue = None,
        sender: ID.Ref = None,
        recipient: ID.Ref = None,
        request_fields: list[str] | dict[str, JsonValue] = None,
        response_format: type[BaseModel] | BaseModel = None,
        progression: Progression | list[ID[RoledMessage].ID] = None,
        imodel: iModel = None,
        tool_schemas: list[dict] = None,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        plain_content: str = None,
        return_ins_res_message: bool = ...,
        include_token_usage_to_model: bool = False,
        _turn_origin: Any = None,
        **kwargs,
    ) -> str | tuple[Instruction, AssistantResponse]: ...

    async def chat(
        self,
        instruction: Instruction | JsonValue = None,
        guidance: JsonValue = None,
        context: JsonValue = None,
        sender: ID.Ref = None,
        recipient: ID.Ref = None,
        request_fields: list[str] | dict[str, JsonValue] = None,
        response_format: type[BaseModel] | BaseModel = None,
        progression: Progression | list[ID[RoledMessage].ID] = None,
        imodel: iModel = None,
        tool_schemas: list[dict] = None,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        plain_content: str = None,
        return_ins_res_message: bool = False,
        include_token_usage_to_model: bool = False,
        _turn_origin: Any = None,
        **kwargs,
    ) -> str | tuple[Instruction, AssistantResponse]:
        """Invoke the chat model. Does not auto-add messages to the branch."""
        from lionagi.operations.chat.chat import ChatParam, chat

        return await chat(
            self,
            instruction=instruction,
            chat_param=ChatParam(
                guidance=guidance,
                context=context,
                sender=sender or self.user or "user",
                recipient=recipient or self.id,
                response_format=response_format or request_fields,
                progression=progression,
                tool_schemas=tool_schemas or [],
                images=images or [],
                image_detail=image_detail or "auto",
                plain_content=plain_content or "",
                include_token_usage_to_model=include_token_usage_to_model,
                imodel=imodel or self.chat_model,
                imodel_kw=kwargs,
                turn_origin=_turn_origin,
            ),
            return_ins_res_message=return_ins_res_message,
        )

    async def chat_and_record(self, instruction: Instruction | JsonValue = None, **kwargs) -> str:
        """Like ``chat()``, but adds the turn via the hooked async add-path
        (mirrors ``communicate()``), so ``on_message_added`` observers (e.g.
        persistence) see it.

        Mints a turn-origin token (this is a public ingress) and forwards it
        unchanged into the delegated ``chat()`` call, so that call consumes
        the same token rather than minting a second one of its own.
        """
        from lionagi.operations._turn_origin import TurnOrigin

        kwargs.pop("return_ins_res_message", None)
        turn_origin = TurnOrigin.unset().mint_if_unset()
        ins, res = await self.chat(
            instruction, return_ins_res_message=True, _turn_origin=turn_origin, **kwargs
        )
        await self.msgs.a_add_message(instruction=ins)
        await self.msgs.a_add_message(assistant_response=res)
        return res.response

    async def parse(
        self,
        text: str,
        handle_validation: Literal["raise", "return_value", "return_none"] = "return_value",
        max_retries: int = 3,
        request_type: type[BaseModel] = None,
        operative: "Operative" = None,
        similarity_algo="jaro_winkler",
        similarity_threshold: float = 0.85,
        fuzzy_match: bool = True,
        handle_unmatched: Literal["ignore", "raise", "remove", "fill", "force"] = "force",
        fill_value: Any = None,
        fill_mapping: dict[str, Any] | None = None,
        strict: bool = False,
        response_format: type[BaseModel] = None,
    ) -> BaseModel | dict | str | None:
        """Parse text into a Pydantic model. Does not add messages to context."""
        _pms = {k: v for k, v in locals().items() if k not in ("self", "_pms") and v is not None}
        from lionagi.operations.parse.parse import parse, prepare_parse_kws

        return await parse(self, **prepare_parse_kws(self, **_pms))

    async def operate(
        self,
        *,
        instruct: "Instruct" = None,
        instruction: Instruction | JsonValue = None,
        guidance: JsonValue = None,
        context: JsonValue = None,
        sender: "SenderRecipient" = None,
        recipient: "SenderRecipient" = None,
        progression: Progression = None,
        chat_model: iModel = None,
        invoke_actions: bool = True,
        tool_schemas: list[dict] = None,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        parse_model: iModel = None,
        skip_validation: bool = False,
        tools: ToolRef = None,
        operative: "Operative" = None,
        response_format: type[BaseModel] = None,  # alias of operative.request_type
        actions: bool = False,
        reason: bool = False,
        call_params: AlcallParams = None,
        action_strategy: Literal["sequential", "concurrent"] = "concurrent",
        verbose_action: bool = False,
        field_models: list[FieldModel] = None,
        exclude_fields: list | dict | None = None,
        handle_validation: Literal["raise", "return_value", "return_none"] = "return_value",
        include_token_usage_to_model: bool = False,
        stream_persist: bool = False,
        persist_dir: str | None = None,
        middle: "Middle | None" = None,
        **kwargs,
    ) -> list | BaseModel | None | dict | str:
        """Operate: chat + optional tool invocation + structured parse. Adds messages."""
        _pms = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "_pms", "kwargs") and v is not None
        }
        if kwargs:
            _pms.update(kwargs)
        from lionagi.operations.operate.operate import operate, prepare_operate_kw

        return await self._observed_run(operate(self, **prepare_operate_kw(self, **_pms)))

    async def communicate(
        self,
        instruction: Instruction | JsonValue = None,
        *,
        guidance: JsonValue = None,
        context: JsonValue = None,
        plain_content: str = None,
        sender: "SenderRecipient" = None,
        recipient: "SenderRecipient" = None,
        progression: ID.IDSeq = None,
        response_format: type[BaseModel] = None,
        request_fields: dict | list[str] = None,
        chat_model: iModel = None,
        parse_model: iModel = None,
        skip_validation: bool = False,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        num_parse_retries: int = 3,
        clear_messages: bool = False,
        include_token_usage_to_model: bool = False,
        **kwargs,
    ) -> BaseModel | dict | str | None:
        """One-shot chat + optional parse, without tool invocation. Adds messages."""
        _pms = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "_pms", "kwargs") and v is not None
        }
        _pms.update(kwargs)

        from lionagi.operations.communicate.communicate import (
            communicate,
            prepare_communicate_kw,
        )

        return await self._observed_run(communicate(self, **prepare_communicate_kw(self, **_pms)))

    async def act(
        self,
        action_request: list | ActionRequest | BaseModel | dict,
        *,
        strategy: Literal["concurrent", "sequential"] = "concurrent",
        verbose_action: bool = False,
        suppress_errors: bool = True,
        call_params: AlcallParams = None,
    ) -> list[ActionResponse]:
        _pms = {k: v for k, v in locals().items() if k not in ("self", "_pms") and v is not None}
        from lionagi.operations.act.act import act, prepare_act_kw

        return await act(self, **prepare_act_kw(self, **_pms))

    async def interpret(
        self,
        text: str,
        domain: str | None = None,
        style: str | None = None,
        interpret_model=None,
        **kwargs,
    ) -> str:
        """Rewrite raw input into a clearer prompt. Does not add messages."""
        _pms = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "_pms", "kwargs") and v is not None
        }
        _pms.update(kwargs)

        from lionagi.operations.interpret.interpret import (
            interpret,
            prepare_interpret_kw,
        )

        return await interpret(self, **prepare_interpret_kw(self, **_pms))

    async def ReAct(  # noqa: N802
        self,
        instruct: "Instruct | dict[str, Any]" = None,
        instruction: Instruction | JsonValue = None,
        guidance: JsonValue = None,
        context: JsonValue = None,
        interpret: bool = False,
        interpret_domain: str | None = None,
        interpret_style: str | None = None,
        interpret_sample: str | None = None,
        interpret_model: iModel | None = None,
        interpret_kwargs: dict | None = None,
        tools: Any = None,
        tool_schemas: Any = None,
        response_format: type[BaseModel] | BaseModel = None,
        intermediate_response_options: list[BaseModel] | BaseModel = None,
        intermediate_listable: bool = False,
        reasoning_effort: Literal["low", "medium", "high"] = None,
        extension_allowed: bool = True,
        max_extensions: int | None = 3,
        response_kwargs: dict | None = None,
        display_as: Literal["json", "yaml"] = "yaml",
        return_analysis: bool = False,
        analysis_model: iModel | None = None,
        verbose: bool = False,
        verbose_length: int = None,
        include_token_usage_to_model: bool = True,
        **kwargs,
    ):
        """Multi-step think-act-observe loop with optional interpretation and extensions."""
        from lionagi.operations.ReAct.ReAct import ReAct

        instruct = _merge_instruct(instruct, instruction, guidance, context)
        kwargs_filtered = {
            k: v for k, v in kwargs.items() if k not in {"verbose_analysis", "verbose_action"}
        }

        # Emitted directly (not via _observed_run) to guarantee exactly ONE
        # RunStart per ReAct call, decoupled from operate()'s own emission.
        import time as _time  # noqa: PLC0415

        has_observer = self._observer is not None
        if has_observer:
            from .signal import RunStart

            await self._safe_emit(RunStart())
        # Task-scoped ContextVar suppresses nested run() emission inside
        # ReActStream without affecting concurrent runs on the same branch.
        from ._lifecycle_ctx import suppress_lifecycle_var

        _t0_react = _time.monotonic()
        _token = suppress_lifecycle_var.set(True)
        try:
            result = await ReAct(
                self,
                instruct,
                interpret=interpret,
                interpret_domain=interpret_domain,
                interpret_style=interpret_style,
                interpret_sample=interpret_sample,
                interpret_kwargs=interpret_kwargs,
                tools=tools,
                tool_schemas=tool_schemas,
                response_format=response_format,
                extension_allowed=extension_allowed,
                max_extensions=max_extensions,
                response_kwargs=response_kwargs,
                return_analysis=return_analysis,
                analysis_model=analysis_model,
                verbose_action=verbose,
                verbose_analysis=verbose,
                verbose_length=verbose_length,
                interpret_model=interpret_model,
                intermediate_response_options=intermediate_response_options,
                intermediate_listable=intermediate_listable,
                reasoning_effort=reasoning_effort,
                display_as=display_as,
                include_token_usage_to_model=include_token_usage_to_model,
                **kwargs_filtered,
            )
        except BaseException as exc:
            suppress_lifecycle_var.reset(_token)
            await self.drain_signals()
            if has_observer:
                from .signal import RunFailed

                await self._safe_emit(RunFailed(data=exc))
            raise
        suppress_lifecycle_var.reset(_token)
        await self.drain_signals()
        if has_observer:
            from .signal import build_run_end

            _dur_react = (_time.monotonic() - _t0_react) * 1000.0
            await self._safe_emit(build_run_end(self, duration_ms=_dur_react, result=result))
        return result

    async def ReActStream(  # noqa: N802
        self,
        instruct: "Instruct | dict[str, Any]" = None,
        instruction: Instruction | JsonValue = None,
        guidance: JsonValue = None,
        context: JsonValue = None,
        interpret: bool = False,
        interpret_domain: str | None = None,
        interpret_style: str | None = None,
        interpret_sample: str | None = None,
        interpret_model: iModel | None = None,
        interpret_kwargs: dict | None = None,
        tools: Any = None,
        tool_schemas: Any = None,
        response_format: type[BaseModel] | BaseModel = None,
        intermediate_response_options: list[BaseModel] | BaseModel = None,
        intermediate_listable: bool = False,
        reasoning_effort: Literal["low", "medium", "high"] = None,
        extension_allowed: bool = True,
        max_extensions: int | None = 3,
        response_kwargs: dict | None = None,
        analysis_model: iModel | None = None,
        verbose: bool = False,
        display_as: Literal["json", "yaml"] = "yaml",
        verbose_length: int = None,
        include_token_usage_to_model: bool = True,
        **kwargs,
    ) -> AsyncGenerator:
        from lionagi.operations.ReAct.ReAct import ReActStream, prepare_react_kw

        instruct_dict = _merge_instruct(instruct, instruction, guidance, context)
        kw = prepare_react_kw(
            self,
            instruct_dict,
            instruction_fallback=str(instruct),
            interpret=interpret,
            interpret_domain=interpret_domain,
            interpret_style=interpret_style,
            interpret_sample=interpret_sample,
            interpret_model=interpret_model,
            interpret_kwargs=interpret_kwargs,
            tools=tools,
            tool_schemas=tool_schemas,
            response_format=response_format,
            intermediate_response_options=intermediate_response_options,
            intermediate_listable=intermediate_listable,
            reasoning_effort=reasoning_effort,
            extension_allowed=extension_allowed,
            max_extensions=max_extensions,
            response_kwargs=response_kwargs,
            display_as=display_as,
            analysis_model=analysis_model,
            verbose_analysis=verbose,
            verbose_length=verbose_length,
            include_token_usage_to_model=include_token_usage_to_model,
            imodel_kw=kwargs,
        )

        async for result in ReActStream(self, **kw):
            if verbose:
                analysis, str_ = result
                from lionagi.libs.schema.as_readable import as_readable

                str_ += "\n---------\n"
                as_readable(str_, md=True, display_str=True)
                yield analysis
            else:
                yield result

    async def run(
        self,
        instruction: str = "",
        *,
        chat_model: "iModel | None" = None,
        guidance=None,
        context=None,
        sender=None,
        recipient=None,
        images=None,
        image_detail="auto",
        stream_persist: bool = False,
        persist_dir: str | None = None,
        response_format=None,
        **kwargs,
    ) -> "AsyncGenerator[RoledMessage, None]":
        """Stream messages from a CLI endpoint."""
        from lionagi.operations.run.run import run as _run
        from lionagi.operations.types import RunParam

        param_kw = dict(
            sender=sender,
            recipient=recipient,
            guidance=guidance,
            context=context,
            images=images,
            image_detail=image_detail,
            stream_persist=stream_persist,
            response_format=response_format,
        )
        if chat_model is not None:
            param_kw["imodel"] = chat_model
        if persist_dir is not None:
            param_kw["persist_dir"] = persist_dir
        if kwargs:
            param_kw["imodel_kw"] = kwargs

        async for msg in _run(self, instruction, RunParam(**param_kw)):
            yield msg
