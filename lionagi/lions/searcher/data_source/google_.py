import os

google_key_scheme = 'GOOGLE_API_KEY'
google_engine_scheme = 'GOOGLE_CSE_ID'

google_api_key = os.getenv(google_key_scheme)
google_engine = os.getenv(google_engine_scheme)

class GoogleSearch:
    """
    A utility class for performing Google searches and retrieving search results.

    Attributes:
        api_key (str): The Google API key used for search (can be set via environment variable).
        search_engine (str): The Google Custom Search Engine ID (can be set via environment variable).

    Methods:
        create_agent_engine(api_key=None, search_engine=None, verbose=False):
            Create an OpenAIAgent for Google search operations.

        url_get_content(url: str):
            Retrieve and parse the content from a given URL.

        search(title: str = None, url: str = None):
            Perform a Google search for a given title or URL and retrieve relevant content.
    """
    api_key = google_api_key
    search_engine = google_engine
    search_url = (
        """
        https://www.googleapis.com/customsearch/v1?key={key}&cx={engine}&q={query}&start={start}
        """
        )

    @classmethod
    def create_agent(cls, api_key=None, search_engine=None, verbose=False):
        """
        Create an OpenAIAgent for Google search operations.

        Args:
            api_key (str, optional): The Google API key for search. Defaults to None (uses class attribute).
            search_engine (str, optional): The Google Custom Search Engine ID. Defaults to None (uses class attribute).
            verbose (bool, optional): Whether to enable verbose mode for the agent. Defaults to False.

        Returns:
            OpenAIAgent: An instance of the OpenAIAgent for performing Google searches.

        Raises:
            ImportError: If there is an issue during the agent creation.
        """
        try:
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
                module_name="tools.google",
                import_name="GoogleSearchToolSpec",
                pip_name="llama-index-tools-google"
            )
            
            from llama_index.agent import OpenAIAgent
            from llama_index.tools.tool_spec.load_and_search.base import LoadAndSearchToolSpec
            from llama_index.tools.google import GoogleSearchToolSpec
            from llama_index.llms.openai import OpenAI
            llm = OpenAI(model="gpt-4-turbo", temperature=0.1)

            api_key = api_key if api_key else cls.api_key
            search_engine = search_engine if search_engine else cls.search_engine
            google_spec = GoogleSearchToolSpec(key=api_key, engine=search_engine)

            # Wrap the google search tool as it returns large payloads
            tools = LoadAndSearchToolSpec.from_defaults(
                google_spec.to_tool_list()[0],
            ).to_tool_list()

            # Create the Agent with our tools
            agent = OpenAIAgent.from_tools(tools, verbose=verbose, llm=llm)
            return agent
        
        except Exception as e:
            raise ImportError(f"Error in importing OpenAIAgent from llama_index: {e}")


# responses_google = []
# async def query_google(query: str):
#     """
#     Search Google and retrieve a natural language answer to a given query.

#     Args:
#         query (str): The search query to find an answer for.

#     Returns:
#         str: A natural language answer obtained from Google search results.

#     Raises:
#         Exception: If there is an issue with making the request or parsing the response.
#     """
#     google_agent = GoogleSearch.create_agent()
#     response = await google_agent.achat(query)
#     responses_google.append(response)
#     return str(response.response)





    # @staticmethod
    # def search(title: str = None, url: str = None):
    #     """
    #     Perform a Google search for a given title or URL and retrieve relevant content.

    #     Args:
    #         title (str, optional): The title or query for the Google search. Defaults to None.
    #         url (str, optional): The URL to retrieve content from. Defaults to None.

    #     Returns:
    #         str: The retrieved information from the Google search or URL, or an error message if no results are found.

    #     Raises:
    #         ValueError: If there is an issue during the search or content retrieval.
    #     """
    #     if not title and not url:
    #         raise 'No search input.'
    #     from ..utils.url_util import get_url_content

    #     if url:
    #         return get_url_content(url)
    #     else:
    #         from googlesearch import search
    #         search_result = search(title)

    #         for url in search_result:
    #             try:
    #                 return get_url_content(url)

    #             except:
    #                 continue
    #         return 'No matched or valid source'



    # get fields of a google search item
    # @classmethod
    # def _get_search_item_field(cls, item: Dict[str, Any]) -> Dict[str, str]:
    #     try:
    #         long_description = item["pagemap"]["metatags"][0]["og:description"]
    #     except KeyError:
    #         long_description = "N/A"
    #     url = item.get("link")
    #
    #     return {
    #         "title": item.get("title"),
    #         "snippet": item.get("snippet"),
    #         "url": item.get("link"),
    #         "long_description": long_description,
    #         "content": get_url_content(url)
    #     }
    #
    # @classmethod
    # def _format_search_url(cls, url, api_key, search_engine, query, start):
    #     url = url or cls.search_url
    #     url = url.format(
    #         key=api_key or cls.api_key,
    #         engine=search_engine or cls.search_engine,
    #         query=query,
    #         start=start
    #     )
    #     return url
    #
    # @classmethod
    # def search(
    #     cls,
    #     query: str =None,
    #     search_url = None,
    #     api_key = None,
    #     search_engine=None,
    #     start: int = 1,
    #     timeout: tuple = (0.5, 0.5),
    #     content=True,
    #     num=5
    #     ):
    #     url = cls._format_search_url(
    #         url = search_url, query=query, api_key=api_key,
    #         search_engine=search_engine, start=start
    #         )
    #     response = get_url_response(url, timeout=timeout)
    #     response_dict = response.json()
    #     items = response_dict.get('items')[:num]
    #     if content:
    #         items = lcall(items, cls._get_search_item_field, dropna=True)
    #     return items