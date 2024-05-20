Auto Explorative Research: LionAGI + RAG with LlamaIndex
========================================================

In this example, we will explore auto explorative research with LionAGI and `LlamaIndex <https://www.llamaindex.ai/>`_
`VectorIndex <https://docs.llamaindex.ai/en/stable/understanding/indexing/indexing.html>`_
`Query Engine <https://docs.llamaindex.ai/en/stable/understanding/querying/querying.html>`_. The process involves
loading papers, embedding them, and utilizing our ``Session`` to cross-reference the index for
clarification when needed.

.. code-block:: python

   %pip install lionagi llama_index pypdf

First, let's build a Vector Index with `LlamaIndex <https://www.llamaindex.ai/>`_

.. code-block:: python

   # define a function to get index

   def get_index(chunks):
       from llama_index import ServiceContext, VectorStoreIndex
       from llama_index.llms import OpenAI

       llm = OpenAI(temperature=0.1, model="gpt-4-1106-preview")
       service_context = ServiceContext.from_defaults(llm=llm)
       return VectorStoreIndex(chunks, include_embeddings=True, service_context=service_context)

.. code-block:: python

   # get llamaindex textnodes, if to_lion is True, you will get Lion DataNode
   text_nodes = li.load(
       'SimpleDirectoryReader', reader_type='llama_index', reader_args=['papers/'],
       to_lion=False, #reader_kwargs = {...}
   )

   chunks = li.chunk(
       documents=text_nodes, chunker_type = 'llama_index', chunker='SentenceSplitter',
       chunker_kwargs={'chunk_size': 512, 'chunk_overlap':20}, to_lion=False,
   )

.. code-block:: python

   index = get_index(chunks)
   # set up query engine
   query_engine = index1.as_query_engine(include_text=False, response_mode="tree_summarize")

The query engine is set up and ready, we can proceed to write the tool description following the OpenAI schema.

.. code-block:: python

   import lionagi as li

   tool_schema = {
            "type": "function",
            "function": {
                "name": "query_arxiv_papers",
                "description": """
                               Perform a query to a QA bot with access to an
                               index built with papers from arxiv
                               """,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "str_or_query_bundle": {
                            "type": "string",
                            "description": "a question to ask the QA bot",
                        }
                    },
                    "required": ["str_or_query_bundle"],
                },
            }
        }

   # we will need to register both the function description
   # and actual implementation
   tool = li.Tool(func=query_engine.query, parser=lambda x: x.response, schema_=tool_schema)

Central to LionAGI’s power is its facilitation of sophisticated workflow manipulation. Let’s explore how to craft
prompts and set up a session for an automated research assistant.

.. code-block:: python

   # a rigidly set up prompt can help make outcome more deterministic
   # though any string will work as well.
   system = {
        "persona": "a helpful world-class researcher",
        "requirements": """
                think step by step before returning a clear, precise
                worded answer with a humble yet confident tone
                """,
        "responsibilities": f"""
                you are asked to help with researching on the topic
                of {query}
                """,
        "tools": "provided with a QA bot for grounding responses"
   }

   # similarly, we can pass in any string or dictionary to instruction
   # here we are modifying model behavior by telling mdel how to output
   deliver_format1 = {"return required": "yes", "return format": "paragraph"}

   deliver_format2 = {"return required": "yes",
        "return format": {
            "json_mode": {
                'paper': "paper_name",
                "summary": "...",
                "research question": "...",
                "talking points": {
                    "point 1": "...",
                    "point 2": "...",
                    "point 3": "..."
                }}}}

   function_call = {
     "notice":f"""
        At each task step, identified by step number, you must use the tool
        at least twice. Notice you are provided with a QA bot as your tool,
        the bot has access to the {num_papers} papers via a queriable index
        that takes natural language query and return a natural language
        answer. You can decide whether to invoke the function call, you will
        need to ask the bot when there are things need clarification or
        further information. you provide the query by asking a question,
        please use the tool as extensively as you can.
       """
   }

   # here we create a two step process imitating the steps human would take to
   # perform the research task
   instruct1 = {
        "task step": "1",
        "task name": "read paper abstracts",
        "task objective": "get initial understanding of the papers of interest",
        "task description": """
                provided with abstracts of paper, provide a brief summary
                highlighting the paper core points, the purpose is to extract
                as much information as possible
                """,
        "deliverable": deliver_format1
   }


   instruct2 = {
        "task step": "2",
        "task name": "propose research questions and talking points",
        "task objective": "initial brainstorming",
        "task description": """
            from the improved understanding of the paper, please propose
            an interesting, unique and practical research question,
            support your reasoning. Kept on asking questions if things are
            not clear.
            """,
        "deliverable": deliver_format2,
        "function calling": function_call
   }

.. code-block:: python

   abstracts = """
   Abstract—Large language models (LLMs), such as ChatGPT and GPT4, are making
   new waves in the field of natural language processing and artificial intelligence,
   due to their emergent ability and generalizability. However, LLMs are black-box
   models, which often fall short of capturing and accessing factual knowledge. In
   contrast, Knowledge Graphs (KGs), Wikipedia and Huapu for example, are structured
   knowledge models that explicitly store rich factual knowledge. KGs can enhance
   LLMs by providing external knowledge for inference and interpretability. Meanwhile,
   KGs are difficult to construct and evolving by nature, which challenges the existing
   methods in KGs to generate new facts and represent unseen knowledge. Therefore, it
   is complementary to unify LLMs and KGs together and simultaneously leverage their
   advantages. In this article, we present a forward-looking roadmap for the unification
   of LLMs and KGs. Our roadmap consists of three general frameworks, namely, 1)
   KG-enhanced LLMs, which incorporate KGs during the pre-training and inference phases
   of LLMs, or for the purpose of enhancing understanding of the knowledge learned by
   LLMs; 2) LLM-augmented KGs, that leverage LLMs for different KG tasks such as embedding,
   completion, construction, graph-to-text generation, and question answering; and 3)
   Synergized LLMs + KGs, in which LLMs and KGs play equal roles and work in a mutually
   beneficial way to enhance both LLMs and KGs for bidirectional reasoning driven by both
   data and knowledge. We review and summarize existing efforts within these three frameworks
   in our roadmap and pinpoint their future research directions.
   """

Next, we define and run the workflow that will manage our research session:

.. code-block:: python

   # Research Assistant Workflow

   # read an abstract, then check against a vector store of papers, and suggest
   # new research topics
   async def read_propose(context, num=5):

        # Instantiate a Session with the system message and directory to save
        # the outputs
        researcher = li.Session(system, dir=dir)

        # Register tools needed for the Session
        # tools are the OpenAI schema,
        researcher.register_tools(tool)

        # Initiate the research process by sending the first set of instructions
        await researcher.chat(instruction=instruct1,
                context=context, temperature=0.7)

        # Use auto_followup to conduct a sequence of interactions
        # tool parser is needed for automatically using tools many times.
        # the accepted final formats are string and dict
        await researcher.auto_followup(instruction=instruct2,
                tools=True, num=num)

        # Return the latest message from the conversation
        return researcher

With asynchronous programming, executing this workflow becomes a breeze:

.. code-block:: python

   researcher = li.to_list(
       await li.alcall(abstracts, read_propose), flatten=True
   )[0]

To review the entire result, check:

.. code-block:: python

   researcher.messages

.. [Ref] Pan, Shirui and Luo, Linhao and Wang, Yufei and Chen, Chen and Wang, Jiapu and Wu, Xindong
   "Unifying Large Language Models and Knowledge Graphs: A Roadmap"
   `arXiv:2306.08302 <https://arxiv.org/abs/2306.08302>`_
