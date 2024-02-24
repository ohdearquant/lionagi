import json
from typing import Any, Optional, List
from enum import Enum
import pandas as pd
from pydantic import BaseModel, Field, model_validator, ValidationError
from lionagi.schema.base_node import BaseNode
from lionagi.util import SysUtil, ConvertUtil, nget, to_dict

_message_fields = ['node_id', 'timestamp', 'role', 'sender', 'recipient', 'content',
                   'metadata', 'relation']


class BranchColumns(List[str], Enum):
    COLUMNS = _message_fields


class MessageField(str, Enum):
    NODE_ID = 'node_id'
    TIMESTAMP = 'timestamp'
    ROLE = 'role'
    SENDER = 'sender'
    RECIPIENT = 'recipient'
    CONTENT = 'content'
    METADATA = 'metadata'
    RELATION = 'relation'


class MessageRoleType(str, Enum):
    SYSTEM = 'system'
    USER = 'user'
    ASSISTANT = 'assistant'


class MessageContentKey(str, Enum):
    INSTRUCTION = 'instruction'
    CONTEXT = 'context'
    SYSTEM = 'system_info'
    ACTION_REQUEST = 'action_request'
    ACTION_RESPONSE = 'action_response'
    RESPONSE = 'assistant_response'


class MessageType(dict, Enum):
    SYSTEM = {
        MessageField.ROLE.value: MessageRoleType.SYSTEM.value,
        MessageField.SENDER.value: MessageRoleType.SYSTEM.value,
        MessageField.RECIPIENT.value: "",
        "content_key": MessageContentKey.SYSTEM.value
    },

    INSTRUCTION = {
        MessageField.ROLE.value: MessageRoleType.USER.value,
        MessageField.SENDER.value: MessageRoleType.USER.value,
        MessageField.RECIPIENT.value: "",
        "content_key": MessageContentKey.INSTRUCTION.value
    },

    CONTEXT = {
        MessageField.ROLE.value: MessageRoleType.USER.value,
        MessageField.SENDER.value: MessageRoleType.USER.value,
        MessageField.RECIPIENT.value: "",
        "content_key": MessageContentKey.CONTEXT.value
    },

    ACTION_REQUEST = {
        MessageField.ROLE.value: MessageRoleType.ASSISTANT.value,
        MessageField.SENDER.value: MessageRoleType.ASSISTANT.value,
        MessageField.RECIPIENT.value: "",
        "content_key": MessageContentKey.ACTION_REQUEST.value
    },

    ACTION_RESPONSE = {
        MessageField.ROLE.value: MessageRoleType.ASSISTANT.value,
        MessageField.SENDER.value: MessageRoleType.ASSISTANT.value,
        MessageField.RECIPIENT.value: "",
        "content_key": MessageContentKey.ACTION_RESPONSE.value
    },

    RESPONSE = {
        MessageField.ROLE.value: MessageRoleType.ASSISTANT.value,
        MessageField.SENDER.value: MessageRoleType.ASSISTANT.value,
        MessageField.RECIPIENT.value: "",
        "content_key": MessageContentKey.RESPONSE.value
    }


class BaseMessage(BaseNode):
    role: MessageRoleType = Field(..., alias=MessageField.ROLE.value)
    sender: str = Field(..., alias=MessageField.SENDER.value)  # Customizable sender
    recipient: Optional[str] = Field(None,
                                     alias=MessageField.RECIPIENT.value)  # Optional recipient

    class Config:
        extra = 'allow'
        use_enum_values = True
        allow_population_by_field_name = True

    @model_validator(pre=True)
    def handle_extra_fields(cls, values):
        """Move undefined fields to metadata."""
        fields = set(values.keys())
        defined_fields = set(cls.__fields__.keys())
        extra_fields = fields - defined_fields

        if extra_fields:
            metadata = values.get('metadata', {})
            for field in extra_fields:
                if field in metadata:
                    metadata[f"{field}_1"] = values.pop(field)
                else:
                    metadata[field] = values.pop(field)
            values['metadata'] = metadata
        return values

    def _to_roled_message(self):
        return {
            MessageField.ROLE.value: self._role.value,
            MessageField.CONTENT.value: (
                json.dumps(self.content) if isinstance(self.content, dict)
                else self.content
            )
        }

    def to_pd_series(self):
        msg_dict = self.to_dict()
        if isinstance(to_dict(message_dict['content']), dict):
            message_dict['content'] = json.dumps(message_dict['content'])
        return pd.Series(msg_dict)

    @classmethod
    def from_pd_series(cls, series: pd.Series):
        self = cls.from_dict(series.to_dict())
        if isinstance(self.content, str):
            try:
                self.content = to_dict(self.content)
            except:
                pass
        return self


    def __str__(self):

        timestamp = f" ({self.timestamp})" if self.timestamp else ""
        content_preview = self.content[:50] + "..." if len(
            self.content) > 50 else self.content
        meta_preview = str(self.metadata)[:50] + "..." if len(
            str(self.metadata)) > 50 else str(self.metadata)

        return (
            f"Message({self._role.value or 'none'}, {self._sender or 'none'}, "
            f"{content_preview or 'none'}, {self.recipient or 'none'},"
            f"{self.timestamp or 'none'})"
        )


class Instruction(BaseMessage):

    def __init__(self, instruction: Any, context: Any = None,
                 sender: str | None = None, recipient: str | None | Any = None,
                 metadata: dict | None = None, relation: list | None | Any = None,
                 **kwargs):
        super().__init__(
            role=MessageType.INSTRUCTION.value[MessageField.ROLE.value],
            sender=sender or MessageType.INSTRUCTION.value[MessageField.SENDER.value],
            content={MessageType.INSTRUCTION.value["content_key"]: instruction},
            recipient=MessageType.SYSTEM.value[MessageField.RECIPIENT.value],
            metadata=metadata or {}, relation=relation or [], **kwargs
        )
        if context:
            self.content.update({MessageType.CONTEXT.value["content_key"]: context})


class System(BaseMessage):

    def __init__(self, system: Any, sender: Optional[str] = None, recipient: Optional[
        str] = None, metadata: Optional[dict] = None, relation: Optional[list] = None,
                 **kwargs):
        super().__init__(
            role=MessageType.SYSTEM.value[MessageField.ROLE.value],
            sender=sender or MessageType.SYSTEM.value[MessageField.SENDER.value],
            content={MessageType.SYSTEM.value["content_key"]: system},
            recipient=recipient or MessageType.SYSTEM.value[MessageField.RECIPIENT.value],
            metadata=metadata or {}, relation=relation or [], **kwargs
        )


class ActionRequest(BaseMessage):

    def __init__(self, action_request: Any, sender: Optional[str] = None,
                 recipient: Optional[
                     str] = None, metadata: Optional[dict] = None,
                 relation: Optional[list] = None,
                 **kwargs
                 ):

        super().__init__(
            role=MessageType.ACTION_REQUEST.value[MessageField.ROLE.value],
            sender=sender or MessageType.ACTION_REQUEST.value[MessageField.SENDER.value],
            content={MessageType.ACTION_REQUEST.value["content_key"]: action_request},
            recipient=recipient or MessageType.ACTION_REQUEST.value[
                MessageField.RECIPIENT.value],
            metadata=metadata or {}, relation=relation or [], **kwargs
        )

    @classmethod
    def from_response(cls, response, sender=None, recipient=None, metadata=None,
                      relation=None,
                      **kwargs):
        return cls(action_request=response, sender=sender, recipient=recipient,
                   metadata=metadata,
                   relation=relation, **kwargs)

    @staticmethod
    def _handle_action_request(response):
        try:
            tool_count = 0
            func_list = []
            while tool_count < len(response['tool_calls']):
                _path = ['tool_calls', tool_count, 'type']

                if nget(response, _path) == 'function':
                    _path1 = ['tool_calls', tool_count, 'function', 'name']
                    _path2 = ['tool_calls', tool_count, 'function', 'arguments']

                    func_content = {
                        "action": ("action_" + nget(response, _path1)),
                        "arguments": nget(response, _path2)
                    }
                    func_list.append(func_content)
                tool_count += 1
            return func_list
        except:
            raise ValueError(
                "Response message must be one of regular assistant_response or function calling"
            )


class ActionResponse(BaseMessage):

    def __init__(self, action_response: Any, sender: Optional[str] = None,
                 recipient: Optional[
                     str] = None, metadata: Optional[dict] = None,
                 relation: Optional[list] = None,
                 **kwargs
                 ):
        super().__init__(
            role=MessageType.ACTION_RESPONSE.value[MessageField.ROLE.value],
            sender=sender or MessageType.ACTION_RESPONSE.value[MessageField.SENDER.value],
            content={MessageType.ACTION_RESPONSE.value["content_key"]: action_response},
            recipient=recipient or MessageType.ACTION_RESPONSE.value[
                MessageField.RECIPIENT.value],
            metadata=metadata or {}, relation=relation or [], **kwargs
        )

    @classmethod
    def from_response(cls, response, sender=None, recipient=None, metadata=None,
                      relation=None,
                      **kwargs):
        return cls(action_response=response, sender=sender, recipient=recipient,
                   metadata=metadata,
                   relation=relation, **kwargs)


class AssistantResponse(BaseMessage):

    def __init__(self, assistant_response: Any, sender: Optional[str] = None,
                 recipient: Optional[
                     str] = None, metadata: Optional[dict] = None,
                 relation: Optional[list] = None,
                 **kwargs
                 ):
        super().__init__(
            role=MessageType.RESPONSE.value[MessageField.ROLE.value],
            sender=sender or MessageType.RESPONSE.value[MessageField.SENDER.value],
            content={MessageType.RESPONSE.value["content_key"]: assistant_response},
            recipient=recipient or MessageType.RESPONSE.value[
                MessageField.RECIPIENT.value],
            metadata=metadata or {}, relation=relation or [], **kwargs
        )

    @classmethod
    def from_response(cls, response, sender=None, recipient=None, metadata=None,
                      relation=None,
                      **kwargs):
        return cls(assistant_response=response, sender=sender, recipient=recipient,
                   metadata=metadata,
                   relation=relation, **kwargs)


class MailCategory(str, Enum):
    MESSAGES = 'messages'
    TOOL = 'actions'
    SERVICE = 'provider'
    MODEL = 'model'


class BaseMail:

    def __init__(self, sender, recipient, category, package):
        self.sender = sender
        self.recipient = recipient
        try:
            if isinstance(category, str):
                category = MailCategory(category)
            if isinstance(category, MailCategory):
                self.category = category
            else:
                raise ValueError(f'Invalid request title. Valid titles are'
                                 f' {list(MailCategory)}')
        except Exception as e:
            raise ValueError(f'Invalid request title. Valid titles are '
                             f'{list(MailCategory)}, Error: {e}')
        self.package = package