# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.ln import is_same_dtype
from lionagi.ln.concurrency import gather
from lionagi.protocols._concepts import Manager

from .imodel import iModel


class iModelManager(Manager):  # noqa: N801 — mirrors iModel naming
    def __init__(self, *args: iModel, **kwargs):
        super().__init__()

        self.registry: dict[str, iModel] = {}
        if args:
            if not is_same_dtype(args, iModel):
                raise TypeError("Input models are not instances of iModel")
            for model in args:
                self.register_imodel(model.endpoint.endpoint, model)

        if kwargs:
            for name, model in kwargs.items():
                self.register_imodel(name, model)

    @property
    def chat(self) -> iModel | None:
        return self.registry.get("chat", None)

    @property
    def parse(self) -> iModel | None:
        return self.registry.get("parse", None)

    def register_imodel(self, name: str, model: iModel):
        if isinstance(model, iModel):
            self.registry[name] = model
        else:
            raise TypeError("Input model is not an instance of iModel")

    async def shutdown(self, *, per_model_timeout: float = 10.0) -> None:
        """Close every registered iModel concurrently, with a per-model timeout.
        Idempotent; per-model failures are logged and swallowed. See docs/internals/runtime.md."""
        import asyncio
        import logging

        log = logging.getLogger("lionagi.service")

        async def _close_one(name: str, model: iModel) -> None:
            try:
                # TODO(#1043 Phase 2): migrate to anyio cancel scope for timeout
                await asyncio.wait_for(model.close(), timeout=per_model_timeout)
            except asyncio.TimeoutError:
                log.warning(
                    "iModel shutdown timed out for %r after %.1fs",
                    name,
                    per_model_timeout,
                )
            except BaseException as exc:  # noqa: BLE001 — cancellation is BaseException
                log.warning(
                    "iModel shutdown failed for %r: %s",
                    name,
                    exc,
                    exc_info=True,
                )

        if not self.registry:
            return
        await gather(
            *(_close_one(name, model) for name, model in self.registry.items()),
            return_exceptions=True,
        )
