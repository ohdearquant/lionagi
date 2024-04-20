from abc import ABC
from pydantic import Field
from lionagi.core.generic import BaseComponent

class Worker(BaseComponent, ABC):
    form_templates: dict = Field(default={}, description="The form templates of the worker")
    work_functions: dict = Field(default={}, description="The work functions of the worker")