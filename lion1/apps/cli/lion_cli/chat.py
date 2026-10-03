"""`lion chat`: the actor a person talks to, built from the hub, and the command around the loop."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hub import Box, BoxTree, DaytonaSandbox, LocalTree, Sandbox, code, files, guidance, khive_ops, shell
from hub.agent.chores import SPAWN_SALIENCE, Bounded
from hub.areas import partial
from hub.backend import ClaudeCodeError, ClaudeCodeSession, CodexError, CodexSession, OpenRouterError
from hub.backend.claude_code import _prompt_tokens
from hub.chat import chat, load_log
from hub.hooks import install, load
from hub.khive import Khive, KhiveError
from hub.tools.code import Tree
from lion1 import Actor, Kind, Profile, lndl
from lion1.actor import DEFAULT_HANDLERS, Run, _text
from lion1.record import Entry

from .agent import backend_for
from .config import instructions
from .view import Console, cost_whole

__all__ = (
    "GIT_TIMEOUT",
    "console",
    "prepare_box",
    "take_patch",
    "spent",
    "signer",
    "status_line",
    "resolve_chat",
    "fresh_log",
    "unsettled",
    "Interrupt",
    "chat_main",
)

# seconds: a resume's patch check, git's own lookup included; the branch `--code` names; each of
# take_patch's calls on this machine (the box's own git for the diff runs under the box's exec timeout)
GIT_TIMEOUT = 30.0


def console(
    directory: Path,
    *,
    name: str = "console",
    system: str = "",
    model: str = "an unnamed model",
    turns: int = 200,
    sandbox: Box | None = None,
    coding: bool = False,
    notify: Callable[[str], None] | None = None,
    tree: Tree | None = None,
    about_box: str | None = None,
    khive_config: Path | None = None,
) -> tuple[Actor, Profile]:
    """The actor a person talks to: the file and directory readers, khive when the directory is an
    actor's own (its `.khive/config.toml` names the actor the reads sign as) or `khive_config` names
    the file to sign with instead (explicit: the directory's own is not read), a shell when a sandbox
    is given, and with `coding` the tools a fix is found and written with (read_lines, search, edit).
    The person's instruction files (`instructions`) close the guidance. `tree` is where the
    file and code tools read and write: the directory itself by default, the copy of it in a box
    with `about_box` saying so (the VM's own guidance otherwise)."""
    directory = Path(directory).resolve()
    tree = tree if tree is not None else LocalTree(directory)
    actor = Actor(name, notes_dir=directory / ".lion" / "notes")
    if (hooks := directory / ".lion" / "hooks.toml").exists():
        install(actor, load(hooks), directory)
    specs = files(actor, tree)
    tools = "read(path), list_dir(path)"
    about_sandbox = ""
    if sandbox is not None:
        specs |= shell(actor, sandbox)
        tools += ", bash(command), python(code)"
        if about_box is not None:
            about_sandbox = about_box
        else:
            network = " The VM has no network." if getattr(sandbox, "offline", False) else ""
            about_sandbox = guidance("sandbox", network=network)
    if coding:
        specs |= code(actor, tree)
        tools += ", read_lines(path, start, end), search(pattern, path, include), edit(path, old, new)"
        about_sandbox += "\n\n" + guidance("code", root=directory, branch=_branch(directory))
    config = khive_config if khive_config is not None else directory / ".khive" / "config.toml"
    if khive_config is not None and not config.is_file():  # explicit: said, never the directory's instead
        if notify is not None:
            notify(f"{config}: no such khive config file; khive tool unavailable")
    elif config.exists():
        actor_id = signer(directory, khive_config)
        if actor_id is None:
            if notify is not None:
                notify(f"{config}: no [actor].id; khive tool unavailable")
        else:
            try:
                khive = Bounded(Khive(directory, actor_id, config=khive_config), recipients=frozenset())
            except KhiveError as e:
                if notify is not None:
                    notify(f"khive unavailable: {e}")
            else:
                specs |= khive_ops(actor, khive)
                tools += (
                    f", khive_ops(ops) signing as {actor_id} (these verbs and no other: "
                    f"{', '.join(sorted(khive.allowed))}; memory.remember with an explicit salience <= "
                    f"{SPAWN_SALIENCE})"
                )
    if not system:
        system = guidance("console", model=model, tools=tools, turns=turns, sandbox=about_sandbox)
        for path, text in instructions(directory):
            system += f"\n\n{_shown(path, directory)} says:\n{text}"
    profile = Profile(name=name, specs=frozenset(specs) | {h.name for h in DEFAULT_HANDLERS}, system=system)
    return actor, profile


def _shown(path: Path, directory: Path) -> str:
    """An instruction file as the guidance and the person read it: from the chat's directory, from
    the home as `~/…`, else whole."""
    for base, lead in ((Path(directory).resolve(), ""), (Path.home().resolve(), "~/")):
        if path.is_relative_to(base):
            return lead + str(path.relative_to(base))
    return str(path)


async def prepare_box(box: Box, directory: Path, say: Callable[[str], None]) -> str:
    """Start `box`, push `directory` into it at the same path, and commit what the push archived as
    the copy's baseline in a repository of its own (`git ls-files` leaves `.git` out); a tracked file
    that .gitignore also matches is in the push, so it is added by name. Returns the baseline's SHA:
    the patch at the end reads against it, whatever the model commits in the box."""
    await box.start()
    pushed = await box.push(directory)  # type: ignore[attr-defined]
    say(f"box {getattr(box, 'image', '?')}: {len(pushed)} file(s) pushed to {box.workdir}")
    git = "git -c user.email=lion@localhost -c user.name=lion"
    line = f"cd {shlex.quote(box.workdir)} && {{ [ -e .git ] || git init -q; }} && {git} add -A && "
    if pushed:
        line += f"{git} --literal-pathspecs add -f --pathspec-from-file=- --pathspec-file-nul && "
    line += f"{git} commit -qm baseline --allow-empty && git rev-parse HEAD"
    r = await box.exec(
        ["bash", "-o", "pipefail", "-c", line],
        stdin=b"".join(os.fsencode(n) + b"\0" for n in pushed),
        timeout=300,
    )
    out = r["stdout"] + r["stderr"]
    sha = r["stdout"].strip().rsplit("\n", 1)[-1]
    if r["rc"] != 0 or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) is None:
        raise RuntimeError(f"the box's git baseline failed (rc {r['rc']}): {out[-400:]}")
    return sha


async def take_patch(
    box: Box,
    directory: Path,
    chats: Path,
    chat_id: str,
    *,
    baseline: str,
    apply: bool,
    say: Callable[[str], None],
) -> Path | None:
    """The diff of the copy in `box` since `baseline` (prepare_box's commit), printed and saved beside
    the chat log as `<chat_id>.patch`; with `apply` it is also applied to `directory` by `git apply`,
    from the top of the repository `directory` sits in (a patch from a subdirectory gets its prefix),
    which lands whole or not at all. None when nothing changed; a diff that fails raises. A patch that
    will not apply is still saved, the failure is said, and the directory is left as it was. Each git
    call on this machine has `GIT_TIMEOUT`; an apply stopped at it is said to leave the directory
    unknown. The diff is the box's own git, each call under the box's `exec` timeout (120 s)."""
    patch = await box.diff(baseline)
    if not patch.strip():
        say("box: no change in the copy")
        return None
    chats.mkdir(parents=True, exist_ok=True)
    path = chats / f"{chat_id}.patch"
    path.write_text(patch)
    say(f"box: the copy's diff, {patch.count(chr(10))} line(s), saved as {path}")
    print(patch, end="" if patch.endswith("\n") else "\n")
    try:
        argv = _git_apply(directory, str(path), timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:  # nothing ran on the directory
        say(f"box: not applied; git rev-parse did not answer within {GIT_TIMEOUT:g}s")
        return path
    if apply:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:  # stopped mid-way: what it wrote is unknown, never undone
            say(
                f"box: git apply did not finish within {GIT_TIMEOUT:g}s and was stopped; whether it "
                f"changed {directory} is unknown: read the files before applying {path} again"
            )
            return path
        if done.returncode == 0:
            say(f"box: applied to {directory} (git apply)")
        else:
            say(f"box: not applied, git apply exited {done.returncode}: {done.stderr.strip()[-300:]}")
    else:
        say(f"box: not applied; `{shlex.join(argv)}` does, or --apply next time")
    return path


def _git_apply(directory: Path, *args: str, timeout: float) -> list[str]:
    """`git apply <args>` for a chat patch over `directory`: from the top of the repository it sits in,
    with its prefix, or over plain files when it sits in none. Finding the top takes `timeout` at most."""
    top = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel", "--show-prefix"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if top.returncode == 0:  # from a subdirectory `git apply` skips every path and exits 0
        root, prefix = top.stdout.split("\n")[:2]
        return ["git", "-C", root, "apply", *([f"--directory={prefix}"] if prefix else []), *args]
    # not a repository: git apply works the directory as plain files
    return ["git", "-C", str(directory), "apply", *args]


def spent(calls: list[dict]) -> tuple[float, int]:
    """What the session was billed: each call's `total_cost_usd` (every attempt, the failed ones
    too), falling back to the last attempt's `usage.cost` for a backend that reports only that, a
    total of 0 being a bill of 0 so only an absent total falls back; and how many calls that sum
    misses, with no figure at all or the router's `cost_complete` false. A USAGE row read back from a
    log with no `cost_complete` is from before the key: whether its subtotal is whole is unknown, so
    it counts with them."""
    cost = 0.0
    for c in calls:
        billed = c.get("total_cost_usd")
        cost += float(billed if billed is not None else (c.get("usage") or {}).get("cost") or 0)
    unknown = sum(
        1 for c in calls if not cost_whole(c) or (c.get("kind") == "USAGE" and "cost_complete" not in c)
    )
    return cost, unknown


def signer(directory: Path, config: Path | None = None) -> str | None:
    """The khive actor the directory's `.khive/config.toml` names, or the one `config` names when it
    is given (then the directory's own is not read); None when the file is absent or names none."""
    config = config if config is not None else directory / ".khive" / "config.toml"
    if not config.is_file():
        return None
    return tomllib.loads(config.read_text()).get("actor", {}).get("id")


def status_line(
    actor: Actor, profile: Profile, runs: list[Run], backend: Any, model: str = "", who: str | None = None
) -> str:
    """`model · actor · inbox N · ctx ~F/B (last call P) · notes N (S chars) · $C`, read off the live
    objects each time: inbound not yet taken into a turn, the context figure the turn notice told
    against its budget (and the prompt the backend last reported), the profile's notes, what was
    billed. Before a run starts there is no context to tell."""
    parts = [f"inbox {len(actor.inbox) + sum(len(r.inbox) for r in runs)}"]
    if model:  # the model and whose name the khive reads and sends carry, first: who is talking
        parts = [model.split("/")[-1], who or "no actor"] + parts
    if runs:
        run = runs[0]
        ctx = f"ctx ~{_k(run.context_figure)}/{_k(run.bounds['context_budget'])}"
        if run.anchor is not None:
            ctx += f" (last call {_k(run.anchor[0])})"
        parts.append(ctx)
    notes = actor.notes_for(profile).list()
    size = sum(len(_text(n.value)) for n in notes)  # the size note.list tells the model
    parts.append(f"notes {len(notes)} ({_k(size)} chars)")
    cost, unknown = spent(getattr(backend, "calls", None) or [])
    parts.append(f"${cost:.4f}" + partial(unknown))
    return " · ".join(parts)


def _k(n: float) -> str:
    """12345 -> 12.3k, 200000 -> 200k: a count at a glance."""
    for scale, unit in ((1_000_000, "M"), (1_000, "k")):
        if n >= scale:
            return f"{n / scale:.1f}".removesuffix(".0") + unit
    return f"{n:g}"


def _branch(directory: Path) -> str:
    """The checked-out branch, from git itself (so a linked worktree's `.git` file and a
    subdirectory both resolve), `(no git)`, or that git did not answer within `GIT_TIMEOUT`."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        return "(no git)"
    except subprocess.TimeoutExpired:
        return f"(git did not answer within {GIT_TIMEOUT:g}s)"
    if result.returncode != 0:
        return "(no git)"
    return result.stdout.strip()


def resolve_chat(chats: Path, which: str) -> Path:
    """`last`, a chat id or a unique prefix of one (the log's stem), or a path to a log."""
    logs = sorted(chats.glob("*.jsonl"), key=lambda p: p.stem)  # by id: `X-02` after `X`, before `X+1s`
    if which == "last":
        if not logs:
            raise SystemExit(f"nothing to continue: no chat logged under {chats} yet")
        return logs[-1]
    hits = [p for p in logs if p.stem.startswith(which)]
    if exact := [p for p in hits if p.stem == which]:  # `X` beside `X-02`: the id as given wins
        return exact[0]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit(f"chat {which!r} is ambiguous: {', '.join(p.stem for p in hits)}")
    path = Path(which)
    if path.exists():
        return path
    raise SystemExit(
        f"no chat {which!r}: none of {len(logs)} under {chats} starts with it, and it is not a path"
    )


def fresh_log(chats: Path, chat_id: str) -> tuple[str, Path]:
    """A log file created here and now, exclusively: the id as given, or with `-02`, `-03`… when a
    session already took it this second (two consoles started together must not share a log)."""
    chats.mkdir(parents=True, exist_ok=True)
    for n in range(1, 100):
        name = chat_id if n == 1 else f"{chat_id}-{n:02d}"
        try:
            with (chats / f"{name}.jsonl").open("x"):
                return name, chats / f"{name}.jsonl"
        except FileExistsError:
            continue
    raise SystemExit(f"{chats}: 99 logs already start with {chat_id}")


def unsettled(history: tuple[Entry, ...]) -> list[str]:
    """The commands a logged session asked for and never saw settle: their aliases, in order. A
    session that ended mid-turn leaves them; the model is told, and nothing runs again on its own."""
    asked: list[str] = []
    settled = {e.alias for e in history if e.kind is Kind.RESULT}
    for e in history:
        if e.kind is not Kind.TEXT:
            continue
        try:
            asked += [la.alias for la in lndl.parse(str(e.content)).lacts]
        except lndl.LNDLError:
            continue
    return [a for a in asked if a not in settled and not a.startswith("_")]


class Interrupt:
    """Ctrl-C. Mid-run, the first one cuts the model's call short and hands the turn back now (or,
    with no call in flight, at the turn's end once its results land); the second one, or one at the
    prompt, ends the conversation now. The counter resets each time the person is asked."""

    def __init__(self, runs: list[Run], task: asyncio.Task, say: Console):
        self.runs, self.task, self.say = runs, task, say
        self.presses = 0

    def __call__(self) -> None:
        self.presses += 1
        if self.presses == 1 and self.runs and not self.say.asking:
            if self.runs[0].interrupt():
                self.say.notice("cut: the model's call was abandoned; Ctrl-C again to quit")
            else:
                self.say.notice("stopping after this turn; Ctrl-C again to quit now")
            return
        self.task.cancel()


async def chat_main(a: argparse.Namespace) -> None:
    directory = Path(a.dir).resolve()
    chats = directory / ".lion" / "chats"
    prior = resolve_chat(chats, a.resume) if a.resume else None  # before this session's own log exists
    if a.log:
        log = Path(a.log)
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log.open("x"):  # created here, exclusively, as `fresh_log` does: one session owns a log
                pass
        except FileExistsError:
            raise SystemExit(
                f"{log} exists; `lion chat -r {log}` continues it, another --log starts fresh"
            ) from None
        chat_id = log.stem
    else:
        chat_id, log = fresh_log(chats, time.strftime("%Y%m%d-%H%M%S"))
    try:
        await _chat_main(a, directory, chats, prior, chat_id, log)
    except BaseException:
        # a start that failed before anything was logged (a bad hooks.toml, no image) must not leave
        # an empty log as `-c`'s next target: the previous history would be lost to the next session
        if log.exists() and log.stat().st_size == 0:  # both paths created the log above, so it is ours
            log.unlink()
        raise


async def _chat_main(
    a: argparse.Namespace, directory: Path, chats: Path, prior: Path | None, chat_id: str, log: Path
) -> None:
    history: tuple = ()
    resumed = notice = None
    ran: dict = {}  # where the continued chat ran, from its log's BOX row; a flag given now wins
    billed: list[dict] = []  # its USAGE rows: the last session call's count seeds the new session's figure
    if prior is not None:
        torn: list[str] = []
        history = load_log(prior, torn, ran, billed)
        resumed = (prior.stem, len(history))
        notes = [f"the previous session's log ended mid-write: {t}" for t in torn]
        if left := unsettled(history):
            notes.append(
                f"the previous session ended before these commands settled: {', '.join(left)}. "
                "They did not run again; ask again for any you still need."
            )
        patch = chats / f"{prior.stem}.patch"
        if patch.exists():
            # does the saved patch come off the directory as it stands (applied), or go on whole (not
            # applied)? Anything else is unknown, told with git's reason; one bound covers every call
            deadline = time.monotonic() + GIT_TIMEOUT
            standing, why = None, ""  # applied: nothing to tell
            try:
                check = _git_apply(directory, "--check", timeout=GIT_TIMEOUT)
                off = subprocess.run(
                    [*check, "-R", str(patch)], capture_output=True, timeout=deadline - time.monotonic()
                )
                if off.returncode != 0:
                    on = subprocess.run(
                        [*check, str(patch)], capture_output=True, timeout=deadline - time.monotonic()
                    )
                    standing = "are not applied to"
                    if on.returncode != 0:  # unreadable, a file changed since, a broken repository
                        first = (off.stderr.decode(errors="replace").strip().splitlines() or [""])[0]
                        why = f" (git apply exited {off.returncode}: {first[:300]})"
            except FileNotFoundError:  # no git here: unchecked, and the model is told that much
                why = " (no git)"
            except subprocess.TimeoutExpired:
                why = f" (git did not answer within {GIT_TIMEOUT:g}s)"
            if why:
                standing = "could not be checked against"
            if standing is not None:
                notes.append(
                    f"the previous session's changes, saved as {patch}, {standing} {directory} as "
                    f"it stands{why}, so edits the record shows may be missing from the files you work "
                    "now; read a file before relying on an edit to it."
                )
        notice = " ".join(notes) or None
    box = getattr(a, "box", None) or ran.get("box", "vm")
    image = getattr(a, "image", None) or ran.get("image", "python:3.12")
    boxed = box == "daytona"
    sandbox: Box | None = None
    tree = about_box = None
    if boxed:
        sandbox = DaytonaSandbox(image, workdir=str(directory))
        tree = BoxTree(sandbox)
        about_box = guidance("box", kind="Daytona", image=image, root=directory)
    elif a.shell or a.code:
        sandbox = Sandbox(directory, f"lion-{chat_id}", offline=a.offline)
    degraded: list[str] = []
    khive_config = None
    if getattr(a, "khive_config", None):  # explicit: refused outright rather than falling back
        khive_config = Path(a.khive_config).expanduser().resolve()
        if not khive_config.is_file():
            raise SystemExit(f"--khive-config {a.khive_config}: no such file")  # as typed, not resolved
        if signer(directory, khive_config) is None:
            raise SystemExit(f"--khive-config {a.khive_config}: no [actor].id in it")
    actor, profile = console(
        directory,
        model=a.model,
        turns=a.turns,
        sandbox=sandbox,
        coding=a.code,
        notify=degraded.append,
        tree=tree,
        about_box=about_box,
        khive_config=khive_config,
    )
    # a claude_code or codex model gets no tools of its own: one turn per call, resumed, and everything
    # it does is a command in its text that this console runs, gates and records
    backend = backend_for(a.model)
    if isinstance(backend, ClaudeCodeSession | CodexSession):
        # a continued chat's new session starts from the whole view: its first figure stands on the
        # count the last session call reported and the characters it stood in (ADR-0009/C4, C7); a
        # codex call whose share of its thread's totals was unknown reported none
        last = next((r for r in reversed(billed) if "prompt_chars" in r and not r.get("usage_unknown")), None)
        if last is not None and (count := _prompt_tokens(last)) is not None:
            backend.base = (count, int(last["prompt_chars"]))
    runs: list[Run] = []
    say = Console(
        a.model,
        log,
        raw=a.raw,
        echo=a.say is not None or not sys.stdin.isatty(),
        verbose=a.verbose,
        calls=getattr(backend, "calls", None),
        head={"kind": "BOX", "box": box} | ({"image": image} if boxed else {}),
        history=directory / ".lion" / "history",
        status=lambda: status_line(actor, profile, runs, backend, a.model, signer(directory, khive_config)),
        directory=directory,
    )
    say.quiet_until = len(history) - 1
    say.open(a.model, directory, chat_id, sorted(profile.specs), resumed)
    for msg in degraded:
        say.notice(msg)
    if composed := instructions(directory):
        say.notice(
            "instructions: " + ", ".join(f"{_shown(p, directory)} ({len(t)} chars)" for p, t in composed)
        )
    if notice:
        say.notice(notice)
    if hooked := len(actor.befores) + len(actor.afters) + len(actor.sections):
        say.notice(f"{hooked} hook(s) from .lion/hooks.toml")
    baseline = ""  # the box copy's baseline commit, this conversation's own
    if boxed:
        try:
            baseline = await prepare_box(sandbox, directory, say.notice)
        except BaseException:  # before any model work: nothing in the box is worth keeping
            await asyncio.shield(sandbox.stop())
            raise
    elif sandbox is not None:
        if (build := await sandbox.ensure_image()) is not None:
            raise SystemExit(f"the sandbox image {sandbox.image} did not build:\n{build}")
        say.notice(f"sandbox {sandbox.name}: a VM over {directory}" + (", no network" if a.offline else ""))
        await sandbox.start()
    off = actor.bus.subscribe("run.started", lambda ev: runs.append(actor.runs[ev.payload["run"]]))
    off_turn = actor.bus.subscribe("turn.started", lambda ev: say.working("thinking"))
    interrupt = Interrupt(runs, asyncio.current_task(), say)
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, interrupt)

    async def ask() -> str | None:
        while True:
            interrupt.presses = 0
            text = await say.ask()
            if text is None or not say.command(text):  # a console command is run here, then ask again
                return text

    interrupted = failed = False
    try:
        run = await chat(
            actor,
            profile,
            backend,
            ask,
            say=say,
            first=a.say,
            max_turns=a.turns,
            history=history,
            notice=notice,
        )
        ended = f"ended {type(run.outcome).__name__} after {run.turn} turn(s)"
    except (KeyboardInterrupt, asyncio.CancelledError):
        ended, interrupted = "interrupted", True
    except (ClaudeCodeError, CodexError, OpenRouterError) as e:
        # the backend's own refusal (a model the CLI does not know, a timeout, a rate limit): one
        # line with its message, then the closing line; the record keeps the message that was sent
        say.failed(f"the model's call failed: {e}")
        ended, failed = "failed", True
    finally:
        off()
        off_turn()
        if sandbox is not None:
            try:
                if boxed and sandbox.dead is not None:
                    say.notice(
                        f"box: it ended before its patch was read ({sandbox.dead}); recovery unavailable"
                    )
                elif boxed:  # the patch first: stop deletes the box
                    try:
                        await asyncio.shield(
                            take_patch(
                                sandbox,
                                directory,
                                chats,
                                chat_id,
                                baseline=baseline,
                                apply=a.apply,
                                say=say.notice,
                            )
                        )
                    except Exception as e:  # the patch was not taken whole: the box may hold the only copy
                        ident = getattr(sandbox, "box_id", None)
                        why = f"taking its patch failed ({type(e).__name__}: {e})"
                        if sandbox.dead is None and ident:
                            say.notice(
                                f"box {ident}: kept, not stopped: {why}; the work is there, at "
                                f"{sandbox.workdir}, until the box's idle stop deletes it"
                            )
                        else:
                            reason = sandbox.dead or "the provider named no box"
                            say.notice(f"box {ident or '?'}: {why}; recovery unavailable: {reason}")
                        raise
                await asyncio.shield(sandbox.stop())
            except asyncio.CancelledError:  # a third Ctrl-C while the VM stops: still close, still leave
                ended, interrupted = "interrupted", True
    calls = getattr(backend, "calls", [])
    say.close(ended, len(calls), *spent(calls), log)
    if failed:
        raise SystemExit(1)
    if interrupted:
        # a backend call may still be on its thread; the loop's shutdown would wait for it
        # (measured: a Ctrl-C that seemed to do nothing). The log is written; leave now.
        sys.stdout.flush()
        os._exit(130)
