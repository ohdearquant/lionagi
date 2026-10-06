"""The accounts of the two CLI providers (ADR-0024): the pool file and every refusal of it, a token's
keychain item, the child's environment, the evidence file under its lock, the two limit-row parsers and
`lion account`. No test reaches the keychain: `security` is a script that keeps its items in a file and
logs its arguments, and every token here is made up."""

import dataclasses
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import lion_cli.account as account_cli
import pytest
from lion_cli.account import rows
from lion_cli.main import main

import hub.accounts as accounts
from hub.accounts import (
    CREDENTIAL_VARIABLES,
    DEFAULT_POOL,
    UNKNOWN,
    Account,
    AccountsError,
    Kept,
    Mark,
    Probe,
    Settings,
    Window,
    child_env,
    claude_code_reading,
    codex_reading,
    fresh,
    held,
    load_pool,
    parse_pool,
    read_evidence,
    readings,
    record,
    remove_token,
    store_token,
    token,
    usable,
)
from hub.areas import DEFAULT_REGISTRY

REPO = Path(__file__).resolve().parents[1]
CANARY = "made-up-canary-0001-Zq7xW"  # the one token every output is searched for
# a made-up value shaped as a Claude Code credential, for the places a label belongs: a label is shown as
# written, so one of this shape is refused before it is kept (ADR-0024/C1)
SHAPED = "sk-ant-oat01-made-up-" + "Zq7xW" * 18
NOW = 1_791_140_000.0  # an instant inside the windows of the rows below
SECURITY = """#!PYTHON
import json, shlex, sys
from pathlib import Path

import os, subprocess, time

store, argv, fail = Path("STORE"), Path("ARGV"), Path("FAIL")
with argv.open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("SECURITY_PARENT_LOG"):  # the arguments of the process that started this one
    ps = ["/bin/ps", "-o", "args=", "-p", str(os.getppid())]
    above = subprocess.run(ps, capture_output=True, text=True)
    with open(os.environ["SECURITY_PARENT_LOG"], "a") as f:
        f.write(above.stdout)
items = json.loads(store.read_text()) if store.exists() else {}
mode = fail.read_text().strip() if fail.exists() else ""


def opt(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def run(args):
    key = f"{opt(args, '-a')}/{opt(args, '-s')}"
    if mode == "refuse":  # the tool says what it was given, as a real tool's error may
        print("refused: " + " ".join(args), file=sys.stderr)
        return 1
    if mode == "partial":  # what it says carries a part of the value and its length, never the whole
        value = opt(args, "-w") or items.get(key, "")
        print(f"prefix={value[:8]} length={len(value)}", file=sys.stderr)
        return 1
    if args[0] == "add-generic-password":
        if mode != "drop":
            items[key] = opt(args, "-w")
        if opt(args, "-a") == "slow":  # held between its start and its write
            Path(str(store) + ".writing").touch()
            time.sleep(1.5)
        return 1 if mode in ("written then failed", "sticky") else 0
    if key not in items:
        print("The specified item could not be found in the keychain.", file=sys.stderr)
        return 44
    if args[0] == "delete-generic-password":
        if mode == "sticky":
            print("refused to delete " + items[key], file=sys.stderr)
            return 1
        del items[key]
    elif mode == "leak":
        print(items[key])
        print("failed reading " + items[key], file=sys.stderr)
        return 1
    else:
        print(items[key])
    return 0


if sys.argv[1:] == ["-i"]:
    rc = 0
    for line in sys.stdin.read().splitlines():
        rc = run(shlex.split(line)) or rc
else:
    rc = run(sys.argv[1:])
store.write_text(json.dumps(items))
sys.exit(rc)
"""
CLAUDE_ROW = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "resetsAt": 1791655200,
        "rateLimitType": "seven_day",
        "utilization": 0.64,
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.46, "resetsAt": 1791143400},
            "seven_day": {"utilization": 0.64, "resetsAt": 1791655200},
        },
    },
}
CODEX_ROW = {
    "timestamp": "2026-10-04T16:00:00.000Z",
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {"model_context_window": 258400},
        "rate_limits": {
            "primary": {"used_percent": 46.0, "window_minutes": 300, "resets_at": 1791143400},
            "secondary": {"used_percent": 25.0, "window_minutes": 10080, "resets_at": 1791655200},
            "plan_type": "pro",
        },
    },
}
CODEX_AT = 1791129600.0  # CODEX_ROW's timestamp


@pytest.fixture
def keychain(tmp_path, monkeypatch):
    """`security` as a script over a file: (items, argument log, failure mode file)."""
    store, argv, fail = tmp_path / "keychain.json", tmp_path / "security.argv", tmp_path / "security.fail"
    script = tmp_path / "security"
    script.write_text(
        SECURITY.replace("PYTHON", sys.executable)
        .replace("STORE", str(store))
        .replace("ARGV", str(argv))
        .replace("FAIL", str(fail))
    )
    script.chmod(0o755)
    monkeypatch.setattr(accounts, "_SECURITY", str(script))
    return store, argv, fail


def items(store: Path) -> dict:
    return json.loads(store.read_text()) if store.exists() else {}


def logged(argv: Path) -> list[list[str]]:
    return [json.loads(line) for line in argv.read_text().splitlines()] if argv.exists() else []


def reading(used: float, observed: float, resets: float | None = None, source: str = "call") -> Window:
    return Window(used, resets, observed, source)


def test_the_pool_sits_beside_the_areas_registry_and_the_evidence_file_beside_the_pool(tmp_path):
    assert DEFAULT_POOL == DEFAULT_REGISTRY.parent / "accounts.toml"
    assert load_pool(tmp_path / "accounts.toml").evidence == tmp_path / "evidence.json"


def test_no_file_is_one_account_per_provider_named_default_on_the_default_credential(tmp_path):
    pool = load_pool(tmp_path / "accounts.toml")
    assert pool.accounts == (
        Account("default", "claude_code", "default"),
        Account("default", "codex", "default"),
    )
    assert pool.settings == Settings()
    assert (Settings().max_age, Settings().keep, Settings().seven_day, Settings().five_hour) == (
        600,
        3600,
        0.97,
        0.95,
    )
    assert Settings().probe_model == {"claude_code": "haiku", "codex": None}
    assert not (tmp_path / "accounts.toml").exists()


def test_a_pool_file_declares_its_accounts_their_plan_groups_and_the_pick_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "accounts.toml"
    path.write_text(
        '[[account]]\nname = "work.1"\nprovider = "claude_code"\ncredential = "keychain"\ngroup = "team_a"\n'
        '[[account]]\nname = "cx-home"\nprovider = "codex"\ncredential = "~/codex-a"\n'
        '[[account]]\nname = "cx_default"\nprovider = "codex"\ncredential = "default"\n'
        "[pick]\nmax_age = 900\nkeep = 1800.5\nseven_day = 1\nfive_hour = 0.5\n"
        'probe_model = { codex = "gpt-small" }\n'
    )
    pool = load_pool(path)
    assert pool.accounts == (
        Account("work.1", "claude_code", "keychain", "team_a"),
        Account("cx-home", "codex", str(tmp_path / "codex-a")),
        Account("cx_default", "codex", "default"),
    )
    assert [a.kept for a in pool.accounts] == ["keychain", "home", "default"]
    assert pool.settings == Settings(900.0, 1800.5, 1.0, 0.5, {"claude_code": "haiku", "codex": "gpt-small"})
    assert parse_pool("", path).accounts == ()  # an empty file declares no account


ACCOUNT = '[[account]]\nname = "a"\nprovider = "claude_code"\ncredential = "default"\n'


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("name = ", "Invalid value"),
        ("accounts = []\n", "no key 'accounts'"),
        ('account = "a"\n', "account is an array of tables"),
        ('account = ["a"]\n', "account 1 is not a table"),
        (ACCOUNT + 'plan = "x"\n', "account 1: no key 'plan'"),
        ('[[account]]\nprovider = "codex"\ncredential = "default"\n', "account 1: its name is missing"),
        (ACCOUNT.replace('"a"', '"a b"'), "account 1: its name is missing or not letters"),
        (ACCOUNT.replace('"a"', '"a/b"'), "account 1: its name is missing or not letters"),
        (ACCOUNT + ACCOUNT, "account 'a' is named twice"),
        (
            ACCOUNT.replace('"claude_code"', '"openai"'),
            "account 'a': its provider is not claude_code or codex",
        ),
        ('[[account]]\nname = "a"\nprovider = "codex"\n', "account 'a': credential is missing"),
        (ACCOUNT.replace('"default"', "1"), "account 'a': credential is missing or not text"),
        (ACCOUNT.replace('"default"', '"/srv/codex"'), "account 'a': a Claude Code credential is"),
        (
            ACCOUNT.replace('"claude_code"', '"codex"').replace('"default"', '"keychain"'),
            "account 'a': a Codex credential is default or a home",
        ),
        (
            ACCOUNT.replace('"claude_code"', '"codex"').replace('"default"', '"codex-home"'),
            "account 'a': a Codex credential is default or a home",
        ),
        (ACCOUNT + 'group = "a team"\n', "account 'a': its group is not letters"),
        (ACCOUNT + "group = 3\n", "account 'a': its group is not letters"),
        (ACCOUNT + f'group = "{"g" * 65}"\n', "account 'a': its group is not letters"),
        (ACCOUNT.replace('"a"', f'"{"n" * 65}"'), "account 1: its name is missing or not letters"),
        (ACCOUNT.replace('"a"', '"SK-ANT-x"'), "account 1: its name is missing or not letters"),
        (
            'account = [{ name = "a", provider = "codex", credential = "default" }]\n',
            "each account is its own [[account]] table",
        ),
        ('[["account"]]\nname = "a"\nprovider = "codex"\ncredential = "default"\n', "its own [[account]]"),
        ("pick = 1\n", "pick is a table"),
        ("[pick]\nwait = 1\n", "[pick] has no key 'wait'"),
        ("[pick]\nmax_age = 0\n", "[pick] max_age is not a number of seconds above zero"),
        ("[pick]\nkeep = -5\n", "[pick] keep is not a number"),
        ("[pick]\nkeep = true\n", "[pick] keep is not a number"),
        ("[pick]\nmax_age = nan\n", "[pick] max_age is not a number"),
        ("[pick]\nseven_day = 0\n", "[pick] seven_day is not a fraction above 0 and at most 1"),
        ("[pick]\nfive_hour = 1.01\n", "[pick] five_hour is not a fraction"),
        ('[pick]\nfive_hour = "0.9"\n', "[pick] five_hour is not a fraction"),
        ('[pick]\nprobe_model = "haiku"\n', "[pick] probe_model is a table"),
        ('[pick]\nprobe_model = { openai = "x" }\n', "[pick] probe_model is a table"),
        ('[pick]\nprobe_model = { claude_code = " " }\n', "[pick] probe_model is a table"),
        ('[pick]\nprobe_model = { claude_code = "haiku 4" }\n', "a model name is letters"),
        (f'[pick]\nprobe_model = {{ codex = "{"m" * 65}" }}\n', "a model name is letters"),
        ('[pick]\nprobe_model = { codex = "sk-ant-api03-x" }\n', "not beginning 'sk-ant-'"),
    ],
)
def test_every_refusal_of_the_pool_file_names_the_entry_and_the_rule(tmp_path, text, said):
    path = tmp_path / "accounts.toml"
    path.write_text(text)
    with pytest.raises(AccountsError) as got:
        load_pool(path)
    assert said in str(got.value) and str(path) in str(got.value)


@pytest.mark.parametrize(
    "text",
    [
        ACCOUNT.replace('"a"', f'"{SHAPED}"'),
        ACCOUNT + f'group = "{SHAPED}"\n',
        ACCOUNT.replace('"claude_code"', f'"{SHAPED}"'),
        ACCOUNT.replace('"default"', f'"{SHAPED}"'),
        ACCOUNT.replace('"claude_code"', '"codex"').replace('"default"', f'"{SHAPED}"'),
        ACCOUNT + f'"{SHAPED}" = 1\n',
        f'"{SHAPED}" = 1\n',
        f'[pick]\n"{SHAPED}" = 1\n',
        *(f'[pick]\n{key} = "{SHAPED}"\n' for key in ("max_age", "keep", "seven_day", "five_hour")),
        f'[pick]\nprobe_model = {{ codex = "{SHAPED}" }}\n',
        f'[pick]\nprobe_model = {{ claude_code = "{SHAPED[:40]}" }}\n',  # short, and still its prefix
    ],
)
def test_no_refusal_of_the_pool_file_repeats_a_value_shaped_as_a_credential(tmp_path, text):
    # a label is shown as written, so a label of a credential's shape is refused; no refusal shows a value
    path = tmp_path / "accounts.toml"
    path.write_text(text)
    with pytest.raises(AccountsError) as got:
        load_pool(path)
    said = str(got.value)
    assert str(path) in said
    assert [w for w in (SHAPED[i : i + 8] for i in range(12, len(SHAPED) - 7)) if w in said] == []
    assert "sk-ant-o" not in said


def test_a_pool_file_that_is_not_utf8_is_refused(tmp_path):
    path = tmp_path / "accounts.toml"
    path.write_bytes(b'[[account]]\nname = "\xff"\n')
    with pytest.raises(AccountsError, match="is not UTF-8"):
        load_pool(path)


def test_a_token_goes_to_the_keychain_tool_on_standard_input_and_reads_back(keychain):
    store, argv, _ = keychain
    store_token("work", CANARY)
    assert items(store) == {"work/lion.account.claude_code": CANARY}
    assert token("work") == CANARY
    store_token("work", "made-up-token-0002")  # -U: the item is replaced
    assert token("work") == "made-up-token-0002"
    calls = logged(argv)
    assert calls[0] == ["-i"]  # the value was on standard input only
    assert calls[1] == ["find-generic-password", "-a", "work", "-s", "lion.account.claude_code", "-w"]
    assert all("made-up" not in arg for call in calls for arg in call)
    assert remove_token("work") is True
    assert remove_token("work") is False  # nothing left to delete
    assert items(store) == {}


@pytest.mark.parametrize(
    ("value", "said"),
    [
        ("", "no token was given"),
        (CANARY * 90, "the token is longer than 2048 characters"),
        (CANARY + "!", "a token is letters, digits, '-', '_' and '.' only"),
        (CANARY + " -w other", "a token is letters, digits"),
        (CANARY + "\nadd-generic-password", "a token is letters, digits"),
    ],
)
def test_a_token_outside_its_form_is_refused_before_the_tool_runs_and_never_repeated(keychain, value, said):
    store, argv, _ = keychain
    with pytest.raises(AccountsError) as got:
        store_token("work", value)
    text = str(got.value)
    assert said in text and "nothing was written" in text
    assert "made-up" not in text and "Zq7x" not in text and str(len(value)) not in text
    assert logged(argv) == [] and items(store) == {}


def test_a_name_that_could_split_the_tools_line_is_refused(keychain):
    _, argv, _ = keychain
    for act in (
        lambda: store_token("a -U", CANARY),
        lambda: token("a\nb"),
        lambda: remove_token(""),
        lambda: store_token(SHAPED, CANARY),
    ):
        with pytest.raises(AccountsError, match="an account's name is letters") as got:
            act()
        assert "Zq7xW" not in str(got.value)
    assert logged(argv) == []


def parts(text: str, value: str = CANARY, length: bool = True) -> list[str]:
    """Every eight characters of `value` in `text`, and its length unless `text` holds other numbers."""
    found = [value[i : i + 8] for i in range(len(value) - 7) if value[i : i + 8] in text]
    return found + ([str(len(value))] if length and str(len(value)) in text else [])


def test_the_keychain_tools_words_never_reach_an_error_only_its_exit_code(keychain, monkeypatch):
    store, argv, fail = keychain
    fail.write_text("refuse")  # its words carry the whole token, and its read back is refused too
    with pytest.raises(AccountsError) as got:
        store_token("work", CANARY)
    assert str(got.value) == (
        "account 'work': the keychain tool exited 1; it could not be read back, so its keychain item may "
        "still hold the token; `security delete-generic-password -a work -s lion.account.claude_code` "
        "deletes it"
    )
    fail.write_text("partial")  # its words carry a part of the token and its length
    for act in (lambda: store_token("work", CANARY), lambda: token("work"), lambda: remove_token("work")):
        with pytest.raises(AccountsError, match="the keychain tool exited 1") as got:
            act()
        assert parts(str(got.value)) == [] and "prefix" not in str(got.value)
    fail.write_text("drop")  # the tool exits 0 and wrote nothing: the read back says so
    with pytest.raises(AccountsError, match="did not read back as written") as got:
        store_token("work", CANARY)
    assert CANARY not in str(got.value)
    fail.write_text("")
    store_token("work", CANARY)
    fail.write_text("leak")  # a failed read whose words carry the value it printed
    with pytest.raises(AccountsError) as got:
        token("work")
    assert str(got.value) == "account 'work': the keychain tool exited 1"
    fail.write_text("refuse")
    with pytest.raises(AccountsError, match="exited 1, so its keychain item was not deleted$"):
        remove_token("work")
    fail.write_text("")
    with pytest.raises(AccountsError, match="has no keychain item; `lion account add` writes it"):
        token("other")
    store.write_text(json.dumps({"odd/lion.account.claude_code": CANARY + "!"}))
    with pytest.raises(AccountsError, match="does not hold a token of the expected form") as got:
        token("odd")
    assert CANARY not in str(got.value)
    assert all(CANARY not in arg for call in logged(argv) for arg in call)


def test_a_write_the_tool_failed_after_storing_deletes_the_item_or_names_the_command_that_does(keychain):
    store, argv, fail = keychain
    fail.write_text("written then failed")
    with pytest.raises(AccountsError) as got:
        store_token("work", CANARY)
    assert str(got.value) == "account 'work': the keychain tool exited 1; the item it wrote was deleted"
    assert items(store) == {} and [c[0] for c in logged(argv)][-1] == "delete-generic-password"
    fail.write_text("sticky")  # the delete is refused too: the refusal names the command that deletes it
    with pytest.raises(AccountsError) as got:
        store_token("work", CANARY)
    assert str(got.value) == (
        "account 'work': the keychain tool exited 1; its keychain item may still hold the token; "
        "`security delete-generic-password -a work -s lion.account.claude_code` deletes it"
    )
    assert CANARY not in str(got.value)


def test_a_write_whose_read_back_cannot_be_read_names_the_command_that_deletes_the_item(
    keychain, monkeypatch
):
    store, _, _ = keychain
    real = accounts._security

    def answer(args, stdin=None):
        if args[0] == "find-generic-password":
            raise AccountsError("the keychain tool did not answer in 10 seconds")
        return real(args, stdin)

    monkeypatch.setattr(accounts, "_security", answer)
    with pytest.raises(AccountsError) as got:
        store_token("work", CANARY)
    assert str(got.value) == (
        "account 'work': the keychain item did not read back as written; it could not be read back, so its "
        "keychain item may still hold the token; `security delete-generic-password -a work -s "
        "lion.account.claude_code` deletes it"
    )


def test_a_keychain_tool_that_cannot_run_or_does_not_answer_is_said(keychain, monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "_SECURITY", str(tmp_path / "no-such-tool"))
    with pytest.raises(AccountsError, match="the keychain tool could not run"):
        token("work")

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10, output=CANARY)

    monkeypatch.setattr(accounts.subprocess, "run", slow)
    with pytest.raises(AccountsError, match="did not answer in 10 seconds") as got:
        store_token("work", CANARY)
    assert CANARY not in str(got.value) and "may still hold the token" in str(got.value)


ENVIRON = {
    "PATH": "/usr/bin:/bin",
    "ANTHROPIC_API_KEY": "made-up-competing-key-01",
    "CLAUDE_CODE_OAUTH_TOKEN": "made-up-inherited-token-01",
    "CC_ACCOUNT": "another",
    "CODEX_ACCESS_TOKEN": "made-up-codex-access-01",
    "CODEX_HOME": "/elsewhere/codex",
}


def test_a_claude_code_child_carries_its_accounts_token_and_no_competing_credential():
    given = dict(ENVIRON)
    env = child_env(Account("work", "claude_code", "keychain"), given, token=CANARY)
    assert env == {
        "PATH": "/usr/bin:/bin",
        "CLAUDE_CODE_OAUTH_TOKEN": CANARY,
        "CODEX_ACCESS_TOKEN": "made-up-codex-access-01",  # another CLI's variable: not this child's
        "CODEX_HOME": "/elsewhere/codex",
    }
    assert given == ENVIRON  # the process's own environment is not touched
    assert CREDENTIAL_VARIABLES["claude_code"] == (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CC_ACCOUNT",
    )


def test_a_claude_code_default_child_sets_nothing_and_keeps_no_competing_credential():
    env = child_env(Account("default", "claude_code", "default"), ENVIRON)
    assert env == {k: v for k, v in ENVIRON.items() if k not in CREDENTIAL_VARIABLES["claude_code"]}


def private_home(tmp_path: Path, login: Path) -> Path:
    """An agent's private home: a directory holding only `auth.json`, a link to `login`'s."""
    home = tmp_path / "agent-home"
    home.mkdir(exist_ok=True)
    (home / "auth.json").unlink(missing_ok=True)
    (home / "auth.json").symlink_to(login / "auth.json")
    return home


def test_a_codex_child_runs_from_the_private_home_and_keeps_no_access_token(tmp_path):
    a = tmp_path / "codex-a"
    a.mkdir()
    (tmp_path / "via").symlink_to(a)  # the account's home named through a link resolves the same
    for credential, login in (
        (str(a), a),
        (str(tmp_path / "via"), a),
        ("default", Path(ENVIRON["CODEX_HOME"])),
    ):
        home = str(private_home(tmp_path, login))
        env = child_env(Account("cx", "codex", credential), ENVIRON, home=home)
        assert env == {
            "PATH": "/usr/bin:/bin",
            "ANTHROPIC_API_KEY": "made-up-competing-key-01",
            "CLAUDE_CODE_OAUTH_TOKEN": "made-up-inherited-token-01",
            "CC_ACCOUNT": "another",
            "CODEX_HOME": home,
        }
    # default with no private home sets nothing: the inherited home stands, the access token goes
    env = child_env(Account("default", "codex", "default"), ENVIRON)
    assert env["CODEX_HOME"] == "/elsewhere/codex" and "CODEX_ACCESS_TOKEN" not in env
    assert CREDENTIAL_VARIABLES["codex"] == ("CODEX_ACCESS_TOKEN",)


def test_a_codex_child_whose_private_home_is_not_its_accounts_login_is_refused(tmp_path, monkeypatch):
    a, b = tmp_path / "codex-a", tmp_path / "codex-b"
    a.mkdir(), b.mkdir()
    cases = [
        (Account("cx", "codex", str(a)), b, ENVIRON),  # another account's login
        (Account("cx", "codex", "default"), a, ENVIRON),  # not default's, the environment's CODEX_HOME
        (Account("cx", "codex", "default"), a, {"PATH": "/usr/bin"}),  # not default's, ~/.codex
    ]
    monkeypatch.setenv("HOME", str(tmp_path / "person"))
    for account, login, environ in cases:
        home = private_home(tmp_path, login)
        with pytest.raises(AccountsError, match="the private home's login is not this account's") as got:
            child_env(account, environ, home=str(home))
        assert str(a) not in str(got.value) and str(b) not in str(got.value)
    copy = tmp_path / "copy-home"  # a login copied, not linked, is not the account's
    copy.mkdir()
    (copy / "auth.json").write_text("{}")
    with pytest.raises(AccountsError, match="is not this account's"):
        child_env(Account("cx", "codex", str(a)), ENVIRON, home=str(copy))
    # default with no CODEX_HOME in the environment is ~/.codex
    default = tmp_path / "person" / ".codex"
    env = child_env(Account("cx", "codex", "default"), {}, home=str(private_home(tmp_path, default)))
    assert env == {"CODEX_HOME": str(tmp_path / "agent-home")}


@pytest.mark.parametrize(
    ("account", "kwargs", "said"),
    [
        (Account("work", "claude_code", "keychain"), {}, "keeps a token in the keychain; none was given"),
        (Account("work", "claude_code", "default"), {"token": CANARY}, "keeps no token; one was given"),
        (Account("cx", "codex", "default"), {"token": CANARY}, "is a Codex account and takes no token"),
        (Account("cx", "codex", "/srv/codex-a"), {}, "needs the agent's private home"),
    ],
)
def test_a_child_environment_that_cannot_be_built_as_chosen_is_refused(account, kwargs, said):
    with pytest.raises(AccountsError, match=said) as got:
        child_env(account, ENVIRON, **kwargs)
    assert CANARY not in str(got.value)


def test_the_evidence_file_keeps_readings_probes_marks_and_last_use(tmp_path):
    path = tmp_path / "evidence.json"
    assert read_evidence(path) == {} and not path.exists()  # a read of no file writes none
    work, cx = Account("work", "claude_code", "keychain"), Account("default", "codex", "default")
    with held(path) as book:
        assert book.kept == {}
        k = book.of(work)
        k.readings["five_hour"] = reading(0.46, NOW, 1791143400.0)
        k.probe = Probe(NOW - 5, "figures")
        k.mark = Mark("quota", NOW, 1791143400.0)
        k.used = NOW
        book.of(cx).mark = Mark("person", NOW)
        book.save()
    kept = read_evidence(path)
    assert kept == {
        ("claude_code", "work"): Kept(
            {"five_hour": reading(0.46, NOW, 1791143400.0)},
            Probe(NOW - 5, "figures"),
            Mark("quota", NOW, 1791143400.0),
            NOW,
        ),
        ("codex", "default"): Kept(mark=Mark("person", NOW)),
    }
    assert json.loads(path.read_text())["codex"]["default"]["mark"] == {
        "kind": "person",
        "at": NOW,
        "until": None,
    }
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_record_keeps_the_later_reading_per_window(tmp_path):
    path = tmp_path / "evidence.json"
    work = Account("work", "claude_code", "keychain")
    record(path, work, {"five_hour": reading(0.40, NOW), "seven_day": reading(0.60, NOW)})
    record(path, work, {"five_hour": reading(0.10, NOW - 60)})  # older: the kept one stands
    record(path, work, {"seven_day": reading(0.62, NOW + 60, source="probe")})
    assert read_evidence(path)[("claude_code", "work")].readings == {
        "five_hour": reading(0.40, NOW),
        "seven_day": reading(0.62, NOW + 60, source="probe"),
    }


WRITER = """
import sys, time
from pathlib import Path
from hub.accounts import Account, held

path, who, n, go = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
while not go.exists():
    time.sleep(0.002)
for i in range(n):
    with held(path) as book:
        kept = book.of(Account(f"{who}-{i}", "claude_code", "default"))
        time.sleep(0.002)  # the decision, between the read and the write
        kept.used = 1800000000.0 + i
        book.save()
"""


def test_two_processes_writing_the_evidence_file_at_once_lose_no_write(tmp_path):
    path, go = tmp_path / "evidence.json", tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, str(path), who, "40", str(go)],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for who in ("p", "q")
    ]
    go.touch()
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    kept = read_evidence(path)
    assert sorted(kept) == sorted(("claude_code", f"{w}-{i}") for w in ("p", "q") for i in range(40))
    assert all(k.used == 1800000000.0 + int(n.rsplit("-", 1)[1]) for (_, n), k in kept.items())
    assert list(tmp_path.glob(".evidence.json.*")) == []  # no temporary file left behind


def test_a_holder_that_waits_past_its_bound_is_refused(tmp_path):
    path = tmp_path / "evidence.json"
    with held(path):
        t0 = time.monotonic()
        with pytest.raises(AccountsError, match="is held by another process past 0.2 seconds"):
            with held(path, wait=0.2):
                pass
        assert 0.2 <= time.monotonic() - t0 < 10
    with held(path, wait=0.2):  # let go, it is taken at once
        pass


def test_a_waiter_on_a_file_that_was_replaced_or_removed_takes_the_lock_on_the_path_anew(tmp_path):
    path = tmp_path / "evidence.json"
    work = Account("work", "claude_code", "default")
    seen: list[dict] = []

    def wait() -> None:
        with held(path, wait=20) as book:
            seen.append(dict(book.kept))

    with held(path) as book:
        waiter = threading.Thread(target=wait)
        waiter.start()
        time.sleep(0.3)  # the waiter has opened the file the save replaces
        book.of(work).used = NOW
        book.save()
        time.sleep(0.3)
        assert seen == []  # the lock went with the write to the new file: the waiter still waits
    waiter.join(20)
    assert seen[0][("claude_code", "work")].used == NOW

    with held(path):
        waiter = threading.Thread(target=wait)
        waiter.start()
        time.sleep(0.3)
        path.unlink()
    waiter.join(20)
    assert seen[1] == {}  # the path's new file, not the removed one


def test_a_write_that_fails_leaves_the_file_as_it_was_and_the_lock_held(tmp_path, monkeypatch):
    path = tmp_path / "evidence.json"
    work = Account("work", "claude_code", "default")
    record(path, work, {"five_hour": reading(0.4, NOW)})
    before = path.read_bytes()

    def broken(src, dst):
        raise OSError("made-up failure")

    with held(path) as book:
        book.of(work).used = NOW
        with monkeypatch.context() as m:
            m.setattr(accounts.os, "replace", broken)
            with pytest.raises(OSError, match="made-up failure"):
                book.save()
        assert path.read_bytes() == before
        assert list(tmp_path.glob(".evidence.json.*")) == []
        with pytest.raises(AccountsError, match="held by another process"):
            with held(path, wait=0.1):
                pass


@pytest.mark.parametrize(
    "doc",
    [
        "not json",
        "[]",
        '{"other": {}}',
        '{"codex": {"a": {"readings": {}}}}',
        '{"codex": {"a": {"readings": {"ten_hour": {"used": 0.1, "resets": null, "observed": 1.0, '
        '"source": "call"}}, "probe": null, "mark": null, "used": null}}}',
        '{"codex": {"a": {"readings": {"five_hour": {"used": -0.1, "resets": null, "observed": 1.0, '
        '"source": "call"}}, "probe": null, "mark": null, "used": null}}}',
        '{"codex": {"a": {"readings": {"five_hour": {"used": 0.1, "resets": null, "observed": 1.0, '
        '"source": "guess"}}, "probe": null, "mark": null, "used": null}}}',
        '{"codex": {"a": {"readings": {"five_hour": {"used": 0.1, "observed": 1.0, "source": "call", '
        '"extra": 1}}, "probe": null, "mark": null, "used": null}}}',
        '{"codex": {"a": {"readings": {}, "probe": {"at": 1.0, "ended": "maybe"}, "mark": null, '
        '"used": null}}}',
        '{"codex": {"a": {"readings": {}, "probe": null, "mark": {"kind": "person", "at": 1.0, '
        '"until": 2.0}, "used": null}}}',
        '{"codex": {"a": {"readings": {}, "probe": null, "mark": null, "used": "yesterday"}}}',
    ],
)
def test_a_file_that_is_not_evidence_is_refused_and_never_overwritten(tmp_path, doc):
    path = tmp_path / "evidence.json"
    path.write_text(doc)
    with pytest.raises(AccountsError, match="is not an evidence file, so it was not read"):
        record(path, Account("a", "codex", "default"), {})
    assert path.read_text() == doc
    path.write_text("")  # the refusal let the lock go: the next holder takes it at once
    with held(path, wait=0.1):
        pass


def test_a_plan_groups_reading_is_every_members_and_stays_within_its_provider(tmp_path):
    pool = parse_pool(
        '[[account]]\nname = "a"\nprovider = "claude_code"\ncredential = "keychain"\ngroup = "plan"\n'
        '[[account]]\nname = "b"\nprovider = "claude_code"\ncredential = "default"\ngroup = "plan"\n'
        '[[account]]\nname = "c"\nprovider = "claude_code"\ncredential = "default"\n'
        '[[account]]\nname = "x"\nprovider = "codex"\ncredential = "default"\ngroup = "plan"\n',
        tmp_path / "accounts.toml",
    )
    a, b, c, x = pool.accounts
    kept = {
        ("claude_code", "a"): Kept(
            {"five_hour": reading(0.2, NOW - 100), "seven_day": reading(0.5, NOW - 10)}
        ),
        ("claude_code", "b"): Kept({"five_hour": reading(0.3, NOW - 50)}),
        ("codex", "x"): Kept({"five_hour": reading(0.9, NOW)}),
    }
    group = {"five_hour": reading(0.3, NOW - 50), "seven_day": reading(0.5, NOW - 10)}
    assert readings(kept, pool, a) == group
    assert readings(kept, pool, b) == group
    assert readings(kept, pool, c) == {}  # no group: its own, and it has none
    assert readings(kept, pool, x) == {"five_hour": reading(0.9, NOW)}


def test_fresh_ends_at_the_reset_instant_and_holds_at_max_age_exactly():
    r = reading(0.5, NOW, resets=NOW + 300)
    assert fresh(r, NOW + 299.5, 600) is True
    assert fresh(r, NOW + 300, 600) is False  # at the reset instant the figure is gone
    assert fresh(r, NOW + 301, 600) is False
    r = reading(0.5, NOW)  # a source that named no reset: max_age alone ends it
    assert fresh(r, NOW + 600, 600) is True  # exactly max_age old is not older than it
    assert fresh(r, NOW + 600.001, 600) is False


def test_usable_at_each_threshold_exactly_and_unknown_with_a_window_unknown():
    s = Settings()

    def both(five: float, seven: float) -> dict:
        return {"five_hour": reading(five, NOW), "seven_day": reading(seven, NOW)}

    assert usable(both(0.9499, 0.9699), s, NOW) is True
    assert usable(both(0.95, 0.5), s, NOW) is False  # five hours at its threshold exactly
    assert usable(both(0.5, 0.97), s, NOW) is False  # seven days at its threshold exactly
    assert usable({"five_hour": reading(0.1, NOW)}, s, NOW) is UNKNOWN
    assert usable({"seven_day": reading(0.1, NOW)}, s, NOW) is UNKNOWN
    assert usable({"seven_day": reading(0.99, NOW)}, s, NOW) is False  # over one: false whatever the other
    assert usable({"five_hour": reading(0.96, NOW)}, s, NOW) is False
    assert usable({}, s, NOW) is UNKNOWN
    stale = {"five_hour": reading(0.1, NOW - 601), "seven_day": reading(0.1, NOW)}
    assert usable(stale, s, NOW) is UNKNOWN  # a reading past max_age is no reading
    assert usable(stale, Settings(max_age=700), NOW) is True


def test_unknown_is_its_own_value_and_never_a_truth_or_a_number():
    with pytest.raises(TypeError):
        bool(UNKNOWN)
    with pytest.raises(TypeError):
        float(UNKNOWN)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        UNKNOWN < 0.5  # type: ignore[operator]  # noqa: B015
    assert repr(UNKNOWN) == "UNKNOWN" and UNKNOWN is not None


def test_the_claude_code_row_gives_each_window_observed_when_read():
    assert claude_code_reading(CLAUDE_ROW, NOW, "call") == {
        "five_hour": Window(0.46, 1791143400.0, NOW, "call"),
        "seven_day": Window(0.64, 1791655200.0, NOW, "call"),
    }


@pytest.mark.parametrize(
    ("change", "left"),
    [
        (lambda w: w["five_hour"].pop("utilization"), {"seven_day"}),
        (lambda w: w["seven_day"].pop("resetsAt"), {"five_hour"}),
        (lambda w: w.pop("five_hour"), {"seven_day"}),
        (lambda w: w["five_hour"].update(utilization="0.46"), {"seven_day"}),
        (lambda w: w["five_hour"].update(utilization=True), {"seven_day"}),
        (lambda w: w["five_hour"].update(utilization=float("nan")), {"seven_day"}),
        (lambda w: w["five_hour"].update(utilization=-0.1), {"seven_day"}),
        (lambda w: w["seven_day"].update(resetsAt="1791655200"), {"five_hour"}),
        (lambda w: w["seven_day"].update(resetsAt=0), {"five_hour"}),
        (lambda w: w.update(seven_day=[0.64]), {"five_hour"}),
    ],
)
def test_a_claude_code_window_with_a_field_missing_or_of_the_wrong_type_is_unknown(change, left):
    row = json.loads(json.dumps(CLAUDE_ROW))
    change(row["rate_limit_info"]["unifiedWindows"])
    got = claude_code_reading(row, NOW, "probe")
    assert set(got) == left and all(r.source == "probe" and r.used > 0 for r in got.values())


@pytest.mark.parametrize(
    "row",
    [
        None,
        [CLAUDE_ROW],
        {**CLAUDE_ROW, "type": "result"},
        {"type": "rate_limit_event"},
        {"type": "rate_limit_event", "rate_limit_info": "allowed"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": ["five_hour"]}},
    ],
)
def test_a_claude_code_row_of_another_shape_gives_no_reading_and_no_error(row):
    assert claude_code_reading(row, NOW, "call") == {}


def test_the_codex_row_gives_each_window_observed_at_its_own_timestamp():
    assert codex_reading(CODEX_ROW, "call") == {
        "five_hour": Window(0.46, 1791143400.0, CODEX_AT, "call"),
        "seven_day": Window(0.25, 1791655200.0, CODEX_AT, "call"),
    }
    # the shape this tree's session tests carry: no reset, so only max_age ends the reading
    row = {
        "timestamp": "2026-10-04T16:00:00+00:00",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {"primary": {"used_percent": 25.0, "window_minutes": 10080}},
        },
    }
    assert codex_reading(row, "probe") == {"seven_day": Window(0.25, None, CODEX_AT, "probe")}


@pytest.mark.parametrize(
    ("change", "left"),
    [
        (lambda r: r["secondary"].pop("used_percent"), {"five_hour"}),
        (lambda r: r["primary"].pop("window_minutes"), {"seven_day"}),
        (lambda r: r["primary"].update(window_minutes="300"), {"seven_day"}),
        (lambda r: r["primary"].update(window_minutes=60), {"seven_day"}),
        (lambda r: r["secondary"].update(used_percent="25"), {"five_hour"}),
        (lambda r: r["secondary"].update(used_percent=None), {"five_hour"}),
        (lambda r: r["secondary"].update(used_percent=False), {"five_hour"}),
        (lambda r: r["secondary"].update(used_percent=-1), {"five_hour"}),
        (lambda r: r.update(primary=[46.0]), {"seven_day"}),
    ],
)
def test_a_codex_window_with_a_field_missing_or_of_the_wrong_type_is_unknown(change, left):
    row = json.loads(json.dumps(CODEX_ROW))
    change(row["payload"]["rate_limits"])
    got = codex_reading(row, "call")
    assert set(got) == left and all(r.used > 0 for r in got.values())


def test_a_codex_reset_that_is_missing_or_not_an_instant_leaves_the_reading_without_one():
    row = json.loads(json.dumps(CODEX_ROW))
    row["payload"]["rate_limits"]["primary"].pop("resets_at")
    row["payload"]["rate_limits"]["secondary"]["resets_at"] = "soon"
    got = codex_reading(row, "call")
    assert got["five_hour"].resets is None and got["seven_day"].resets is None
    assert got["five_hour"].used == 0.46


@pytest.mark.parametrize(
    "row",
    [
        None,
        "token_count",
        {**CODEX_ROW, "timestamp": None},
        {**CODEX_ROW, "timestamp": 1791158400},
        {**CODEX_ROW, "timestamp": "2026-10-04T16:00:00"},  # no zone: no instant
        {**CODEX_ROW, "timestamp": "yesterday"},
        {k: v for k, v in CODEX_ROW.items() if k != "timestamp"},
        {**CODEX_ROW, "payload": None},
        {**CODEX_ROW, "payload": {**CODEX_ROW["payload"], "type": "agent_message"}},
        {**CODEX_ROW, "payload": {**CODEX_ROW["payload"], "rate_limits": None}},
    ],
)
def test_a_codex_row_of_another_shape_or_with_no_instant_gives_no_reading_and_no_error(row):
    assert codex_reading(row, "call") == {}


def run(argv: list[str], capsys, stdin: str = "") -> tuple[int, str, str]:
    """`lion` as a person runs it: (exit code, standard output, standard error)."""
    was, sys.stdin = sys.stdin, io.StringIO(stdin)
    try:
        main(argv)
        code = 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stdin = was
    out, err = capsys.readouterr()
    return code, out, err


POOL_TEXT = (
    "# the accounts on this machine\n"
    "\n"
    "[[account]]\n"
    'name = "own"   # the CLI\'s own sign-in\n'
    'provider = "claude_code"\n'
    'credential = "default"\n'
    "\n"
    "[pick]\n"
    "max_age = 900  # seconds\n"
)


@pytest.mark.parametrize("text", [POOL_TEXT, POOL_TEXT.rstrip("\n"), POOL_TEXT.replace("\n", "\r\n"), ""])
def test_add_writes_the_entry_at_the_end_and_keeps_every_other_byte(tmp_path, keychain, capsys, text):
    store, _, _ = keychain
    path = tmp_path / "accounts.toml"
    if text:
        path.write_bytes(text.encode())
    code, out, err = run(
        [
            "account",
            "add",
            "work",
            "--provider",
            "claude_code",
            "--credential",
            "keychain",
            "--group",
            "team",
            "--file",
            str(path),
        ],
        capsys,
        stdin=CANARY + "\n",
    )
    assert (code, err) == (0, "")
    assert out == f"added work to {path}: claude_code, keychain, group team\n"
    written = path.read_bytes().decode()
    entry = '[[account]]\nname = "work"\nprovider = "claude_code"\ncredential = "keychain"\ngroup = "team"\n'
    joint = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    assert written == text + joint + entry
    assert items(store) == {"work/lion.account.claude_code": CANARY}
    assert load_pool(path).settings.max_age == (900 if text else 600)


def test_add_reads_the_token_without_echo_at_a_terminal(tmp_path, keychain, monkeypatch, capsys):
    store, _, _ = keychain
    asked = []

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    def getpass(prompt: str) -> str:
        asked.append(prompt)
        return CANARY + "\n"

    monkeypatch.setattr(account_cli.getpass, "getpass", getpass)
    monkeypatch.setattr(sys, "stdin", Terminal())
    main(
        [
            "account",
            "add",
            "work",
            "--provider",
            "claude_code",
            "--credential",
            "keychain",
            "--file",
            str(tmp_path / "accounts.toml"),
        ]
    )
    assert asked == ["the account's token (not shown): "]
    assert items(store) == {"work/lion.account.claude_code": CANARY}
    assert CANARY not in "".join(capsys.readouterr())


def test_add_a_codex_home_and_a_default_reads_no_token_and_says_no_place(tmp_path, keychain, capsys):
    _, argv, _ = keychain
    path = tmp_path / "accounts.toml"
    home = tmp_path / "codex-a"
    code, out, _ = run(
        ["account", "add", "cx", "--provider", "codex", "--credential", str(home), "--file", str(path)],
        capsys,
    )
    assert code == 0 and out == f"added cx to {path}: codex, home\n" and str(home) not in out
    code, out, _ = run(
        [
            "account",
            "add",
            "own",
            "--provider",
            "claude_code",
            "--credential",
            "default",
            "--file",
            str(path),
        ],
        capsys,
    )
    assert code == 0 and out == f"added own to {path}: claude_code, default\n"
    assert logged(argv) == []  # neither touched the keychain
    assert [a.name for a in load_pool(path).accounts] == ["cx", "own"]


def test_add_refuses_a_name_already_present_and_a_file_it_cannot_add_to_in_place(tmp_path, keychain, capsys):
    store, argv, _ = keychain
    path = tmp_path / "accounts.toml"
    path.write_text(POOL_TEXT)
    add = [
        "account",
        "add",
        "own",
        "--provider",
        "claude_code",
        "--credential",
        "keychain",
        "--file",
        str(path),
    ]
    code, out, err = run(add, capsys, stdin=CANARY)
    assert code == 1 and out == ""
    assert err == f"lion account add: account 'own' is already in {path}; nothing was written\n"
    inline = 'account = [{ name = "a", provider = "codex", credential = "default" }]\n'
    path.write_text(inline)
    code, _, err = run(add, capsys, stdin=CANARY)
    assert code == 1 and "each account is its own [[account]] table" in err
    assert path.read_text() == inline and logged(argv) == [] and items(store) == {}
    for name, group in ((SHAPED, None), ("x", SHAPED), ("n" * 65, None)):  # labels of a credential's shape
        argv_ = [*add[:2], name, *add[3:], *([] if group is None else ["--group", group])]
        path.write_text(POOL_TEXT)
        code, out, err = run(argv_, capsys, stdin=CANARY)
        assert code == 1 and out == "" and "not letters" in err
        assert "Zq7xW" not in err and path.read_text() == POOL_TEXT and items(store) == {}


@pytest.mark.parametrize("reread", ["refused", "another account"])
def test_an_add_whose_text_does_not_read_back_as_the_one_account_added_writes_nothing(
    tmp_path, keychain, monkeypatch, capsys, reread
):
    # the pool's own rules leave no file whose added text reads back otherwise; the read-back is the
    # backstop for one that ever does, so the parse of the added text is made to differ here
    store, argv, _ = keychain
    path = tmp_path / "accounts.toml"
    path.write_text(POOL_TEXT)
    real = account_cli.parse_pool

    def parse(text, where):
        pool = real(text, where)
        if text.startswith(POOL_TEXT) and text != POOL_TEXT:  # the text with the entry added
            if reread == "refused":
                raise AccountsError("made-up refusal")
            return dataclasses.replace(pool, accounts=pool.accounts[:-1])
        return pool

    monkeypatch.setattr(account_cli, "parse_pool", parse)
    add = ["account", "add", "work", "--provider", "claude_code", "--credential", "keychain"]
    code, out, err = run([*add, "--file", str(path)], capsys, stdin=CANARY)
    assert code == 1 and out == ""
    assert (
        err == f"lion account add: {path}: the account could not be added in place, so nothing was written\n"
    )
    assert path.read_text() == POOL_TEXT and logged(argv) == [] and items(store) == {}


def test_a_file_write_that_fails_after_the_item_takes_the_item_back(tmp_path, keychain, monkeypatch, capsys):
    store, argv, _ = keychain
    path = tmp_path / "accounts.toml"
    path.write_text(POOL_TEXT)

    def broken(src, dst):
        raise OSError("made-up failure")

    monkeypatch.setattr(account_cli.os, "replace", broken)
    with pytest.raises(OSError, match="made-up failure"):
        run(
            [
                "account",
                "add",
                "work",
                "--provider",
                "claude_code",
                "--credential",
                "keychain",
                "--file",
                str(path),
            ],
            capsys,
            stdin=CANARY,
        )
    assert path.read_text() == POOL_TEXT and list(tmp_path.glob(".accounts.toml.*.tmp")) == []
    assert items(store) == {} and logged(argv)[-1][0] == "delete-generic-password"
    calls = len(logged(argv))
    with pytest.raises(OSError, match="made-up failure"):  # an account with no item: none to take back
        run(
            ["account", "add", "cx", "--provider", "codex", "--credential", "default", "--file", str(path)],
            capsys,
        )
    assert path.read_text() == POOL_TEXT and len(logged(argv)) == calls


def test_a_file_write_that_fails_with_an_item_that_cannot_be_taken_back_names_the_command_that_deletes_it(
    tmp_path, keychain, monkeypatch, capsys
):
    store, argv, fail = keychain
    path = tmp_path / "accounts.toml"
    path.write_text(POOL_TEXT)

    def broken(src, dst):
        fail.write_text("sticky")  # from here on the tool keeps every item it is asked to delete
        raise OSError("made-up failure")

    monkeypatch.setattr(account_cli.os, "replace", broken)
    add = ["account", "add", "work", "--provider", "claude_code", "--credential", "keychain"]
    with pytest.raises(OSError, match="made-up failure") as raised:
        run([*add, "--file", str(path)], capsys, stdin=CANARY)
    assert raised.value.__notes__ == [
        "account 'work': the keychain tool exited 1, so its keychain item was not deleted; "
        "`security delete-generic-password -a work -s lion.account.claude_code` deletes it"
    ]
    assert path.read_text() == POOL_TEXT and items(store) == {"work/lion.account.claude_code": CANARY}
    assert logged(argv)[-1][0] == "delete-generic-password"


REMOVE_TEXT = (
    "# the accounts on this machine\n"
    "\n"
    "[[account]]\n"
    'name = "a"\n'
    'provider = "claude_code"\n'
    'credential = "default"\n'
    "\n"
    "# the work plan\n"
    "[[account]]\n"
    'name = "b"\n'
    'provider = "claude_code"\n'
    "# its token is in the keychain\n"
    'credential = "keychain"\n'
    'group = "team"\n'
    "\n"
    "# the codex one\n"
    "[[ account ]]  # spaced\n"
    'name = "c"\n'
    'provider = "codex"\n'
    'credential = "HOME/codex-c"\n'
    "\n"
    "[pick]\n"
    "keep = 1800\n"
)


def test_remove_deletes_the_table_and_its_item_and_leaves_the_rest_and_a_codex_home(
    tmp_path, keychain, capsys
):
    store, _, _ = keychain
    path = tmp_path / "accounts.toml"
    path.write_text(REMOVE_TEXT.replace("HOME", str(tmp_path)))
    (tmp_path / "codex-c").mkdir()
    store_token("b", CANARY)
    code, out, err = run(["account", "remove", "b", "--file", str(path)], capsys)
    assert (code, err) == (0, "") and out == f"removed b from {path}, and its keychain item\n"
    expected = REMOVE_TEXT.replace("HOME", str(tmp_path))
    gone = expected[expected.index("# the work plan") : expected.index("# the codex one")]
    assert path.read_text() == expected.replace(gone, "")
    assert items(store) == {}
    code, out, _ = run(["account", "remove", "c", "--file", str(path)], capsys)
    assert out == f"removed c from {path}, its home is left to its person\n"
    assert (tmp_path / "codex-c").is_dir()
    code, out, _ = run(["account", "remove", "a", "--file", str(path)], capsys)
    assert out == f"removed a from {path}, on the CLI's own sign-in\n"
    assert path.read_text() == "# the accounts on this machine\n\n[pick]\nkeep = 1800\n"
    assert load_pool(path).settings.keep == 1800


def test_remove_writes_the_file_before_it_deletes_the_item(tmp_path, keychain, monkeypatch, capsys):
    path = tmp_path / "accounts.toml"
    path.write_text(REMOVE_TEXT.replace("HOME", str(tmp_path)))
    store_token("b", CANARY)
    seen = []

    def delete(name):
        seen.append([a.name for a in load_pool(path).accounts])
        return remove_token(name)

    monkeypatch.setattr(account_cli, "remove_token", delete)
    assert run(["account", "remove", "b", "--file", str(path)], capsys)[0] == 0
    assert seen == [["a", "c"]]  # the file no longer named it when its item went


def test_a_remove_whose_item_is_not_deleted_puts_its_table_back(tmp_path, keychain, monkeypatch, capsys):
    store, _, fail = keychain
    path = tmp_path / "accounts.toml"
    text = REMOVE_TEXT.replace("HOME", str(tmp_path))
    path.write_text(text)
    store_token("b", CANARY)
    fail.write_text("sticky")
    code, out, err = run(["account", "remove", "b", "--file", str(path)], capsys)
    assert (code, out) == (1, "")
    assert err == (
        "lion account remove: account 'b': the keychain tool exited 1, so its keychain item was not "
        "deleted; its table was put back, so nothing was changed\n"
    )
    assert path.read_text() == text and items(store) == {"b/lion.account.claude_code": CANARY}
    writes = []
    real = account_cli._write

    def second_fails(p, t):
        writes.append(t)
        if len(writes) == 2:
            raise OSError("made-up failure")
        real(p, t)

    monkeypatch.setattr(account_cli, "_write", second_fails)
    code, _, err = run(["account", "remove", "b", "--file", str(path)], capsys)
    assert code == 1 and "could not be put back (OSError), so the item is left with no entry" in err
    assert "`security delete-generic-password -a b -s lion.account.claude_code` deletes it" in err
    assert "b" not in [a.name for a in load_pool(path).accounts] and CANARY not in err


def test_remove_says_a_keychain_account_that_had_no_item(tmp_path, keychain, capsys):
    path = tmp_path / "accounts.toml"
    path.write_text('[[account]]\nname = "b"\nprovider = "claude_code"\ncredential = "keychain"\n')
    code, out, _ = run(["account", "remove", "b", "--file", str(path)], capsys)
    assert code == 0 and out == f"removed b from {path}, which had no keychain item\n"
    assert path.read_text() == ""


def test_remove_refuses_an_absent_name_and_an_account_it_cannot_remove_in_place(tmp_path, keychain, capsys):
    _, argv, _ = keychain
    path = tmp_path / "accounts.toml"
    code, _, err = run(["account", "remove", "a", "--file", str(path)], capsys)
    assert code == 1 and err == f"lion account remove: no account 'a' in {path}\n"
    code, _, err = run(["account", "remove", SHAPED, "--file", str(path)], capsys)
    assert code == 1 and "an account's name is letters" in err and "Zq7xW" not in err
    inline = 'account = [{ name = "a", provider = "claude_code", credential = "keychain" }]\n'
    path.write_text(inline)
    code, _, err = run(["account", "remove", "a", "--file", str(path)], capsys)
    assert code == 1 and "each account is its own [[account]] table" in err
    assert path.read_text() == inline and logged(argv) == []
    path.write_bytes(b"\xff")
    code, _, err = run(["account", "remove", "a", "--file", str(path)], capsys)
    assert code == 1 and "is not UTF-8, so nothing was written" in err


def test_a_remove_that_finds_no_table_of_its_account_in_the_text_changes_nothing(
    tmp_path, keychain, monkeypatch, capsys
):
    # as with add: no file the pool accepts has an account whose table the text does not hold, so each
    # table's own parse is made to name another account here
    store, argv, _ = keychain
    path = tmp_path / "accounts.toml"
    text = REMOVE_TEXT.replace("HOME", str(tmp_path))
    path.write_text(text)
    store_token("b", CANARY)
    calls = len(logged(argv))
    real = account_cli.parse_pool

    def parse(source, where):
        pool = real(source, where)
        if source == text:
            return pool
        return dataclasses.replace(  # one table, as remove reads it out of the text
            pool, accounts=tuple(dataclasses.replace(a, name=a.name + "x") for a in pool.accounts)
        )

    monkeypatch.setattr(account_cli, "parse_pool", parse)
    code, out, err = run(["account", "remove", "b", "--file", str(path)], capsys)
    assert code == 1 and out == ""
    assert (
        err
        == f"lion account remove: {path}: account 'b' could not be removed in place, so nothing was changed\n"
    )
    assert path.read_text() == text and len(logged(argv)) == calls
    assert items(store) == {"b/lion.account.claude_code": CANARY}


WRAPPER = """import sys

import hub.accounts as accounts

accounts._SECURITY = sys.argv[1]  # the test's script; the real tool is never run
assert accounts._SECURITY != "/usr/bin/security"
from lion_cli.main import main

main(sys.argv[2:])
"""


def lion_process(tmp_path: Path, args: list[str], stdin: str, env: dict | None = None) -> subprocess.Popen:
    """`lion` as its own process, on the test's keychain script."""
    wrapper = tmp_path / "lion_wrapper.py"
    wrapper.write_text(WRAPPER)
    return subprocess.Popen(
        [sys.executable, str(wrapper), str(tmp_path / "security"), *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **(env or {})},
    )


def test_lion_account_add_as_its_own_process_has_the_token_in_no_processs_arguments(tmp_path, keychain):
    store, argv, _ = keychain
    path, above = tmp_path / "accounts.toml", tmp_path / "parent.args"
    args = [
        "account",
        "add",
        "work",
        "--provider",
        "claude_code",
        "--credential",
        "keychain",
        "--file",
        str(path),
    ]
    proc = lion_process(tmp_path, args, CANARY, {"SECURITY_PARENT_LOG": str(above)})
    out, err = proc.communicate(CANARY + "\n", timeout=60)
    assert (proc.returncode, err) == (0, "") and out == f"added work to {path}: claude_code, keychain\n"
    assert items(store) == {"work/lion.account.claude_code": CANARY}
    parents = above.read_text().splitlines()
    assert len(parents) == 2 and all("lion_wrapper.py" in a and "account add work" in a for a in parents)
    # a path holds digits of its own, so the length is not looked for in arguments
    assert parts("\n".join(parents), length=False) == [] and parts(argv.read_text(), length=False) == []


def test_two_adds_at_once_on_one_pool_both_land(tmp_path, keychain):
    # the first holds its keychain write for 1.5 s; the second starts inside it. Without the pool's lock
    # the second reads the file the first has not written yet, and the later write drops the other's
    store, _, _ = keychain
    path = tmp_path / "accounts.toml"
    path.write_text(POOL_TEXT)
    add = ["--provider", "claude_code", "--credential", "keychain", "--file", str(path)]
    first = lion_process(tmp_path, ["account", "add", "slow", *add], CANARY)
    first.stdin.write(CANARY + "\n")
    first.stdin.close()
    writing = Path(str(store) + ".writing")
    deadline = time.monotonic() + 30
    while not writing.exists():
        assert first.poll() is None and time.monotonic() < deadline
        time.sleep(0.01)
    second = lion_process(tmp_path, ["account", "add", "fast", *add], CANARY)
    out, err = second.communicate(CANARY + "\n", timeout=60)
    assert (second.returncode, err) == (0, ""), err
    assert first.wait(timeout=60) == 0, first.stderr.read()
    first.stdout.close(), first.stderr.close()
    assert [a.name for a in load_pool(path).accounts] == ["own", "slow", "fast"]
    assert set(items(store)) == {"slow/lion.account.claude_code", "fast/lion.account.claude_code"}
    assert (tmp_path / ".accounts.toml.lock").exists()


def test_a_command_that_waits_past_its_bound_for_the_pools_lock_is_refused_before_it_reads(tmp_path):
    import fcntl

    path = tmp_path / "accounts.toml"
    fd = os.open(tmp_path / ".accounts.toml.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        read = []
        with pytest.raises(AccountsError, match="is being changed by another command past 0.05 seconds"):
            with account_cli._locked(path, wait=0.05):
                read.append(1)
        assert read == []
    finally:
        os.close(fd)


def test_list_shows_each_window_with_its_age_and_source_and_each_mark(tmp_path, keychain, capsys):
    path = tmp_path / "accounts.toml"
    path.write_text(
        '[[account]]\nname = "a"\nprovider = "claude_code"\ncredential = "keychain"\ngroup = "plan"\n'
        '[[account]]\nname = "b"\nprovider = "claude_code"\ncredential = "default"\ngroup = "plan"\n'
        f'[[account]]\nname = "c"\nprovider = "codex"\ncredential = "{tmp_path}/codex-c"\n'
        '[[account]]\nname = "d"\nprovider = "codex"\ncredential = "default"\n'
    )
    pool = load_pool(path)
    a, b, c, d = pool.accounts
    now = time.time()
    record(pool.evidence, a, {"five_hour": reading(0.46, now - 90, now + 3600, "probe")})
    record(pool.evidence, b, {"seven_day": reading(0.6, now - 7200, now + 86400)})
    record(
        pool.evidence,
        c,
        {"seven_day": reading(0.25, now - 30), "five_hour": reading(0.9, now - 600, now - 1)},
    )
    with held(pool.evidence) as book:
        book.of(a).mark = Mark("quota", now, 1791143400.0)
        book.of(b).mark = Mark("person", now)
        book.of(c).mark = Mark("quota", now)
        book.save()
    code, out, err = run(["account", "list", "--file", str(path)], capsys)
    assert (code, err) == (0, "")
    until = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(1791143400.0))
    assert out.splitlines() == [
        "a: claude_code, keychain, group plan; five_hour 0.46 (1m old, from a probe); "
        f"seven_day 0.60 (2h old, from a call, older than max_age); marked: out of quota until {until}",
        "b: claude_code, default, group plan; five_hour 0.46 (1m old, from a probe); "
        "seven_day 0.60 (2h old, from a call, older than max_age); marked: needs its person",
        "c: codex, home; five_hour unknown; seven_day 0.25 (30s old, from a call); "
        "marked: out of quota until a probe succeeds",
        "d: codex, default; five_hour unknown; seven_day unknown; no mark",
    ]
    code, out, _ = run(["account", "list", "--json", "--file", str(path)], capsys)
    listed = json.loads(out)
    assert [r["credential"] for r in listed] == ["keychain", "default", "home", "default"]
    assert listed[2]["windows"]["five_hour"] is None  # past its reset: no reading, never a number
    assert (
        listed[0]["windows"]["five_hour"]["source"] == "probe" and listed[0]["windows"]["five_hour"]["fresh"]
    )
    assert listed[3] == {
        "name": "d",
        "provider": "codex",
        "credential": "default",
        "group": None,
        "windows": {"five_hour": None, "seven_day": None},
        "mark": None,
    }
    assert str(tmp_path / "codex-c") not in out


def test_list_with_no_file_shows_the_two_defaults_and_an_empty_file_says_so(tmp_path, capsys):
    path = tmp_path / "accounts.toml"
    code, out, _ = run(["account", "list", "--file", str(path)], capsys)
    assert out.splitlines() == [
        "default: claude_code, default; five_hour unknown; seven_day unknown; no mark",
        "default: codex, default; five_hour unknown; seven_day unknown; no mark",
    ]
    assert not path.exists() and not (tmp_path / "evidence.json").exists()  # a read writes nothing
    path.write_text("")
    code, out, _ = run(["account", "list", "--file", str(path)], capsys)
    assert out == f"no accounts: {path} declares none\n"
    assert rows(path) == []
    assert [account_cli._age(s) for s in (-3, 59, 60, 3599, 3600, 86400 * 2)] == [
        "0s",
        "59s",
        "1m",
        "59m",
        "1h",
        "2d",
    ]


def test_a_made_up_token_is_in_nothing_any_command_or_refusal_wrote(tmp_path, keychain, monkeypatch, capsys):
    store, argv, fail = keychain
    path = tmp_path / "accounts.toml"
    said: list[str] = []  # every text a command printed and every message an act raised

    def lion(argv_: list[str], stdin: str = "") -> int:
        code, out, err = run(argv_, capsys, stdin)
        said.extend([out, err])
        return code

    def refused(act) -> None:
        with pytest.raises((AccountsError, OSError)) as got:
            act()
        said.append(str(got.value))

    add = [
        "account",
        "add",
        "work",
        "--provider",
        "claude_code",
        "--credential",
        "keychain",
        "--file",
        str(path),
    ]
    assert lion(add, CANARY + "\n") == 0
    pool = load_pool(path)
    record(pool.evidence, pool.accounts[0], {"five_hour": reading(0.46, time.time() - 5)})
    assert lion(["account", "list", "--file", str(path)]) == 0
    assert lion(["account", "list", "--json", "--file", str(path)]) == 0
    other = ["account", "add", "x", "--provider", "claude_code", "--file", str(path)]
    assert lion(add, CANARY) == 1  # present
    assert lion([*other, "--credential", CANARY], CANARY) == 1  # the token where a reference belongs
    assert lion([*other, "--credential", "keychain", "--group", "a team"], CANARY) == 1
    assert (
        lion(
            [
                "account",
                "add",
                "a b",
                "--provider",
                "claude_code",
                "--credential",
                "keychain",
                "--file",
                str(path),
            ],
            CANARY,
        )
        == 1
    )
    for bad in (CANARY + "!", CANARY * 90, "", CANARY + " " + CANARY):
        assert lion([*other, "--credential", "keychain"], bad) == 1
    for mode in ("refuse", "drop", "partial", "written then failed"):
        fail.write_text(mode)
        assert lion([*other, "--credential", "keychain"], CANARY) == 1
    fail.write_text("")
    for name, group in ((SHAPED, None), ("y", SHAPED)):
        shaped = [*other[:2], name, *other[3:], "--credential", "keychain"]
        assert lion(shaped + ([] if group is None else ["--group", group]), CANARY) == 1
    assert lion(["account", "remove", SHAPED, "--file", str(path)]) == 1

    def broken(src, dst):
        raise OSError("made-up failure")

    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 10, output=CANARY)

    with monkeypatch.context() as m:
        m.setattr(account_cli.os, "replace", broken)
        refused(lambda: run([*other, "--credential", "keychain"], capsys, CANARY))
    said.extend(capsys.readouterr())
    bad = tmp_path / "bad.toml"
    bad.write_text(f'[[account]]\nname = "z"\nprovider = "claude_code"\ncredential = "{CANARY}"\n')
    assert lion(["account", "list", "--file", str(bad)]) == 1
    assert lion(["account", "remove", "z", "--file", str(bad)]) == 1
    refused(lambda: load_pool(bad))
    bad.write_text(f'[[account]]\nname = "z"\nprovider = "codex"\ncredential = "{CANARY}"\n')
    refused(lambda: load_pool(bad))
    for key in ("max_age", "seven_day"):
        bad.write_text(f'[pick]\n{key} = "{CANARY}"\n')
        assert lion(["account", "list", "--file", str(bad)]) == 1
    bad.write_text(f'[pick]\nprobe_model = {{ codex = "{SHAPED}" }}\n')
    assert lion(["account", "list", "--file", str(bad)]) == 1
    store.write_text(json.dumps({**items(store), "odd/lion.account.claude_code": CANARY + "!"}))
    refused(lambda: token("odd"))
    store_token("odd", CANARY)
    fail.write_text("leak")
    refused(lambda: token("odd"))
    fail.write_text("refuse")
    refused(lambda: store_token("odd", CANARY))
    assert lion(["account", "remove", "work", "--file", str(path)]) == 1
    fail.write_text("")
    for account, kwargs in (
        (Account("o", "claude_code", "default"), {"token": CANARY}),
        (Account("o", "codex", "default"), {"token": CANARY}),
    ):
        refused(lambda account=account, kwargs=kwargs: child_env(account, ENVIRON, **kwargs))
    with monkeypatch.context() as m:
        m.setattr(accounts.subprocess, "run", slow)
        refused(lambda: store_token("odd", CANARY))
    assert lion(["account", "remove", "work", "--file", str(path)]) == 0
    assert items(store) == {"odd/lion.account.claude_code": CANARY}  # the one item left is the test's own

    windows = {CANARY[i : i + 8] for i in range(len(CANARY) - 7)}  # any part of it
    windows |= {SHAPED[i : i + 8] for i in range(12, len(SHAPED) - 7)}
    written = [
        *said,
        argv.read_text(),
        path.read_text(),
        load_pool(path).evidence.read_text(),
    ]
    assert len(said) >= 50 and all(s is not None for s in said)
    assert [w for w in windows for text in written if w in text] == []
    assert "lion account add: " in "".join(said)  # the refusals were read, not an empty capture
