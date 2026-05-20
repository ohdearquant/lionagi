# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

LNDL_SYSTEM_PROMPT = """LNDL — Structured Output with Natural Thinking

SYNTAX

Variables:
<lvar Model.field alias>value</lvar>      — namespaced (Pydantic model fields)
<lvar alias>value</lvar>                  — raw (scalars only)

Actions:
<lact Model.field alias>fn(arg="val")</lact>  — namespaced (result fills model field)
<lact alias>fn(arg="val")</lact>              — direct (entire output)

Output:
OUT{specName: [alias1, alias2], scalar: literal}

RULES

1. <lvar> and <lact> are SIBLINGS — NEVER nest one inside the other.
2. ONLY tags referenced in OUT{} are executed/used. Everything else is scratch (thinking).
3. OUT{} uses arrays for models, literals for scalars:
   OUT{report: [t, s], score: 0.85}
4. <lact> body is a Python function call with keyword args:
   fn(path="file.py", pattern="def", limit=10)

TYPE RULES

Scalars (float, str, int, bool):
  Literal:  OUT{score: 0.85}
  Variable: <lvar score s>0.85</lvar> → OUT{score: [s]}

Models (Pydantic):
  Array of aliases: OUT{report: [title, body]}
  Can mix lvars + lacts: OUT{report: [title, api_call, footer]}

Actions:
  Namespaced: <lact Report.summary s>summarize(text="...")</lact>
  Direct:     <lact data>fetch(url="...")</lact>
  Only executed if in OUT{}. Not in OUT{} = scratch thinking, not executed.

EXAMPLE 1: Variables only

Specs: report(Report: title, summary), quality(float)

<lvar Report.title t>Architecture Analysis</lvar>
<lvar Report.summary s>The system uses a layered design...</lvar>

OUT{report: [t, s], quality: 0.92}

EXAMPLE 2: Tool calls with arguments

Specs: analysis(Analysis: file_name, line_count, findings)

<lvar Analysis.file_name name>main.py</lvar>
<lact Analysis.line_count lc>count_lines(path="main.py")</lact>
<lact Analysis.findings f>grep_file(path="main.py", pattern="async def")</lact>

OUT{analysis: [name, lc, f]}

"lc" and "f" execute — their results fill line_count and findings.

EXAMPLE 3: Drafting — declare multiple, commit the best

Let me try two approaches...
<lact broad>search(query="AI", limit=100)</lact>
<lact focused>search(query="AI safety", limit=20)</lact>

<lvar Report.title t>AI Safety Analysis</lvar>
<lvar Report.summary s>Based on focused results...</lvar>

OUT{report: [t, s, focused]}

Only "focused" executes. "broad" was scratch thinking — never runs.

ERRORS TO AVOID

<lvar x><lact fn>fn()</lact></lvar>        # WRONG: nested tags — siblings only
OUT{report: {title: "X"}}                  # WRONG: constructor — use arrays
<lact name="X">fn()</lact>                 # WRONG: XML attributes — use <lact X>
<lact op>fn()</lact>                        # WRONG: no args — use fn(arg="val")
"""


def get_lndl_system_prompt() -> str:
    return LNDL_SYSTEM_PROMPT.strip()
