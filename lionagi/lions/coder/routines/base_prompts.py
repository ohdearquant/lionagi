GUIDANCE_RESPONSE = """
...
"""

PLAN_PROMPT = "..."
WRITE_PROMPT = "..."
REVIEW_PROMPT = "..."
MODIFY_PROMPT = """
...
"""
DEBUG_PROMPT = """
...
"""

CODER_PROMPTS = {
    "system": GUIDANCE_RESPONSE,
    "plan_code": PLAN_PROMPT,
    "write_code": WRITE_PROMPT,
    "review_code": REVIEW_PROMPT,
    "modify_code": MODIFY_PROMPT,
    "debug_code": DEBUG_PROMPT,
}
