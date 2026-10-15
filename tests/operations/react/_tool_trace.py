"""Script only model replies while exercising real ReAct, parsing and tools."""

import json

from lionagi.testing import TestBranch


def tool_branch(rounds, tools, answer):
    responses = [
        json.dumps({"analysis": "Use the requested tools", "extension_needed": False, **data})
        for data in rounds
    ]
    responses.append(json.dumps({"answer": answer}))
    return TestBranch.from_text(responses, tools=tools)
