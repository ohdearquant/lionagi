"""Tools an app registers on an actor. Each module offers its Specs and one `register`-shaped
function that returns the spec names it added, so a profile can name exactly those."""

from .box import Box, BoxBase, BoxTree, git_diff
from .code import Edit, LocalTree, ReadLines, Search, Tree, code
from .daytona import DaytonaSandbox, SandboxDead
from .docker import DockerSandbox, docker
from .fs import ListDir, Read, files
from .khive import KhiveOps, khive_ops
from .shell import Bash, Python, Sandbox, shell
from .watch import Criteria, Watch, criteria

__all__ = (
    "Box",
    "BoxBase",
    "BoxTree",
    "git_diff",
    "Edit",
    "LocalTree",
    "ReadLines",
    "Search",
    "Tree",
    "code",
    "DaytonaSandbox",
    "SandboxDead",
    "DockerSandbox",
    "docker",
    "ListDir",
    "Read",
    "files",
    "KhiveOps",
    "khive_ops",
    "Bash",
    "Python",
    "Sandbox",
    "shell",
    "Criteria",
    "Watch",
    "criteria",
)
