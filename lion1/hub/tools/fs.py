"""The file system, read-only: a file whole, a directory's names. Over a `Tree`, so the same two tools
read a directory here or the copy of it in a box."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from lionagi import Actor, Context

from .code import LocalTree, Tree

__all__ = ("Read", "ListDir", "files")


class Read(BaseModel):
    """Read a file, whole: the view budget is the only size control."""

    path: str = Field(description="absolute, or relative to the console's directory")


class ListDir(BaseModel):
    """List a directory: names, with / after a directory."""

    path: str = Field(default=".", description="absolute, or relative to the console's directory")


def files(actor: Actor, root: Path | Tree) -> set[str]:
    """Register `read` and `list_dir` over `root`: a directory on this machine (a `LocalTree` of it,
    nothing outside it) or any `Tree`; relative paths resolve against the tree's root."""
    tree: Tree = LocalTree(root) if isinstance(root, (str, Path)) else root

    @actor.handler(Read)
    async def read(req: Read, ctx: Context) -> str:
        return await tree.read(req.path)

    @actor.handler(ListDir)
    async def list_dir(req: ListDir, ctx: Context) -> list[str]:
        return await tree.list_dir(req.path)

    return {"read", "list_dir"}
