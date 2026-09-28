"""The parser and the entry: `lion <subcommand>`."""

from __future__ import annotations

import argparse
import asyncio

from lionagi.checkpoint import KINDS, WHYS

from .agent import agent_main
from .areas import areas_main
from .chat import chat_main
from .context import BUDGET, context_main

__all__ = ("parser", "main")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lion", description="lionagi v1")
    sub = ap.add_subparsers(dest="command", required=True)
    ag = sub.add_parser("agent", help="an actor's long-running agent, from its directory")
    ag.add_argument(
        "--dir", required=True, help="the agent directory: .khive/config.toml, chores.toml, notes, ledger"
    )
    ag.add_argument("--model", default=None, help="the model; overrides chores.toml")
    ag.add_argument("--once", action="store_true", help="one wake, the record printed, then exit")
    ag.add_argument(
        "--rerun",
        nargs="+",
        default=None,
        metavar="ID",
        help="read mail back through one wake, by id; then exit",
    )
    ag.add_argument("--wait", type=int, default=30, help="seconds --once waits for mail")
    ag.add_argument("--check", default=None, help="run one instrument, print and land it; nothing is sent")
    ag.add_argument("--ticks", action="store_true", help="create the missing tick reminders and exit")
    ag.add_argument("--arm", action="store_true", help="the landing arm: identity and boundary, then exit")
    ag.add_argument("--stats", action="store_true", help="the day's tokens, wakes, mail and idle polls")
    ch = sub.add_parser("chat", help="talk with an actor at the terminal; the record prints as it grows")
    ch.add_argument(
        "--dir", default=".", help="the console's directory: files, and khive if it holds .khive/config.toml"
    )
    ch.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    ch.add_argument("--say", default=None, help="the first message; otherwise asked for")
    ch.add_argument("--rounds", type=int, default=200, help="the run's round budget; the model is told")
    ch.add_argument(
        "--shell",
        action="store_true",
        help="give the model bash and python in a Linux VM over this directory",
    )
    ch.add_argument("--offline", action="store_true", help="the VM gets no network (with --shell)")
    ch.add_argument(
        "--box",
        choices=("vm", "daytona"),
        default="vm",
        help="where bash, python and the file tools work: the local VM over this directory (vm), or a "
        "Daytona box the directory is pushed into at the same path; its diff comes back as a patch",
    )
    ch.add_argument("--image", default="python:3.12", help="the Daytona box's image (with --box daytona)")
    ch.add_argument(
        "--apply",
        action="store_true",
        help="apply the box's patch to this directory at the end (git apply --3way); default: print + save",
    )
    ch.add_argument(
        "--code",
        action="store_true",
        help="the coding preset: read_lines, search and edit on this directory, the shell VM (implies "
        "--shell), and the root AGENTS.md/CLAUDE.md in the guidance",
    )
    ch.add_argument(
        "--log", default=None, help="the record, one JSON line per entry; default .lion/chats/<time>.jsonl"
    )
    ch.add_argument(
        "--raw", action="store_true", help="print the record's own lines instead of the console view"
    )
    ch.add_argument(
        "-r",
        "--resume",
        nargs="?",
        const="last",
        default=None,
        help="continue a chat: its id, a prefix, a path; bare = last",
    )
    ch.add_argument(
        "-c", "--continue", dest="resume", action="store_const", const="last", help="continue the last chat"
    )
    ch.add_argument(
        "-v", "--verbose", action="store_true", help="show results' first lines and the loop's notices"
    )
    ar = sub.add_parser(
        "areas", help="the residents by area: serving, in a wake, wakes, today's runs and cost"
    )
    ar.add_argument(
        "--file", default="~/.lionagi/areas.toml", help="the registry: [area.<name>] chair, home, agents"
    )
    ar.add_argument(
        "--serve", type=int, default=None, metavar="PORT", help="serve the page and /api/areas on PORT"
    )
    ar.add_argument("--host", default="127.0.0.1")
    ar.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="NAME",
        help="an extra host name or IP this server also answers to (repeatable)",
    )
    ar.add_argument("--refresh", type=int, default=5, help="seconds between the page's reloads")
    ar.add_argument("--json", action="store_true", help="print the rows as JSON instead of the table")
    ar.add_argument(
        "--as",
        dest="as_dir",
        default=".",
        metavar="DIR",
        help="with --serve: the directory whose .khive/config.toml names who the page reads and mails as",
    )
    here = argparse.ArgumentParser(add_help=False)
    here.add_argument(
        "--dir", default=".", help="the session's directory: .khive/config.toml names the actor"
    )
    cx = sub.add_parser("context", help="a Claude Code session's checkpoint and restore (ADR-0019)")
    verbs = cx.add_subparsers(dest="verb", required=True)
    cs = verbs.add_parser("set", parents=[here], help="declare a var; the next checkpoint takes it")
    cs.add_argument("--name", required=True)
    cs.add_argument("--kind", required=True, choices=KINDS)
    cs.add_argument("--value", required=True, help="text, or JSON when it opens with { or [")
    cc = verbs.add_parser("checkpoint", parents=[here], help="take the checkpoint, no model call")
    cc.add_argument("--why", choices=WHYS, default="manual")
    cc.add_argument("--session", default=None, help="default: the newest transcript's session")
    cc.add_argument(
        "--transcript",
        default=None,
        help="default: the newest *.jsonl under ~/.claude/projects/<cwd slug>/, never a _precompact_ copy",
    )
    cc.add_argument("--budget", type=int, default=BUDGET, help="tokens; the kept view is half of it")
    cr = verbs.add_parser("restore", parents=[here], help="print the first prompt of the fresh session")
    cr.add_argument("--id", default=None, help="a checkpoint id; default: the latest")
    cr.add_argument("--json", action="store_true", help="print {id, first_prompt, watches, spawns}")
    ct = verbs.add_parser("status", parents=[here], help="the latest checkpoint and the figure now")
    ct.add_argument("--session", default=None, help="default: the newest transcript's session")
    cv = verbs.add_parser("show", parents=[here], help="print one var's value, a lazy one included")
    cv.add_argument("--var", required=True, help="the var's name, as the restore prompt gives it")
    cv.add_argument("--id", default=None, help="a checkpoint id; default: the latest")
    return ap


def main(argv: list[str] | None = None) -> None:
    a = parser().parse_args(argv)
    if a.command == "agent":
        asyncio.run(agent_main(a))
    elif a.command == "areas":
        areas_main(a)
    elif a.command == "context":
        context_main(a)
    elif a.command == "chat":
        try:
            asyncio.run(chat_main(a))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
