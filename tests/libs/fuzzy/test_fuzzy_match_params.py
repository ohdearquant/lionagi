# tests/libs/fuzzy/test_fuzzy_match_params.py
import inspect

from lionagi.ln.fuzzy._fuzzy_match import FuzzyMatchKeysParams, fuzzy_match_keys
from lionagi.ln.types import Unset


def _signature_defaults() -> dict:
    return {
        name: param.default
        for name, param in inspect.signature(fuzzy_match_keys).parameters.items()
        if param.default is not inspect.Parameter.empty
    }


def test_forwarded_defaults_agree_with_the_function_signature():
    """The declared defaults must not silently override the ones they forward to.

    ``FuzzyMatchKeysParams.__call__`` calls ``fuzzy_match_keys(..., **default_kw())``,
    and ``default_kw`` drops sentinel-valued fields. A field declared with a real
    default is therefore forwarded explicitly and wins over the signature default,
    so the two spellings of every such default have to stay equal. Nothing else
    couples them: they live in different statements and can drift independently.
    """
    forwarded = FuzzyMatchKeysParams().default_kw()
    signature_defaults = _signature_defaults()

    # A vacuous pin is the failure mode here: if nothing is forwarded, every
    # comparison below passes while proving nothing.
    assert forwarded, "no defaults forwarded; the agreement below would be vacuous"
    assert signature_defaults, "no signature defaults read; comparison would be vacuous"

    mismatched = {
        name: (value, signature_defaults.get(name))
        for name, value in forwarded.items()
        if name not in signature_defaults or signature_defaults[name] != value
    }
    assert not mismatched, f"forwarded defaults override the function signature: {mismatched}"


def test_sentinel_fields_are_left_to_the_function_signature():
    """Sentinel-declared fields must stay unforwarded.

    ``fill_value`` and ``fill_mapping`` declare ``Unset`` while the function
    declares ``None``. That disagreement is only safe because ``default_kw`` drops
    sentinels, leaving the function's own default in force. Declaring either of
    them with a real value would start forwarding it and change how unmatched keys
    are filled, which no other test would notice.
    """
    params = FuzzyMatchKeysParams()
    forwarded = params.default_kw()

    # Named literally rather than derived from the class: a set built by asking
    # which fields are sentinel-declared would shrink to match any change here,
    # and pass while the behaviour it guards was being altered.
    for field in ("fill_value", "fill_mapping"):
        assert getattr(params, field, Unset) is Unset, (
            f"{field} is no longer sentinel-declared, so it is now forwarded and "
            "overrides the function's own default for unmatched keys"
        )
        assert field not in forwarded, f"{field} was forwarded despite being sentinel-declared"
