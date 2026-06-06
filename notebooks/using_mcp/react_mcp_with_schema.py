#!/usr/bin/env python3
"""
Test ReAct reasoning with MCP search tools and proper Pydantic schemas
"""

import asyncio

from pydantic import BaseModel

from lionagi import Branch, iModel
from lionagi.protocols.action.manager import load_mcp_tools
from lionagi.service.third_party.exa_models import ExaSearchRequest
from lionagi.service.third_party.pplx_models import PerplexityChatRequest


class ExaRequest(BaseModel):
    request: ExaSearchRequest


class PerplexityRequest(BaseModel):
    request: PerplexityChatRequest


async def test_react_with_mcp():
    print("🦁 Testing ReAct with MCP Search Tools (with Pydantic schemas)")
    print("=" * 60)

    # 1. Load MCP search tools with proper Pydantic schemas
    print("\n1. Loading MCP search tools with Pydantic validation...")
    tools = await load_mcp_tools(
        "/Users/lion/projects/lionagi/cookbooks/using_mcp/.mcp.json",
        server_names=["search"],
        request_options_map={
            "search": {
                "exa_search": ExaRequest,
                "perplexity_search": PerplexityRequest,
            }
        },
    )
    print(f"   ✅ Loaded {len(tools)} search tools with schemas:")
    for tool in tools:
        print(f"      - {tool.function} (has request_options: {tool.request_options is not None})")

    # 2. Create a Branch with the tools
    print("\n2. Creating Branch with gpt-4.1-mini...")
    branch = Branch(
        name="react_test",
        chat_model=iModel(provider="openai", model="gpt-4.1-mini"),
        tools=tools,
    )
    print("   ✅ Branch created")

    # 3. Run ReAct reasoning
    print("\n3. Running ReAct reasoning (max 3 extensions)...")
    print("   Question: What are the latest developments in Model Context Protocol (MCP)?")
    print("\n   Executing ReAct...")

    try:
        result = await branch.ReAct(
            instruct={
                "instruction": (
                    "Research the latest developments in Model Context Protocol (MCP). "
                    "Use the search tools to find recent information about MCP, "
                    "its features, and adoption by different platforms."
                ),
                "context": {},
            },
            tools=["exa_search", "perplexity_search"],
            max_extensions=3,
            verbose=True,
        )

        print("\n4. ReAct Result:")
        print("-" * 40)
        if result:
            # Print result content
            if hasattr(result, "content"):
                print(result.content)
            else:
                print(str(result))
        print("-" * 40)

    except Exception as e:
        print(f"\n❌ ReAct failed: {e}")
        import traceback

        traceback.print_exc()

    print("\n" + "=" * 60)
    print("✅ Test Complete!")
    print("\nKey improvements:")
    print("  • Tools now have proper Pydantic schemas for validation")
    print("  • Model knows exactly what parameters to provide")
    print("  • Type safety and validation at runtime")


if __name__ == "__main__":
    asyncio.run(test_react_with_mcp())
