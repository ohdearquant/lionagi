"""`lion agent`: an actor's long-running agent built from its directory, served, or run one step at a
time with its evidence landed there (ADR-0013, ADR-0016)."""

from __future__ import annotations

import argparse
import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any

from hub.agent.agent import Agent, AgentError, Budget, Gtd, Wake
from hub.agent.chores import Bounded, Chores, Outcome
from hub.agent.desk import Desk, Reader
from hub.agent.instruments import GH, GIT, LAUNCHCTL, instruments
from hub.agent.verdicts import Verdicts
from hub.guidance import guidance
from hub.harness import ClaudeCodeSession, OpenRouter
from hub.hooks import install, load
from hub.khive import Khive
from hub.tools import LocalTree, files, khive_ops
from lionagi import Actor, Profile
from lionagi.actor import DEFAULT_HANDLERS

__all__ = (
    "build",
    "chair",
    "land",
    "arm",
    "render",
    "stats",
    "backend_for",
    "agent_main",
)


def build(directory: Path, backend) -> tuple[Chores, Agent]:
    """The agent from its directory: the trusted senders are the floor (owner, steward, itself) plus
    the config's own `trusted` list; the khive client is bounded to read verbs and to those
    recipients and the desks `[desk.routes]` names; mail from anyone else is dropped at the agent's
    gate before any run."""
    cfg = tomllib.loads((directory / "chores.toml").read_text())
    actor_id = cfg["actor"]
    trusted = frozenset({cfg["owner"], cfg.get("steward"), actor_id, *cfg.get("trusted", [])} - {None})
    routes = dict((cfg.get("desk") or {}).get("routes", {}))  # the desk forwards to these; `Desk` checks them
    if routes and not cfg["desk"].get("front", True):  # no Desk would check them, yet they widen the sends
        raise ValueError("[desk.routes] needs the front desk: front = false names no routes")
    khive = Bounded(Khive(directory, actor_id), recipients=trusted | frozenset(routes.values()))
    actor = Actor("chores", privileges={"comm.send"}, notes_dir=directory / "notes")
    if (hooks := directory / "hooks.toml").exists():
        install(actor, load(hooks), directory)
    profiles: dict[str, Profile] = {}
    budget = Budget.load(cfg.get("budget"))  # `[budget]`: the caps, each defaulting to Budget's own
    agent = Agent(
        actor,
        khive,
        backend,
        lambda mail: profiles["wake"],
        # an actor as the chair takes every sender's mail (`[chair] admit = "all"`): the gate would silently
        # shelve a new peer; the guidance still says whose mail is work. Anything else keeps the trusted gate.
        admit=None
        if isinstance(cfg.get("chair"), dict) and cfg["chair"].get("admit") == "all"
        else (lambda m: m is not None and chores.admit(m)),
        owner=cfg["owner"],
        # sweep, route, OUT is three clean rounds; a slip (measured: an unquoted dict key) costs one each
        max_rounds=6,
        batch=int(cfg.get("batch", 5)),
        budget=budget,
        # the chair is one conversation for the life of the process: a burst of mail, a plain reply,
        # the next burst in the same run; no wake of its own for what arrives meanwhile
        continuous=isinstance(cfg.get("chair"), dict),
        # `[wake]`: which mail wakes now, which waits for a batch, which is dropped (none = every mail now);
        # the file is re-read while serving, so a cap or a rule edited from outside takes effect
        wake_rules=Wake.load(cfg.get("wake")),
        gtd=Gtd.load(cfg.get("gtd")),  # `[gtd]`: the queue as a wake source and a notification line
        config=directory / "chores.toml",
    )
    reader = None
    if cfg.get("desk"):  # the owner's box, read through a reader that only reads: the front desk's
        reader = Reader(Khive(Path(cfg["desk"]["actor_dir"]), cfg["owner"]))
    chores = Chores(
        agent,
        cfg["owner"],
        cfg.get("steward"),
        instruments(cfg, khive, agent.state),
        directory / "ledger.jsonl",
        trusted=trusted,
        aged_after=int(cfg.get("aged_after", 2)),
        aged_every=cfg.get("aged_every", "every:1h"),
        dead_after=int(cfg.get("dead_after", 3)),
    )
    # the front desk reads the owner's box through that reader; `front = false` builds no front desk
    if cfg.get("desk") and cfg["desk"].get("front", True):
        d = cfg["desk"]
        chores.digest_every = str(d.get("brief", "every:12h"))  # the digest is the front desk's brief
        v = d.get("verdicts")  # `[desk.verdicts]` absent: a verdict notice is listed like any mail
        if v is not None and not v.get("verdict_dir"):
            raise ValueError("[desk.verdicts] names the verdict_dir the review daemon writes its verdicts to")
        agent.desk = Desk(
            agent,
            chores,
            reader,
            directory / "desk.jsonl",
            answers_per_hour=int(d.get("answers_per_hour", 10)),
            clarify_hours=int(d.get("clarify_hours", 24)),
            escalate_from=tuple(d.get("escalate_from", ())),
            escalations_per_day=int(d.get("escalations_per_day", 40)),
            routes=routes,
            tasks_per_day=int(d.get("tasks_per_day", 20)),
            verdicts=None
            if v is None
            else Verdicts(
                verdict_dir=str(v["verdict_dir"]),
                actor_map=dict(v.get("actor_map", {})),
                actor_prefix=dict(v.get("actor_prefix", {})),
                author_actor=dict(v.get("author_actor", {})),
                stub_decisions=frozenset(v.get("stub_decisions", ["ENGINE-UNAVAILABLE"])),
            ),
            verdict_from=tuple((v or {}).get("from", ())),
        )
    profiles["chores"], profiles["reader"] = chores.profiles()
    # `[chair]`: the chair itself is the resident; admitted mail runs under its own hat, not the chores'
    profiles["wake"] = chair(directory, cfg, actor, khive) if "chair" in cfg else profiles["chores"]
    return chores, agent


def chair(directory: Path, cfg: dict, actor: Actor, khive: Bounded) -> Profile:
    """The chair as the resident: what the trusted senders mail is its own work, answered on the
    thread with `send`, with the directory's files, the bounded khive reads and its notes as the
    runtime's tools; `[chair].system` adds the chair's own role text. No chore runs under it. The
    runtime's tools cannot act irreversibly; the CLI's own tools under `[claude_code]` can (Bash
    reaches git and gh), and there the chair's red lines are guidance, as for a Claude Code session: no
    merge, no approve, no push, no process signal, no secret; a click stays with the steward."""
    if not isinstance(
        cfg["chair"], dict
    ):  # `chair = true` is not a section: name the shape, not an AttributeError
        raise ValueError("[chair] is a table: `[chair]` with an optional `system`")
    specs = files(actor, LocalTree(directory)) | khive_ops(actor, khive) | {"send"}
    specs |= {h.name for h in DEFAULT_HANDLERS}
    tools = (
        f"read(path), list_dir(path), khive_ops(ops) signing as {khive.actor} ({len(khive.allowed)} verbs: "
        "reads, comm.read, schedule.remind, memory.remember at spawn salience), send(to, thread, text), "
        "note.get(key), note.list()"
    )
    senders = ", ".join(sorted(khive.recipients or ()))
    if own := (cfg.get("claude_code") or {}).get("tools"):
        dirs = ", ".join([str(directory), *map(str, cfg["claude_code"].get("add_dirs", ()))])
        tools += (
            f"; and your own Claude Code tools ({', '.join(own)}) run inside each round in {dirs}, "
            "with the khive MCP signing as you when it is configured"
        )
    text = guidance("chair", actor=cfg["actor"], owner=cfg["owner"], senders=senders, tools=tools)
    if role := str(cfg["chair"].get("system", "")).strip():
        text += "\n\n" + role
    return Profile(name="chair", specs=frozenset(specs), privileges=frozenset({"comm.send"}), system=text)


def land(directory: Path, name: str, text: str) -> Path:
    """Landing evidence lives in the agent directory where a reader can open it, not on a console:
    `landing/<date>-<name>.txt`, overwritten by the day's latest run of that name."""
    path = directory / "landing" / f"{time.strftime('%Y%m%d')}-{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


async def arm(chores: Chores, agent: Agent) -> tuple[int, list[str]]:
    """The landing arm, run once from the agent directory: (1) identity, the actor's own inbox read
    through the bounded client returns rows addressed to it; (2) boundary, write verbs, a call the
    boundary cannot name, mail as an op, a memory above spawn salience and a send to an outsider are
    refused by the client, the client's own denial text quoted. 0 only when both hold."""
    rc, lines = 0, []
    (page,) = await agent.khive.exec('comm.inbox(limit=5, status="all")')
    rows = page.get("messages", []) if isinstance(page, dict) else (page or [])
    mine = [m for m in rows if (m.get("properties") or {}).get("to_actor") == agent.khive.actor]
    lines.append(
        f"identity: {agent.khive.actor} from {agent.khive.cwd}: {len(rows)} row(s), "
        f"{len(mine)} addressed to it"
    )
    if not mine:
        lines.append("identity: FAILED, no row addressed to the actor (seed one with comm.send first)")
        rc = 1
    for probe in (
        'gtd.complete(id="00000000-0000-0000-0000-000000000000")',
        'update(id="x", content="y")',
        'x.gtd.complete(id="a")',
        'comm.send(to="email:outsider@example.com", content="arm")',
        'memory.remember(content="arm", salience=0.9)',
    ):
        try:
            await agent.khive.exec(probe)
            lines.append(f"boundary: FAILED, {probe} was not refused")
            rc = 1
        except AgentError as e:
            lines.append(f"boundary: refused {probe}: {e}")
    try:
        await agent.khive.send("email:outsider@example.com", "arm")
        lines.append("boundary: FAILED, a send to an outsider was not refused")
        rc = 1
    except AgentError as e:
        lines.append(f"boundary: refused send to an outsider: {e}")
    lines.append(f"trusted senders: {sorted(chores.trusted)}")
    lines.append(f"programs: gh={GH} git={GIT} launchctl={LAUNCHCTL}")
    # the claude CLI child inherits this process's env and prefers ANTHROPIC_API_KEY when it is set: a
    # resident on the subscription shows both absent (the CLI's own login) or the OAuth token alone
    seen = {
        k: "set" if os.environ.get(k) else "absent" for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    }
    account = (
        os.environ.get("CC_ACCOUNT") or "unnamed (the CLI's own login)"
    )  # CC_ACCOUNT: the account's name, as whatever launched the agent set it
    lines.append(f"auth env the claude child would inherit: {seen}; account: {account}")
    if seen["ANTHROPIC_API_KEY"] == "set":
        lines.append("auth: FAILED, ANTHROPIC_API_KEY is set; a resident bills the key, not the subscription")
        rc = 1
    return rc, lines


def render(name: str, inst: Any, out: Outcome) -> str:
    """One instrument's outcome as the landing file and the console show it."""
    control = "ok" if out.control else "FAILED"
    lines = [
        f"{name}: control {control}, {len(out.findings)} finding(s), escalate={out.escalate}, "
        f"count={out.count}, direction {inst.direction}",
        f"answers: {inst.answers}",
        f"known-positive: {out.positive or '(none)'}",
        f"population: {out.population}\npredicate: {out.predicate}",
    ]
    lines += [f"- {f}" for f in out.findings]
    return "\n".join(lines + [out.evidence])


def stats(directory: Path) -> str:
    """The numbers behind "does a long-running agent save tokens": the day's spend row (model calls,
    tokens in and out, dollars, wakes that ran, mail handled, idle polls at zero) beside the last
    wake's cursor, read from the agent's own notes; nothing is computed that the agent did not write."""
    notes = directory / "notes" / "agent.json"
    if not notes.exists():
        return f"no notes at {notes}: the agent has not run from this directory"
    store = json.loads(notes.read_text()).get("notes", {})
    spend = (store.get("spend") or {}).get("value") or {}
    cursor = (store.get("cursor") or {}).get("value") or {}
    tokens = cursor.get("tokens") or {}
    mail = spend.get("mail", 0)
    lines = [
        f"day {spend.get('day')}: {spend.get('calls', 0)} model call(s), {spend.get('in', 0)} in, "
        f"{spend.get('out', 0)} out, ${spend.get('cost', 0.0):.4f}; {spend.get('runs', 0)} wake(s) with "
        f"{mail} mail; {spend.get('idle', 0)} idle poll(s) at 0 tokens",
        (
            f"per handled message: {(spend.get('in', 0) + spend.get('out', 0)) / mail:.0f} tokens"
            if mail
            else "per handled message: no mail handled today"
        ),
        f"last wake {cursor.get('wake')} at {cursor.get('at')}: {cursor.get('outcome')} after "
        f"{cursor.get('rounds')} round(s), {len(cursor.get('mail') or [])} mail, {cursor.get('sent')} sent; "
        f"{tokens.get('calls', 0)} call(s), {tokens.get('in', 0)} in, {tokens.get('out', 0)} out, "
        f"${tokens.get('cost', 0.0):.4f}; {cursor.get('idle_polls', 0)} idle poll(s) before it",
    ]
    ledger = directory / "ledger.jsonl"
    if ledger.exists():
        rows = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
        answered = sum(1 for r in rows if r.get("answered", r.get("control")))
        lines.append(f"ledger: {len(rows)} check(s), {answered} answered, {len(rows) - answered} not")
    desk = directory / "desk.jsonl"
    if desk.exists() and spend.get("day"):
        rows = [json.loads(ln) for ln in desk.read_text().splitlines() if ln.strip()]
        mine = [r for r in rows if r["at"].startswith(spend["day"])]
        answered = sum(1 for r in mine if r["class"] == "answered")
        left = len(mine) - answered
        lines.append(
            f"desk: {len(mine)} of the owner's message(s) read today, {answered} answered, {left} left"
        )
    return "\n".join(lines)


def backend_for(
    model: str, options: dict | None = None, *, cwd: Path | None = None
) -> OpenRouter | ClaudeCodeSession:
    """The console and agent backend: DeepSeek models route to CoreWeave, then DeepInfra or Fireworks,
    never the first-party endpoint; other models take OpenRouter's own routing. `claude_code` or
    `claude_code/<model>` is the claude CLI on the subscription, never an API key, on one resumed
    session per run: each round sends only what the view gained, so the prefix caches (the chat shape
    resent the whole transcript every round). `options`
    is the config's `[claude_code]` table: `tools` gives the CLI its own tools inside each round, in
    `cwd` (the agent directory: khive signs as its actor) with `add_dirs` reachable, `allowed` for
    permission rules beyond the tools, `mcp_config` for the one MCP it loads, `max_turns` per round,
    `timeout` seconds a round may take (a round of real work can run past the default, 300),
    `effort` the CLI's effort level."""
    if model == "claude_code" or model.startswith("claude_code/"):
        o = options or {}
        return ClaudeCodeSession(
            model=model.partition("/")[2] or None,
            tools=tuple(o.get("tools", ())),
            allowed=tuple(o.get("allowed", ())),
            max_turns=int(o.get("max_turns", 1)),
            timeout=float(o.get("timeout", 300.0)),
            effort=o.get("effort"),  # the CLI's --effort; absent = the CLI's default
            cwd=cwd,
            add_dirs=tuple(str(d) for d in o.get("add_dirs", ())),
            mcp_config=o.get("mcp_config"),
            cli=o.get("cli"),  # under launchd the PATH holds no claude: the config names the binary
        )
    deepseek = model.startswith("deepseek")
    return OpenRouter(
        model=model,
        timeout=120,
        provider=(
            {
                "order": ["coreweave", "deepinfra", "fireworks"],
                "allow_fallbacks": False,
                "data_collection": "deny",
            }
            if deepseek
            else None
        ),
        reasoning={"max_tokens": 2000},
        stop=["<system round="],
    )


async def agent_main(a: argparse.Namespace) -> None:
    directory = Path(a.dir).resolve()
    if a.stats:
        print(stats(directory))
        return
    cfg = tomllib.loads((directory / "chores.toml").read_text())
    backend = backend_for(
        a.model or cfg.get("model") or "deepseek/deepseek-v4.1-flash", cfg.get("claude_code"), cwd=directory
    )
    chores, agent = build(directory, backend)
    pid = agent._claim()  # one process per identity: every branch below acts as it, and serve keeps the lock
    try:
        if a.check:
            inst = dict(chores.instruments)[a.check]
            text = render(a.check, inst, await inst.run({}))
            print(text)
            print(f"landed: {land(directory, f'check-{a.check}', text)}")
            return
        if a.ticks:
            print("created:", [] if "chair" in cfg else await chores.ensure_ticks())
            return
        if a.arm:
            rc, lines = await arm(chores, agent)
            text = "\n".join(lines + [f"exit {rc}"])
            print(text)
            print(f"landed: {land(directory, 'arm', text)}")
            raise SystemExit(rc)
        if a.once or a.rerun:
            # one wake: the mail that arrives, or (--rerun) mail already read, back through a wake by id,
            # the batch a killed round left unanswered
            run = await agent.rerun(list(a.rerun)) if a.rerun else await agent.wake(wait_ms=a.wait * 1000)
            if run is None:
                print(f"no mail within {a.wait}s")
                return
            for e in run.record:
                print(
                    f"[{e.seq:>3} {e.name:>6} r{e.round}]",
                    str(e.content)[:600].replace("\n", "\n" + " " * 18),
                )
            return
        # a chair runs no chore: no digest, no aged pass, no tick in its box (each would be a model call)
        created = [] if "chair" in cfg else await chores.ensure_ticks()
        settings = (  # the values this process holds; the file may say otherwise until a restart
            f"; claude_code timeout={backend.timeout:.0f}s max_turns={backend.max_turns}"
            if isinstance(backend, ClaudeCodeSession)
            else ""
        )
        rules = agent.wake_rules  # [budget] and [wake] follow the file while serving; the start values here
        settings += (
            f"; budget cost_per_day={agent.budget.cost_per_day} wakes_per_hour={agent.budget.wakes_per_hour}"
            f"; wake {rules.describe() if rules is not None else 'every mail now'}"
            f"; gtd {agent.gtd.describe() if agent.gtd is not None else 'off'}"
        )
        print(
            f"serving {agent.khive.actor} from {directory} as pid {os.getpid()}; ticks created: {created}; "
            f"programs gh={GH} git={GIT}{settings}",
            flush=True,
        )
        await agent.serve(log=lambda line: print(time.strftime("%Y-%m-%d %H:%M:%S"), line, flush=True))
    finally:
        agent._release(pid)
