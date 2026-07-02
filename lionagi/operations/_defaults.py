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


def make_parse_param(
    response_format,
    imodel,
    *,
    handle_validation="return_value",
    num_retries: int | None = None,
    fuzzy_kw: dict | None = None,
):
    """Build a ParseParam with standard defaults for structured-output parsing.

    Covers the common case (bare get_default_parse_call, empty FuzzyMatchKeysParams).
    For communicate's per-call retry variant pass num_retries; for non-default fuzzy
    settings pass fuzzy_kw (keys forwarded to FuzzyMatchKeysParams).
    """
    from lionagi.ln.fuzzy import FuzzyMatchKeysParams

    from .types import ParseParam

    _alcall = get_default_parse_call()
    if num_retries is not None:
        _alcall = _alcall.with_updates(retry_attempts=num_retries)

    _fmp = FuzzyMatchKeysParams(**fuzzy_kw) if fuzzy_kw else FuzzyMatchKeysParams()

    return ParseParam(
        response_format=response_format,
        fuzzy_match_params=_fmp,
        handle_validation=handle_validation,
        alcall_params=_alcall,
        imodel=imodel,
        imodel_kw={},
    )


def get_default_action_call() -> AlcallParams:
    global _ACTION_CALL
    if _ACTION_CALL is None:
        _ACTION_CALL = AlcallParams(output_dropna=True)
    return _ACTION_CALL
