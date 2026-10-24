# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

from pydantic import Field, PrivateAttr, field_validator

from lionagi.ln.concurrency import current_time, fail_after, get_cancelled_exc_class
from lionagi.protocols.types import Event, EventStatus

from ._types import AssociatedEventInfo, HookEventTypes
from .hook_registry import HookRegistry


class HookEvent(Event):
    registry: HookRegistry = Field(..., exclude=True)
    hook_type: HookEventTypes
    exit: bool = Field(False, exclude=True)
    timeout: int | float = Field(30, exclude=True)
    params: dict[str, Any] = Field(default_factory=dict, exclude=True)
    event_like: Event | type[Event] = Field(..., exclude=True)
    _should_exit: bool = PrivateAttr(False)
    _exit_cause: BaseException | None = PrivateAttr(None)

    associated_event_info: AssociatedEventInfo | None = None

    @field_validator("exit", mode="before")
    def _validate_exit(cls, v: Any) -> bool:
        if v is None:
            return False
        return v

    async def invoke(self):
        start = current_time()
        self.execution.status = EventStatus.PROCESSING
        final_status = EventStatus.PROCESSING
        try:
            with fail_after(self.timeout):
                (res, se, st), meta = await self.registry.call(
                    self.event_like,
                    hook_type=self.hook_type,
                    should_exit=self.exit,
                    **self.params,
                )

                self.associated_event_info = AssociatedEventInfo(**meta)
                self._should_exit = se
                if not (
                    isinstance(st, EventStatus)
                    or (isinstance(st, str) and st in EventStatus.allowed())
                ):
                    # Preserve invalid-status precedence over response interpretation.
                    self.status = st
                final_status = st
                if isinstance(res, tuple) and len(res) == 2:
                    self.execution.response = None
                    self.execution.error = str(res[1])
                    self._exit_cause = res[1]
                    raise res[1]
                if isinstance(res, Exception):
                    self.execution.response = None
                    self.execution.error = str(res)
                    self._exit_cause = res
                else:
                    self.execution.response = res
                    self.execution.error = None
        except get_cancelled_exc_class():
            raise

        except Exception as e:
            final_status = EventStatus.FAILED
            self._record_failure(e)

        finally:
            self.execution.duration = current_time() - start
            try:
                self.status = final_status
            except Exception as e:
                # Preserve setter validation for custom registry results.
                self._record_failure(e)
                self.status = EventStatus.FAILED

    def _record_failure(self, error: Exception) -> None:
        self.execution.response = None
        self.execution.error = str(error)
        self._exit_cause = error
        self._should_exit = bool(self.exit)
