from .entity_extraction import KGTripletExtractor


class KGraph:
    """
    A class representing a Knowledge Graph (KGraph) for extracting relations from text.

    Methods:
        text_to_wiki_kb(text, model=None, tokenizer=None, device='cpu', span_length=512, article_title=None,
                        article_publish_date=None, verbose=False):
            Extract relations from input text and create a Knowledge Base (KB) containing entities and relations.
    """

    @staticmethod
    def text_to_wiki_kb(text, **kwargs):
        """
        Extract relations from input text and create a Knowledge Base (KB) containing entities and relations.

        Args:
            text (str): The input text from which relations are extracted.
            **kwargs: Additional keyword arguments passed to the underlying extraction method.

        Returns:
            KnowledgeBase: A Knowledge Base (KB) containing entities and relations extracted from the input text.
        """
        return KGTripletExtractor.text_to_wiki_kb(text, **kwargs)
