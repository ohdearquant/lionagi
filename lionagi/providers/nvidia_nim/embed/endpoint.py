from lionagi.service.connections.endpoint import Endpoint

from .._config import NvidiaNimConfigs


@NvidiaNimConfigs.EMBED.register
class NvidiaNimEmbedEndpoint(Endpoint):
    """NVIDIA NIM embedding endpoint."""

    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "nvidia/nv-embed-v1"})
        super().__init__(config, **kwargs)
