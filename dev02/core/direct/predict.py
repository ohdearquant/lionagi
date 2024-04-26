"""
This module contains the PredictTemplate class for predicting the next sentence(s) based on a given sentence.

The PredictTemplate class is a subclass of ScoredTemplate and provides functionality for predicting the next sentence(s)
using a language model. It includes fields for the input sentence, number of sentences to predict, predicted answer,
confidence score, and reason for the prediction.
"""

from lionagi.libs import func_call
from lionagi.integrations.bridge.pydantic_.pydantic_bridge import Field

from lionagi.core.form.scored_form import ScoredForm
from lionagi.core.branch.branch import Branch


class PredictTemplate(ScoredForm):
    """
    A class for predicting the next sentence(s) based on a given sentence.

    Attributes:
        template_name (str): The name of the predict template (default: "default_predict_template").
        sentence (str | list | dict): The given sentence(s) to predict.
        num_sentences (int): The number of sentences to predict.
        answer (str | list): The predicted sentence(s).
        signature (str): The signature indicating the input and output fields (default: "sentence -> answer").

    Methods:
        __init__(self, sentence=None, num_sentences=None, confidence_score=False, reason=False, **kwargs):
            Initializes a new instance of the PredictTemplate class.

        async predict(sentence=None, num_sentences=1, confidence_score=False, reason=False, retries=2,
                       delay=0.5, backoff_factor=2, default_value=None, timeout=None, branch_name=None,
                       system=None, messages=None, service=None, sender=None, llmconfig=None, tools=None,
                       datalogger=None, persist_path=None, tool_manager=None, **kwargs) -> PredictTemplate:
            Predicts the next sentence(s) based on the given sentence using a language model.
    """

    template_name: str = "default_predict"
    sentence: str | list | dict = Field(
        default_factory=str, description="the given sentence(s) to predict"
    )
    num_sentences: int = Field(
        default_factory=int, description="the number of sentences to predict"
    )
    answer: str | list = Field(
        default_factory=str, description="the predicted sentence(s) or desired output"
    )
    signature: str = "sentence -> answer"

    def __init__(
        self,
        sentence=None,
        instruction=None,
        num_sentences=1,
        confidence_score=False,
        reason=False,
        **kwargs,
    ):
        """
        Initializes a new instance of the PredictTemplate class.

        Args:
            sentence (Optional[str | list | dict]): The given sentence(s) to predict.
            num_sentences (Optional[int]): The number of sentences to predict.
            confidence_score (bool): Whether to include the confidence score in the output (default: False).
            reason (bool): Whether to include the reason for the prediction in the output (default: False).
            **kwargs: Additional keyword arguments.
        """
        super().__init__(**kwargs)

        self.sentence = sentence or ""
        self.num_sentences = num_sentences
        self.task = f"follow instruction to predict the next {self.num_sentences} sentence(s). Instruction: {instruction}."

        if reason:
            self.output_fields.append("reason")

        if confidence_score:
            self.output_fields.append("confidence_score")


async def predict(
    sentence=None,
    num_sentences=1,
    confidence_score=False,
    instruction=None,
    branch=None,
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
) -> "PredictTemplate":
    """
    Predicts the next sentence(s) based on the given sentence using a language model.

    Args:
        sentence (Optional[str | list | dict]): The given sentence(s) to predict.
        num_sentences (int): The number of sentences to predict (default: 1).
        confidence_score (bool): Whether to include the confidence score in the output (default: False).
        reason (bool): Whether to include the reason for the prediction in the output (default: False).
        retries (int): The number of retries for the API call (default: 2).
        delay (float): The initial delay between retries in seconds (default: 0.5).
        backoff_factor (float): The backoff factor for exponential delay between retries (default: 2).
        default_value (Optional[Any]): The default value to return if the API call fails (default: None).
        timeout (Optional[float]): The timeout for the API call in seconds (default: None).
        branch_name (Optional[str]): The name of the branch to use for prediction.
        system (Optional[Any]): The system configuration for the branch.
        messages (Optional[Any]): The messages to initialize the branch with.
        service (Optional[Any]): The service to use for prediction.
        sender (Optional[str]): The sender of the prediction request.
        llmconfig (Optional[Any]): The configuration for the language model.
        tools (Optional[Any]): The tools to use for prediction.
        datalogger (Optional[Any]): The data logger for the branch.
        persist_path (Optional[str]): The path to persist the branch data.
        tool_manager (Optional[Any]): The tool manager for the branch.
        **kwargs: Additional keyword arguments for the API call.

    Returns:
        PredictTemplate: The predict template with the predicted sentence(s).
    """
    branch = branch or Branch(
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

    predict_template = PredictTemplate(
        instruction=instruction,
        sentence=sentence,
        num_sentences=num_sentences,
        confidence_score=confidence_score,
        reason=reason,
    )

    await func_call.rcall(
        branch.chat,
        form=predict_template,
        retries=retries,
        delay=delay,
        backoff_factor=backoff_factor,
        default=default_value,
        timeout=timeout,
        **kwargs,
    )

    return predict_template
