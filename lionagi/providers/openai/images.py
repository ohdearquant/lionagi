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


def _replayable_file_factory(file_data, field_name: str, *, require_replayable: bool = True):
    """Return a zero-arg callable producing a fresh file object for one retry attempt.
    See docs/internals/runtime.md for the replay-safety invariant."""
    if file_data is None:
        return lambda: None
    if isinstance(file_data, (bytes, bytearray)):
        snapshot = bytes(file_data)
        return lambda: io.BytesIO(snapshot)
    if not require_replayable:
        return lambda: file_data

    seekable = getattr(file_data, "seekable", None)
    if not callable(seekable) or not seekable():
        if require_replayable:
            raise TypeError(
                f"{field_name} must be bytes, bytearray, or a seekable stream to "
                "support retries; pass bytes, or configure the endpoint with "
                "max_retries=1 for a non-seekable stream."
            )
        return lambda: file_data
    # Snapshot once, restore position — a live stream handed to each attempt would
    # already be at EOF on retry (RetryConfig re-invokes _call), uploading empty.
    start_pos = file_data.tell()
    snapshot = file_data.read()
    file_data.seek(start_pos)
    return lambda: io.BytesIO(snapshot)


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
        import aiohttp

        image_data = kwargs.pop("image", None)
        image_filename: str = kwargs.pop("image_filename", "image.png")
        mask_data = kwargs.pop("mask", None)
        mask_filename: str = kwargs.pop("mask_filename", "mask.png")

        can_retry = self._can_retry()
        image_factory = _replayable_file_factory(image_data, "image", require_replayable=can_retry)
        mask_factory = _replayable_file_factory(mask_data, "mask", require_replayable=can_retry)

        multipart_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

        def _build_form():
            form = aiohttp.FormData()
            for key, value in payload.items():
                if value is not None:
                    form.add_field(key, str(value))

            image_obj = image_factory()
            if image_obj is not None:
                form.add_field(
                    "image",
                    image_obj,
                    filename=image_filename,
                    content_type="image/png",
                )

            mask_obj = mask_factory()
            if mask_obj is not None:
                form.add_field(
                    "mask",
                    mask_obj,
                    filename=mask_filename,
                    content_type="image/png",
                )
            return {"data": form}

        # API fields stay in the multipart body; only transport kwargs
        # (proxy, ssl, timeout, ...) are forwarded to the HTTP layer.
        transport_kwargs = {k: v for k, v in kwargs.items() if k not in payload}
        return await self._call_aiohttp(
            payload=payload,
            headers=multipart_headers,
            request_body_factory=_build_form,
            response_mode="json",
            error_context="Image edit request",
            **transport_kwargs,
        )
