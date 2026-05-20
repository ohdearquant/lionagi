# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, JsonValue, PrivateAttr, field_serializer

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
from lionagi.utils import copy

from .prompts import LION_SYSTEM_MESSAGE

if TYPE_CHECKING:
    from lionagi.operations.operate.operative import Operative
    from lionagi.operations.types import Middle


__all__ = ("Branch",)


_DEFAULT_ALCALL_PARAMS = None


class Branch(Element, Relational):
    """
    Manages a conversation 'branch' with messages, tools, and iModels.

    The `Branch` class serves as a high-level interface or orchestrator that:
        - Handles message management (`MessageManager`).
        - Registers and invokes tools/actions (`ActionManager`).
        - Manages model instances (`iModelManager`).
        - Logs activity (`LogManager`).

    **Key responsibilities**:
        - Storing and organizing messages, including system instructions, user instructions, and model responses.
        - Handling asynchronous or synchronous execution of LLM calls and tool invocations.
        - Providing a consistent interface for "operate," "chat," "communicate," "parse," etc.

    Attributes:
        user (SenderRecipient | None):
            The user or "owner" of this branch (often tied to a session).
        name (str | None):
            A human-readable name for this branch.

    Note:
        Actual implementations for chat, parse, operate, etc., are referenced
        via lazy loading or modular imports. You typically won't need to
        subclass `Branch`, but you can instantiate it and call the
        associated methods for complex orchestrations.
    """

    user: SenderRecipient | None = Field(
        None,
        description=(
            "The user or sender of the branch, often a session object or "
            "an external user identifier. Not to be confused with the "
            "LLM API's user parameter."
        ),
    )

    name: str | None = Field(
        None,
        description="A human-readable name of the branch (optional).",
    )

    _message_manager: MessageManager | None = PrivateAttr(None)
    _action_manager: ActionManager | None = PrivateAttr(None)
    _imodel_manager: iModelManager | None = PrivateAttr(None)
    _log_manager: DataLogger | None = PrivateAttr(None)
    _operation_manager: OperationManager | None = PrivateAttr(None)

    def __init__(
        self,
        *,
        user: "SenderRecipient" = None,
        name: str | None = None,
        messages: Pile[RoledMessage] = None,  # message manager kwargs
        system: System | JsonValue = None,
        system_sender: "SenderRecipient" = None,
        chat_model: iModel | dict | str = None,  # iModelManager kwargs
        parse_model: iModel | dict | str = None,
        imodel: iModel = None,  # deprecated, alias of chat_model
        tools: FuncTool | list[FuncTool] = None,  # ActionManager kwargs
        log_config: DataLoggerConfig | dict = None,  # LogManager kwargs
        system_datetime: bool | str = None,
        system_template=None,
        system_template_context: dict = None,
        logs: Pile[Log] = None,
        use_lion_system_message: bool = False,
        **kwargs,
    ):
        """
        Initializes a `Branch` with references to managers and an optional mailbox.

        Args:
            user (SenderRecipient, optional):
                The user or sender context for this branch.
            name (str | None, optional):
                A human-readable name for this branch.
            messages (Pile[RoledMessage], optional):
                Initial messages for seeding the MessageManager.
            system (System | JsonValue, optional):
                Optional system-level configuration or message for the LLM.
            system_sender (SenderRecipient, optional):
                Sender to attribute to the system message if it is added.
            chat_model (iModel, optional):
                The primary "chat" iModel for conversation. If not provided,
                uses default provider and model from settings.
            parse_model (iModel, optional):
                The "parse" iModel for structured data parsing.
                Defaults to chat_model if not provided.
            imodel (iModel, optional):
                Deprecated. Alias for `chat_model`.
            tools (FuncTool | list[FuncTool], optional):
                Tools or a list of tools for the ActionManager.
            log_config (LogManagerConfig | dict, optional):
                Configuration dict or object for the LogManager.
            system_datetime (bool | str, optional):
                Whether to include timestamps in system messages (True/False)
                or a string format for datetime.
            system_template (jinja2.Template | str, optional):
                Optional Jinja2 template for system messages.
            system_template_context (dict, optional):
                Context for rendering the system template.
            logs (Pile[Log], optional):
                Existing logs to seed the LogManager.
            use_lion_system_message (bool, optional):
                If `True`, uses the Lion system message for the branch.
            **kwargs:
                Additional parameters passed to `Element` parent init.
        """
        super().__init__(user=user, name=name, **kwargs)

        # --- MessageManager ---
        from lionagi.protocols.messages.manager import MessageManager

        self._message_manager = MessageManager(messages=messages)

        if any(
            bool(x)
            for x in [
                system,
                system_datetime,
                system_template,
                system_template_context,
                use_lion_system_message,
            ]
        ):
            if use_lion_system_message:
                system = f"Developer Prompt: {str(system)}" if system else ""
                system = (LION_SYSTEM_MESSAGE + "\n\n" + system).strip()

            # Note: system_template and system_template_context are deprecated
            # Template rendering has been removed from the message system
            self._message_manager.add_message(
                system=system,
                system_datetime=system_datetime,
                recipient=self.id,
                sender=system_sender or self.user or MessageRole.SYSTEM,
            )

        chat_model = chat_model or imodel
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

        # --- ActionManager ---
        self._action_manager = ActionManager()
        if tools:
            self.register_tools(tools)

        # --- LogManager ---
        if log_config:
            if isinstance(log_config, dict):
                log_config = DataLoggerConfig(**log_config)
            self._log_manager = DataLogger.from_config(log_config, logs=logs)
        else:
            self._log_manager = DataLogger(**settings.LOG_CONFIG, logs=logs)

        self._operation_manager = OperationManager()

    # -------------------------------------------------------------------------
    # Properties to expose managers and core data
    # -------------------------------------------------------------------------
    @property
    def system(self) -> System | None:
        """The system message/configuration, if any."""
        return self._message_manager.system

    @property
    def on_message_added(self) -> list:
        return self._message_manager._on_message_added

    @property
    def msgs(self) -> MessageManager:
        """Returns the associated MessageManager."""
        return self._message_manager

    @property
    def acts(self) -> ActionManager:
        """Returns the associated ActionManager for tool management."""
        return self._action_manager

    @property
    def mdls(self) -> iModelManager:
        """Returns the associated iModelManager."""
        return self._imodel_manager

    @property
    def progression(self) -> Progression:
        """Active message ordering for LLM calls.

        If metadata contains 'current_progression', uses that (agent-managed
        subset). Otherwise returns the full progression from MessageManager.
        Context tools modify current_progression to evict messages from the
        LLM's view without deleting them from the conversation record.
        """
        cp = self.metadata.get("current_progression")
        if cp is not None:
            return cp
        return self._message_manager.progression

    @property
    def messages(self) -> Pile[RoledMessage]:
        """Convenience property to retrieve all messages from MessageManager."""
        return self._message_manager.messages

    @property
    def token_budget(self):
        """Current token budget: used, limit, remaining, usage_pct.

        Reads context window from the provider's CONTEXT_WINDOWS dict.
        Uses branch.progression (respects evicted messages).
        """
        from lionagi.service.token_budget import get_token_budget

        return get_token_budget(self)

    @property
    def logs(self) -> Pile[Log]:
        """Convenience property to retrieve all logs from the LogManager."""
        return self._log_manager.logs

    @property
    def chat_model(self) -> iModel:
        """
        The primary "chat" model (`iModel`) used for conversational LLM calls.
        """
        return self._imodel_manager.chat

    @chat_model.setter
    def chat_model(self, value: iModel) -> None:
        """
        Sets the primary "chat" model in the iModelManager.

        Args:
            value (iModel): The new chat model to register.
        """
        self._imodel_manager.register_imodel("chat", value)

    @property
    def parse_model(self) -> iModel:
        """The "parse" model (`iModel`) used for structured data parsing."""
        return self._imodel_manager.parse

    @parse_model.setter
    def parse_model(self, value: iModel) -> None:
        """
        Sets the "parse" model in the iModelManager.

        Args:
            value (iModel): The new parse model to register.
        """
        self._imodel_manager.register_imodel("parse", value)

    @property
    def tools(self) -> dict[str, Tool]:
        """
        All registered tools (actions) in the ActionManager,
        keyed by their tool names or IDs.
        """
        return self._action_manager.registry

    def get_operation(self, operation: str) -> Callable | None:
        if hasattr(self, operation):
            return getattr(self, operation)
        return self._operation_manager.registry.get(operation)

    # -------------------------------------------------------------------------
    # Cloning
    # -------------------------------------------------------------------------
    async def aclone(self, sender: ID.Ref = None) -> "Branch":
        """
        Asynchronously clones this `Branch` with optional new sender ID.

        Args:
            sender (ID.Ref, optional):
                If provided, this ID is set as the sender for all cloned messages.

        Returns:
            Branch: A new branch instance, containing cloned state.
        """
        async with self.msgs.messages:
            return self.clone(sender)

    def clone(self, sender: ID.Ref = None) -> "Branch":
        """
        Clones this `Branch` synchronously, optionally updating the sender ID.

        Args:
            sender (ID.Ref, optional):
                If provided, all messages in the clone will have this sender ID.
                Otherwise, uses the current branch's ID.

        Raises:
            ValueError: If `sender` is not a valid ID.Ref.

        Returns:
            Branch: A new branch object with a copy of the messages, system info, etc.
        """
        if sender is not None:
            if not ID.is_id(sender):
                raise ValueError(
                    f"Cannot clone Branch: '{sender}' is not a valid sender ID."
                )
            sender = ID.get_id(sender)

        system = self.msgs.system.clone() if self.msgs.system else None
        tools = (
            list(self._action_manager.registry.values())
            if self._action_manager.registry
            else None
        )
        # Transfer iModels: CLI endpoints get a fresh copy, API endpoints share
        chat_model = (
            self.chat_model.copy() if self.chat_model.is_cli else self.chat_model
        )
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

    def register_tools(
        self, tools: FuncTool | list[FuncTool] | LionTool, update: bool = False
    ):
        """
        Registers one or more tools in the ActionManager.

        Args:
            tools (FuncTool | list[FuncTool] | LionTool):
                A single tool or a list of tools to register.
            update (bool, optional):
                If `True`, updates existing tools with the same name.
        """
        tools = [tools] if not isinstance(tools, list) else tools
        for tool in tools:
            self._register_tool(tool, update=update)

    @field_serializer("user")
    def _serialize_user(self, v):
        return str(v) if v else None

    # -------------------------------------------------------------------------
    # Conversion / Serialization
    # -------------------------------------------------------------------------
    def to_df(self, *, progression: Progression = None):
        """
        Convert branch messages into a `pandas.DataFrame`.

        Args:
            progression (Progression, optional):
                A custom message ordering. If `None`, uses the stored progression.

        Returns:
            pd.DataFrame: Each row represents a message, with columns defined by MESSAGE_FIELDS.
        """
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
            """connect to an api endpoint"""
            api_call = await imodel.invoke(**kwargs)
            self._log_manager.log(api_call)
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
        if "clone_from" in v:
            v["clone_from"] = {
                "id": str(v["clone_from"].id),
                "user": str(v["clone_from"].user),
                "created_at": v["clone_from"].created_at,
                "progression": [str(i) for i in v["clone_from"].msgs.progression],
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
        if self.messages:
            dict_["messages"] = self.messages.to_dict(mode=mode)
        if include_logs and self.logs:
            dict_["logs"] = self.logs.to_dict(mode=mode)
        if self.system:
            dict_["system"] = self.system.to_dict(mode=mode)
        if include_log_config:
            dict_["log_config"] = self._log_manager._config.model_dump()
        dict_["chat_model"] = self.chat_model.to_dict(
            include_request_options, include_processor_config
        )
        if self.parse_model is not self.chat_model:
            dict_["parse_model"] = self.parse_model.to_dict(
                include_request_options, include_processor_config
            )
        return dict_

    @classmethod
    def from_dict(cls, data: dict):
        """
        Creates a `Branch` instance from a serialized dictionary.

        Args:
            data (dict):
                Must include (or optionally include) `messages`, `logs`,
                `chat_model`, `parse_model`, `system`, and `log_config`.

        Returns:
            Branch: A new `Branch` instance based on the deserialized data.
        """
        dict_ = {
            "messages": data.pop("messages", Unset),
            "logs": data.pop("logs", Unset),
            "chat_model": data.pop("chat_model", Unset),
            "parse_model": data.pop("parse_model", Unset),
            "system": data.pop("system", Unset),
            "log_config": data.pop("log_config", Unset),
        }

        # When restoring from serialized state, the system message is already
        # in the messages pile. Don't pass it again to __init__ or it'll duplicate.
        messages_val = dict_.get("messages", Unset)
        system_val = dict_.get("system", Unset)
        if messages_val is not Unset and system_val is not Unset:
            dict_["system"] = Unset  # already in messages, skip re-adding

        params = {}

        # Merge in the rest of the data
        for k, v in data.items():
            # If the item is a dict with an 'id', we expand it
            if isinstance(v, dict) and "id" in v:
                params.update(v)
            else:
                params[k] = v

        params.update(dict_)
        # Remove placeholders (Unset) so we don't incorrectly assign them
        return cls(**{k: v for k, v in params.items() if v is not Unset})

    def dump_logs(self, clear: bool = True, persist_path=None):
        """
        Dumps the log to a file or clears it.

        Args:
            clear (bool, optional):
                If `True`, clears the log after dumping.
            persist_path (str, optional):
                The file path to save the log to.
        """
        self._log_manager.dump(clear=clear, persist_path=persist_path)

    async def adump_logs(self, clear: bool = True, persist_path=None):
        """
        Asynchronously dumps the log to a file or clears it.
        """
        await self._log_manager.adump(clear=clear, persist_path=persist_path)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._log_manager.adump(clear=True)

    # -------------------------------------------------------------------------
    # Asynchronous Operations (chat, parse, operate, etc.)
    # -------------------------------------------------------------------------
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
        """
        Invokes the chat model with the current conversation history. This method does not
        automatically add messages to the branch. It is typically used for orchestrating.

        **High-level flow**:
            1. Construct a sequence of messages from the stored progression.
            2. Integrate any pending action responses into the context.
            3. Invoke the chat model with the combined messages.
            4. Capture and return the final response as an `AssistantResponse`.

        Args:
            instruction (Any):
                Main user instruction text or structured data.
            guidance (Any):
                Additional system or user guidance text.
            context (Any):
                Context data to pass to the model.
            sender (Any):
                The user or entity sending this message (defaults to `Branch.user`).
            recipient (Any):
                The recipient of this message (defaults to `self.id`).
            request_fields (Any):
                Partial field-level validation reference (rarely used).
            response_format (type[BaseModel], optional):
                A Pydantic model type for structured model responses.
            progression (Any):
                Custom ordering of messages in the conversation.
            imodel (iModel, optional):
                An override for the chat model to use. If not provided, uses `self.chat_model`.
            tool_schemas (Any, optional):
                Additional schemas for tool invocation in function-calling.
            images (list, optional):
                Optional images relevant to the model's context.
            image_detail (Literal["low", "high", "auto"], optional):
                Level of detail for image-based context (if relevant).
            plain_content (str, optional):
                Plain text content, will override any other content.
            return_ins_res_message:
                If `True`, returns the final `Instruction` and `AssistantResponse` objects.
                else, returns only the response content.
            **kwargs:
                Additional parameters for the LLM invocation.

        Returns:
            tuple[Instruction, AssistantResponse]:
                The `Instruction` object and the final `AssistantResponse`.
        """
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
        handle_validation: Literal[
            "raise", "return_value", "return_none"
        ] = "return_value",
        max_retries: int = 3,
        request_type: type[BaseModel] = None,
        operative: "Operative" = None,
        similarity_algo="jaro_winkler",
        similarity_threshold: float = 0.85,
        fuzzy_match: bool = True,
        handle_unmatched: Literal[
            "ignore", "raise", "remove", "fill", "force"
        ] = "force",
        fill_value: Any = None,
        fill_mapping: dict[str, Any] | None = None,
        strict: bool = False,
        suppress_conversion_errors: bool = False,
        response_format: type[BaseModel] = None,
    ) -> BaseModel | dict | str | None:
        """
        Attempts to parse text into a structured Pydantic model using parse model logic. New messages are not appeneded to conversation context.

        If fuzzy matching is enabled, tries to map partial or uncertain keys
        to the known fields of the model. Retries are performed if initial parsing fails.

        Args:
            text (str):
                The raw text to parse.
            handle_validation (Literal["raise","return_value","return_none"]):
                What to do if parsing fails (default: "return_value").
            max_retries (int):
                Number of times to retry parsing on failure (default: 3).
            request_type (type[BaseModel], optional):
                The Pydantic model to parse into.
            operative (Operative, optional):
                An `Operative` object with known request model and settings.
            similarity_algo (str):
                Algorithm name for fuzzy field matching.
            similarity_threshold (float):
                Threshold for matching (0.0 - 1.0).
            fuzzy_match (bool):
                Whether to attempt fuzzy matching for unmatched fields.
            handle_unmatched (Literal["ignore","raise","remove","fill","force"]):
                Policy for unrecognized fields (default: "force").
            fill_value (Any):
                Default placeholder for missing fields (if fill is used).
            fill_mapping (dict[str, Any] | None):
                A mapping of specific fields to fill values.
            strict (bool):
                If True, raises errors on ambiguous fields or data types.
            suppress_conversion_errors (bool):
                If True, logs or ignores conversion errors instead of raising.

        Returns:
            BaseModel | dict | str | None:
                Parsed model instance, or a fallback based on `handle_validation`.
        """

        _pms = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "_pms") and v is not None
        }
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
        handle_validation: Literal[
            "raise", "return_value", "return_none"
        ] = "return_value",
        include_token_usage_to_model: bool = False,
        stream_persist: bool = False,
        persist_dir: str | None = None,
        middle: "Middle | None" = None,
        **kwargs,
    ) -> list | BaseModel | None | dict | str:
        """
        Orchestrates an "operate" flow with optional tool invocation and
        structured response validation. Messages **are** automatically
        added to the conversation.

        CLI endpoints (``chat_model.is_cli``) stream via ``run()`` and
        accumulate text; API endpoints round-trip through ``communicate()``.
        Pass ``middle`` to override the dispatch explicitly, or use
        ``stream_persist``/``persist_dir`` for JSONL chunk logging on CLI runs.

        **Workflow**:
        1) Builds or updates an `Operative` object to specify how the LLM should respond.
        2) Sends an instruction (`instruct`) or direct `instruction` text to `branch.chat()`.
        3) Optionally validates/parses the result into a model or dictionary.
        4) If `invoke_actions=True`, any requested tool calls are automatically invoked.
        5) Returns either the final structure, raw response, or an updated `Operative`.

        Args:
            branch (Branch):
                The active branch that orchestrates messages, models, and logs.
            instruct (Instruct, optional):
                Contains the instruction, guidance, context, etc. If not provided,
                uses `instruction`, `guidance`, `context` directly.
            instruction (Instruction | JsonValue, optional):
                The main user instruction or content for the LLM.
            guidance (JsonValue, optional):
                Additional system or user instructions.
            context (JsonValue, optional):
                Extra context data.
            sender (SenderRecipient, optional):
                The sender ID for newly added messages.
            recipient (SenderRecipient, optional):
                The recipient ID for newly added messages.
            progression (Progression, optional):
                Custom ordering of conversation messages.

            chat_model (iModel, optional):
                The LLM used for the main chat operation. Defaults to `branch.chat_model`.
            invoke_actions (bool, optional):
                If `True`, executes any requested tools found in the LLM's response.
            tool_schemas (list[dict], optional):
                Additional schema definitions for tool-based function-calling.
            images (list, optional):
                Optional images appended to the LLM context.
            image_detail (Literal["low","high","auto"], optional):
                The level of image detail, if relevant.
            parse_model (iModel, optional):
                Model used for deeper or specialized parsing, if needed.
            skip_validation (bool, optional):
                If `True`, bypasses final validation and returns raw text or partial structure.
            tools (ToolRef, optional):
                Tools to be registered or made available if `invoke_actions` is True.
            operative (Operative, optional):
                If provided, reuses an existing operative's config for parsing/validation.
            response_format (type[BaseModel], optional):
                Expected Pydantic model for the final response (alias for `operative.request_type`).
                rather than the structured or raw output.
            actions (bool, optional):
                If `True`, signals that function-calling or "action" usage is expected.
            reason (bool, optional):
                If `True`, signals that the LLM should provide chain-of-thought or reasoning (where applicable).
            action_strategy (Literal["sequential","concurrent"], optional):
                The strategy for invoking tools (default: "concurrent").
            verbose_action (bool, optional):
                If `True`, logs detailed information about tool invocation.
            field_models (list[FieldModel] | None, optional):
                Field-level definitions or overrides for the model schema.
            exclude_fields (list|dict|None, optional):
                Which fields to exclude from final validation or model building.
            handle_validation (Literal["raise","return_value","return_none"], optional):
                How to handle parsing failures (default: "return_value").
            include_token_usage_to_model:
                If `True`, includes token usage in the model messages.
            **kwargs:
                Additional keyword arguments passed to the LLM via `branch.chat()`.

        Returns:
            list | BaseModel | None | dict | str:
                - The parsed or raw response from the LLM,
                - `None` if validation fails and `handle_validation='return_none'`,
                - or the entire `Operative` object if `return_operative=True`.

        Raises:
            ValueError:
                - If both `operative_model` and `response_format` or `request_model` are given.
                - If the LLM's response cannot be parsed into the expected format and `handle_validation='raise'`.
        """
        _pms = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "_pms") and v is not None
        }
        from lionagi.operations.operate.operate import operate, prepare_operate_kw

        return await operate(self, **prepare_operate_kw(self, **_pms))

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
        """
        A simpler orchestration than `operate()`, typically without tool invocation. Messages are automatically added to the conversation.

        **Flow**:
          1. Sends an instruction (or conversation) to the chat model.
          2. Optionally parses the response into a structured model or fields.
          3. Returns either the raw string, the parsed model, or a dict of fields.

        Args:
            instruction (Instruction | dict, optional):
                The user's main query or data.
            guidance (JsonValue, optional):
                Additional instructions or context for the LLM.
            context (JsonValue, optional):
                Extra data or context.
            plain_content (str, optional):
                Plain text content appended to the instruction.
            sender (SenderRecipient, optional):
                Sender ID (defaults to `Branch.user`).
            recipient (SenderRecipient, optional):
                Recipient ID (defaults to `self.id`).
            progression (ID.IDSeq, optional):
                Custom ordering of messages.
            response_format (type[BaseModel], optional):
                Alias for `request_model`. If both are provided, raises ValueError.
            request_fields (dict|list[str], optional):
                If you only need certain fields from the LLM's response.
            chat_model (iModel, optional):
                An alternative to the default chat model.
            parse_model (iModel, optional):
                If parsing is needed, you can override the default parse model.
            skip_validation (bool, optional):
                If True, returns the raw response string unvalidated.
            images (list, optional):
                Any relevant images.
            image_detail (Literal["low","high","auto"], optional):
                Image detail level (if used).
            num_parse_retries (int, optional):
                Maximum parsing retries (capped at 5).
            clear_messages (bool, optional):
                Whether to clear stored messages before sending.
            **kwargs:
                Additional arguments for the underlying LLM call.

        Returns:
            Any:
                - Raw string (if `skip_validation=True`),
                - A validated Pydantic model,
                - A dict of the requested fields,
                - or `None` if parsing fails and `handle_validation='return_none'`.
        """
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

        return await communicate(self, **prepare_communicate_kw(self, **_pms))

    async def act(
        self,
        action_request: list | ActionRequest | BaseModel | dict,
        *,
        strategy: Literal["concurrent", "sequential"] = "concurrent",
        verbose_action: bool = False,
        suppress_errors: bool = True,
        call_params: AlcallParams = None,
    ) -> list[ActionResponse]:
        _pms = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "_pms") and v is not None
        }
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
        """
        Interprets (rewrites) a user's raw input into a more formal or structured
        LLM prompt. This function can be seen as a "prompt translator," which
        ensures the user's original query is clarified or enhanced for better
        LLM responses. Messages are not automatically added to the conversation.

        The method calls `branch.chat()` behind the scenes with a system prompt
        that instructs the LLM to rewrite the input. You can provide additional
        parameters in `**kwargs` (e.g., `parse_model`, `skip_validation`, etc.)
        if you want to shape how the rewriting is done.

        Args:
            branch (Branch):
                The active branch context for messages, logging, etc.
            text (str):
                The raw user input or question that needs interpreting.
            domain (str | None, optional):
                Optional domain hint (e.g. "finance", "marketing", "devops").
                The LLM can use this hint to tailor its rewriting approach.
            style (str | None, optional):
                Optional style hint (e.g. "concise", "detailed").
            **kwargs:
                Additional arguments passed to `branch.communicate()`,
                such as `parse_model`, `skip_validation`, `temperature`, etc.

        Returns:
            str:
                A refined or "improved" user prompt string, suitable for feeding
                back into the LLM as a clearer instruction.

        Example:
            refined = await interpret(
                branch=my_branch, text="How do I do marketing analytics?",
                domain="marketing", style="detailed"
            )
            # refined might be "Explain step-by-step how to set up a marketing analytics
            #  pipeline to track campaign performance..."
        """

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

    async def ReAct(
        self,
        instruct: "Instruct | dict[str, Any]",
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
        """
        Performs a multi-step "ReAct" flow (inspired by the ReAct paradigm in LLM usage),
        which may include:
        1) Optionally interpreting the user's original instructions via `branch.interpret()`.
        2) Generating chain-of-thought analysis or reasoning using a specialized schema (`ReActAnalysis`).
        3) Optionally expanding the conversation multiple times if the analysis indicates more steps (extensions).
        4) Producing a final answer by invoking the branch's `instruct()` method.

        Args:
            branch (Branch):
                The active branch context that orchestrates messages, models, and actions.
            instruct (Instruct | dict[str, Any]):
                The user's instruction object or a dict with equivalent keys.
            interpret (bool, optional):
                If `True`, first interprets (`branch.interpret`) the instructions to refine them
                before proceeding. Defaults to `False`.
            interpret_domain (str | None, optional):
                Optional domain hint for the interpretation step.
            interpret_style (str | None, optional):
                Optional style hint for the interpretation step.
            interpret_sample (str | None, optional):
                Optional sample hint for the interpretation step.
            interpret_kwargs (dict | None, optional):
                Additional arguments for the interpretation step.
            tools (Any, optional):
                Tools to be made available for the ReAct process. If omitted or `None`,
                and if no `tool_schemas` are provided, it defaults to `True` (all tools).
            tool_schemas (Any, optional):
                Additional or custom schemas for tools if function calling is needed.
            response_format (type[BaseModel], optional):
                The final schema for the user-facing output after the ReAct expansions.
                If `None`, the output is raw text or an unstructured response.
            extension_allowed (bool, optional):
                Whether to allow multiple expansions if the analysis indicates more steps.
                Defaults to `False`.
            max_extensions (int | None, optional):
                The max number of expansions if `extension_allowed` is `True`.
                If omitted, no upper limit is enforced (other than logic).
            response_kwargs (dict | None, optional):
                Extra kwargs passed into the final `_instruct()` call that produces the
                final output. Defaults to `None`.
            return_analysis (bool, optional):
                If `True`, returns both the final output and the list of analysis objects
                produced throughout expansions. Defaults to `False`.
            analysis_model (iModel | None, optional):
                A custom LLM model for generating the ReAct analysis steps. If `None`,
                uses the branch's default `chat_model`.
            include_token_usage_to_model:
                If `True`, includes token usage in the model messages.
            verbose (bool):
                If `True`, logs detailed information about the process.
            verbose_length (int):
                If `verbose=True`, limits the length of logged strings to this value.
            **kwargs:
                Additional keyword arguments passed into the initial `branch.operate()` call.

        Returns:
            Any | tuple[Any, list]:
                - If `return_analysis=False`, returns only the final output (which may be
                a raw string, dict, or structured model depending on `response_format`).
                - If `return_analysis=True`, returns a tuple of (final_output, list_of_analyses).
                The list_of_analyses is a sequence of the intermediate or extended
                ReActAnalysis objects.

        Notes:
            - Messages are automatically added to the branch context during the ReAct process.
            - If `max_extensions` is greater than 5, a warning is logged, and it is set to 5.
            - If `interpret=True`, the user instruction is replaced by the interpreted
            string before proceeding.
            - The expansions loop continues until either `analysis.extension_needed` is `False`
            or `extensions` (the remaining allowed expansions) is `0`.
        """
        from lionagi.operations.ReAct.ReAct import ReAct

        # Remove potential duplicate parameters from kwargs
        kwargs_filtered = {
            k: v
            for k, v in kwargs.items()
            if k not in {"verbose_analysis", "verbose_action"}
        }

        return await ReAct(
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

    async def ReActStream(
        self,
        instruct: "Instruct | dict[str, Any]",
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

        # Convert Instruct to dict if needed
        instruct_dict = (
            instruct.to_dict() if isinstance(instruct, Instruct) else dict(instruct)
        )

        # Build InterpretContext if interpretation requested
        intp_param = None
        if interpret:
            intp_param = InterpretParam(
                domain=interpret_domain or "general",
                style=interpret_style or "concise",
                sample_writing=interpret_sample or "",
                imodel=interpret_model or analysis_model or self.chat_model,
                imodel_kw=interpret_kwargs or {},
            )

        # Build ChatContext
        chat_param = ChatParam(
            guidance=instruct_dict.get("guidance"),
            context=instruct_dict.get("context"),
            sender=self.user or "user",
            recipient=self.id,
            response_format=None,
            progression=None,
            tool_schemas=tool_schemas or [],
            images=[],
            image_detail="auto",
            plain_content="",
            include_token_usage_to_model=include_token_usage_to_model,
            imodel=analysis_model or self.chat_model,
            imodel_kw=kwargs,
        )

        # Build ActionContext
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

        # Build ParseContext
        from lionagi.operations.parse.parse import get_default_call

        parse_param = ParseParam(
            response_format=ReActAnalysis,
            fuzzy_match_params=FuzzyMatchKeysParams(),
            handle_validation="return_value",
            alcall_params=get_default_call(),
            imodel=analysis_model or self.chat_model,
            imodel_kw={},
        )

        # Response context for final answer
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
        """Stream Messages from a CLI endpoint.

        Yields Instruction, AssistantResponse, ActionRequest, and
        ActionResponse messages as they arrive from the stream.

        Pass ``chat_model`` to override the branch default, enabling
        multi-model conversations on a single branch.

        Set ``stream_persist=True`` to enable real-time JSONL logging
        of each message as it arrives, with branch state consolidated
        on exit (including timeout/interrupt).
        """
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


# File: lionagi/session/branch.py
