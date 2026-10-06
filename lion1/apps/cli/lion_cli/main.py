"""The parser and the entry: `lion <subcommand>`."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from hub.backend.claude_code.checkpoint import SESSION_KINDS, SESSION_WHYS
from hub.flow import FlowError

from .agent import agent_main
from .areas import areas_main
from .chat import chat_main
from .config import chat_config, config_main
from .context import BUDGET, context_main
from .submit import SubmitError, flow_main, result_main, run_main

__all__ = ("parser", "main")


def _run_id(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("expected a run id")
    return value


def parser(chat: dict | None = None) -> argparse.ArgumentParser:
    """The command tree; `chat` replaces `lion chat`'s built-in defaults (a flag on the line still wins)."""
    ap = argparse.ArgumentParser(prog="lion", description="lion1")
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
    ag.add_argument(
        "--forget",
        default=None,
        type=_run_id,
        metavar="RUN",
        help="let go of the run a refused wake names, so the next wake starts a new chain; then exit",
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
    ch.add_argument("--turns", type=int, default=200, help="the run's turn budget; the model is told")
    ch.add_argument(
        "--shell",
        action="store_true",
        help="give the model bash and python in a Linux VM over this directory",
    )
    ch.add_argument("--offline", action="store_true", help="the VM gets no network (with --shell)")
    ch.add_argument(
        "--box",
        choices=("vm", "daytona"),
        default=None,
        help="where bash, python and the file tools work: the local VM over this directory (vm), or a "
        "Daytona box the directory is pushed into at the same path; its diff comes back as a patch. "
        "Default: where a continued chat ran, else vm",
    )
    ch.add_argument(
        "--image",
        default=None,
        help="the Daytona box's image (with --box daytona); default: a continued chat's, else python:3.12",
    )
    ch.add_argument(
        "--apply",
        action="store_true",
        help="apply the box's patch to this directory at the end (git apply); default: print + save",
    )
    ch.add_argument(
        "--code",
        action="store_true",
        help="the coding preset: read_lines, search and edit on this directory, and the shell VM (implies "
        "--shell)",
    )
    ch.add_argument(
        "--khive-config",
        default=None,
        metavar="FILE",
        help="the khive config the chat signs with (its [actor].id, passed to kkernel as --config), over "
        "the directory's own .khive/config.toml; a missing file refuses to start rather than falling back",
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
    ch.set_defaults(**(chat or {}))
    after = argparse.ArgumentParser(add_help=False)  # where the result goes (ADR-0026 C4)
    after.add_argument("--thread", default=None, help="the thread the result mail goes on")
    after.add_argument(
        "--notify",
        default=None,
        metavar="ACTOR",
        help="who the result mail goes to; default: the directory's actor",
    )
    after.add_argument(
        "--var",
        action="append",
        default=None,
        metavar="NAME=VALUE",
        help="a variable the text names as {NAME}",
    )
    rn = sub.add_parser(
        "run", parents=[after], help="one job of a profile from the directory's profiles.toml; its id first"
    )
    rn.add_argument("--dir", default=".", help="the actor's directory: .khive/config.toml, profiles.toml")
    rn.add_argument("--profile", required=True, help="a [profile.<name>] of profiles.toml")
    rn.add_argument("--say", required=True, help="the job; a template of the --var names when any is given")
    rn.add_argument(
        "--file", action="append", default=None, help="a file whose text is an input after --say (repeatable)"
    )
    rn.add_argument("--model", default=None, help="the model; overrides the profile's")
    fl = sub.add_parser(
        "flow",
        parents=[after],
        help="a [flow.<name>] of the directory's flows.toml; its id, then each step's",
    )
    fl.add_argument("--dir", default=".", help="the actor's directory: .khive/config.toml, flows.toml")
    fl.add_argument("--name", required=True, help="the flow")
    rs = sub.add_parser("result", help="a run's or a flow's row with its output, read back by id")
    rs.add_argument("id", type=_run_id, help="the id `lion run` or `lion flow` printed first")
    rs.add_argument("--dir", default=".", help="the actor's directory the run's rows are in")
    rs.add_argument("--history", action="store_true", help="the run's messages too, in order")
    cf = sub.add_parser(
        "config", help="the [chat] values a bare `lion chat` runs with, and the file each came from"
    )
    cf.add_argument(
        "--dir", default=".", help="the chat's directory: its .lion/config.toml wins over ~/.lion/config.toml"
    )
    ar = sub.add_parser(
        "areas", help="the residents by area: serving, in a wake, wakes, today's runs and cost"
    )
    ar.add_argument(
        "--file", default="~/.lionagi/areas.toml", help="the registry: [area.<name>] chair, desks"
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
    cx = sub.add_parser(
        "context", help="a Claude Code or Codex session's checkpoint in the store, and its restore (ADR-0019)"
    )
    verbs = cx.add_subparsers(dest="verb", required=True)
    cs = verbs.add_parser(
        "set", parents=[here], help="declare a var; every restore carries it until it is declared again"
    )
    cs.add_argument("--name", required=True)
    cs.add_argument("--kind", required=True, choices=SESSION_KINDS)
    cs.add_argument("--value", required=True, help="text, or JSON when it opens with { or [")
    cc = verbs.add_parser(
        "checkpoint",
        parents=[here],
        help="write the session's exchanges since the last take into the store, no model call",
    )
    cc.add_argument("--why", choices=SESSION_WHYS, default="manual")
    cc.add_argument(
        "--session", default=None, help="default: the newest transcript's session; with --codex, a thread id"
    )
    cc.add_argument(
        "--transcript",
        default=None,
        help="default: the newest *.jsonl under ~/.claude/projects/<cwd slug>/, never a _precompact_ copy; "
        "a Codex rollout given here is taken as a Codex session",
    )
    cc.add_argument(
        "--budget", type=int, default=BUDGET, help="tokens a restore folds to; a take keeps every exchange"
    )
    how = cc.add_mutually_exclusive_group()
    how.add_argument(
        "--hook",
        action="store_true",
        help="run as Claude Code's PreCompact hook: the session, transcript and cwd come on stdin, the "
        "reason is precompact",
    )
    how.add_argument(
        "--codex",
        action="store_true",
        help="a Codex session: --session's rollout, else the newest under $CODEX_HOME/sessions whose cwd "
        "is --dir, never a sub-agent's",
    )
    cr = verbs.add_parser("restore", parents=[here], help="print the first prompt of the fresh session")
    cr.add_argument(
        "--id",
        default=None,
        help="a session id, or <session>@<turns>; default: the latest session at its last turn",
    )
    cr.add_argument("--json", action="store_true", help="print {id, first_prompt, watches, spawns}")
    ct = verbs.add_parser("status", parents=[here], help="the latest checkpoint and the figure now")
    ct.add_argument(
        "--session", default=None, help="default: the newest transcript's session; with --codex, a thread id"
    )
    ct.add_argument(
        "--codex", action="store_true", help="the figure now is a Codex session's, from its rollout"
    )
    cv = verbs.add_parser("show", parents=[here], help="print one var's value, a lazy one included")
    cv.add_argument("--var", required=True, help="the var's name, as the restore prompt gives it")
    cv.add_argument(
        "--id",
        default=None,
        help="the checkpoint id the restore prompt names; a declared value is the actor's whichever",
    )
    return ap


def main(argv: list[str] | None = None) -> None:
    ap = parser()
    a = ap.parse_args(argv)
    if a.command == "agent" and a.forget is not None:
        # each action exits after itself, so another one beside the forget would run in its place
        beside = [f"--{k}" for k in ("once", "rerun", "check", "ticks", "arm", "stats") if getattr(a, k)]
        if beside:
            ap.error(f"argument --forget: not allowed with {', '.join(beside)}")
    if a.command == "chat":
        # the directory's file is found by --dir, so the first parse reads it; the second takes the
        # files' values as defaults, which a flag given on the line beats even when it equals the default
        a = parser({k: v for k, (v, _) in chat_config(Path(a.dir)).items()}).parse_args(argv)
    if a.command == "agent":
        asyncio.run(agent_main(a))
    elif a.command == "areas":
        areas_main(a)
    elif a.command == "context":
        context_main(a)
    elif a.command == "config":
        config_main(a, parser().parse_args(["chat"]))
    elif a.command == "chat":
        try:
            asyncio.run(chat_main(a))
        except KeyboardInterrupt:
            pass
    elif a.command in ("run", "flow", "result"):
        act = {"run": run_main, "flow": flow_main, "result": result_main}[a.command]
        try:
            code = asyncio.run(act(a))
        except (SubmitError, FlowError) as e:  # refused before any row
            raise SystemExit(f"lion {a.command}: {e}") from None
        except (KeyboardInterrupt, asyncio.CancelledError):  # interrupted: its rows and mail are done
            raise SystemExit(130) from None
        raise SystemExit(code)


if __name__ == "__main__":
    main()
