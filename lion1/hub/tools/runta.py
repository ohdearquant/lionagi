"""A box on Runta: a Linux runtime from a Runta image, whose outbound network is governed by a policy the
service enforces outside the guest, with checkpoints, suspend and resume. The box shape of
`hub.tools.box`. The `runta` package (`runta-sdk`) is imported when a box starts, never at import: the hub
loads without it.

The SDK runs commands, moves files, takes checkpoints, pauses, resumes and sets the egress policy. A
runtime that needs an image, secrets or Runta's LLM capture is created over the REST API, which the SDK's
own create does not reach; so are image builds and the records Runta keeps about a runtime (`events`,
`token_savings`, `tool_calls`). Both read one token: `RUNTA_TOKEN`, else the `token` in Runta's config
file.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lionagi.bus import Event

from .box import BoxBase, pack
from .daytona import SandboxDead

__all__ = (
    "ENDPOINT",
    "token",
    "endpoint",
    "Egress",
    "EgressSecret",
    "SecretInjection",
    "Checkpoint",
    "TokenSaving",
    "TokenSavings",
    "ToolCallFinding",
    "ToolCall",
    "ToolCalls",
    "RuntaApi",
    "RuntaError",
    "RuntaSandbox",
    "events",
    "token_savings",
    "tool_calls",
    "find_image",
    "build_image",
)

ENDPOINT = "https://api.runta.com"
_CONFIG = Path.home() / ".config" / "runta" / "config.toml"
_AGENT = "lionagi-hub (runta box)"  # the service's edge refuses urllib's default agent; this one is ours
_REQUEST_TIMEOUT = 60.0  # seconds one request may take


def _config() -> dict[str, Any]:
    path = Path(os.environ.get("RUNTA_CONFIG") or _CONFIG)
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def token() -> str:
    """`RUNTA_TOKEN` from the environment, else the `token` in Runta's config file (`RUNTA_CONFIG`, by
    default `~/.config/runta/config.toml`): where the SDK and the CLI look, in their order."""
    value = os.environ.get("RUNTA_TOKEN", "").strip() or str(_config().get("token") or "").strip()
    if not value:
        raise RuntimeError("RUNTA_TOKEN: not in the environment and no token in Runta's config file")
    return value


def endpoint() -> str:
    """`RUNTA_ENDPOINT`, else the config file's `endpoint`, else the public API."""
    value = os.environ.get("RUNTA_ENDPOINT", "").strip() or str(_config().get("endpoint") or "").strip()
    return (value or ENDPOINT).rstrip("/")


# --- typed configuration --------------------------------------------------------------------------

_HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")


class Egress(BaseModel):
    """Where a runtime may connect out. Runta enforces it outside the guest, so a process in the box
    cannot widen it. A change to a running runtime can read back before it takes effect: a caller that
    needs it closed proves it from inside."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["allowlist", "denylist"] = Field(
        description="allowlist: only `hosts` are reachable. denylist: every host but `hosts` is."
    )
    hosts: tuple[str, ...] = Field(
        default=(), description="Bare host names, lowercase: no scheme, port, path or wildcard."
    )

    @field_validator("hosts")
    @classmethod
    def _bare(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        for host in hosts:
            if len(host) > 253 or not _HOST.fullmatch(host):
                raise ValueError(f"{host!r} is not a bare lowercase host name")
        return hosts

    def api(self) -> dict[str, Any]:
        # the create body takes one list, the mode's own: the other one is refused as unknown
        key = "allowed_hosts" if self.mode == "allowlist" else "denied_hosts"
        return {"mode": self.mode, key: list(self.hosts)}

    @classmethod
    def from_api(cls, policy: Mapping[str, Any] | None) -> Egress | None:
        if not policy:
            return None
        mode = policy["mode"]
        hosts = policy.get("allowed_hosts" if mode == "allowlist" else "denied_hosts") or []
        return cls(mode=mode, hosts=tuple(hosts))


class EgressSecret(BaseModel):
    """A secret Runta adds to requests leaving the box for `host`: the guest sends the request without
    it and never holds the value."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(description="The host pattern the request goes to.")
    name: str = Field(description="The header or query parameter that carries the value.")
    template: str = Field(description="Runta's value template, rendered from the secret.")
    action: Literal["header", "query"] = Field(default="header", description="Where the value goes.")
    path: str | None = Field(default=None, description="A path pattern the request must match, when set.")

    def api(self) -> dict[str, Any]:
        rule: dict[str, Any] = {
            "host_pattern": self.host,
            "action": self.action,
            "name": self.name,
            "value_template": self.template,
        }
        if self.path is not None:
            rule["path_pattern"] = self.path
        return rule


class SecretInjection(BaseModel):
    """One stored Runta secret and where the runtime receives it: environment variables, egress
    headers or query parameters, managed files under the guest's home. The secret itself is created
    in Runta (its dashboard or API); this names it by id and never carries its value."""

    model_config = ConfigDict(frozen=True)

    secret_id: str = Field(description="The id Runta gave the stored secret.")
    env: Mapping[str, str] = Field(default_factory=dict, description="Variable name to value template.")
    egress: tuple[EgressSecret, ...] = Field(default=(), description="Values added to outbound requests.")
    files: Mapping[str, str] = Field(
        default_factory=dict, description="Path under the guest's home to value template."
    )

    def api(self) -> dict[str, Any]:
        config: dict[str, Any] = {"secret_id": self.secret_id}
        if self.env:
            config["environment"] = [{"name": k, "value_template": v} for k, v in self.env.items()]
        if self.egress:
            config["egress_rules"] = [rule.api() for rule in self.egress]
        if self.files:
            config["files"] = [{"path": k, "value_template": v} for k, v in self.files.items()]
        return config


# --- typed records ----------------------------------------------------------------------------------


class _Record(BaseModel):
    # a field Runta adds later is kept, never a reason to refuse the record
    model_config = ConfigDict(frozen=True, extra="allow")


class Checkpoint(_Record):
    """A checkpoint of a runtime's filesystem and running processes."""

    id: str
    display_name: str = ""
    state: str = Field(default="", description="creating, ready or failed.")


class TokenSaving(_Record):
    """One LLM request Runta rewrote on its way out, and what that saved."""

    id: str
    event_id: str
    runtime_id: str
    observed_at_unix_millis: int
    provider: str | None = None
    model: str | None = None
    endpoint: str | None = None
    wire_bytes_before: int
    wire_bytes_after: int
    saved_wire_bytes: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    saved_estimated_tokens: int
    methods: list[dict[str, Any]] = Field(default_factory=list)
    parse_error: str | None = None


class TokenSavings(_Record):
    summary: dict[str, Any] = Field(default_factory=dict)
    events: list[TokenSaving] = Field(default_factory=list)


class ToolCallFinding(_Record):
    """What Runta's analysis says a tool call's output wasted: a large output, repeated lines, keys,
    paths or calls."""

    kind: str
    severity: str
    confidence: str
    title: str
    detail: str
    estimated_savings_tokens: int


class ToolCall(_Record):
    """One tool call an agent harness made inside a runtime, as Runta captured and analysed it."""

    id: str
    event_id: str
    runtime_id: str
    observed_at_unix_millis: int
    provider: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    input_chars: int
    output_chars: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_savings_tokens: int
    truncated: bool
    findings: list[ToolCallFinding] = Field(default_factory=list)
    analysis_issues: list[dict[str, Any]] = Field(default_factory=list)
    input_preview: str = ""
    output_preview: str = ""


class ToolCalls(_Record):
    summary: dict[str, Any] = Field(default_factory=dict)
    calls: list[ToolCall] = Field(default_factory=list)


def as_event(record: Mapping[str, Any]) -> Event:
    """One of Runta's runtime events as the bus's `Event`: kind `runta.<kind>`, the record's ids and
    payload as the payload, and `at` the time Runta says it happened. A record without those fields
    is refused: an event read as something else is worse than none."""
    try:
        kind, millis = record["kind"], record["occurred_at_unix_millis"]
        payload = {"id": record["id"], "runtime_id": record["runtime_id"], "payload": record["payload"]}
    except KeyError as e:
        raise ValueError(f"a Runta event without {e.args[0]!r}: {str(dict(record))[:200]}") from None
    if not isinstance(kind, str) or not isinstance(millis, int):
        raise ValueError(f"a Runta event with kind {kind!r} at {millis!r}")
    return Event(kind=f"runta.{kind}", payload=payload, at=millis / 1000)


# --- the REST API -----------------------------------------------------------------------------------


class RuntaError(RuntimeError):
    """The API refused a request: its status, Runta's error code and message."""

    def __init__(self, status: int, code: str | None, message: str):
        super().__init__(f"HTTP {status} {code or ''}: {message}".replace("  ", " "))
        self.status, self.code = status, code


class RuntaApi:
    """The REST calls the SDK does not make. A refused request raises `RuntaError`; a 429, a 5xx or a
    dropped connection is retried with the same idempotency key, so a create is never made twice."""

    def __init__(self, tok: str | None = None, base: str | None = None, *, attempts: int = 5):
        import httpx

        self._http = httpx.AsyncClient(
            base_url=base or endpoint(),
            headers={"authorization": f"Bearer {tok or token()}", "user-agent": _AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
        self.attempts = attempts

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        deadline: float | None = None,
    ) -> Any:
        """One request, retried as the class says. A `deadline` (a `time.monotonic()` instant) bounds it
        by wall clock: each request ends there at the latest, however its body arrives (its timeout, the
        time left to it and at most the usual 60 s, bounds only one socket operation at a time), a backoff
        wait ends there at the latest, and nothing is retried past it. What ends at the deadline is raised:
        the request's failure, or a `TimeoutError` when the deadline fell during a request or had passed
        before one could begin."""
        import httpx

        # one key for every attempt of this call: a retried create is the same create
        headers = {"idempotency-key": str(uuid.uuid4())} if method == "POST" else {}
        attempt = 0
        while True:
            attempt += 1
            last = attempt >= self.attempts
            left: float | None = None
            options: dict[str, float] = {}
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{method} {path}: its deadline had passed before the request began")
                options["timeout"] = min(_REQUEST_TIMEOUT, left)
            wall = asyncio.timeout(left)  # no limit without a deadline
            try:
                async with wall:
                    resp = await self._http.request(
                        method, path, json=json, params=params, headers=headers, **options
                    )
            except httpx.TransportError:
                if last or not await self._backoff(attempt, deadline):
                    raise
                continue
            except TimeoutError as exc:
                if not wall.expired():
                    raise
                raise TimeoutError(f"{method} {path}: its deadline fell during the request") from exc
            if resp.status_code < 400:
                body = resp.json() if resp.content else {}
                return body.get("data", body) if isinstance(body, dict) else body
            retry = not last and (resp.status_code == 429 or resp.status_code >= 500)
            if retry and await self._backoff(attempt, deadline):
                continue
            try:
                err = resp.json().get("error") or {}
            except ValueError:
                err = {}
            raise RuntaError(resp.status_code, err.get("code"), err.get("message") or resp.text[:300])

    @staticmethod
    async def _backoff(attempt: int, deadline: float | None) -> bool:
        """The wait before another attempt; False when `deadline` comes first, the wait ending there."""
        delay = 2**attempt
        if deadline is not None:
            left = deadline - time.monotonic()
            if delay >= left:
                await asyncio.sleep(max(left, 0))
                return False
        await asyncio.sleep(delay)
        return True

    async def pages(self, path: str, key: str, params: Mapping[str, Any]) -> tuple[list[Any], dict]:
        """Every row of a paged list, and the last page's other fields (a summary, say)."""
        rows: list[Any] = []
        query = {k: v for k, v in params.items() if v is not None}
        while True:
            data = await self.call("GET", path, params=query)
            rows.extend(data.get(key) or [])
            nxt = data.get("next_page_token")
            if not nxt:
                return rows, {k: v for k, v in data.items() if k not in (key, "next_page_token")}
            query["page_token"] = nxt

    async def close(self) -> None:
        await self._http.aclose()


async def _with_api(fn, api: RuntaApi | None):
    own = api is None
    api = api or RuntaApi()
    try:
        return await fn(api)
    finally:
        if own:
            await api.close()


async def events(
    runtime_id: str,
    *,
    kind: str | None = None,
    harness: str | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    api: RuntaApi | None = None,
) -> list[Event]:
    """Runta's events for a runtime, every page, as bus `Event`s in the order Runta lists them. Runta
    writes them for the LLM traffic of agent harnesses it captures (`capture=True` on the box): a
    runtime that only ran commands has none."""
    params = {"runtime_id": runtime_id, "kind": kind, "harness": harness, "session_id": session_id}
    params |= {"agent_id": agent_id, "page_size": 100}

    async def read(api: RuntaApi) -> list[Event]:
        rows, _ = await api.pages("/v2/events", "events", params)
        return [as_event(row) for row in rows]

    return await _with_api(read, api)


async def token_savings(runtime_id: str, *, api: RuntaApi | None = None) -> TokenSavings:
    """What Runta's token saving did to a runtime's LLM requests, with its summary."""

    async def read(api: RuntaApi) -> TokenSavings:
        rows, rest = await api.pages("/v2/token-savings", "events", {"runtime_id": runtime_id})
        return TokenSavings(summary=rest.get("summary") or {}, events=rows)

    return await _with_api(read, api)


async def tool_calls(runtime_id: str, *, api: RuntaApi | None = None) -> ToolCalls:
    """The tool calls Runta captured in a runtime, each with its analysis, and the summary."""

    async def read(api: RuntaApi) -> ToolCalls:
        rows, rest = await api.pages("/v2/token-analysis", "calls", {"runtime_id": runtime_id})
        return ToolCalls(summary=rest.get("summary") or {}, calls=rows)

    return await _with_api(read, api)


_OLDEST = datetime.min.replace(tzinfo=UTC)


def _updated(build: Mapping[str, Any]) -> datetime:
    """When a build was updated, as an aware time; a row whose `updated_at` is missing, does not parse
    or carries no offset (a date alone, say) is the oldest."""
    try:
        at = datetime.fromisoformat(build.get("updated_at") or "")
    except (TypeError, ValueError):
        return _OLDEST
    return at if at.tzinfo is not None else _OLDEST


async def find_image(name: str, *, api: RuntaApi | None = None) -> str | None:
    """The runtime-image id of the newest successful build named `name` in the one page of at most 100
    builds the service returns, None when it holds none: the image is built again. The listing takes a
    `limit` (default 50, at most 100, a larger one refused) and no name filter or cursor, so a matching
    build the page omits is not found. Its order is not stated: the page is sorted by `updated_at`,
    compared as instants."""

    async def find(api: RuntaApi) -> str | None:
        data = await api.call("GET", "/v2/images/builds", params={"limit": 100})
        builds = data.get("builds", data) if isinstance(data, dict) else data
        done = [b for b in builds if b.get("name") == name and b.get("status") == "succeeded"]
        done.sort(key=_updated)
        return done[-1]["runtime_image_id"] if done else None

    return await _with_api(find, api)


async def build_image(
    name: str,
    dockerfile: str,
    *,
    architecture: Literal["x86_64", "aarch64"] = "x86_64",
    timeout: float = 3600.0,
    api: RuntaApi | None = None,
) -> str:
    """Build a Runta image from `dockerfile` and wait for it: its runtime-image id. A build that ends
    any other way than `succeeded`, or outlives `timeout`, raises with its last state. `timeout` is
    counted from before the create request: each request and each 15 s wait ends at the deadline at the
    latest, by wall clock, and a failed request is retried only while time is left. So a build is
    reported timed out at the deadline plus scheduling latency, a failure at the deadline is that
    timeout, and a build that succeeded is reported as such."""

    async def build(api: RuntaApi) -> str:
        deadline = time.monotonic() + timeout

        async def request(late: str, method: str, path: str, **kwargs: Any) -> Any:
            try:
                return await api.call(method, path, deadline=deadline, **kwargs)
            except Exception as exc:
                if time.monotonic() < deadline:
                    raise
                raise TimeoutError(late) from exc  # a failure at the deadline is the build's timeout

        made = await request(
            f"image build {name!r} was not created within {timeout:g}s",
            "POST",
            "/v2/images/builds",
            json={"name": name, "dockerfile": dockerfile, "architecture": architecture},
        )
        state = made
        while state.get("status") in ("pending", "building", "queued", "running"):
            late = f"image build {made['id']} still {state.get('status')} after {timeout:g}s"
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(late)
            await asyncio.sleep(min(15, left))
            state = await request(late, "GET", f"/v2/images/builds/{made['id']}")
        if state.get("status") != "succeeded":
            raise RuntimeError(f"image build {made['id']} ended {state.get('status')}: {str(state)[:400]}")
        return state["runtime_image_id"]

    return await _with_api(build, api)


# --- the box ----------------------------------------------------------------------------------------

_TIMED_OUT = "Command timed out after"  # the service's own deadline, as its stderr says it


@dataclass
class RuntaSandbox(BoxBase):
    """One runtime from `image` (a Runta image id, None for the account's default) or from
    `from_checkpoint`. `egress`, `secrets` and `capture` are set when the runtime is made; `set_egress`
    changes the policy of a running one. `start` refuses a runtime that reports another policy than
    `egress`, so a restored box that needs a stricter one starts under the checkpoint's own, calls
    `set_egress` and proves the closure from inside."""

    image: str | None = None
    workdir: str = "/work"
    prelude: str = ""
    cpu: int = 2
    memory: int = 2  # GiB
    name: str = "lionagi"
    egress: Egress | None = None
    secrets: tuple[SecretInjection, ...] = ()
    capture: bool = False  # Runta's LLM tool-I/O capture: what `events` and `tool_calls` read
    from_checkpoint: str | None = None  # a checkpoint id to start from instead of an image
    create_timeout: float = 600.0
    dead: str | None = None  # the error that ended the box; a caller reads it to end its run
    started: bool = False
    runtime_id: str | None = None  # kept after stop: Runta's records of the runtime outlive it
    _client: Any = None
    _rt: Any = None
    _api: RuntaApi | None = None

    @property
    def box_id(self) -> str | None:
        """The provider's id of the live box."""
        return self.runtime_id if self._rt is not None else None

    def _body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "llm_tool_io_capture_enabled": self.capture,
        }
        if self.from_checkpoint:
            body["checkpoint_id"] = self.from_checkpoint
        else:
            body["resources"] = {"requests": {"vcpus": self.cpu, "memory_mib": self.memory * 1024}}
            if self.image:
                body["image"] = {"id": self.image}
        if self.egress is not None:
            body["egress_policy"] = self.egress.api()
        if self.secrets:
            body["secret_configuration"] = [s.api() for s in self.secrets]
        return body

    async def start(self) -> RuntaSandbox:
        from runta import AsyncRunta

        tok, base = token(), endpoint()
        self._client = AsyncRunta(token=tok, endpoint=base)
        self._api = RuntaApi(tok, base)
        try:
            made = await self._api.call("POST", "/v2/runtimes", json=self._body())
            self.runtime_id = made["id"]
            self._rt = await self._client.runtimes.get(self.runtime_id)
            await self._rt.wait_until_ready(timeout=self.create_timeout)
            await self._own_policy("started")
            # straight to the runtime: `exec` starts in the workdir this makes, after the prelude
            r = await self._on_runtime(["mkdir", "-p", "--", self.workdir], timeout=60)
            if r.exit_code != 0:
                err = r.stderr.decode(errors="replace")
                raise SandboxDead(f"mkdir {self.workdir} exited {r.exit_code}: {err[-300:]}")
        except BaseException as e:
            await self._stop_for(e)
            raise
        self.started = True
        return self

    async def _own_policy(self, when: str) -> None:
        """Only a runtime that reports the box's own policy is a box. A restored runtime runs its
        checkpoint's processes from the moment it is restored, so a policy set after leaves them a window
        under the other, and a change reads back before it holds: the policy rides the create, and what
        the runtime did before this read is the service's to have gated."""
        if self.egress is None:
            return
        got = Egress.from_api(_policy_json((await self._rt.refresh_info()).egress_policy))
        if _reach(got) != _reach(self.egress):
            raise SandboxDead(
                f"{when}, the runtime reports egress policy {got or 'none'}, not the box's {self.egress}"
            )

    async def _stop_for(self, e: BaseException) -> None:
        """Delete the runtime behind a failed start or resume. A deletion that fails does not replace the
        failure: the error names both, and the runtime left behind."""
        self._deleted = False
        try:
            await self.stop()
        except Exception as left:
            what = "was deleted, then a client did not close" if self._deleted else "was not deleted"
            more = "".join(f"; {note}" for note in getattr(left, "__notes__", ()))
            if isinstance(e, Exception):
                raise SandboxDead(f"{e}; and runtime {self.runtime_id} {what}: {left!r}{more}") from e
            raise e from left

    async def _gone(self, error: BaseException) -> bool:
        """Whether the runtime behind a failed call has ended: the exec channel answers a deleted runtime
        with a refused websocket, not an API error, so the runtime's own state decides."""
        from runta import ApiError

        try:
            status = (await self._rt.status()).value
        except ApiError as e:
            return e.status_code == 404
        except Exception:
            return False
        return status in ("error", "crashed", "deleting", "shutdown")

    async def _on_runtime(self, cmd: list[str], **kwargs: Any) -> Any:
        """A command sent to the runtime as it is: the runtime's own result, or `SandboxDead` when the
        runtime has ended, which is all the bench runner retries."""
        try:
            return await self._rt.exec(cmd, **kwargs)
        except Exception as e:
            if await self._gone(e):
                self.dead = f"{type(e).__name__}: {e}"
                raise SandboxDead(self.dead) from None
            raise

    async def exec(
        self,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = 120,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One command in the box from `workdir`, after `prelude`, ended there at `timeout` (rc 137 and
        `timed_out`), None no deadline; stdout and stderr apart, as the service returns them."""
        t0 = time.monotonic()
        cmd = self.wrap(argv)
        if timeout is not None:
            cmd = ["timeout", "-s", "KILL", f"{timeout:g}", *cmd]
        cmd = ["sh", "-c", 'cd -- "$0" && exec "$@"', self.workdir, *cmd]
        r = await self._on_runtime(
            cmd, timeout=None if timeout is None else timeout + 15, env=env or None, stdin=stdin
        )
        elapsed = time.monotonic() - t0
        rc, stderr = r.exit_code, r.stderr.decode(errors="replace")
        # 137 alone is any SIGKILL and 124 a command's own; the clock and the service's line say which
        timed_out = (
            timeout is not None
            and elapsed >= timeout
            and (rc == 137 or (rc == 124 and stderr.rstrip().startswith(_TIMED_OUT)))
        )
        if timed_out:
            stderr = (stderr + f"\nended at the {timeout:g}s timeout").strip()
        return {
            "rc": rc,
            "stdout": r.stdout.decode(errors="replace"),
            "stderr": stderr,
            "elapsed": round(elapsed, 2),
            "timed_out": timed_out,
        }

    async def read(self, path: str) -> str:
        from runta import ApiError

        try:
            data = await self._rt.files.read(self.abs(path))
        except ApiError as e:
            if e.status_code == 404:
                raise FileNotFoundError(str(e)) from None
            raise
        try:
            return bytes(data).decode()
        except UnicodeDecodeError as e:
            raise ValueError(
                f"{path} is not UTF-8 text ({e}); an edit would rewrite its bytes, so it is refused"
            ) from None

    async def write(self, path: str, text: str) -> None:
        await self._rt.files.write(self.abs(path), text.encode())  # the service makes the parents

    async def push(self, directory: Path, dest: str | None = None) -> list[str]:
        """Copy `directory` into the box at `dest` (default `workdir`), as `DaytonaSandbox.push` does:
        the files git sees when it is a repository, else every file. Returns the names archived."""
        data, names = pack(Path(directory))
        archive = f"/tmp/lion-push-{uuid.uuid4().hex}.tgz"
        await self._rt.files.write(archive, data)
        q = shlex.quote(dest or self.workdir)
        rc, out = await self.run(f"mkdir -p {q} && tar -xzf {archive} -C {q} && rm -f {archive}", timeout=600)
        if rc != 0:
            raise OSError(f"push into {dest or self.workdir} failed (rc {rc}): {out[-400:]}")
        return names

    async def set_egress(self, egress: Egress) -> Egress:
        """Set the running box's egress policy; returns the policy the service reports. Enforcement
        follows seconds later: prove it from inside before relying on it."""
        info = await self._rt.egress.set_policy(_sdk_policy(egress))
        self.egress = egress
        return Egress.from_api(_policy_json(info.egress_policy)) or egress

    async def checkpoint(self, name: str | None = None, *, timeout: float = 600.0) -> Checkpoint:
        """A checkpoint of the box's filesystem and processes, once it is ready: `from_checkpoint=` on a
        new box starts from it."""
        info = await self._rt.checkpoints.create(name=name, timeout=timeout)
        state = getattr(info, "state", "")
        return Checkpoint(
            id=info.id,
            display_name=getattr(info, "display_name", "") or "",
            state=getattr(state, "value", state) or "",
        )

    async def suspend(self) -> None:
        """Pause the runtime: no command runs until `resume`; files and processes are kept."""
        await self._rt.pause()

    async def resume(self, *, timeout: float = 300.0) -> None:
        """Resume a suspended runtime. Its policy is read before the resume, so a mismatch, or no read,
        deletes it with its processes never woken, and again after, the read that holds. A runtime that
        fails to resume is deleted too."""
        try:
            await self._own_policy("before the resume")
            await self._rt.resume(timeout=timeout)
            await self._own_policy("resumed")
        except BaseException as e:  # no resume, another policy or no read of it: not shown to be a box
            await self._stop_for(e)
            raise

    async def events(self, **filters: Any) -> list[Event]:
        """This box's events (see the module's `events`); readable after `stop`."""
        if self.runtime_id is None:
            raise RuntimeError("the box has no runtime yet")
        return await events(self.runtime_id, **filters)

    async def stop(self) -> None:
        """Delete the runtime (Runta's `rm`) and close the clients. The deletion and both closes are
        tried whatever happens. A cancellation from a close is raised, else a failed deletion, else the
        first close failure; the failures beside the one raised ride it as notes, each naming its client."""
        deleting: BaseException | None = None
        try:
            if self._rt is not None:
                await self._rt.delete()
                self._deleted = True
            elif self.runtime_id is not None and self._client is not None and not self.started:
                # made but never resolved: resolve it by id, so the SDK's delete reads the revision it needs
                await (await self._client.runtimes.get(self.runtime_id)).delete()
                self._deleted = True
        except BaseException as e:
            deleting = e
        self._rt = None
        self.started = False
        client, self._client = self._client, None
        api, self._api = self._api, None
        closing: list[tuple[str, BaseException]] = []
        cancelled: BaseException | None = None  # the first cancellation is raised; the rest ride it as notes
        for name, held in (("SDK client", client), ("API client", api)):
            if held is None:
                continue
            try:
                await held.close()
            except Exception as e:
                closing.append((name, e))
            except BaseException as e:
                if cancelled is None:
                    cancelled = e
                else:
                    closing.append((name, e))
        raised, rest = (deleting if cancelled is None else cancelled), closing
        if raised is None:
            if not closing:
                return
            raised, rest = closing[0][1], closing[1:]
        if deleting is not None and cancelled is not None:  # the cancel outranks it; what it said rides along
            raised.add_note(f"the deletion also failed: {deleting!r}")
        for name, failure in rest:
            raised.add_note(f"closing the {name} also failed: {failure!r}")
        raise raised


def _policy_json(policy: Any) -> dict[str, Any] | None:
    if policy is None:
        return None
    return policy.to_json() if hasattr(policy, "to_json") else dict(policy)


def _reach(policy: Egress | None) -> tuple[str, frozenset[str]] | None:
    """What a policy lets through: its mode and its hosts as a set, since the service may list them in
    another order."""
    return None if policy is None else (policy.mode, frozenset(policy.hosts))


def _sdk_policy(egress: Egress) -> Any:
    from runta import EgressPolicy

    allow = egress.mode == "allowlist"
    hosts = list(egress.hosts)
    return EgressPolicy(
        mode=egress.mode, allowed_hosts=hosts if allow else [], denied_hosts=[] if allow else hosts
    )
