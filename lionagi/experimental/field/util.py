from typing import Any
from lionagi.libs import convert

def get_input_output_fields(str_: str) -> Any:
    inputs, outputs = str_.split("->")

    input_fields = [convert.strip_lower(i) for i in inputs.split(",")]
    output_fields = [convert.strip_lower(o) for o in outputs.split(",")]

    return input_fields, output_fields