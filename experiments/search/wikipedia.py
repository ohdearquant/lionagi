class WikipediaSearch:
    """
    A utility class for searching and retrieving information from Wikipedia.

    Methods:
        query(query: str, lang: str = 'en'):
            Search for a query on Wikipedia and retrieve relevant information.
    """

    # @staticmethod
    # def create_agent_engine(verbose=False):
    #     from llama_hub.tools.wikipedia import WikipediaToolSpec
    #     from llama_index.agent import OpenAIAgent
    #
    #     tool_spec = WikipediaToolSpec()
    #
    #     agent = OpenAIAgent.from_tools(tool_spec.to_tool_list(), verbose=verbose)
    #     return agent

    @staticmethod
    def query(query: str, lang: str = "en"):
        """
        Search for a query on Wikipedia and retrieve relevant information.

        Args:
            query (str): The query to search for on Wikipedia.
            lang (str, optional): The language for the Wikipedia search. Default is 'en' (English).

        Returns:
            str: The retrieved information from Wikipedia or an error message if no results are found.

        Raises:
            WikipediaException: If there is an issue during the Wikipedia search or content retrieval.
        """
        import wikipedia
        from llama_index import Document, VectorStoreIndex

        wikipedia.set_lang(lang)

        res = wikipedia.search(query, results=1)
        if len(res) == 0:
            return "No search results."
        try:
            wikipedia_page = wikipedia.page(res[0], auto_suggest=False)
        except wikipedia.PageError:
            return f"Unable to load page {res[0]}."
        content = wikipedia_page.content

        documents = [Document(text=content)]
        index = VectorStoreIndex.from_documents(documents)
        query_engine = index.as_query_engine()
        response = query_engine.query(query)
        return response.response
