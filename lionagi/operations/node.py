import logging
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, PrivateAttr

from lionagi._errors import ExecutionError, OperationError
from lionagi.protocols.types import ID, Event, Node

BranchOperations = Literal[
    "chat",
    "operate",
    "communicate",
    "parse",
    "ReAct",
    "interpret",
    "act",
    "ReActStream",
    "run",
    "chat_and_record",
]

logger = logging.getLogger("operation")


class Operation(Node, Event):
    """Operation node for flow graphs; set ``_branch`` before calling ``invoke()``."""

    operation: BranchOperations | str
    parameters: dict[str, Any] | BaseModel = Field(
        default_factory=dict,
        description="Parameters for the operation",
        exclude=True,
    )
    _branch: Any = PrivateAttr(default=None)

    @property
    def branch_id(self) -> UUID | None:
        if a := self.metadata.get("branch_id"):
            return ID.get_id(a)

    @branch_id.setter
    def branch_id(self, value: str | UUID | None):
        if value is None:
            self.metadata.pop("branch_id", None)
        else:
            self.metadata["branch_id"] = str(value)

    @property
    def graph_id(self) -> str | None:
        if a := self.metadata.get("graph_id"):
            return ID.get_id(a)

    @graph_id.setter
    def graph_id(self, value: str | UUID | None):
        if value is None:
            self.metadata.pop("graph_id", None)
        else:
            self.metadata["graph_id"] = str(value)

    @property
    def request(self) -> dict:
        params = self.parameters
        if hasattr(params, "model_dump"):
            params = params.model_dump()
        elif hasattr(params, "dict"):
            params = params.dict()

        return params if isinstance(params, dict) else {}

    @property
    def response(self):
        return self.execution.response if self.execution else None

    async def _invoke(self):
        """Execute the operation on the pre-set branch; called by Event.invoke()."""
        branch = self._branch
        if branch is None:
            raise ExecutionError(
                "Operation._branch must be set before invoke(). "
                "Use operation._branch = branch before calling invoke()."
            )

        meth = branch.get_operation(self.operation)
        if meth is None:
            raise OperationError(f"Unsupported operation type: {self.operation}")

        self.branch_id = branch.id

        if self.operation == "ReActStream":
            res = []
            async for i in meth(**self.request):
                res.append(i)
            return res
        return await meth(**self.request)


def create_operation(
    operation: BranchOperations | str,
    parameters: dict[str, Any] | BaseModel = None,
    **kwargs,
):
    """Create an Operation node."""
    return Operation(operation=operation, parameters=parameters, **kwargs)
