# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from lionagi.ln import (
    AlcallParams,
    extract_json,
    fuzzy_validate_mapping,
    get_cancelled_exc_class,
    to_list,
)
from lionagi.ln.fuzzy import FuzzyMatchKeysParams
from lionagi.operations.schema.structure import Structure
from lionagi.protocols.types import AssistantResponse

from .._defaults import get_default_parse_call as get_default_call
from ..types import (
    ExtractionError,
    HandleValidation,
    ParseError,
    ParseParam,
    SchemaRejectedError,
    UnparsedResponse,
)

if TYPE_CHECKING:
    from lionagi.ln.types import Operable
    from lionagi.session.branch import Branch


logger = logging.getLogger(__name__)

_BASE_REFORMAT_GUIDANCE = "follow the required response format, using the model schema as a guide"

# Validation errors list every offending field; keep the tail out of the prompt.
_MAX_REPORTED_ERROR_CHARS = 800


class _ReformatAttempts:
    """Per-attempt framing for the reformat turn.

    Every reformat turn is a repair of the *same* text, so without this the
    retry loop reissues a byte-identical request; on a deterministic engine
    (temperature 0) that cannot produce a different answer, and the caller pays
    for each repetition. Each turn instead reports the failure that preceded
    it, so the request differs and the model is told what to fix.
    """

    __slots__ = ("count", "last_error", "last_exc")

    def __init__(self) -> None:
        self.count = 0
        self.last_error: str | None = None
        self.last_exc: Exception | None = None

    def record_failure(self, exc: Exception) -> None:
        message = str(exc).strip() or type(exc).__name__
        if len(message) > _MAX_REPORTED_ERROR_CHARS:
            message = message[:_MAX_REPORTED_ERROR_CHARS] + " …(truncated)"
        self.last_error = message
        self.last_exc = exc

    def failure_kind(self) -> str:
        """Classify the last failure; unclassified causes count as extraction."""
        return self.last_exc.kind if isinstance(self.last_exc, ParseError) else "extraction"

    def as_unparsed(self, text: str) -> UnparsedResponse:
        """Wrap the raw text with why it could not be parsed."""
        exc = self.last_exc
        return UnparsedResponse(
            text,
            failure_kind=self.failure_kind(),
            validation_error=(
                exc.validation_error if isinstance(exc, SchemaRejectedError) else None
            ),
        )

    def guidance(self) -> str:
        self.count += 1
        parts = [_BASE_REFORMAT_GUIDANCE]
        if self.last_error:
            parts.append(
                f"The previous attempt did not validate: {self.last_error}. "
                "Correct exactly what that error names — every field it reports "
                "must carry a value of the required type."
            )
        if self.count > 1:
            parts.append(
                f"This is attempt {self.count}; earlier attempts failed. Return only "
                "the JSON object, with no text before or after it."
            )
        return " ".join(parts)


def _try_propagate_structure(content: Any, parse_param: "ParseParam") -> "ParseParam":
    """If parse_param has no Structure yet, pull _structure_instance from instruction content."""
    if not isinstance(parse_param.structure, Structure) and content is not None:
        si = getattr(content, "_structure_instance", None)
        if si is not None:
            return parse_param.with_updates(structure=si)
    return parse_param


def prepare_parse_kws(
    branch: "Branch",
    text: str,
    handle_validation: HandleValidation = "return_value",
    max_retries: int = 3,
    request_type: type[BaseModel] | None = None,
    operative=None,
    similarity_algo="jaro_winkler",
    similarity_threshold: float = 0.85,
    fuzzy_match: bool = True,
    handle_unmatched: Literal["ignore", "raise", "remove", "fill", "force"] = "force",
    fill_value: Any = None,
    fill_mapping: dict[str, Any] | None = None,
    strict: bool = False,
    response_format=None,
    request_fields=None,
    structure=None,
    return_res_message: bool = False,
    **kw,
):
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
    structure = parse_param.structure if isinstance(parse_param.structure, Structure) else None
    attempts = _ReformatAttempts()

    if structure is not None:
        try:
            result = structure.parse(
                text,
                fuzzy_match_params=parse_param.fuzzy_match_params,
            )
            return result if not return_res_message else (result, None)
        except Exception as e:
            attempts.record_failure(e)
    else:
        try:
            result = _validate_dict_or_model(
                text, parse_param.response_format, parse_param.fuzzy_match_params
            )
            return result if not return_res_message else (result, None)
        except Exception as e:
            attempts.record_failure(e)

    async def _inner_parse(i):
        # This retries a failed parse by calling the public Branch.chat()
        # directly — not a user-originated turn, so it must never mint or
        # fire its own USER_PROMPT_SUBMIT: the outer call this repair serves
        # (whatever public ingress that was) already fired at most once.
        from .._turn_origin import TurnOrigin

        _, res = await branch.chat(
            instruction="reformat text into specified model or structure",
            guidance=attempts.guidance(),
            context=[{"text_to_format": text}],
            request_fields=(
                parse_param.response_format
                if isinstance(parse_param.response_format, dict)
                else None
            ),
            response_format=(
                parse_param.response_format
                if isinstance(parse_param.response_format, BaseModel)
                or (
                    isinstance(parse_param.response_format, type)
                    and issubclass(parse_param.response_format, BaseModel)
                )
                else None
            ),
            imodel=(
                branch.parse_model
                if parse_param._is_sentinel(parse_param.imodel)
                else parse_param.imodel
            ),
            sender=branch.user,
            recipient=branch.id,
            return_ins_res_message=True,
            _turn_origin=TurnOrigin.no_origin(),
        )

        res.metadata["is_parsed"] = True
        res.metadata["original_text"] = text

        try:
            if structure is not None:
                parsed = structure.parse(
                    res.response,
                    fuzzy_match_params=parse_param.fuzzy_match_params,
                )
            else:
                parsed = _validate_dict_or_model(
                    res.response,
                    parse_param.response_format,
                    parse_param.fuzzy_match_params,
                )
        except Exception as e:
            attempts.record_failure(e)
            raise
        return parsed, res

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
                # Keep the message and the ValueError base, so existing handlers
                # are unaffected, but carry the kind and the pydantic error.
                failure_cls = (
                    SchemaRejectedError
                    if attempts.failure_kind() == "validation"
                    else ExtractionError
                )
                last = attempts.last_exc
                raise failure_cls(
                    f"Failed to parse response: {e}",
                    validation_error=(
                        last.validation_error if isinstance(last, SchemaRejectedError) else None
                    ),
                ) from e
            case "return_none":
                return (None, None) if return_res_message else None
            case "return_value":
                # Degrade to the raw text as before, but attached to why it
                # failed — a schema rejection must not look like a parse error.
                value = attempts.as_unparsed(text)
                if value.failure_kind == "validation":
                    logger.warning(
                        "parse: response was valid JSON but did not satisfy %s; "
                        "returning raw text. Validation error: %s",
                        getattr(
                            parse_param.response_format, "__name__", parse_param.response_format
                        ),
                        attempts.last_error,
                    )
                return (value, None) if return_res_message else value
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
        return "\n\n".join(blocks)
    return text


def _validate_dict_or_model(
    text: str,
    response_format: type[BaseModel] | dict | Any,
    fuzzy_match_params: FuzzyMatchKeysParams | dict | None = None,
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
            # Isolated from the extraction steps above: by here the JSON was
            # recovered, so anything raised is the schema rejecting the data.
            try:
                return response_format.model_validate(dict_)
            except PydanticValidationError as e:
                raise SchemaRejectedError(f"Failed to parse text: {e}", validation_error=e) from e
        return dict_

    except ParseError:
        raise
    except Exception as e:
        raise ExtractionError(f"Failed to parse text: {e}") from e
