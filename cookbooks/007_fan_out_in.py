from lionagi import Branch, Builder, Operation, Session, iModel
from lionagi.operations.fields import LIST_INSTRUCT_FIELD_MODEL, Instruct
from lionagi.protocols.types import AssistantResponse

CC_WORKSPACE = ".khive/workspace"


def create_cc(
    subdir: str,
    model: str = "sonnet",
    verbose_output: bool = True,
    permission_mode="default",
    auto_finish: bool = False,
):
    return iModel(
        provider="claude_code",
        endpoint="query_cli",
        model=model,
        ws=f"{CC_WORKSPACE}/{subdir}",
        verbose_output=verbose_output,
        add_dir="../../../",
        permission_mode=permission_mode,
        cli_display_theme="dark",
        auto_finish=auto_finish,
    )


prompt = """
Task: Quickly Investigate the codebase in the specified directory and provide a concise overview.

---START
read into the specified dir, glance over the key components and pay attention to architecture, 
design patterns, and any notable features. Think deeply about the codebase and give three parallel
instructions, as part of the structured output (`instruct_model`) in the final response message.

---Then
The instruct models will be run in parallel by each researcher branch, and I will provide you with 
the researchers' findings for you to continue your investigation.

---Finally
Once the researchers have completed their tasks, synthesize the information they provided into a cohesive
overview of the codebase, including:
1. Key components and their roles
2. Architectural patterns used
3. Design patterns and notable features
---END
"""


async def main():
    try:
        orc_cc = create_cc("orchestrator")
        orc_branch = Branch(
            chat_model=orc_cc,
            parse_model=orc_cc,
            use_lion_system_message=True,
            system_datetime=True,
            name="orchestrator",
        )
        session = Session(default_branch=orc_branch)

        builder = Builder("CodeInvestigator")
        root = builder.add_operation(
            "operate",
            instruct=Instruct(
                instruction=prompt,
                context="lionagi",
            ),
            reason=True,
            field_models=[LIST_INSTRUCT_FIELD_MODEL],
        )

        result = await session.flow(builder.get_graph())

        instruct_model: list[Instruct] = result["operation_results"][root].instruct_model
        research_nodes = []

        for i in instruct_model:
            node = builder.add_operation(
                "communicate",
                depends_on=[root],
                chat_model=create_cc("researcher"),
                **i.to_dict(),
            )
            research_nodes.append(node)

        costs = 0

        def get_context(node_id):
            nonlocal costs
            g = builder.get_graph()
            node: Operation = g.internal_nodes[node_id]
            branch = session.get_branch(node.branch_id, None)
            if (
                branch
                and len(branch.messages) > 0
                and isinstance(msg := branch.messages[-1], AssistantResponse)
            ):
                costs += msg.model_response.get("total_cost_usd") or 0
                return f"""
            Response: {msg.model_response.get("result") or "Not available"}
            Summary: {msg.model_response.get("summary") or "Not available"}
            """.strip()

        await session.flow(builder.get_graph())
        ctx = [get_context(i) for i in research_nodes]

        synthesis = builder.add_operation(
            "communicate",
            depends_on=research_nodes,
            branch=orc_branch,
            instruction="Synthesize the information from the researcher branches.",
            context=[i for i in ctx if i is not None],
        )

        result3 = await session.flow(builder.get_graph())
        result_synthesis = result3["operation_results"][synthesis]

        builder.visualize("LionAGI codebase investigation: fan-out fan-in pattern with Claude Code")

        print(f"Flow total cost: ${costs:.4f}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    import anyio

    anyio.run(main)
