"""The probe, the pick and `lion account probe` (ADR-0024/D4, D5, D8). The CLIs are scripts each test
writes in its own directory: each reads a plan by the account it runs for and appends every call to a
file. No test reaches a provider, the keychain (`security` is the script of lion1/tests/test_accounts.py) or a
person's Codex home, and every token is made up."""

import contextlib
import functools
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import lion_cli.account as account_cli
import pytest

import hub.accounts as accounts_module
from hub.accounts import (
    CREDENTIAL_VARIABLES,
    Account,
    AccountsError,
    Hold,
    Kept,
    Mark,
    Pool,
    Probe,
    Probed,
    Settings,
    Window,
    _time,
    held,
    load_pool,
    pick,
    probe,
    read_evidence,
    store_token,
)
from tests.test_accounts import CANARY, REPO, SHAPED, keychain, parts, run  # noqa: F401  (a fixture)

CLAUDE = """#!PYTHON
import json, os, re, subprocess, sys, time
from pathlib import Path

plan = json.loads(Path("PLAN").read_text()) if Path("PLAN").exists() else {}
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
own = re.fullmatch(r"sk-ant-oat01-made-up-([a-z]+)-(Zq7xW){8}", token)
who = own.group(1) if own else "default"
step = plan.get(who, {})
prompt = sys.stdin.read()
with open("CALLS", "a") as f:
    # the variables' names only, and of the token only whether it is the one made up for this account
    call = {"cli": "claude", "who": who, "token": bool(own), "argv": sys.argv[1:]}
    call["names"] = sorted(os.environ)
    f.write(json.dumps({**call, "prompt": prompt}) + "\\n")
if step.get("leak"):  # what a CLI prints may hold its credential
    print("token " + token, flush=True)
    print("token " + token, file=sys.stderr, flush=True)
if step.get("hang"):  # a child of its own that holds its stdout open, and no end
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path("PIDS").write_text(f"{os.getpid()} {child.pid}")
    time.sleep(30)
if step.get("linger"):  # a child of its own left running when it ends, on no pipe of the probe's
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **quiet)
    Path("PIDS").write_text(f"{os.getpid()} {child.pid}")
if step.get("detach"):  # a child that leaves its group for a session of its own, holding its stdout
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    Path("PIDS").write_text(f"{os.getpid()} {child.pid}")
    time.sleep(30)
time.sleep(step.get("sleep", 0))
now = int(time.time())
print(json.dumps({"type": "system", "subtype": "init"}))
for windows in step.get("rows", []):
    resets = {"five_hour": now + 3600, "seven_day": now + 86400}
    unified = {w: {"utilization": u, "resetsAt": resets[w]} for w, u in windows.items()}
    info = {"status": "allowed", "unifiedWindows": unified}
    print(json.dumps({"type": "rate_limit_event", "rate_limit_info": info}))
print(json.dumps({"type": "result", "subtype": "success", "result": "ok"}))
sys.exit(step.get("rc", 0))
"""
CODEX = """#!PYTHON
import json, os, subprocess, sys, time, uuid
from pathlib import Path

plan = json.loads(Path("PLAN").read_text()) if Path("PLAN").exists() else {}
args = sys.argv[1:]
home = os.environ.get("CODEX_HOME") or os.path.join(os.environ["HOME"], ".codex")
who = Path(home).name if os.environ.get("CODEX_HOME") else "default"
step = plan.get(who, {})
prompt = sys.stdin.read() if args[:1] == ["exec"] else ""
with open("CALLS", "a") as f:
    call = {"cli": "codex", "who": who, "argv": args, "names": sorted(os.environ), "home": home}
    f.write(json.dumps({**call, "cwd": os.getcwd(), "prompt": prompt}) + "\\n")


def hang():  # a child of its own that holds its stdout open, and no end
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path("PIDS").write_text(f"{os.getpid()} {child.pid}")
    time.sleep(30)


if args == ["--version"]:
    if step.get("hang") == "version":
        hang()
    print(step.get("release", "codex-cli 0.156.1"))
    sys.exit(0)
if args[:2] == ["debug", "models"]:
    print(json.dumps({"models": [{"slug": "small", "display_name": "small", "shell_type": "local"}]}))
    sys.exit(0)
if step.get("hang") == "exec":
    hang()
if step.get("linger"):  # a child of its own left running when it ends, on no pipe of the probe's
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **quiet)
    Path("PIDS").write_text(f"{os.getpid()} {child.pid}")
thread = step.get("thread") or str(uuid.uuid4())
stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def count(limits):
    payload = {"type": "token_count", "rate_limits": limits}
    return {"timestamp": stamp, "type": "event_msg", "payload": payload}


rows = [{"timestamp": stamp, "type": "session_meta", "payload": {"id": thread}}]
if "limits" in step:  # an earlier row first: the last one is the reading
    rows += [count({"primary": {"used_percent": 99.0, "window_minutes": 300}, "secondary": None})]
    rows += [count(step["limits"])]
rows += [{"timestamp": stamp, "type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}}]
if "limits" in step or step.get("rollout"):
    day = Path(home, "sessions", "2026", "10", "09")
    day.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\\n" for r in rows)
    Path(day, f"rollout-2026-10-09T00-00-00-{thread}.jsonl").write_text(text)
if not step.get("no thread"):
    print(json.dumps({"type": "thread.started", "thread_id": thread}))
print("a line that is not json")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
sys.exit(step.get("rc", 0))
"""
# what the process this runtime runs in may hold: a credential of each CLI that no child may inherit
COMPETING = {
    "ANTHROPIC_API_KEY": "made-up-competing-key-01",
    "CLAUDE_CODE_OAUTH_TOKEN": "made-up-inherited-token-01",
    "CC_ACCOUNT": "another",
    "CODEX_ACCESS_TOKEN": "made-up-codex-access-01",
}
ARGV = [
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--no-session-persistence",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--model",
    "haiku",
    "--tools",
    "",
    "--max-turns",
    "1",
]
PROMPT = "Reply with the single word: ok"
FIGURES = [{"five_hour": 0.46, "seven_day": 0.64}]


def made_up(name: str) -> str:
    """The token made up for an account, in the form a token takes; the scripts know an account by it."""
    return f"sk-ant-oat01-made-up-{name}-" + "Zq7xW" * 8


@pytest.fixture(autouse=True)
def person(tmp_path, monkeypatch) -> Path:
    """A home of the test's own and no credential variable, so nothing here reads a person's Codex home
    or hands a child a credential this process happened to hold."""
    home = tmp_path / "person"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for v in ("CODEX_HOME", *CREDENTIAL_VARIABLES["claude_code"], *CREDENTIAL_VARIABLES["codex"]):
        monkeypatch.delenv(v, raising=False)
    return home


@pytest.fixture
def clis(tmp_path, monkeypatch):
    """`claude` and `codex` as scripts in a directory put first on PATH."""
    where = tmp_path / "bin"
    where.mkdir()
    plan, calls, pids = tmp_path / "plan.json", tmp_path / "calls.jsonl", tmp_path / "pids"
    for name, text in (("claude", CLAUDE), ("codex", CODEX)):
        script = where / name
        text = text.replace("PYTHON", sys.executable).replace("PLAN", str(plan))
        script.write_text(text.replace("CALLS", str(calls)).replace("PIDS", str(pids)))
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{where}{os.pathsep}{os.environ['PATH']}")

    def logged(cli: str | None = None) -> list[dict]:
        rows = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return [r for r in rows if cli is None or r["cli"] == cli]

    return SimpleNamespace(
        claude=str(where / "claude"),
        codex=str(where / "codex"),
        plan=lambda steps: plan.write_text(json.dumps(steps)),
        calls=logged,
        pids=pids,
    )


def environ(home: Path, **more: str) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": str(home), **COMPETING, **more}


def gone(pid: int) -> bool:
    """The process is gone, a killed one reaped by whoever reaps it, within five seconds."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


OPENED: list[str] = []


def watch_logins() -> list[str]:
    """Every login file this process opens from here on, by an audit hook, which no open escapes."""

    def hook(event: str, args: tuple) -> None:
        if event == "open" and isinstance(args[0], str | bytes | os.PathLike):
            if os.path.basename(os.fsdecode(args[0])) == "auth.json":
                OPENED.append(os.fsdecode(args[0]))

    if not getattr(watch_logins, "hooked", False):
        sys.addaudithook(hook)
        watch_logins.hooked = True  # type: ignore[attr-defined]
    OPENED.clear()
    return OPENED


@pytest.mark.usefixtures("keychain")
def test_a_claude_code_probe_reads_the_streams_limit_rows_on_the_probe_model(person, clis):
    store_token("work", made_up("work"))
    clis.plan({"work": {"rows": [{"five_hour": 0.9, "seven_day": 0.9}, *FIGURES]}})
    t0 = time.time()
    got = probe(Account("work", "claude_code", "keychain"), Settings(), environ(person), claude=clis.claude)
    t1 = time.time()
    assert (got.ended, got.why) == ("figures", None) and t0 <= got.at <= t1
    assert {w: r.used for w, r in got.readings.items()} == {"five_hour": 0.46, "seven_day": 0.64}  # the last
    assert all(r.source == "probe" and t0 <= r.observed <= t1 < r.resets for r in got.readings.values())
    (call,) = clis.calls()
    assert call["argv"] == ARGV and call["prompt"] == PROMPT
    # the CLI's own model when none is set; the one set otherwise
    clis.plan({"default": {}})
    own = Account("default", "claude_code", "default")
    for models, want in (({"claude_code": None}, []), ({"claude_code": "small"}, ["--model", "small"])):
        assert (
            probe(own, Settings(probe_model=models), environ(person), claude=clis.claude).ended == "no figure"
        )
        assert clis.calls()[-1]["argv"] == ARGV[:8] + want + ARGV[10:]


def test_a_claude_code_probe_whose_stream_has_no_limit_row_gives_no_figure(person, clis):
    clis.plan({"default": {"rows": [{"five_hour": "high"}]}})  # a row of another shape is no figure either
    got = probe(Account("default", "claude_code", "default"), Settings(), environ(person), claude=clis.claude)
    assert got == Probed(got.at, "no figure", {}, None)


def codex_home(tmp_path: Path, name: str) -> Path:
    home = tmp_path / name
    home.mkdir()
    (home / "auth.json").write_text("{}")  # the account's login, never opened by the runtime (C1)
    return home


def test_a_codex_probe_reads_the_last_token_count_row_of_its_rollout_under_the_accounts_home(
    tmp_path, person, clis
):
    home = codex_home(tmp_path, "cx-a")
    resets = int(time.time()) + 86400
    seven = {"used_percent": 24.0, "window_minutes": 10080, "resets_at": resets}
    clis.plan({"cx-a": {"limits": {"primary": seven, "secondary": None}}})
    opened = watch_logins()
    t0 = int(time.time())
    settings = Settings(probe_model={"claude_code": "haiku", "codex": "small"})
    got = probe(Account("cx-a", "codex", str(home)), settings, environ(person), codex=clis.codex)
    assert (got.ended, got.why) == ("figures", None)
    observed = got.readings["seven_day"].observed
    assert t0 <= observed <= time.time()  # the row's own timestamp, to its second
    assert got.readings == {
        "seven_day": Window(0.24, float(resets), observed, "probe"),
        "five_hour": Window(None, None, observed, "probe"),  # its slot empty beside the seven days: absent
    }
    calls = clis.calls()
    assert [c["argv"][:2] for c in calls] == [["--version"], ["debug", "models"], ["exec", "--json"]]
    assert {c["home"] for c in calls} == {str(home)} and calls[-1]["prompt"] == PROMPT
    run_ = calls[-1]["argv"]
    assert "--ephemeral" not in run_ and run_[-1] == "-" and run_[run_.index("-m") + 1] == "small"
    assert 'sandbox_mode="read-only"' in run_ and "shell_tool" in run_  # the backend's own flags
    assert not Path(calls[-1]["cwd"]).exists()  # its scratch directory went with it
    assert opened == []  # neither this process nor the probe opened the login file
    # default: the CLI's own model, and the home the environment names, else the person's
    clis.plan({"default": {"limits": {"primary": None, "secondary": {**seven, "window_minutes": 300}}}})
    got = probe(Account("default", "codex", "default"), Settings(), environ(person), codex=clis.codex)
    assert set(got.readings) == {"five_hour", "seven_day"} and got.readings["seven_day"].used is None
    assert clis.calls()[-1]["home"] == str(person / ".codex") and "-m" not in clis.calls()[-1]["argv"]


@pytest.mark.parametrize(
    "step",
    [
        {"no thread": True, "limits": {"primary": None, "secondary": None}},
        {"thread": "not-a-thread", "limits": {"primary": None, "secondary": None}},
        {},  # a thread with no rollout
        {"rollout": True},  # a rollout with no token count
        {"limits": None},  # a token count with no rate limits
    ],
)
def test_a_codex_probe_whose_rollout_gives_no_reading_gives_no_figure(tmp_path, person, clis, step):
    home = codex_home(tmp_path, "cx-a")
    clis.plan({"cx-a": step})
    got = probe(Account("cx-a", "codex", str(home)), Settings(), environ(person), codex=clis.codex)
    assert (got.ended, got.readings, got.why) == ("no figure", {}, None)


@pytest.mark.parametrize("cli", ["claude", "codex"])
@pytest.mark.usefixtures("keychain")
def test_a_probe_whose_cli_exits_non_zero_fails_with_its_exit_code_alone(tmp_path, person, clis, cli):
    store_token("work", made_up("work"))
    home = codex_home(tmp_path, "cx-a")
    seven = {"used_percent": 24.0, "window_minutes": 10080}
    clis.plan(
        {"work": {"rows": FIGURES, "leak": True, "rc": 3}, "cx-a": {"limits": {"primary": seven}, "rc": 3}}
    )
    account = (
        Account("work", "claude_code", "keychain") if cli == "claude" else Account("cx-a", "codex", str(home))
    )
    got = probe(account, Settings(), environ(person), claude=clis.claude, codex=clis.codex)
    assert got == Probed(got.at, "failed", {}, f"{cli} exited 3")  # a reading it printed is not taken


@pytest.mark.parametrize(("cli", "where"), [("claude", True), ("codex", "exec"), ("codex", "version")])
def test_a_probe_past_its_bound_fails_and_leaves_no_process_of_its_own(tmp_path, person, clis, cli, where):
    home = codex_home(tmp_path, "cx-a")
    clis.plan({"default": {"hang": where}, "cx-a": {"hang": where}})
    account = (
        Account("default", "claude_code", "default")
        if cli == "claude"
        else Account("cx-a", "codex", str(home))
    )
    t0 = time.monotonic()
    got = probe(account, Settings(), environ(person), claude=clis.claude, codex=clis.codex, timeout=1.5)
    assert time.monotonic() - t0 < 10
    assert got == Probed(got.at, "failed", {}, f"{cli} did not end within 1.5 seconds")
    pids = [int(p) for p in clis.pids.read_text().split()]
    assert len(pids) == 2 and all(gone(p) for p in pids)  # the CLI and the child it started


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_a_probe_that_ends_leaves_no_process_its_cli_started(tmp_path, person, clis, cli):
    home = codex_home(tmp_path, "cx-a")
    clis.plan({"default": {"linger": True}, "cx-a": {"linger": True}})
    account = (
        Account("default", "claude_code", "default")
        if cli == "claude"
        else Account("cx-a", "codex", str(home))
    )
    got = probe(account, Settings(), environ(person), claude=clis.claude, codex=clis.codex)
    assert got == Probed(got.at, "no figure", {}, None)  # it ended, and on its own
    pids = [int(p) for p in clis.pids.read_text().split()]
    assert len(pids) == 2 and all(gone(p) for p in pids)


def test_a_probe_past_its_bound_does_not_wait_on_a_pipe_held_by_a_child_outside_its_group(person, clis):
    clis.plan({"default": {"detach": True}})
    t0 = time.monotonic()
    got = probe(
        Account("default", "claude_code", "default"),
        Settings(),
        environ(person),
        claude=clis.claude,
        timeout=1.5,
    )
    took = time.monotonic() - t0
    cli, child = (int(p) for p in clis.pids.read_text().split())
    try:
        assert took < 10  # the child holds the probe's stdout for 30 seconds
        assert got == Probed(got.at, "failed", {}, "claude did not end within 1.5 seconds")
        assert gone(cli)
        os.kill(child, 0)  # alive: it left the group, so the group's kill did not reach it
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)


@pytest.mark.usefixtures("keychain")
def test_a_probes_child_holds_its_accounts_credential_and_none_competing(tmp_path, person, clis):
    store_token("work", made_up("work"))
    home = codex_home(tmp_path, "cx-a")
    clis.plan({})
    env = environ(person, CODEX_HOME=str(tmp_path / "elsewhere"))
    for account in (
        Account("work", "claude_code", "keychain"),
        Account("default", "claude_code", "default"),
        Account("cx-a", "codex", str(home)),
        Account("default", "codex", "default"),
    ):
        assert probe(account, Settings(), env, claude=clis.claude, codex=clis.codex).ended == "no figure"
    work, own, *codex = clis.calls()
    assert (work["who"], work["token"]) == ("work", True) and "CLAUDE_CODE_OAUTH_TOKEN" in work["names"]
    assert (own["who"], own["token"]) == ("default", False) and "CLAUDE_CODE_OAUTH_TOKEN" not in own["names"]
    for call in (work, own):
        assert not {"ANTHROPIC_API_KEY", "CC_ACCOUNT"} & set(call["names"])
    assert [c["home"] for c in codex] == [str(home)] * 3 + [str(tmp_path / "elsewhere")] * 3
    assert all("CODEX_ACCESS_TOKEN" not in c["names"] for c in codex)  # the catalogue's asks included


@pytest.mark.usefixtures("keychain")
def test_a_probe_that_cannot_start_or_read_its_token_fails_with_a_cause(tmp_path, person, clis, monkeypatch):
    home = codex_home(tmp_path, "cx-a")
    env, s = environ(person), Settings()
    nowhere = str(tmp_path / "no-such-cli")
    got = probe(Account("default", "claude_code", "default"), s, env, claude=nowhere)
    assert (got.ended, got.why) == ("failed", "claude could not start: No such file or directory")
    got = probe(Account("cx-a", "codex", str(home)), s, env, codex=nowhere)
    assert (got.ended, got.why) == ("failed", "codex could not start: No such file or directory")
    got = probe(Account("nobody", "claude_code", "keychain"), s, env, claude=clis.claude)
    assert got.why == (
        "its token could not be read: account 'nobody' has no keychain item; `lion account add` writes it"
    )
    clis.plan({"cx-a": {"release": "codex-cli 9.9.9-made-up"}})  # a release the backend refuses
    got = probe(Account("cx-a", "codex", str(home)), s, env, codex=clis.codex)
    assert (
        got.why == "codex gave no release or model catalogue the backend calls with"
        and "9.9.9" not in got.why
    )
    assert [c["argv"] for c in clis.calls()] == [["--version"]]  # no model call went out
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert probe(Account("default", "claude_code", "default"), s, env).why == "no claude on PATH"
    assert probe(Account("default", "codex", "default"), s, env).why == "no codex on PATH"
    assert clis.calls("claude") == []


def pool_of(tmp_path: Path, *names: str, text: str = "") -> Pool:
    """A pool of Claude Code accounts on the CLI's own sign-in, and whatever else `text` declares."""
    path = tmp_path / "accounts.toml"
    entries = "".join(
        f'[[account]]\nname = "{n}"\nprovider = "claude_code"\ncredential = "default"\n' for n in names
    )
    path.write_text(entries + text)
    return load_pool(path)


def fig(used: float | None, now: float) -> Window:
    return Window(None, None, now - 5, "call") if used is None else Window(used, now + 3600, now - 5, "call")


def seed(pool, now: float, **figures: tuple[float | None, float | None]) -> None:
    """Each named account's readings, fresh at `now`: (seven days, five hours), None for an absent window."""
    with held(pool.evidence) as book:
        for a in pool.accounts:
            if a.name in figures:
                seven, five = figures[a.name]
                book.of(a).readings = {"seven_day": fig(seven, now), "five_hour": fig(five, now)}
        book.save()


def never(account: Account, settings: Settings) -> Probed:
    raise AssertionError(f"{account.name} was probed")


def fake(results: dict[str, Probed]):
    """A probe that answers by the account's name and counts its calls."""
    calls: list[str] = []

    def probe_(account: Account, settings: Settings) -> Probed:
        calls.append(account.name)
        return results[account.name]

    return probe_, calls


def order(pool, now: float, probe_=never) -> list[str]:
    """The pick's whole order: each pick narrowed to the accounts not chosen yet."""
    left, out = [a.name for a in pool.accounts if a.provider == "claude_code"], []
    while left:
        out.append(pick(pool, "claude_code", left, None, now, probe_).name)
        left.remove(out[-1])
    return out


def test_the_pick_ranks_by_seven_days_then_five_hours_then_name_an_absent_window_before_a_figure(tmp_path):
    pool = pool_of(tmp_path, "a", "b", "c", "d", "e", "f", "g", "h", "i")
    now = time.time()
    seed(pool, now, a=(0.5, 0.1), b=(0.2, 0.9), c=(0.2, 0.3), d=(0.2, 0.3), e=(None, 0.8), f=(0.2, 0.0))
    # beside figures of 0 with names before theirs, absent windows still go first: absent is no 0
    seed(pool, now, g=(None, None), h=(0.0, 0.0), i=(0.2, None))
    assert order(pool, now) == ["g", "e", "h", "i", "f", "c", "d", "b", "a"]


def test_the_unknown_follow_every_ranked_one_least_recently_used_first_then_name(tmp_path):
    pool = pool_of(tmp_path, "a", "b", "c", "d", "e")
    now = time.time()
    seed(pool, now, e=(0.9, 0.9))  # ranked, though its figures are the highest
    with held(pool.evidence) as book:
        book.of(pool.accounts[0]).used = now - 500
        book.of(pool.accounts[2]).used = now - 100
        book.save()
    probe_, calls = fake({n: Probed(now, "no figure") for n in "abcd"})
    assert order(pool, now, probe_) == ["e", "b", "d", "a", "c"]  # never used first, by name, then a, c
    assert sorted(calls) == ["a", "b", "c", "d"]  # once each over five picks: the rest within max_age


def test_keep_stays_on_the_account_used_within_keep_while_its_readings_say_usable(tmp_path):
    pool = pool_of(tmp_path, "a", "b", "c", text="[pick]\nkeep = 1800\n")
    now = time.time()
    seed(pool, now, a=(0.1, 0.1), b=(0.8, 0.9))
    probe_, calls = fake(
        {"c": Probed(now, "figures", {"seven_day": fig(0.3, now), "five_hour": fig(0.3, now)})}
    )
    assert pick(pool, "claude_code", None, ("b", now - 1800), now, probe_).name == "b"
    assert calls == []  # kept on its readings: nobody probed, c unknown as it is
    assert pick(pool, "claude_code", None, ("b", now - 1801), now, probe_).name == "a"  # past keep
    assert calls == ["c"]
    seed(pool, now, b=(0.8, 0.95))  # no longer usable: it goes, and is out
    assert pick(pool, "claude_code", None, ("b", now - 10), now, probe_).name == "a"
    with pytest.raises(Hold, match="b: five_hour 0.95 is at or over 0.95"):
        pick(pool, "claude_code", ["b"], ("b", now - 10), now, never)


def test_keep_makes_no_probe_for_its_account_and_holds_on_its_plan_groups_reading(tmp_path):
    pool = pool_of(
        tmp_path,
        "a",
        text='[[account]]\nname = "b"\nprovider = "claude_code"\ncredential = "default"\ngroup = "plan"\n'
        '[[account]]\nname = "x"\nprovider = "claude_code"\ncredential = "default"\ngroup = "plan"\n',
    )
    now = time.time()
    seed(pool, now, a=(0.1, 0.1))  # b, used within keep, has no reading and is not probed: it goes
    took = {"seven_day": fig(0.8, now), "five_hour": fig(0.9, now)}
    probe_, calls = fake({"x": Probed(now, "figures", took)})
    assert pick(pool, "claude_code", ["a", "b"], ("b", now - 10), now, probe_).name == "a"
    assert calls == []
    # its plan group's member, probed in the pick, reads it usable: it stays
    assert pick(pool, "claude_code", None, ("b", now - 10), now, probe_).name == "b"
    assert calls == ["x"]
    seed(pool, now + 1, x=(0.99, 0.1))  # its plan group's later reading is its own: no longer usable
    assert pick(pool, "claude_code", None, ("b", now - 10), now + 1, never).name == "a"


def test_each_mark_puts_its_account_out_until_the_mark_ends(tmp_path):
    pool = pool_of(tmp_path, "a", "b", "c", "d")
    now = time.time()
    seed(pool, now, a=(0.1, 0.1), b=(0.2, 0.2), c=(0.3, 0.3), d=(0.4, 0.4))

    def mark(name: str, m: Mark) -> None:
        with held(pool.evidence) as book:
            book.of(next(a for a in pool.accounts if a.name == name)).mark = m
            book.save()

    mark("a", Mark("person", now))
    mark("b", Mark("quota", now))  # no end: until a probe succeeds
    mark("c", Mark("quota", now, now + 60))
    mark("d", Mark("quota", now - 120, now - 1))  # its end has passed: in
    assert pick(pool, "claude_code", None, ("a", now - 5), now, never).name == "d"  # a marked: not kept
    mark("d", Mark("quota", now - 120, now))  # ending at the pick's instant: passed
    assert pick(pool, "claude_code", None, None, now, never).name == "d"
    mark("d", Mark("person", now))
    with pytest.raises(Hold) as got:
        pick(pool, "claude_code", None, None, now, never)
    assert got.value.out == {
        "a": "marked: needs its person",
        "b": "marked: out of quota until a probe succeeds",
        "c": f"marked: out of quota until {_time(now + 60)}",
        "d": "marked: needs its person",
    }


def test_a_candidate_probed_within_max_age_is_not_probed_again(tmp_path, person, clis):
    pool = pool_of(tmp_path, "a")
    probe_ = functools.partial(probe, environ=environ(person), claude=clis.claude)
    clis.plan({"default": {}})  # each probe gives no figure, so a's usable stays unknown
    assert pick(pool, "claude_code", None, None, time.time(), probe_).name == "a"
    at = read_evidence(pool.evidence)[("claude_code", "a")].probe.at
    assert pick(pool, "claude_code", None, None, at + 600, probe_).name == "a"  # within: unknown, chosen
    assert len(clis.calls()) == 1
    assert pick(pool, "claude_code", None, None, at + 601, probe_).name == "a"  # past it: probed again
    assert len(clis.calls()) == 2
    clis.plan({"default": {"rc": 1}})
    with pytest.raises(Hold, match="a: its probe failed: claude exited 1$"):
        pick(pool, "claude_code", None, None, at + 1300, probe_)
    failed = read_evidence(pool.evidence)[("claude_code", "a")].probe
    assert failed.ended == "failed" and len(clis.calls()) == 3
    with pytest.raises(Hold) as got:  # within max_age of a failed probe, still unknown: out, not probed
        pick(pool, "claude_code", None, None, failed.at + 300, probe_)
    assert got.value.out == {
        "a": f"its last probe failed at {_time(failed.at)}; the next may run at {_time(failed.at + 600)}"
    }
    assert len(clis.calls()) == 3


def test_a_candidate_whose_probe_failed_is_out_and_its_probe_is_written(tmp_path):
    pool = pool_of(tmp_path, "a", "b")
    now = time.time()
    took = {"seven_day": fig(0.6, now), "five_hour": fig(0.6, now)}
    probe_, calls = fake(
        {"a": Probed(now - 1, "failed", why="claude exited 1"), "b": Probed(now, "figures", took)}
    )
    assert pick(pool, "claude_code", None, None, now, probe_).name == "b"
    kept = read_evidence(pool.evidence)
    assert kept[("claude_code", "a")] == Kept(probe=Probe(now - 1, "failed"))
    assert kept[("claude_code", "b")] == Kept(took, Probe(now, "figures"), None, now)
    assert sorted(calls) == ["a", "b"]


def test_the_probes_of_a_pick_run_at_once(tmp_path):
    pool = pool_of(tmp_path, "a", "b", "c")
    now = time.time()
    met = threading.Barrier(3, timeout=10)

    def probe_(account: Account, settings: Settings) -> Probed:
        met.wait()  # passes only once all three probes are running at the same time
        return Probed(now, "no figure")

    assert pick(pool, "claude_code", None, None, now, probe_).name == "a"


def test_a_probe_function_that_raises_raises_from_the_pick_after_every_probe_and_writes_nothing(tmp_path):
    pool = pool_of(tmp_path, "a", "b", "c")
    now = time.time()
    took = {"seven_day": fig(0.1, now), "five_hour": fig(0.1, now)}
    probe_, calls = fake({"a": Probed(now, "figures", took), "c": Probed(now, "figures", took)})
    with pytest.raises(KeyError, match="b"):  # b is not in what it answers
        pick(pool, "claude_code", None, None, now, probe_)
    assert sorted(calls) == ["a", "b", "c"]  # every probe ran to its end first
    with held(pool.evidence, 1) as book:  # the lock was let go, and nothing was written
        assert book.kept == {}


@pytest.fixture
def ctrl_c_raises():
    """The handler a person's process has. A job a non-interactive shell starts in the background inherits
    SIGINT ignored, and a held signal re-delivered to that handler is nothing (a whole-tree gate launched
    that way read these tests red); the hold is asserted under the terminal's handler."""
    before = signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    signal.signal(signal.SIGINT, before)


def interrupted(where: str, now: float, monkeypatch, writer) -> tuple:
    """A probe that takes a while, and one Ctrl-C (SIGINT to the main thread) where `where` says: as the first
    probe's thread starts, while the probes run, or as the first probe is written by `writer`'s `_took`; and
    the names of the probes that ended."""
    main, ended, fired = threading.main_thread().ident, [], []

    def once(f):
        def at(*args, **kwargs):
            got = f(*args, **kwargs)
            if not fired:
                fired.append(where)
                signal.pthread_kill(main, signal.SIGINT)
            return got

        return at

    if where == "start":
        monkeypatch.setattr(threading.Thread, "start", once(threading.Thread.start))
    elif where == "write":
        monkeypatch.setattr(writer, "_took", once(writer._took))

    def probe_(account: Account, settings: Settings) -> Probed:
        if where == "wait" and account.name == "a":
            time.sleep(0.2)
            fired.append(where)
            signal.pthread_kill(main, signal.SIGINT)
        time.sleep(0.5)
        ended.append(account.name)
        return Probed(now, "no figure")

    return probe_, ended, fired


@pytest.mark.parametrize("where", ["start", "wait", "write"])
def test_an_interrupted_pick_writes_the_probes_it_made_before_the_interruption_goes_on(
    tmp_path, monkeypatch, where, ctrl_c_raises
):
    pool = pool_of(tmp_path, "a", "b")
    now = time.time()
    probe_, ended, fired = interrupted(where, now, monkeypatch, accounts_module)
    with pytest.raises(KeyboardInterrupt):
        pick(pool, "claude_code", None, None, now, probe_)
    monkeypatch.undo()
    assert fired == [where] and sorted(ended) == ["a", "b"]  # it went on only once every probe had ended
    kept = read_evidence(pool.evidence)
    assert {n: k.probe for (_, n), k in kept.items()} == {
        "a": Probe(now, "no figure"),
        "b": Probe(now, "no figure"),
    }
    assert all(k.used is None for k in kept.values())  # nothing was chosen
    assert pick(pool, "claude_code", None, None, now + 1, never).name == "a"  # and neither is probed again


def test_a_pin_that_is_not_usable_holds_and_never_falls_back(tmp_path):
    pool = pool_of(tmp_path, "a", "b")
    now = time.time()
    seed(pool, now, a=(0.98, 0.96), b=(0.1, 0.1))
    with pytest.raises(Hold) as got:
        pick(pool, "claude_code", ["a"], ("a", now - 5), now, never)
    assert str(got.value) == (
        "no claude_code account can run the wake: a: seven_day 0.98 is at or over 0.97; five_hour 0.96 is at "
        "or over 0.95"
    )
    assert isinstance(got.value, AccountsError) and list(got.value.out) == ["a"]
    assert all(k.used is None for k in read_evidence(pool.evidence).values())  # nothing was chosen


def test_a_kill_of_the_probes_group_the_host_refuses_leaves_the_probe_a_failure_not_an_exception(
    tmp_path, monkeypatch
):
    # measured in a sandbox: once the CLI had exited and its descendant had left the group, killpg
    # raised PermissionError, which escaped the probe and the pick before anything was written; a
    # retry within max_age then probed again. A group the probe could not kill may have members alive
    # past it, which D4 promises never happens, so the probe is a failure whatever the CLI printed
    monkeypatch.setattr(accounts_module.os, "killpg", _raise(PermissionError(1, "Operation not permitted")))
    # the leaders the refused kill leaves running are the host's, not the probe's; here they are
    # reaped by the test itself at its end, so no handle of a running process outlives it
    made: list[subprocess.Popen] = []
    real = subprocess.Popen

    class Recorded(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(accounts_module.subprocess, "Popen", Recorded)
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\nsleep 5\n")
    cli.chmod(0o755)
    t0 = time.monotonic()
    out, why = accounts_module._call("claude", [str(cli)], {}, time.monotonic() + 0.2, 0.2)
    assert out == b"" and why == "claude's process group could not be killed: Operation not permitted"
    assert time.monotonic() - t0 < 1.0  # the leader the refused kill left running is not waited on
    quick = tmp_path / "claude-ok"
    quick.write_text("#!/bin/sh\necho ok\n")
    quick.chmod(0o755)
    out, why = accounts_module._call("claude", [str(quick)], {}, time.monotonic() + 2, 2)
    assert out == b"" and why == "claude's process group could not be killed: Operation not permitted"
    pool = pool_of(tmp_path, "a")
    took = probe(pool.accounts[0], pool.settings, environ={}, claude=str(cli), timeout=0.2)
    assert took.ended == "failed" and "could not be killed" in took.why
    assert len(made) == 3 and sum(p.returncode is None for p in made) == 2  # two sleepers, one quick
    for child in made:
        with contextlib.suppress(ProcessLookupError):
            child.kill()
        child.wait(timeout=10)
    assert all(p.returncode is not None for p in made)


def test_a_pipe_the_host_refuses_to_read_and_a_scratch_it_refuses_to_write_leave_the_probe_a_failure(
    tmp_path, monkeypatch, person, clis
):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho ok\n")
    cli.chmod(0o755)
    monkeypatch.setattr(subprocess.Popen, "communicate", _raise(OSError(5, "Input/output error")))
    out, why = accounts_module._call("claude", [str(cli)], {}, time.monotonic() + 2, 2)
    assert out == b"" and why == "claude could not be read: Input/output error"
    pool = pool_of(tmp_path, "a")
    took = probe(pool.accounts[0], pool.settings, environ={}, claude=str(cli), timeout=2)
    assert took.ended == "failed" and took.why == "claude could not be read: Input/output error"
    monkeypatch.undo()
    # a close the host refuses after the reading is complete leaks a handle, not the reading; a reap it
    # refuses (the child reaped by another hand) leaves how the CLI ended unknown, so the probe failed
    real_communicate = subprocess.Popen.communicate

    class RefusingClose:
        @staticmethod
        def close():
            raise OSError(5, "Input/output error")

    def closing_refused(self, *args, **kwargs):
        got = real_communicate(self, *args, **kwargs)
        self.stdout = RefusingClose()
        return got

    monkeypatch.setattr(subprocess.Popen, "communicate", closing_refused)
    out, why = accounts_module._call("claude", [str(cli)], {}, time.monotonic() + 2, 2)
    assert (out, why) == (b"ok\n", None)
    monkeypatch.undo()

    def reap_refused(self, *args, **kwargs):
        got = real_communicate(self, *args, **kwargs)
        self.wait = _raise(ChildProcessError(10, "No child processes"))
        return got

    monkeypatch.setattr(subprocess.Popen, "communicate", reap_refused)
    out, why = accounts_module._call("claude", [str(cli)], {}, time.monotonic() + 2, 2)
    assert out == b"" and why == "claude's exit could not be read: No child processes"
    took = probe(pool.accounts[0], pool.settings, environ={}, claude=str(cli), timeout=2)
    assert took.ended == "failed" and took.why == "claude's exit could not be read: No child processes"
    monkeypatch.undo()
    monkeypatch.setattr(accounts_module.tempfile, "mkdtemp", _raise(OSError(28, "No space left on device")))
    home = codex_home(tmp_path, "cx-a")
    clis.plan({"cx-a": {}})
    settings = Settings(probe_model={"claude_code": "haiku", "codex": "small"})
    got = probe(Account("cx-a", "codex", str(home)), settings, environ(person), codex=clis.codex)
    assert (got.ended, got.why) == ("failed", "codex's scratch could not be made: No space left on device")


def _raise(exc):
    def at(*args, **kwargs):
        raise exc

    return at


def test_a_pin_whose_probe_gave_no_figure_runs_and_one_whose_probe_failed_holds(tmp_path):
    pool = pool_of(tmp_path, "a", "b")
    now = time.time()
    seed(pool, now, b=(0.1, 0.1))
    probe_, calls = fake({"a": Probed(now, "no figure")})
    assert pick(pool, "claude_code", ["a"], None, now, probe_).name == "a"  # unknown is not out (C4)
    (tmp_path / "other").mkdir()
    other = pool_of(tmp_path / "other", "a", "b")
    seed(other, now, b=(0.1, 0.1))
    probe_, calls = fake({"a": Probed(now, "failed", why="claude exited 1")})
    with pytest.raises(Hold) as got:
        pick(other, "claude_code", ["a"], None, now, probe_)
    assert got.value.out == {"a": "its probe failed: claude exited 1"} and calls == ["a"]


def test_a_pick_with_no_usable_candidate_holds_naming_each_and_why(tmp_path):
    pool = pool_of(tmp_path, "a", "b", "c", "d", "e")
    now = time.time()
    seed(pool, now, c=(0.5, 0.95))
    with held(pool.evidence) as book:
        book.of(pool.accounts[0]).mark = Mark("person", now)
        book.of(pool.accounts[3]).probe = Probe(now - 10, "failed")
        book.of(pool.accounts[4]).readings = {"seven_day": fig(0.97, now - 700)}  # stale: no reading
        book.save()
    probe_, calls = fake({n: Probed(now, "failed", why="claude exited 2") for n in "be"})
    with pytest.raises(Hold) as got:
        pick(pool, "claude_code", None, None, now, probe_)
    assert got.value.out == {
        "a": "marked: needs its person",
        "b": "its probe failed: claude exited 2",
        "c": "five_hour 0.95 is at or over 0.95",
        "d": f"its last probe failed at {_time(now - 10)}; the next may run at {_time(now + 590)}",
        "e": "its probe failed: claude exited 2",
    }
    assert str(got.value) == "no claude_code account can run the wake: " + "; ".join(
        f"{n}: {why}" for n, why in got.value.out.items()
    )
    assert sorted(calls) == ["b", "e"]
    codex_only = pool_of(
        tmp_path, text='[[account]]\nname = "x"\nprovider = "codex"\ncredential = "default"\n'
    )
    for narrowed in (None, []):
        with pytest.raises(Hold, match="no claude_code account to pick from in ") as got:
            pick(codex_only, "claude_code", narrowed, None, now, never)
        assert got.value.out == {}
    with pytest.raises(Hold, match="no codex account to pick from"):
        pick(pool, "codex", None, None, now, never)


def test_a_name_in_accounts_that_is_not_the_pools_on_the_provider_is_refused(tmp_path):
    pool = pool_of(
        tmp_path, "a", text='[[account]]\nname = "cx"\nprovider = "codex"\ncredential = "default"\n'
    )
    now = time.time()
    cases = [
        (["a", "zz"], f"the agent's accounts name 'zz', which is not in {pool.path}, not one on claude_code"),
        (["cx"], "the agent's accounts name 'cx', which is a codex account, not one on claude_code"),
        ([SHAPED], f"the agent's accounts name outside the rule, which is not in {pool.path}, not one on"),
    ]
    for names, said in cases:
        with pytest.raises(AccountsError) as got:
            pick(pool, "claude_code", names, None, now, never)
        assert not isinstance(got.value, Hold) and str(got.value).startswith(said)
        assert "Zq7xW" not in str(got.value)
    assert not pool.evidence.exists()  # refused before the evidence file was read


def test_the_pick_writes_the_chosen_accounts_last_use(tmp_path):
    pool = pool_of(tmp_path, "a", "b")
    now = time.time()
    seed(pool, now, a=(0.1, 0.1), b=(0.2, 0.2))
    assert pick(pool, "claude_code", None, None, now, never).name == "a"
    kept = read_evidence(pool.evidence)
    assert kept[("claude_code", "a")].used == now and kept[("claude_code", "b")].used is None
    assert json.loads(pool.evidence.read_text())["claude_code"]["a"]["used"] == now
    assert pick(pool, "claude_code", None, ("a", now), now + 1, never).name == "a"
    assert read_evidence(pool.evidence)[("claude_code", "a")].used == now + 1


PICKER = """
import functools, sys, time
from hub.accounts import load_pool, pick, probe

pool = load_pool(sys.argv[1])
print(pick(pool, "claude_code", None, None, time.time(), functools.partial(probe, claude=sys.argv[2])).name)
"""


def test_two_picks_in_two_processes_at_once_probe_once_in_total(tmp_path, clis):
    pool = pool_of(tmp_path, "a")
    clis.plan({"default": {"rows": FIGURES, "sleep": 1.5}})  # the first pick probes, holding the lock
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "src")])}

    def picker() -> subprocess.Popen:
        argv = [sys.executable, "-c", PICKER, str(pool.path), clis.claude]
        return subprocess.Popen(
            argv, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

    first = picker()
    deadline = time.monotonic() + 60
    while not clis.calls():  # the first is probing, under the lock
        assert first.poll() is None and time.monotonic() < deadline
        time.sleep(0.01)
    second = picker()
    outs = [p.communicate(timeout=120) for p in (first, second)]
    assert [p.returncode for p in (first, second)] == [0, 0], outs
    assert [o for o, _ in outs] == ["a\n", "a\n"]
    assert len(clis.calls()) == 1  # the second read what the first learned
    assert read_evidence(pool.evidence)[("claude_code", "a")].probe.ended == "figures"


def test_a_pick_that_waits_past_its_bound_for_the_lock_holds_naming_the_lock(tmp_path):
    pool = pool_of(tmp_path, "a")
    with held(pool.evidence):
        with pytest.raises(Hold) as got:
            pick(pool, "claude_code", None, None, time.time(), never, wait=0.2)
    assert str(got.value) == f"{pool.evidence} is held by another process past 0.2 seconds"
    assert got.value.out == {}


PROBE_POOL = """[[account]]
name = "work"
provider = "claude_code"
credential = "keychain"
[[account]]
name = "own"
provider = "claude_code"
credential = "default"
[[account]]
name = "cx"
provider = "codex"
credential = "HOME/cx-c"
"""
SEVEN = {"used_percent": 25.0, "window_minutes": 10080}


def probe_pool(tmp_path: Path, clis) -> Path:
    store_token("work", made_up("work"))
    codex_home(tmp_path, "cx-c")
    path = tmp_path / "accounts.toml"
    path.write_text(PROBE_POOL.replace("HOME", str(tmp_path)))
    plan = {
        "work": {"rows": FIGURES},
        "default": {},
        "cx-c": {"limits": {"primary": SEVEN, "secondary": None}},
    }
    clis.plan(plan)
    return path


AGE = r"\d+s old"


@pytest.mark.usefixtures("keychain")
def test_lion_account_probe_probes_every_account_and_one_by_name(tmp_path, clis, capsys):
    path = probe_pool(tmp_path, clis)
    code, out, err = run(["account", "probe", "--file", str(path)], capsys)
    assert (code, err) == (0, ""), err
    lines = out.splitlines()
    assert len(lines) == 3
    assert re.fullmatch(
        f"work: claude_code, keychain; probed: figures; five_hour 0.46 \\({AGE}, from a probe\\); "
        f"seven_day 0.64 \\({AGE}, from a probe\\); no mark",
        lines[0],
    )
    assert (
        lines[1]
        == "own: claude_code, default; probed: no figure; five_hour unknown; seven_day unknown; no mark"
    )
    assert re.fullmatch(
        f"cx: codex, home; probed: figures; five_hour absent \\({AGE}, from a probe\\); "
        f"seven_day 0.25 \\({AGE}, from a probe\\); no mark",
        lines[2],
    )
    kept = read_evidence(tmp_path / "evidence.json")
    assert [
        kept[k].probe.ended for k in (("claude_code", "work"), ("claude_code", "own"), ("codex", "cx"))
    ] == [
        "figures",
        "no figure",
        "figures",
    ]
    assert len(clis.calls("claude")) == 2 and len(clis.calls("codex")) == 3
    with held(tmp_path / "evidence.json") as book:  # an hour on: each may be probed again
        for k in book.kept.values():
            k.probe = Probe(k.probe.at - 3600, k.probe.ended)
        book.save()
    code, out, _ = run(["account", "probe", "work", "--json", "--file", str(path)], capsys)
    (row,) = json.loads(out)
    assert code == 0 and row["name"] == "work" and row["windows"]["seven_day"]["used"] == 0.64
    assert row["probe"] == {"probed": True, "ended": "figures", "why": None, "mark_ended": None}
    assert len(clis.calls("claude")) == 3 and len(clis.calls("codex")) == 3  # that one alone


@pytest.mark.usefixtures("keychain")
def test_lion_account_probe_within_max_age_says_when_the_next_may_run(tmp_path, clis, capsys):
    path = probe_pool(tmp_path, clis)
    assert run(["account", "probe", "--file", str(path)], capsys)[0] == 0
    calls = len(clis.calls())
    kept = read_evidence(tmp_path / "evidence.json")
    code, out, _ = run(["account", "probe", "own", "--file", str(path)], capsys)
    at = kept[("claude_code", "own")].probe.at
    assert code == 0 and out == (
        f"own: claude_code, default; not probed: its last probe was at {_time(at)}, the next may run at "
        f"{_time(at + 600)}; five_hour unknown; seven_day unknown; no mark\n"
    )
    code, out, _ = run(["account", "probe", "--json", "--file", str(path)], capsys)
    assert [r["probe"] for r in json.loads(out)] == [
        {"probed": False, "at": kept[k].probe.at, "next": kept[k].probe.at + 600}
        for k in (("claude_code", "work"), ("claude_code", "own"), ("codex", "cx"))
    ]
    assert len(clis.calls()) == calls


@pytest.mark.parametrize("where", ["start", "wait", "write"])
def test_an_interrupted_lion_account_probe_writes_the_probes_it_made(
    tmp_path, monkeypatch, where, ctrl_c_raises
):
    path = pool_of(tmp_path, "a", "b").path
    now = time.time()
    probe_, ended, fired = interrupted(where, now, monkeypatch, account_cli)
    monkeypatch.setattr(account_cli, "probe_account", probe_)
    with pytest.raises(KeyboardInterrupt):
        account_cli.probe(path)
    monkeypatch.undo()
    assert fired == [where] and sorted(ended) == ["a", "b"]
    kept = read_evidence(tmp_path / "evidence.json")
    assert {n: k.probe for (_, n), k in kept.items()} == {
        "a": Probe(now, "no figure"),
        "b": Probe(now, "no figure"),
    }


@pytest.mark.usefixtures("keychain")
def test_a_probe_that_did_not_fail_ends_a_person_mark_and_a_quota_mark_with_no_end(tmp_path, clis, capsys):
    path = probe_pool(tmp_path, clis)
    now = time.time()
    pool = load_pool(path)
    work, own, cx = pool.accounts
    with held(pool.evidence) as book:
        book.of(work).mark = Mark("quota", now, now + 3600)  # its end still ahead: it stays
        book.of(own).mark = Mark("person", now)
        book.of(cx).mark = Mark("quota", now)
        book.save()
    code, out, _ = run(["account", "probe", "--file", str(path)], capsys)
    lines = out.splitlines()
    assert code == 0
    assert lines[0].startswith("work: claude_code, keychain; probed: figures; ")
    assert lines[0].endswith(f"; marked: out of quota until {_time(now + 3600)}")
    assert lines[1] == (
        "own: claude_code, default; probed: no figure, and its mark ended (it needed its person); "
        "five_hour unknown; seven_day unknown; no mark"
    )
    assert lines[2].startswith(
        "cx: codex, home; probed: figures, and its mark ended (it was out of quota until a probe succeeded); "
    )
    kept = read_evidence(pool.evidence)
    assert [kept[(a.provider, a.name)].mark for a in pool.accounts] == [
        Mark("quota", now, now + 3600),
        None,
        None,
    ]
    with held(pool.evidence) as book:  # a probe that fails ends nothing, and the command exits 1
        book.of(own).mark = Mark("person", now)
        book.of(own).probe = Probe(now - 3600, "no figure")
        book.save()
    clis.plan({"default": {"rc": 1}})
    code, out, err = run(["account", "probe", "own", "--file", str(path)], capsys)
    assert (code, err) == (1, "")
    assert out == (
        "own: claude_code, default; probed: failed, claude exited 1; five_hour unknown; seven_day unknown; "
        "marked: needs its person\n"
    )
    assert read_evidence(pool.evidence)[("claude_code", "own")].mark == Mark("person", now)


def test_lion_account_probe_refuses_a_name_not_in_the_pool_and_says_an_empty_pool(tmp_path, capsys):
    path = tmp_path / "accounts.toml"
    path.write_text("")
    assert run(["account", "probe", "--file", str(path)], capsys) == (
        0,
        f"no accounts: {path} declares none\n",
        "",
    )
    code, out, err = run(["account", "probe", "zz", "--file", str(path)], capsys)
    assert (code, out, err) == (1, "", f"lion account probe: no account 'zz' in {path}\n")
    code, _, err = run(["account", "probe", SHAPED, "--file", str(path)], capsys)
    assert code == 1 and "an account's name is letters" in err and "nothing was probed" in err
    assert "Zq7xW" not in err


@pytest.mark.usefixtures("keychain")
def test_a_made_up_token_is_in_nothing_a_probe_or_a_pick_wrote(tmp_path, person, clis, capfd):
    store_token("canary", CANARY)
    canary = Account("canary", "claude_code", "keychain")
    env = environ(person)
    said: list[str] = []
    for step in (
        {"leak": True, "rows": FIGURES},
        {"leak": True},
        {"leak": True, "rc": 2},
        {"leak": True, "hang": True},
    ):
        clis.plan({"canary": step})
        said.append(repr(probe(canary, Settings(), env, claude=clis.claude, timeout=1.5)))
    pools = []
    for n, step in enumerate(({"leak": True, "rows": FIGURES}, {"leak": True, "rc": 1})):
        where = tmp_path / f"pool-{n}"
        where.mkdir()
        (where / "accounts.toml").write_text(
            '[[account]]\nname = "canary"\nprovider = "claude_code"\ncredential = "keychain"\n'
        )
        pools.append(load_pool(where / "accounts.toml"))
        clis.plan({"canary": step})
        probe_ = functools.partial(probe, environ=env, claude=clis.claude)
        try:
            said.append(repr(pick(pools[-1], "claude_code", None, None, time.time(), probe_)))
        except Hold as e:
            said += [str(e), repr(e.out)]
    assert said[-2].startswith("no claude_code account can run the wake: canary: its probe failed")
    for step in ({"leak": True, "rows": FIGURES}, {"leak": True, "rc": 1}):
        clis.plan({"canary": step})
        with held(pools[0].evidence) as book:  # its last probe an hour back, so it is probed again
            book.of(canary).probe = Probe(time.time() - 3600, "figures")
            book.save()
        for form in ([], ["--json"]):
            code, out, err = run(["account", "probe", *form, "--file", str(pools[0].path)], capfd)
            said += [out, err]
            with held(pools[0].evidence) as book:
                book.of(canary).probe = Probe(time.time() - 3600, "figures")
                book.save()
    said += list(capfd.readouterr())
    calls = clis.calls("claude")
    assert len(calls) == 10 and all(c["who"] == "canary" and c["token"] for c in calls)  # it had the token
    written = [*said, *(p.evidence.read_text() for p in pools)]
    assert [w for text in written for w in parts(text, length=False)] == []
    assert any("claude exited 1" in s for s in said) and any("probed: figures" in s for s in said)
