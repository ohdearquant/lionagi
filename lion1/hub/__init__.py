"""The hub: what every app of lionagi v1 reaches for, versioned with the runtime.

`hub.guidance(name, **fields)` is the prose a profile is given, one markdown file per role under
`hub/guidance/`, so a change to what a model is told is a diff someone reads. `hub.tools` are the
handlers an app registers on an actor: the file readers, the bounded khive client. The runtime
(`lionagi/`) knows nothing of the hub; an app (`apps/`) composes the two.
"""

from .guidance import guidance
from .tools import (
    Bash,
    Box,
    BoxTree,
    DaytonaSandbox,
    DockerSandbox,
    KhiveOps,
    ListDir,
    LocalTree,
    Python,
    Read,
    RuntaSandbox,
    Sandbox,
    code,
    files,
    git_diff,
    khive_ops,
    shell,
)

__all__ = (
    "guidance",
    "Bash",
    "Box",
    "BoxTree",
    "DaytonaSandbox",
    "DockerSandbox",
    "KhiveOps",
    "ListDir",
    "LocalTree",
    "Python",
    "Read",
    "RuntaSandbox",
    "Sandbox",
    "code",
    "files",
    "git_diff",
    "khive_ops",
    "shell",
)
