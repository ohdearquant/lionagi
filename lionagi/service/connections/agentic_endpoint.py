# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import re
from typing import Any, ClassVar

from .endpoint import Endpoint
from .endpoint_config import EndpointConfig

__all__ = ("AgenticEndpoint",)

# A stored session id is restored only as a UUID in its canonical lowercase form, the shape of the
# ids the Claude Code CLI issues. A value read from a file in any other shape restores nothing, so
# it can never become a flag or any other argument of the spawned CLI.
_SESSION_ID_SHAPE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# What the Claude Code CLI says when asked to resume a session it holds no transcript of.
_UNHELD_SESSION_TEXT = "no conversation found with session id"


class AgenticEndpoint(Endpoint):
    """Base for CLI/in-process agentic endpoints; subclasses implement ``stream()`` yielding ``StreamChunk``."""

    is_cli: ClassVar[bool] = True

    # Early-streaming transports gate run.py's default first-output and
    # between-chunk liveness watchdogs.
    # See docs/internals/runtime.md.
    streams_first_output_early: ClassVar[bool] = False

    DEFAULT_CONCURRENCY_LIMIT: ClassVar[int] = 3
    DEFAULT_QUEUE_CAPACITY: ClassVar[int] = 10

    def __init__(self, config: dict | EndpointConfig = None, **kwargs):
        super().__init__(config=config, **kwargs)
        self._session_id: str | None = None
        self._session_id_reported = False

    @property
    def provider_session_id(self) -> str | None:
        return self._session_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str | None):
        """Set the id the CLI reported in this process; None clears it."""
        self._session_id = value
        self._session_id_reported = value is not None

    def restore_session_id(self, value: Any) -> None:
        """Set the id a snapshot stored, which the next request resumes.

        Only a string in the lowercase UUID shape the CLI issues is an id. Any other value, a
        flag or a non-string included, restores nothing, without an error, and leaves the
        endpoint with no id to resume. Unlike ``session_id``, this does not mark the id as
        reported by the CLI in this process, so ``reported_session_id`` stays None until the
        stream reports one.
        """
        is_id = isinstance(value, str) and _SESSION_ID_SHAPE.fullmatch(value) is not None
        self._session_id = value if is_id else None
        self._session_id_reported = False

    @property
    def reported_session_id(self) -> str | None:
        """The session id the CLI reported in this process, or None.

        An id restored from a snapshot is not one until the stream reports an id, the same or a
        new one. ``session_id`` returns the id to resume, restored or reported.
        """
        return self._session_id if self._session_id_reported else None

    def to_dict(self):
        """The endpoint's serialized form, with the CLI's session id once it has one.

        ``iModel.from_dict`` reads the id back, so a model restored from a snapshot continues
        the CLI's own session instead of starting one.
        """
        data = super().to_dict()
        if isinstance(self._session_id, str) and self._session_id:
            data["session_id"] = self._session_id
        return data

    def _create_http_session(self, client_kwargs=None):
        raise NotImplementedError("Agentic endpoints do not use HTTP sessions")

    async def _call_http(self, *a, **kw):
        raise NotImplementedError("Agentic endpoints do not use HTTP")

    async def _stream_http(self, payload: dict, headers: dict, **kwargs):
        raise NotImplementedError("Agentic endpoints do not use HTTP streaming")
