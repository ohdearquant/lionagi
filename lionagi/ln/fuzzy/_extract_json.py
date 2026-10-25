import logging
import re
from typing import Any

import orjson

from ._fuzzy_json import MAX_JSON_INPUT_SIZE, fuzzy_json

logger = logging.getLogger(__name__)

# A ```json block whose opening and closing fences each stand on a line of their own, after any
# indentation, the closing one at least as long as the opening one. A fence inside a JSON string
# never starts a line, since a JSON string holds its newlines as escapes, so text that writes
# a fenced block inside an argument value does not end the block early.
_JSON_FENCED_BLOCK_PATTERN = re.compile(
    r"^[ \t]*(`{3,})json[ \t]*\r?\n(.*?)\r?\n[ \t]*\1`*[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
# Any ```json ... ``` span, a fence on the same line included. Read in place beside the blocks
# above where it lies outside them, and over the whole text when none of those parses.
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
    fenced = [
        (m.start(), m.end(), m.group(2)) for m in _JSON_FENCED_BLOCK_PATTERN.finditer(input_str)
    ]
    spans = [(m.start(), m.end(), m.group(1)) for m in _JSON_BLOCK_PATTERN.finditer(input_str)]
    beside = [
        span
        for span in spans
        if not any(span[0] < end and start < span[1] for start, end, _ in fenced)
    ]
    passes = [[(block, True) for _, _, block in spans]]
    if fenced:
        # A span beside the blocks counts toward a single result only when it parses, so a
        # stray span that is not JSON leaves the result the blocks alone give.
        merged = sorted(
            [(*block, True) for block in fenced] + [(*span, False) for span in beside],
            key=lambda item: item[0],
        )
        passes.insert(0, [(block, counts_unparsed) for _, _, block, counts_unparsed in merged])
    for matches in passes:
        results = []
        found = 0
        for m, counts_unparsed in matches:
            try:
                if fuzzy_parse:
                    results.append(fuzzy_json(m))
                else:
                    results.append(orjson.loads(m))
            except Exception:  # noqa: S112  # intentional parser fallback: skip unparseable JSON blocks, not an error
                if counts_unparsed:
                    found += 1
                continue
            found += 1
        if results:
            if return_one_if_single and found == 1:
                return results[0]
            return results
    return []
