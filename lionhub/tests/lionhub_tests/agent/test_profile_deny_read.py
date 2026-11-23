# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""An agent profile's ``deny_read`` adds to what its children may not read, on every route.

The entries only tighten: they land after every ``allow_read`` exemption, so a profile that is
exempted from a path and also denies it keeps it denied. A route with nothing to hold them
refuses the run, naming what is missing. HOME is a throwaway directory in every test.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi._errors import ConfigurationError

child_sandbox = importlib.import_module("lionhub.agent.child_sandbox")
seatbelt = importlib.import_module("lionhub.tools._seatbelt")
claude_code = importlib.import_module("lionhub.providers.anthropic.claude_code")
codex = importlib.import_module("lionhub.providers.openai.codex")
gemini_code = importlib.import_module("lionhub.providers.google.gemini_code")
spawn = importlib.import_module("lionhub.orchestration.spawn")
factory = importlib.import_module("lionhub.agent.factory")

VAULT = "~/vault"
KEYS = "/opt/profile-keys"
AGY = "/opt/agy"

#: The settings deny ``~/vault`` and exempt both test profiles from it; ``builder`` denies it
#: again in its own file, ``plain`` does not.
SETTINGS = {
    "child_sandbox": {
        "deny_read": [VAULT],
        "profiles": {"builder": {"allow_read": [VAULT]}, "plain": {"allow_read": [VAULT]}},
    }
}
OFF = {"child_sandbox": {"enabled": False}}


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    home = tmp_path.resolve() / "home"
    agents = home / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "builder.md").write_text(f"---\nname: builder\ndeny_read:\n  - {VAULT}\n---\n")
    (agents / "plain.md").write_text("---\nname: plain\n---\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", home / ".lionagi")
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", home / ".lionagi" / "state.db")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("LIONAGI_LOGINS_DIR", raising=False)
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: SETTINGS)
    return home


@pytest.fixture
def sandbox_off(monkeypatch):
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: OFF)


def _work(tmp_path: Path) -> Path:
    work = tmp_path.resolve() / "work"
    work.mkdir(exist_ok=True)
    return work


# --------------------------------------------------------------------------- the profile key


def test_entries_are_absolute_or_home_relative_and_kept_once():
    assert child_sandbox.profile_deny_read("p", [VAULT, "/opt/keys//", VAULT]) == (
        VAULT,
        "/opt/keys",
    )


def test_a_key_left_out_has_no_entries():
    assert child_sandbox.profile_deny_read("p", None) == ()


def test_a_path_that_does_not_exist_is_accepted(tmp_path):
    absent = str(tmp_path.resolve() / "not-yet" / "key")
    assert child_sandbox.profile_deny_read("p", [absent]) == (absent,)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (VAULT, "must be a list of non-empty paths"),
        ([VAULT, ""], "must be a list of non-empty paths"),
        ([3], "must be a list of non-empty paths"),
        (["vault"], "'vault' must be an absolute path or start with '~/'"),
        (["./vault"], "'./vault' must be an absolute path or start with '~/'"),
        (["~/a/../b"], "'~/a/../b' must not contain '.' or '..'"),
        (["/opt/./keys"], "'/opt/./keys' must not contain '.' or '..'"),
    ],
)
def test_a_malformed_value_is_refused_naming_the_entry(value, message):
    with pytest.raises(ConfigurationError, match=re.escape(message)):
        child_sandbox.profile_deny_read("p", value)


@pytest.mark.parametrize("character", list("*?[]{}"))
def test_a_pattern_is_refused_on_every_route(character):
    with pytest.raises(ConfigurationError, match="is a pattern"):
        child_sandbox.profile_deny_read("p", [f"~/keys{character}"])


def test_an_entry_through_a_link_is_refused_with_the_spelling_to_write(tmp_path):
    real = tmp_path.resolve() / "real"
    real.mkdir()
    (tmp_path.resolve() / "link").symlink_to(real)
    with pytest.raises(ConfigurationError) as refused:
        child_sandbox.profile_deny_read("p", [str(tmp_path.resolve() / "link" / "key")])
    assert repr(str(real / "key")) in str(refused.value)
    assert child_sandbox.profile_deny_read("p", [str(real / "key")]) == (str(real / "key"),)


@pytest.mark.skipif(os.path.realpath("/tmp") == "/tmp", reason="/tmp is not a link on this host")
def test_a_tmp_entry_names_where_tmp_resolves():
    # On macOS /tmp is a link to /private/tmp, so this is the arm an ordinary entry hits.
    resolved = os.path.realpath("/tmp") + "/keys"
    with pytest.raises(ConfigurationError, match=re.escape(repr(resolved))):
        child_sandbox.profile_deny_read("p", ["/tmp/keys"])


def test_the_profile_carries_its_entries_and_reports_them():
    builder = spawn.load_agent_profile("builder")
    assert builder.deny_read == (VAULT,)
    assert "deny_read" not in builder.extra
    assert spawn.profile_config(builder)["deny_read"] == [VAULT]
    assert "deny_read" not in spawn.profile_config(spawn.load_agent_profile("plain"))


def test_a_malformed_entry_refuses_the_profile_at_load(home):
    (home / ".lionagi" / "agents" / "bad.md").write_text("---\ndeny_read:\n  - vault\n---\n")
    with pytest.raises(ConfigurationError, match="'vault' must be an absolute path"):
        spawn.load_agent_profile("bad")


def test_a_frontmatter_that_does_not_read_refuses_when_it_names_deny_read(home):
    text = "---\nname: [broken\ndeny_read:\n  - ~/vault\n---\nReview.\n"
    (home / ".lionagi" / "agents" / "broken.md").write_text(text)
    with pytest.raises(ConfigurationError, match="deny_read could not be read"):
        spawn.load_agent_profile("broken")


def test_deny_read_after_the_leading_frontmatter_refuses(home):
    text = "---\nname: late\n---\nReview.\n\n---\ndeny_read:\n  - ~/vault\n---\n"
    (home / ".lionagi" / "agents" / "late.md").write_text(text)
    with pytest.raises(ConfigurationError, match="sets deny_read outside the leading"):
        spawn.load_agent_profile("late")


def test_a_project_profile_may_add_entries(tmp_path, monkeypatch):
    project = tmp_path.resolve() / "project"
    (project / ".lionagi" / "agents").mkdir(parents=True)
    (project / ".lionagi" / "agents" / "proj.md").write_text(
        f"---\nname: proj\ndeny_read:\n  - {KEYS}\n---\n"
    )
    monkeypatch.chdir(project)
    assert KEYS in child_sandbox.resolve_child_sandbox("proj").deny_read


# --------------------------------------------------------------------------- Claude Code


def test_the_resolved_policy_keeps_the_entry_an_allow_read_names():
    plain = child_sandbox.resolve_child_sandbox("plain")
    builder = child_sandbox.resolve_child_sandbox("builder")
    # Control: the exemption lifts the settings' entry for a profile that does not deny it.
    assert VAULT not in plain.deny_read
    assert VAULT in builder.deny_read
    assert VAULT in builder.deny_edit
    rendered = child_sandbox.claude_settings_for(builder)["sandbox"]["filesystem"]["denyRead"]
    assert VAULT in rendered or os.path.expanduser(VAULT) in rendered


def test_a_profile_with_entries_is_refused_while_the_sandbox_is_off(sandbox_off):
    with pytest.raises(ConfigurationError, match="only the child sandbox holds"):
        child_sandbox.resolve_child_sandbox("builder")
    assert child_sandbox.resolve_child_sandbox("plain").enabled is False


@pytest.fixture
def claude_stubs(monkeypatch):
    monkeypatch.setattr(claude_code, "resolve_claude_cli", lambda: "claude")
    monkeypatch.setattr(
        child_sandbox, "_cli_version", lambda binary: child_sandbox.CLAUDE_CLI_FLOOR
    )
    monkeypatch.setattr(child_sandbox, "check_sandbox_backend", lambda: None)


def _claude_deny_read(request) -> list[str]:
    args = request.as_cmd_args()
    (value,) = [args[i + 1] for i, arg in enumerate(args) if arg == "--settings"]
    return json.loads(value)["sandbox"]["filesystem"]["denyRead"]


def _rendered(entry: str, rendered: list[str]) -> bool:
    return entry in rendered or os.path.expanduser(entry) in rendered


@pytest.mark.usefixtures("claude_stubs")
@pytest.mark.parametrize("workload", ["heavy", "light"])
def test_a_claude_request_denies_the_entry_over_an_exempting_policy(tmp_path, workload):
    exempting = child_sandbox.resolve_child_sandbox("plain")
    assert VAULT not in exempting.deny_read
    control = claude_code.ClaudeCodeRequest(
        prompt="hi", repo=_work(tmp_path), workload=workload, child_sandbox=exempting.to_dict()
    )
    assert not _rendered(VAULT, _claude_deny_read(control))
    request = control.model_copy(update={"profile_deny_read": (VAULT,)})
    assert _rendered(VAULT, _claude_deny_read(request))


@pytest.mark.usefixtures("claude_stubs")
def test_a_claude_request_with_no_stored_policy_denies_the_entry(tmp_path):
    request = claude_code.ClaudeCodeRequest(
        prompt="hi", repo=_work(tmp_path), profile_deny_read=(KEYS,)
    )
    assert KEYS in _claude_deny_read(request)


@pytest.mark.usefixtures("claude_stubs", "sandbox_off")
def test_a_claude_request_with_entries_is_refused_while_the_sandbox_is_off(tmp_path):
    request = claude_code.ClaudeCodeRequest(
        prompt="hi", repo=_work(tmp_path), profile_deny_read=(KEYS,)
    )
    with pytest.raises(ConfigurationError, match="only the child sandbox holds"):
        request.as_cmd_args()


# --------------------------------------------------------------------------- codex


def _codex_env(tmp_path: Path) -> dict[str, str]:
    codex_home = tmp_path.resolve() / "codex-home"
    codex_home.mkdir(exist_ok=True)
    (codex_home / "config.toml").write_text("")
    return {"CODEX_HOME": str(codex_home)}


@pytest.mark.parametrize("workload", ["heavy", "light"])
def test_the_codex_permission_profile_denies_the_entry(tmp_path, workload):
    work, env = _work(tmp_path), _codex_env(tmp_path)

    def profile_args(**fields) -> str:
        request = codex.CodexCodeRequest(
            prompt="hi", repo=work, env=env, workload=workload, **fields
        )
        plain = codex._plain_request(request)
        policy = codex._permission_profile_policy(plain)
        return " ".join(codex._permission_profile_args(plain, work, env, policy, None)), policy

    control, _ = profile_args()
    assert KEYS not in control
    rendered, policy = profile_args(profile_deny_read=[KEYS, VAULT])
    assert KEYS in rendered
    assert KEYS in policy.deny_read and KEYS in policy.deny_edit
    # An allow_read names ~/vault for a profile; codex reads no exemption, and the profile's own
    # entry keeps it denied either way. The profile expands ``~`` against the child's and the
    # account's home, so the rendered entry is matched by its last part.
    assert VAULT in policy.deny_read
    assert re.search(r'/vault" = "deny"', rendered)


def test_a_codex_request_with_entries_is_refused_while_the_sandbox_is_off(tmp_path, sandbox_off):
    request = codex.CodexCodeRequest(prompt="hi", repo=_work(tmp_path), profile_deny_read=[KEYS])
    with pytest.raises(ConfigurationError, match="only the child sandbox holds"):
        codex._permission_profile_policy(codex._plain_request(request))


def test_a_codex_request_with_entries_is_refused_under_bypass(tmp_path):
    request = codex.CodexCodeRequest(
        prompt="hi", repo=_work(tmp_path), bypass_approvals=True, profile_deny_read=[KEYS]
    )
    with pytest.raises(ConfigurationError, match="bypass_approvals turns codex's sandbox off"):
        codex._permission_profile_policy(codex._plain_request(request))


# --------------------------------------------------------------------------- Antigravity CLI


def _deny_params(argv: list[str]) -> set[str]:
    params = dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "-D")
    return {value for name, value in params.items() if name.startswith("DENY_READ_")}


def test_the_macos_sandbox_denies_the_entry_to_the_antigravity_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    keys = str(tmp_path.resolve() / "keys")
    request = gemini_code.GeminiCodeRequest(prompt="hi", repo=_work(tmp_path))
    assert keys not in _deny_params(gemini_code._confined_agy_command(AGY, request))
    request = request.model_copy(update={"profile_deny_read": [keys]})
    assert keys in _deny_params(gemini_code._confined_agy_command(AGY, request))


def test_an_antigravity_request_with_entries_is_refused_without_the_sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: False)
    request = gemini_code.GeminiCodeRequest(
        prompt="hi", repo=_work(tmp_path), profile_deny_read=[KEYS]
    )
    with pytest.raises(ConfigurationError, match="sandbox-exec"):
        gemini_code._confined_agy_command(AGY, request)


@pytest.mark.skipif(not seatbelt.seatbelt_available(), reason="needs macOS sandbox-exec")
def test_a_denied_read_fails_by_reading_beside_a_control_read_that_succeeds(tmp_path):
    # The command _confined_agy_command builds, with a shell in the CLI's place: one run reads
    # the denied file and the control file beside it.
    secrets = tmp_path.resolve() / "secrets"
    secrets.mkdir()
    (secrets / "denied").write_text("denied-content")
    (secrets / "control").write_text("control-content")
    request = gemini_code.GeminiCodeRequest(
        prompt="hi", repo=_work(tmp_path), profile_deny_read=[str(secrets / "denied")]
    )
    argv = gemini_code._confined_agy_command(AGY, request)
    prefix = argv[: argv.index(AGY)]
    script = f'cat "{secrets / "control"}"; echo; cat "{secrets / "denied"}"; echo "rc=$?"'
    result = subprocess.run(
        [*prefix, "/bin/sh", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert "control-content" in result.stdout
    assert "denied-content" not in result.stdout
    assert "Operation not permitted" in result.stderr
    assert re.search(r"rc=[1-9]", result.stdout)


# --------------------------------------------------------------------------- API-model tools


@pytest.fixture
def link_probe_passes(monkeypatch):
    monkeypatch.setattr(
        "lionhub.tools.seatbelt_link_probe.require_passing_link_record", lambda policy: None
    )


@pytest.mark.usefixtures("link_probe_passes")
def test_the_write_tool_set_denies_the_entry(tmp_path):
    from lionhub.serve.cli.agent import _make_api_agent_spec

    keys = str(tmp_path.resolve() / "keys")
    common = dict(cwd=str(_work(tmp_path)), effort=None, system_prompt=None, role="reviewer")
    control = _make_api_agent_spec(tools="write", **common)
    assert keys not in {str(p) for p in control.seatbelt.deny_read}
    spec = _make_api_agent_spec(tools="write", profile_deny_read=(keys,), **common)
    assert keys in {str(p) for p in spec.seatbelt.deny_read}
    assert spec._profile_deny_read == (keys,)


def test_the_read_tool_set_is_refused(tmp_path):
    from lionhub.serve.cli.agent import _make_api_agent_spec

    with pytest.raises(ConfigurationError, match="read tool set"):
        _make_api_agent_spec(
            cwd=str(_work(tmp_path)),
            effort=None,
            system_prompt=None,
            role="reviewer",
            tools="read",
            profile_deny_read=(KEYS,),
        )


# --------------------------------------------------------------------------- routes


def test_the_route_table():
    refusal = child_sandbox.profile_deny_read_refusal
    assert refusal("claude_code", is_cli=True) is None
    assert refusal("codex", is_cli=True) is None
    assert refusal("anthropic", is_cli=False, api_tools="write") is None
    assert "read tool set" in refusal("anthropic", is_cli=False, api_tools="read")
    # An API model whose tools were not built with the entries, as a flow's worker is not.
    assert "write tool set runs under" in refusal("anthropic", is_cli=False)
    assert "read tool set" not in refusal("anthropic", is_cli=False)
    assert "pi provider" in refusal("pi", is_cli=True)


@pytest.mark.parametrize(("available", "refused"), [(True, False), (False, True)])
def test_the_antigravity_route_follows_the_host_sandbox(monkeypatch, available, refused):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: available)
    reason = child_sandbox.profile_deny_read_refusal("gemini_code", is_cli=True)
    assert (reason is not None) is refused


def _model(provider: str, *, is_cli: bool = True):
    config = SimpleNamespace(provider=provider, kwargs={})
    return SimpleNamespace(is_cli=is_cli, endpoint=SimpleNamespace(config=config))


@pytest.mark.parametrize("provider", ["claude_code", "codex", "gemini_code"])
def test_a_cli_model_that_can_hold_the_entries_takes_them(monkeypatch, provider):
    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: True)
    model = _model(provider)
    factory._hold_profile_deny_read([model], None, "builder")
    assert model.endpoint.config.kwargs["profile_deny_read"] == [VAULT]


def test_a_profile_without_entries_hands_nothing():
    model = _model("pi")
    factory._hold_profile_deny_read([model], None, "plain")
    assert model.endpoint.config.kwargs == {}


@pytest.mark.parametrize(
    ("model", "reason"),
    [
        (_model("pi"), "pi provider has no child sandbox"),
        (_model("anthropic", is_cli=False), "tools were not given one"),
    ],
)
def test_a_model_that_cannot_hold_the_entries_refuses_the_build(model, reason):
    with pytest.raises(ConfigurationError, match=reason) as refused:
        factory._hold_profile_deny_read([model], None, "builder")
    assert "'builder' declares deny_read" in str(refused.value)
    assert model.endpoint.config.kwargs == {}


def test_an_api_model_whose_spec_holds_the_entries_is_built():
    model = _model("anthropic", is_cli=False)
    spec = SimpleNamespace(_profile_deny_read=(VAULT,))
    factory._hold_profile_deny_read([model], spec, "builder")
    assert model.endpoint.config.kwargs == {}


def test_entries_handed_in_are_refused_while_the_sandbox_is_off(sandbox_off):
    with pytest.raises(ConfigurationError, match="only the child sandbox holds"):
        factory._hold_profile_deny_read([_model("codex")], None, "orc", entries=(KEYS,))


# --------------------------------------------------------------------------- before the run


def _role(lines: str, model: str = "claude_code/sonnet") -> str:
    return f"---\nmodel: {model}\n{lines}---\nReview.\n"


def _wire_run(monkeypatch, tmp_path: Path, role_text: str) -> dict:
    import lionhub.serve.cli.agent as agent_mod

    from ..serve.cli.test_agent_resume_model_override import _wire_agent_stubs

    _wire_agent_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: SETTINGS)
    profile = spawn._parse_profile("role", role_text)
    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
    state: dict = {"allocated": 0, "run_dir": tmp_path / "runs" / "r"}

    def allocate():
        state["allocated"] += 1
        state["run_dir"].mkdir(parents=True)
        return SimpleNamespace(
            run_id="r",
            state_root=state["run_dir"],
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
            write_manifest=lambda data: None,
        )

    monkeypatch.setattr(agent_mod, "allocate_run", allocate)
    return state


_DENY = f"deny_read:\n  - {KEYS}\n"


@pytest.mark.asyncio
async def test_li_agent_hands_the_entries_to_the_cli_model(monkeypatch, tmp_path):
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch

    state = _wire_run(monkeypatch, tmp_path, _role(_DENY))
    seen: dict = {}

    async def fake_operate(self, instruction=None, **kw):
        seen["kwargs"] = dict(self.chat_model.endpoint.config.kwargs)
        return "ok"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    await agent_mod._run_agent(None, "review", agent_name="role")
    assert state["allocated"] == 1
    assert seen["kwargs"]["profile_deny_read"] == [KEYS]


@pytest.mark.asyncio
async def test_li_agent_refuses_a_route_that_cannot_hold_the_entries_before_the_run(
    monkeypatch, tmp_path
):
    import lionhub.serve.cli.agent as agent_mod

    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: False)
    state = _wire_run(monkeypatch, tmp_path, _role(_DENY, model="gemini-cli/gemini-2.5-pro"))
    with pytest.raises(ConfigurationError, match="sandbox-exec") as refused:
        await agent_mod._run_agent(None, "review", agent_name="role")
    assert "'role' declares deny_read" in str(refused.value)
    assert state["allocated"] == 0


@pytest.mark.asyncio
async def test_li_agent_refuses_the_entries_while_the_sandbox_is_off_before_the_run(
    monkeypatch, tmp_path
):
    import lionhub.serve.cli.agent as agent_mod

    state = _wire_run(monkeypatch, tmp_path, _role(_DENY))
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: OFF)
    with pytest.raises(ConfigurationError, match="only the child sandbox holds"):
        await agent_mod._run_agent(None, "review", agent_name="role")
    assert state["allocated"] == 0


@pytest.mark.asyncio
async def test_an_orchestrator_whose_model_cannot_hold_the_entries_is_refused_before_its_run(
    monkeypatch, tmp_path
):
    import lionhub.serve.cli.orchestrate._orchestration as orch_mod

    monkeypatch.setattr(seatbelt, "seatbelt_available", lambda: False)
    profile = spawn._parse_profile("orc", _role(_DENY, model="gemini-cli/gemini-2.5-pro"))
    allocated: list = []
    monkeypatch.setattr(orch_mod, "load_agent_profile", lambda name, *a, **kw: profile)
    monkeypatch.setattr(orch_mod, "allocate_run", lambda *a, **kw: allocated.append(1))
    with pytest.raises(ConfigurationError, match="'orc' declares deny_read"):
        await orch_mod.setup_orchestration(
            pattern_name="Fanout",
            model_spec=None,
            agent_name="orc",
            save_dir=tmp_path / "runs",
            cwd=str(tmp_path),
            yolo=False,
            verbose=False,
            effort=None,
            theme=None,
            no_mcp_config=True,
        )
    assert allocated == []


def _worker_profile(entries: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        name="reader",
        raw_body=True,
        system_prompt="Read and report.",
        effort=None,
        yolo=False,
        fast_mode=False,
        khive_injection=None,
        workload="light",
        deny_read=entries,
    )


def test_a_worker_whose_model_cannot_hold_the_entries_is_refused_when_built(tmp_path):
    from ..serve.cli.orchestrate.test_worker_artifact_directive import _build_worker

    worker = {"agent_id": "reader", "role": "reader", "bare": False, "is_cli": False}
    # Control: the same worker on the same API model builds when its role declares nothing.
    _env, _imodel, built = _build_worker(
        tmp_path, profile=_worker_profile(()), provider="anthropic", **worker
    )
    assert "imodel_kwargs" in built
    with pytest.raises(ConfigurationError, match="'reader' declares deny_read") as refused:
        _build_worker(tmp_path, profile=_worker_profile((KEYS,)), provider="anthropic", **worker)
    assert "tools were not given one" in str(refused.value)


# --------------------------------------------------------------------------- engine stages


# A stage role must be one AgentSpec composes, so these use built-in role names, each given a
# profile file of its own.


def _stage_profile(home: Path, role: str, lines: str = "") -> None:
    (home / ".lionagi" / "agents" / f"{role}.md").write_text(f"---\nname: {role}\n{lines}---\n")


@pytest.mark.asyncio
async def test_an_engine_stage_whose_role_declares_entries_is_refused_when_built(home):
    from lionhub.orchestration.engines.engine import Engine

    _stage_profile(home, "critic", _DENY)
    run = Engine().new_run()
    with pytest.raises(ConfigurationError, match="'critic' declares deny_read") as refused:
        await run.make_agent("critic", name="c1")
    assert "engine stage takes only the model and effort" in str(refused.value)
    assert run.agents_made == 0


@pytest.mark.asyncio
async def test_an_engine_stage_whose_role_declares_no_entries_is_built(home):
    from lionhub.orchestration.engines.engine import Engine

    _stage_profile(home, "researcher")
    run = Engine().new_run()
    branch = await run.make_agent("researcher", name="r1")
    assert branch.name == "r1"
    assert run.agents_made == 1


@pytest.mark.asyncio
async def test_an_engine_stage_whose_role_has_a_malformed_deny_read_is_refused(home):
    from lionhub.orchestration.engines.engine import Engine

    _stage_profile(home, "analyst", "deny_read:\n  - relative/keys\n")
    run = Engine().new_run()
    with pytest.raises(ConfigurationError, match="must be an absolute path"):
        await run.make_agent("analyst", name="a1")
    assert run.agents_made == 0


#: A setting the loader refuses for a reason of its own, which never names deny_read.
_BAD_MCP = "mcp: everything\n"


def _load_failure(role: str) -> str:
    with pytest.raises(Exception) as failed:
        spawn.load_agent_profile(role)
    assert not isinstance(failed.value, FileNotFoundError)
    return str(failed.value)


@pytest.mark.asyncio
async def test_an_engine_stage_whose_role_fails_to_load_and_names_entries_is_refused(home):
    from lionhub.orchestration.engines.engine import Engine

    _stage_profile(home, "critic", _DENY + _BAD_MCP)
    # The load fails on the other setting, and its reason does not name the key.
    assert "deny_read" not in _load_failure("critic")
    run = Engine().new_run()
    with pytest.raises(ConfigurationError, match="'critic' did not load") as refused:
        await run.make_agent("critic", name="c1")
    assert "critic.md names deny_read" in str(refused.value)
    assert run.agents_made == 0


@pytest.mark.asyncio
async def test_an_engine_stage_whose_role_fails_to_load_without_naming_entries_is_built(home):
    from lionhub.orchestration.engines.engine import Engine

    # Control: the same file without deny_read runs on its other settings, as before.
    _stage_profile(home, "critic", _BAD_MCP)
    _load_failure("critic")
    run = Engine().new_run()
    branch = await run.make_agent("critic", name="c1")
    assert branch.name == "c1"
    assert run.agents_made == 1


@pytest.mark.asyncio
async def test_an_engine_stage_whose_role_is_a_link_to_an_unreadable_target_is_refused(home):
    from lionhub.orchestration.engines.engine import Engine

    (home / ".lionagi" / "agents" / "critic.md").symlink_to(home / "gone.md")
    run = Engine().new_run()
    with pytest.raises(ConfigurationError, match="symlink whose target cannot be read"):
        await run.make_agent("critic", name="c1")
    assert run.agents_made == 0


@pytest.mark.asyncio
async def test_an_engine_stage_whose_role_has_no_profile_is_built(home):
    from lionhub.orchestration.engines.engine import Engine

    run = Engine().new_run()
    branch = await run.make_agent("critic", name="c1")
    assert branch.name == "c1"
    assert run.agents_made == 1


def test_the_files_searched_are_the_ones_the_loader_reads(home):
    agents = home / ".lionagi" / "agents"
    names = spawn._profile_files_naming_deny_read
    assert names("builder") == (f"{agents / 'builder.md'} names deny_read",)
    assert names("plain") == ()
    assert names("absent") == ()
    # Two spellings of a name the loader refuses to choose between: each is searched.
    (agents / "a_b_c.md").write_text(f"---\nname: a_b_c\n{_DENY}---\n")
    (agents / "a-b-c.md").write_text("---\nname: a-b-c\n---\n")
    with pytest.raises(ValueError, match="ambiguous"):
        spawn.load_agent_profile("a-b_c")
    assert names("a-b_c") == (f"{agents / 'a_b_c.md'} names deny_read",)
    # A file the loader finds and cannot read: its entries cannot be ruled out.
    (agents / "locked.md").write_text("---\nname: locked\n---\n")
    (agents / "locked.md").chmod(0)
    try:
        if os.access(agents / "locked.md", os.R_OK):
            pytest.skip("this user reads a file with no permissions")
        (found,) = names("locked")
        assert found.startswith(f"{agents / 'locked.md'} cannot be read")
    finally:
        (agents / "locked.md").chmod(0o644)
