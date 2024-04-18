class KnowledgeBase:
    """
    A class to represent a Knowledge Base (KB) containing entities, relations, and sources.

    Attributes:
        entities (dict): A dictionary of entities in the KB, where the keys are entity titles, and the values are
                         entity information (excluding the title).
        relations (list): A list of relations in the KB, where each relation is a dictionary containing information
                          about the relation (head, type, tail) and metadata (article_url and spans).
        sources (dict): A dictionary of information about the sources of relations, where the keys are article URLs,
                       and the values are source data (article_title and article_publish_date).

    Methods:
        merge_with_kb(kb2): Merge another Knowledge Base (kb2) into this KB.
        are_relations_equal(r1, r2): Check if two relations (r1 and r2) are equal.
        exists_relation(r1): Check if a relation (r1) already exists in the KB.
        merge_relations(r2): Merge the information from relation r2 into an existing relation in the KB.
        get_wikipedia_data(candidate_entity): Get data for a candidate entity from Wikipedia.
        add_entity(e): Add an entity to the KB.
        add_relation(r, article_title, article_publish_date): Add a relation to the KB.
        print(): Print the entities, relations, and sources in the KB.
        extract_relations_from_model_output(text): Extract relations from the model output text.

    """

    def __init__(self):
        """
        Initialize an empty Knowledge Base (KB) with empty dictionaries for entities, relations, and sources.
        """
        self.entities = {}  # { entity_title: {...} }
        self.relations = []  # [ head: entity_title, type: ..., tail: entity_title,
        # meta: { article_url: { spans: [...] } } ]
        self.sources = {}  # { article_url: {...} }

    def merge_with_kb(self, kb2):
        """
        Merge another Knowledge Base (KB) into this KB.

        Args:
            kb2 (KnowledgeBase): The Knowledge Base (KB) to merge into this KB.
        """
        for r in kb2.relations:
            article_url = list(r["meta"].keys())[0]
            source_data = kb2.sources[article_url]
            self.add_relation(
                r, source_data["article_title"], source_data["article_publish_date"]
            )

    def are_relations_equal(self, r1, r2):
        """
        Check if two relations (r1 and r2) are equal.

        Args:
            r1 (dict): The first relation to compare.
            r2 (dict): The second relation to compare.

        Returns:
            bool: True if the relations are equal, False otherwise.
        """
        return all(r1[attr] == r2[attr] for attr in ["head", "type", "tail"])

    def exists_relation(self, r1):
        """
        Check if a relation (r1) already exists in the KB.

        Args:
            r1 (dict): The relation to check for existence in the KB.

        Returns:
            bool: True if the relation exists in the KB, False otherwise.
        """
        return any(self.are_relations_equal(r1, r2) for r2 in self.relations)

    def merge_relations(self, r2):
        """
        Merge the information from relation r2 into an existing relation in the KB.

        Args:
            r2 (dict): The relation to merge into an existing relation in the KB.
        """
        r1 = [r for r in self.relations if self.are_relations_equal(r2, r)][0]

        # if different article
        article_url = list(r2["meta"].keys())[0]
        if article_url not in r1["meta"]:
            r1["meta"][article_url] = r2["meta"][article_url]

        # if existing article
        else:
            spans_to_add = [
                span
                for span in r2["meta"][article_url]["spans"]
                if span not in r1["meta"][article_url]["spans"]
            ]
            r1["meta"][article_url]["spans"] += spans_to_add

    def get_wikipedia_data(self, candidate_entity):
        """
        Get data for a candidate entity from Wikipedia.

        Args:
            candidate_entity (str): The candidate entity title.

        Returns:
            dict: A dictionary containing information about the candidate entity (title, url, summary).
                  None if the entity does not exist in Wikipedia.
        """

        try:
            page = wikipedia.page(candidate_entity, auto_suggest=False)
            entity_data = {
                "title": page.title,
                "url": page.url,
                "summary": page.summary,
            }
            return entity_data
        except:
            return None

    def add_entity(self, e):
        """
        Add an entity to the KB.

        Args:
            e (dict): A dictionary containing information about the entity (title and additional attributes).
        """
        self.entities[e["title"]] = {k: v for k, v in e.items() if k != "title"}

    def add_relation(self, r, article_title, article_publish_date):
        """
        Add a relation to the KB.

        Args:
            r (dict): A dictionary containing information about the relation (head, type, tail, and metadata).
            article_title (str): The title of the article containing the relation.
            article_publish_date (str): The publish date of the article.
        """
        # check on wikipedia
        candidate_entities = [r["head"], r["tail"]]
        entities = [self.get_wikipedia_data(ent) for ent in candidate_entities]

        # if one entity does not exist, stop
        if any(ent is None for ent in entities):
            return

        # manage new entities
        for e in entities:
            self.add_entity(e)

        # rename relation entities with their wikipedia titles
        r["head"] = entities[0]["title"]
        r["tail"] = entities[1]["title"]

        # add source if not in kb
        article_url = list(r["meta"].keys())[0]
        if article_url not in self.sources:
            self.sources[article_url] = {
                "article_title": article_title,
                "article_publish_date": article_publish_date,
            }

        # manage new relation
        if not self.exists_relation(r):
            self.relations.append(r)
        else:
            self.merge_relations(r)

    def print(self):
        """
        Print the entities, relations, and sources in the KB.

        Returns:
            None
        """
        print("Entities:")
        for e in self.entities.items():
            print(f"  {e}")
        print("Relations:")
        for r in self.relations:
            print(f"  {r}")
        print("Sources:")
        for s in self.sources.items():
            print(f"  {s}")

    @staticmethod
    def extract_relations_from_model_output(text):
        """
        Extract relations from the model output text.

        Args:
            text (str): The model output text containing relations.

        Returns:
            list: A list of dictionaries, where each dictionary represents a relation (head, type, tail).
        """
        relations = []
        relation, subject, relation, object_ = "", "", "", ""
        text = text.strip()
        current = "x"
        text_replaced = text.replace("<s>", "").replace("<pad>", "").replace("</s>", "")
        for token in text_replaced.split():
            if token == "<triplet>":
                current = "t"
                if relation != "":
                    relations.append(
                        {
                            "head": subject.strip(),
                            "type": relation.strip(),
                            "tail": object_.strip(),
                        }
                    )
                    relation = ""
                subject = ""
            elif token == "<subj>":
                current = "s"
                if relation != "":
                    relations.append(
                        {
                            "head": subject.strip(),
                            "type": relation.strip(),
                            "tail": object_.strip(),
                        }
                    )
                object_ = ""
            elif token == "<obj>":
                current = "o"
                relation = ""
            else:
                if current == "t":
                    subject += " " + token
                elif current == "s":
                    object_ += " " + token
                elif current == "o":
                    relation += " " + token
        if subject != "" and relation != "" and object_ != "":
            relations.append(
                {
                    "head": subject.strip(),
                    "type": relation.strip(),
                    "tail": object_.strip(),
                }
            )
        return relations
