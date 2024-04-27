from .base import BaseMessage, MessageRole, SYSTEM_FIELDS

class Instruction(BaseMessage):
    """
    Represents an instruction message with contextual data. Extends BaseMessage.
    """

    def __init__(
        self, 
        instruction=None, 
        context=None,
        sender=None,
        output_fields=None,
        recipient=None, 
        **kwargs  # additional context fields
    ):
        """
        Initializes an Instruction message with a set role and optional fields.

        Parameters:
        - instruction: The main instruction, defaults to "N/A" if not provided.
        - context: JSON serializable context for the instruction.
        - sender: Sender's identifier.
        - output_fields: Expected output fields for the instruction.
        - recipient: Recipient's identifier.
        - **kwargs: Additional context fields.
        """
        super().__init__(
            role=MessageRole.USER,
            sender=sender,
            content={"instruction": instruction or "N/A"},
            recipient=recipient
        )

        self._initiate_content(context, output_fields, **kwargs)

    def _initiate_content(self, context, output_fields, **kwargs):
        """
        Processes context and output fields to update message content.
        """
        if context:
            context = (
                {"context": context} 
                if not isinstance(context, dict) 
                else context
            )
            if (
                additional_context := {
                    k: v for k, v in kwargs.items() if k not in SYSTEM_FIELDS
                }
            ) != {}:
                context['additional_context'] = additional_context    
            self.content.update(context)
            
        if output_fields:
            self.content["output_fields"] = self._format_output_field(output_fields)

    @staticmethod
    def _format_output_field(output_fields):
        format_ = f"""
        MUST EXACTLY FOLLOW THE RESPONSE FORMAT. NO ADDITIONAL COMMENTS ALLOWED!
        ```json
        {output_fields}
        ```
        """
        return {"response_format": format_.replace("        ", "")}