# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import io


def _replayable_file_factory(file_data, field_name: str, *, require_replayable: bool = True):
    """Return a zero-arg callable producing a fresh file object for one retry attempt.
    See docs/internals/runtime.md for the replay-safety invariant."""
    if file_data is None:
        return lambda: None
    if isinstance(file_data, (bytes, bytearray)):
        snapshot = bytes(file_data)
        return lambda: io.BytesIO(snapshot)
    if not require_replayable:
        return lambda: file_data

    seekable = getattr(file_data, "seekable", None)
    if not callable(seekable) or not seekable():
        if require_replayable:
            raise TypeError(
                f"{field_name} must be bytes, bytearray, or a seekable stream to "
                "support retries; pass bytes, or configure the endpoint with "
                "max_retries=1 for a non-seekable stream."
            )
        return lambda: file_data
    # Snapshot once, restore position — a live stream handed to each attempt would
    # already be at EOF on retry (RetryConfig re-invokes _call), uploading empty.
    start_pos = file_data.tell()
    snapshot = file_data.read()
    file_data.seek(start_pos)
    return lambda: io.BytesIO(snapshot)


__all__ = ("_replayable_file_factory",)
