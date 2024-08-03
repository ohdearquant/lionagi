from enum import Enum
from pydantic import Field
from lion_core.libs import to_dict
from lionagi.os.primitives import Form


class UnitForm(Form):
    """
    A base form class for units that includes fields for confidence scoring and reasoning.

    Attributes:
        template_name (str): The name of the template.
        confidence_score (float): A numeric confidence score between 0 and 1 with precision to 2 decimal places.
        reason (str | None): A field for providing concise reasoning for the process.
    """

    template_name: str = "UnitDirective"

    confidence_score: float = Field(
        None,
        description=(
            "Provide a numeric confidence score on how likely you successfully achieved the task  between 0 and 1, with precision in 2 decimal places. 1 being very confident in a good job, 0 is not confident at all."
        ),
        validation_kwargs={
            "upper_bound": 1,
            "lower_bound": 0,
            "num_type": float,
            "precision": 2,
        },
    )

    reason: str | None = Field(
        None,
        description=(
            "Explain yourself, provide concise reasoning for the process. Must start with: Let's think step by step, "
        ),
    )

    actions: dict | None = Field(
        None,
        description=(
            "Actions to take based on the context and instruction. "
            "Format: {action_n: {function: ..., arguments: {param1: ..., "
            "param2: ...}}}. Leave blank if no actions are needed. "
            "Must use provided functions and parameters, DO NOT MAKE UP NAMES!!! "
            "Flag `action_required` as True if filled. When providing parameters, "
            "you must follow the provided type and format. If the parameter is a "
            "number, you should provide a number like 1, 23, or 1.1 if float is "
            "allowed."
        ),
        examples=["{action_1: {function: 'add', arguments: {num1: 1, num2: 2}}}"],
    )

    action_required: bool | None = Field(
        None,
        description="Set to True if you provide actions, or provide actions if True.",
        examples=[True, False],
    )

    answer: str | None = Field(
        None,
        description=(
            "Adhere to the prompt and all user instructions. Provide the answer "
            "for the task. if actions are required at this step, set "
            "`action_required` to True and write only `PLEASE_ACTION` to the answer_field."
            "Additionally, if extensions are allowed and needed at this step to provide a"
            " high-quality, accurate answer, set extension_required to True and"
            "you will have another chance to provide the answer after the actions are done.",
        ),
    )

    extension_required: bool | None = Field(
        None,
        description=(
            "Set to True if more steps are needed to provide an accurate answer. "
            "If True, additional rounds are allowed."
        ),
        examples=[True, False],
    )

    prediction: None | str = Field(
        None,
        description="Provide the likely prediction based on context and instruction.",
    )

    plan: dict | str | None = Field(
        None,
        description=(
            "Provide a step-by-step plan. Format: {step_n: {plan: ..., reason: ...}}. "
            "Achieve the final answer at the last step. Set `extend_required` to True "
            "if plan requires more steps."
        ),
        examples=["{step_1: {plan: '...', reason: '...'}}"],
    )

    next_steps: dict | str | None = Field(
        None,
        description=(
            "Brainstorm ideas on next actions to take. Format: {next_step_n: {plan: "
            "..., reason: ...}}. Next_step is about anticipating future actions, "
            "but it does not have to be in a sequential format. Set `extend_required` "
            "to True if more steps are needed."
        ),
        examples=["{next_step_1: {plan: '...', reason: '...'}}"],
    )

    score: float | None = Field(
        None,
        description=(
            "A numeric score. Higher is better. If not otherwise instructed, "
            "fill this field with your own performance rating. Try hard and be "
            "self-critical."
        ),
        examples=[0.2, 5, 2.7],
    )

    reflection: str | None = Field(
        None,
        description=(
            "Reason your own reasoning. Create specific items on how/where/what "
            "you could improve to better achieve the task, or can the problem be "
            "solved in a different and better way. If you can think of a better "
            "solution, please provide it and fill the necessary fields in "
            "`action_required`, `extension_required`, `next_steps` if appropriate."
        ),
    )

    selection: Enum | str | list | None = Field(
        None, description="a single item from the choices."
    )

    tool_schema: list | dict | None = Field(
        None, description="The list of tools available for using."
    )

    assignment: str = Field("task -> answer")

    is_extension: bool = Field(False)

    def __init__(
        self,
        instruction=None,
        context=None,
        reason: bool = True,
        predict: bool = False,
        score=True,
        select=None,
        plan=None,
        brainstorm=None,
        reflect=None,
        tool_schema=None,
        allow_action: bool = False,
        allow_extension: bool = False,
        max_extension: int = None,
        confidence=None,
        score_num_digits=None,
        score_range=None,
        select_choices=None,
        plan_num_step=None,
        predict_num_sentences=None,
        **kwargs,
    ):
        """
        Initialize the UnitForm with various parameters and settings.

        Args:
            instruction (str|None): Additional instruction.
            context (str|None): Additional context.
            reason (bool): Flag to include reasoning.
            predict (bool): Flag to include prediction.
            score (bool): Flag to include score.
            select (Enum|str|list|None): Selection choices.
            plan (dict|str|None): Step-by-step plan.
            brainstorm (bool): Flag to include brainstorming next steps.
            reflect (bool): Flag to include self-reflection.
            tool_schema (list|dict|None): Schema of available tools.
            allow_action (bool): Allow actions to be added.
            allow_extension (bool): Allow extension for more steps.
            max_extension (int|None): Maximum number of extensions allowed.
            confidence (bool|None): Flag to include confidence score.
            score_num_digits (int|None): Number of decimal places for the score.
            score_range (list|None): Range for the score.
            select_choices (list|None): Choices for selection.
            plan_num_step (int|None): Number of steps in the plan.
            predict_num_sentences (int|None): Number of sentences for prediction.
            **kwargs: Additional keyword arguments.
        """
        super().__init__(**kwargs)

        self.task = (
            f"Follow the prompt and provide the necessary output.\n"
            f"- Additional instruction: {str(instruction or 'N/A')}\n"
            f"- Additional context: {str(context or 'N/A')}\n"
        )

        if reason:
            self.append_to_request("reason")

        if allow_action:
            self.append_to_request("actions")
            self.append_to_request("action_required")
            self.append_to_request("reason")
            self.task += "- Reason and prepare actions with GIVEN TOOLS ONLY.\n"

        if allow_extension:
            self.append_to_request("extension_required")
            self.task += (
                f"- Allow auto-extension up to another {max_extension} rounds.\n"
            )

        if tool_schema:
            self.append_to_input("tool_schema")
            self.tool_schema = tool_schema

        if brainstorm:
            self.append_to_request("next_steps")
            self.append_to_request("extension_required")
            self.task += "- Explore ideas on next actions to take.\n"

        if plan:
            plan_num_step = plan_num_step or 3
            max_extension = max_extension or plan_num_step
            allow_extension = True
            self.append_to_request("plan")
            self.append_to_request("extension_required")
            self.task += (
                f"- Generate a {plan_num_step}-step plan based on the context.\n"
            )

        if predict:
            self.append_to_request("prediction")
            self.task += (
                f"- Predict the next {predict_num_sentences or 1} sentence(s).\n"
            )

        if select:
            self.append_to_request("selection")
            self.task += (
                f"- Select 1 item from the provided choices: {select_choices}.\n"
            )

        if confidence:
            self.append_to_request("confidence_score")

        if score:
            self.append_to_request("score")

            score_range = score_range or [0, 10]
            score_num_digits = score_num_digits or 0

            self.validation_kwargs["score"] = {
                "upper_bound": score_range[1],
                "lower_bound": score_range[0],
                "num_type": int if score_num_digits == 0 else float,
                "precision": score_num_digits if score_num_digits != 0 else None,
            }

            self.task += (
                f"- Give a numeric score in [{score_range[0]}, {score_range[1]}] "
                f"and precision of {score_num_digits or 0} decimal places.\n"
            )

        if reflect:
            self.append_to_request("reflection")

    def display(self):
        """
        Display the current form fields and values in a user-friendly format.
        """
        fields = self.work_fields.copy()

        if "task" in fields and len(str(fields["task"])) > 2000:
            fields["task"] = fields["task"][:2000] + "..."

        if "tool_schema" in fields:
            tools = to_dict(fields["tool_schema"])["tools"]
            fields["available_tools"] = [i["function"]["name"] for i in tools]
            fields.pop("tool_schema")

        if "actions" in fields:
            a = ""
            idx = 0
            for _, v in fields["actions"].items():
                a += (
                    f"\n \n{idx+1}. **{v['function']}**"
                    f"({', '.join([f'{k}: {v}' for k, v in v['arguments'].items()])}), "
                )
                idx += 1
            fields["actions"] = a[:-2]

        if "action_response" in fields:
            a = ""
            idx = 0
            for _, v in fields["action_response"].items():
                a += (
                    f"\n \n{idx+1}. **{v['function']}**"
                    f"({', '.join([f'{k}: {v}' for k, v in v['arguments'].items()])})"
                )
                if len(str(v["output"])) > 30:
                    a += f" \n \n {v['output']}, "
                else:
                    a += f" = {v['output']}, "
                idx += 1
            fields["action_response"] = a[:-2]

        super().display(fields=fields)
