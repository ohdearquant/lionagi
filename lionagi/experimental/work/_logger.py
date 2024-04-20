from collections import deque
from typing import Dict
from pydantic import BaseModel, Field
from .schema import Work, WorkStatus

class WorkLog(BaseModel):
    """Model to store and manage work logs."""
    logs: Dict[str, Work] = Field(default={}, description="Logs of work items")
    pending: deque = Field(
        default_factory=deque, description="Priority queue of pending work items"
    )
    errored: deque = Field(
        default_factory=deque, description="Queue of errored work items"
    )
    
    def append(self, work: Work):
        """Append a work item to the logs and pending queue."""
        self.logs[str(work.form_id)] = work
        self.pending.append(str(work.form_id))

    def get_by_status(self, status: WorkStatus) -> Dict[str, Work]:
        """Get work items by their status."""
        return {
            wid: work for wid, work in self.logs.items() if work.status == status
        }
        