# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel

from lionagi.utils import to_dict

__all__ = ("RUNTIME_STATE_NAMES", "AgenticHandlersMixin")

# The runtime-only values a CLI endpoint can hold. Named here so a caller that
# supplies them to an endpoint with no route for them can be refused, which
# needs the names before any endpoint class is in hand.
RUNTIME_STATE_NAMES: tuple[str, ...] = ("env", "on_spawn")


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
        """Lift the declared runtime values off the caller's OWN objects.

        Called before ``Endpoint.__init__``, which copies a supplied
        ``EndpointConfig`` with ``model_copy(deep=True)``. A deep copy of a
        bound method copies its receiver too, so the callback that reaches the
        CLI would notify a copy of the supervisor while the real one — the
        thing that owns the durable process accounting — hears nothing, and
        every wiring check still passes. Reading the values here, from the
        object the caller handed in, is what preserves identity.

        Nothing is mutated: the copy still happens and the copied values are
        still removed from the serialized config afterwards. This only decides
        which of the two the endpoint ends up holding.
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

        The constructor route lifts these off the caller's config before the
        endpoint exists. A caller who hands over an endpoint INSTANCE has
        missed that window entirely: the endpoint was built without them, and
        the values arrive beside an object that is finished. They are written
        here instead of dropped, which is what happened before and is the worst
        of the three options — a child would inherit the wrong environment and
        no supervisor would hear about it, with nothing raised and nothing
        logged.

        Writing onto the supplied instance rather than a copy is deliberate and
        matches what the same branch already does with ``provider`` and
        ``base_url``: the caller handed this object over to be configured. A
        ``None`` is not a value here, it is the absence of one, and it must not
        erase state the endpoint was built with.

        Returns the names it could not place, so the caller can refuse rather
        than let them evaporate.
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

        ``iModel(**kwargs)`` forwards anything it does not recognise into
        ``EndpointConfig.kwargs``, which is a supported way to configure an
        endpoint and also the thing ``Endpoint.to_dict`` serializes — so it
        reaches ``iModel.to_dict``, ``Branch.to_dict``, and from there the run
        snapshots written to disk. A child environment left there is a
        credential in a saved file, and a callback left there is a function in a
        structure something is about to JSON-encode.

        Holding it here instead keeps the same configuration route working while
        the value stays in memory. It also survives ``iModel.copy``, which deep
        copies the config and then calls ``copy_runtime_state_to``: a deep copy
        of a bound callback rebinds it to a copied receiver, so the original
        supervisor would quietly stop hearing from the copy's legs.
        """
        self._runtime_state: dict[str, object] = {}
        for name in self._runtime_state_fields:
            if name in self.config.kwargs:
                self._runtime_state[name] = self.config.kwargs.pop(name)
        # Values taken off the caller's own objects win over what the pop just
        # produced: everything in config.kwargs came through a deep copy, and
        # for a bound callback that copy is a different receiver.
        if supplied:
            self._runtime_state.update(supplied)

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

        The request model is rebuilt here from ``to_dict(request)``, which goes
        through ``model_dump()`` and so omits every field declared
        ``exclude=True``. For a field that only shapes serialization that is
        harmless, but the fields named in ``_runtime_state_fields`` carry live
        objects the process needs — a child environment, a spawn callback — and
        losing them hands the CLI a request whose runtime wiring silently
        reverted to its defaults. The names are declared rather than derived
        from ``exclude``, because most excluded fields genuinely have nothing to
        carry and a rule that swept them all in would change unrelated
        behaviour. Anything already in ``req_dict`` came from an explicit kwarg
        or from the endpoint config and keeps precedence.
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
