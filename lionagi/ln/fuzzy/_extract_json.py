import logging
import re
from typing import Any

import orjson

from ._fuzzy_json import MAX_JSON_INPUT_SIZE, fuzzy_json

logger = logging.getLogger(__name__)

_JSON_BLOCK_PATTERN = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def extract_json(
    input_data: str | list[str],
    /,
    *,
    fuzzy_parse: bool = False,
    return_one_if_single: bool = True,
    max_size: int = MAX_JSON_INPUT_SIZE,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Parse JSON directly or extract from ```json blocks; fuzzy_parse applies fuzzy_json on failures."""
    if isinstance(input_data, list):
        for item in input_data:
            if len(item) > max_size:
                raise ValueError(
                    f"Input size ({len(item)} bytes) exceeds maximum ({max_size} bytes)"
                )

    if isinstance(input_data, list):
        input_str = "\n".join(input_data)
    else:
        input_str = input_data

    if len(input_str) > max_size:
        raise ValueError(f"Input size ({len(input_str)} bytes) exceeds maximum ({max_size} bytes)")

    # 1. Try direct parsing
    try:
        if fuzzy_parse:
            return fuzzy_json(input_str)
        return orjson.loads(input_str)
    except Exception:
        logger.debug("Direct JSON parse failed; falling back to markdown block extraction.")

    # 2. Attempt extracting JSON blocks from markdown
    matches = _JSON_BLOCK_PATTERN.findall(input_str)
    if not matches:
        return []

    if return_one_if_single and len(matches) == 1:
        data_str = matches[0]
        try:
            if fuzzy_parse:
                return fuzzy_json(data_str)
            return orjson.loads(data_str)
        except Exception:
            return []

    # Multiple matches
    results = []
    for m in matches:
        try:
            if fuzzy_parse:
                results.append(fuzzy_json(m))
            else:
                results.append(orjson.loads(m))
        except Exception:  # noqa: S112  # intentional parser fallback: skip unparseable JSON blocks, not an error
            continue
    return results
