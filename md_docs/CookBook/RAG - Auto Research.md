
# Auto Explorative Research with LionAGI & LlamaIndex

This guide introduces a workflow using LionAGI and LlamaIndex for automated explorative research, particularly focusing on indexing and querying a dataset of academic papers. Here’s how you can set up, query, and automate research tasks with these tools.

## Getting Started

First, ensure you have the necessary Python packages:

```python
%pip install lionagi llama_index pypdf
```

## Building a Vector Index with LlamaIndex

### Creating the Index:

To index your documents for efficient querying, follow these steps:

```python
def get_index(chunks):
    from llama_index import ServiceContext, VectorStoreIndex
    from llama_index.llms import OpenAI

    llm = OpenAI(temperature=0.1, model="gpt-4-1106-preview")
    service_context = ServiceContext.from_defaults(llm=llm)
    return VectorStoreIndex(chunks, include_embeddings=True, service_context=service_context)
```

### Loading Documents and Creating Chunks:

Load your documents and split them into manageable chunks:

```python
text_nodes = li.load(
    'SimpleDirectoryReader', reader_type='llama_index', reader_args=['papers/'],
    to_lion=False,
)

chunks = li.chunk(
    documents=text_nodes, chunker_type='llama_index', chunker='SentenceSplitter',
    chunker_kwargs={'chunk_size': 512, 'chunk_overlap':20}, to_lion=False,
)
```

## Setting Up the Query Engine

With your index ready, configure the query engine like so:

```python
index = get_index(chunks)
query_engine = index.as_query_engine(include_text=False, response_mode="tree_summarize")
```

## Crafting Your Research Tool

Define your research tool following a schema:

```python
import lionagi as li

tool_schema = {
    "type": "function",
    "function": {
        "name": "query_arxiv_papers",
        "description": "Query academic papers efficiently.",
        "parameters": {
            "str_or_query_bundle": {
                "type": "string",
                "description": "Enter your query here.",
            }
        },
        "required": ["str_or_query_bundle"],
    }
}

tool = li.Tool(func=query_engine.query, parser=lambda x: x.response, schema_=tool_schema)
```

## Automating Research Tasks

### Creating a Research Session:

Set up your session with detailed instructions and a system persona:

```python
system = {
    "persona": "A helpful world-class researcher",
    "requirements": "Provide clear, precise answers with a humble tone.",
    "responsibilities": "Assist in researching topics via queries.",
    "tools": "A QA bot for grounding responses."
}

# Task instructions
instruct1 = {
    "task step": "1",
    "task name": "Read paper abstracts",
    "task objective": "Gain initial insights from abstracts.",
    "task description": "Summarize the core points of papers."
}

instruct2 = {
    "task step": "2",
    "task name": "Brainstorm research questions",
    "task objective": "Initial brainstorming for research questions.",
    "task description": "Propose questions and talking points."
}
```

### Running the Workflow:

The research workflow simplifies iterating through abstracts to generate new research ideas:

```python
async def read_propose(context, num=5):
    researcher = li.Session(system, dir=dir)
    researcher.register_tools(tool)

    await researcher.chat(instruct1, context=context, temperature=0.7)
    await researcher.auto_followup(instruct2, tools=True, num=num)

    return researcher
```

### Executing and Reviewing Results:

Use asynchronous programming to execute your workflow and review the results for insights:

```python
researcher = li.to_list(
    await li.alcall(abstracts, read_propose), flatten=True
)[0]

researcher.messages
```

## Conclusion

This guide demonstrates setting up an explorative research workflow using LionAGI and LlamaIndex. By following these steps, you can automate the process of querying and analyzing a large corpus of academic papers, facilitating the generation of new research insights and questions.
