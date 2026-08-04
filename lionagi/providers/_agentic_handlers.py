# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel

from lionagi.utils import to_dict

__all__ = ("AgenticHandlersMixin",)


class AgenticHandlersMixin:
    _handler_params: ClassVar[tuple[str, ...]] = ()
    _handler_kwarg: ClassVar[str] = ""
    _request_model: ClassVar[type[BaseModel] | None] = None
    _filter_model_fields: ClassVar[bool] = True
    # Fields excluded from the request model's dump that nonetheless have to
    # survive create_payload's rebuild. See _carried_runtime_state.
    _runtime_state_fields: ClassVar[tuple[str, ...]] = ()

    def _init_handlers(self, handlers: dict | None = None) -> None:
        config_handlers = self.config.kwargs.pop(self._handler_kwarg, None)
        self._handlers: dict[str, Callable | None] = {k: None for k in self._handler_params}
        if config_handlers is not None:
            self._validate_handlers(config_handlers)
            self._handlers.update(config_handlers)
        if handlers is not None:
            self._validate_handlers(handlers)
            self._handlers.update(handlers)

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

    def _runtime_handlers(self, kwargs: dict) -> dict:
        handlers = self._handlers.copy()
        call_handlers = {k: kwargs.pop(k) for k in list(kwargs) if k in self._handler_params}
        if call_handlers:
            self._validate_handlers(call_handlers)
            handlers.update(call_handlers)
        return {k: v for k, v in handlers.items() if v is not None}

    def create_payload(self, request: dict | BaseModel, **kwargs):
        req_dict = {**self.config.kwargs, **to_dict(request), **kwargs}
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
