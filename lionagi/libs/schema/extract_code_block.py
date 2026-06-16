# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import re


def extract_code_block(
    str_to_parse: str,
    return_as_list: bool = False,
    languages: list[str] | None = None,
    categorize: bool = False,
) -> str | list[str] | dict[str, list[str]]:
    """Extract fenced ``` or ~~~ code blocks; return_as_list/categorize/languages control output form."""
    code_blocks = []
    code_dict = {}

    pattern = re.compile(
        r"""
        ^(?P<fence>```|~~~)[ \t]*     # Opening fence ``` or ~~~
        (?P<lang>[\w+-]*)[ \t]*\n     # Optional language identifier
        (?P<code>.*?)(?<=\n)          # Code content
        ^(?P=fence)[ \t]*$            # Closing fence matching the opening
        """,
        re.MULTILINE | re.DOTALL | re.VERBOSE,
    )

    for match in pattern.finditer(str_to_parse):
        lang = match.group("lang") or "plain"
        code = match.group("code")

        if languages is None or lang in languages:
            if categorize:
                code_dict.setdefault(lang, []).append(code)
            else:
                code_blocks.append(code)

    if categorize:
        return code_dict
    elif return_as_list:
        return code_blocks
    else:
        return "\n\n".join(code_blocks)
