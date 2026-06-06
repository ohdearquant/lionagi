from lionagi import Branch, iModel

BASE_CONFIG = {
    "provider": "claude_code",
    "endpoint": "query_cli",
    "model": "sonnet",
    "api_key": "dummy_api_key",
    "allowed_tools": ["Read"],
    "permission_mode": "bypassPermissions",
    "verbose_output": True,
    "cli_display_theme": "dark",
}

prompt = """
Read into lionagi, explain to me the
1. architecture of protocols and operations
2. how branch, and session work together
3. how do these parts form lionagi system
"""


async def main():
    try:
        k_model = iModel(cwd="lionagi", **BASE_CONFIG)
        investigator = Branch(
            name="lionagi_investigator",
            chat_model=k_model,
            parse_model=k_model,
        )

        print(f"User:\n{prompt}\n")
        await investigator.communicate(prompt)

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    import anyio

    anyio.run(main)
