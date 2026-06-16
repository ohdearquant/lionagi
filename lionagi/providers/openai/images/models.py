# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import Literal

from pydantic import BaseModel, Field


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


__all__ = ("ImageGenerationRequest", "ImageEditRequest")
