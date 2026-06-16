# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""OpenAI image endpoints: generation (/v1/images/generations) and editing (/v1/images/edits)."""

from __future__ import annotations

import io

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from .._config import OpenAIConfigs

__all__ = ("OpenaiImageGenerationEndpoint", "OpenaiImageEditEndpoint")


@OpenAIConfigs.IMAGE_GENERATION.register
class OpenaiImageGenerationEndpoint(Endpoint):
    """DALL-E / gpt-image-1 image generation endpoint."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.OPENAI_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)


@OpenAIConfigs.IMAGE_EDIT.register
class OpenaiImageEditEndpoint(Endpoint):
    """DALL-E image editing (inpainting) endpoint; pass image/mask bytes via kwargs as multipart/form-data."""

    transport_arg_keys = ("image", "image_filename", "mask", "mask_filename")

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.OPENAI_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)

    async def _call(self, payload: dict, headers: dict, **kwargs):
        """Encode request as multipart/form-data."""
        self._assert_ssrf_safe_url()

        import aiohttp

        image_data: bytes | None = kwargs.pop("image", None)
        image_filename: str = kwargs.pop("image_filename", "image.png")
        mask_data: bytes | None = kwargs.pop("mask", None)
        mask_filename: str = kwargs.pop("mask_filename", "mask.png")

        form = aiohttp.FormData()
        for key, value in payload.items():
            if value is not None:
                form.add_field(key, str(value))

        if image_data is not None:
            form.add_field(
                "image",
                (
                    io.BytesIO(image_data)
                    if isinstance(image_data, (bytes, bytearray))
                    else image_data
                ),
                filename=image_filename,
                content_type="image/png",
            )

        if mask_data is not None:
            form.add_field(
                "mask",
                (io.BytesIO(mask_data) if isinstance(mask_data, (bytes, bytearray)) else mask_data),
                filename=mask_filename,
                content_type="image/png",
            )

        multipart_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

        async with self._create_http_session() as session:
            async with session.post(
                url=self.config.full_url,
                headers=multipart_headers,
                data=form,
            ) as response:
                if response.status != 200:
                    error_body = await response.text()
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"Image edit request failed ({response.status}): {error_body}",
                        headers=response.headers,
                    )
                return await response.json()
