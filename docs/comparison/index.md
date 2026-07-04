# How lionagi Compares

An architecture-level comparison of lionagi with LangChain / LangGraph, plus a
field matrix covering LlamaIndex and AG2. Both stacks answer the same eight
architectural questions (model abstraction, message state, tool calling,
control flow, structured output, multi-agent coordination, persistence,
human-in-the-loop) with different instincts about who owns the loop and where
the system ends.

Every lionagi claim is grounded in this repository's source; the LangChain side
comes from an AST-level digest of `langchain-ai/langchain` and `langgraph`.

<a href="langchain-map.html" target="_blank">Open the full map in its own tab</a>
(recommended on smaller screens).

<iframe src="langchain-map.html"
        title="LangChain × lionagi architecture map"
        style="width: 100%; height: 85vh; border: 1px solid var(--md-default-fg-color--lightest); border-radius: 6px;"
        loading="lazy"></iframe>
