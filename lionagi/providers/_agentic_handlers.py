# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel

from lionagi.service.connections.endpoint_config import RUNTIME_STATE_NAMES
from lionagi.utils import to_dict

__all__ = ("RUNTIME_STATE_NAMES", "AgenticHandlersMixin")

# RUNTIME_STATE_NAMES is re-exported, not redefined. It is declared beside the
# config that must not serialize these values, because that model is what every
# route to a written-down config passes through; a second list here would be a
# second thing to keep in step, and the one that fell behind would be the one
# deciding what gets written down.


class AgenticHandlersMixin:
    _handler_params: ClassVar[tuple[str, ...]] = ()
    _handler_kwarg: ClassVar[str] = ""
    _request_model: ClassVar[type[BaseModel] | None] = None
    _filter_model_fields: ClassVar[bool] = True
    # Fields excluded from the request model's dump that nonetheless have to
    # survive create_payload's rebuild. See _carried_runtime_state.
    _runtime_state_fields: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def take_supplied_runtime_state(cls, config, kwargs: dict) -> dict:
        """Lift the declared runtime values off the caller's OWN objects,
        before ``Endpoint.__init__`` deep-copies the supplied config and
        silently rebinds any callback to a copy of its receiver.
        See docs/internals/providers.md#runtime-only-endpoint-state-kept-out-of-serialization.
        """
        taken: dict[str, object] = {}
        source = getattr(config, "kwargs", None)
        if isinstance(config, dict):
            source = config
        if isinstance(source, dict):
            for name in cls._runtime_state_fields:
                if name in source:
                    taken[name] = source[name]
        for name in cls._runtime_state_fields:
            if name in kwargs:
                taken[name] = kwargs[name]
        return taken

    def adopt_runtime_state(self, kwargs: dict) -> tuple[str, ...]:
        """Take declared runtime values onto an endpoint already built.

        For a caller handing over a finished ``Endpoint`` instance rather than
        a config, who has missed the constructor's lift-before-copy window.
        Writes onto the supplied instance, not a copy — matching ``provider``
        and ``base_url`` on the same branch. ``None`` here means absence, not
        a value, and must not erase existing state. Returns the names it
        could not place, so the caller can refuse rather than let them
        evaporate silently.
        """
        placed = set()
        for name in self._runtime_state_fields:
            if kwargs.get(name) is not None:
                self._runtime_state[name] = kwargs[name]
                placed.add(name)
        return tuple(
            n for n in RUNTIME_STATE_NAMES if kwargs.get(n) is not None and n not in placed
        )

    def _init_handlers(self, handlers: dict | None = None, supplied: dict | None = None) -> None:
        config_handlers = self.config.kwargs.pop(self._handler_kwarg, None)
        self._handlers: dict[str, Callable | None] = {k: None for k in self._handler_params}
        if config_handlers is not None:
            self._validate_handlers(config_handlers)
            self._handlers.update(config_handlers)
        if handlers is not None:
            self._validate_handlers(handlers)
            self._handlers.update(handlers)
        # Called from here so every endpoint that initialises handlers gets it,
        # rather than from four constructors where the fifth would be missed.
        self._init_runtime_state(supplied)

    def _init_runtime_state(self, supplied: dict | None = None) -> None:
        """Move declared runtime state out of the serializable endpoint config.

        ``EndpointConfig.kwargs`` reaches ``iModel.to_dict``, ``Branch.to_dict``,
        and the run snapshots written to disk, so a child environment or
        callback left there is a credential or a live object about to be
        JSON-encoded. Holding it in memory here instead keeps the same
        configuration route working; ``copy_runtime_state_to`` (used by
        ``iModel.copy``) copies it shallowly for the same reason.
        See docs/internals/providers.md#runtime-only-endpoint-state-kept-out-of-serialization.
        """
        self._runtime_state: dict[str, object] = {}
        self.drain_runtime_state()
        # Values taken off the caller's own objects win over what the pop just
        # produced: everything in config.kwargs came through a deep copy, and
        # for a bound callback that copy is a different receiver.
        if supplied:
            self._runtime_state.update(supplied)

    def drain_runtime_state(self) -> None:
        """Move declared runtime values out of the serializable config.

        Called wherever the config could be re-populated after construction
        (``EndpointConfig.update()``, ``iModel.from_dict()``) and again before
        serialization — not on the ``create_payload`` read path, since a value
        still sitting in ``config.kwargs`` there works fine unread; writing it
        down is what makes a credential durable, so that's where draining
        belongs.
        """
        for name in self._runtime_state_fields:
            if name in self.config.kwargs:
                self._runtime_state[name] = self.config.kwargs.pop(name)

    def to_dict(self, **kwargs):
        """Drain before serializing, so a child environment can't reach a run
        snapshot as a credential in a saved file."""
        self.drain_runtime_state()
        return super().to_dict(**kwargs)

    def _validate_handlers(self, handlers: dict[str, Callable | None], /) -> None:
        if not isinstance(handlers, dict):
            raise ValueError("Handlers must be a dictionary")
        for k, v in handlers.items():
            if k not in self._handler_params:
                raise ValueError(f"Invalid handler key: {k}")
            if not (v is None or callable(v)):
                raise ValueError(f"Handler value must be callable or None, got {type(v)}")

    def _set_handlers(self, value: dict) -> None:
        self._validate_handlers(value)
        self._handlers = {k: None for k in self._handler_params}
        self._handlers.update(value)

    def update_handlers(self, **kwargs) -> None:
        self._validate_handlers(kwargs)
        self._set_handlers({**self._handlers, **kwargs})

    def copy_runtime_state_to(self, other) -> None:
        if isinstance(other, type(self)):
            other._set_handlers(self._handlers.copy())
            # Shallow on purpose. These are live objects — an open callback, a
            # mapping the caller may still hold — and copying them would hand
            # the copy a different object under the same name.
            other._runtime_state = dict(self._runtime_state)

    def _runtime_handlers(self, kwargs: dict) -> dict:
        handlers = self._handlers.copy()
        call_handlers = {k: kwargs.pop(k) for k in list(kwargs) if k in self._handler_params}
        if call_handlers:
            self._validate_handlers(call_handlers)
            handlers.update(call_handlers)
        return {k: v for k, v in handlers.items() if v is not None}

    def create_payload(self, request: dict | BaseModel, **kwargs):
        # _runtime_state sits where its values sat when they were still in
        # config.kwargs, so moving them out of the serialized config changed
        # where they live and not which one wins.
        req_dict = {**self._runtime_state, **self.config.kwargs, **to_dict(request), **kwargs}
        messages = req_dict.pop("messages", [])
        if self._filter_model_fields and self._request_model is not None:
            req_dict = {k: v for k, v in req_dict.items() if k in self._request_model.model_fields}
        req_dict.update(self._carried_runtime_state(request, req_dict))
        req_obj = self._request_model(messages=messages, **req_dict)
        return {"request": req_obj}, {}

    def _carried_runtime_state(self, request, req_dict: dict) -> dict:
        """Values from ``_runtime_state_fields`` that the rebuild would lose.

        ``to_dict(request)`` goes through ``model_dump()``, which omits every
        ``exclude=True`` field — harmless for most, but the runtime-state
        fields carry live objects (env, spawn callback) whose loss would
        silently revert the CLI request's wiring to defaults. Anything already
        in ``req_dict`` (explicit kwarg or endpoint config) keeps precedence.
        See docs/internals/providers.md#runtime-only-endpoint-state-kept-out-of-serialization.
        """
        model = self._request_model
        if model is None or not isinstance(request, model):
            return {}
        carried = {}
        for name in self._runtime_state_fields:
            if name in req_dict:
                continue
            value = getattr(request, name, None)
            if value is not None:
                carried[name] = value
        return carried
