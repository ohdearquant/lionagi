from enum import Enum
from typing import Any, Dict, List
from pydantic import Field
from lionagi.core.generic import BaseComponent


class WorkStatus(str, Enum):
    """Enum to represent different statuses of work."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Work(BaseComponent):
    """Base component for handling individual units of work."""
    form_id: str = Field(..., description="ID of the form for this work")
    priority: int = Field(default=0, description="Priority of the work")
    status: WorkStatus = Field(
        default=WorkStatus.PENDING, description="Current status of the work"
    )
    deliverables: Dict[str, Any] | list = Field(
        default={}, description="Deliverables produced by the work"
    )
    dependencies: List['Work'] = Field(
        default_factory=list, description="List of work items this work depends on"
    )
