# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.ln import AlcallParams

STANDARD_REMOVED_KWARGS: dict[str, str] = {
    "request_model": "response_format=",
    "operative_model": "response_format=",
    "imodel": "chat_model=",
}

_PARSE_CALL = None
_ACTION_CALL = None


def get_default_parse_call() -> AlcallParams:
    global _PARSE_CALL
    if _PARSE_CALL is None:
        _PARSE_CALL = AlcallParams(
            retry_initial_delay=1,
            retry_backoff=1.85,
            retry_attempts=3,
            max_concurrent=1,
            throttle_period=1,
        )
    return _PARSE_CALL


def get_default_action_call() -> AlcallParams:
    global _ACTION_CALL
    if _ACTION_CALL is None:
        _ACTION_CALL = AlcallParams(output_dropna=True)
    return _ACTION_CALL
