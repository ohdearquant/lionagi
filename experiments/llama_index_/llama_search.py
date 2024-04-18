import os
from llama_index.agent import OpenAIAgent


class Search:

    def __init__(self) -> None:
        self.responses = {}

    def google(self, query, verbose=False):
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
        google_key_scheme = "GOOGLE_API_KEY"
        google_engine_scheme = "GOOGLE_CSE_ID"

        google_api_key = os.getenv(google_key_scheme)
        google_engine = os.getenv(google_engine_scheme)
        try:
            from llama_index.tools.tool_spec.load_and_search.base import (
                LoadAndSearchToolSpec,
            )
            from llama_hub.tools.google_search.base import GoogleSearchToolSpec

            google_spec = GoogleSearchToolSpec(key=google_api_key, engine=google_engine)

            # Wrap the google search tool as it returns large payloads
            tools = LoadAndSearchToolSpec.from_defaults(
                google_spec.to_tool_list()[0],
            ).to_tool_list()

            # Create the Agent with our tools
            agent = OpenAIAgent.from_tools(tools, verbose=verbose)
            return self._output(query, agent, "google")

        except Exception as e:
            raise ImportError(f"Error in importing OpenAIAgent from llama_index: {e}")

    def wikipedia(self, query, verbose=False):
        from llama_hub.tools.wikipedia import WikipediaToolSpec

        tool_spec = WikipediaToolSpec()
        agent = OpenAIAgent.from_tools(tool_spec.to_tool_list(), verbose=verbose)

        return self._output(query, agent, "wikipedia")

    def _output(self, query, agent, _type):
        response = agent.chat(query)
        if _type in self.responses:
            self.responses[_type] = [response]
        else:
            self.responses[_type].append(response)
        return str(response.response)
