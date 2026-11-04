import logging
from typing import Literal

from dotenv import load_dotenv
from khivemcp import ServiceGroup, operation

from lionagi import iModel
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.third_party.exa_models import ExaSearchRequest
from lionagi.service.third_party.pplx_models import PerplexityChatRequest

logger = logging.getLogger(__name__)

PROVIDERS = Literal["exa", "perplexity"]


class SearchServiceGroup(ServiceGroup):
    def __init__(self, config: dict | None = None):
        """
        Initialize the LLMServiceGroup.
        """
        load_dotenv()
        super().__init__(config=config)
        logger.info(
            "[LLMServiceGroup] Initialized.",
        )
        self.imodels = {}

    @operation(name="exa_search", schema=ExaSearchRequest)
    async def exa_search(self, request: ExaSearchRequest):
        """Performs a search using Exa's search endpoint."""
        if "exa_search" not in self.imodels:
            self.imodels["exa_search"] = iModel(
                provider="exa",
                endpoint="search",
                queue_capacity=5,
                interval=1,
                limit_requests=5,
                api_key="EXA_API_KEY",
            )

        result: APICalling = await self.imodels["exa_search"].invoke(
            **request.model_dump(exclude_none=True),
            is_cached=True,
        )
        return {
            "id": str(result.id),
            "created_at": result.created_datetime.isoformat(),
            "status": result.execution.status.value,
            "duration": result.execution.duration,
            "response": result.execution.response,
            "error": result.execution.error,
        }

    @operation(name="perplexity_search", schema=PerplexityChatRequest)
    async def perplexity_search(self, request: PerplexityChatRequest):
        """Performs a search using Perplexity's chat completion endpoint."""
        if "perplexity_search" not in self.imodels:
            self.imodels["perplexity_search"] = iModel(
                provider="perplexity",
                endpoint="chat",
                interval=60,
                limit_tokens=20000,
                api_key="PERPLEXITY_API_KEY",
                limit_requests=10,
            )

        imodel = self.imodels["perplexity_search"]
        result: APICalling = await imodel.invoke(
            **request.model_dump(exclude_none=True),
            is_cached=True,
        )
        return {
            "id": str(result.id),
            "created_at": result.created_datetime.isoformat(),
            "status": result.execution.status.value,
            "duration": result.execution.duration,
            "response": result.execution.response,
            "error": result.execution.error,
        }
