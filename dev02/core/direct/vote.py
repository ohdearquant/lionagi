"""
This module contains the vote function for generating and scoring multiple outputs and selecting the top-ranked ones.

The vote function generates multiple outputs using a specified directive function (default: predict), scores each output
using the score function, and returns the top-ranked output(s) based on the scores. It allows for customization of the
number of generations, number of outputs to return, number of scorers, score range, and scorer instruction.
"""

from lionagi.libs import func_call
import numpy as np
from lionagi.core.direct.predict import predict
from lionagi.core.direct.score import score


async def vote(
    sentence,
    directive=predict,
    num_generations=5,
    num_output=1,
    num_scorer=5,
    score_range=(0, 100),
    num_digit=2,
    scorer_instruction=None,
    **kwargs,
):
    """
    Generates and scores multiple outputs and returns the top-ranked output(s).

    Args:
        sentence (str): The input sentence or context.
        directive (function): The function used to generate outputs (default: predict).
        num_generations (int): The number of outputs to generate (default: 5).
        num_output (int): The number of top-ranked outputs to return (default: 1).
        num_scorer (int): The number of scorers to use for scoring each output (default: 5).
        score_range (tuple): The range of scores to assign (default: (0, 100)).
        num_digit (int): The number of digits after the decimal point for scores (default: 2).
        scorer_instruction (str): The instruction for the scorers (default: None).
        **kwargs: Additional keyword arguments to pass to the directive function.

    Returns:
        The top-ranked output if num_output is 1, or a list of top-ranked outputs if num_output is greater than 1.
    """

    async def _inner(i):
        out_ = await directive(sentence, **kwargs)
        score_ = await score(
            out_.answer,
            context=sentence,
            instruction=scorer_instruction,
            score_range=score_range,
            num_digit=num_digit,
            num_instances=num_scorer,
            return_template=False,
        )

        out_.__setattr__("score", score_)
        return out_

    _outs = await func_call.alcall(list(range(num_generations)), _inner)

    top_index = np.argsort([i.score for i in _outs])[-num_output:]
    final_output = list(np.array(_outs)[top_index])

    return final_output[0] if len(final_output) == 1 else final_output
