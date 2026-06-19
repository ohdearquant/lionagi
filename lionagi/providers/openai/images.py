# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""OpenAI image endpoints: generation (/v1/images/generations) and editing (/v1/images/edits)."""

from __future__ import annotations

import io
from typing import Literal

from pydantic import BaseModel, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import OpenAIConfigs


class ImageGenerationRequest(BaseModel):
    """Request body for OpenAI image generation (POST /v1/images/generations)."""

    prompt: str = Field(
        ...,
        description="Text description of the desired image(s).",
    )
    model: str = Field(
        default="dall-e-3",
        description="Model to use: 'dall-e-2', 'dall-e-3', or 'gpt-image-1'.",
    )
    n: int | None = Field(
        default=1,
        ge=1,
        le=10,
        description="Number of images to generate (1–10; dall-e-3 only supports 1).",
    )
    quality: Literal["standard", "hd", "low", "medium", "high", "auto"] | None = Field(
        default=None,
        description="Image quality. 'hd' for dall-e-3; 'low'/'medium'/'high'/'auto' for gpt-image-1.",
    )
    response_format: Literal["url", "b64_json"] | None = Field(
        default=None,
        description="Format of generated images. Defaults to 'url'.",
    )
    size: str | None = Field(
        default=None,
        description=(
            "Image size. dall-e-2: '256x256', '512x512', '1024x1024'. "
            "dall-e-3: '1024x1024', '1792x1024', '1024x1792'. "
            "gpt-image-1: '1024x1024', '1536x1024', '1024x1536', 'auto'."
        ),
    )
    style: Literal["vivid", "natural"] | None = Field(
        default=None,
        description="dall-e-3 only. 'vivid' for dramatic, 'natural' for realistic.",
    )
    user: str | None = Field(
        default=None,
        description="End-user identifier for abuse monitoring.",
    )


class ImageEditRequest(BaseModel):
    """Request body for OpenAI image editing (POST /v1/images/edits); image sent as multipart/form-data."""

    prompt: str = Field(
        ...,
        description="Text description of the desired edits.",
    )
    model: str = Field(
        default="dall-e-2",
        description="Model to use: 'dall-e-2' or 'gpt-image-1'.",
    )
    n: int | None = Field(
        default=1,
        ge=1,
        le=10,
        description="Number of edited images to generate.",
    )
    size: str | None = Field(
        default=None,
        description="Image size: '256x256', '512x512', or '1024x1024'.",
    )
    response_format: Literal["url", "b64_json"] | None = Field(
        default=None,
        description="Format of generated images.",
    )
    user: str | None = Field(
        default=None,
        description="End-user identifier for abuse monitoring.",
    )


__all__ = (
    "ImageGenerationRequest",
    "ImageEditRequest",
    "OpenaiImageGenerationEndpoint",
    "OpenaiImageEditEndpoint",
)


@OpenAIConfigs.IMAGE_GENERATION.register
class OpenaiImageGenerationEndpoint(Endpoint):
    """DALL-E / gpt-image-1 image generation endpoint."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)


@OpenAIConfigs.IMAGE_EDIT.register
class OpenaiImageEditEndpoint(Endpoint):
    """DALL-E image editing (inpainting) endpoint; pass image/mask bytes via kwargs as multipart/form-data."""

    transport_arg_keys = ("image", "image_filename", "mask", "mask_filename")

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
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
