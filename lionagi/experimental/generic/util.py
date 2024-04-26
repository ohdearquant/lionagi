from lionagi.libs import convert


system_fields = [
    "id_",
    "node_id",
    "meta",
    "metadata",
    "timestamp",
    "content",
    "assignment",
    "assignments",
    "task",
    "template_name",
    "version",
    "description",
    "in_validation_kwargs",
    "out_validation_kwargs",
    "fix_input",
    "fix_output",
    "input_fields",
    "output_fields",
    "choices",
    "prompt_fields",
    "prompt_fields_annotation",
    "instruction_context",
    "instruction",
    "instruction_output_fields",
    "inputs",
    "outputs",
    "process",
    "_validate_field",
    "_process_input",
    "_process_response",
    "_validate_field_choices",
    "_validate_input_choices",
    "_validate_output_choices",
]


def get_input_output_fields(str_: str) -> list[list[str]]:

    inputs, outputs = str_.split("->")

    input_fields = [convert.strip_lower(i) for i in inputs.split(",")]
    output_fields = [convert.strip_lower(o) for o in outputs.split(",")]

    return input_fields, output_fields
