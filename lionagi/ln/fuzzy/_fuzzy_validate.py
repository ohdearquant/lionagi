from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from lionagi._errors import ValidationError

from ..types import KeysLike
from ._extract_json import extract_json
from ._fuzzy_match import FuzzyMatchKeysParams, fuzzy_match_keys
from ._string_similarity import SIMILARITY_TYPE
from ._to_dict import to_dict

if TYPE_CHECKING:
    from pydantic import BaseModel


__all__ = ("fuzzy_validate_pydantic",)


def fuzzy_validate_pydantic(
    text,
    /,
    model_type: "type[BaseModel]",
    fuzzy_parse: bool = True,
    fuzzy_match: bool = False,
    fuzzy_match_params: FuzzyMatchKeysParams | dict = None,
):
    try:
        model_data = extract_json(text, fuzzy_parse=fuzzy_parse)
    except Exception as e:
        raise ValidationError(f"Failed to extract valid JSON from model response: {e}") from e

    d = model_data
    if fuzzy_match:
        if fuzzy_match_params is None:
            model_data = fuzzy_match_keys(d, model_type.model_fields, handle_unmatched="remove")
        elif isinstance(fuzzy_match_params, dict):
            model_data = fuzzy_match_keys(d, model_type.model_fields, **fuzzy_match_params)
        elif isinstance(fuzzy_match_params, FuzzyMatchKeysParams):
            model_data = fuzzy_match_params(d, model_type.model_fields)
        else:
            raise TypeError("fuzzy_keys_params must be a dict or FuzzyMatchKeysParams instance")

    try:
        return model_type.model_validate(model_data)
    except Exception as e:
        raise ValidationError(f"Validation failed: {e}") from e


def fuzzy_validate_mapping(
    d: Any,
    keys: KeysLike,
    /,
    *,
    similarity_algo: SIMILARITY_TYPE | Callable[[str, str], float] = "jaro_winkler",
    similarity_threshold: float = 0.85,
    fuzzy_match: bool = True,
    handle_unmatched: Literal["ignore", "raise", "remove", "fill", "force"] = "ignore",
    fill_value: Any = None,
    fill_mapping: dict[str, Any] | None = None,
    strict: bool = False,
    suppress_conversion_errors: bool = False,
) -> dict[str, Any]:
    """
    Validate and correct any input into a dictionary with expected keys.

    Args:
        d: Input to validate. Can be:
            - Dictionary
            - JSON string or markdown code block
            - XML string
            - Object with to_dict/model_dump method
            - Any type convertible to dictionary
        keys: List of expected keys or dictionary mapping keys to types.
        similarity_algo: String similarity algorithm or custom function.
        similarity_threshold: Minimum similarity score for fuzzy matching.
        fuzzy_match: If True, use fuzzy matching for key correction.
        handle_unmatched: How to handle unmatched keys:
            - "ignore": Keep unmatched keys
            - "raise": Raise error for unmatched keys
            - "remove": Remove unmatched keys
            - "fill": Fill missing keys with default values
            - "force": Combine "fill" and "remove" behaviors
        fill_value: Default value for filling unmatched keys.
        fill_mapping: Dictionary mapping keys to default values.
        strict: Raise error if any expected key is missing.
        suppress_conversion_errors: Return empty dict on conversion errors.

    Returns:
        Validated and corrected dictionary.

    Raises:
        ValueError: If input cannot be converted or validation fails.
        TypeError: If input types are invalid.
    """
    if d is None:
        raise TypeError("Input cannot be None")

    # Try converting to dictionary
    try:
        if isinstance(d, str):
            try:
                json_result = extract_json(d, fuzzy_parse=True, return_one_if_single=True)
                dict_input = json_result[0] if isinstance(json_result, list) else json_result
            except Exception:
                dict_input = to_dict(d, str_type="json", fuzzy_parse=True, suppress=True)
        else:
            dict_input = to_dict(d, use_model_dump=True, fuzzy_parse=True, suppress=True)

        if not isinstance(dict_input, dict):
            if suppress_conversion_errors:
                dict_input = {}
            else:
                raise ValueError(f"Failed to convert input to dictionary: {type(dict_input)}")

    except Exception as e:
        if suppress_conversion_errors:
            dict_input = {}
        else:
            raise ValueError(f"Failed to convert input to dictionary: {e}") from e

    # Validate the dictionary
    return fuzzy_match_keys(
        dict_input,
        keys,
        similarity_algo=similarity_algo,
        similarity_threshold=similarity_threshold,
        fuzzy_match=fuzzy_match,
        handle_unmatched=handle_unmatched,
        fill_value=fill_value,
        fill_mapping=fill_mapping,
        strict=strict,
    )
