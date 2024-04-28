from typing import Tuple
from lionagi.libs import convert


def parse_tool_response(response: dict) -> Tuple[str, dict]:
    try:
        func = response["action"][7:]
        args = convert.to_dict(response["arguments"])
        return func, args
    except Exception:
        try:
            func = response["recipient_name"].split(".")[-1]
            args = response["parameters"]
            return func, args
        except:
            raise ValueError("response is not a valid function call")
