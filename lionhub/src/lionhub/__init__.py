# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""lionhub: the parts of lionagi built on top of the core.

The core (primitives, messages, sessions, operations, the service layer) never imports
this package. A few packages still under ``lionagi/`` do, because later moves take them
out of the core; ``tests/test_moved_modules.py`` lists them and fails when the list and
the tree disagree. ``lionhub`` ships in the same distribution as ``lionagi`` and imports
it freely. Modules that moved here from ``lionagi`` keep their old import paths until
lionagi 2.0; the table of moves is ``lionagi._moved.MOVED``.
"""

__all__ = ()
