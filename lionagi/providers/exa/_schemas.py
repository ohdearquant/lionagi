# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class LivecrawlType(str, Enum):
    never = "never"
    fallback = "fallback"
    always = "always"


class _ExaBase(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ContentsText(_ExaBase):
    include_html_tags: bool | None = Field(default=False, alias="includeHtmlTags")
    max_characters: int | None = Field(default=None, alias="maxCharacters")


class ContentsHighlights(_ExaBase):
    highlights_per_url: int | None = Field(default=1, alias="highlightsPerUrl")
    num_sentences: int | None = Field(default=5, alias="numSentences")
    query: None | str = Field(default=None)


class ContentsSummary(_ExaBase):
    query: None | str = Field(default=None)


class ContentsExtras(_ExaBase):
    links: int | None = Field(default=None)
    image_links: int | None = Field(default=None, alias="imageLinks")


class Contents(_ExaBase):
    text: None | ContentsText = Field(default=None)
    highlights: None | ContentsHighlights = Field(default=None)
    summary: None | ContentsSummary = Field(default=None)
    livecrawl: None | LivecrawlType = Field(default=LivecrawlType.never)
    livecrawl_timeout: int | None = Field(
        default=10000,
        alias="livecrawlTimeout",
        description="Timeout in ms for livecrawling.",
    )
    subpages: int | None = Field(default=None)
    subpage_target: None | str | list[str] = Field(
        default=None,
        alias="subpageTarget",
        description="Target subpage(s) to crawl, e.g. 'cited papers'.",
    )
    extras: None | ContentsExtras = Field(default=None)


__all__ = (
    "LivecrawlType",
    "_ExaBase",
    "ContentsText",
    "ContentsHighlights",
    "ContentsSummary",
    "ContentsExtras",
    "Contents",
)
