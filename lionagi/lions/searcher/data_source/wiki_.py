class WikipediaSearch:
    """
    A utility class for searching and retrieving information from Wikipedia.

    Methods:
        query(query: str, lang: str = 'en'):
            Search for a query on Wikipedia and retrieve relevant information.
    """

    # @staticmethod
    def create_agent(verbose=False):
        from lionagi.libs import SysUtil
        SysUtil.check_import(
            package_name="llama_index",
            pip_name="llama-index"
        )
        
        SysUtil.check_import(
            package_name="llama_index",
            module_name="agent.openai",
            import_name="OpenAIAgent",
            pip_name="llama-index-agent-openai"
        )
        
        SysUtil.check_import(
            package_name="llama_index",
            module_name="tools.wikipedia",
            import_name="WikipediaToolSpec",
            pip_name="llama-index-tools-wikipedia"
        )
            
        from llama_index.tools.wikipedia import WikipediaToolSpec
        from llama_index.agent.openai import OpenAIAgent
        from llama_index.llms.openai import OpenAI
        llm = OpenAI(model="gpt-4-turbo", temperature=0.1)

        tool_spec = WikipediaToolSpec()

        agent = OpenAIAgent.from_tools(tool_spec.to_tool_list(), verbose=verbose, llm=llm)
        return agent


# responses_wiki = []
# async def query_wiki(query: str):
#     """
#     Search Wikipedia and retrieve a natural language answer to a given query.

#     Args:
#         query (str): The search query to find an answer for.

#     Returns:
#         str: A natural language answer obtained from Google search results.

#     Raises:
#         Exception: If there is an issue with making the request or parsing the response.
#     """
#     wiki_agent = WikipediaSearch.create_agent()
#     response = await wiki_agent.achat(query)
#     responses_wiki.append(response)
#     return str(response.response)



    # @staticmethod
    # def query(query: str, lang: str = 'en'):
    #     """
    #     Search for a query on Wikipedia and retrieve relevant information.

    #     Args:
    #         query (str): The query to search for on Wikipedia.
    #         lang (str, optional): The language for the Wikipedia search. Default is 'en' (English).

    #     Returns:
    #         str: The retrieved information from Wikipedia or an error message if no results are found.

    #     Raises:
    #         WikipediaException: If there is an issue during the Wikipedia search or content retrieval.
    #     """
    #     import lionagi.lions.searcher.wikipedia as wikipedia
    #     from llama_index import Document, VectorStoreIndex

    #     wikipedia.set_lang(lang)

    #     res = wikipedia.search(query, results=1)
    #     if len(res) == 0:
    #         return "No search results."
    #     try:
    #         wikipedia_page = wikipedia.page(res[0], auto_suggest=False)
    #     except wikipedia.PageError:
    #         return f"Unable to load page {res[0]}."
    #     content = wikipedia_page.content

    #     documents = [Document(text=content)]
    #     index = VectorStoreIndex.from_documents(documents)
    #     query_engine = index.as_query_engine()
    #     response = query_engine.query(query)
    #     return response.response