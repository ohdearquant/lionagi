from ..message.base import BaseMessage, MessageRole


# action response must correlates to a specific action request
class ActionResponse(BaseMessage):
    function: str | None = None
    arguments: dict | None = None
    output: any | None = None

    def __init__(
        self,
        action_request=None,
        output=None, 
        sender=None,        # the sender of action response is executor
    ):
        super().__init__(
            role=MessageRole.ASSISTANT,
            sender=sender or "executor",
            recipient=action_request.id_,
        )
        self.function=action_request.function
        self.arguments=action_request.arguments
        self.output=output
