"""Shared record definitions for persisted live stream output.

The live stream buffer has one writer (``lionagi.operations.run.run``) and one reader
(``lionagi.cli.monitor``); both take the record marker and the legacy text rule from here.
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


def legacy_stream_text(record: Any) -> str | None:
    """The display text of a legacy stream record: ``delta.text``, then ``text``, then ``content``.

    A legacy record may carry metadata in ``content`` beside its text, so ``content`` is read
    last. Only a string counts, returned as a plain ``str`` so that a subclass's conversion hook
    never runs; anything else at a key moves on to the next one.
    """
    for value in (
        _get(_get(record, "delta"), "text"),
        _get(record, "text"),
        _get(record, "content"),
    ):
        if isinstance(value, str):
            return str.__str__(value)
    return None
