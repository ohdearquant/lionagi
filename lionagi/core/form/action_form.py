from typing import Any
from lionagi.integrations.bridge.pydantic_.pydantic_bridge import Field

from lionagi.core.form.scored_form import ScoredForm


class ActionRequest: ...


class ActionForm(ScoredForm):

    action_needed: bool | None = Field(
        False, description="true if actions are needed else false"
    )

    actions: list[dict | ActionRequest | Any] | None = Field(
        default_factory=list,
        description="""provide The list of action(s) to take, each action in {"function": function_name, "arguments": {param1:..., param2:..., ...}}. Leave blank if no further actions are needed, you must use provided parameters for each action, DO NOT MAKE UP KWARG NAME!!!""",
    )

    answer: str | dict | Any | None = Field(
        default_factory=str,
        description="output answer to the questions asked if further actions are not needed, leave blank if an accurate answer cannot be provided from context during this step",
    )

    signature: str = "sentence -> reason, action_needed, actions, answer"
