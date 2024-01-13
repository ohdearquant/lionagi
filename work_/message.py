import json
from typing import Any, Optional
from lionagi.utils import dynamic_flatten
from lionagi.schema import BaseNode

class Message():
    role: Optional[str] = None
    name: Optional[str] = None

    def _to_message(self, use_name=False, name=None):
        out = {"content": json.dumps(self.content) if isinstance(self.content, dict) else self.content}
        if use_name:
            out.update({"name": name or self.name})
        else:
            out.update({"role": self.role})
        return out
    
    @classmethod
    def __call__(
        cls,
        role_: str, 
        content: Any, 
        content_key: str, 
        name: Optional[str] = None
    ) -> None:
        self = cls()
        self.role = role_
        self.content = {content_key: content}
        self.name = name or role_
        return self

    @property
    def role_msg(self):
        return self._to_message()
        
    @property
    def name_msg(self):
        return self._to_message(use_name=True)
        
    @property
    def msg_content(self):
        return self._to_message()['content']

    @property
    def msg_content_flattened(self):
        return dynamic_flatten(self.msg_content)

    def has_content_key(self, key: str, recursive=False, sep = '_', max_depth=None) -> bool:
        ...

    def get_role(self):
        return str(self.role).strip().lower()
    
    def set_role(self, role):
        self.role=role
        
    def set_name(self, name):
        self.name=name
    
    def get_name(self):
        return str(self.name).strip().lower()
        
    def __str__(self):
        content_preview = (
            (str(self.content)[:75] + '...') if self.content and len(self.content) > 75 
            else str(self.content)
        )
        return f"Message(role={self.role}, name={self.name}, content='{content_preview}')"

class Response(Message):

    def _create_message(self, response: Any, name: Optional[str] = None) -> None:
        self.role = "assistant"
        try:
            response = response["message"]
            if str(response['content']) == "None":
                try:
                    tool_count = 0
                    func_list = []
                    while tool_count < len(response['tool_calls']):
                        if response['tool_calls'][tool_count]['type'] == 'function':
                            func_content = {
                                "function": ("func_" + response['tool_calls'][tool_count]['function']['name']),
                                "arguments": response['tool_calls'][tool_count]['function']['arguments']
                                }
                            func_list.append(func_content)
                        tool_count += 1

                    self.name = name or "func_request"
                    self.content = {'function_list': func_list}
                except:
                    raise ValueError("Response message must be one of regular response or function calling")
            else:
                self.content = response['content']
                self.name = name or "assistant"
        except:
            self.name = name or "func_call"
            self.content = response
    
class System(Message):
    
    def _create_message(self, system: Any, name: Optional[str] = None) -> None:
        self.__call__(
            role_="system", 
            content_key="system", 
            content=system, 
            name=name
        )

class Instruction(Message):

    def _create_message(self, instruction: Any, context=None ,name: Optional[str] = None) -> None:
        self.__call__(
            role_="user", 
            content_key="instruction", 
            content=instruction, 
            name=name
        )
        if context: 
            self.content.update({"context": context})



# filename: messages.py
import json
from typing import Any, Optional, Dict, List
from lionagi.utils import dynamic_flatten
from lionagi.schema import BaseNode

class Message(BaseNode):
    role: Optional[str] = None
    name: Optional[str] = None
    attachments: List[str] = []

    def _to_message(self, use_name=False, name=None):
        out = {"content": json.dumps(self.content) if isinstance(self.content, dict) else self.content}
        if use_name:
            out.update({"name": name or self.name})
        else:
            out.update({"role": self.role})
        if self.attachments:
            out.update({"attachments": self.attachments})
        return out
    
    @classmethod
    def __call__(
        cls,
        role_: str, 
        content: Any, 
        content_key: str, 
        name: Optional[str] = None
    ) -> 'Message':
        self = cls()
        self.role = role_
        self.content = {content_key: content}
        self.name = name or role_
        return self

    @property
    def role_msg(self):
        return self._to_message()
        
    @property
    def name_msg(self):
        return self._to_message(use_name=True)
        
    @property
    def msg_content(self):
        return self._to_message()['content']

    @property
    def msg_content_flattened(self):
        return dynamic_flatten(self.msg_content)

    def has_content_key(self, key: str, recursive=False, sep = '_', max_depth=None) -> bool:
        # Implementation of has_content_key
        ...

    def get_role(self):
        return str(self.role).strip().lower()
    
    def set_role(self, role):
        self.role = role
        
    def set_name(self, name):
        self.name = name
    
    def get_name(self):
        return str(self.name).strip().lower()
        
    def is_empty(self):
        return not bool(self.content)
    
    def add_attachment(self, attachment):
        self.attachments.append(attachment)
    
    def remove_attachment(self, attachment):
        self.attachments.remove(attachment)
    
    def get_all_keys(self):
        if isinstance(self.content, dict):
            return list(self.content.keys())
        return []
    
    def serialize(self):
        return json.dumps(self._to_message())
    
    @classmethod
    def deserialize(cls, message_str):
        data = json.loads(message_str)
        obj = cls()
        obj.role = data.get('role')
        obj.name = data.get('name')
        obj.content = json.loads(data.get('content'))
        obj.attachments = data.get('attachments', [])
        return obj
    
    def update_content(self, key, value):
        if isinstance(self.content, dict):
            self.content[key] = value
    
    def clear_content(self):
        self.content = {}
    
    def get_summary(self):
        if isinstance(self.content, dict):
            return ', '.join(f"{k}: {str(v)[:10]}" for k, v in self.content.items())
        return str(self.content)[:20]
    
    def validate_content(self, schema):
        # Placeholder for schema validation logic
        return True
    
    def __str__(self):
        content_preview = (
            (str(self.content)[:75] + '...') if self.content and len(self.content) > 75 
            else str(self.content)
        )
        return f"Message(role={self.role}, name={self.name}, content='{content_preview}')"