# SPDX-License-Identifier: BSD-3-Clause
"""Action and observation types for the Agent Workbench environment."""

from typing import Any

from openenv.core.env_server.types import Action, Observation
from pydantic import Field


class WorkbenchAction(Action):
    """One tool call. ``tool`` names a tool listed at reset; ``args`` holds its arguments."""

    tool: str = Field(
        ...,
        description="Tool to call: list_files, read_file, run_python, write_file, run_tests, check or submit",
    )
    args: dict[str, Any] = Field(
        default_factory=dict, description="Arguments for the tool, as listed at reset"
    )


class WorkbenchObservation(Observation):
    """What the agent sees after reset or after a tool call."""

    task: str | None = Field(default=None, description="The task, sent once at reset")
    tools: str | None = Field(
        default=None, description="The tools and their arguments, sent once at reset"
    )
    output: str = Field(default="", description="Result of the last tool call")
    error: str | None = Field(
        default=None, description="Why the last action was refused, if it was"
    )
    steps_left: int = Field(default=0, description="Tool calls left before the episode ends")
