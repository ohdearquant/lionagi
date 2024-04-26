"""
This module contains the ScoreTemplate class and related functions for scoring a given context using a language model.

The ScoreTemplate class is a subclass of ScoredTemplate and provides functionality for scoring a given context
based on specified instructions, score range, and other parameters. It includes fields for the input sentence,
score range, inclusive flag, number of digits, confidence score, and reason for the score.

The module also includes functions for scoring a single instance or multiple instances of the context using the
ScoreTemplate class and a language model.
"""

from pydantic import Field
import numpy as np
from lionagi.libs import func_call, convert
from lionagi.core.form.scored_form import ScoredForm
from lionagi.core.branch.branch import Branch


class ScoreTemplate(ScoredForm):
    """
    A class for scoring a given context using a language model.

    Attributes:
        template_name (str): The name of the score template (default: "default_score").
        sentence (str | list | dict): The given context to score.
        answer (float): The numeric score for the context.
        signature (str): The signature indicating the input and output fields (default: "sentence -> answer").

    Methods:
        __init__(self, sentence=None, instruction=None, score_range=(1, 10), inclusive=True, num_digit=0,
                 confidence_score=False, reason=False, **kwargs):
            Initializes a new instance of the ScoreTemplate class.
    """

    template_name: str = "default_score"
    sentence: str | list | dict = Field(
        default_factory=str, description="the given context to score"
    )
    answer: float = Field(default_factory=float, description=f"a numeric score")
    signature: str = "sentence -> answer"

    def __init__(
        self,
        sentence=None,
        instruction=None,
        score_range=(1, 10),
        inclusive=True,
        num_digit=0,
        confidence_score=False,
        reason=False,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.sentence = sentence

        return_precision = ""
        if num_digit == 0:
            return_precision = "integer"
        else:
            return_precision = f"num:{convert.to_str(num_digit)}f"

        self.task = f"""
score context according to the following constraints
1. objective, {convert.to_str(instruction)}
2. score range, {convert.to_str(score_range)}
3. include_endpoints, {"yes" if inclusive else "no"}
4. format the score in {return_precision}
"""

        if reason:
            self.output_fields.append("reason")

        if confidence_score:
            self.output_fields.append("confidence_score")

        self.out_validation_kwargs["answer"] = {
            "upper_bound": score_range[1],
            "lower_bound": score_range[0],
            "num_type": int if num_digit == 0 else float,
            "precision": num_digit if num_digit != 0 else None,
        }


async def _score(
    sentence,
    instruction=None,
    score_range=(1, 10),
    inclusive=True,
    num_digit=0,
    confidence_score=False,
    reason=False,
    retries=2,
    delay=0.5,
    backoff_factor=2,
    default_value=None,
    timeout=None,
    branch_name=None,
    system=None,
    messages=None,
    service=None,
    sender=None,
    llmconfig=None,
    tools=None,
    datalogger=None,
    persist_path=None,
    tool_manager=None,
    **kwargs,
):
    """
    Scores a given context using a language model.

    Args:
        sentence (str | list | dict): The given context to score.
        instruction (Optional[str]): The instruction for scoring the context.
        score_range (tuple): The range of valid scores (default: (1, 10)).
        inclusive (bool): Whether to include the endpoints of the score range (default: True).
        num_digit (int): The number of digits after the decimal point for the score (default: 0).
        confidence_score (bool): Whether to include the confidence score in the output (default: False).
        reason (bool): Whether to include the reason for the score in the output (default: False).
        retries (int): The number of retries for the API call (default: 2).
        delay (float): The initial delay between retries in seconds (default: 0.5).
        backoff_factor (float): The backoff factor for exponential delay between retries (default: 2).
        default_value (Optional[Any]): The default value to return if the API call fails (default: None).
        timeout (Optional[float]): The timeout for the API call in seconds (default: None).
        branch_name (Optional[str]): The name of the branch to use for scoring.
        system (Optional[Any]): The system configuration for the branch.
        messages (Optional[Any]): The messages to initialize the branch with.
        service (Optional[Any]): The service to use for scoring.
        sender (Optional[str]): The sender of the scoring request.
        llmconfig (Optional[Any]): The configuration for the language model.
        tools (Optional[Any]): The tools to use for scoring.
        datalogger (Optional[Any]): The data logger for the branch.
        persist_path (Optional[str]): The path to persist the branch data.
        tool_manager (Optional[Any]): The tool manager for the branch.
        **kwargs: Additional keyword arguments for the API call.

    Returns:
        ScoreTemplate: The score template with the scored context.
    """

    if "temperature" not in kwargs:
        kwargs["temperature"] = 0.1

    instruction = instruction or ""

    branch = Branch(
        name=branch_name,
        system=system,
        messages=messages,
        service=service,
        sender=sender,
        llmconfig=llmconfig,
        tools=tools,
        datalogger=datalogger,
        persist_path=persist_path,
        tool_manager=tool_manager,
    )

    _template = ScoreTemplate(
        sentence=sentence,
        instruction=instruction,
        score_range=score_range,
        inclusive=inclusive,
        num_digit=num_digit,
        confidence_score=confidence_score,
        reason=reason,
    )

    await func_call.rcall(
        branch.chat,
        form=_template,
        retries=retries,
        delay=delay,
        backoff_factor=backoff_factor,
        default=default_value,
        timeout=timeout,
        **kwargs,
    )

    return _template


async def score(
    sentence,
    *,
    num_instances=1,
    instruction=None,
    score_range=(1, 10),
    inclusive=True,
    num_digit=0,
    confidence_score=False,
    reason=False,
    retries=2,
    delay=0.5,
    backoff_factor=2,
    default_value=None,
    timeout=None,
    branch_name=None,
    system=None,
    messages=None,
    service=None,
    sender=None,
    llmconfig=None,
    tools=None,
    datalogger=None,
    persist_path=None,
    tool_manager=None,
    return_template=True,
    **kwargs,
) -> ScoreTemplate | float:
    """
    Scores a given context using a language model, with the option to score multiple instances.

    Args:
        sentence (str | list | dict): The given context to score.
        num_instances (int): The number of instances to score (default: 1).
        instruction (Optional[str]): The instruction for scoring the context.
        score_range (tuple): The range of valid scores (default: (1, 10)).
        inclusive (bool): Whether to include the endpoints of the score range (default: True).
        num_digit (int): The number of digits after the decimal point for the score (default: 0).
        confidence_score (bool): Whether to include the confidence score in the output (default: False).
        reason (bool): Whether to include the reason for the score in the output (default: False).
        retries (int): The number of retries for the API call (default: 2).
        delay (float): The initial delay between retries in seconds (default: 0.5).
        backoff_factor (float): The backoff factor for exponential delay between retries (default: 2).
        default_value (Optional[Any]): The default value to return if the API call fails (default: None).
        timeout (Optional[float]): The timeout for the API call in seconds (default: None).
        branch_name (Optional[str]): The name of the branch to use for scoring.
        system (Optional[Any]): The system configuration for the branch.
        messages (Optional[Any]): The messages to initialize the branch with.
        service (Optional[Any]): The service to use for scoring.
        sender (Optional[str]): The sender of the scoring request.
        llmconfig (Optional[Any]): The configuration for the language model.
        tools (Optional[Any]): The tools to use for scoring.
        datalogger (Optional[Any]): The data logger for the branch.
        persist_path (Optional[str]): The path to persist the branch data.
        tool_manager (Optional[Any]): The tool manager for the branch.
        return_template (bool): Whether to return the score template or only the score (default: True).
        **kwargs: Additional keyword arguments for the API call.

    Returns:
        ScoreTemplate | float: The score template with the scored context or the average score if `return_template` is False.
    """

    async def _inner(i=0):
        return await _score(
            sentence=sentence,
            instruction=instruction,
            score_range=score_range,
            inclusive=inclusive,
            num_digit=num_digit,
            confidence_score=confidence_score,
            reason=reason,
            retries=retries,
            delay=delay,
            backoff_factor=backoff_factor,
            default_value=default_value,
            timeout=timeout,
            branch_name=branch_name,
            system=system,
            messages=messages,
            service=service,
            sender=sender,
            llmconfig=llmconfig,
            tools=tools,
            datalogger=datalogger,
            persist_path=persist_path,
            tool_manager=tool_manager,
            **kwargs,
        )

    if num_instances == 1:
        _out = await _inner()
        return _out if return_template else _out.answer

    elif num_instances > 1:
        _outs = await func_call.alcall(range(num_instances), _inner)
        return _outs if return_template else np.mean([i.answer for i in _outs])
