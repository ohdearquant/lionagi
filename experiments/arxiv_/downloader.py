import os
import zipfile
import re
import arxiv
from bibtexparser import loads
from datetime import datetime


class PaperDownloader:
    def download_paper(self, paper_id, dirpath="downloads", filename=None):
        """Subclasses should implement this method."""
        raise NotImplementedError


class ArxivPaperDownloader(PaperDownloader):
    def download_paper(self, paper_id, dirpath="downloads", filename=None):
        """Downloads a paper from arXiv."""
        if not filename:
            filename = f"{paper_id}.zip"
        source_file_path = os.path.join(dirpath, filename)
        # Ensure dirpath exists
        if not os.path.exists(dirpath):
            os.makedirs(dirpath)
        try:
            paper = next(arxiv.Client().results(arxiv.Search(id_list=[paper_id])))
            paper.download_source(dirpath=dirpath, filename=filename)
        except Exception as e:
            print(f"Error downloading paper: {e}")
            return None
        return source_file_path
