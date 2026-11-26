# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The Anthropic account engine (ADR-0126). Private: no public names yet.

Routed accounts, the monthly budget ledger, dated pricing and key resolution, reached from the
hub's Anthropic adapter. The core keeps the seam it reads in ``lionagi.service.accounts``: the
shared types, the routing scope, the request guard and the usage-shape helpers.
"""

__all__ = ()
