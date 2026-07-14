---
hide:
  - navigation
  - toc
---

<!-- markdownlint-disable MD046 -->

<article class="lion-home" aria-label="LionAGI documentation home">
  <section class="lion-hero" aria-labelledby="lion-hero-title">
    <div class="lion-hero__copy">
      <div class="lion-kicker">
        <img src="assets/lionagi-mark.svg" alt="" width="28" height="28">
        <span>lionagi 0.28</span>
        <span class="lion-kicker__status">Governed orchestration</span>
      </div>

      <h1 id="lion-hero-title">
        Agents that work.<br>
        <span>Orchestration you control.</span>
      </h1>

      <p class="lion-hero__lede">
        Run the coding agents and models you already use, compose them into parallel
        fan-outs and dependency-aware flows, and keep every run inspectable, resumable,
        and under your control.
      </p>

      <div class="lion-actions" aria-label="Start with lionagi">
        <a class="lion-button lion-button--primary" href="getting-started/install/">
          Start building <span aria-hidden="true">→</span>
        </a>
        <a class="lion-button lion-button--secondary" href="api/">
          Explore the Python SDK
        </a>
      </div>

      <ul class="lion-proof" aria-label="LionAGI highlights">
        <li>Python 3.10+</li>
        <li>CLI and SDK</li>
        <li>Apache-2.0</li>
      </ul>
    </div>

    <div class="lion-terminal" aria-label="LionAGI command line examples">
      <div class="lion-terminal__bar">
        <span class="lion-terminal__dots" aria-hidden="true"><i></i><i></i><i></i></span>
        <span>terminal</span>
        <span class="lion-terminal__ready">ready</span>
      </div>
      <pre><code><span class="lion-terminal__comment"># install once</span>
<span class="lion-terminal__prompt">$</span> pip install lionagi

<span class="lion-terminal__comment"># one durable agent turn</span>
<span class="lion-terminal__prompt">$</span> li agent claude/sonnet \
    <span class="lion-terminal__string">"Map the risks in this change"</span>

<span class="lion-terminal__comment"># a planned DAG of specialists</span>
<span class="lion-terminal__prompt">$</span> li o flow codex/gpt-5.5 \
    <span class="lion-terminal__string">"Audit auth and propose fixes"</span> --cwd .</code></pre>
      <div class="lion-terminal__lanes" aria-hidden="true">
        <span>plan</span><i></i><span>research</span><i></i><span>implement</span><i></i><span>review</span>
      </div>
      <div class="lion-terminal__foot">
        <span><b></b> state persisted locally</span>
        <span>~/.lionagi/runs/</span>
      </div>
    </div>
  </section>

  <section class="lion-section" aria-labelledby="choose-surface">
    <div class="lion-section__heading">
      <p class="lion-overline">Start where you work</p>
      <h2 id="choose-surface">One system, three useful surfaces.</h2>
      <p>Use the terminal for direct work, Python for application control, and Studio for live operations.</p>
    </div>

    <div class="lion-surface-grid">
      <article class="lion-surface-card">
        <div class="lion-card__topline">
          <span class="lion-card__index">01</span>
          <code>li</code>
        </div>
        <h3>Command line</h3>
        <p>Start one agent, fan out independent workers, or ask an orchestrator to plan a DAG.</p>
        <a href="getting-started/first-flow/">Run your first flow <span aria-hidden="true">→</span></a>
      </article>

      <article class="lion-surface-card">
        <div class="lion-card__topline">
          <span class="lion-card__index">02</span>
          <code>Branch</code>
        </div>
        <h3>Python SDK</h3>
        <p>Build stateful model interactions with typed output, tools, providers, and explicit graph execution.</p>
        <a href="api/branch/">Meet the core API <span aria-hidden="true">→</span></a>
      </article>

      <article class="lion-surface-card lion-surface-card--accent">
        <div class="lion-card__topline">
          <span class="lion-card__index">03</span>
          <code>local :8765</code>
        </div>
        <h3>Lion Studio</h3>
        <p>See active agents, schedules, execution graphs, artifacts, and run history in one local-first cockpit.</p>
        <a href="https://lion-studio.khive.ai">Open Studio <span aria-hidden="true">↗</span></a>
      </article>
    </div>
  </section>

  <section class="lion-section lion-section--split" aria-labelledby="right-structure">
    <div class="lion-section__heading lion-section__heading--sticky">
      <p class="lion-overline">Scale with intent</p>
      <h2 id="right-structure">Use exactly as much structure as the task needs.</h2>
      <p>Each lane adds a capability without replacing the one before it.</p>
      <a class="lion-text-link" href="choosing-a-surface/">Choose the right surface <span aria-hidden="true">→</span></a>
    </div>

    <ol class="lion-ladder">
      <li>
        <span class="lion-ladder__number">01</span>
        <div>
          <div class="lion-ladder__title"><code>li agent</code><span>One focused branch</span></div>
          <p>Ask, act, inspect, and resume. The smallest useful unit of agent work.</p>
        </div>
      </li>
      <li>
        <span class="lion-ladder__number">02</span>
        <div>
          <div class="lion-ladder__title"><code>li o fanout</code><span>Independent workers</span></div>
          <p>Split one task across parallel workers, then optionally synthesize their results.</p>
        </div>
      </li>
      <li>
        <span class="lion-ladder__number">03</span>
        <div>
          <div class="lion-ladder__title"><code>li o flow</code><span>Dependency-aware DAG</span></div>
          <p>Let an orchestrator plan specialist work while the engine resolves dependencies.</p>
        </div>
      </li>
      <li>
        <span class="lion-ladder__number">04</span>
        <div>
          <div class="lion-ladder__title"><code>li schedule</code><span>Durable operations</span></div>
          <p>Promote repeatable work into playbooks, schedules, monitored runs, and Studio workflows.</p>
        </div>
      </li>
    </ol>
  </section>

  <section class="lion-section lion-sdk" aria-labelledby="plain-python">
    <div class="lion-sdk__copy">
      <p class="lion-overline">A Python API that stays Python</p>
      <h2 id="plain-python">Typed results without hiding the loop.</h2>
      <p>
        A <code>Branch</code> owns conversation state, tools, and model configuration.
        <code>operate()</code> adds tool use and structured output; <code>Session</code>
        coordinates branches when the work becomes a graph.
      </p>
      <div class="lion-inline-links">
        <a href="api/operations/">Structured operations <span aria-hidden="true">→</span></a>
        <a href="api/session/">Session and flows <span aria-hidden="true">→</span></a>
      </div>
    </div>

    <div class="lion-code-card" aria-label="Python structured output example">
      <div class="lion-code-card__label"><span>risk_assessment.py</span><span>Python</span></div>
      <pre><code><span class="lion-code__kw">from</span> pydantic <span class="lion-code__kw">import</span> BaseModel
<span class="lion-code__kw">from</span> lionagi <span class="lion-code__kw">import</span> Branch

<span class="lion-code__kw">class</span> <span class="lion-code__type">Assessment</span>(BaseModel):
    risk: str
    reasons: list[str]

branch = Branch(
    chat_model=<span class="lion-code__string">"codex/gpt-5.5"</span>,
    system=<span class="lion-code__string">"You are a careful reviewer."</span>,
)

result = <span class="lion-code__kw">await</span> branch.operate(
    instruction=<span class="lion-code__string">"Assess this change."</span>,
    response_format=Assessment,
)</code></pre>
    </div>
  </section>

  <section class="lion-section" aria-labelledby="control-feature">
    <div class="lion-section__heading">
      <p class="lion-overline">Trust comes from visibility</p>
      <h2 id="control-feature">Control is a feature, not an afterthought.</h2>
    </div>

    <div class="lion-feature-grid">
      <article>
        <span class="lion-feature__mark" aria-hidden="true"></span>
        <h3>Typed, inspectable state</h3>
        <p>Branches, messages, operations, and graphs are explicit objects—not state hidden inside a chain.</p>
        <a href="concepts/#branch">Understand Branch <span aria-hidden="true">→</span></a>
      </article>
      <article>
        <span class="lion-feature__mark" aria-hidden="true"></span>
        <h3>Runs that survive the terminal</h3>
        <p>Run records, branch snapshots, artifacts, monitoring, and resume are built into the CLI path.</p>
        <a href="cookbook/resumable-background/">Learn durable runs <span aria-hidden="true">→</span></a>
      </article>
      <article>
        <span class="lion-feature__mark" aria-hidden="true"></span>
        <h3>Governed tool execution</h3>
        <p>Permission policies, guard hooks, and isolated git worktrees put boundaries around agent actions.</p>
        <a href="api/agent-config/">Configure an agent <span aria-hidden="true">→</span></a>
      </article>
      <article>
        <span class="lion-feature__mark" aria-hidden="true"></span>
        <h3>Providers without lock-in</h3>
        <p>API models and coding-agent CLIs share one model-service boundary and compose in the same flow.</p>
        <a href="reference/providers/">Browse providers <span aria-hidden="true">→</span></a>
      </article>
    </div>
  </section>

  <section class="lion-section" aria-labelledby="use-real-work">
    <div class="lion-section__heading lion-section__heading--row">
      <div>
        <p class="lion-overline">Copy, run, adapt</p>
        <h2 id="use-real-work">Start with real work.</h2>
      </div>
      <a class="lion-text-link" href="cookbook/">View the cookbook <span aria-hidden="true">→</span></a>
    </div>

    <div class="lion-recipe-grid">
      <a href="cookbook/codebase-audit/">
        <span>Engineering</span>
        <h3>Audit a codebase in parallel</h3>
        <p>Fan out focused reviewers and keep their findings independent.</p>
        <b aria-hidden="true">↗</b>
      </a>
      <a href="cookbook/research-synthesis/">
        <span>Research</span>
        <h3>Synthesize across workers</h3>
        <p>Gather multiple perspectives, then consolidate them into one result.</p>
        <b aria-hidden="true">↗</b>
      </a>
      <a href="cookbook/resumable-background/">
        <span>Operations</span>
        <h3>Run long work in the background</h3>
        <p>Detach a flow, monitor its durable state, and resume any branch later.</p>
        <b aria-hidden="true">↗</b>
      </a>
    </div>
  </section>

  <section class="lion-final" aria-labelledby="build-first-run">
    <img src="assets/lionagi-mark.svg" alt="" width="48" height="48">
    <p class="lion-overline">Your next run can be durable</p>
    <h2 id="build-first-run">Start with one agent. Add orchestration when it earns its place.</h2>
    <div class="lion-actions" aria-label="LionAGI next steps">
      <a class="lion-button lion-button--primary" href="getting-started/install/">Install lionagi <span aria-hidden="true">→</span></a>
      <a class="lion-button lion-button--secondary" href="comparison/">See how it compares</a>
    </div>
  </section>
</article>

<!-- markdownlint-enable MD046 -->
