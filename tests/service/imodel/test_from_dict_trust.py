"""What a restored model takes from stored data, and what it takes only from the caller."""

import httpx
import pytest
from pydantic import SecretStr

import lionagi.config
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

OPENAI_URL = "https://api.openai.com/v1"
ELSEWHERE = "https://gateway.example/v1"
LATTICE_MODEL = "qwen3.5-0.8b"


@pytest.fixture
def env_key(monkeypatch):
    patched = lionagi.config.settings.model_copy(
        update={"OPENAI_API_KEY": SecretStr("sk-from-env")}
    )
    monkeypatch.setattr(lionagi.config, "settings", patched)
    return "sk-from-env"


def _stored(**config):
    data = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key").to_dict()
    data["endpoint"]["config"].update(config)
    return data


def test_the_env_key_is_attached_at_the_providers_registered_url(env_key):
    config = iModel.from_dict(_stored()).endpoint.config
    assert config.base_url == OPENAI_URL
    assert config._api_key == env_key


def test_the_env_key_does_not_follow_a_stored_url_elsewhere(env_key):
    config = iModel.from_dict(_stored(base_url=ELSEWHERE)).endpoint.config
    assert config.base_url == ELSEWHERE
    assert config._api_key is None


def test_a_caller_url_elsewhere_gets_no_env_key_either(env_key):
    config = iModel.from_dict(_stored(), base_url=ELSEWHERE).endpoint.config
    assert config.base_url == ELSEWHERE
    assert config._api_key is None


@pytest.mark.parametrize(
    "stored", [OPENAI_URL + "/", "https://API.OpenAI.com/v1", "HTTPS://api.openai.com:443/v1"]
)
def test_the_env_key_is_attached_at_another_spelling_of_the_registered_url(env_key, stored):
    assert iModel.from_dict(_stored(base_url=stored)).endpoint.config._api_key == env_key


@pytest.mark.parametrize(
    "stored",
    [
        "https://api.openai.com:8443/v1",
        "http://api.openai.com/v1",
        "https://api.openai.com/v2",
        "https://api.openai.com.gateway.example/v1",
        "https://someone@api.openai.com/v1",
    ],
)
def test_the_env_key_does_not_follow_a_near_spelling(env_key, stored):
    assert iModel.from_dict(_stored(base_url=stored)).endpoint.config._api_key is None


@pytest.mark.parametrize("caller_url", [None, OPENAI_URL])
def test_a_stored_processor_config_sets_only_the_rate_limits(env_key, caller_url):
    data = _stored()
    data["processor_config"].update(
        limit_requests=7, base_url=ELSEWHERE, provider="anthropic", env={"HOME": "/elsewhere"}
    )
    model = iModel.from_dict(data, base_url=caller_url)
    assert model.executor.config["limit_requests"] == 7
    assert model.endpoint.config.base_url == OPENAI_URL
    assert model.endpoint.config.provider == "openai"
    assert model.endpoint.config._api_key == env_key


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("queue_capacity", 17),
        ("capacity_refresh_time", 13.0),
        ("interval", 3.0),
        ("limit_requests", 7),
        ("limit_tokens", 11),
        ("concurrency_limit", 5),
    ],
)
def test_a_stored_processor_config_keeps_each_rate_limit(name, value):
    data = _stored()
    data["processor_config"][name] = value
    assert iModel.from_dict(data).executor.config[name] == value


def test_a_caller_key_is_used_at_any_url(env_key):
    config = iModel.from_dict(_stored(base_url=ELSEWHERE), api_key="sk-caller").endpoint.config
    assert config._api_key == "sk-caller"


@pytest.mark.parametrize("base_url", [OPENAI_URL, ELSEWHERE])
def test_a_stored_key_is_never_resolved(env_key, monkeypatch, base_url):
    monkeypatch.setenv("SOME_OTHER_SECRET", "other-secret-value")
    config = iModel.from_dict(
        _stored(base_url=base_url, api_key="SOME_OTHER_SECRET")
    ).endpoint.config
    # The stored name is not read: the env key at the registered URL, and none elsewhere.
    assert config._api_key == (env_key if base_url == OPENAI_URL else None)


def test_a_stored_local_network_opt_in_is_not_restored():
    config = iModel.from_dict(_stored(allow_local_network=True)).endpoint.config
    assert config.allow_local_network is False


def test_a_local_provider_keeps_its_registered_opt_in_at_any_port():
    url = "http://127.0.0.1:9999/v1"
    data = iModel(provider="lattice", model=LATTICE_MODEL, base_url=url).to_dict()
    config = iModel.from_dict(data).endpoint.config
    assert config.base_url == url
    assert config.allow_local_network is True


def test_a_stored_config_can_turn_a_local_providers_opt_in_off():
    url = "https://models.example/v1"
    model = iModel(provider="lattice", model=LATTICE_MODEL, base_url=url, allow_local_network=False)
    config = iModel.from_dict(model.to_dict()).endpoint.config
    assert config.base_url == url
    assert config.allow_local_network is False


def _lattice_row():
    return iModel(
        provider="lattice", model=LATTICE_MODEL, base_url="http://127.0.0.1:9999/v1"
    ).to_dict()


@pytest.mark.parametrize("stored", [False, 0, 0.0, "false", "off", None, "true"])
def test_a_stored_opt_in_other_than_true_turns_a_local_providers_off(stored):
    data = _lattice_row()
    data["endpoint"]["config"]["allow_local_network"] = stored
    assert iModel.from_dict(data).endpoint.config.allow_local_network is False


def test_a_stored_opt_in_off_in_kwargs_turns_a_local_providers_off():
    data = _lattice_row()
    data["endpoint"]["config"].setdefault("kwargs", {})["allow_local_network"] = False
    assert iModel.from_dict(data).endpoint.config.allow_local_network is False


def test_a_stored_true_keeps_a_local_providers_registered_opt_in():
    data = _lattice_row()
    assert data["endpoint"]["config"]["allow_local_network"] is True
    assert iModel.from_dict(data).endpoint.config.allow_local_network is True


def test_the_caller_decides_the_opt_in():
    opened = iModel.from_dict(_stored(), allow_local_network=True)
    assert opened.endpoint.config.allow_local_network is True
    lattice = iModel(provider="lattice", model=LATTICE_MODEL).to_dict()
    closed = iModel.from_dict(lattice, allow_local_network=False)
    assert closed.endpoint.config.allow_local_network is False


@pytest.mark.parametrize("where", ["top-level", "kwargs"])
@pytest.mark.parametrize(
    ("name", "value"), [("api_key", "SOME_OTHER_SECRET"), ("allow_local_network", True)]
)
def test_the_endpoint_itself_drops_a_stored_key_or_opt_in(env_key, monkeypatch, where, name, value):
    monkeypatch.setenv("SOME_OTHER_SECRET", "other-secret-value")
    data = _stored()["endpoint"]
    config = data["config"]
    (config if where == "top-level" else config["kwargs"])[name] = value

    restored = Endpoint.from_dict(data).config
    assert name not in restored.kwargs
    assert restored._api_key is None
    assert restored.allow_local_network is False


@pytest.mark.parametrize("where", ["top-level", "kwargs"])
def test_unwritten_names_are_dropped_wherever_the_row_puts_them(where):
    data = iModel(provider="codex", model="gpt-5.5", verbose_output=True).to_dict()
    config = data["endpoint"]["config"]
    names = {"account_connectors": ["github"], "env": {"TOKEN": "x"}, "on_spawn": "callback"}
    (config if where == "top-level" else config["kwargs"]).update(names)

    for restored in (
        Endpoint.from_dict(data["endpoint"]).config,
        iModel.from_dict(data).endpoint.config,
    ):
        assert not set(names) & set(restored.kwargs)
        # The persisted settings around them survive.
        assert restored.kwargs["model"] == "gpt-5.5"
        assert restored.kwargs["verbose_output"] is True


def test_a_branch_takes_the_callers_model_over_the_stored_one(env_key):
    branch = Branch(chat_model=iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key"))
    data = branch.to_dict()
    data["chat_model"]["endpoint"]["config"]["base_url"] = ELSEWHERE

    stored = Branch.from_dict(data).chat_model.endpoint.config
    assert stored.base_url == ELSEWHERE
    assert stored._api_key is None

    live = iModel(provider="openai", model="gpt-4.1-mini", base_url=ELSEWHERE, api_key="sk-caller")
    restored = Branch.from_dict(data, chat_model=live)
    assert restored.chat_model is live
    # No parse model was stored, so the parse model follows the chat model.
    assert restored.parse_model is live
    # The caller's data is left as it was.
    assert "chat_model" in data


def test_a_branch_takes_a_callers_parse_model_beside_the_stored_chat_model():
    chat = iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key")
    parse = iModel(provider="openai", model="gpt-4.1-nano", api_key="test-key")
    data = Branch(chat_model=chat, parse_model=parse).to_dict()

    live = iModel(provider="openai", model="gpt-4.1", api_key="sk-caller")
    restored = Branch.from_dict(data, parse_model=live)
    assert restored.parse_model is live
    assert restored.chat_model.endpoint.config.kwargs["model"] == "gpt-4.1-mini"


CREDENTIAL_HEADERS = [
    "Authorization",
    "x-api-key",
    "X_Api_Key",
    "Proxy-Authorization",
    "api-key",
    # Credentials outside the library's credential vocabulary, matched on restore only.
    "Cookie",
    "Ocp-Apim-Subscription-Key",
    "X-Functions-Key",
    "X-Auth",
    "X-Auth-Key",
    # Not a credential, but it would send the restored key to another virtual host.
    "Host",
]
CALLERS = {
    "env-key": {},
    "caller-key": {"api_key": "sk-caller"},
    "caller-url": {"api_key": "sk-caller", "base_url": ELSEWHERE},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CREDENTIAL_HEADERS)
@pytest.mark.parametrize("caller", list(CALLERS.values()), ids=list(CALLERS))
async def test_a_stored_credential_header_never_reaches_the_request(env_key, name, caller):
    data = _stored(
        default_headers={name: "stored-secret", "OpenAI-Beta": "kept", "X-Monkey": "kept"}
    )
    data["endpoint"]["config"]["client_kwargs"] = {
        "headers": [[name, "stored-secret"], ["X-Trace", "kept"]],
        "auth": ["user", "stored-secret"],
        "trust_env": True,
    }
    endpoint = iModel.from_dict(data, **caller).endpoint

    _, headers = endpoint.create_payload({"model": "gpt-4.1-mini", "messages": []})
    assert headers["Authorization"] == f"Bearer {caller.get('api_key', env_key)}"
    assert "stored-secret" not in headers.values()
    assert headers["OpenAI-Beta"] == "kept"
    # Ends in "key" but not in "-Key": a header name, not a key.
    assert headers["X-Monkey"] == "kept"

    session = endpoint._create_http_session()
    try:
        assert "stored-secret" not in session.headers.values()
        assert session.headers["X-Trace"] == "kept"
        assert session.auth is None
        assert session.trust_env is False
    finally:
        await session.aclose()


STORED_PASSWORD_URL = "https://stored-user:stored-secret@gateway.example/v1"


async def _send(endpoint):
    seen = {}

    def handler(request):
        seen["authorization"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    session = endpoint._create_http_session({"transport": httpx.MockTransport(handler)})
    try:
        await session.post(endpoint.config.full_url, json={})
    finally:
        await session.aclose()
    return seen


@pytest.mark.asyncio
async def test_a_user_and_password_in_the_stored_url_never_reach_the_request(env_key):
    endpoint = iModel.from_dict(_stored(base_url=STORED_PASSWORD_URL)).endpoint
    assert endpoint.config.base_url == ELSEWHERE
    assert endpoint.config._api_key is None

    seen = await _send(endpoint)
    assert seen["url"] == f"{ELSEWHERE}/chat/completions"
    assert seen["authorization"] is None


def test_the_providers_url_stored_with_a_password_gets_neither(env_key):
    # Restored without the password, and still not the registered URL as written.
    stored = "https://stored-user:stored-secret@api.openai.com/v1"
    config = iModel.from_dict(_stored(base_url=stored)).endpoint.config
    assert config.base_url == OPENAI_URL
    assert config._api_key is None


def test_a_callers_url_keeps_its_user_and_password(env_key):
    caller = "https://caller-user:caller-secret@gateway.example/v1"
    config = iModel.from_dict(_stored(), base_url=caller).endpoint.config
    assert config.base_url == caller
    assert config._api_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cast", [bytes, bytearray])
async def test_a_stored_url_in_bytes_loses_its_user_and_password(env_key, cast):
    model = iModel.from_dict(_stored(base_url=cast(STORED_PASSWORD_URL.encode())))
    assert model.endpoint.config.base_url == ELSEWHERE
    assert "stored-secret" not in str(model.to_dict())

    seen = await _send(model.endpoint)
    assert seen["authorization"] is None


def test_the_providers_url_stored_in_bytes_with_a_password_gets_no_env_key(env_key):
    stored = b"https://stored-user:stored-secret@api.openai.com/v1"
    config = iModel.from_dict(_stored(base_url=stored)).endpoint.config
    assert config.base_url == OPENAI_URL
    assert config._api_key is None


def test_a_callers_url_replaces_stored_bytes_that_are_not_utf8():
    stored = b"https://stored-user:stored-secret@gateway.example/v1/\xff"
    config = iModel.from_dict(_stored(base_url=stored), base_url=ELSEWHERE).endpoint.config
    assert config.base_url == ELSEWHERE


def _restore_through(entry, data):
    """Restore stored iModel data through one of the three entry points."""
    if entry == "imodel":
        return iModel.from_dict(data).endpoint.config
    if entry == "endpoint":
        return Endpoint.from_dict(data["endpoint"]).config
    branch = Branch(chat_model=iModel(provider="openai", model="gpt-4.1-mini", api_key="test-key"))
    branch_data = branch.to_dict()
    branch_data["chat_model"] = data
    return Branch.from_dict(branch_data).chat_model.endpoint.config


@pytest.mark.parametrize("entry", ["imodel", "endpoint", "branch"])
@pytest.mark.parametrize("cast", [bytes, bytearray])
def test_stored_bytes_lose_their_user_and_password_on_every_restore(entry, cast):
    data = _stored(base_url=cast(STORED_PASSWORD_URL.encode()))
    assert _restore_through(entry, data).base_url == ELSEWHERE


@pytest.mark.parametrize("entry", ["imodel", "endpoint", "branch"])
@pytest.mark.parametrize("cast", [bytes, bytearray])
def test_stored_bytes_that_are_not_utf8_are_refused_without_their_text(entry, cast):
    data = _stored(base_url=cast(b"https://stored-user:stored-secret@gateway.example/v1/\xff"))
    with pytest.raises(ValueError, match="do not decode as UTF-8") as refused:
        _restore_through(entry, data)
    assert "stored-se" not in str(refused.value)
    assert "chat_model=" in str(refused.value)
    # The decode error holds the bytes; it is not attached to the refusal.
    assert refused.value.__context__ is None


# Spellings the HTTP client reads with a host and a user, though a URL split by the
# standard library refuses them or places the '@' elsewhere.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        "https://stored-user:stored-se[cret@gateway.example/v1",
        "https://stored-user:stored-se]cret@gateway.example/v1",
        "https://[stored-user:stored-secret@gateway.example/v1",
        "https://stored-user:stored-secret@x@gateway.example/v1",
        "HTTPS://stored-user:stored-secret@gateway.example/v1",
        "https://stored-user%40corp:stored-secret@gateway.example/v1",
        "https://stored-user@gateway.example/v1",
        "https://:@gateway.example/v1",
        "https://stored-user:stored-secret@gateway.example:/v1",
        "https://stored-user:%" + "25" * 15 + "41@gateway.example/v1",
    ],
)
async def test_a_user_the_client_would_read_is_removed(env_key, stored):
    model = iModel.from_dict(_stored(base_url=stored))
    assert model.endpoint.config.base_url == ELSEWHERE
    assert "stored-se" not in str(model.to_dict())

    seen = await _send(model.endpoint)
    assert seen["authorization"] is None


@pytest.mark.parametrize(
    ("stored", "restored"),
    [
        (
            "https://stored-user:stored-secret@gateway.example:8443/v1",
            "https://gateway.example:8443/v1",
        ),
        ("https://stored-user:stored-secret@127.0.0.1:8000/v1", "https://127.0.0.1:8000/v1"),
        ("https://stored-user:stored-secret@[::1]:8080/v1", "https://[::1]:8080/v1"),
        ("https://stored-user:stored-secret@[fe80::1%25en0]/v1", "https://[fe80::1%25en0]/v1"),
        ("https://stored-user:stored-secret@例え.jp/v1", "https://xn--r8jz45g.jp/v1"),
        ("https://stored-user:stored-secret@gateway.example/v1?team=x#y", f"{ELSEWHERE}?team=x#y"),
    ],
)
def test_a_user_is_removed_before_a_port_an_ip_address_or_a_name_outside_ascii(stored, restored):
    assert iModel.from_dict(_stored(base_url=stored)).endpoint.config.base_url == restored


def test_a_stored_kwargs_base_url_is_not_restored():
    data = _stored()
    data["endpoint"]["config"].setdefault("kwargs", {})["base_url"] = STORED_PASSWORD_URL
    model = iModel.from_dict(data)
    assert "base_url" not in model.endpoint.config.kwargs
    assert "stored-se" not in str(model.to_dict())


def _encoded(layers: int) -> str:
    # 'A' (%41) under *layers* more layers of percent-encoding.
    return "%" + "25" * layers + "41"


# A stored URL is restored only as http:// or https://, a host name or IP address, an
# optional port number, and no '@' once the user and password are removed. Each of these
# is outside that form, and the reason the refusal gives is the first part that is.
REFUSED = {
    "does not begin with http:// or https://": [
        "https:\\\\stored-user:stored-secret@gateway.example/v1",
        "https:/\\stored-user:stored-secret@gateway.example/v1",
        "https:stored-user:stored-secret@gateway.example/v1",
        "https:/stored-user:stored-secret/v1",
        "https:stored-user:stored-secret/v1",
        " https://stored-user:stored-secret@gateway.example/v1",
        "\thttps://stored-user:stored-secret@gateway.example/v1",
        "stored-user:stored-secret/v1",
        "stored-user:stored-secret@gateway.example/v1",
        "stored-user%3Astored-secret",
        "ftp://stored-user:stored-secret@gateway.example/v1",
        "localhost:8000/v1",
    ],
    "does not read as a URL the HTTP client can send": [
        "https://stored-user:stored-se/cret@gateway.example/v1",
        "https://stored-user:stored-se?cret@gateway.example/v1",
        "https://stored-user:stored-se#cret@gateway.example/v1",
        "https://stored-user:stored-secret＠gateway.example/v1",
        "https://stored-user:stored-secret%40gateway.example/v1",
        "https://stored-user:stored-secret%2540gateway.example/v1",
        "https://stored-user:stored-secret%EF%BC%A0gateway.example/v1",
        "https://stored-user:stored-secret％40gateway.example/v1",
        "https://stored-user:stored-secret﹫gateway.example/v1",
        "https://stored-user:stored-secret/v1",
        "https://stored-user：12/v1",
        "https://gateway.example:port/v1",
        "https://api.openai.com:notaport/v1",
        "https://[stored-user:stored-secret]/v1",
        "https://stored-user[:stored-secret]/v1",
        "https://[fe80::1%25stored%3Astored-secret]/v1",
        "https://stored-user:stored-secret@gateway.example/v1/\ud800",
        "https://stored-\ud800user:12/v1",
        "https://gateway.example/v1/\ud800",
    ],
    "has no host": [
        "https://stored-user:stored-secret@",
        "https:///stored-user:stored-secret",
        "https://:12/stored-secret",
    ],
    "has something other than a host name or IP address and a port number": [
        "https://stored-user%3Astored-secret%40gateway.example/v1",
        "https://stored-user%3astored-secret%40gateway.example/v1",
        "https://other:pass@stored-user%3Astored-secret%40gateway.example/v1",
        "https://stored-user%3AStored-secret%2540gateway.example/v1",
        "https://stored-user%253Astored-secret%25252540gateway.example/v1",
        "https://stored-user%3Astored-secret%EF%BC%A0gateway.example/v1",
        "https://stored-user%3Astored-secret%EF%B9%ABgateway.example/v1",
        "https://stored-user%3Astored-secret%25EF%25BC%25A0gateway.example/v1",
        "https://stored-user%3Astored-secret/v1",
        "https://stored-user%EF%BC%9Astored-secret/v1",
        "https://stored-user%3A12/stored-secret@gateway.example/v1",
        "https://[fe80::1%25stored-user:stored-secret]/v1",
        "https://[fe80::1%stored-secret]/v1",
        "https://stored-user:/stored-secret@gateway.example/v1",
        "https://gateway.example:/v1",
        "https://@gateway.example/v1",
        f"https://gateway{_encoded(14)}.example/v1",
    ],
    "has an '@' after its host": [
        "https://stored-user:12/stored-secret@gateway.example/v1",
        "https://stored-user:12?stored-secret@gateway.example/v1",
        "https://stored-user:12#stored-secret@gateway.example/v1",
        "https://stored-user:12/stored-secret%40gateway.example/v1",
        "https://other:pass@stored-user:12/stored-secret@gateway.example/v1",
        "https://[::1]:12/stored-secret@gateway.example/v1",
        "https://stored-user:stored-secret@gateway.example/v1/@team",
        "https://gateway.example/v1/@team",
        "https://gateway.example/v1/%40team",
        "https://gateway.example/v1/%2540team",
        "https://gateway.example/v1/＠team",
        "https://gateway.example/v1?team=%40x",
        "https://gateway.example/v1#%40team",
        "https://gateway.example:8443/v1?team=@x",
        "https://gateway.example:8443/v1#@team",
        "https://[::1]/v1/@team",
    ],
    "is still percent-encoded after 16 layers are read": [
        f"internal{_encoded(15)}",
        f"https://gateway.example/v1/{_encoded(15)}",
        f"https://gateway.example:8443/v1/{_encoded(15)}",
    ],
}
REFUSED_CASES = [(reason, stored) for reason, urls in REFUSED.items() for stored in urls]


@pytest.mark.parametrize(("reason", "stored"), REFUSED_CASES)
def test_a_stored_url_outside_the_restorable_form_is_refused(reason, stored):
    with pytest.raises(ValueError) as refused:
        iModel.from_dict(_stored(base_url=stored))
    message = str(refused.value)
    assert message.startswith(f"The stored base_url {reason}, so a user and password")
    for part in ("stored-se", "Stored-se", "stored-user", "gateway.example", "api.openai"):
        assert part not in message
    assert "chat_model=" in message
    # The client's and the address parser's errors repeat the text; neither is attached.
    assert refused.value.__context__ is None


@pytest.mark.parametrize("entry", ["imodel", "endpoint", "branch"])
@pytest.mark.parametrize("stored", [urls[0] for urls in REFUSED.values()])
def test_every_restore_refuses_a_stored_url_outside_the_form(entry, stored):
    with pytest.raises(ValueError, match="cannot be removed"):
        _restore_through(entry, _stored(base_url=stored))


@pytest.mark.parametrize("stored", [stored for _, stored in REFUSED_CASES])
def test_a_callers_url_replaces_a_stored_url_that_is_refused(stored):
    config = iModel.from_dict(_stored(base_url=stored), base_url=ELSEWHERE).endpoint.config
    assert config.base_url == ELSEWHERE


@pytest.mark.parametrize(
    "stored",
    [
        "internal",
        "",
        "gateway.example/v1",
        "https://gateway.example",
        "https://gateway.example:8443/v1",
        "HTTPS://Gateway.Example/v1",
        "http://localhost:11434/v1",
        "https://127.0.0.1:8000/v1",
        "https://[::1]:8080/v1",
        "https://[fe80::1%25en0]/v1",
        "https://gateway_x.example/v1",
        "https://例え.jp/v1",
        "https://gateway.example/v1/models:list",
        "https://gateway.example/v1?team=x#y",
        f"https://gateway.example/v1/{_encoded(14)}",
    ],
)
def test_a_stored_url_in_the_restorable_form_is_kept_as_written(stored):
    assert iModel.from_dict(_stored(base_url=stored)).endpoint.config.base_url == stored
