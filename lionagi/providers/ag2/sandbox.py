# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Daytona sandbox manager for AG2 NLIP agents: creates isolated sandboxes and returns their URLs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

NLIP_SERVER_SCRIPT = '''\
import os, sys, asyncio, logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("autogen").setLevel(logging.WARNING)

from autogen import ConversableAgent
from autogen.agentchat.contrib.nlip_agent import AG2NlipApplication
import uvicorn

agent = ConversableAgent(
    name="{name}",
    system_message="""{system_message}""",
    llm_config={{
        "config_list": [{{"model": "{model}", "api_key": os.environ.get("OPENAI_API_KEY", "")}}],
    }},
    human_input_mode="NEVER",
)
app = AG2NlipApplication(agent)
uvicorn.run(app, host="0.0.0.0", port=8000)
'''


@dataclass
class SandboxAgent:
    """A sandbox running an NLIP agent server."""

    name: str
    sandbox: Any
    url: str
    sandbox_id: str


@dataclass
class SandboxManager:
    """Creates and tracks Daytona sandboxes for AG2 NLIP agents; cleans up on exit."""

    api_key: str | None = None
    target: str = "us"
    model: str = "gpt-5.4-mini"
    env_vars: dict[str, str] = field(default_factory=dict)
    _sandboxes: list[SandboxAgent] = field(default_factory=list)
    _daytona: Any = None

    def _get_daytona(self):
        if self._daytona is None:
            from daytona import Daytona

            kwargs = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.target:
                kwargs["target"] = self.target
            self._daytona = Daytona(**kwargs)
        return self._daytona

    async def create_agent_sandbox(
        self,
        name: str,
        system_message: str,
        *,
        model: str | None = None,
        extra_env: dict[str, str] | None = None,
        extra_packages: list[str] | None = None,
    ) -> SandboxAgent:
        """Create a Daytona sandbox running an NLIP agent server; returns SandboxAgent with URL."""
        from daytona import CreateSandboxFromImageParams, Image, Resources

        daytona = self._get_daytona()

        packages = ["ag2[nlip,openai]", "uvicorn"]
        if extra_packages:
            packages.extend(extra_packages)

        image = Image.base("python:3.12-slim").pip_install(packages)

        env = {**self.env_vars}
        if extra_env:
            env.update(extra_env)

        sandbox = daytona.create(
            CreateSandboxFromImageParams(
                image=image,
                env_vars=env,
                resources=Resources(cpu=1, memory=2, disk=4),
                auto_stop_interval=0,
            ),
            timeout=120,
        )

        script = NLIP_SERVER_SCRIPT.format(
            name=name.replace('"', '\\"'),
            system_message=system_message.replace('"""', '\\"""'),
            model=model or self.model,
        )

        sandbox.process.code_run(script)
        url = sandbox.get_preview_link(8000).url

        agent = SandboxAgent(
            name=name,
            sandbox=sandbox,
            url=url,
            sandbox_id=str(sandbox.id),
        )
        self._sandboxes.append(agent)
        logger.info("Created sandbox agent %r at %s", name, url)
        return agent

    async def create_agent_configs(
        self,
        specs: list[dict[str, str]],
        *,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Create sandboxed agent configs from name/system_message specs; returns nlip_url configs."""
        configs = []
        for spec in specs:
            agent = await self.create_agent_sandbox(
                name=spec["name"],
                system_message=spec.get("system_message", f"You are {spec['name']}."),
                model=model,
            )
            configs.append(
                {
                    "name": agent.name,
                    "role": spec.get("role", "remote agent"),
                    "nlip_url": agent.url,
                }
            )
        return configs

    def cleanup(self):
        """Delete all sandboxes."""
        daytona = self._get_daytona()
        for agent in self._sandboxes:
            try:
                daytona.delete(agent.sandbox)
                logger.info("Deleted sandbox %r", agent.name)
            except Exception:
                logger.warning("Failed to delete sandbox %r", agent.name, exc_info=True)
        self._sandboxes.clear()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.cleanup()
