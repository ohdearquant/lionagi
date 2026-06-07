#!/usr/bin/env python3
"""
Apples-to-Apples Agentic Framework Benchmark
--------------------------------------------

Design goals
- Minimize "apples vs oranges" by normalizing categories to *core-only, local-only* work:
  1) Imports: `import <package>`
  2) Orchestrator (minimal "ready" object), with no network and only core package modules.

- For LangChain we use **langchain_core** primitives (PromptTemplate, RunnableLambda) to avoid
  pulling `langchain_community` or other extras; that's the fairest baseline for chain composition.
  For LangGraph we compile a one-node identity graph using START/END (core API).
  For LlamaIndex we use SimpleChatEngine with MockLLM (both in core) to avoid network.
  For AutoGen we use a single ConversableAgent with llm_config=False (local-only).

- Two timing modes:
  - mode="cold": import + construct (one fresh Python process per run).
  - mode="construct": pre-import (un-timed), then time *only* the construction body.

- Memory:
  - Report both RSS and USS (USS ≈ private pages for truer per-process footprint).

- Robustness:
  - CPU pinning best-effort (Linux/psutil).
  - PYTHONHASHSEED=0 for stable hashing.
  - p95, MAD, 10% trimmed mean.

Outputs
- Prints console tables + a feature-parity matrix.
- Writes:
    benchmark_summary_<timestamp>.csv
    benchmark_detailed_<timestamp>.csv
    benchmark_results_<timestamp>.json
"""

import argparse
import csv
import json
import math
import os
import re
import statistics
import subprocess
import sys
import textwrap
from datetime import datetime

RUNS = 20
# Turn this on if you want to ALSO benchmark the common "ecosystem" cost versions
# (e.g., LangGraph+LangChain community fake LLM). Left OFF by default for parity.
INCLUDE_COMMUNITY_EXTRAS = False

# -----------------------------
# Benchmark cases (normalized)
# -----------------------------

benchmark_categories = {
    "imports": {
        "description": "Core package import (cold start, core-only)",
        "cases": {
            "lionagi": "import importlib; importlib.import_module('lionagi')",
            "langgraph": "import importlib; importlib.import_module('langgraph')",
            "langchain_core": "import importlib; importlib.import_module('langchain_core')",
            "llamaindex": "import importlib; importlib.import_module('llama_index.core')",
            "autogen": "import importlib; importlib.import_module('autogen')",
        },
    },
    "basic_primitives": {
        "description": "Basic primitive object creation (core building blocks)",
        "cases": {
            # LionAGI: Branch creation (basic orchestrator)
            "lionagi": textwrap.dedent(
                """
                from lionagi import Branch
                branch = Branch()
            """
            ),
            # LangGraph: StateGraph creation (no compilation)
            "langgraph": textwrap.dedent(
                """
                from typing_extensions import TypedDict
                from langgraph.graph import StateGraph

                class S(TypedDict):
                    x: int

                g = StateGraph(S)
            """
            ),
            # LangChain (core): PromptTemplate creation
            "langchain_core": textwrap.dedent(
                """
                from langchain_core.prompts import PromptTemplate
                prompt = PromptTemplate.from_template("Hello {name}")
            """
            ),
            # LlamaIndex: Document creation
            "llamaindex": textwrap.dedent(
                """
                from llama_index.core.schema import Document
                doc = Document(text="Test document")
            """
            ),
            # AutoGen: Basic agent creation
            "autogen": textwrap.dedent(
                """
                from autogen import ConversableAgent
                agent = ConversableAgent(
                    name="test",
                    llm_config=False,
                    human_input_mode="NEVER"
                )
            """
            ),
        },
    },
    "orchestrators": {
        "description": "Minimal orchestrator objects (ready-to-use state)",
        "cases": {
            # LionAGI: Session ready for use
            "lionagi": textwrap.dedent(
                """
                from lionagi import Session
                s = Session()
            """
            ),
            # LangGraph: Compiled graph ready for execution
            "langgraph": textwrap.dedent(
                """
                from typing_extensions import TypedDict
                from langgraph.graph import StateGraph, START, END

                class S(TypedDict):
                    x: int

                def echo(state: S):
                    return state

                g = StateGraph(S)
                g = g.add_node("echo", echo)
                g = g.add_edge(START, "echo").add_edge("echo", END)
                compiled = g.compile()
            """
            ),
            # LangChain (core): Runnable chain ready for execution
            "langchain_core": textwrap.dedent(
                """
                from langchain_core.prompts import PromptTemplate
                from langchain_core.runnables import RunnableLambda

                prompt = PromptTemplate.from_template("{x}")
                echo = RunnableLambda(lambda x: x)
                chain = prompt | echo
            """
            ),
            # LlamaIndex: Chat engine ready for queries
            "llamaindex": textwrap.dedent(
                """
                from llama_index.core import Settings
                from llama_index.core.llms import MockLLM
                from llama_index.core.embeddings import MockEmbedding
                from llama_index.core.chat_engine import SimpleChatEngine

                Settings.llm = MockLLM()
                Settings.embed_model = MockEmbedding(embed_dim=32)
                engine = SimpleChatEngine.from_defaults()
            """
            ),
            # AutoGen: Agent ready for conversation
            "autogen": textwrap.dedent(
                """
                from autogen import ConversableAgent
                assistant = ConversableAgent(
                    name="assistant",
                    llm_config=False,
                    human_input_mode="NEVER"
                )
            """
            ),
        },
    },
    "workflow_setup": {
        "description": "Practical workflow setup (multi-component coordination)",
        "cases": {
            # LionAGI: Branch ready for conversation
            "lionagi": textwrap.dedent(
                """
                from lionagi import Branch

                # Create branch for conversation (no network)
                branch = Branch(
                    system="You are a helpful assistant"
                )
            """
            ),
            # LangGraph: Multi-node workflow graph
            "langgraph": textwrap.dedent(
                """
                from typing_extensions import TypedDict
                from langgraph.graph import StateGraph, START, END

                class State(TypedDict):
                    messages: list
                    count: int

                def process_msg(state: State):
                    return {"messages": state["messages"] + ["processed"], "count": state["count"] + 1}

                def validate(state: State):
                    return {"messages": state["messages"] + ["validated"], "count": state["count"]}

                workflow = StateGraph(State)
                workflow.add_node("process", process_msg)
                workflow.add_node("validate", validate)
                workflow.add_edge(START, "process")
                workflow.add_edge("process", "validate")
                workflow.add_edge("validate", END)
                app = workflow.compile()
            """
            ),
            # LangChain (core): Sequential processing chain
            "langchain_core": textwrap.dedent(
                """
                from langchain_core.prompts import PromptTemplate
                from langchain_core.runnables import RunnableLambda, RunnableSequence

                # Create processing pipeline
                template = PromptTemplate.from_template("Process: {input}")
                processor = RunnableLambda(lambda x: {"result": f"processed_{x['input']}"})
                validator = RunnableLambda(lambda x: {"final": f"validated_{x['result']}"})

                pipeline = RunnableSequence(template, processor, validator)
            """
            ),
            # LlamaIndex: Query engine with custom retrieval
            "llamaindex": textwrap.dedent(
                """
                from llama_index.core import Settings, VectorStoreIndex
                from llama_index.core.llms import MockLLM
                from llama_index.core.schema import Document
                from llama_index.core.embeddings import MockEmbedding

                # Setup mock environment
                Settings.llm = MockLLM()
                Settings.embed_model = MockEmbedding(embed_dim=32)

                # Create documents and index
                docs = [Document(text=f"Document {i}") for i in range(3)]
                index = VectorStoreIndex.from_documents(
                    docs, embed_model=Settings.embed_model
                )
                query_engine = index.as_query_engine()
            """
            ),
            # AutoGen: Multi-agent conversation setup
            "autogen": textwrap.dedent(
                """
                from autogen import ConversableAgent, GroupChat, GroupChatManager

                # Create multiple agents
                user = ConversableAgent("user", llm_config=False, human_input_mode="NEVER")
                assistant = ConversableAgent("assistant", llm_config=False, human_input_mode="NEVER")
                critic = ConversableAgent("critic", llm_config=False, human_input_mode="NEVER")

                # Setup group chat
                groupchat = GroupChat(
                    agents=[user, assistant, critic],
                    messages=[],
                    max_round=3
                )
                manager = GroupChatManager(groupchat=groupchat, llm_config=False)
            """
            ),
        },
    },
    "data_processing": {
        "description": "Data structure creation and processing (realistic workload)",
        "cases": {
            # LionAGI: Create multiple messages and nodes
            "lionagi": textwrap.dedent(
                """
                from lionagi.protocols.generic.element import Element

                # Create multiple elements
                elements = []
                for i in range(10):
                    element = Element()
                    element.metadata["id"] = f"element_{i}"
                    elements.append(element)

                # Process elements
                processed = [str(elem) for elem in elements]
            """
            ),
            # LangGraph: State processing with larger data
            "langgraph": textwrap.dedent(
                """
                from typing_extensions import TypedDict
                from langgraph.graph import StateGraph, START, END

                class DataState(TypedDict):
                    items: list
                    processed: list

                def batch_proc(state: DataState):
                    processed = [f"processed_{item}" for item in state["items"]]
                    return {"items": state["items"], "processed": processed}

                # Create workflow with data
                items = [f"item_{i}" for i in range(20)]
                workflow = StateGraph(DataState)
                workflow.add_node("process", batch_proc)
                workflow.add_edge(START, "process")
                workflow.add_edge("process", END)
                app = workflow.compile()

                # Initialize state
                initial_state = {"items": items, "processed": []}
            """
            ),
            # LangChain (core): Document processing pipeline
            "langchain_core": textwrap.dedent(
                """
                from langchain_core.prompts import PromptTemplate
                from langchain_core.runnables import RunnableLambda

                # Create documents
                docs = [{"content": f"Document {i} content"} for i in range(15)]

                # Processing pipeline
                extractor = RunnableLambda(lambda x: {"title": f"Title_{x['content'][:10]}", "content": x["content"]})
                summarizer = RunnableLambda(lambda x: {"summary": f"Summary of {x['title']}", "original": x})

                # Process documents
                pipeline = extractor | summarizer
                processed_docs = [pipeline.invoke(doc) for doc in docs[:5]]  # Process subset
            """
            ),
            # LlamaIndex: Multiple document indexing
            "llamaindex": textwrap.dedent(
                """
                from llama_index.core import Settings, VectorStoreIndex
                from llama_index.core.llms import MockLLM
                from llama_index.core.schema import Document
                from llama_index.core.embeddings import MockEmbedding

                Settings.llm = MockLLM()
                Settings.embed_model = MockEmbedding(embed_dim=32)

                # Create multiple documents
                documents = []
                for i in range(25):
                    doc = Document(
                        text=f"This is document {i} with content about topic {i%5}",
                        metadata={"id": i, "topic": i%5}
                    )
                    documents.append(doc)

                # Create index
                index = VectorStoreIndex.from_documents(
                    documents[:10], embed_model=Settings.embed_model
                )  # Index subset
            """
            ),
            # AutoGen: Message processing with multiple agents
            "autogen": textwrap.dedent(
                """
                from autogen import ConversableAgent

                # Create agents
                agents = []
                for i in range(5):
                    agent = ConversableAgent(
                        name=f"agent_{i}",
                        llm_config=False,
                        human_input_mode="NEVER"
                    )
                    agents.append(agent)

                # Create messages
                messages = []
                for i in range(30):
                    message = {
                        "role": "user",
                        "content": f"Message {i}",
                        "metadata": {"id": i, "priority": i%3}
                    }
                    messages.append(message)
            """
            ),
        },
    },
}

# Optional ecosystem cases that people *often* end up importing in real apps.
if INCLUDE_COMMUNITY_EXTRAS:
    benchmark_categories["ecosystem_imports"] = {
        "description": "Common ecosystem imports (not apples-to-apples; optional)",
        "cases": {
            "langchain_full": "import importlib; importlib.import_module('langchain')",
            "langchain_community_fake_llm": textwrap.dedent(
                """
                # Fake LLM for tests (pulls community package)
                from langchain_community.llms.fake import FakeListLLM
                llm = FakeListLLM(responses=["ok"])
            """
            ),
        },
    }

# -----------------------------
# Harness internals
# -----------------------------


def _safe_block(s: str) -> str:
    return s.replace('"""', r"\"\"\"")


def split_imports_and_body(original_code: str):
    """Separate import lines from the rest so we can time construct-only."""
    cleaned = textwrap.dedent(original_code)
    cleaned = re.sub(
        r"^\s*print\(\s*\(time\.perf_counter\(\)\s*-\s*t\)\s*\*1000\s*\)\s*$",
        "",
        cleaned,
        flags=re.MULTILINE,
    ).strip()

    import_re = re.compile(r"^\s*(import\s+\S+|from\s+[\w\.]+\s+import\s+.+)")
    lines = cleaned.splitlines()
    import_lines, body_lines = [], []
    for ln in lines:
        if import_re.match(ln.strip()):
            import_lines.append(ln)
        else:
            body_lines.append(ln)

    def _strip_empty(lst):
        while lst and not lst[0].strip():
            lst.pop(0)
        while lst and not lst[-1].strip():
            lst.pop()
        return lst

    import_lines = _strip_empty(import_lines)
    body_lines = _strip_empty(body_lines) or ["pass"]
    return "\n".join(import_lines), "\n".join(body_lines)


def create_wrapper_code(original_code: str, mode: str, label: str):
    """
    mode: 'cold' (import+construct)  or  'construct' (post-import construct only)
    """
    imports_code, body_code = split_imports_and_body(original_code)

    cpu_pin = """
try:
    import os, psutil
    try:
        os.sched_setaffinity(0, {0})
    except Exception:
        try:
            psutil.Process().cpu_affinity([0])
        except Exception:
            pass
except Exception:
    pass
"""

    mem_helpers = """
def _mem_mb(proc):
    rss = proc.memory_info().rss / (1024*1024)
    try:
        uss = proc.memory_full_info().uss / (1024*1024)
    except Exception:
        uss = rss
    return rss, uss
"""

    if mode == "cold":
        combined_code = imports_code + chr(10) + body_code
        return f'''
import gc, json, os, psutil, sys, time
{cpu_pin}
gc.collect()

# Force a true cold start for targeted frameworks
if hasattr(sys, "modules"):
    targets = {{"lionagi", "langgraph", "langchain", "langchain_core", "llama_index", "autogen", "langchain_community"}}
    for module_name in list(sys.modules):
        if any(module_name.startswith(t) for t in targets):
            del sys.modules[module_name]

process = psutil.Process(os.getpid())
{mem_helpers}
rss_before, uss_before = _mem_mb(process)

start = time.perf_counter()
exec("""{_safe_block(combined_code)}""", globals())
end = time.perf_counter()

rss_after, uss_after = _mem_mb(process)
print(json.dumps({{
    "mode": "cold",
    "framework": "{label}",
    "execution_time_ms": round((end - start) * 1000, 6),
    "rss_before_mb": round(rss_before, 6),
    "rss_after_mb": round(rss_after, 6),
    "rss_delta_mb": round(rss_after - rss_before, 6),
    "uss_before_mb": round(uss_before, 6),
    "uss_after_mb": round(uss_after, 6),
    "uss_delta_mb": round(uss_after - uss_before, 6)
}}))
'''
    elif mode == "construct":
        return f'''
import gc, json, os, psutil, sys, time
{cpu_pin}
gc.collect()
process = psutil.Process(os.getpid())
{mem_helpers}

# Pre-imports (un-timed)
exec("""{_safe_block(imports_code)}""", globals())

rss_before, uss_before = _mem_mb(process)
start = time.perf_counter()
exec("""{_safe_block(body_code)}""", globals())
end = time.perf_counter()
rss_after, uss_after = _mem_mb(process)

print(json.dumps({{
    "mode": "construct",
    "framework": "{label}",
    "execution_time_ms": round((end - start) * 1000, 6),
    "rss_before_mb": round(rss_before, 6),
    "rss_after_mb": round(rss_after, 6),
    "rss_delta_mb": round(rss_after - rss_before, 6),
    "uss_before_mb": round(uss_before, 6),
    "uss_after_mb": round(uss_after, 6),
    "uss_delta_mb": round(uss_after - uss_before, 6)
}}))
'''
    else:
        raise ValueError(f"Unknown mode: {mode}")


def _mad(vals):
    if not vals:
        return 0.0
    med = statistics.median(vals)
    return float(statistics.median([abs(v - med) for v in vals]))


def _p95(vals):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = 0.95 * (len(s) - 1)
    f, c = math.floor(k), math.ceil(k)
    return float(s[int(k)]) if f == c else float(s[f] + (k - f) * (s[c] - s[f]))


def _trimmed_mean(vals, proportion_to_cut=0.1):
    if not vals:
        return 0.0
    n = len(vals)
    k = int(n * proportion_to_cut)
    trimmed = sorted(vals)[k : n - k] if n > 2 * k else vals
    return float(statistics.mean(trimmed))


def run_case(label, code, mode):
    individual_runs = []
    times, rss_deltas, uss_deltas = [], [], []

    wrapper_code = create_wrapper_code(code, mode, label)

    for run_number in range(RUNS):
        try:
            start_time = datetime.now()
            # Harden environment to avoid accidental network/key usage
            child_env = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                # Common provider keys blanked
                "OPENAI_API_KEY": "",
                "AZURE_OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "COHERE_API_KEY": "",
                "GOOGLE_API_KEY": "",
                "MISTRAL_API_KEY": "",
                # Avoid extra threads/noise in some deps
                "TOKENIZERS_PARALLELISM": "false",
            }
            result = subprocess.run(
                [sys.executable, "-c", wrapper_code],
                capture_output=True,
                text=True,
                check=True,
                env=child_env,
                timeout=30,  # prevent hangs
            )
            end_time = datetime.now()

            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            if not lines:
                raise RuntimeError("No benchmark output produced")
            data = json.loads(lines[-1])

            execution_time = data["execution_time_ms"]
            rss_before, rss_after, rss_delta = (
                data["rss_before_mb"],
                data["rss_after_mb"],
                data["rss_delta_mb"],
            )
            uss_before, uss_after, uss_delta = (
                data["uss_before_mb"],
                data["uss_after_mb"],
                data["uss_delta_mb"],
            )

            times.append(execution_time)
            rss_deltas.append(rss_delta)
            uss_deltas.append(uss_delta)

            individual_runs.append(
                {
                    "framework": label,
                    "mode": mode,
                    "run_number": run_number + 1,
                    "execution_time_ms": round(execution_time, 3),
                    # Back-compat: memory_* == RSS
                    "memory_before_mb": round(rss_before, 3),
                    "memory_after_mb": round(rss_after, 3),
                    "memory_delta_mb": round(rss_delta, 3),
                    "rss_before_mb": round(rss_before, 3),
                    "rss_after_mb": round(rss_after, 3),
                    "rss_delta_mb": round(rss_delta, 3),
                    "uss_before_mb": round(uss_before, 3),
                    "uss_after_mb": round(uss_after, 3),
                    "uss_delta_mb": round(uss_delta, 3),
                    "wall_clock_start": start_time.isoformat(),
                    "wall_clock_end": end_time.isoformat(),
                    "wall_clock_duration_ms": round(
                        (end_time - start_time).total_seconds() * 1000, 3
                    ),
                }
            )

        except subprocess.TimeoutExpired as e:
            end_time = datetime.now()
            print(f"TIMEOUT in {label} run {run_number + 1}: exceeded {e.timeout}s")
            individual_runs.append(
                {
                    "framework": label,
                    "mode": mode,
                    "run_number": run_number + 1,
                    "execution_time_ms": None,
                    "error": f"timeout {e.timeout}s",
                    "memory_before_mb": None,
                    "memory_after_mb": None,
                    "memory_delta_mb": None,
                    "rss_before_mb": None,
                    "rss_after_mb": None,
                    "rss_delta_mb": None,
                    "uss_before_mb": None,
                    "uss_after_mb": None,
                    "uss_delta_mb": None,
                    "wall_clock_start": start_time.isoformat(),
                    "wall_clock_end": end_time.isoformat(),
                    "wall_clock_duration_ms": round(
                        (end_time - start_time).total_seconds() * 1000, 3
                    ),
                }
            )
            # continue to next run/case

        except subprocess.CalledProcessError as e:
            print(f"ERROR in {label} run {run_number + 1}: {e.stderr}")
            individual_runs.append(
                {
                    "framework": label,
                    "mode": mode,
                    "run_number": run_number + 1,
                    "execution_time_ms": None,
                    "error": e.stderr,
                    "memory_before_mb": None,
                    "memory_after_mb": None,
                    "memory_delta_mb": None,
                    "rss_before_mb": None,
                    "rss_after_mb": None,
                    "rss_delta_mb": None,
                    "uss_before_mb": None,
                    "uss_after_mb": None,
                    "uss_delta_mb": None,
                    "wall_clock_start": None,
                    "wall_clock_end": None,
                    "wall_clock_duration_ms": None,
                }
            )
        except Exception as e:
            print(f"UNEXPECTED ERROR in {label} run {run_number + 1}: {str(e)}")
            individual_runs.append(
                {
                    "framework": label,
                    "mode": mode,
                    "run_number": run_number + 1,
                    "execution_time_ms": None,
                    "error": str(e),
                    "memory_before_mb": None,
                    "memory_after_mb": None,
                    "memory_delta_mb": None,
                    "rss_before_mb": None,
                    "rss_after_mb": None,
                    "rss_delta_mb": None,
                    "uss_before_mb": None,
                    "uss_after_mb": None,
                    "uss_delta_mb": None,
                    "wall_clock_start": None,
                    "wall_clock_end": None,
                    "wall_clock_duration_ms": None,
                }
            )

    if not times:
        return None, individual_runs

    return {
        "framework": label,
        "mode": mode,
        "mean_ms": round(statistics.mean(times), 1),
        "median_ms": round(statistics.median(times), 1),
        "stdev_ms": round(statistics.pstdev(times), 1),
        "mad_ms": round(_mad(times), 1),
        "trimmed_mean_ms": round(_trimmed_mean(times), 1),
        "p95_ms": round(_p95(times), 1),
        "min_ms": round(min(times), 1),
        "max_ms": round(max(times), 1),
        "mean_rss_mb": round(statistics.mean(rss_deltas), 3),
        "max_rss_mb": round(max(rss_deltas), 3),
        "mean_uss_mb": round(statistics.mean(uss_deltas), 3),
        "max_uss_mb": round(max(uss_deltas), 3),
        "successful_runs": len(times),
        "total_runs": RUNS,
    }, individual_runs


def print_header():
    print(f"🦁 Apples-to-Apples Agentic Benchmark — {RUNS} runs")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Timestamp: {datetime.now().isoformat()}")
    print()


def print_category_table(category_name, category_data, results):
    # Separate results by mode
    cold_results = []
    construct_results = []

    for result, runs in results:
        if result["mode"] == "cold":
            cold_results.append((result, runs))
        else:
            construct_results.append((result, runs))

    # Print cold mode table
    if cold_results:
        print(f"\n📊 {category_data['description']} — **Cold Mode**")
        print("=" * 125)
        print(
            f"{'Framework':<14} {'Median':<8} {'P95':<8} {'Min':<8} {'Max':<8} "
            f"{'StdDev':<8} {'RSS(MB)':<10} {'USS(MB)':<10} {'Range':<20}"
        )
        print("-" * 125)
        for result, _runs in cold_results:
            r = result
            rng = f"{r['min_ms']:.1f}-{r['max_ms']:.1f}"
            rss_mem = f"{r['mean_rss_mb']:.1f}"
            uss_mem = f"{r['mean_uss_mb']:.1f}"
            print(
                f"{r['framework']:<14} {r['median_ms']:<8.1f} {r['p95_ms']:<8.1f} "
                f"{r['min_ms']:<8.1f} {r['max_ms']:<8.1f} {r['stdev_ms']:<8.1f} "
                f"{rss_mem:<10} {uss_mem:<10} {rng:<20}"
            )

    # Print construct mode table
    if construct_results:
        print(f"\n📊 {category_data['description']} — **Construct Mode**")
        print("=" * 125)
        print(
            f"{'Framework':<14} {'Median':<8} {'P95':<8} {'Min':<8} {'Max':<8} "
            f"{'StdDev':<8} {'RSS(MB)':<10} {'USS(MB)':<10} {'Range':<20}"
        )
        print("-" * 125)
        for result, _runs in construct_results:
            r = result
            rng = f"{r['min_ms']:.1f}-{r['max_ms']:.1f}"
            rss_mem = f"{r['mean_rss_mb']:.1f}"
            uss_mem = f"{r['mean_uss_mb']:.1f}"
            print(
                f"{r['framework']:<14} {r['median_ms']:<8.1f} {r['p95_ms']:<8.1f} "
                f"{r['min_ms']:<8.1f} {r['max_ms']:<8.1f} {r['stdev_ms']:<8.1f} "
                f"{rss_mem:<10} {uss_mem:<10} {rng:<20}"
            )


def feature_parity_matrix():
    """
    Print a quick parity matrix to show comparability of the orchestrator snippets.
    """
    rows = [
        # framework, object built, graph/chain compiled?, core-only?, LLM used?, network?, notes
        (
            "lionagi",
            "Session()",
            "N/A",
            "Yes",
            "No",
            "No",
            "Minimal runtime container",
        ),
        (
            "langgraph",
            "StateGraph(...).compile()",
            "Yes",
            "Yes",
            "No",
            "No",
            "One-node identity graph (START→node→END)",
        ),
        (
            "langchain_core",
            "PromptTemplate | RunnableLambda",
            "Yes",
            "Yes",
            "No",
            "No",
            "LCEL chain; no community deps",
        ),
        (
            "llamaindex",
            "SimpleChatEngine(MockLLM)",
            "Yes",
            "Yes",
            "MockLLM",
            "No",
            "Built-in MockLLM; no network",
        ),
        (
            "autogen",
            "ConversableAgent(llm_config=False)",
            "N/A",
            "Yes",
            "No",
            "No",
            "Single agent; LLM disabled",
        ),
    ]

    print("\n🧮 Feature Parity Matrix (orchestrators)")
    print("=" * 120)
    print(
        f"{'Framework':<16} {'Object':<38} {'Compiled?':<10} {'Core-only':<10} {'LLM?':<8} {'Network?':<10} {'Notes'}"
    )
    print("-" * 120)
    for r in rows:
        print(f"{r[0]:<16} {r[1]:<38} {r[2]:<10} {r[3]:<10} {r[4]:<8} {r[5]:<10} {r[6]}")


def main():
    parser = argparse.ArgumentParser(
        description="Apples-to-Apples Agentic Framework Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
        Examples:
          # Run benchmark with default 3 runs
          python benchmark_professional.py

          # Run with 20 runs for production data
          python benchmark_professional.py --runs 20

          # Run and generate markdown report automatically
          python benchmark_professional.py --report

          # Production run with report
          python benchmark_professional.py --runs 20 --report
        """
        ),
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=20,  # Default value
        help="Number of runs per test case (default: 20)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate markdown report after benchmark completes",
    )
    parser.add_argument(
        "--report-out",
        default="report.md",
        help="Output file for markdown report (default: report.md)",
    )
    args = parser.parse_args()

    # Update global RUNS if specified
    global RUNS
    if args.runs:
        RUNS = args.runs

    print_header()
    all_results, all_runs = [], []

    for category_name, category_data in benchmark_categories.items():
        # Build a list of (result, runs)
        cat_results = []
        for framework, code in category_data["cases"].items():
            modes = ["cold"] if category_name == "imports" else ["cold", "construct"]
            for mode in modes:
                result, runs = run_case(framework, code, mode)
                if result:
                    # Attach category metadata and store
                    result["category"] = category_name
                    result["category_description"] = category_data["description"]
                    all_results.append(result)
                    for run in runs:
                        run["category"] = category_name
                        run["category_description"] = category_data["description"]
                    all_runs.extend(runs)
                    cat_results.append((result, runs))

        # Sort by (framework, mode) then median time
        cat_results.sort(
            key=lambda x: (
                x[0]["framework"],
                0 if x[0]["mode"] == "cold" else 1,
                x[0]["median_ms"],
            )
        )
        print_category_table(category_name, category_data, cat_results)

    # Parity matrix at the end for quick reference
    feature_parity_matrix()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_csv = f"benchmark_summary_{timestamp}.csv"
    detailed_csv = f"benchmark_detailed_{timestamp}.csv"
    json_filename = f"benchmark_results_{timestamp}.json"

    # Summary CSV
    with open(summary_csv, "w", newline="") as f:
        fieldnames = [
            "category",
            "category_description",
            "framework",
            "mode",
            "mean_ms",
            "median_ms",
            "stdev_ms",
            "mad_ms",
            "trimmed_mean_ms",
            "p95_ms",
            "min_ms",
            "max_ms",
            "mean_rss_mb",
            "max_rss_mb",
            "mean_uss_mb",
            "max_uss_mb",
            "successful_runs",
            "total_runs",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_results:
            writer.writerow(r)

    # Detailed CSV
    with open(detailed_csv, "w", newline="") as f:
        fieldnames = [
            "category",
            "category_description",
            "framework",
            "mode",
            "run_number",
            "execution_time_ms",
            # Back-compat (RSS mirrored):
            "memory_before_mb",
            "memory_after_mb",
            "memory_delta_mb",
            # Explicit memory fields:
            "rss_before_mb",
            "rss_after_mb",
            "rss_delta_mb",
            "uss_before_mb",
            "uss_after_mb",
            "uss_delta_mb",
            "wall_clock_start",
            "wall_clock_end",
            "wall_clock_duration_ms",
            "error",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for run in all_runs:
            writer.writerow(run)

    # JSON bundle
    with open(json_filename, "w") as jf:
        json.dump(
            {
                "metadata": {
                    "runs": RUNS,
                    "python_version": sys.version.split()[0],
                    "timestamp": datetime.now().isoformat(),
                    "description": "Apples-to-Apples Agentic Framework Benchmark (cold & construct-only)",
                    "include_community_extras": INCLUDE_COMMUNITY_EXTRAS,
                },
                "categories": benchmark_categories,
                "summary_results": all_results,
                "detailed_results": all_runs,
            },
            jf,
            indent=2,
        )

    print("\n📄 Results exported to:")
    print(f"  📊 Summary:  {summary_csv}")
    print(f"  📋 Detailed: {detailed_csv}")
    print(f"  📦 JSON:     {json_filename}")

    # Generate report if requested
    if args.report:
        print("\n📝 Generating markdown report...")
        try:
            # Import here to avoid dependency if not using reports
            from generate_benchmark_report import main as generate_report

            # Create a mock args object for the report generator
            class ReportArgs:
                def __init__(self, summary, json_file, out, lion):
                    self.summary = summary
                    self.json = json_file
                    self.out = out
                    self.lion = lion

            # Run the report generator with our files
            old_argv = sys.argv
            sys.argv = ["generate_benchmark_report.py"]  # Mock argv to avoid argparse conflicts

            report_args = ReportArgs(
                summary=summary_csv,
                json_file=json_filename,
                out=args.report_out,
                lion="lionagi",
            )

            # Patch argparse.parse_args temporarily
            import argparse as ap_module

            old_parse_args = ap_module.ArgumentParser.parse_args
            ap_module.ArgumentParser.parse_args = lambda self, args=None, namespace=None: (
                report_args
            )

            try:
                generate_report()
            finally:
                # Restore original
                ap_module.ArgumentParser.parse_args = old_parse_args
                sys.argv = old_argv

        except ImportError:
            print("  ⚠️  Could not import generate_benchmark_report.py")
            print("  ℹ️  Make sure generate_benchmark_report.py is in the same directory")
        except Exception as e:
            print(f"  ❌ Report generation failed: {e}")


if __name__ == "__main__":
    main()
