# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from lionagi.hooks.bus import HookBus

    from .observer import SessionObserver

from pydantic import Field, JsonValue, PrivateAttr, field_serializer, model_validator
from typing_extensions import Self

from lionagi.protocols.types import (
    ID,
    MESSAGE_FIELDS,
    Graph,
    Node,
    Pile,
    Progression,
    Relational,
    RoledMessage,
    SenderRecipient,
    System,
)

from .._errors import ItemNotFoundError
from ..ln import lcall
from ..protocols.generic import Flow
from ..protocols.messages import Message
from .branch import Branch, OperationManager, Tool
from .exchange import Exchange


class Session(Node, Relational):
    """Multi-branch conversation session with exchange, observer, and hook bus."""

    branches: Pile[Branch] = Field(
        default_factory=lambda: Pile(item_type={Branch}, strict_type=False)
    )
    exchange: Exchange = Field(default_factory=Exchange, exclude=True)
    default_branch: Any = Field(default=None, exclude=True)
    name: str = Field(default="Session")
    user: SenderRecipient | None = None
    _operation_manager: OperationManager = PrivateAttr(default_factory=OperationManager)
    _observer: Any = PrivateAttr(default=None)
    _hooks: Any = PrivateAttr(default=None)

    @field_serializer("user")
    def _serialize_user(self, value: SenderRecipient | None) -> JsonValue:
        if value is None:
            return None
        return str(value)

    async def ainclude_branches(self, branches: ID[Branch].ItemSeq):
        async with self.branches:
            self.include_branches(branches)

    def include_branches(self, branches: ID[Branch].ItemSeq):
        def _take_in_branch(branch: Branch):
            if branch not in self.branches:
                self.branches.include(branch)

            branch.user = self.id
            branch._operation_manager = self._operation_manager
            branch._observer = self.observer
            if self._hooks is not None:
                branch._hooks = self._hooks
            if not self.exchange.has(branch.id):
                self.exchange.register(branch.id)
            if self.default_branch is None:
                self.default_branch = branch

        branches = [branches] if isinstance(branches, Branch) else branches

        for i in branches:
            _take_in_branch(i)

    def register_operation(self, operation: str, func: Callable, *, update: bool = False):
        self._operation_manager.register(operation, func, update=update)

    def operation(self, name: str = None, *, update: bool = False):
        """Decorator to register a function as a named operation."""

        def decorator(func: Callable) -> Callable:
            operation_name = name if name is not None else func.__name__
            self.register_operation(operation_name, func, update=update)
            return func

        return decorator

    async def run_operation(
        self, operation: str, *, branch: Branch | ID.Ref | None = None, **kwargs: Any
    ) -> Any:
        """Invoke a registered or built-in branch operation by name."""
        b = branch or self.default_branch
        if isinstance(b, str | UUID):
            b = self.branches[b]
        meth = b.get_operation(operation)
        if meth is None:
            raise ValueError(f"Unknown operation: {operation!r}")
        return await meth(**kwargs)

    @property
    def hooks(self) -> "HookBus":
        """Lazily-created per-session hook bus."""
        if self._hooks is None:
            from lionagi.hooks import build_session_bus

            self._hooks = build_session_bus(observer=self.observer)
        return self._hooks

    @property
    def observer(self) -> "SessionObserver":
        """Lazily-created reactive event dispatcher."""
        if self._observer is None:
            from .observer import SessionObserver

            self._observer = SessionObserver(session=self)
        return self._observer

    def observe(
        self,
        *keys: type | Callable | Any,
        handler: Callable | None = None,
        role: str | None = None,
    ) -> Any:
        """Subscribe a handler to AND-composed conditions. Usable as a decorator."""
        return self.observer.observe(*keys, handler=handler, role=role)

    def route(self, condition: Callable, *, into: str) -> "SessionObserver":
        return self.observer.route(condition, into=into)

    def gate(self, check: Callable) -> "SessionObserver":
        """Set the governance gate consulted before events are dispatched."""
        return self.observer.gate(check)

    async def emit(self, event: Any) -> list[Any]:
        return await self.observer.emit(event)

    async def authorize(self, action: Any) -> bool:
        """Pre-invoke governance gate. Allows when no gate is set."""
        return await self.observer.authorize(action)

    @model_validator(mode="after")
    def _initialize_branches(self) -> Self:
        if self.default_branch is None:
            self.default_branch = Branch()
        if self.default_branch not in self.branches:
            self.branches.include(self.default_branch)
        if self.branches:
            self.include_branches(self.branches)
        return self

    def _lookup_branch_by_name(self, name: str) -> Branch | None:
        for branch in self.branches:
            if branch.name == name:
                return branch
        return None

    def get_branch(self, branch: ID.Ref | str, default: Any = ..., /) -> Branch:
        with contextlib.suppress(ItemNotFoundError, ValueError):
            id_ = ID.get_id(branch)
            return self.branches[id_]

        if isinstance(branch, str):
            if b := self._lookup_branch_by_name(branch):
                return b

        if default is ...:
            raise ItemNotFoundError(f"Branch '{branch}' not found.")
        return default

    def new_branch(
        self,
        system: System | JsonValue = None,
        system_sender: SenderRecipient = None,
        system_datetime: bool | str = None,
        user: SenderRecipient = None,
        name: str | None = None,
        messages: Pile[RoledMessage] = None,
        tools: Tool | Callable | list = None,
        as_default_branch: bool = False,
        **kwargs,
    ) -> Branch:
        params = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "as_default_branch", "kwargs") and v is not None
        }
        branch = Branch(**params, **kwargs)  # type: ignore
        self.include_branches(branch)
        if as_default_branch:
            self.default_branch = branch
        return branch

    def remove_branch(
        self,
        branch: ID.Ref,
        delete: bool = False,
    ):
        branch = ID.get_id(branch)

        if branch not in self.branches:
            _s = str(branch) if len(str(branch)) < 10 else str(branch)[:10] + "..."
            raise ItemNotFoundError(f"Branch {_s}.. does not exist.")
        branch: Branch = self.branches[branch]

        self.branches.exclude(branch)
        self.exchange.unregister(branch.id)

        if self.default_branch.id == branch.id:
            if not self.branches:
                self.default_branch = None
            else:
                self.default_branch = self.branches[0]

        if delete:
            del branch

    async def asplit(self, branch: ID.Ref) -> Branch:
        async with self.branches:
            return self.split(branch)

    def split(self, branch: ID.Ref) -> Branch:
        branch: Branch = self.branches[branch]
        branch_clone = branch.clone(sender=self.id)
        self.include_branches(branch_clone)
        return branch_clone

    def change_default_branch(self, branch: ID.Ref):
        branch = self.branches[branch]
        if not isinstance(branch, Branch):
            raise ValueError("Input value for branch is not a valid branch.")
        self.default_branch = branch

    def register_participant(self, entity_id: UUID) -> Flow[Message, Progression]:
        return self.exchange.register(entity_id)

    def send(
        self,
        sender: UUID,
        recipient: UUID | None,
        content: Any,
        channel: str | None = None,
    ) -> Message:
        return self.exchange.send(sender, recipient, content, channel)

    def receive(self, owner_id: UUID, sender: UUID | None = None) -> list[Message]:
        return self.exchange.receive(owner_id, sender)

    def pop_message(self, owner_id: UUID, sender: UUID) -> Message | None:
        return self.exchange.pop_message(owner_id, sender)

    async def collect(self, owner_id: UUID) -> int:
        return await self.exchange.collect(owner_id)

    async def sync(self) -> int:
        return await self.exchange.sync()

    def to_df(
        self,
        branches: ID.RefSeq = None,
        exclude_clone: bool = False,
        exclude_load: bool = False,
    ):
        out = self.concat_messages(
            branches=branches,
            exclude_clone=exclude_clone,
            exclude_load=exclude_load,
        )
        return out.to_df(columns=MESSAGE_FIELDS)

    def concat_messages(
        self,
        branches: ID.RefSeq = None,
        exclude_clone: bool = False,
        exclude_load: bool = False,
    ) -> Pile[RoledMessage]:
        if not branches:
            branches = self.branches

        if any(i not in self.branches for i in branches):
            raise ValueError("Branch does not exist.")

        messages = lcall(
            branches,
            lambda x: list(self.branches[x].messages),
            input_unique=True,
            input_flatten=True,
            input_dropna=True,
            output_flatten=True,
            output_unique=True,
        )
        return Pile(collections=messages, item_type={RoledMessage}, strict_type=False)

    async def flow(
        self,
        graph: Graph,
        *,
        context: dict[str, Any] | None = None,
        parallel: bool = True,
        max_concurrent: int = 5,
        verbose: bool = False,
        default_branch: Branch | ID.Ref | None = None,
        alcall_params: Any = None,
        on_progress: Any = None,
        reactive: bool = False,
        spawn_type: type | None = None,
        node_builder: Any = None,
        max_spawn: int = 50,
        executor_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a graph-based DAG workflow, optionally reactive (self-expanding)."""
        from lionagi.operations.flow import flow

        branch = default_branch or self.default_branch
        if isinstance(branch, str | UUID):
            branch = self.branches[branch]

        return await flow(
            session=self,
            graph=graph,
            branch=branch,
            context=context,
            parallel=parallel,
            max_concurrent=max_concurrent,
            verbose=verbose,
            alcall_params=alcall_params,
            on_progress=on_progress,
            reactive=reactive,
            spawn_type=spawn_type,
            node_builder=node_builder,
            max_spawn=max_spawn,
            executor_ref=executor_ref,
        )

    async def flow_stream(
        self,
        graph: Graph,
        *,
        context: dict[str, Any] | None = None,
        max_concurrent: int = 5,
        verbose: bool = False,
        default_branch: Branch | ID.Ref | None = None,
        alcall_params: Any = None,
        spawn_type: type | None = None,
        node_builder: Any = None,
        max_spawn: int = 50,
    ):
        """Stream graph execution, yielding a FlowEvent per completed op."""
        from lionagi.operations.flow import flow_stream

        branch = default_branch or self.default_branch
        if isinstance(branch, str | UUID):
            branch = self.branches[branch]

        async for event in flow_stream(
            session=self,
            graph=graph,
            branch=branch,
            context=context,
            max_concurrent=max_concurrent,
            verbose=verbose,
            alcall_params=alcall_params,
            spawn_type=spawn_type,
            node_builder=node_builder,
            max_spawn=max_spawn,
        ):
            yield event


__all__ = ("Session",)
