"""Shared Claude Code refusal event used by provider and CLI tests."""

REFUSAL = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "api_error_status": 429,
    "result": "You've hit your session limit · resets 3am",
}
