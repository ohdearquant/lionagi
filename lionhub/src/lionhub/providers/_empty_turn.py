# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A CLI turn that ended the way a finished turn ends, with nothing in it.

A CLI can close a turn cleanly (exit status 0, a turn-end event, no error event) having written no
assistant text and made no tool call. Read as a completed run that is an empty answer with no
reason attached, and a caller reading only the status takes it for success. This module decides
whether a finished session is such a turn and fails it, carrying the end of the CLI's stderr as
the only evidence of why.

The decision reads the session the parser built, never the provider's own events, so one rule
serves every CLI parser. A provider takes part by calling :func:`empty_turn_chunk` where its
stream ends and by adding its pair of event streams (one whose text counts, one empty) to the
conformance test. The cause of such a turn has not been measured; a prompt a hook refused is one
possible cause.
"""

from __future__ import annotations

from lionagi._provider_errors import ProviderEmptyTurnError
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk

__all__ = (
    "EMPTY_TURN_KEY",
    "STDERR_TAIL_CHARS",
    "empty_turn_chunk",
    "empty_turn_error",
    "is_empty_turn",
)

#: The metadata key that marks a stream chunk as the failure of an empty turn.
EMPTY_TURN_KEY = "empty_turn"

#: The most of the end of the CLI's stderr an empty turn's failure carries, in characters.
STDERR_TAIL_CHARS = 2000

_FAILURE = "The CLI ended the turn without assistant text, a tool call or an error (empty_turn)."


def is_empty_turn(session: CLISession) -> bool:
    """Whether *session* ended with no failure, no tool call and no assistant text.

    Text counts when it holds a character other than whitespace, in a text chunk or in the
    session's result. Reasoning and notices are not assistant text.
    """
    if session.is_error or session.tool_uses or session.tool_results:
        return False
    if isinstance(session.result, str) and session.result.strip():
        return False
    return not any(
        chunk.type == "text" and isinstance(chunk.content, str) and chunk.content.strip()
        for chunk in session.chunks
    )


def _stderr_sentence(stderr: str | None, unread: str | None) -> tuple[str, str]:
    """The sentence naming what the CLI's stderr said, and the tail it quotes."""
    if stderr is None:
        return "No stderr was captured.", ""
    text = stderr.strip()
    if not text:
        if unread:
            return f"Its stderr could not be read: {unread}.", ""
        return "It wrote nothing to stderr.", ""
    tail = text[-STDERR_TAIL_CHARS:]
    earlier = "[earlier stderr omitted] " if len(text) > STDERR_TAIL_CHARS else ""
    note = f" [{unread}]" if unread else ""
    return f"Its stderr ended with: {earlier}{tail}{note}", tail


def empty_turn_chunk(
    session: CLISession, stderr: str | None = None, unread: str | None = None
) -> StreamChunk:
    """Mark *session* failed as an empty turn and return the error chunk that says so.

    *stderr* is the CLI's stderr with credentials already removed, ``None`` when the provider
    captured none; *unread* says why it may be incomplete. The session is marked failed the way
    any failed turn is, ``is_error`` and a result naming the cause, and the chunk carries
    ``empty_turn`` and the quoted ``stderr_tail`` in its metadata.
    """
    sentence, tail = _stderr_sentence(stderr, unread)
    message = f"{_FAILURE} {sentence}"
    session.is_error = True
    session.result = message
    chunk = StreamChunk(
        type="error",
        content=message,
        is_error=True,
        metadata={EMPTY_TURN_KEY: True, "stderr_tail": tail},
    )
    session.chunks.append(chunk)
    return chunk


def empty_turn_error(session: CLISession) -> ProviderEmptyTurnError | None:
    """The error for *session*'s empty-turn failure, or ``None`` when it has none."""
    for chunk in session.chunks:
        if chunk.type == "error" and chunk.is_error and chunk.metadata.get(EMPTY_TURN_KEY):
            return ProviderEmptyTurnError(
                chunk.content or _FAILURE,
                stderr_tail=chunk.metadata.get("stderr_tail") or "",
            )
    return None
