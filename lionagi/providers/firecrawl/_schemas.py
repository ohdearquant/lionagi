# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from enum import Enum


class OutputFormat(str, Enum):
    markdown = "markdown"
    html = "html"
    raw_html = "rawHtml"
    links = "links"
    screenshot = "screenshot"


__all__ = ("OutputFormat",)
