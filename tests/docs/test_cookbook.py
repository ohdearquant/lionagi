"""Tests for code examples from cookbook documentation.

Covers: brainstorming.md, code-review-crew.md, claim-extraction.md,
research-synthesis.md, data-persistence.md, hr-automation.md.

All tests use mocked iModels -- no real API calls are made.
"""

import asyncio

import pytest
from pydantic import BaseModel, Field, ValidationError

from lionagi.session.branch import Branch
from lionagi.session.session import Session
from tests.utils.mock_factory import LionAGIMockFactory

# ---------------------------------------------------------------------------
# Inline Pydantic models (as cookbook docs define them)
# ---------------------------------------------------------------------------


class Claim(BaseModel):
    text: str
    source: str
    confidence: float = Field(ge=0, le=1)


class ReviewResult(BaseModel):
    issues: list[str]
    severity: str
    recommendation: str


class CandidateEvaluation(BaseModel):
    name: str
    score: float = Field(ge=0, le=100)
    strengths: list[str]
    concerns: list[str]
    recommendation: str


# ---------------------------------------------------------------------------
# Brainstorming cookbook
# ---------------------------------------------------------------------------


class TestBrainstorming:
    """Tests for brainstorming.md cookbook patterns."""

    def test_diverse_personality_branches(self):
        """Creating multiple branches with diverse personality system prompts."""
        personas = [
            "You are a bold, risk-taking entrepreneur.",
            "You are a cautious, detail-oriented analyst.",
            "You are a creative, out-of-the-box thinker.",
        ]

        branches = []
        for persona in personas:
            branch = LionAGIMockFactory.create_mocked_branch(
                name=f"brainstormer_{len(branches)}",
                response=f"Idea from persona {len(branches)}",
            )
            # Verify we can set a system prompt on construction
            persona_branch = Branch(
                system=persona,
                name=f"brainstormer_{len(branches)}",
            )
            assert persona_branch.system is not None
            assert persona in persona_branch.system.rendered
            branches.append(branch)

        assert len(branches) == 3
        for i, branch in enumerate(branches):
            assert branch.name == f"brainstormer_{i}"

    @pytest.mark.asyncio
    async def test_parallel_brainstorm_gather(self):
        """asyncio.gather pattern with multiple mocked branches."""
        branches = [
            LionAGIMockFactory.create_mocked_branch(
                name=f"thinker_{i}",
                response=f"Idea #{i}: a creative solution",
            )
            for i in range(3)
        ]

        async def get_idea(branch: Branch) -> str:
            result = await branch.communicate("Generate a creative idea")
            return result

        results = await asyncio.gather(*(get_idea(b) for b in branches))

        assert len(results) == 3
        for result in results:
            assert result is not None

    def test_brainstorm_branches_have_independent_histories(self):
        """Each brainstorm branch maintains its own message history."""
        b1 = Branch(system="You are optimistic.", name="optimist")
        b2 = Branch(system="You are pessimistic.", name="pessimist")

        assert b1.id != b2.id
        assert b1.system.rendered != b2.system.rendered
        # Each branch has exactly one message (the system message)
        assert len(b1.messages) == 1
        assert len(b2.messages) == 1


# ---------------------------------------------------------------------------
# Code Review Crew cookbook
# ---------------------------------------------------------------------------


class TestCodeReviewCrew:
    """Tests for code-review-crew.md cookbook patterns."""

    def test_reviewer_branches_creation(self):
        """Creating security, performance, maintainability reviewer branches."""
        reviewers = {
            "security": "You are a security expert. Find vulnerabilities.",
            "performance": "You are a performance expert. Find bottlenecks.",
            "maintainability": "You are a maintainability expert. Assess code clarity.",
        }

        branches = {}
        for role, prompt in reviewers.items():
            branch = Branch(system=prompt, name=role)
            branches[role] = branch

        assert len(branches) == 3
        assert "security" in branches
        assert "performance" in branches
        assert "maintainability" in branches

        for role, branch in branches.items():
            assert branch.name == role
            assert branch.system is not None

    def test_review_result_model_validates(self):
        """ReviewResult pydantic model validates as expected."""
        result = ReviewResult(
            issues=["SQL injection risk", "Missing input validation"],
            severity="high",
            recommendation="Add parameterized queries and input sanitization.",
        )
        assert len(result.issues) == 2
        assert result.severity == "high"

        # Empty issues list is valid
        clean = ReviewResult(issues=[], severity="low", recommendation="Code looks good.")
        assert len(clean.issues) == 0

    def test_builder_exists(self):
        """OperationGraphBuilder (Builder) exists for workflow construction."""
        from lionagi.operations.builder import OperationGraphBuilder

        assert OperationGraphBuilder is not None


# ---------------------------------------------------------------------------
# Claim Extraction cookbook
# ---------------------------------------------------------------------------


class TestClaimExtraction:
    """Tests for claim-extraction.md cookbook patterns."""

    def test_claim_model_validation(self):
        """Claim pydantic model with text/source/confidence fields validates."""
        claim = Claim(
            text="The earth orbits the sun.",
            source="astronomy textbook",
            confidence=0.99,
        )
        assert claim.text == "The earth orbits the sun."
        assert claim.source == "astronomy textbook"
        assert claim.confidence == 0.99

    def test_claim_confidence_bounds(self):
        """Claim confidence must be between 0 and 1."""
        # Valid boundary values
        Claim(text="t", source="s", confidence=0.0)
        Claim(text="t", source="s", confidence=1.0)

        # Invalid values
        with pytest.raises(ValidationError):
            Claim(text="t", source="s", confidence=1.5)
        with pytest.raises(ValidationError):
            Claim(text="t", source="s", confidence=-0.1)

    def test_branch_with_extraction_system_prompt(self):
        """Branch with a claim extraction system prompt can be created."""
        extraction_prompt = (
            "You are a claim extraction specialist. "
            "Extract factual claims from the provided text. "
            "For each claim, identify the source and your confidence level."
        )
        branch = Branch(system=extraction_prompt, name="claim_extractor")
        assert branch.system is not None
        assert "claim extraction" in branch.system.rendered

    @pytest.mark.asyncio
    async def test_extraction_with_mocked_branch(self, mocked_branch_structured):
        """Branch with tools for extraction patterns returns a result."""
        result = await mocked_branch_structured.communicate(
            "Extract claims from: The sun is a star located at the center of the solar system."
        )
        assert result is not None


# ---------------------------------------------------------------------------
# Research Synthesis cookbook
# ---------------------------------------------------------------------------


class TestResearchSynthesis:
    """Tests for research-synthesis.md cookbook patterns."""

    def test_session_with_researcher_and_synthesizer(self):
        """Session with researcher and synthesizer branches."""
        session = Session()

        researcher = session.new_branch(
            system="You are a thorough researcher. Gather relevant information.",
            name="researcher",
        )
        synthesizer = session.new_branch(
            system="You are a synthesizer. Combine research into coherent summaries.",
            name="synthesizer",
        )

        assert researcher.name == "researcher"
        assert synthesizer.name == "synthesizer"
        # Session includes the default branch plus the two we created
        assert len(session.branches) == 3

    def test_fan_out_pattern_construction(self):
        """Fan-out/fan-in pattern: multiple researcher branches feed a synthesizer."""
        session = Session()

        topics = [
            "machine learning",
            "natural language processing",
            "robotics",
        ]
        researchers = []
        for topic in topics:
            branch = session.new_branch(
                system=f"You are a researcher specializing in {topic}.",
                name=f"researcher_{topic.replace(' ', '_')}",
            )
            researchers.append(branch)

        synthesizer = session.new_branch(
            system="You are a synthesis expert. Combine findings from multiple researchers.",
            name="synthesizer",
        )

        assert len(researchers) == 3
        assert synthesizer.name == "synthesizer"
        # default_branch + 3 researchers + 1 synthesizer = 5
        assert len(session.branches) == 5

    def test_session_get_branch_by_name(self):
        """Session can retrieve branches by name."""
        session = Session()
        session.new_branch(system="Researcher prompt", name="my_researcher")

        found = session.get_branch("my_researcher")
        assert found is not None
        assert found.name == "my_researcher"


# ---------------------------------------------------------------------------
# Data Persistence cookbook
# ---------------------------------------------------------------------------


class TestDataPersistence:
    """Tests for data-persistence.md cookbook patterns."""

    def test_branch_to_dict_returns_dict(self):
        """branch.to_dict() returns a dictionary."""
        branch = LionAGIMockFactory.create_mocked_branch(
            name="persist_test",
            response="some response",
        )
        data = branch.to_dict()
        assert isinstance(data, dict)
        assert "messages" in data

    def test_branch_roundtrip_serialization(self):
        """Branch.from_dict(branch.to_dict()) roundtrips structurally."""
        original = LionAGIMockFactory.create_mocked_branch(
            name="roundtrip_test",
            user="roundtrip_user",
            response="roundtrip response",
        )
        data = original.to_dict()
        restored = Branch.from_dict(data)

        assert isinstance(restored, Branch)
        assert restored.name == original.name
        assert restored.user == original.user

    def test_branch_to_df_method_exists(self):
        """branch.to_df() exists as a callable method."""
        branch = Branch(system="test system", name="df_test")
        assert callable(branch.to_df)

    def test_branch_to_df_returns_dataframe(self):
        """branch.to_df() returns a pandas DataFrame."""
        import pandas as pd

        branch = Branch(system="test system", name="df_test")
        df = branch.to_df()
        assert isinstance(df, pd.DataFrame)
        # System message should be one row
        assert len(df) == 1


# ---------------------------------------------------------------------------
# HR Automation cookbook
# ---------------------------------------------------------------------------


class TestHRAutomation:
    """Tests for hr-automation.md cookbook patterns."""

    def test_multi_branch_workflow_creation(self):
        """Multi-branch workflow: screener, interviewer, evaluator branches."""
        screener = Branch(
            system="You are an HR screener. Review resumes for minimum qualifications.",
            name="screener",
        )
        interviewer = Branch(
            system="You are an interviewer. Conduct structured behavioral interviews.",
            name="interviewer",
        )
        evaluator = Branch(
            system="You are an evaluator. Score candidates based on interview performance.",
            name="evaluator",
        )

        workflow = [screener, interviewer, evaluator]
        assert len(workflow) == 3
        assert workflow[0].name == "screener"
        assert workflow[1].name == "interviewer"
        assert workflow[2].name == "evaluator"

    def test_candidate_evaluation_model(self):
        """CandidateEvaluation pydantic model validates properly."""
        evaluation = CandidateEvaluation(
            name="Alice Smith",
            score=85.5,
            strengths=["Strong technical skills", "Good communication"],
            concerns=["Limited management experience"],
            recommendation="Proceed to final round",
        )
        assert evaluation.name == "Alice Smith"
        assert evaluation.score == 85.5
        assert len(evaluation.strengths) == 2

        # Score bounds
        with pytest.raises(ValidationError):
            CandidateEvaluation(
                name="X",
                score=101,
                strengths=[],
                concerns=[],
                recommendation="No",
            )
        with pytest.raises(ValidationError):
            CandidateEvaluation(
                name="X",
                score=-1,
                strengths=[],
                concerns=[],
                recommendation="No",
            )

    def test_state_persistence_pattern(self):
        """branch.to_dict() captures state for HR workflow persistence."""
        screener = LionAGIMockFactory.create_mocked_branch(
            name="screener",
            response="Candidate meets minimum qualifications.",
        )

        state = screener.to_dict()
        assert isinstance(state, dict)
        assert "messages" in state

        # State can be restored
        restored = Branch.from_dict(state)
        assert restored.name == "screener"

    @pytest.mark.asyncio
    async def test_sequential_workflow_execution(self):
        """Branches can be invoked sequentially to simulate a pipeline."""
        screener = LionAGIMockFactory.create_mocked_branch(
            name="screener", response="Candidate passes screening."
        )
        evaluator = LionAGIMockFactory.create_mocked_branch(
            name="evaluator", response="Score: 82/100. Recommend hire."
        )

        # Step 1: screening
        screen_result = await screener.communicate("Review this resume: 5 years Python experience.")
        assert screen_result is not None

        # Step 2: evaluation using screening result as context
        eval_result = await evaluator.communicate(
            f"Evaluate candidate based on screening: {screen_result}"
        )
        assert eval_result is not None
