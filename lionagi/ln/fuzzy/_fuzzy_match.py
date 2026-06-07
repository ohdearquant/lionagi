from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from ..types import KeysLike, ModelConfig, Params, Unset
from ._string_similarity import (
    SIMILARITY_ALGO_MAP,
    SIMILARITY_TYPE,
    SimilarityFunc,
    string_similarity,
)

__all__ = (
    "fuzzy_match_keys",
    "FuzzyMatchKeysParams",
)


HandleUnmatched = Literal["ignore", "raise", "remove", "fill", "force"]


def fuzzy_match_keys(
    d_: dict[str, Any],
    keys: KeysLike,
    /,
    *,
    similarity_algo: SIMILARITY_TYPE | SimilarityFunc = "jaro_winkler",
    similarity_threshold: float = 0.85,
    fuzzy_match: bool = True,
    handle_unmatched: HandleUnmatched = "ignore",
    fill_value: Any = None,
    fill_mapping: dict[str, Any] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """
    Validate and correct dictionary keys based on expected keys using string similarity.

    Args:
        d_: The dictionary to validate and correct keys for.
        keys: List of expected keys or dictionary mapping keys to types.
        similarity_algo: String similarity algorithm to use or custom function.
        similarity_threshold: Minimum similarity score for fuzzy matching.
        fuzzy_match: If True, use fuzzy matching for key correction.
        handle_unmatched: Specifies how to handle unmatched keys:
            - "ignore": Keep unmatched keys in output.
            - "raise": Raise ValueError if unmatched keys exist.
            - "remove": Remove unmatched keys from output.
            - "fill": Fill unmatched keys with default value/mapping.
            - "force": Combine "fill" and "remove" behaviors.
        fill_value: Default value for filling unmatched keys.
        fill_mapping: Dictionary mapping unmatched keys to default values.
        strict: If True, raise ValueError if any expected key is missing.

    Returns:
        A new dictionary with validated and corrected keys.

    Raises:
        ValueError: If validation fails based on specified parameters.
        TypeError: If input types are invalid.
        AttributeError: If key validation fails.
    """
    # Input validation
    if not isinstance(d_, dict):
        raise TypeError("First argument must be a dictionary")
    if keys is None:
        raise TypeError("Keys argument cannot be None")
    # A bare str is a Sequence[str] but iterating it yields single characters,
    # not key names.  Reject it early with a clear message rather than silently
    # producing wrong results.
    if isinstance(keys, str):
        raise TypeError(
            "keys must be a Mapping (dict) or a non-string Sequence of key names "
            "(e.g. list, tuple, frozenset); got a bare str"
        )
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between 0.0 and 1.0")

    # Extract expected keys: Mapping types expose their keys via .keys();
    # all other Sequence types (list, tuple, frozenset, …) are iterable directly.
    fields_set = set(keys.keys()) if isinstance(keys, Mapping) else set(keys)
    if not fields_set:
        return d_.copy()  # Return copy of original if no expected keys

    # Initialize output dictionary and tracking sets
    corrected_out = {}
    matched_expected = set()
    matched_input = set()

    # Get similarity function
    if isinstance(similarity_algo, str):
        if similarity_algo not in SIMILARITY_ALGO_MAP:
            raise ValueError(f"Unknown similarity algorithm: {similarity_algo}")
        similarity_func = SIMILARITY_ALGO_MAP[similarity_algo]
    else:
        similarity_func = similarity_algo

    # First pass: exact matches
    for key in d_:
        if key in fields_set:
            corrected_out[key] = d_[key]
            matched_expected.add(key)
            matched_input.add(key)

    # Second pass: fuzzy matching if enabled
    if fuzzy_match:
        remaining_input = set(d_.keys()) - matched_input
        remaining_expected = fields_set - matched_expected

        for key in remaining_input:
            if not remaining_expected:
                break

            matches = string_similarity(
                key,
                list(remaining_expected),
                algorithm=similarity_func,
                threshold=similarity_threshold,
                return_most_similar=True,
            )

            if matches:
                match = matches
                corrected_out[match] = d_[key]
                matched_expected.add(match)
                matched_input.add(key)
                remaining_expected.remove(match)
            elif handle_unmatched == "ignore":
                corrected_out[key] = d_[key]

    # Handle unmatched keys based on handle_unmatched parameter
    unmatched_input = set(d_.keys()) - matched_input
    unmatched_expected = fields_set - matched_expected

    if handle_unmatched == "raise" and unmatched_input:
        raise ValueError(f"Unmatched keys found: {unmatched_input}")

    elif handle_unmatched == "ignore":
        for key in unmatched_input:
            corrected_out[key] = d_[key]

    elif handle_unmatched in ("fill", "force"):
        # Fill missing expected keys
        for key in unmatched_expected:
            if fill_mapping and key in fill_mapping:
                corrected_out[key] = fill_mapping[key]
            else:
                corrected_out[key] = fill_value

        # For "fill" mode, also keep unmatched original keys
        if handle_unmatched == "fill":
            for key in unmatched_input:
                corrected_out[key] = d_[key]

    # Check strict mode
    if strict and unmatched_expected:
        raise ValueError(f"Missing required keys: {unmatched_expected}")

    return corrected_out


@dataclass(slots=True, init=False, frozen=True)
class FuzzyMatchKeysParams(Params):
    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=False)
    _func: ClassVar[Any] = fuzzy_match_keys

    similarity_algo: SIMILARITY_TYPE | SimilarityFunc = "jaro_winkler"
    similarity_threshold: float = 0.85

    fuzzy_match: bool = True
    handle_unmatched: HandleUnmatched = "ignore"

    fill_value: Any = Unset
    fill_mapping: dict[str, Any] | Any = Unset
    strict: bool = False

    def __call__(self, d_: dict[str, Any], keys: KeysLike) -> dict[str, Any]:
        return fuzzy_match_keys(d_, keys, **self.default_kw())
