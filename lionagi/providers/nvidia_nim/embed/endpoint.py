from lionagi.service.connections.endpoint import Endpoint

from .._config import NvidiaNimConfigs


@NvidiaNimConfigs.EMBED.register
class NvidiaNimEmbedEndpoint(Endpoint):
    """NVIDIA NIM embedding endpoint.

    Note: Verify available embedding models at https://build.nvidia.com/
    """

    def __init__(self, config=None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.NVIDIA_NIM_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("kwargs", {"model": "nvidia/nv-embed-v1"})
        super().__init__(config, **kwargs)
