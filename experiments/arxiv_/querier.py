import os
import arxiv
from deepocean.utils.io_util import IOUtil
from bibtexparser import loads


class ArxivSearch:

    criteria_map = {
        "relevance": arxiv.SortCriterion.Relevance,
        "submit": arxiv.SortCriterion.SubmittedDate,
        "update": arxiv.SortCriterion.LastUpdatedDate,
    }

    order_map = {"asc": arxiv.SortOrder.Ascending, "desc": arxiv.SortOrder.Descending}

    def __init__(self, persist_dir=None) -> None:
        self.persist_dir = persist_dir
        self.client = arxiv.Client()
        if not persist_dir:
            self.persist_dir = os.path.join(os.getcwd(), "arxiv_downloads/")

    def search(
        self,
        query=None,
        id_list=None,
        max_results=10,
        sort_by="relevance",
        sort_order="desc",
    ):
        if (query is None and id_list is None) or (query and id_list):
            raise ValueError("Only one of query or id_list must be provided.")

        if sort_by not in self.criteria_map.keys():
            raise ValueError(f"Invalid sort criterion: {sort_by}")

        search_config = {
            "sort_by": self.criteria_map[sort_by],
            "max_results": max_results,
            "sort_order": self.order_map[sort_order],
        }

        if query:
            search_config.update({"query": query})
        else:
            search_config.update({"id_list": id_list})

        return self.client.results(arxiv.Search(**search_config))

    def download(self, result, dirpath=None, filename=None, kind="source"):
        if kind not in ["source", "pdf"]:
            raise ValueError(f"Invalid kind: {kind}")
        if not dirpath:
            dirpath = self.persist_dir
        if kind == "source":
            return result.download_source(dirpath=dirpath, filename=filename)
        else:
            return result.download_pdf(dirpath=dirpath, filename=filename)

    @staticmethod
    def fetch_arxiv_results(
        persist_dir=None,
        query=None,
        id_list=None,
        max_results=10,
        sort_by="relevance",
        sort_order="desc",
    ):
        results = ArxivSearch(persist_dir).search(
            query=query,
            id_list=id_list,
            max_results=max_results,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return results
