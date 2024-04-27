import inspect
from lionagi.libs import convert, ParseUtil
from ..generic import BaseComponent
from ..message.base import BaseMessage, MessageRole


class ActionRequest(BaseMessage):
    function: str | None = None
    arguments: dict | None = None
    
    def __init__(
        self, 
        function=None, 
        arguments=None, 
        sender=None,                 # sender is the assistant
        executor=None,               # executor is the recipient
    ):
        
        super().__init__(
            role=MessageRole.ASSISTANT,
            sender=sender,
            recipient=(
                executor if isinstance(executor, str) 
                else executor.id_ if isinstance(executor, BaseComponent)
                else "executor"
            )
        )
        self.function=function if inspect.isfunction(function) else function.__name__
        self.arguments=self._prepare_arguments(arguments)
    

    def _prepare_arguments(self, arguments):
        if not isinstance(arguments, dict):
            try:
                arguments=ParseUtil.fuzzy_parse_json(convert.to_str(arguments))
            except Exception as e:
                raise ValueError(f"Invalid arguments: {e}") from e
        if isinstance(arguments, dict):
            return arguments
        raise ValueError(f"Invalid arguments: {arguments}")