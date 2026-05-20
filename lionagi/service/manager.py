# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.protocols._concepts import Manager
from lionagi.utils import is_same_dtype

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

    async def shutdown(self) -> None:
        """Close every registered iModel.

        Stops each iModel's RateLimitedAPIExecutor (and therefore its
        background ``start_replenishing`` task). Without this, the
        replenisher task stays scheduled on the event loop and prevents
        ``anyio.run`` / ``asyncio.run`` from returning even after the
        owning coroutine completes — manifesting as a hanging CLI process.

        Safe to call multiple times; ``iModel.close()`` is idempotent on
        an already-stopped executor. Per-model failures (including
        cancellation, which is ``BaseException`` not ``Exception``) are
        logged and swallowed so one broken endpoint cannot leave the
        rest of the registry leaked.
        """
        import logging

        log = logging.getLogger("lionagi.service")
        for name, model in self.registry.items():
            try:
                await model.close()
            except BaseException as exc:  # noqa: BLE001 — cancellation is BaseException
                log.warning(
                    "iModel shutdown failed for %r: %s", name, exc, exc_info=True
                )
