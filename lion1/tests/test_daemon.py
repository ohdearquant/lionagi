"""The `/v2` routes `lion areas --serve` answers for the Mac app: each read's answer, each write's
refusal and its replay under one `once`, the loopback admission in front of them, and that
`daemon.can` names exactly the writes a route serves. No test reaches the network or the keychain:
OpenRouter answers with recorded bodies and `security` is a script that reads its items from a file."""

import http.client
import json
import sys
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import lion_cli.areas as areas_cli
import lion_cli.daemon as daemon
import pytest
from lion_cli.daemon import CAN, agent_ids

from hub.areas import Area
from hub.backend.openrouter import OpenRouterError
from tests.test_areas import (  # noqa: F401 (a fixture)
    FailingKhive,
    call,
    read_events,
    recorder,
    serve_on,
    servers_end,
)

DAY = time.strftime("%Y-%m-%d")
CHORES = (
    "# the resident's own file\n"
    'actor = "agent:x:desk"\n'
    'owner = "agent:x"\n'
    'model = "deepseek/deepseek-v4.1-flash"\n'
    "notes_kept = 3  # a key the app never shows\n"
    "\n"
    "[budget]\n"
    "cost_per_day = 1.5  # dollars, the day's cap\n"
    "# a comment inside the table\n"
    "wakes_per_hour = 6\n"
    "\n"
    "[wake]\n"
    'default = "batch"\n'
    'batch_after = "30m"\n'
    "batch_size = 5\n"
    "quiet = []\n"
    "[[wake.rule]]\n"
    'from = ["agent:x"]\n'
    'then = "now"\n'
)
# two entries of OpenRouter's public model list as it answered, and its documented `/key` answer
LISTED = [
    {
        "id": "deepseek/deepseek-v4.1-flash",
        "name": "DeepSeek: DeepSeek V4.1 Flash",
        "context_length": 1048576,
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        "pricing": {"prompt": "0.000000003", "completion": "0.0000024"},
    },
    {
        "id": "typesafe/jev-router",
        "name": "TypeSafe: Jev Router",
        "context_length": 1000000,
        "architecture": {"input_modalities": ["audio", "file", "image", "text", "video"]},
        "pricing": {"prompt": "-1", "completion": "-1"},
    },
]
KEY = {
    "data": {
        "label": "made-up...890",
        "limit": 100,
        "limit_remaining": 74.5,
        "usage": 25.5,
        "usage_daily": 25.5,
        "usage_weekly": 25.5,
        "usage_monthly": 25.5,
        "is_management_key": False,
    }
}
SECURITY = """#!PYTHON
import json, sys
from pathlib import Path

store, argv = Path("STORE"), Path("ARGV")
with argv.open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
items = json.loads(store.read_text()) if store.exists() else {}


def opt(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def run(args):
    key = f"{opt(args, '-a')}/{opt(args, '-s')}"
    if key not in items:
        return 44
    if "-w" in args:
        print(items[key])
    return 0


sys.exit(run(sys.argv[1:]))
"""


@pytest.fixture(autouse=True)
def keychain(tmp_path, monkeypatch):
    """`security` as a script over a file, and OpenRouter as recorded answers; (items, argv log, asked)."""
    store, argv = tmp_path / "keychain.json", tmp_path / "security.argv"
    script = tmp_path / "security"
    script.write_text(
        SECURITY.replace("PYTHON", sys.executable).replace("STORE", str(store)).replace("ARGV", str(argv))
    )
    script.chmod(0o755)
    monkeypatch.setattr(daemon, "_SECURITY", str(script))
    monkeypatch.delenv("OPENROUTER_KEYCHAIN_ACCOUNT", raising=False)

    def api_key() -> str:  # the backend's lookup, over the same items
        items = json.loads(store.read_text()) if store.exists() else {}
        if "khive/OPENROUTER_API_KEY" not in items:
            raise OpenRouterError("no OpenRouter key in the keychain")
        return items["khive/OPENROUTER_API_KEY"]

    asked: list[tuple[str, dict]] = []

    def get(url: str, headers: dict):
        asked.append((url, headers))
        return {"data": LISTED} if url.endswith("/models") else KEY

    monkeypatch.setattr(daemon, "api_key", api_key)
    monkeypatch.setattr(daemon, "_get", get)
    return store, argv, asked


class Mail:
    """The person's box as comm.inbox answers it; what is sent and marked read, recorded."""

    def __init__(self, unread=(), read=(), sent=()):
        self.actor, self.calls = "person:me", []
        self.unread, self.read, self.sent_box = list(unread), list(read), list(sent)
        self.sends: list[dict] = []
        self.marked: list[list[str]] = []

    async def exec(self, ops: str) -> list:
        if "offset=" in ops:
            return [{"messages": []}]
        box = self.sent_box if 'box="sent"' in ops else self.unread if 'status="unread"' in ops else self.read
        return [{"messages": list(box)}]

    async def send(self, to: str, content: str, **kw) -> dict:
        self.sends.append({"to": to, "content": content, **kw})
        return {"id": f"s{len(self.sends)}"}

    async def mark_read(self, ids: list[str]) -> None:
        self.marked.append(ids)
        self.read = [m for m in self.unread if m["id"] in ids] + self.read
        self.unread = [m for m in self.unread if m["id"] not in ids]


def msg(i: str, sender: str, thread: str, subject: str = "", content: str = "text", at: str = "") -> dict:
    return {
        "id": i,
        "full_id": i,
        "from": sender,
        "subject": subject,
        "content": content,
        "created_at": at or "2026-10-04T10:00:00Z",
        "properties": {"from_actor": sender, "thread_id": thread},
    }


def wake(n: int, sent: int, at: str = "09:00:00") -> str:
    return (
        f"{DAY} {at} wake {n}: Success after 2 turns, 1 mail, {sent} sent; 1 model call(s), 10 in, 5 out, "
        "$0.1250; 4 idle poll(s) before it at 0 tokens"
    )


LOG = [
    "serving agent:x:desk from /x as pid 1; ticks created: []",
    f"{DAY} 08:59:00 took 1 mail",
    wake(1, 1, "08:59:30"),
    f"{DAY} 09:00:00 read the mail",
    wake(2, 0),
]


def resident(
    root: Path, name: str = "desk", chores: str = CHORES, lines: list[str] = (), unknown: int | None = 0
) -> Path:
    """A resident's directory; `unknown` is its day's count of calls of unknown cost, None a day's row
    from before the count."""
    d = root / name
    (d / "notes").mkdir(parents=True)
    (d / "chores.toml").write_text(chores)
    cursor = {"wake": 2, "at": f"{DAY} 09:00:00", "outcome": "Success"}
    spend = {"day": DAY, "runs": 2, "mail": 2, "cost": 0.25, "calls": 3}
    if unknown is not None:
        spend["unknown"] = unknown
    notes = {"cursor": {"key": "cursor", "value": cursor}, "spend": {"key": "spend", "value": spend}}
    (d / "notes" / "agent.json").write_text(json.dumps({"version": 1, "notes": notes}))
    (d / "serve.log").write_text("".join(line + "\n" for line in lines))
    return d


def reg(root: Path, *dirs: Path) -> Path:
    path = root / "areas.toml"
    path.write_text('[area.x]\nchair = "agent:x"\ndesks = [' + ", ".join(f'"{d}"' for d in dirs) + "]\n")
    return path


def once() -> str:
    return uuid.uuid4().hex


def get(port: int, path: str) -> tuple[int, dict]:
    code, text, _ = call(port, "GET", path)
    return code, json.loads(text)


def write(port: int, method: str, path: str, doc: dict, headers: dict | None = None) -> tuple[int, dict]:
    head = {"Content-Type": "application/json", **(headers or {})}
    code, text, _ = call(port, method, path, json.dumps(doc), head)
    return code, json.loads(text)


def unsent(port: int, method: str, path: str, headers: dict | None = None) -> tuple[int, dict]:
    """A write whose head says a body follows that never comes: any answer was given before the body
    was read, since reading it would wait out the timeout."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.putrequest(method, path)
    for k, v in {"Content-Type": "application/json", "Content-Length": "64", **(headers or {})}.items():
        conn.putheader(k, v)
    conn.endheaders()
    r = conn.getresponse()
    doc = json.loads(r.read())
    conn.close()
    return r.status, doc


def peer_is(monkeypatch, addr: str) -> None:
    """Every connection a server accepts from here on reads as coming from `addr`."""
    accept = ThreadingHTTPServer.get_request

    def get_request(server):
        conn, (_, port) = accept(server)
        return conn, (addr, port)

    monkeypatch.setattr(ThreadingHTTPServer, "get_request", get_request)


# ---- reads ----


def test_today_counts_each_resident_and_names_what_this_server_does(tmp_path):
    d = resident(tmp_path, lines=LOG)
    port = serve_on(reg(tmp_path, d), ("person:me", Mail(unread=[msg("u1", "agent:x:desk", "t1")])))
    code, doc = get(port, "/v2/today")
    assert code == 200 and doc["date"] == DAY
    assert doc["totals"] == {"running": 0, "wakes": 2, "acted": 1, "cost": 0.25, "cap": 1.5}
    assert doc["for_you"] == {"open": 1}
    (area,) = doc["areas"]
    assert (area["name"], area["chair"], area["state"]) == ("x", None, "stopped")
    assert [a["id"] for a in area["agents"]] == ["x/desk"] and area["today"] == {"cost": 0.25, "wakes": 2}
    assert doc["daemon"]["can"] == list(CAN) and doc["daemon"]["version"]
    # no identity: the person's mail is not read, so its count is unknown, never 0
    port = serve_on(reg(tmp_path, d))
    assert get(port, "/v2/today")[1]["for_you"] == {"open": None}


def test_an_area_answers_its_chair_its_agents_and_a_timeline_newest_first(tmp_path):
    d = resident(tmp_path, lines=LOG)
    chair = resident(tmp_path, "chair", CHORES.replace('"agent:x:desk"', '"agent:x"'))
    later = time.time() + 2 * 86400
    (d / "ledger.jsonl").write_text(
        json.dumps({"ts": later, "chore": "disk", "escalate": True, "findings": ["full"]}) + "\nnot json\n"
    )
    row = {"utc": "2026-10-04T08:00:00.123Z", "class": "escalated", "from": "agent:y", "subject": "s"}
    (d / "desk.jsonl").write_text(json.dumps(row) + "\n")
    port = serve_on(reg(tmp_path, d, chair))
    code, doc = get(port, "/v2/areas/x")
    assert code == 200 and doc["chair"]["id"] == "x/chair" and doc["chair"]["kind"] == "chair"
    assert [a["id"] for a in doc["agents"]] == ["x/desk"]
    timeline = doc["timeline"]
    assert [t["at"] for t in timeline] == sorted((t["at"] for t in timeline), reverse=True)
    assert timeline[0] == {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(later)),
        "agent": "x/desk",
        "text": "escalated disk: full",
        "raised": True,
    }
    assert {"at": "2026-10-04T08:00:00Z", "agent": "x/desk", "text": "escalated mail from agent:y: s"} in [
        {k: t[k] for k in ("at", "agent", "text")} for t in timeline if t["raised"]
    ]
    assert sorted(t["text"][:6] for t in timeline if not t["raised"]) == ["wake 1", "wake 2"]
    assert get(port, "/v2/areas/none")[0] == 404


def test_an_agent_answers_its_row_its_definition_and_what_the_app_may_change(tmp_path):
    d = resident(tmp_path, lines=LOG)
    port = serve_on(reg(tmp_path, d))
    code, doc = get(port, "/v2/agents/x/desk")
    assert code == 200 and get(port, "/v2/agents/x%2Fdesk") == (200, doc)
    row = doc["row"]
    assert (row["id"], row["name"], row["kind"], row["state"]) == (
        "x/desk",
        "agent:x:desk",
        "agent",
        "stopped",
    )
    assert row["model"] == "openrouter/deepseek/deepseek-v4.1-flash" and row["runs_in"] == "local"
    assert row["today"] == {"cost": 0.25, "wakes": 2, "acted": 1, "calls": 3}
    assert "mail from agent:x arrives: wake now" in row["woken_by"]
    assert "other admitted mail arrives: collect it for the window" in row["woken_by"]
    definition = doc["definition"]
    assert definition["limits"] == {
        "cost_per_day": 1.5,
        "turns_per_wake": 6,
        "time_per_wake_s": None,
        "fold_at_tokens": None,
        "escalates_to": "agent:x",
    }
    rule = definition["wake"]["rules"][0]
    assert rule["source"] == {"kind": "mail", "from": ["agent:x"]} and rule["deliver"] == {"kind": "now"}
    default = definition["wake"]["rules"][1]
    assert default["deliver"] == {"kind": "collect", "items": 5, "minutes": 30}
    assert (definition["wake"]["at_most_per_hour"], definition["wake"]["quiet"]) == (6, None)
    assert definition["plugins"] == [{"name": "lion", "items": ["check", "digest", "aged"]}]
    assert definition["shell_rules"] is None and "tools" not in definition
    assert doc["editable"] == ["model", "limits.cost_per_day", "wake.at_most_per_hour", "wake.quiet"]
    assert doc["refused"] is None
    assert doc["files"] == {"instance": str(d / "chores.toml")}
    assert get(port, "/v2/agents/x/none")[0] == 404
    # a file its own loaders refuse: the row says so, and there is no definition to edit
    (d / "chores.toml").write_text(CHORES.replace('default = "batch"', 'default = "later"'))
    doc = get(port, "/v2/agents/x/desk")[1]
    assert doc["definition"] is None and doc["editable"] == []
    assert doc["row"]["woken_by"][0].startswith("chores.toml refused: [wake] default 'later'")


def test_two_desks_of_one_area_with_one_name_get_ids_that_tell_them_apart():
    ids = [i for i, _, _ in agent_ids([Area("x", "c", ["/a/desk", "/b/desk", "/c/other"])])]
    assert ids[2] == "x/other" and ids[0].startswith("x/desk~") and len(set(ids)) == 3


def test_wakes_page_newest_first_and_the_record_holds_one_wakes_lines(tmp_path):
    port = serve_on(reg(tmp_path, resident(tmp_path, lines=LOG)))
    code, doc = get(port, "/v2/agents/x/desk/wakes?limit=1")
    assert code == 200 and doc["more"] is True
    (w,) = doc["wakes"]
    assert (w["id"], w["outcome"], w["turns"], w["cost"], w["live"], w["commands"]) == (
        "2",
        "nothing",
        2,
        0.125,
        False,
        [],
    )
    assert w["source"] == {"kind": "mail", "phrase": "1 mail"} and w["text"].startswith("wake 2: Success")
    assert [x["outcome"] for x in get(port, "/v2/agents/x/desk/wakes?before=2")[1]["wakes"]] == ["acted"]
    code, doc = get(port, "/v2/agents/x/desk/record?wake=2")
    assert code == 200 and [r["line"] for r in doc["rows"]] == LOG[3:5]
    assert [r["seq"] for r in get(port, "/v2/agents/x/desk/record?after=3")[1]["rows"]] == [4]
    assert get(port, "/v2/agents/x/desk/wakes?limit=0")[0] == 400
    assert get(port, "/v2/agents/x/desk/record?wake=9")[0] == 404


def test_an_agents_partial_cost_is_unknown_and_so_is_every_sum_over_it(tmp_path):
    whole = resident(tmp_path)
    port = serve_on(reg(tmp_path, whole, resident(tmp_path, "partial", unknown=1)))
    doc = get(port, "/v2/today")[1]
    (area,) = doc["areas"]
    assert {a["id"]: a["today"]["cost"] for a in area["agents"]} == {"x/desk": 0.25, "x/partial": None}
    assert (doc["totals"]["cost"], area["today"]["cost"]) == (None, None)
    assert get(port, "/v2/areas/x")[1]["today"]["cost"] is None
    assert get(port, "/v2/agents/x/partial")[1]["row"]["today"]["cost"] is None
    # a day's row from before the count is partial by a number nobody knows
    port = serve_on(reg(tmp_path, whole, resident(tmp_path, "older", unknown=None)))
    assert get(port, "/v2/today")[1]["totals"]["cost"] is None
    port = serve_on(reg(tmp_path, whole, resident(tmp_path, "counted")))
    doc = get(port, "/v2/today")[1]
    assert (doc["totals"]["cost"], doc["areas"][0]["today"]["cost"]) == (0.5, 0.5)


def test_the_days_cap_is_unknown_when_a_residents_budget_is_refused(tmp_path):
    other = resident(tmp_path, "other", CHORES.replace("cost_per_day = 1.5", 'cost_per_day = "lots"'))
    port = serve_on(reg(tmp_path, resident(tmp_path), other))
    assert get(port, "/v2/today")[1]["totals"]["cap"] is None  # Budget.load refuses "lots"
    (other / "chores.toml").write_text(CHORES.replace("cost_per_day = 1.5", "cost_per_day = 2"))
    assert get(port, "/v2/today")[1]["totals"]["cap"] == 3.5
    (other / "chores.toml").write_text("[budget\n")  # a file the resident cannot read at all
    assert get(port, "/v2/today")[1]["totals"]["cap"] is None


def test_a_wake_that_missed_calls_of_unknown_cost_has_no_cost(tmp_path):
    partial = wake(3, 1, "09:30:00").replace("$0.1250;", "$0.1250 (partial: 1 call(s) of unknown cost);")
    port = serve_on(reg(tmp_path, resident(tmp_path, lines=[*LOG, partial])))
    wakes = get(port, "/v2/agents/x/desk/wakes")[1]["wakes"]
    assert [(w["id"], w["cost"]) for w in wakes] == [("3", None), ("2", 0.125), ("1", 0.125)]


def test_for_you_lists_the_persons_unread_mail_newest_first_and_names_its_agent(tmp_path):
    d = resident(tmp_path)
    mail = Mail(
        unread=[
            msg("u1", "agent:x:desk", "t1", "report"),
            msg("u2", "agent:y", "t2", content="first line\nmore", at="2026-10-04T11:00:00Z"),
        ],
        read=[msg("r1", "agent:y", "t3", "already read")],
        sent=[msg("s0", "person:me", "t1")],
    )
    port = serve_on(reg(tmp_path, d), ("person:me", mail))
    code, doc = get(port, "/v2/foryou")
    assert code == 200
    assert [(i["id"], i["open"], i["kind"]) for i in doc["items"]] == [
        ("u2", True, "note"),
        ("u1", True, "reply"),
    ]  # the read message is not listed
    assert doc["items"][1]["from"] == {"agent": "x/desk", "area": "x"}
    assert doc["items"][0]["from"] == {"agent": "agent:y", "area": None}
    assert doc["items"][0]["title"] == "first line" and all(i["choices"] == [] for i in doc["items"])
    port = serve_on(reg(tmp_path, d))  # no identity: no mail is read
    assert get(port, "/v2/foryou") == (200, {"items": []})


def test_the_for_you_count_is_the_open_items_and_null_when_the_mail_is_not_read(tmp_path):
    d = resident(tmp_path)
    unread = [msg("u1", "agent:x:desk", "t1"), msg("u2", "agent:y", "t2")]
    mail = Mail(unread=unread, read=[msg("r1", "agent:y", "t3")])
    port = serve_on(reg(tmp_path, d), ("person:me", mail))
    count, items = get(port, "/v2/today")[1]["for_you"], get(port, "/v2/foryou")[1]["items"]
    assert len(items) == 2 and count == {"open": sum(1 for i in items if i["open"])} == {"open": 2}
    port = serve_on(reg(tmp_path, d))  # no identity
    assert get(port, "/v2/today")[1]["for_you"] == {"open": None}
    port = serve_on(reg(tmp_path, d), ("person:me", FailingKhive()))  # the mail cannot be read
    assert get(port, "/v2/today")[1]["for_you"] == {"open": None} and get(port, "/v2/foryou")[0] == 502


def test_plugins_answer_the_default_plugin_and_the_old_library_routes_are_gone(tmp_path):
    port = serve_on(reg(tmp_path, resident(tmp_path)))
    code, doc = get(port, "/v2/plugins")
    assert code == 200 and doc["marketplaces"] == [] and doc["shell_rules"] == []
    (plugin,) = doc["plugins"]
    assert (plugin["name"], plugin["source"], plugin["kind"], plugin["on"]) == (
        "lion",
        "default",
        "plugin",
        True,
    )
    assert plugin["skills"] == [] and plugin["not_used"] == [] and plugin["given_to"] == ["x/desk"]
    assert {a["name"]: a["in_use"] for a in plugin["agents"]} == {
        "chores": 1,
        "reader": 0,
        "chair": 0,
        "console": 0,
    }
    tools = {t["name"]: t for t in plugin["tools"]}
    assert {
        "read",
        "edit",
        "bash",
        "khive_ops",
        "web_fetch",
        "criteria",
        "note.get",
        "send",
        "check",
        "sweep",
    } <= set(tools)
    assert (tools["bash"]["group"], tools["bash"]["effect"], tools["bash"]["kind"]) == (
        "shell",
        "write",
        "function",
    )
    assert tools["web_fetch"]["effect"] == "read"
    assert any(a["name"] == "command" and a["required"] for a in tools["bash"]["args"])
    assert get(port, "/v2/plugins/lion") == (200, plugin)
    for path in ("/v2/plugins/other", "/v2/library/tools", "/v2/library/servers", "/v2/library/plugins"):
        assert get(port, path)[0] == 404, path
    code, doc = get(port, "/v2/profiles")
    assert code == 200 and [p["name"] for p in doc["profiles"]] == ["chores", "reader", "chair", "console"]


def test_the_library_lists_the_providers_and_every_model_added_used_or_run_by_default(tmp_path):
    chores = CHORES.replace('model = "deepseek/deepseek-v4.1-flash"', 'model = "codex/gpt-x"')
    port = serve_on(reg(tmp_path, resident(tmp_path, chores=chores)))
    code, doc = get(port, "/v2/library/models")
    assert code == 200 and [p["name"] for p in doc["providers"]] == ["claude_code", "codex", "openrouter"]
    openrouter = doc["providers"][2]
    assert (openrouter["state"], openrouter["key"], openrouter["limits"]) == ("off", {"set": False}, None)
    models = {m["id"]: m for m in doc["models"]}
    assert models["codex/gpt-x"]["given_to"] == ["x/desk"] and models["codex/gpt-x"]["on_plan"] is True
    default = models["openrouter/deepseek/deepseek-v4.1-flash"]
    assert default["default_for_new"] is True and default["given_to"] == []
    assert default["price"] is None and default["images"] is None  # no list of OpenRouter's kept yet


def test_the_catalogue_and_the_usage_are_openrouters_own_recorded_answers(tmp_path, keychain):
    store, _, asked = keychain
    port = serve_on(reg(tmp_path, resident(tmp_path)))
    code, doc = get(port, "/v2/library/providers/openrouter/catalogue?q=deepseek")
    assert code == 200 and doc["fetched_at"].endswith("Z")
    assert doc["models"] == [
        {
            "model_id": "deepseek/deepseek-v4.1-flash",
            "name": "DeepSeek: DeepSeek V4.1 Flash",
            "context": 1048576,
            "price": {"in": 0.003, "out": 2.4},
            "takes": ["text", "image"],
        }
    ]
    router = get(port, "/v2/library/providers/openrouter/catalogue")[1]["models"][1]
    assert router["model_id"] == "typesafe/jev-router" and router["price"] is None  # a router's -1: not fixed
    assert sum(url.endswith("/models") for url, _ in asked) == 1  # kept for a day, not fetched again
    default = next(m for m in get(port, "/v2/library/models")[1]["models"] if m["default_for_new"])
    assert (default["price"], default["context"], default["images"]) == (
        {"in": 0.003, "out": 2.4},
        1048576,
        True,
    )
    code, doc = get(port, "/v2/library/providers/openrouter/usage")
    assert code == 409 and "no OpenRouter key" in doc["errors"][0]["message"]
    store.write_text(json.dumps({"khive/OPENROUTER_API_KEY": "made-up-key"}))
    code, doc = get(port, "/v2/library/providers/openrouter/usage")
    assert code == 200 and {k: v for k, v in doc.items() if k != "fetched_at"} == {
        "credits_left": 74.5,
        "used_today": 25.5,
        "used_month": 25.5,
        "log": [],
    }
    assert asked[-1] == (daemon._OPENROUTER_KEY, {"Authorization": "Bearer made-up-key"})
    for path in ("codex/catalogue", "codex/usage"):
        assert get(port, f"/v2/library/providers/{path}")[0] == 404


def test_events_send_today_then_each_new_wake_and_a_change_of_the_library_or_the_mail(tmp_path, monkeypatch):
    monkeypatch.setattr(areas_cli, "_FORYOU_S", 0.2)
    monkeypatch.setattr(areas_cli, "_PING_S", 0.5)
    d = resident(tmp_path, lines=LOG)
    mail = Mail()
    port = serve_on(reg(tmp_path, d), ("person:me", mail))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/v2/events")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    got = read_events(resp, lambda ev: any(e == "today" for e, _ in ev), 5)
    today = next(x for e, x in got if e == "today")
    assert today["totals"]["wakes"] == 2 and today["for_you"] == {"open": 0}
    # a ping ends a pass that has read serve.log once; a line written before that read is history
    read_events(resp, lambda ev: any(e == "ping" for e, _ in ev), 5)
    with open(d / "serve.log", "a") as f:
        f.write(wake(3, 1, "09:30:00") + "\n")
    got = read_events(resp, lambda ev: any(e == "wake" for e, _ in ev), 5)
    (new,) = [x for e, x in got if e == "wake"]  # the log's earlier wakes are history, not sent
    assert new["agent"] == "x/desk" and (new["wake"]["id"], new["wake"]["outcome"]) == ("3", "acted")
    (tmp_path / "models.toml").write_text("")
    got = read_events(resp, lambda ev: ("library", {"what": "models"}) in ev, 5)
    mail.unread.append(msg("u9", "agent:x:desk", "t9", "new"))
    got = read_events(resp, lambda ev: any(e == "foryou" for e, _ in ev), 5)
    assert [x["id"] for e, x in got if e == "foryou"] == ["u9"]
    mail.read.insert(0, mail.unread.pop())  # read elsewhere: it leaves the list, said once
    got = read_events(resp, lambda ev: any(e == "foryou" for e, _ in ev), 5)
    assert [(x["id"], x["open"]) for e, x in got if e == "foryou"] == [("u9", False)]
    got = read_events(resp, lambda ev: any(e == "ping" for e, _ in ev), 3)
    conn.close()


# ---- writes ----


def test_a_definition_write_changes_its_keys_and_keeps_every_other_byte_and_a_replay_writes_nothing(tmp_path):
    d = resident(tmp_path)
    (d / "chores.toml").chmod(0o640)
    port = serve_on(reg(tmp_path, d))
    path = "/v2/agents/x/desk/definition"
    want = {"limits": {"cost_per_day": 3}, "wake": {"quiet": {"from": "22:00", "to": "06:00"}}}
    code, doc = write(port, "PUT", path, {"definition": want, "dry_run": True, "once": once()})
    assert code == 200 and doc["needs_restart"] is False
    assert doc["changes"] == [
        {"path": "limits.cost_per_day", "old": 1.5, "new": 3},
        {"path": "wake.quiet", "old": None, "new": {"from": "22:00", "to": "06:00"}},
    ]
    assert (d / "chores.toml").read_text() == CHORES  # a dry run writes nothing
    token = once()
    first = write(port, "PUT", path, {"definition": want, "once": token})
    assert first[0] == 200 and first[1]["changes"] == doc["changes"]
    written = CHORES.replace("cost_per_day = 1.5  #", "cost_per_day = 3.0  #").replace(
        "quiet = []", 'quiet = ["22:00-06:00"]'
    )
    assert (d / "chores.toml").read_text() == written  # the comments and `notes_kept` kept
    assert (d / "chores.toml").stat().st_mode & 0o777 == 0o640
    (d / "chores.toml").write_text(written.replace("3.0", "1.0"))  # an edit from elsewhere after it
    assert write(port, "PUT", path, {"definition": want, "once": token}) == first  # the first answer again
    assert "cost_per_day = 1.0  #" in (d / "chores.toml").read_text()  # and nothing written twice
    # the model is one of the library's, and the resident reads it at its start
    (tmp_path / "models.toml").write_text('[model."codex/gpt-x"]\nthrough = "codex"\nmodel_id = "gpt-x"\n')
    code, doc = write(port, "PUT", path, {"definition": {"model": "codex/gpt-x"}, "once": once()})
    assert code == 200 and doc["needs_restart"] is True
    assert 'model = "codex/gpt-x"\n' in (d / "chores.toml").read_text()


def test_a_definition_the_loaders_or_the_app_shape_refuse_leaves_the_file_byte_identical(tmp_path):
    d = resident(tmp_path)
    raw = (d / "chores.toml").read_bytes()
    port = serve_on(reg(tmp_path, d))
    path = "/v2/agents/x/desk/definition"
    for want, where in [
        ({"wake": {"quiet": {"from": "25:00", "to": "07:00"}}}, None),  # Wake.load: hours 00-23
        ({"wake": {"quiet": "23:00"}}, "wake.quiet"),
        ({"limits": {"turns_per_wake": 9}}, "limits.turns_per_wake"),
        ({"limits": {"cost_per_day": -1}}, "limits.cost_per_day"),
        ({"wake": {"at_most_per_hour": 0}}, "wake.at_most_per_hour"),
        ({"model": "openrouter/not/added"}, "model"),
        ({"model": "elsewhere/x"}, "model"),
        ({"instructions": {"extra": "be brief"}}, "instructions.extra"),  # only a chair carries its own
        ({"nope": 1}, "nope"),
    ]:
        code, doc = write(port, "PUT", path, {"definition": want, "once": once()})
        assert code == 400 and doc["ok"] is False, want
        assert doc["errors"][0].get("path") == where, doc
        assert (d / "chores.toml").read_bytes() == raw
    assert write(port, "PUT", path, {"definition": "x", "once": once()})[0] == 400
    assert write(port, "PUT", "/v2/agents/x/none/definition", {"definition": {}, "once": once()})[0] == 404
    (d / "chores.toml").write_text(CHORES.replace('default = "batch"', 'default = "later"'))
    raw = (d / "chores.toml").read_bytes()
    code, doc = write(port, "PUT", path, {"definition": {"limits": {"cost_per_day": 2}}, "once": once()})
    assert code == 409 and (d / "chores.toml").read_bytes() == raw
    # a `budget` that is no table: its loader fails on it as on no value it names
    (d / "chores.toml").write_text("budget = 3\n" + CHORES.replace("[budget]", "[budgets]"))
    raw = (d / "chores.toml").read_bytes()
    code, doc = get(port, "/v2/agents/x/desk")
    assert code == 200 and doc["definition"] is None and doc["editable"] == [] and doc["refused"]
    assert get(port, "/v2/today")[1]["totals"]["cap"] is None
    code, doc = write(port, "PUT", path, {"definition": {"limits": {"cost_per_day": 2}}, "once": once()})
    assert code == 409 and (d / "chores.toml").read_bytes() == raw
    # a table only the start's checks refuse: the read says so, as the write of it would
    (d / "chores.toml").write_text(CHORES + "\n[claude_code]\nmax_turns = 3\n")
    raw = (d / "chores.toml").read_bytes()
    code, doc = get(port, "/v2/agents/x/desk")
    assert code == 200 and doc["definition"] is None and doc["editable"] == []
    assert "claude_code" in doc["refused"]
    for want in ({}, {"limits": {"cost_per_day": 2}}):  # an unchanged write is refused as a changed one
        code, doc = write(port, "PUT", path, {"definition": want, "once": once()})
        assert code == 409 and "refused by its own loaders" in doc["errors"][0]["message"]
        assert (d / "chores.toml").read_bytes() == raw


def test_a_definition_saved_elsewhere_after_the_read_is_refused_409_and_left_as_saved(tmp_path, monkeypatch):
    d = resident(tmp_path)
    port = serve_on(reg(tmp_path, d))
    theirs = CHORES.replace("wakes_per_hour = 6", "wakes_per_hour = 4  # saved in an editor")
    loader = daemon.codex_options

    def saved_meanwhile(table):  # the last loader the host runs before its compare: an editor saves first
        (d / "chores.toml").write_text(theirs)
        return loader(table)

    monkeypatch.setattr(daemon, "codex_options", saved_meanwhile)
    want = {"definition": {"limits": {"cost_per_day": 3}}, "once": once()}
    code, doc = write(port, "PUT", "/v2/agents/x/desk/definition", want)
    assert code == 409 and doc["errors"][0]["message"].startswith("chores.toml changed while this was read")
    assert (d / "chores.toml").read_text() == theirs


def test_control_holds_and_resumes_answers_the_row_and_refuses_what_it_does_not_serve(tmp_path):
    d = resident(tmp_path)
    port = serve_on(reg(tmp_path, d))
    path = "/v2/agents/x/desk/control"
    token = once()
    first = write(port, "POST", path, {"action": "hold", "reason": "lunch", "once": token})
    assert first[0] == 200 and first[1]["row"]["id"] == "x/desk" and "lunch" in (d / "hold").read_text()
    assert write(port, "POST", path, {"action": "hold", "reason": "other", "once": token}) == first
    assert "lunch" in (d / "hold").read_text()
    assert write(port, "POST", path, {"action": "resume", "once": once()})[0] == 200
    assert not (d / "hold").exists()
    for action in ("stop", "cap", ""):
        code, doc = write(port, "POST", path, {"action": action, "once": once()})
        assert code == 400 and doc["errors"][0]["path"] == "action"
    code, doc = write(port, "POST", path, {"action": "restart", "once": once()})
    assert code == 409 and doc["errors"][0]["message"].startswith("no launchd plist")
    assert write(port, "POST", "/v2/agents/x/none/control", {"action": "hold", "once": once()})[0] == 404


def test_a_message_is_mail_to_the_agents_actor_sent_once_per_token(tmp_path):
    d = resident(tmp_path)
    mail = Mail()
    port = serve_on(reg(tmp_path, d), ("person:me", mail))
    path = "/v2/agents/x/desk/message"
    token = uuid.uuid4()
    first = write(port, "POST", path, {"text": "hi", "once": str(token)})
    assert first == (200, {"ok": True, "id": "s1"})
    assert mail.sends == [{"to": "agent:x:desk", "content": "hi", "idempotency_key": f"ui:{token.hex}"}]
    assert (
        write(port, "POST", path, {"text": "hi", "once": token.hex}) == first
    )  # a UUID's hyphens: one token
    for body, where in [
        ({"text": "hi", "images": [{"mime": "image/png", "data": ""}]}, "images"),
        ({"text": " "}, "text"),
    ]:
        code, doc = write(port, "POST", path, {**body, "once": once()})
        assert code == 400 and doc["errors"][0]["path"] == where
    assert len(mail.sends) == 1
    port = serve_on(reg(tmp_path, d))  # no identity: nothing is sent as nobody
    assert write(port, "POST", path, {"text": "hi", "once": once()})[0] == 409


def test_an_answer_goes_back_to_the_asker_on_its_thread_and_marks_the_item_done(tmp_path):
    d = resident(tmp_path)
    unread = [msg("u1", "agent:x:desk", "t1", "report"), msg("u2", "agent:y", "t2")]
    mail = Mail(unread=unread, read=[msg("r1", "agent:y", "t3")])
    port = serve_on(reg(tmp_path, d), ("person:me", mail))
    token = once()
    first = write(port, "POST", "/v2/foryou/u1/answer", {"text": "thanks", "once": token})
    assert first[0] == 200 and first[1]["item"]["id"] == "u1" and first[1]["item"]["open"] is False
    assert mail.sends == [
        {
            "to": "agent:x:desk",
            "content": "thanks",
            "subject": "Re: report",
            "thread_id": "t1",
            "idempotency_key": "ui:" + token,
        }
    ]
    assert mail.marked == [["u1"]]
    assert write(port, "POST", "/v2/foryou/u1/answer", {"text": "thanks", "once": token}) == first
    assert len(mail.sends) == 1
    code, doc = write(port, "POST", "/v2/foryou/u2/answer", {"choice": "yes", "once": once()})
    assert code == 400 and doc["errors"][0]["path"] == "choice"
    assert write(port, "POST", "/v2/foryou/u2/answer", {"text": "", "once": once()})[0] == 400
    for gone in ("u1", "r1", "zz"):  # answered, read before, never there: not listed
        assert write(port, "POST", f"/v2/foryou/{gone}/answer", {"text": "x", "once": once()})[0] == 404
    assert mail.marked == [["u1"]] and len(mail.sends) == 1


def test_a_done_marks_the_item_read_and_it_leaves_the_list_and_the_count(tmp_path):
    unread = [msg("u1", "agent:x:desk", "t1"), msg("u2", "agent:y", "t2")]
    mail = Mail(unread=unread, read=[msg("r1", "agent:y", "t3")])
    port = serve_on(reg(tmp_path, resident(tmp_path)), ("person:me", mail))
    assert [i["id"] for i in get(port, "/v2/foryou")[1]["items"]] == ["u1", "u2"]
    assert get(port, "/v2/today")[1]["for_you"] == {"open": 2}
    token = once()
    assert write(port, "POST", "/v2/foryou/u1/done", {"once": token}) == (200, {"ok": True})
    assert mail.marked == [["u1"]]
    assert [i["id"] for i in get(port, "/v2/foryou")[1]["items"]] == ["u2"]
    assert get(port, "/v2/today")[1]["for_you"] == {"open": 1}
    assert write(port, "POST", "/v2/foryou/u1/done", {"once": token}) == (200, {"ok": True})  # a replay
    for gone in ("u1", "r1", "zz"):  # done, read before, never there: not listed
        code, doc = write(port, "POST", f"/v2/foryou/{gone}/done", {"once": once()})
        assert code == 404 and doc["errors"][0]["message"].startswith(f"no item {gone!r}"), gone
    assert mail.marked == [["u1"]]


def test_a_model_is_added_only_when_its_provider_lists_it_and_removed_only_when_unused(tmp_path):
    port = serve_on(reg(tmp_path, resident(tmp_path)))
    path, added = "/v2/library/models", "openrouter/typesafe/jev-router"
    body = {"through": "openrouter", "model_id": "typesafe/jev-router"}
    code, doc = write(port, "POST", path, {**body, "dry_run": True, "once": once()})
    assert code == 200 and doc["check"] is None and doc["model"]["id"] == added
    assert not (tmp_path / "models.toml").exists()  # a dry run writes nothing
    token = once()
    first = write(port, "POST", path, {**body, "shown_as": "Router", "once": token})
    assert first[0] == 200 and first[1]["model"]["shown_as"] == "Router"
    assert write(port, "POST", path, {**body, "shown_as": "Router", "once": token}) == first
    assert write(port, "POST", path, {**body, "once": once()})[0] == 409  # already there
    for bad, where in [
        ({"through": "openrouter", "model_id": "no/such"}, "model_id"),
        ({"through": "elsewhere", "model_id": "x"}, "through"),
        ({**body, "limits": {"daily": 1}}, "limits"),
        ({**body, "shown_as": 3}, "shown_as"),
    ]:
        code, doc = write(port, "POST", path, {**bad, "once": once()})
        assert code == 400 and doc["errors"][0]["path"] == where, bad
    code, doc = write(port, "PATCH", f"{path}/{added}", {"shown_as": "R2", "once": once()})
    assert code == 200 and doc["model"]["shown_as"] == "R2"
    assert write(port, "PATCH", f"{path}/{added}", {"price": 1, "once": once()})[0] == 400
    code, doc = write(port, "DELETE", f"{path}/openrouter/deepseek/deepseek-v4.1-flash", {"once": once()})
    assert code == 409 and "x/desk" in doc["errors"][0]["message"]  # in use: named, not removed
    assert write(port, "DELETE", f"{path}/{added}", {"once": once()}) == (200, {"ok": True})
    assert added not in {m["id"] for m in get(port, path)[1]["models"]}
    assert write(port, "DELETE", f"{path}/{added}", {"once": once()})[0] == 404


def test_no_route_writes_or_removes_a_key_and_no_answer_carries_one(tmp_path, keychain):
    store, argv, _ = keychain
    secret = "qzx7vvk3-not-issued-9wq2"  # made up: no provider issued it
    store.write_text(json.dumps({"khive/OPENROUTER_API_KEY": secret}))
    port = serve_on(reg(tmp_path, resident(tmp_path, lines=LOG)), ("person:me", Mail()))
    for name in ("openrouter", "codex", "other"):
        for method in ("PUT", "DELETE"):
            code, doc = unsent(port, method, f"/v2/library/providers/{name}/key")
            assert code == 404 and doc["errors"][0]["message"].startswith("no route"), (method, name)
    assert json.loads(store.read_text()) == {"khive/OPENROUTER_API_KEY": secret}
    parts = {secret[i : i + 8] for i in range(len(secret) - 7)} | {secret.encode().hex()}
    head = {"Content-Type": "application/json"}
    code, text, _ = call(
        port, "POST", "/v2/library/providers/openrouter/check", json.dumps({"once": once()}), head
    )
    doc = json.loads(text)
    assert code == 200 and doc["provider"]["state"] == "ok" and doc["provider"]["key"] == {"set": True}
    assert "74.5" in doc["provider"]["detail"] and not [p for p in parts if p in text]
    reads = [
        "/v2/today",
        "/v2/areas/x",
        "/v2/agents/x/desk",
        "/v2/agents/x/desk/wakes",
        "/v2/agents/x/desk/record",
        "/v2/foryou",
        "/v2/plugins",
        "/v2/plugins/lion",
        "/v2/library/models",
        "/v2/library/providers/openrouter/catalogue",
        "/v2/library/providers/openrouter/usage",
        "/v2/profiles",
    ]
    for read in reads:
        code, text, _ = call(port, "GET", read)
        assert code == 200 and not [p for p in parts if p in text], read
    assert get(port, "/v2/library/models")[1]["providers"][2]["key"] == {"set": True}
    asked = [json.loads(line) for line in argv.read_text().splitlines()]
    assert asked and not [a for a in asked if "-w" in a]  # whether it is set, asked without its value


def test_a_codex_check_reads_the_clis_release_and_claude_code_has_no_check(tmp_path, monkeypatch):
    cli = tmp_path / "codex"
    cli.write_text('#!/bin/sh\necho "codex-cli 0.156.1"\n')
    cli.chmod(0o755)
    monkeypatch.setattr(daemon.shutil, "which", lambda name: str(cli) if name == "codex" else None)
    port = serve_on(reg(tmp_path, resident(tmp_path)))
    path = "/v2/library/providers/codex/check"
    code, doc = write(port, "POST", path, {"once": once()})
    assert code == 200 and doc["provider"]["state"] == "ok" and "0.156.1" in doc["provider"]["detail"]
    cli.write_text('#!/bin/sh\necho "codex-cli 9.9.9"\n')
    code, doc = write(port, "POST", path, {"once": once()})
    assert doc["provider"]["state"] == "warn" and "every wake on it is refused" in doc["provider"]["detail"]
    cli.write_text("#!/bin/sh\nexit 3\n")
    assert write(port, "POST", path, {"once": once()})[1]["provider"]["state"] == "warn"
    assert write(port, "POST", "/v2/library/providers/claude_code/check", {"once": once()})[0] == 404


def test_open_opens_only_under_a_directory_the_registry_names(tmp_path, monkeypatch):
    record = tmp_path / "opened"
    monkeypatch.setattr(daemon, "_OPEN", str(recorder(tmp_path / "open", record, 0)))
    d = resident(tmp_path)
    port = serve_on(reg(tmp_path, d))
    token = once()
    first = write(port, "POST", "/v2/open", {"path": str(d / "chores.toml"), "with": "editor", "once": token})
    assert first == (200, {"ok": True})
    assert record.read_text().split() == ["-t", str((d / "chores.toml").resolve())]
    assert (
        write(port, "POST", "/v2/open", {"path": str(d / "chores.toml"), "with": "editor", "once": token})
        == first
    )
    assert write(port, "POST", "/v2/open", {"path": str(d), "with": "finder", "once": once()})[0] == 200
    assert record.read_text().split() == ["-t", str((d / "chores.toml").resolve()), "-R", str(d.resolve())]
    for body, code in [
        ({"path": str(tmp_path / "areas.toml"), "with": "editor"}, 404),  # the registry is no desk
        ({"path": str(d / ".." / ".." / "x"), "with": "editor"}, 404),
        ({"path": str(d / "none"), "with": "editor"}, 404),
        ({"path": str(d), "with": "editor"}, 400),
        ({"path": str(d), "with": "terminal"}, 400),
    ]:
        assert write(port, "POST", "/v2/open", {**body, "once": once()})[0] == code, body
    assert len(record.read_text().split()) == 4


# ---- admission and what `can` says ----


def test_every_v2_write_takes_a_well_formed_once_and_without_one_does_nothing(tmp_path):
    d = resident(tmp_path)
    port = serve_on(reg(tmp_path, d))
    path, body = "/v2/agents/x/desk/definition", {"definition": {"limits": {"cost_per_day": 9}}}
    for token in (None, "", "not-hex", "0" * 31):
        code, doc = write(port, "PUT", path, body if token is None else {**body, "once": token})
        assert code == 400 and doc["errors"][0]["path"] == "once", token
    assert (d / "chores.toml").read_text() == CHORES


def test_v2_refuses_a_peer_off_loopback_before_it_reads_or_does_anything(tmp_path, monkeypatch):
    d = resident(tmp_path)
    port = serve_on(reg(tmp_path, d))
    assert get(port, "/v2/today")[0] == 200  # from loopback
    peer_is(monkeypatch, "192.168.1.20")
    code, doc = get(port, "/v2/today")
    assert code == 403 and doc["errors"][0]["message"].startswith("192.168.1.20 is not loopback")
    code, doc = unsent(port, "PUT", "/v2/agents/x/desk/definition")
    assert code == 403 and (d / "chores.toml").read_text() == CHORES


def test_v2_refuses_a_tailnet_peer_the_older_routes_admit(tmp_path, monkeypatch):
    port = serve_on(reg(tmp_path, resident(tmp_path)))
    peer_is(monkeypatch, "100.64.0.1")
    assert call(port, "GET", "/api/areas")[0] == 200
    code, doc = get(port, "/v2/today")
    assert code == 403 and doc["errors"][0]["message"].startswith("100.64.0.1 is not loopback")


def test_v2_refuses_a_host_name_added_with_allow_that_the_older_routes_answer(tmp_path):
    port = serve_on(reg(tmp_path, resident(tmp_path)), allow=["device.example.net"])
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        assert call(port, "GET", "/v2/today", headers={"Host": host})[0] == 200, host
    assert call(port, "GET", "/api/areas", headers={"Host": f"device.example.net:{port}"})[0] == 200
    # `[::1]` too: the server listens on IPv4 alone, so no client reaches it by that name
    for host in (f"device.example.net:{port}", f"[::1]:{port}", "localhost"):
        code, text, _ = call(port, "GET", "/v2/today", headers={"Host": host})
        assert code == 403 and json.loads(text)["errors"][0]["message"].startswith(f"host {host!r}"), host


def test_v2_refuses_a_foreign_origin_on_a_get_that_the_older_routes_answer(tmp_path):
    port = serve_on(reg(tmp_path, resident(tmp_path)), allow=["device.example.net"])
    for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        assert call(port, "GET", "/v2/today", headers={"Origin": origin})[0] == 200, origin
    for origin in ("http://evil.example", f"http://device.example.net:{port}", f"https://localhost:{port}"):
        assert call(port, "GET", "/api/areas", headers={"Origin": origin})[0] == 200, origin
        code, text, _ = call(port, "GET", "/v2/today", headers={"Origin": origin})
        assert code == 403 and json.loads(text)["errors"][0]["message"].startswith(f"origin {origin!r}")


def test_v2_refuses_a_foreign_origin_on_a_write_before_its_body_is_read(tmp_path):
    d = resident(tmp_path)
    port = serve_on(reg(tmp_path, d), allow=["device.example.net"])
    writes = [
        ("PUT", "/v2/agents/x/desk/definition"),
        ("POST", "/v2/agents/x/desk/control"),
        ("DELETE", "/v2/library/models/codex"),
    ]
    for method, path in writes:  # the second Origin is one the older routes take on a POST
        for origin in ("http://evil.example", f"https://device.example.net:{port}"):
            code, doc = unsent(port, method, path, {"Origin": origin})
            assert code == 403 and doc["errors"][0]["message"].startswith(f"origin {origin!r}"), path
    assert (d / "chores.toml").read_text() == CHORES and not (d / "hold").exists()
    mine = {"Origin": f"http://localhost:{port}"}
    assert write(port, "POST", writes[1][1], {"action": "hold", "once": once()}, mine)[0] == 200
    assert (d / "hold").exists()


def test_the_v2_event_stream_refuses_what_every_v2_route_refuses(tmp_path, monkeypatch):
    port = serve_on(reg(tmp_path, resident(tmp_path, lines=LOG)), allow=["device.example.net"])

    def opened(headers: dict) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v2/events", headers=headers)
        resp = conn.getresponse()
        conn.close()
        return resp.status, resp.getheader("Content-Type")

    assert opened({}) == (200, "text/event-stream; charset=utf-8")
    assert opened({"Origin": "http://evil.example"}) == (403, "application/json")
    assert opened({"Host": f"device.example.net:{port}"}) == (403, "application/json")
    peer_is(monkeypatch, "100.64.0.1")
    assert opened({}) == (403, "application/json")


ROUTES = {  # every name the contract gives `can`, and a route that would serve it
    "definition": ("PUT", "/v2/agents/x/desk/definition"),
    "control": ("POST", "/v2/agents/x/desk/control"),
    "message": ("POST", "/v2/agents/x/desk/message"),
    "foryou": ("POST", "/v2/foryou/u1/done"),
    "models": ("POST", "/v2/library/models"),
    "provider_key": ("PUT", "/v2/library/providers/openrouter/key"),
    "check_provider": ("POST", "/v2/library/providers/openrouter/check"),
    "open": ("POST", "/v2/open"),
    "add_agent": ("POST", "/v2/agents"),
    "inspect": ("POST", "/v2/agents/inspect"),
    "try_tool": ("POST", "/v2/plugins/lion/tools/read/try"),
    "plugins": ("POST", "/v2/plugins"),
    "marketplaces": ("POST", "/v2/marketplaces"),
    "shell_rules": ("PUT", "/v2/shell-rules/default"),
    "test_model": ("POST", "/v2/library/models/codex/test"),
    "providers": ("PATCH", "/v2/library/providers/openrouter"),
    "limits": ("POST", "/v2/library/providers"),
    "give": ("POST", "/v2/give"),
}


def test_can_names_exactly_the_writes_a_route_serves(tmp_path):
    port = serve_on(reg(tmp_path, resident(tmp_path)))
    assert set(CAN) <= set(ROUTES) and get(port, "/v2/today")[1]["daemon"]["can"] == list(CAN)
    for name, (method, path) in ROUTES.items():
        code, doc = write(port, method, path, {"once": once()})
        served = not (code == 404 and doc["errors"][0]["message"].startswith("no route"))
        assert served is (name in CAN), (name, code, doc)
