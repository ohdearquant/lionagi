# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""lionagi command-line interface.

The `li` entry point is `lionagi.cli.main:main`; import it from there.
This package init stays empty so that importing any submodule (e.g.
lionagi.cli._logging from lionagi.studio.cli, which main.py itself imports
at module level) never re-enters main.py through the package init.
"""
