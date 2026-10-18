# SPDX-License-Identifier: BSD-3-Clause

"""Agent Workbench Environment Client."""

from typing import Dict

from openenv.core import EnvClient
from openenv.core.client_types import StepResult
from openenv.core.env_server.types import State

from .models import WorkbenchAction, WorkbenchObservation


class AgentWorkbenchEnv(EnvClient[WorkbenchAction, WorkbenchObservation, State]):
    """Client for the Agent Workbench environment.

    >>> with AgentWorkbenchEnv(base_url="http://localhost:8000") as client:
    ...     result = client.reset(task_id="math-divisible-1")
    ...     result = client.step(WorkbenchAction(tool="list_files"))
    """

    def _step_payload(self, action: WorkbenchAction) -> Dict:
        """
        Convert WorkbenchAction to JSON payload for step message.

        Args:
            action: WorkbenchAction instance

        Returns:
            Dictionary representation suitable for JSON encoding
        """
        return {"tool": action.tool, "args": action.args}

    def _parse_result(self, payload: Dict) -> StepResult[WorkbenchObservation]:
        """
        Parse server response into StepResult[WorkbenchObservation].

        Args:
            payload: JSON response data from server

        Returns:
            StepResult with WorkbenchObservation
        """
        obs_data = payload.get("observation", {})
        observation = WorkbenchObservation(
            task=obs_data.get("task"),
            tools=obs_data.get("tools"),
            output=obs_data.get("output", ""),
            error=obs_data.get("error"),
            steps_left=obs_data.get("steps_left", 0),
            done=payload.get("done", False),
            reward=payload.get("reward"),
            metadata=payload.get("metadata", obs_data.get("metadata", {})),
        )

        return StepResult(
            observation=observation,
            reward=payload.get("reward"),
            done=payload.get("done", False),
            metadata=payload.get("metadata"),
        )

    def _parse_state(self, payload: Dict) -> State:
        """
        Parse server response into State object.

        Args:
            payload: JSON response from state request

        Returns:
            State object with episode_id and step_count
        """
        return State(
            episode_id=payload.get("episode_id"),
            step_count=payload.get("step_count", 0),
        )
