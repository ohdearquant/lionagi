from .base import BaseMessage, MessageRole

class System(BaseMessage):
    """
    Represents a system message that encapsulates system-specific information.
    Extends BaseMessage.
    """

    def __init__(
        self, 
        system_info: any, 
        sender=None, 
        recipient=None
    ):
        """
        Initializes a System message with system information, sender, and recipient.

        Parameters:
        - system_info: The main content of the system message.
        - sender: Sender's identifier, defaults to 'system' if not provided.
        - recipient: Recipient's identifier, defaults to 'assistant' if not provided.
        """
        super().__init__(
            role=MessageRole.SYSTEM,
            sender=sender or "system",
            content={"system_info": system_info},
            recipient=recipient or "assistant"
        )

    @property
    def system_info(self):
        """
        Retrieves the system information stored in the message content.
        """
        return self.content["system_info"]