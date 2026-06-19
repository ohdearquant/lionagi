# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any, Literal

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
    _capabilities: Any = PrivateAttr(None)
    _loop_control: "LoopControl | None" = PrivateAttr(None)
    _signal_tasks: list = PrivateAttr(default_factory=list)

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
        **kwargs,
    ):
        super().__init__(user=user, name=name, **kwargs)

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
        """Emit a lifecycle event, swallowing observer exceptions.

        Policy: lifecycle observer failures must never alter run outcomes.
        Exceptions are logged but not re-raised.
        """
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
        try:
            result = await coro
        except BaseException as exc:
            await self.drain_signals()
            if has_observer:
                from .signal import RunFailed

                await self._safe_emit(RunFailed(data=exc))
            raise
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

        branch_clone = Branch(
            system=system,
            user=self.user,
            messages=[msg.clone() for msg in self.msgs.messages],
            tools=tools,
            chat_model=chat_model,
            parse_model=parse_model,
            metadata={"clone_from": self},
        )
        for message in branch_clone.msgs.messages:
            message.sender = sender or self.id
            message.recipient = branch_clone.id

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
        v = dict(v)
        source = v["clone_from"]
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
        **kwargs,
    ) -> tuple[Instruction, AssistantResponse]:
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
            ),
            return_ins_res_message=return_ins_res_message,
        )

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

        # Emit lifecycle signals directly rather than through _observed_run so
        # that exactly ONE RunStart is emitted per ReAct call.  Using
        # _observed_run would be correct today, but if operate() inside
        # ReActStream were ever wrapped with its own _observed_run, the outer
        # wrapper here would produce N+1 RunStart events.  Inlining the
        # emission removes that coupling.
        import time as _time  # noqa: PLC0415

        has_observer = self._observer is not None
        if has_observer:
            from .signal import RunStart

            await self._safe_emit(RunStart())
        # Suppress nested lifecycle emission from run() calls inside ReActStream
        # using a task-scoped ContextVar so that concurrent runs on the SAME
        # branch are never affected — each asyncio task carries its own copy.
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
        from lionagi.ln.fuzzy import FuzzyMatchKeysParams
        from lionagi.operations.ReAct.ReAct import ReActStream
        from lionagi.operations.ReAct.utils import ReActAnalysis
        from lionagi.operations.types import (
            ActionParam,
            ChatParam,
            InterpretParam,
            ParseParam,
        )

        instruct_dict = _merge_instruct(instruct, instruction, guidance, context)

        intp_param = None
        if interpret:
            intp_param = InterpretParam(
                domain=interpret_domain or "general",
                style=interpret_style or "concise",
                sample_writing=interpret_sample or "",
                imodel=interpret_model or analysis_model or self.chat_model,
                imodel_kw=interpret_kwargs or {},
            )

        chat_param = ChatParam.from_branch(
            self,
            guidance=instruct_dict.get("guidance"),
            context=instruct_dict.get("context"),
            tool_schemas=tool_schemas or [],
            include_token_usage_to_model=include_token_usage_to_model,
            imodel=analysis_model or self.chat_model,
            imodel_kw=kwargs,
        )

        action_param = None
        if tools is not None or tool_schemas is not None:
            from lionagi.operations.act.act import _get_default_call_params

            action_param = ActionParam(
                action_call_params=_get_default_call_params(),
                tools=tools or True,
                strategy="concurrent",
                suppress_errors=True,
                verbose_action=False,
            )

        from lionagi.operations.parse.parse import get_default_call

        parse_param = ParseParam(
            response_format=ReActAnalysis,
            fuzzy_match_params=FuzzyMatchKeysParams(),
            handle_validation="return_value",
            alcall_params=get_default_call(),
            imodel=analysis_model or self.chat_model,
            imodel_kw={},
        )

        resp_ctx = response_kwargs or {}
        if response_format:
            resp_ctx["response_format"] = response_format

        async for result in ReActStream(
            self,
            instruction=instruct_dict.get("instruction", str(instruct)),
            chat_param=chat_param,
            action_param=action_param,
            parse_param=parse_param,
            intp_param=intp_param,
            resp_ctx=resp_ctx,
            reasoning_effort=reasoning_effort,
            reason=True,
            field_models=None,
            handle_validation="return_value",
            invoke_actions=True,
            clear_messages=False,
            intermediate_response_options=intermediate_response_options,
            intermediate_listable=intermediate_listable,
            intermediate_nullable=False,
            max_extensions=max_extensions,
            extension_allowed=extension_allowed,
            verbose_analysis=verbose,
            display_as=display_as,
            verbose_length=verbose_length,
            continue_after_failed_response=False,
        ):
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
