# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""lionagi.engines — domain-specific agent engines over the reactive substrate (ADR-0034)."""

from __future__ import annotations

from .coding import (
    ChangeProposed,
    CodeResultRecorded,
    CodingChainEvent,
    CodingEngine,
    CodingRun,
    TestsRan,
    WorkPlanned,
)
from .coding import (
    VerifyResult as CodeVerifyResult,
)
from .engine import (
    ChainRun,
    Engine,
    EngineBudgetError,
    EngineEvent,
    EngineResult,
    EngineRun,
    JudgeVerdict,
)
from .hypothesis import (
    ApplicationMapped,
    ChainEvent,
    ConclusionDrawn,
    EvidenceCollected,
    ExperimentDesigned,
    FindingPosted,
    HypothesisEngine,
    HypothesisFormed,
    HypothesisRun,
    QuestionRaised,
    ResultRecorded,
    render_evidence,
    trace_chains,
)
from .planning import PlanError, PlanningEngine
from .research import (
    ContradictionFound,
    DepthRequested,
    FindingEmitted,
    ResearchEngine,
)
from .review import (
    DEFAULT_DIMENSIONS,
    DimensionClean,
    IssueFound,
    ReviewEngine,
    ReviewVerdict,
    VerifyResult,
)

__all__ = (
    "Engine",
    "EngineRun",
    "ChainRun",
    "EngineEvent",
    "EngineBudgetError",
    "EngineResult",
    "JudgeVerdict",
    # planning (Planned-DAG shape — li o flow as an engine)
    "PlanningEngine",
    "PlanError",
    # research (Tree shape)
    "ResearchEngine",
    "FindingEmitted",
    "DepthRequested",
    "ContradictionFound",
    # review (Dimensional shape)
    "ReviewEngine",
    "IssueFound",
    "DimensionClean",
    "VerifyResult",
    "ReviewVerdict",
    "DEFAULT_DIMENSIONS",
    # hypothesis (Chain shape — evidence chains for decisions)
    "HypothesisEngine",
    "HypothesisRun",
    "ChainEvent",
    "FindingPosted",
    "QuestionRaised",
    "EvidenceCollected",
    "HypothesisFormed",
    "ExperimentDesigned",
    "ResultRecorded",
    "ConclusionDrawn",
    "ApplicationMapped",
    "trace_chains",
    "render_evidence",
    # coding (Gated-Loop shape); CodeVerifyResult disambiguates the review
    # engine's VerifyResult at the package level
    "CodingEngine",
    "CodingRun",
    "CodingChainEvent",
    "WorkPlanned",
    "ChangeProposed",
    "TestsRan",
    "CodeVerifyResult",
    "CodeResultRecorded",
)
