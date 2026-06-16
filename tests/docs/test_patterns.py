"""Tests for pattern documentation examples."""

import asyncio

import pytest

from lionagi.testing import LionAGIMockFactory

# ============================================================================
# Helpers / tool functions used across tests
# ============================================================================


def search_knowledge(query: str) -> str:
    """Search the knowledge base."""
    return f"Results for: {query}"


def search_papers(query: str, max_results: int = 5) -> str:
    """Search academic papers."""
    return f"Papers about: {query}"


# ============================================================================
# Fan-Out/In Pattern
# ============================================================================


class TestFanOutIn:
    """Tests for the fan-out/in (expert panel) pattern from fan-out-in.md."""

    def test_multiple_branches_with_different_system_prompts(self):
        """Multiple branches can be created with distinct system prompts."""
        from lionagi import Branch

        expert_prompts = {
            "security": "You are a security expert. Focus on vulnerabilities.",
            "performance": "You are a performance expert. Focus on latency.",
            "ux": "You are a UX expert. Focus on user experience.",
        }

        branches = {}
        for role, prompt in expert_prompts.items():
            branches[role] = Branch(system=prompt, name=role)

        assert len(branches) == 3
        for role, branch in branches.items():
            assert branch.name == role
            assert branch.system is not None
            # Each branch has its own message history
            assert len(branch.msgs.messages) > 0

    @pytest.mark.asyncio
    async def test_fan_out_gather_with_mocked_branches(self):
        """asyncio.gather with multiple mocked branch.communicate calls works."""
        branches = {
            "security": LionAGIMockFactory.create_mocked_branch(
                name="security",
                response="No vulnerabilities found.",
            ),
            "performance": LionAGIMockFactory.create_mocked_branch(
                name="performance",
                response="Latency is within acceptable limits.",
            ),
            "ux": LionAGIMockFactory.create_mocked_branch(
                name="ux",
                response="The user flow is intuitive.",
            ),
        }

        question = "Review this API design."
        results = await asyncio.gather(
            *(branch.communicate(question) for branch in branches.values())
        )

        assert len(results) == 3
        assert all(isinstance(r, str) for r in results)
        assert "vulnerabilities" in results[0]
        assert "Latency" in results[1]
        assert "intuitive" in results[2]

    def test_session_with_multiple_branches_for_fan_out(self):
        """Session can hold multiple branches for a fan-out pattern."""
        from lionagi import Session

        session = Session()
        roles = ["analyst", "critic", "synthesizer"]
        created_branches = []
        for role in roles:
            branch = session.new_branch(
                name=role,
                system=f"You are a {role}.",
            )
            created_branches.append(branch)

        # Session should contain all branches (plus the default branch)
        assert len(session.branches) >= len(roles)

        # Each created branch should be retrievable by name
        for role in roles:
            found = session.get_branch(role)
            assert found is not None
            assert found.name == role

    @pytest.mark.asyncio
    async def test_fan_out_aggregation(self):
        """Fan-out results can be aggregated into a synthesizer branch."""
        expert_responses = [
            "Security: LGTM",
            "Performance: Needs caching",
            "UX: Add loading indicator",
        ]
        synthesizer = LionAGIMockFactory.create_mocked_branch(
            name="synthesizer",
            response="Combined review: add caching and loading indicator.",
        )

        # Simulate aggregating expert responses into context
        combined = "\n".join(f"Expert {i + 1}: {r}" for i, r in enumerate(expert_responses))
        result = await synthesizer.communicate(f"Synthesize these reviews:\n{combined}")
        assert isinstance(result, str)
        assert len(result) > 0


# ============================================================================
# Sequential Analysis Pattern
# ============================================================================


class TestSequentialAnalysis:
    """Tests for the sequential pipeline pattern from sequential-analysis.md."""

    def test_sequential_pipeline_branch_creation(self):
        """Parser, validator, and formatter branches can be created."""
        from lionagi import Branch

        parser = Branch(
            system="You are a data parser. Extract structured data.",
            name="parser",
        )
        validator = Branch(
            system="You are a data validator. Check for errors.",
            name="validator",
        )
        formatter = Branch(
            system="You are a formatter. Output clean markdown.",
            name="formatter",
        )

        assert parser.name == "parser"
        assert validator.name == "validator"
        assert formatter.name == "formatter"

        # Each branch is independent
        assert parser.id != validator.id
        assert validator.id != formatter.id

    def test_branches_have_independent_message_history(self):
        """Each branch maintains its own independent message history."""
        from lionagi import Branch

        branch_a = Branch(system="System A", name="A")
        branch_b = Branch(system="System B", name="B")

        # Initial message counts should be identical (just system message)
        assert len(branch_a.msgs.messages) == len(branch_b.msgs.messages)

        # They should not share the same Pile instance
        assert branch_a.msgs.messages is not branch_b.msgs.messages

    @pytest.mark.asyncio
    async def test_sequential_communicate_pipeline(self):
        """A sequential pipeline passes output of one branch as input to the next."""
        parser = LionAGIMockFactory.create_mocked_branch(
            name="parser",
            response="Parsed: {name: Alice, age: 30}",
        )
        validator = LionAGIMockFactory.create_mocked_branch(
            name="validator",
            response="Valid: all fields present and correctly typed.",
        )
        formatter = LionAGIMockFactory.create_mocked_branch(
            name="formatter",
            response="# Alice\n- Age: 30",
        )

        # Stage 1: parse
        parsed = await parser.communicate("Extract info: Alice is 30.")
        assert "Parsed" in parsed

        # Stage 2: validate
        validated = await validator.communicate(f"Validate this: {parsed}")
        assert "Valid" in validated

        # Stage 3: format
        formatted = await formatter.communicate(f"Format this: {parsed}")
        assert "Alice" in formatted

    def test_communicate_is_async_method(self):
        """branch.communicate is an async method (coroutine function)."""
        import asyncio

        from lionagi import Branch

        branch = Branch()
        assert asyncio.iscoroutinefunction(branch.communicate)


# ============================================================================
# Tournament Validation Pattern
# ============================================================================


class TestTournamentValidation:
    """Tests for the tournament/judge pattern from tournament-validation.md."""

    def test_multiple_solver_branches_different_prompts(self):
        """Multiple solver branches can be created with different strategies."""
        from lionagi import Branch

        strategies = [
            "Solve step by step with formal reasoning.",
            "Use creative analogies and lateral thinking.",
            "Apply brute force enumeration of possibilities.",
        ]

        solvers = []
        for i, strategy in enumerate(strategies):
            solver = Branch(system=strategy, name=f"solver_{i}")
            solvers.append(solver)

        assert len(solvers) == 3
        for i, solver in enumerate(solvers):
            assert solver.name == f"solver_{i}"
            assert solver.system is not None

    def test_judge_branch_creation(self):
        """A judge branch can be created with an evaluation prompt."""
        from lionagi import Branch

        judge = Branch(
            system=(
                "You are a judge. Evaluate multiple solutions "
                "and select the best one based on correctness and clarity."
            ),
            name="judge",
        )
        assert judge.name == "judge"
        assert judge.system is not None

    def test_clone_returns_independent_branch(self):
        """branch.clone() returns a new Branch with independent state."""
        from lionagi import Branch

        original = Branch(
            system="You are a problem solver.",
            name="original",
        )
        cloned = original.clone()

        # Clone is a separate Branch instance
        assert cloned is not original
        assert cloned.id != original.id

        # Clone preserves the system message
        assert cloned.system is not None

    def test_clone_has_independent_message_pile(self):
        """Cloned branch has its own message Pile, not a shared reference."""
        from lionagi import Branch

        original = Branch(system="Solve problems.", name="solver")
        cloned = original.clone()

        # Message piles are different objects
        assert cloned.msgs.messages is not original.msgs.messages

    @pytest.mark.asyncio
    async def test_tournament_solver_and_judge_flow(self):
        """Solvers produce answers, judge selects the best one."""
        solvers = [
            LionAGIMockFactory.create_mocked_branch(
                name=f"solver_{i}",
                response=f"Solution {i}: x = {i + 1}",
            )
            for i in range(3)
        ]
        judge = LionAGIMockFactory.create_mocked_branch(
            name="judge",
            response="Best solution: Solution 2 (x = 3) for correctness.",
        )

        # All solvers answer concurrently
        solutions = await asyncio.gather(*(s.communicate("Solve: 2x + 1 = 7") for s in solvers))
        assert len(solutions) == 3
        assert all("Solution" in s for s in solutions)

        # Judge evaluates
        combined = "\n".join(solutions)
        verdict = await judge.communicate(f"Evaluate:\n{combined}")
        assert "Best solution" in verdict


# ============================================================================
# Conditional Flows Pattern
# ============================================================================


class TestConditionalFlows:
    """Tests for conditional workflow patterns from conditional-flows.md."""

    def test_builder_construction(self):
        """Builder() (OperationGraphBuilder) can be instantiated."""
        from lionagi import Builder

        builder = Builder()
        assert builder is not None

    def test_multiple_specialized_branches(self):
        """Multiple branches can be created with specialized roles."""
        from lionagi import Branch

        roles = {
            "classifier": "Classify the input into categories.",
            "summarizer": "Produce a concise summary.",
            "translator": "Translate text to the target language.",
            "sentiment": "Analyze sentiment and return positive/negative/neutral.",
        }

        branches = {}
        for role, prompt in roles.items():
            branches[role] = Branch(system=prompt, name=role)

        assert len(branches) == 4
        for role in roles:
            assert branches[role].name == role
            assert branches[role].system is not None

    @pytest.mark.asyncio
    async def test_conditional_routing(self):
        """Simulate conditional routing: classify then route to specialist."""
        classifier = LionAGIMockFactory.create_mocked_branch(
            name="classifier",
            response="category: technical",
        )
        technical = LionAGIMockFactory.create_mocked_branch(
            name="technical",
            response="Technical analysis: the code uses O(n) complexity.",
        )
        general = LionAGIMockFactory.create_mocked_branch(
            name="general",
            response="General summary of the topic.",
        )

        # Step 1: classify
        classification = await classifier.communicate("Analyze this algorithm.")
        assert "technical" in classification

        # Step 2: route based on classification
        if "technical" in classification:
            result = await technical.communicate("Deep dive into the algorithm.")
        else:
            result = await general.communicate("Summarize the topic.")

        assert "Technical analysis" in result

    def test_session_branch_routing_by_name(self):
        """Session supports looking up branches by name for routing."""
        from lionagi import Session

        session = Session()
        session.new_branch(name="math", system="You solve math problems.")
        session.new_branch(name="code", system="You write code.")
        session.new_branch(name="essay", system="You write essays.")

        # Route by name
        math_branch = session.get_branch("math")
        assert math_branch is not None
        assert math_branch.name == "math"

        code_branch = session.get_branch("code")
        assert code_branch is not None
        assert code_branch.name == "code"


# ============================================================================
# ReAct with RAG Pattern
# ============================================================================


class TestReActWithRAG:
    """Tests for the ReAct+RAG pattern from react-with-rag.md."""

    def test_branch_with_tools_for_react(self):
        """Branch can be constructed with search tools for ReAct."""
        from lionagi import Branch

        branch = Branch(
            system="You are a research assistant with access to search tools.",
            tools=[search_knowledge, search_papers],
            name="researcher",
        )

        assert branch.name == "researcher"
        registry = branch.acts.registry
        assert "search_knowledge" in registry
        assert "search_papers" in registry

    def test_react_method_exists_and_is_callable(self):
        """branch.ReAct exists as an async method."""
        import asyncio

        from lionagi import Branch

        branch = Branch(
            tools=[search_knowledge],
            name="react_branch",
        )
        assert hasattr(branch, "ReAct")
        assert asyncio.iscoroutinefunction(branch.ReAct)

    def test_operate_method_exists_and_is_callable(self):
        """branch.operate exists as an async method."""
        import asyncio

        from lionagi import Branch

        branch = Branch(name="operate_branch")
        assert hasattr(branch, "operate")
        assert asyncio.iscoroutinefunction(branch.operate)

    def test_tool_registration_with_search_functions(self):
        """Search-like functions are properly registered as tools."""
        from lionagi import Branch

        branch = Branch(tools=[search_knowledge, search_papers])
        registry = branch.acts.registry

        # Tools are registered by function name
        assert "search_knowledge" in registry
        assert "search_papers" in registry

        # The tool objects are callable
        knowledge_tool = registry["search_knowledge"]
        papers_tool = registry["search_papers"]
        assert knowledge_tool is not None
        assert papers_tool is not None

    def test_tool_functions_return_expected_values(self):
        """The search tool functions produce expected output."""
        result = search_knowledge("quantum computing")
        assert result == "Results for: quantum computing"

        result = search_papers("machine learning", max_results=3)
        assert result == "Papers about: machine learning"

    def test_branch_with_tools_and_system_prompt(self):
        """Branch can combine system prompt and tools for a RAG agent."""
        from lionagi import Branch

        branch = Branch(
            system=(
                "You are a research assistant. Use search_knowledge "
                "to find relevant information before answering."
            ),
            tools=[search_knowledge, search_papers],
            name="rag_agent",
        )

        assert branch.system is not None
        assert branch.name == "rag_agent"
        assert len(branch.acts.registry) == 2
