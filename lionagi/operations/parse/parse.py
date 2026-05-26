# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import contextlib
import warnings
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from lionagi.ln import (
    AlcallParams,
    extract_json,
    fuzzy_validate_mapping,
    get_cancelled_exc_class,
    to_list,
)
from lionagi.ln.fuzzy import FuzzyMatchKeysParams
from lionagi.protocols.types import AssistantResponse

from ..types import HandleValidation, ParseParam

if TYPE_CHECKING:
    from lionagi.ln.types import Operable
    from lionagi.session.branch import Branch


_CALL = None  # type: ignore


def prepare_parse_kws(
    branch: "Branch",
    text: str,
    handle_validation: HandleValidation = "return_value",
    max_retries: int = 3,
    request_type: type[BaseModel] = None,
    operative=None,
    similarity_algo="jaro_winkler",
    similarity_threshold: float = 0.85,
    fuzzy_match: bool = True,
    handle_unmatched: Literal["ignore", "raise", "remove", "fill", "force"] = "force",
    fill_value: Any = None,
    fill_mapping: dict[str, Any] | None = None,
    strict: bool = False,
    suppress_conversion_errors: bool = False,
    response_format=None,
    request_fields=None,
    structure=None,
    return_res_message: bool = False,
    **kw,
):
    if suppress_conversion_errors:
        warnings.warn(
            "Parameter 'suppress_conversion_errors' is deprecated and no longer used. "
            "It will be removed in v0.21.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    response_format = operative.request_type if operative else response_format or request_type
    _alcall_params = get_default_call()
    max_retries = operative.max_retries if operative else max_retries or 3

    fuzzy_params = FuzzyMatchKeysParams(
        similarity_algo=similarity_algo,
        similarity_threshold=similarity_threshold,
        handle_unmatched=handle_unmatched,
        fill_value=fill_value,
        fill_mapping=fill_mapping,
        strict=strict,
        fuzzy_match=fuzzy_match,
    )

    return {
        "text": text,
        "parse_param": ParseParam(
            response_format=response_format or request_fields,
            structure=structure,
            fuzzy_match_params=fuzzy_params,
            handle_validation=handle_validation,
            alcall_params=_alcall_params.with_updates(retry_attempts=max_retries),
            imodel=branch.parse_model,
            imodel_kw=kw,
        ),
        "return_res_message": return_res_message,
    }


async def parse(
    branch: "Branch",
    text: str,
    parse_param: ParseParam,
    return_res_message: bool = False,
) -> Any | tuple[Any, AssistantResponse | None]:
    # When a Structure instance is provided, delegate to it
    if parse_param.structure is not None:
        with contextlib.suppress(Exception):
            result = parse_param.structure.parse(
                text,
                fuzzy_match_params=parse_param.fuzzy_match_params,
            )
            return result if not return_res_message else (result, None)
    else:
        # Try direct validation first
        with contextlib.suppress(Exception):
            result = _validate_dict_or_model(
                text, parse_param.response_format, parse_param.fuzzy_match_params
            )
            return result if not return_res_message else (result, None)

    async def _inner_parse(i):
        _, res = await branch.chat(
            instruction="reformat text into specified model or structure",
            guidance="follow the required response format, using the model schema as a guide",
            context=[{"text_to_format": text}],
            request_fields=(
                parse_param.response_format
                if isinstance(parse_param.response_format, dict)
                else None
            ),
            response_format=(
                parse_param.response_format
                if isinstance(parse_param.response_format, BaseModel)
                else None
            ),
            imodel=parse_param.imodel or branch.parse_model,
            sender=branch.user,
            recipient=branch.id,
            return_ins_res_message=True,
        )

        res.metadata["is_parsed"] = True
        res.metadata["original_text"] = text

        if parse_param.structure is not None:
            return (
                parse_param.structure.parse(
                    res.response,
                    fuzzy_match_params=parse_param.fuzzy_match_params,
                ),
                res,
            )
        return (
            _validate_dict_or_model(
                res.response,
                parse_param.response_format,
                parse_param.fuzzy_match_params,
            ),
            res,
        )

    _call = parse_param.alcall_params or get_default_call()
    if isinstance(parse_param.alcall_params, dict):
        _call = AlcallParams(**parse_param.alcall_params)

    try:
        result = await _call([0], _inner_parse)
    except get_cancelled_exc_class():
        raise
    except Exception as e:
        match parse_param.handle_validation:
            case "raise":
                raise ValueError(f"Failed to parse response: {e}") from e
            case "return_none":
                return (None, None) if return_res_message else None
            case "return_value":
                return (text, None) if return_res_message else text
    return (*result[0],) if return_res_message else result[0][0]


def _is_lndl_operable(response_format: Any) -> bool:
    """Return True when the caller opted into LNDL by passing an Operable."""
    try:
        from lionagi.ln.types import Operable

        return isinstance(response_format, Operable)
    except ImportError:
        return False


def _extract_lndl(text: str, operable: "Operable") -> Any:
    """Route text through the LNDL Phase 1 extract path."""
    from lionagi.lndl.extract import extract_lndl_blocks

    blocks = extract_lndl_blocks(text)
    if blocks:
        return blocks[0]
    return text


def _validate_dict_or_model(
    text: str,
    response_format: type[BaseModel] | dict | Any,
    fuzzy_match_params: FuzzyMatchKeysParams | dict = None,
):
    if _is_lndl_operable(response_format):
        return _extract_lndl(text, response_format)

    try:
        if isinstance(fuzzy_match_params, dict):
            fuzzy_match_params = FuzzyMatchKeysParams(**fuzzy_match_params)

        d_ = extract_json(text, fuzzy_parse=True, return_one_if_single=False)
        dict_, keys_ = None, None
        if d_:
            dict_ = to_list(d_, flatten=True)[0]
        if isinstance(fuzzy_match_params, FuzzyMatchKeysParams):
            keys_ = (
                response_format.model_fields
                if isinstance(response_format, type)
                else response_format
            )
            dict_ = fuzzy_validate_mapping(dict_, keys_, **fuzzy_match_params.to_dict())
        elif fuzzy_match_params:
            keys_ = (
                response_format.model_fields
                if isinstance(response_format, type)
                else response_format
            )
            dict_ = fuzzy_validate_mapping(
                dict_,
                keys_,
                handle_unmatched="force",
                fill_value=None,
                strict=False,
            )
        if isinstance(response_format, type) and issubclass(response_format, BaseModel):
            return response_format.model_validate(dict_)
        return dict_

    except Exception as e:
        raise ValueError(f"Failed to parse text: {e}") from e


def get_default_call() -> AlcallParams:
    global _CALL
    if _CALL is None:
        _CALL = AlcallParams(
            retry_initial_delay=1,
            retry_backoff=1.85,
            retry_attempts=3,
            max_concurrent=1,
            throttle_period=1,
        )
    return _CALL
