from typing import Any, Literal

from lion_core.setting import LN_UNDEFINED
from lion_core.unit.unit import UnitProcessor as CoreProcessor
from lion_core.libs import rcall

from lionagi.os.primitives import Instruction, ActionRequest, System, Form
from lionagi.os.session.branch.branch import Branch
from lionagi.os.operator.imodel.imodel import iModel
from lionagi.os.operator.validator.validator import Validator
from lionagi.os.operator.validator.rulebook import RuleBook
from lionagi.os.operator.processor.unit.utils import retry_kwargs
from lionagi.os.operator.processor.unit.process_chat import process_chat
from lionagi.os.operator.processor.unit.process_direct import process_direct


class UnitProcessor(CoreProcessor):
    """
    A processor for handling unit operations in the Lion framework.

    This class provides methods for chatting and direct processing,
    with support for various configurations and retry mechanisms.
    """

    def __init__(
        self,
        branch: Branch,
        imodel: iModel = None,
        rulebook: RuleBook = None,
        verbose: bool = True,
        **kwargs,
    ):
        self.branch = branch
        self.imodel = imodel or branch.imodel
        self.validator = Validator(rulebook=rulebook) if rulebook else Validator()
        self.verbose = verbose
        self.retry_kwargs = kwargs or retry_kwargs

    async def chat(
        self,
        branch: Branch,
        *,
        form=None,
        sender=None,
        recipient=None,
        instruction: Any = None,
        context: Any = None,
        request_fields=None,
        system: Any = None,
        action_request: ActionRequest | None = None,
        imodel=None,
        images=None,
        image_path=None,
        image_detail: Literal["low", "high", "auto"] = None,
        system_datetime: bool | str | None = None,
        metadata: Any = None,
        delete_previous_system: bool = False,
        tools: bool | None = None,
        system_metadata: Any = None,
        model_config: dict | None = None,
        clear_messages: bool = False,
        fill_value: Any = None,
        fill_mapping: dict[str, Any] | None = None,
        validator: Validator | None = None,
        rulebook=None,
        strict_validation: bool = False,
        use_annotation: bool = True,
        return_branch: bool = False,
        retries: int = 0,
        delay: float = 0,
        backoff_factor: float = 1,
        default: Any = LN_UNDEFINED,
        timeout: float | None = None,
        timing: bool = False,
        **kwargs: Any,
    ) -> Any:
        """
        Perform a chat operation with retry capabilities.

        Args:
            branch: The branch to use for the chat. If None, uses the processor's branch.
            form: The form to process in the chat.
            clear_messages: Whether to clear existing messages before chatting.
            system: System message or configuration.
            system_metadata: Additional metadata for the system message.
            system_datetime: Datetime for the system message.
            delete_previous_system: Whether to delete the previous system message.
            instruction: Instruction for the chat.
            context: Additional context for the chat.
            action_request: Action request for the chat.
            image: Image data for the chat.
            image_path: Path to an image file.
            sender: Sender of the message.
            recipient: Recipient of the message.
            requested_fields: Fields requested in the response.
            metadata: Additional metadata for the instruction.
            tools: Whether to include tools in the configuration.
            invoke_tool: Whether to invoke tools for action requests.
            model_config: Additional model configuration.
            imodel: The iModel to use for chat completion.
            handle_unmatched: Strategy for handling unmatched fields.
            fill_value: Value to use for filling unmatched fields.
            fill_mapping: Mapping for filling unmatched fields.
            validator: The validator to use for form validation.
            rulebook: Optional rulebook for validation.
            strict_validation: Whether to use strict validation.
            use_annotation: Whether to use annotation for validation.
            return_branch: Whether to return the branch along with the result.
            retries: Number of retries for the operation.
            delay: Delay between retries.
            backoff_factor: Factor to increase delay between retries.
            default: Default value to return if all retries fail.
            timeout: Timeout for the operation.
            timing: Whether to time the operation.
            **kwargs: Additional keyword arguments for the chat process.

        Returns:
            The result of the chat operation.
        """
        kwargs = {
            **retry_kwargs,
            **{
                "retries": retries,
                "delay": delay,
                "backoff_factor": backoff_factor,
                "default": default,
                "timeout": timeout,
                "timing": timing,
            },
        }

        kwargs = {**self.retry_kwargs, **kwargs}
        return await rcall(
            process_chat,
            branch=branch or self.branch,
            form=form,
            clear_messages=clear_messages,
            system=system,
            system_metadata=system_metadata,
            system_datetime=system_datetime,
            delete_previous_system=delete_previous_system,
            instruction=instruction,
            context=context,
            action_request=action_request,
            images=images,
            image_path=image_path,
            image_detail=image_detail,
            sender=sender,
            recipient=recipient,
            request_fields=request_fields,
            metadata=metadata,
            tools=tools,
            model_config=model_config,
            imodel=imodel or self.imodel or branch.imodel,
            fill_value=fill_value,
            fill_mapping=fill_mapping,
            validator=validator or self.validator,
            rulebook=rulebook,
            strict_validation=strict_validation,
            use_annotation=use_annotation,
            return_branch=return_branch,
            **kwargs,
        )

    async def direct(
        self,
        branch: Branch = None,
        form: Form = None,
        instruction: str | None = None,
        context: dict[str, Any] | None = None,
        tools: Any = None,
        reason: bool = False,
        predict: bool = False,
        score: bool = False,
        select: Any = None,
        plan: Any = None,
        brainstorm: Any = None,
        reflect: Any = None,
        tool_schema: Any = None,
        allow_action: bool = False,
        allow_extension: bool = False,
        max_extension: int | None = None,
        confidence: Any = None,
        score_num_digits: int | None = None,
        score_range: tuple[float, float] | None = None,
        select_choices: list[str] | None = None,
        plan_num_step: int | None = None,
        predict_num_sentences: int | None = None,
        clear_messages: bool = False,
        verbose_direct: bool = True,
        image: str | list[str] | None = None,
        image_path: str | None = None,
        return_branch: bool = False,
        retries: int = 0,
        delay: float = 0,
        backoff_factor: float = 1,
        default: Any = LN_UNDEFINED,
        timeout: float | None = None,
        timing: bool = False,
        verbose=True,
        **kwargs: Any,
    ) -> Any:
        """
        Perform a direct processing operation with retry capabilities.

        Args:
            branch: The branch to use for processing.
            form: The form to process.
            instruction: The instruction for processing.
            context: Additional context for processing.
            tools: Tools to use in processing.
            reason: Whether to include reasoning.
            predict: Whether to include prediction.
            score: Whether to include scoring.
            select: Selection criteria.
            plan: Planning information.
            brainstorm: Brainstorming information.
            reflect: Reflection information.
            tool_schema: Schema for the tools.
            allow_action: Whether to allow actions.
            allow_extension: Whether to allow extensions.
            max_extension: Maximum number of extensions allowed.
            confidence: Confidence level.
            score_num_digits: Number of digits for scoring.
            score_range: Range for scoring.
            select_choices: Choices for selection.
            plan_num_step: Number of steps in the plan.
            predict_num_sentences: Number of sentences for prediction.
            clear_messages: Whether to clear messages before processing.
            verbose: Whether to enable verbose output.
            image: Image data for processing.
            image_path: Path to an image file.
            return_branch: Whether to return the branch along with the result.
            retries: Number of retries for the operation.
            delay: Delay between retries.
            backoff_factor: Factor to increase delay between retries.
            default: Default value to return if all retries fail.
            timeout: Timeout for the operation.
            timing: Whether to time the operation.
            **kwargs: Additional keyword arguments for processing.

        Returns:
            The result of the direct processing operation.
        """
        kwargs = {
            **retry_kwargs,
            **{
                "retries": retries,
                "delay": delay,
                "backoff_factor": backoff_factor,
                "default": default,
                "timeout": timeout,
                "timing": timing,
            },
        }

        kwargs = {**self.retry_kwargs, **kwargs}
        return await rcall(
            process_direct,
            branch=branch or self.branch,
            form=form,
            instruction=instruction,
            context=context,
            tools=tools,
            reason=reason,
            predict=predict,
            score=score,
            select=select,
            plan=plan,
            brainstorm=brainstorm,
            reflect=reflect,
            tool_schema=tool_schema,
            allow_action=allow_action,
            allow_extension=allow_extension,
            max_extension=max_extension,
            confidence=confidence,
            score_num_digits=score_num_digits,
            score_range=score_range,
            select_choices=select_choices,
            plan_num_step=plan_num_step,
            predict_num_sentences=predict_num_sentences,
            clear_messages=clear_messages,
            verbose_direct=verbose_direct,
            image=image,
            image_path=image_path,
            return_branch=return_branch,
            verbose=verbose,
            **kwargs,
        )
