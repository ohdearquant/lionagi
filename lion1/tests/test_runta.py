"""The Runta box in the hub. `runta` (the SDK) and `httpx` are stubbed as modules in `sys.modules`,
never installed: the suite passes with neither present, as it does where neither is installed. A
stub runtime that genuinely executes runs commands on this machine, under a shim `timeout` on PATH
(coreutils is not on every machine), as the existing `daytona` stub does; a stub that only records a
call is used where the exact argv or the exact SDK timeout matters more than a real process. The
real service is exercised once end to end with `LION_RUNTA_E2E=1`."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from hub import BoxTree, RuntaSandbox, code, shell
from hub.tools import SandboxDead
from hub.tools import runta as runta_mod
from lionagi import Actor, Kind, Profile, RunRequest
from lionagi.actor import DEFAULT_HANDLERS
from tests.test_actor import scripted

Egress = runta_mod.Egress
EgressSecret = runta_mod.EgressSecret
SecretInjection = runta_mod.SecretInjection


@pytest.fixture(autouse=True)
def _runta_env(request, monkeypatch):
    """A token and endpoint for every test: the handful of calls that build their own `RuntaApi`
    with none passed in (`events`, `token_savings`, `tool_calls`, `find_image`, `build_image`,
    `RuntaSandbox.start`) need one. A test of `token()`/`endpoint()` itself overrides this. The
    real-runtime test keeps the real environment."""
    if request.function is test_a_real_runtime_runs_the_shell_and_code_tools_proves_egress_and_stops:
        return
    monkeypatch.setenv("RUNTA_TOKEN", "test-token")
    monkeypatch.setenv("RUNTA_ENDPOINT", "http://api.test")


# --- shared doubles ----------------------------------------------------------------------------


class _ApiError(Exception):
    def __init__(self, status_code: int | None = None, code: str | None = None):
        super().__init__(f"runta ApiError {status_code} {code}")
        self.status_code, self.code = status_code, code


class _InvalidStatus(Exception):
    """What the exec channel raises for a runtime that is gone: a refused websocket, not an API error."""


class _EgressPolicySDK:
    def __init__(self, *, mode: str, allowed_hosts: list[str], denied_hosts: list[str]):
        self.mode, self.allowed_hosts, self.denied_hosts = mode, list(allowed_hosts), list(denied_hosts)

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "allowed_hosts": self.allowed_hosts, "denied_hosts": self.denied_hosts}


def install_runta_minimal(monkeypatch) -> types.ModuleType:
    """A `runta` module with just `ApiError` and `EgressPolicy`: enough for a test whose runtime is
    its own hand-rolled double, not the SDK's own shapes."""
    mod = types.ModuleType("runta")
    mod.ApiError = _ApiError
    mod.EgressPolicy = _EgressPolicySDK
    monkeypatch.setitem(sys.modules, "runta", mod)
    return mod


def stub_runta(root: Path, *, fail_wait: BaseException | None = None) -> types.ModuleType:
    """A `runta` module whose one runtime executes commands on this machine: a box whose `workdir`
    is a real path under `root` runs genuinely there, and `timeout -s KILL <s> argv` is a shim
    ending a command with rc 137, as the service's own does. `fail_wait`, when given, is raised by
    every runtime's `wait_until_ready`. `preset_policy[rt_id]` seeds a runtime's reported egress
    policy before any `set_egress`; `runtimes_store` and `async_runta_instances` let a test reach
    back into what the SDK made."""
    shim = root / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "timeout").write_text(
        f"#!{sys.executable}\nimport subprocess, sys\n"
        "try:\n    sys.exit(subprocess.run(sys.argv[4:], timeout=float(sys.argv[3])).returncode)\n"
        "except subprocess.TimeoutExpired:\n    sys.exit(137)\n"
    )
    (shim / "timeout").chmod((shim / "timeout").stat().st_mode | stat.S_IEXEC)
    (root / "tmp").mkdir(exist_ok=True)

    def local(path: str) -> Path:
        return root / "tmp" / Path(path).name if path.startswith("/tmp/lion-") else Path(path)

    runtimes_store: dict[str, Any] = {}
    preset_policy: dict[str, Any] = {}
    delete_fails: dict[str, BaseException] = {}  # a runtime whose `delete` raises this
    refresh_fails: dict[str, BaseException] = {}  # a runtime whose `refresh_info` raises this
    async_runta_instances: list[Any] = []

    class Files:
        async def read(self, path):
            p = local(path)
            if not p.exists():
                raise _ApiError(status_code=404)
            return p.read_bytes()

        async def write(self, path, data):
            p = local(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

    class EgressApi:
        def __init__(self, rt):
            self._rt = rt

        async def set_policy(self, policy):
            self._rt.set_policy_calls += 1
            self._rt.policy = policy
            return types.SimpleNamespace(egress_policy=policy)

    class Checkpoints:
        def __init__(self, rt):
            self._rt = rt

        async def create(self, *, name=None, timeout=600.0):
            self._rt.checkpoint_names.append(name)
            return types.SimpleNamespace(
                id=f"ckpt-{len(self._rt.checkpoint_names)}", display_name=name or "", state="ready"
            )

    class Runtime:
        def __init__(self, rt_id):
            self.id = rt_id
            self.policy = preset_policy.get(rt_id)
            self.deleted = self.paused = False
            self.set_policy_calls = 0
            self.checkpoint_names: list[str | None] = []
            self.files = Files()
            self.egress = EgressApi(self)
            self.checkpoints = Checkpoints(self)
            self._status = "running"

        async def exec(self, cmd, *, timeout=None, env=None, stdin=None):
            # `push`'s archive lives at a literal /tmp path, outside the box's own workdir tree;
            # redirected under `root` here, as the existing `daytona` stub redirects it too
            cmd = [a.replace("/tmp/lion-", f"{root}/tmp/lion-") if isinstance(a, str) else a for a in cmd]
            merged = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", **(env or {})}
            r = subprocess.run(cmd, capture_output=True, env=merged, input=stdin)
            return types.SimpleNamespace(exit_code=r.returncode, stdout=r.stdout, stderr=r.stderr)

        async def wait_until_ready(self, timeout=600.0):
            if fail_wait is not None:
                raise fail_wait

        async def status(self):
            return types.SimpleNamespace(value=self._status)

        async def refresh_info(self):
            if self.id in refresh_fails:
                raise refresh_fails[self.id]
            return types.SimpleNamespace(egress_policy=self.policy)

        async def pause(self):
            self.paused = True

        async def resume(self, timeout=300.0):
            self.paused = False

        async def delete(self):
            if self.id in delete_fails:
                raise delete_fails[self.id]
            self.deleted = True

    class Runtimes:
        async def get(self, rt_id):
            if rt_id not in runtimes_store:
                runtimes_store[rt_id] = Runtime(rt_id)
            return runtimes_store[rt_id]

    class AsyncRunta:
        def __init__(self, *, token, endpoint):
            self.token, self.endpoint = token, endpoint
            self.runtimes = Runtimes()
            self.closed = False
            async_runta_instances.append(self)

        async def close(self):
            self.closed = True

    mod = types.ModuleType("runta")
    mod.AsyncRunta = AsyncRunta
    mod.ApiError = _ApiError
    mod.EgressPolicy = _EgressPolicySDK
    mod.runtimes_store = runtimes_store
    mod.preset_policy = preset_policy
    mod.delete_fails = delete_fails
    mod.refresh_fails = refresh_fails
    mod.async_runta_instances = async_runta_instances
    return mod


class Resp:
    """One `httpx` response: a JSON body (or `text`, raw and possibly not JSON)."""

    def __init__(self, status_code: int, body: Any = None, *, text: str | None = None):
        self.status_code = status_code
        self.content = text.encode() if text is not None else (json.dumps(body).encode() if body else b"")

    def json(self) -> Any:
        return json.loads(self.content)

    @property
    def text(self) -> str:
        return self.content.decode(errors="replace")


def install_httpx(monkeypatch, queue: list) -> tuple[list[dict], list[Any], type[Exception]]:
    """An `httpx` module whose `AsyncClient.request` answers from `queue`, in order (an exception
    instance is raised rather than returned); records every call and every client made."""
    calls: list[dict] = []
    clients: list[Any] = []

    class TransportError(Exception):
        pass

    class AsyncClient:
        def __init__(self, *, base_url, headers, timeout):
            self.base_url, self.headers, self.timeout = base_url, headers, timeout
            self.closed = False
            clients.append(self)

        async def request(self, method, path, *, json=None, params=None, headers=None):
            # a snapshot, not the live object: `pages()` mutates its `params` dict between calls
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "json": dict(json) if isinstance(json, dict) else json,
                    "params": dict(params) if isinstance(params, dict) else params,
                    "headers": dict(headers) if headers else {},
                }
            )
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        async def aclose(self):
            self.closed = True

    mod = types.ModuleType("httpx")
    mod.AsyncClient = AsyncClient
    mod.TransportError = TransportError
    monkeypatch.setitem(sys.modules, "httpx", mod)
    return calls, clients, TransportError


async def fast_sleep(_seconds: float) -> None:
    return None


class RecordingRuntime:
    """A runtime double that only records what `exec` was handed and answers one scripted result,
    after `delay` seconds of real (but tiny) waiting: for pinning the argv and the SDK timeout
    `RuntaSandbox.exec` builds, and for landing its elapsed time on either side of a deadline,
    without mocking the clock asyncio's own loop reads too."""

    def __init__(self, *, exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"", delay: float = 0.0):
        self.calls: list[dict] = []
        self.exit_code, self.stdout, self.stderr, self.delay = exit_code, stdout, stderr, delay

    async def exec(self, cmd, *, timeout, env, stdin):
        self.calls.append({"cmd": cmd, "timeout": timeout, "env": env, "stdin": stdin})
        if self.delay:
            await asyncio.sleep(self.delay)
        return types.SimpleNamespace(exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr)

    async def status(self):
        return types.SimpleNamespace(value="running")


def _ready_box(
    monkeypatch,
    tmp_path,
    *,
    egress=None,
    fail_wait=None,
    post=None,
    rt_id="rt-1",
    from_checkpoint=None,
    precreate_workdir=True,
):
    """A `RuntaSandbox` wired to `stub_runta` and a one-response `httpx` queue, ready for `start()`."""
    monkeypatch.setenv("RUNTA_TOKEN", "tok")
    monkeypatch.setenv("RUNTA_ENDPOINT", "http://api.test")
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    mod = stub_runta(tmp_path, fail_wait=fail_wait)
    monkeypatch.setitem(sys.modules, "runta", mod)
    queue = [post or Resp(200, {"data": {"id": rt_id}})]
    calls, clients, _ = install_httpx(monkeypatch, queue)
    work = tmp_path / "work"
    if precreate_workdir:
        work.mkdir(exist_ok=True)
    box = RuntaSandbox(image="img", workdir=str(work), egress=egress, from_checkpoint=from_checkpoint)
    return box, mod, calls, clients


# --- 1. token() and endpoint() -------------------------------------------------------------------


def test_token_and_endpoint_read_the_environment_then_the_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNTA_TOKEN", raising=False)
    monkeypatch.delenv("RUNTA_ENDPOINT", raising=False)
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("RUNTA_CONFIG", str(cfg))
    with pytest.raises(RuntimeError, match="RUNTA_TOKEN"):
        runta_mod.token()
    assert runta_mod.endpoint() == runta_mod.ENDPOINT  # neither set: the public default
    cfg.write_text('token = "cfg-token"\nendpoint = "https://cfg.example/"\n')
    assert runta_mod.token() == "cfg-token"
    assert runta_mod.endpoint() == "https://cfg.example"  # a trailing slash is dropped
    monkeypatch.setenv("RUNTA_TOKEN", "env-token")
    assert runta_mod.token() == "env-token"  # the environment wins over the config file
    monkeypatch.setenv("RUNTA_ENDPOINT", "https://env.example/")
    assert runta_mod.endpoint() == "https://env.example"


def test_a_default_run_test_holds_the_test_credential():
    # the probe of the test below, which runs it where the environment holds another one
    assert (runta_mod.token(), runta_mod.endpoint()) == ("test-token", "http://api.test")


def test_the_fixture_keeps_a_credential_from_the_environment_out_of_a_default_run(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('token = "config-credential"\nendpoint = "http://config.invalid"\n')
    env = {k: v for k, v in os.environ.items() if k not in ("LION_RUNTA_E2E", "FORCE_COLOR")}
    env |= {"RUNTA_TOKEN": "env-credential", "RUNTA_ENDPOINT": "http://env.invalid"}
    env |= {"RUNTA_CONFIG": str(config), "PY_COLORS": "0"}
    probe = f"{Path(__file__).resolve()}::test_a_default_run_test_holds_the_test_credential"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", probe, "-q"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0 and "1 passed" in done.stdout, done.stdout[-1500:] + done.stderr[-500:]


# --- 2. Egress ------------------------------------------------------------------------------------


def test_egress_api_shape_and_from_api_roundtrip():
    allow = Egress(mode="allowlist", hosts=("example.com", "a-b.example.co.uk"))
    # the service refuses the other mode's list as an unknown field, so only the mode's own is sent
    assert allow.api() == {"mode": "allowlist", "allowed_hosts": ["example.com", "a-b.example.co.uk"]}
    deny = Egress(mode="denylist", hosts=("bad.com",))
    assert deny.api() == {"mode": "denylist", "denied_hosts": ["bad.com"]}
    assert Egress.from_api(None) is None
    assert Egress.from_api(allow.api()) == allow
    assert Egress.from_api(deny.api()) == deny


BAD_HOSTS = {
    "a scheme": "https://example.com",
    "a port": "example.com:8080",
    "a path": "example.com/path",
    "a wildcard": "*.example.com",
    "uppercase": "EXAMPLE.com",
    "an empty label": "",
    "over 253 chars": ".".join(["ab"] * 100),
}


@pytest.mark.parametrize("host", list(BAD_HOSTS.values()), ids=list(BAD_HOSTS))
def test_egress_refuses_anything_but_a_bare_lowercase_host_name(host):
    with pytest.raises(ValidationError):
        Egress(mode="allowlist", hosts=(host,))


# --- 3. SecretInjection ----------------------------------------------------------------------------


def test_secret_injection_sections_are_absent_when_empty_and_carry_no_bare_value_key():
    bare = SecretInjection(secret_id="sec1")
    assert bare.api() == {"secret_id": "sec1"}
    full = SecretInjection(
        secret_id="sec1",
        env={"API_KEY": "{{secret}}"},
        egress=(
            EgressSecret(host="api.example.com", name="X-Key", template="{{secret}}"),
            EgressSecret(
                host="api2.example.com", name="k", template="{{secret}}", action="query", path="/v1/*"
            ),
        ),
        files={"home/.netrc": "{{secret}}"},
    )
    api = full.api()
    assert api["secret_id"] == "sec1"
    assert api["environment"] == [{"name": "API_KEY", "value_template": "{{secret}}"}]
    assert api["egress_rules"][0] == {
        "host_pattern": "api.example.com",
        "action": "header",
        "name": "X-Key",
        "value_template": "{{secret}}",
    }
    assert "path_pattern" not in api["egress_rules"][0]
    assert api["egress_rules"][1]["path_pattern"] == "/v1/*"
    assert api["files"] == [{"path": "home/.netrc", "value_template": "{{secret}}"}]

    def no_bare_value_key(obj):
        if isinstance(obj, dict):
            assert "value" not in obj
            for v in obj.values():
                no_bare_value_key(v)
        elif isinstance(obj, list):
            for v in obj:
                no_bare_value_key(v)

    no_bare_value_key(api)


# --- 4. as_event -----------------------------------------------------------------------------------


REC = {
    "kind": "started",
    "occurred_at_unix_millis": 1234567890123,
    "id": "e1",
    "runtime_id": "r1",
    "payload": {"a": 1},
}


def test_as_event_shapes_kind_payload_and_at_and_refuses_a_bad_record():
    ev = runta_mod.as_event(REC)
    assert ev.kind == "runta.started"
    assert ev.payload == {"id": "e1", "runtime_id": "r1", "payload": {"a": 1}}
    assert ev.at == pytest.approx(1234567890.123)
    for missing in ("kind", "occurred_at_unix_millis", "id", "runtime_id", "payload"):
        bad = {k: v for k, v in REC.items() if k != missing}
        with pytest.raises(ValueError, match=missing):
            runta_mod.as_event(bad)
    with pytest.raises(ValueError):
        runta_mod.as_event({**REC, "kind": 123})
    with pytest.raises(ValueError):
        runta_mod.as_event({**REC, "occurred_at_unix_millis": "123"})


# --- 5. RuntaApi.call --------------------------------------------------------------------------------


def test_call_returns_the_data_member_whether_the_body_is_a_dict_or_not(monkeypatch):
    # a fresh client per case: `RuntaApi` binds the `httpx` module's client once, at construction
    install_httpx(monkeypatch, [Resp(200, {"data": {"ok": True}})])
    assert asyncio.run(runta_mod.RuntaApi(tok="t", base="http://x").call("GET", "/v2/x")) == {"ok": True}
    install_httpx(monkeypatch, [Resp(200, [1, 2, 3])])
    assert asyncio.run(runta_mod.RuntaApi(tok="t", base="http://x").call("GET", "/v2/x")) == [1, 2, 3]
    install_httpx(monkeypatch, [Resp(200, {"no_data_key": 1})])
    got = asyncio.run(runta_mod.RuntaApi(tok="t", base="http://x").call("GET", "/v2/x"))
    assert got == {"no_data_key": 1}


def test_post_carries_an_idempotency_key_and_get_does_not(monkeypatch):
    calls, _, _ = install_httpx(monkeypatch, [Resp(200, {"data": {}}), Resp(200, {"data": {}})])
    api = runta_mod.RuntaApi(tok="t", base="http://x")
    asyncio.run(api.call("POST", "/v2/x", json={"a": 1}))
    asyncio.run(api.call("GET", "/v2/x"))
    assert "idempotency-key" in calls[0]["headers"]
    assert calls[1]["headers"] == {}


def test_429_then_200_is_retried_with_the_same_idempotency_key(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    calls, _, _ = install_httpx(monkeypatch, [Resp(429, {"error": {}}), Resp(200, {"data": {"ok": True}})])
    api = runta_mod.RuntaApi(tok="t", base="http://x")
    assert asyncio.run(api.call("POST", "/v2/x", json={})) == {"ok": True}
    assert len(calls) == 2
    assert calls[0]["headers"]["idempotency-key"] == calls[1]["headers"]["idempotency-key"]


def test_400_raises_at_once_with_status_code_and_message(monkeypatch):
    calls, _, _ = install_httpx(
        monkeypatch, [Resp(400, {"error": {"code": "bad_request", "message": "nope"}})]
    )
    api = runta_mod.RuntaApi(tok="t", base="http://x")
    with pytest.raises(runta_mod.RuntaError) as e:
        asyncio.run(api.call("POST", "/v2/x", json={}))
    assert (e.value.status, e.value.code) == (400, "bad_request")
    assert "nope" in str(e.value)
    assert len(calls) == 1


def test_5xx_on_every_attempt_raises_after_attempts_requests(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    calls, _, _ = install_httpx(monkeypatch, [Resp(503, {"error": {}})] * 3)
    api = runta_mod.RuntaApi(tok="t", base="http://x", attempts=3)
    with pytest.raises(runta_mod.RuntaError) as e:
        asyncio.run(api.call("GET", "/v2/x"))
    assert e.value.status == 503 and len(calls) == 3


def test_a_transport_error_is_retried_and_raised_on_the_last_attempt(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    queue: list = []
    calls, _, TransportError = install_httpx(monkeypatch, queue)
    queue += [TransportError("boom"), TransportError("boom again")]
    api = runta_mod.RuntaApi(tok="t", base="http://x", attempts=2)
    with pytest.raises(TransportError, match="boom again"):
        asyncio.run(api.call("GET", "/v2/x"))
    assert len(calls) == 2


def test_an_error_body_that_is_not_json_still_raises_runta_error(monkeypatch):
    calls, _, _ = install_httpx(monkeypatch, [Resp(502, text="upstream exploded, not json")])
    api = runta_mod.RuntaApi(tok="t", base="http://x", attempts=1)
    with pytest.raises(runta_mod.RuntaError, match="upstream exploded"):
        asyncio.run(api.call("GET", "/v2/x"))


# --- 6. RuntaApi.pages -------------------------------------------------------------------------------


def test_pages_follows_next_page_token_drops_none_params_and_merges_the_last_pages_rest(monkeypatch):
    queue = [
        Resp(200, {"data": {"events": [{"id": "a"}], "next_page_token": "p2", "summary": {"n": 1}}}),
        Resp(200, {"data": {"events": [{"id": "b"}], "summary": {"n": 2}}}),
    ]
    calls, _, _ = install_httpx(monkeypatch, queue)
    api = runta_mod.RuntaApi(tok="t", base="http://x")
    rows, rest = asyncio.run(api.pages("/v2/events", "events", {"runtime_id": "r1", "kind": None}))
    assert rows == [{"id": "a"}, {"id": "b"}]
    assert rest == {"summary": {"n": 2}}
    assert calls[0]["params"] == {"runtime_id": "r1"}
    assert calls[1]["params"] == {"runtime_id": "r1", "page_token": "p2"}


# --- 7. events / token_savings / tool_calls -----------------------------------------------------


def test_events_returns_typed_bus_events_matching_as_event(monkeypatch):
    queue = [Resp(200, {"data": {"events": [REC]}})]
    install_httpx(monkeypatch, queue)
    evs = asyncio.run(runta_mod.events("r1"))
    assert evs == [runta_mod.as_event(REC)]


def test_a_client_events_makes_itself_is_closed_a_client_passed_in_is_not(monkeypatch):
    calls, clients, _ = install_httpx(monkeypatch, [Resp(200, {"data": {"events": [REC]}})])
    asyncio.run(runta_mod.events("r1"))
    assert len(clients) == 1 and clients[0].closed is True

    calls2, clients2, _ = install_httpx(monkeypatch, [Resp(200, {"data": {"events": [REC]}})])
    api = runta_mod.RuntaApi(tok="t", base="http://x")
    asyncio.run(runta_mod.events("r1", api=api))
    assert clients2[0].closed is False


def _install_token_savings_response(monkeypatch) -> dict:
    saving = {
        "id": "s1",
        "event_id": "e1",
        "runtime_id": "r1",
        "observed_at_unix_millis": 1,
        "wire_bytes_before": 100,
        "wire_bytes_after": 50,
        "saved_wire_bytes": 50,
        "estimated_tokens_before": 10,
        "estimated_tokens_after": 5,
        "saved_estimated_tokens": 5,
    }
    install_httpx(monkeypatch, [Resp(200, {"data": {"events": [saving], "summary": {"saved": 5}}})])
    return saving


def test_token_savings_returns_a_typed_summary_and_typed_events(monkeypatch):
    saving = _install_token_savings_response(monkeypatch)
    ts = asyncio.run(runta_mod.token_savings("r1"))
    assert isinstance(ts, runta_mod.TokenSavings) and ts.summary == {"saved": 5}
    assert isinstance(ts.events[0], runta_mod.TokenSaving) and ts.events[0].id == saving["id"]


def test_tool_calls_returns_a_typed_summary_with_nested_findings(monkeypatch):
    call_rec = {
        "id": "c1",
        "event_id": "e1",
        "runtime_id": "r1",
        "observed_at_unix_millis": 1,
        "input_chars": 10,
        "output_chars": 20,
        "estimated_input_tokens": 1,
        "estimated_output_tokens": 2,
        "estimated_savings_tokens": 0,
        "truncated": False,
        "findings": [
            {
                "kind": "large_output",
                "severity": "low",
                "confidence": "high",
                "title": "t",
                "detail": "d",
                "estimated_savings_tokens": 1,
            }
        ],
    }
    install_httpx(monkeypatch, [Resp(200, {"data": {"calls": [call_rec], "summary": {"n": 1}}})])
    tc = asyncio.run(runta_mod.tool_calls("r1"))
    assert isinstance(tc, runta_mod.ToolCalls) and tc.summary == {"n": 1}
    assert isinstance(tc.calls[0], runta_mod.ToolCall)
    assert isinstance(tc.calls[0].findings[0], runta_mod.ToolCallFinding)


# --- 8. find_image -----------------------------------------------------------------------------------


BUILDS = [
    {"name": "img-a", "status": "succeeded", "updated_at": "2026-01-01", "runtime_image_id": "old"},
    {"name": "img-a", "status": "succeeded", "updated_at": "2026-02-01", "runtime_image_id": "new"},
    {"name": "img-a", "status": "failed", "updated_at": "2026-03-01", "runtime_image_id": "nope"},
    {"name": "img-b", "status": "succeeded", "updated_at": "2026-04-01", "runtime_image_id": "other"},
]


def test_find_image_picks_the_newest_succeeded_build_of_that_name(monkeypatch):
    install_httpx(monkeypatch, [Resp(200, {"data": BUILDS})])
    assert asyncio.run(runta_mod.find_image("img-a")) == "new"
    install_httpx(monkeypatch, [Resp(200, {"data": {"builds": BUILDS}})])
    assert asyncio.run(runta_mod.find_image("img-a")) == "new"
    install_httpx(monkeypatch, [Resp(200, {"data": []})])
    assert asyncio.run(runta_mod.find_image("img-missing")) is None


# --- 9. build_image ----------------------------------------------------------------------------------


def _fast_clock(monkeypatch) -> list[float]:
    """A monotonic clock a test drives by hand, and an `asyncio.sleep` that advances it instead of
    waiting: `build_image`'s poll loop is timed without any real delay."""
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return now


def test_build_image_polls_until_succeeded_and_returns_the_runtime_image_id(monkeypatch):
    _fast_clock(monkeypatch)
    queue = [
        Resp(200, {"data": {"id": "b1", "status": "pending"}}),
        Resp(200, {"data": {"id": "b1", "status": "building"}}),
        Resp(200, {"data": {"id": "b1", "status": "succeeded", "runtime_image_id": "img-x"}}),
    ]
    calls, _, _ = install_httpx(monkeypatch, queue)
    rid = asyncio.run(runta_mod.build_image("n", "FROM x\n", timeout=1000.0))
    assert rid == "img-x"
    assert calls[0]["method"] == "POST" and calls[0]["path"] == "/v2/images/builds"
    assert calls[0]["json"] == {"name": "n", "dockerfile": "FROM x\n", "architecture": "x86_64"}


def test_build_image_ending_failed_raises_runtime_error(monkeypatch):
    _fast_clock(monkeypatch)
    queue = [
        Resp(200, {"data": {"id": "b2", "status": "pending"}}),
        Resp(200, {"data": {"id": "b2", "status": "failed"}}),
    ]
    install_httpx(monkeypatch, queue)
    with pytest.raises(RuntimeError, match="b2 ended failed"):
        asyncio.run(runta_mod.build_image("n", "FROM x\n", timeout=1000.0))


def test_build_image_past_the_timeout_raises_with_no_real_waiting(monkeypatch):
    _fast_clock(monkeypatch)
    queue = [
        Resp(200, {"data": {"id": "b3", "status": "pending"}}),
        Resp(200, {"data": {"id": "b3", "status": "building"}}),
        Resp(200, {"data": {"id": "b3", "status": "building"}}),
    ]
    install_httpx(monkeypatch, queue)
    t0 = time.perf_counter()
    with pytest.raises(TimeoutError, match="b3"):
        asyncio.run(runta_mod.build_image("n", "FROM x\n", timeout=20.0))
    assert time.perf_counter() - t0 < 2  # the wait was simulated, never real


# --- 10. RuntaSandbox.start ---------------------------------------------------------------------


def test_the_create_body_shapes_resources_image_egress_secrets_and_capture():
    box = RuntaSandbox(
        image="img-1",
        cpu=4,
        memory=3,
        name="n1",
        egress=Egress(mode="allowlist", hosts=("a.com",)),
        secrets=(SecretInjection(secret_id="s1"),),
        capture=True,
    )
    body = box._body()
    assert body["name"] == "n1" and body["llm_tool_io_capture_enabled"] is True
    assert body["resources"] == {"requests": {"vcpus": 4, "memory_mib": 3 * 1024}}
    assert body["image"] == {"id": "img-1"}
    assert body["egress_policy"] == {"mode": "allowlist", "allowed_hosts": ["a.com"]}
    assert body["secret_configuration"] == [{"secret_id": "s1"}]
    assert "checkpoint_id" not in body


def test_from_checkpoint_sends_checkpoint_id_and_no_resources_or_image():
    body = RuntaSandbox(from_checkpoint="ckpt1", name="n2", image="ignored")._body()
    assert body["checkpoint_id"] == "ckpt1"
    assert "resources" not in body and "image" not in body


WANTED = Egress(mode="allowlist", hosts=("a.com",))


@pytest.mark.parametrize(
    ("checkpoint", "reported", "said"),
    [
        (
            "ckpt-1",
            _EgressPolicySDK(mode="denylist", allowed_hosts=[], denied_hosts=["bad.com"]),
            ("denylist", "bad.com"),
        ),
        (
            None,
            _EgressPolicySDK(mode="allowlist", allowed_hosts=["b.com"], denied_hosts=[]),
            ("allowlist", "b.com"),
        ),
        ("ckpt-1", None, ("none",)),
    ],
    ids=["restored under a denylist", "fresh under another allowlist", "restored under no policy"],
)
def test_start_refuses_a_runtime_that_reports_another_policy_naming_both_and_deletes_it(
    tmp_path, monkeypatch, checkpoint, reported, said
):
    box, mod, calls, clients = _ready_box(
        monkeypatch, tmp_path, egress=WANTED, from_checkpoint=checkpoint, precreate_workdir=False
    )
    if reported is not None:
        mod.preset_policy["rt-1"] = reported
    with pytest.raises(SandboxDead) as e:
        asyncio.run(box.start())
    for part in (*said, "allowlist", "a.com"):  # the runtime's policy, then the box's
        assert part in str(e.value)
    rt = mod.runtimes_store["rt-1"]
    # no policy is set on the way: a restored runtime's processes ran under the other one until now
    assert rt.deleted is True and rt.set_policy_calls == 0
    assert not (tmp_path / "work").exists()  # no command ran on the runtime
    assert box.started is False and box.box_id is None
    assert clients[0].closed is True and mod.async_runta_instances[-1].closed is True
    assert [c["method"] for c in calls] == ["POST"]
    assert calls[0]["json"]["egress_policy"] == WANTED.api()  # the box sent its own with the create


def test_a_deletion_that_fails_does_not_replace_the_refusal(tmp_path, monkeypatch):
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, egress=WANTED, precreate_workdir=False)
    mod.preset_policy["rt-1"] = _EgressPolicySDK(mode="denylist", allowed_hosts=[], denied_hosts=["bad.com"])
    mod.delete_fails["rt-1"] = OSError("gone away")
    with pytest.raises(SandboxDead) as e:
        asyncio.run(box.start())
    said = str(e.value)
    for part in ("denylist", "bad.com", "a.com", "runtime rt-1 was not deleted", "OSError('gone away')"):
        assert part in said
    assert box.started is False and clients[0].closed is True  # the clients still close


def test_a_client_that_fails_to_close_after_a_deletion_is_told_apart_from_a_runtime_left(
    tmp_path, monkeypatch
):
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, egress=WANTED, precreate_workdir=False)
    mod.preset_policy["rt-1"] = _EgressPolicySDK(mode="denylist", allowed_hosts=[], denied_hosts=["bad.com"])

    async def close_fails():
        raise OSError("socket")

    with pytest.raises(SandboxDead) as e:
        asyncio.run(_start_with_client_close_failing(box, mod, close_fails))
    assert "runtime rt-1 was deleted, then a client did not close: OSError('socket')" in str(e.value)
    assert mod.runtimes_store["rt-1"].deleted is True


async def _start_with_client_close_failing(box, mod, close_fails):
    """`start` with the SDK client's `close` failing: patched once the client exists, before the refusal."""
    original = box.__class__._own_policy

    async def own_policy_then_patch(self, when):
        mod.async_runta_instances[-1].close = close_fails
        await original(self, when)

    box.__class__._own_policy = own_policy_then_patch
    try:
        await box.start()
    finally:
        box.__class__._own_policy = original


def test_resume_whose_policy_cannot_be_read_deletes_the_runtime_and_raises_that_error(tmp_path, monkeypatch):
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, egress=WANTED)
    mod.preset_policy["rt-1"] = _EgressPolicySDK(mode="allowlist", allowed_hosts=["a.com"], denied_hosts=[])
    asyncio.run(box.start())
    rt = mod.runtimes_store["rt-1"]
    asyncio.run(box.suspend())
    mod.refresh_fails["rt-1"] = OSError("no read")
    with pytest.raises(OSError, match="no read"):
        asyncio.run(box.resume())
    assert rt.paused is False and rt.deleted is True and box.started is False and box.box_id is None


def test_resume_whose_sdk_call_fails_deletes_the_runtime_and_raises_that_error(tmp_path, monkeypatch):
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, egress=WANTED)
    mod.preset_policy["rt-1"] = _EgressPolicySDK(mode="allowlist", allowed_hosts=["a.com"], denied_hosts=[])
    asyncio.run(box.start())
    rt = mod.runtimes_store["rt-1"]
    asyncio.run(box.suspend())

    async def lost_response(timeout=300.0):
        raise OSError("lost response")  # the service may have resumed the runtime; nothing says which

    rt.resume = lost_response
    with pytest.raises(OSError, match="lost response"):
        asyncio.run(box.resume())
    assert rt.deleted is True and box.started is False and box.box_id is None
    assert clients[0].closed is True and mod.async_runta_instances[-1].closed is True


def test_resume_refused_with_a_deletion_that_fails_keeps_the_refusal(tmp_path, monkeypatch):
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, egress=WANTED)
    mod.preset_policy["rt-1"] = _EgressPolicySDK(mode="allowlist", allowed_hosts=["a.com"], denied_hosts=[])
    asyncio.run(box.start())
    rt = mod.runtimes_store["rt-1"]
    asyncio.run(box.suspend())
    rt.policy = _EgressPolicySDK(mode="denylist", allowed_hosts=[], denied_hosts=["bad.com"])
    mod.delete_fails["rt-1"] = OSError("gone away")
    with pytest.raises(SandboxDead) as e:
        asyncio.run(box.resume())
    for part in ("resumed", "denylist", "runtime rt-1 was not deleted", "OSError('gone away')"):
        assert part in str(e.value)
    assert rt.deleted is False and box.started is False


def test_resume_refuses_a_runtime_that_comes_back_under_another_policy_and_deletes_it(tmp_path, monkeypatch):
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, egress=WANTED)
    mod.preset_policy["rt-1"] = _EgressPolicySDK(mode="allowlist", allowed_hosts=["a.com"], denied_hosts=[])
    asyncio.run(box.start())
    rt = mod.runtimes_store["rt-1"]
    asyncio.run(box.suspend())
    assert rt.paused is True
    rt.policy = _EgressPolicySDK(mode="denylist", allowed_hosts=[], denied_hosts=["bad.com"])
    with pytest.raises(SandboxDead) as e:
        asyncio.run(box.resume())
    for part in ("resumed", "denylist", "bad.com", "a.com"):
        assert part in str(e.value)
    assert rt.paused is False and rt.deleted is True and box.started is False and box.box_id is None
    # the same policy back: the box goes on
    box2, mod2, _, _ = _ready_box(monkeypatch, tmp_path, egress=WANTED, rt_id="rt-2")
    mod2.preset_policy["rt-2"] = _EgressPolicySDK(mode="allowlist", allowed_hosts=["a.com"], denied_hosts=[])
    asyncio.run(box2.start())
    asyncio.run(box2.suspend())
    asyncio.run(box2.resume())
    assert mod2.runtimes_store["rt-2"].deleted is False and box2.started is True


@pytest.mark.parametrize("checkpoint", [None, "ckpt-1"], ids=["fresh", "restored"])
@pytest.mark.parametrize(
    ("hosts", "reported"),
    [(("a.com",), ["a.com"]), (("b.com", "a.com"), ["a.com", "b.com"])],
    ids=["the same policy", "the same hosts in another order"],
)
def test_start_returns_a_box_whose_runtime_reports_its_own_policy(
    tmp_path, monkeypatch, checkpoint, hosts, reported
):
    wanted = Egress(mode="allowlist", hosts=hosts)
    box, mod, _, _ = _ready_box(monkeypatch, tmp_path, egress=wanted, from_checkpoint=checkpoint)
    mod.preset_policy["rt-1"] = _EgressPolicySDK(mode="allowlist", allowed_hosts=reported, denied_hosts=[])
    assert asyncio.run(box.start()) is box
    rt = mod.runtimes_store["rt-1"]
    assert rt.set_policy_calls == 0 and rt.deleted is False
    assert box.started is True and box.box_id == "rt-1"
    asyncio.run(box.stop())


def test_a_box_with_no_policy_sends_none_and_checks_none(tmp_path, monkeypatch):
    box, mod, calls, _ = _ready_box(monkeypatch, tmp_path, from_checkpoint="ckpt-1")
    mod.preset_policy["rt-1"] = _EgressPolicySDK(mode="denylist", allowed_hosts=[], denied_hosts=["bad.com"])
    assert asyncio.run(box.start()) is box  # the restored runtime's own policy is not this box's to judge
    assert "egress_policy" not in calls[0]["json"]
    assert mod.runtimes_store["rt-1"].set_policy_calls == 0
    asyncio.run(box.stop())


def test_a_failure_after_the_create_deletes_the_runtime_and_closes_both_clients(tmp_path, monkeypatch):
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, fail_wait=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(box.start())
    assert mod.runtimes_store["rt-1"].deleted is True
    assert clients[0].closed is True
    assert mod.async_runta_instances[-1].closed is True


def test_a_refused_create_closes_both_clients_and_deletes_nothing(tmp_path, monkeypatch):
    refusal = Resp(400, {"error": {"code": "bad", "message": "no"}})
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, post=refusal)
    with pytest.raises(runta_mod.RuntaError):
        asyncio.run(box.start())
    assert mod.runtimes_store == {}  # `runtimes.get` was never reached
    assert clients[0].closed is True
    assert mod.async_runta_instances[-1].closed is True
    assert len(calls) == 1  # the refused POST alone; no DELETE


def test_start_can_create_a_workdir_that_does_not_already_exist_on_the_runtime(tmp_path, monkeypatch):
    # guards start making its workdir straight on the runtime: exec starts in the workdir it makes
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path, precreate_workdir=False)
    assert not (tmp_path / "work").exists()
    asyncio.run(box.start())
    assert (tmp_path / "work").is_dir()
    asyncio.run(box.stop())


async def _refuses_the_workdir_command(cmd, **kwargs):
    raise _InvalidStatus("server rejected WebSocket connection: HTTP 404")


async def _status_is_not_found():
    raise _ApiError(status_code=404)


@pytest.mark.parametrize("ended", ["deleting", "not found"])
def test_a_runtime_that_ends_before_its_workdir_is_made_raises_sandbox_dead(tmp_path, monkeypatch, ended):
    box, mod, _, clients = _ready_box(monkeypatch, tmp_path, precreate_workdir=False)
    rt = asyncio.run(mod.AsyncRunta(token="t", endpoint="e").runtimes.get("rt-1"))
    rt.exec = _refuses_the_workdir_command
    if ended == "deleting":
        rt._status = "deleting"
    else:
        rt.status = _status_is_not_found
    with pytest.raises(SandboxDead, match="HTTP 404"):  # the runner retries on this and nothing else
        asyncio.run(box.start())
    assert box.dead is not None and "HTTP 404" in box.dead
    assert rt.deleted is True and box.started is False
    assert mod.async_runta_instances[-1].closed is True


def test_a_runtime_that_fails_its_workdir_while_running_re_raises_the_error_as_is(tmp_path, monkeypatch):
    box, mod, _, _ = _ready_box(monkeypatch, tmp_path, precreate_workdir=False)
    rt = asyncio.run(mod.AsyncRunta(token="t", endpoint="e").runtimes.get("rt-1"))
    rt.exec = _refuses_the_workdir_command  # the runtime still reads as running: not a dead one
    with pytest.raises(_InvalidStatus):
        asyncio.run(box.start())
    assert box.dead is None
    assert rt.deleted is True and box.started is False


# --- 11. exec --------------------------------------------------------------------------------------


def test_exec_wraps_a_finite_timeout_and_hands_the_sdk_timeout_plus_15(monkeypatch):
    install_runta_minimal(monkeypatch)
    rt = RecordingRuntime()
    box = RuntaSandbox(workdir="/w")
    box._rt = rt
    asyncio.run(box.exec(["echo", "hi"], timeout=5.0))
    call = rt.calls[-1]
    assert call["timeout"] == 20.0
    assert call["cmd"] == [
        "sh",
        "-c",
        'cd -- "$0" && exec "$@"',
        "/w",
        "timeout",
        "-s",
        "KILL",
        "5",
        "echo",
        "hi",
    ]

    asyncio.run(box.exec(["echo", "hi"], timeout=None))
    call = rt.calls[-1]
    assert call["timeout"] is None
    assert call["cmd"] == ["sh", "-c", 'cd -- "$0" && exec "$@"', "/w", "echo", "hi"]


def test_rc_137_is_timed_out_only_once_elapsed_reaches_the_deadline(monkeypatch):
    install_runta_minimal(monkeypatch)
    box = RuntaSandbox(workdir="/w")
    box._rt = RecordingRuntime(exit_code=137, delay=0.0)  # a SIGKILL from elsewhere, well inside the deadline
    r = asyncio.run(box.exec(["x"], timeout=5.0))
    assert r["rc"] == 137 and r["stderr"] == "" and r["timed_out"] is False
    box2 = RuntaSandbox(workdir="/w")
    box2._rt = RecordingRuntime(exit_code=137, delay=0.05)  # elapsed reaches the (short) deadline
    r = asyncio.run(box2.exec(["x"], timeout=0.03))
    assert r["timed_out"] is True and r["stderr"] == "ended at the 0.03s timeout"


@pytest.mark.parametrize(
    ("rc", "stderr"),
    [(137, b""), (124, b"Command timed out after 0.034s\n")],
    ids=["a kill", "the service's own 124"],
)
def test_a_run_past_a_fractional_deadline_is_timed_out_though_its_rounded_time_is_under_it(
    monkeypatch, rc, stderr
):
    install_runta_minimal(monkeypatch)
    clock = iter([100.0, 100.0344])  # 0.0344 s elapsed, reported as 0.03: under a 0.034 s deadline
    monkeypatch.setattr(runta_mod, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    box = RuntaSandbox(workdir="/w")
    box._rt = RecordingRuntime(exit_code=rc, stderr=stderr)
    r = asyncio.run(box.exec(["x"], timeout=0.034))
    assert r["timed_out"] is True and r["elapsed"] == 0.03
    assert r["stderr"].endswith("ended at the 0.034s timeout")


def test_rc_124_is_timed_out_only_with_the_services_own_message_past_the_deadline(monkeypatch):
    install_runta_minimal(monkeypatch)
    box = RuntaSandbox(workdir="/w")
    box._rt = RecordingRuntime(exit_code=124, stderr=b"own exit 124\n", delay=0.05)
    r = asyncio.run(box.exec(["x"], timeout=0.03))  # past the deadline, but this is the command's own 124
    assert r["timed_out"] is False and r["stderr"] == "own exit 124\n"

    box2 = RuntaSandbox(workdir="/w")
    box2._rt = RecordingRuntime(exit_code=124, stderr=b"Command timed out after 0.03s\n", delay=0.05)
    r = asyncio.run(box2.exec(["x"], timeout=0.03))
    assert r["timed_out"] is True
    assert r["stderr"] == "Command timed out after 0.03s\n\nended at the 0.03s timeout"


def test_an_exception_while_the_runtime_is_gone_raises_sandbox_dead_and_sets_dead(monkeypatch):
    mod = install_runta_minimal(monkeypatch)

    class Gone404:
        async def exec(self, cmd, *, timeout, env, stdin):
            raise RuntimeError("socket closed")

        async def status(self):
            raise mod.ApiError(status_code=404)

    box = RuntaSandbox(workdir="/w")
    box._rt = Gone404()
    with pytest.raises(SandboxDead, match="socket closed"):
        asyncio.run(box.exec(["x"]))
    assert box.dead is not None and "socket closed" in box.dead

    class Crashed:
        async def exec(self, cmd, *, timeout, env, stdin):
            raise RuntimeError("boom")

        async def status(self):
            return types.SimpleNamespace(value="crashed")

    box2 = RuntaSandbox(workdir="/w")
    box2._rt = Crashed()
    with pytest.raises(SandboxDead, match="boom"):
        asyncio.run(box2.exec(["x"]))
    assert box2.dead is not None


def test_an_exception_while_the_runtime_is_running_is_reraised_as_is(monkeypatch):
    install_runta_minimal(monkeypatch)

    class Running:
        async def exec(self, cmd, *, timeout, env, stdin):
            raise RuntimeError("transient")

        async def status(self):
            return types.SimpleNamespace(value="running")

    box = RuntaSandbox(workdir="/w")
    box._rt = Running()
    with pytest.raises(RuntimeError, match="transient"):
        asyncio.run(box.exec(["x"]))
    assert box.dead is None


def test_exec_runs_for_real_streams_apart_feeds_stdin_env_workdir_and_the_prelude_once(tmp_path, monkeypatch):
    mod = stub_runta(tmp_path)
    monkeypatch.setitem(sys.modules, "runta", mod)
    work = tmp_path / "work"
    work.mkdir()
    box = RuntaSandbox(workdir=str(work), prelude="export LION_N=$((${LION_N:-0}+1))")
    box._rt = asyncio.run(mod.AsyncRunta(token="t", endpoint="e").runtimes.get("r1"))

    r = asyncio.run(box.exec(["bash", "-c", "echo out; echo bad >&2; printf tail; exit 3"]))
    assert (r["rc"], r["stdout"], r["stderr"]) == (3, "out\ntail", "bad\n")
    r = asyncio.run(box.exec(["cat"], stdin=b"fed\n"))
    assert (r["rc"], r["stdout"]) == (0, "fed\n")
    r = asyncio.run(box.exec(["pwd"]))
    assert r["stdout"].strip() == str(work)
    r = asyncio.run(box.exec(["sh", "-c", "echo $X"], env={"X": "v1"}))
    assert r["stdout"] == "v1\n"
    r = asyncio.run(box.exec(["sh", "-c", "echo $LION_N"]))
    assert r["stdout"] == "1\n"  # the prelude runs exactly once
    r = asyncio.run(box.exec(["sh", "-c", "echo $LION_N"], timeout=5))
    assert r["stdout"] == "1\n"  # and once more under the timeout wrapper, not twice
    r = asyncio.run(box.exec(["sleep", "2"], timeout=1))
    assert r["rc"] == 137 and r["timed_out"] is True


# --- 12. read / write -------------------------------------------------------------------------------


def test_read_write_roundtrips_missing_and_non_utf8_files(tmp_path, monkeypatch):
    mod = stub_runta(tmp_path)
    monkeypatch.setitem(sys.modules, "runta", mod)
    work = tmp_path / "work"
    work.mkdir()
    box = RuntaSandbox(workdir=str(work))
    box._rt = asyncio.run(mod.AsyncRunta(token="t", endpoint="e").runtimes.get("r1"))

    with pytest.raises(FileNotFoundError):
        asyncio.run(box.read("missing.txt"))
    asyncio.run(box.write("sub/dir/f.txt", "hi\n"))
    assert asyncio.run(box.read("sub/dir/f.txt")) == "hi\n"
    assert (work / "sub" / "dir" / "f.txt").read_text() == "hi\n"
    (work / "latin.txt").write_bytes(b"caf\xe9\n")
    with pytest.raises(ValueError, match="not UTF-8"):
        asyncio.run(box.read("latin.txt"))


# --- 13. push --------------------------------------------------------------------------------------


def test_push_copies_only_what_git_sees_and_returns_those_names(tmp_path, monkeypatch):
    mod = stub_runta(tmp_path)
    monkeypatch.setitem(sys.modules, "runta", mod)
    work = tmp_path / "work"
    work.mkdir()
    box = RuntaSandbox(workdir=str(work))
    box._rt = asyncio.run(mod.AsyncRunta(token="t", endpoint="e").runtimes.get("r1"))

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    (src / "keep.py").write_text("x = 1\n")
    (src / ".gitignore").write_text("build/\n")
    (src / "build").mkdir()
    (src / "build" / "out.o").write_bytes(b"\0")
    assert asyncio.run(box.push(src)) == [".gitignore", "keep.py"]
    assert sorted(p.name for p in work.iterdir()) == [".gitignore", "keep.py"]


# --- 14. set_egress / checkpoint / suspend / resume / events / stop -----------------------------


def test_set_egress_checkpoint_suspend_resume_events_after_stop_and_stop_twice(tmp_path, monkeypatch):
    mod = stub_runta(tmp_path)
    monkeypatch.setitem(sys.modules, "runta", mod)
    work = tmp_path / "work"
    work.mkdir()
    box = RuntaSandbox(workdir=str(work))
    rt = asyncio.run(mod.AsyncRunta(token="t", endpoint="e").runtimes.get("r1"))
    box._rt, box.runtime_id, box.started = rt, "r1", True

    new_egress = Egress(mode="denylist", hosts=("bad.com",))
    reported = asyncio.run(box.set_egress(new_egress))
    assert reported == new_egress and box.egress == new_egress and rt.set_policy_calls == 1

    ckpt = asyncio.run(box.checkpoint(name="snap1"))
    assert isinstance(ckpt, runta_mod.Checkpoint)
    assert ckpt.display_name == "snap1" and ckpt.state == "ready"

    asyncio.run(box.suspend())
    assert rt.paused is True
    asyncio.run(box.resume())
    assert rt.paused is False

    assert box.box_id == "r1"

    seen: list[str] = []

    async def fake_events(rid, **filters):
        seen.append(rid)
        return []

    monkeypatch.setattr(runta_mod, "events", fake_events)
    asyncio.run(box.stop())
    assert rt.deleted is True
    assert box.box_id is None and box.runtime_id == "r1"  # the id outlives the runtime

    asyncio.run(box.events())
    assert seen == ["r1"]

    asyncio.run(box.stop())  # a second stop is a no-op, not an error
    assert rt.deleted is True


@pytest.mark.parametrize(
    "failing",
    [("sdk",), ("api",), ("sdk", "api")],
    ids=["the SDK client", "the API client", "both"],
)
def test_stop_closes_both_clients_when_one_fails_to_close_and_raises_the_first_error(
    tmp_path, monkeypatch, failing
):
    box, mod, calls, clients = _ready_box(monkeypatch, tmp_path)
    asyncio.run(box.start())
    sdk, api, closed = mod.async_runta_instances[-1], box._api, []

    def closing(name, real):
        async def close():
            closed.append(name)
            if name in failing:
                raise OSError(name)
            await real()

        return close

    sdk.close, api.close = closing("sdk", sdk.close), closing("api", api.close)
    with pytest.raises(OSError) as e:
        asyncio.run(box.stop())
    assert str(e.value) == failing[0]  # the first of the errors, raised once both had been tried
    assert closed == ["sdk", "api"] and mod.runtimes_store["rt-1"].deleted is True


def test_events_without_a_runtime_id_refuses():
    box = RuntaSandbox(workdir="/w")
    with pytest.raises(RuntimeError, match="no runtime"):
        asyncio.run(box.events())


# --- 15. the shell and code tools through the box -------------------------------------------------


def test_the_shell_and_code_tools_run_in_the_runta_box_through_the_same_registrations(tmp_path, monkeypatch):
    mod = stub_runta(tmp_path)
    monkeypatch.setitem(sys.modules, "runta", mod)
    work = tmp_path / "work"
    work.mkdir()
    box = RuntaSandbox(workdir=str(work))
    box._rt = asyncio.run(mod.AsyncRunta(token="t", endpoint="e").runtimes.get("r1"))
    box.runtime_id, box.started = "r1", True

    (work / "pkg").mkdir()
    (work / "pkg" / "m.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return f()\n")
    actor = Actor("d")
    specs = shell(actor, box) | code(actor, BoxTree(box)) | {h.name for h in DEFAULT_HANDLERS}
    profile = Profile(name="d", specs=frozenset(specs), system="x")
    backend = scripted(
        '<lact b1>bash(command="ls pkg")</lact><lact p1>python(code="print(6*7)")</lact>'
        '<lact s1>search(pattern="return f", path="pkg")</lact>'
        '<lact r1>read_lines(path="pkg/m.py", start=5, end=6)</lact>'
        '<lact e1>edit(path="pkg/m.py", old="return f()", new="return f() + 1")</lact>',
        "done",
    )
    run = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=2), backend))
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["b1"].ok and r["b1"].value["stdout"] == "m.py\n" and r["b1"].value["rc"] == 0
    assert r["p1"].ok and r["p1"].value["stdout"] == "42\n"
    assert r["s1"].ok and r["s1"].value == "pkg/m.py:6:    return f()\n"
    assert r["r1"].ok and r["r1"].value == "pkg/m.py lines 5-6 of 6\n5: def g():\n6:     return f()"
    assert r["e1"].ok and (work / "pkg" / "m.py").read_text().endswith("return f() + 1\n")


# --- 16. a real Runta runtime, opted in -----------------------------------------------------------


@pytest.mark.skipif(os.environ.get("LION_RUNTA_E2E") != "1", reason="LION_RUNTA_E2E=1 runs a real runtime")
def test_a_real_runtime_runs_the_shell_and_code_tools_proves_egress_and_stops(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (src / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    box = RuntaSandbox(
        workdir="/work",
        name="lionagi-test",
        cpu=1,
        memory=1,
        egress=Egress(mode="allowlist", hosts=("pypi.org", "files.pythonhosted.org")),
    )
    actor = Actor("e")
    specs = shell(actor, box) | code(actor, BoxTree(box)) | {h.name for h in DEFAULT_HANDLERS}
    profile = Profile(name="e", specs=frozenset(specs), system="x")

    async def go():
        await box.start()
        try:
            assert sorted(await box.push(src)) == ["calc.py", "test_calc.py"]
            call = "python3 -c 'from calc import add; print(add(2, 2))'"
            allowed = "curl -sS -m 8 -o /dev/null -w '%{http_code}' https://pypi.org/ || echo blocked"
            # without -f curl exits 0 on any HTTP answer, so `blocked` means the connection itself failed
            blocked = "curl -sS -m 8 -o /dev/null https://example.com/ && echo open || echo blocked"
            backend = scripted(
                f'<lact b1>bash(command="{call}; ls *.py")</lact>'
                '<lact s1>search(pattern="return a", path=".")</lact>'
                f'<lact a1>bash(command="{allowed}")</lact>'
                f'<lact d1>bash(command="{blocked}")</lact>',
                '<lact e1>edit(path="calc.py", old="a - b", new="a + b")</lact>',
                f'<lact b2>bash(command="{call}")</lact>',
                "done",
            )
            return await actor.run(profile, RunRequest(inputs=("go",), max_rounds=4), backend)
        finally:
            await box.stop()

    run = asyncio.run(go())
    r = {e.alias: e.content for e in run.record if e.kind is Kind.RESULT}
    assert r["b1"].value["stdout"] == "0\ncalc.py\ntest_calc.py\n" and r["b1"].value["rc"] == 0
    assert r["s1"].value == "calc.py:2:    return a - b\n"
    assert r["a1"].value["stdout"].strip() in ("200", "301", "302", "403")  # reachable: an HTTP answer
    assert r["d1"].value["stdout"].strip() == "blocked"  # outside the allowlist: the connection itself fails
    assert r["e1"].ok and r["b2"].value["stdout"] == "4\n"
    assert box.started is False and box.box_id is None
