"""Shared record definitions for persisted live stream output.

The live stream buffer has one writer (``lionagi.operations.run.run``) and one reader
(``lionagi.cli.monitor``); both take the record marker and the legacy text rules from here.
"""

from collections.abc import Mapping
from typing import Any

STREAM_CHUNK_MARKER = "_lionagi_stream_chunk"


def _get(mapping: Any, key: str) -> Any:
    """``mapping[key]`` or None, without a dict subclass's ``get`` and never raising."""
    if not isinstance(mapping, Mapping):
        return None
    try:
        return dict.get(mapping, key) if isinstance(mapping, dict) else mapping.get(key)
    except Exception:
        return None


def _plain(value: Any) -> str | None:
    """A string as a plain ``str``, so a subclass's conversion hook never runs; else None."""
    return str.__str__(value) if isinstance(value, str) else None


def legacy_stream_text(record: Any) -> str | None:
    """The display text of a legacy stream record: ``delta.text``, then ``text``.

    ``content`` is never shown: a legacy record may carry metadata or non-text output there.
    A key whose value is not a string, or is empty, moves on to the next one.
    """
    for value in (_get(_get(record, "delta"), "text"), _get(record, "text")):
        if text := _plain(value):
            return text
    return None


def legacy_fallback_text(record: Any) -> str | None:
    """The text the writer keeps for a legacy chunk it cannot serialize.

    ``content``, then ``text``, then ``delta.text``: the first key holding a string wins,
    an empty one included, as the writer has always read them.
    """
    for value in (
        _get(record, "content"),
        _get(record, "text"),
        _get(_get(record, "delta"), "text"),
    ):
        if (text := _plain(value)) is not None:
            return text
    return None
