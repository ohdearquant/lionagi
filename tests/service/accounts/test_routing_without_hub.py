# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from tests.service.test_hub_absent_characterization import _run_blocked


def test_account_router_uses_adapter_evidence_without_lionhub(tmp_path):
    report = _run_blocked(
        """
import asyncio
from uuid import uuid4

from pydantic import SecretStr

from lionagi.service.accounts import _router
from lionagi.service.accounts._pricing import CURRENT_REVISION
from lionagi.service.accounts._types import AccountSpec, Reservation, Scope

seen = {}


class Text(str):
    pass


class Ledger:
    async def reserve(self, scope, attempt_id, quote, accounts, *, cert):
        seen["reserved_cert"] = cert
        return Reservation(attempt_id, scope, accounts[0].name, "2026-11", quote)

    async def settle(self, attempt_id, usage, **evidence):
        seen["settled"] = evidence
        seen["usage"] = type(usage).__name__


def cache_evidence_for(payload):
    seen["cache_input"] = {
        "content_is_plain_str": type(payload["messages"][0]["content"]) is str,
        "service_tier": payload["service_tier"],
    }
    return _router.RoutedCacheEvidence("test-cert", True, False)


async def resolve_key(*args):
    return SecretStr("test-key")


_router.validate_routed_request = lambda *args, **kwargs: None
_router.resolve_account_key = resolve_key


async def run():
    model = "claude-sonnet-5-5"
    request = {
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": Text("hello")}],
    }
    router = _router.AccountRouter(
        sources=None,
        ledger=Ledger(),
        accounts=(AccountSpec("alpha", "ALPHA_KEY", 10**15),),
        revision=CURRENT_REVISION,
    )
    scope = Scope("branch", uuid4(), "anthropic")
    try:
        positional = router.begin(
            scope, request, [], {}, endpoint=None, request_kwargs={}, body_kwargs={},
        )
    except TypeError as exc:
        assert "positional" in str(exc)
    else:
        positional.close()
        raise AssertionError("AccountRouter.begin accepted a fourth positional argument")
    attempt = await router.begin(
        scope, request, [],
        endpoint=None, request_kwargs={}, body_kwargs={}, cache_evidence_for=cache_evidence_for,
    )
    seen["request_body"] = json.loads(attempt.body)
    await router.settle_response(
        attempt, {"model": model, "usage": {"input_tokens": 4, "output_tokens": 1}}
    )


asyncio.run(run())
seen["hub_modules"] = sorted(
    name for name in sys.modules if name == "lionhub" or name.startswith("lionhub.")
)
print(json.dumps(seen))
""",
        tmp_path,
    )

    assert report == {
        "cache_input": {"content_is_plain_str": True, "service_tier": "standard_only"},
        "reserved_cert": "test-cert",
        "request_body": {
            "model": "claude-sonnet-5-5",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hello"}],
            "service_tier": "standard_only",
        },
        "settled": {"cert": "test-cert", "hour_after_key": True, "server_tools": False},
        "usage": "CompleteUsage",
        "hub_modules": [],
    }
