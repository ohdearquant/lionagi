"""
Copyright 2024 HaiyangLi

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""
This module contains the BaseAgent class, which serves as a base class for agents.
"""
import asyncio

from typing import Any, Callable

from lion_core.abc import BaseExecutor
from lion_core.libs import pcall
from lion_core.communication import MailManager

from lionagi.os.primitives import StartMail, Node


class BaseAgent(Node):

    def __init__(
        self,
        structure: BaseExecutor,
        executable: BaseExecutor,
        output_parser=None,
        **kwargs,
    ) -> None:
        """
        Initializes the BaseAgent instance.

        Args:
            structure: The structure of the agent.
            executable_obj: The executable object of the agent.
            output_parser: A function for parsing the agent's output (optional).
        """
        super().__init__(**kwargs)
        self.structure: BaseExecutor = structure
        self.executable: BaseExecutor = executable
        self.start: StartMail = StartMail()
        self.mail_manager: MailManager = MailManager(
            [self.structure, self.executable, self.start]
        )
        self.output_parser: Callable | None = output_parser
        self.start_context: Any | None = None

    async def mail_manager_control(self, refresh_time=1):
        """
        Controls the mail manager execution based on the structure and executable states.

        Args:
            refresh_time: The time interval (in seconds) for checking the execution states (default: 1).
        """
        while not self.structure.execute_stop or not self.executable.execute_stop:
            await asyncio.sleep(refresh_time)
        self.mail_manager.execute_stop = True

    async def execute(self, context=None):
        """
        Executes the agent with the given context and returns the parsed output (if available).

        Args:
            context: The initial context for the agent (optional).

        Returns:
            The parsed output of the agent (if available).
        """
        self.start_context = context
        self.start.trigger(
            context=context,
            structure_id=self.structure.ln_id,
            executable_id=self.executable.ln_id,
        )
        await pcall(
            [
                self.structure.execute,
                self.executable.execute,
                self.mail_manager.execute,
                self.mail_manager_control,
            ]
        )

        self.structure.execute_stop = False
        self.executable.execute_stop = False
        self.mail_manager.execute_stop = False

        if self.output_parser:
            return self.output_parser(self)
