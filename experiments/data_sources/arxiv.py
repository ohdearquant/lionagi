import os
import zipfile
import re
import arxiv
from bibtexparser import loads
from datetime import datetime


class ArxivPaper:
    def __init__(self, arxiv_id):
        """Initialize the ArxivPaper object with an arXiv ID.

        Args:
            arxiv_id (str): The arXiv ID of the paper.
        """
        self.arxiv_id = arxiv_id
        self.title = None
        self.authors = []
        self.submitted_date = None
        self.summary = None
        self.tags = []
        self.labels = set()
        self.bib_info = None
        self.source_file_path = None
        self.latex_content = None
        self.fetch_data_from_arxiv()

    def fetch_data_from_arxiv(self):
        """Fetch metadata from arXiv for the paper specified by the arXiv ID."""
        search = arxiv.Search(id_list=[self.arxiv_id])
        paper = next(search.results())
        self.title = paper.title
        self.authors = [author.name for author in paper.authors]
        self.submitted_date = paper.published
        self.summary = paper.summary
        self.tags = [tag.term for tag in paper.tags]

    def download_source(self, dirpath="downloads", filename=None):
        """Download the source files of the paper from arXiv.

        Args:
            dirpath (str): Directory path to download the source files to.
            filename (str, optional): Custom filename for the downloaded source.
        """
        if not filename:
            filename = f"{self.arxiv_id}.zip"
        self.source_file_path = os.path.join(dirpath, filename)
        paper = next(arxiv.Client().results(arxiv.Search(id_list=[self.arxiv_id])))
        paper.download_source(dirpath=dirpath, filename=filename)

    def analyze_source_structure(self):
        """Analyze the source structure of the downloaded paper."""
        if self.source_file_path:
            with zipfile.ZipFile(self.source_file_path, "r") as zip_ref:
                zip_ref.extractall(os.path.dirname(self.source_file_path))
            output_dir = os.path.dirname(self.source_file_path)
            files = os.listdir(output_dir)
            main_tex_path = self._find_main_tex_file(files, output_dir)
            if main_tex_path:
                self.latex_content = main_tex_path
                metadata = self._parse_latex_metadata(main_tex_path)
                self._update_metadata_from_latex(metadata)
            bib_file_path = self._find_bib_file(files, output_dir)
            if bib_file_path:
                self.bib_info = self._parse_bib_file(bib_file_path)

    def _update_metadata_from_latex(self, metadata):
        """Update the paper's metadata based on its LaTeX source.

        Args:
            metadata (dict): A dictionary containing metadata extracted from LaTeX.
        """
        self.title = metadata.get("title", self.title)
        self.authors = metadata.get("authors", self.authors)
        self.summary = metadata.get("abstract", self.summary)

    def add_label(self, label):
        """Add a label to the paper.

        Args:
            label (str): The label to be added.
        """
        self.labels.add(label)

    def __str__(self):
        """Return a string representation of the paper."""
        return f"{self.arxiv_id}: {self.title} by {', '.join(self.authors)}"

    @staticmethod
    def _find_main_tex_file(files, output_dir):
        """Find the main .tex file in a directory.

        Args:
            files (list): A list of filenames to search through.
            output_dir (str): The directory containing the files.

        Returns:
            str: The path to the main .tex file, or None if not found.
        """
        for file in files:
            if file.endswith(".tex"):
                file_path = os.path.join(output_dir, file)
                with open(file_path, "r") as tex_file:
                    if "\\documentclass{" in tex_file.read():
                        return file_path
        return None

    @staticmethod
    def _parse_latex_metadata(main_tex_path):
        """Parse metadata from a LaTeX file.

        Args:
            main_tex_path (str): The path to the main .tex file.

        Returns:
            dict: A dictionary containing extracted metadata.
        """
        metadata = {"title": None, "authors": [], "abstract": None}
        with open(main_tex_path, "r") as file:
            content = file.read()
            title_match = re.search(r"\\title\{(.+?)\}", content, re.DOTALL)
            if title_match:
                metadata["title"] = title_match.group(1).strip()
            authors_matches = re.finditer(r"\\author\{(.+?)\}", content, re.DOTALL)
            metadata["authors"] = [match.group(1).strip() for match in authors_matches]
            abstract_match = re.search(
                r"\\begin\{abstract\}(.+?)\\end\{abstract\}", content, re.DOTALL
            )
            if abstract_match:
                metadata["abstract"] = abstract_match.group(1).strip()
        return metadata

    @staticmethod
    def _parse_bib_file(bib_file_path):
        """Parse a .bib file to extract bibliography entries.

        Args:
            bib_file_path (str): The path to the .bib file.

        Returns:
            list: A list of bibliography entries.
        """
        with open(bib_file_path, "r") as bibtex_file:
            bib_database = loads(bibtex_file.read())
        return bib_database.entries

    def _find_bib_file(self, files, output_dir):
        """Find a .bib file in a directory.

        Args:
            files (list): A list of filenames to search through.
            output_dir (str): The directory containing the files.

        Returns:
            str: The path to the .bib file, or None if not found.
        """
        for file in files:
            if file.lower().endswith(".bib"):
                return os.path.join(output_dir, file)
        return None


class ArxivPaperCollection:
    def __init__(self):
        """Initialize an empty collection of ArxivPaper objects."""
        self.papers = {}

    def add_paper(self, paper):
        """Add an ArxivPaper to the collection.

        Args:
            paper (ArxivPaper): The paper to be added.
        """
        self.papers[paper.arxiv_id] = paper

    def remove_paper(self, arxiv_id):
        """Remove a paper from the collection by its arXiv ID.

        Args:
            arxiv_id (str): The arXiv ID of the paper to remove.
        """
        if arxiv_id in self.papers:
            del self.papers[arxiv_id]

    def get_paper(self, arxiv_id):
        """Get a paper from the collection by its arXiv ID.

        Args:
            arxiv_id (str): The arXiv ID of the paper to retrieve.

        Returns:
            ArxivPaper: The requested paper, or None if not found.
        """
        return self.papers.get(arxiv_id)

    def search_by_keyword(self, keyword):
        """Search for papers containing a keyword in their metadata.

        Args:
            keyword (str): The keyword to search for.

        Returns:
            ArxivPaperCollection: A new collection of papers matching the keyword.
        """
        return ArxivPaperCollection(
            {
                arxiv_id: paper
                for arxiv_id, paper in self.papers.items()
                if keyword.lower() in paper.title.lower()
                or keyword.lower() in paper.summary.lower()
                or any(keyword.lower() in tag.lower() for tag in paper.tags)
            }
        )

    def filter_by_author(self, author):
        """Filter the collection by author name.

        Args:
            author (str): The author's name to filter by.

        Returns:
            ArxivPaperCollection: A new collection of papers by the specified author.
        """
        return ArxivPaperCollection(
            {
                arxiv_id: paper
                for arxiv_id, paper in self.papers.items()
                if author in paper.authors
            }
        )

    def filter_by_date(self, start_date, end_date):
        """Filter the collection by submission date range.

        Args:
            start_date (str): The start date in 'YYYY-MM-DD' format.
            end_date (str): The end date in 'YYYY-MM-DD' format.

        Returns:
            ArxivPaperCollection: A new collection of papers within the date range.
        """
        start_date = datetime.strptime(start_date, "%Y-%m-%d")
        end_date = datetime.strptime(end_date, "%Y-%m-%d")
        return ArxivPaperCollection(
            {
                arxiv_id: paper
                for arxiv_id, paper in self.papers.items()
                if start_date <= paper.submitted_date <= end_date
            }
        )

    def apply_label(self, arxiv_id, label):
        """Apply a label to a paper in the collection.

        Args:
            arxiv_id (str): The arXiv ID of the paper to label.
            label (str): The label to apply.
        """
        if arxiv_id in self.papers:
            self.papers[arxiv_id].add_label(label)


import os
import zipfile
import re
import arxiv
from bibtexparser import loads
from datetime import datetime


class PaperDownloader:
    """Base class for downloading papers from different APIs."""

    def download_paper(self, paper_id, dirpath="downloads", filename=None):
        """Download paper by ID.

        Args:
            paper_id (str): The ID of the paper to download.
            dirpath (str): The directory path to save the paper.
            filename (str, optional): The filename to save the paper as.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")


class ArxivDownloader(PaperDownloader):
    """Class for downloading papers from arXiv."""

    def download_paper(self, paper_id, dirpath="downloads", filename=None):
        """Download paper by ID from arXiv.

        Args:
            paper_id (str): The arXiv ID of the paper to download.
            dirpath (str): The directory path to save the paper.
            filename (str, optional): The filename to save the paper as.
        """
        if not filename:
            filename = f"{paper_id}.zip"
        source_file_path = os.path.join(dirpath, filename)
        paper = next(arxiv.Client().results(arxiv.Search(id_list=[paper_id])))
        paper.download_source(dirpath=dirpath, filename=filename)
        return source_file_path


class PaperParser:
    """Base class for parsing papers."""

    def parse(self, source_file_path):
        """Parse the paper source files.

        Args:
            source_file_path (str): The path to the paper's source files.

        Returns:
            dict: Parsed metadata and content from the paper.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")


class LatexParser(PaperParser):
    """Class for parsing LaTeX source files."""

    def parse(self, source_file_path):
        """Parse LaTeX source files for metadata and content.

        Args:
            source_file_path (str): The path to the paper's source files.

        Returns:
            dict: Parsed metadata and content from the paper.
        """
        metadata = {"title": None, "authors": [], "abstract": None, "bib_info": None}
        with zipfile.ZipFile(source_file_path, "r") as zip_ref:
            zip_ref.extractall(os.path.dirname(source_file_path))
        output_dir = os.path.dirname(source_file_path)
        files = os.listdir(output_dir)
        main_tex_path = self._find_main_tex_file(files, output_dir)
        if main_tex_path:
            metadata.update(self._parse_latex_metadata(main_tex_path))
        bib_file_path = self._find_bib_file(files, output_dir)
        if bib_file_path:
            metadata["bib_info"] = self._parse_bib_file(bib_file_path)
        return metadata

    @staticmethod
    def _find_main_tex_file(files, output_dir):
        for file in files:
            if file.endswith(".tex"):
                file_path = os.path.join(output_dir, file)
                with open(file_path, "r") as tex_file:
                    if "\\documentclass{" in tex_file.read():
                        return file_path
        return None

    @staticmethod
    def _parse_latex_metadata(main_tex_path):
        metadata = {"title": None, "authors": [], "abstract": None}
        with open(main_tex_path, "r") as file:
            content = file.read()
            title_match = re.search(r"\\title\{(.+?)\}", content, re.DOTALL)
            if title_match:
                metadata["title"] = title_match.group(1).strip()
            authors_matches = re.finditer(r"\\author\{(.+?)\}", content, re.DOTALL)
            metadata["authors"] = [match.group(1).strip() for match in authors_matches]
            abstract_match = re.search(
                r"\\begin\{abstract\}(.+?)\\end\{abstract\}", content, re.DOTALL
            )
            if abstract_match:
                metadata["abstract"] = abstract_match.group(1).strip()
        return metadata

    @staticmethod
    def _parse_bib_file(bib_file_path):
        with open(bib_file_path, "r") as bibtex_file:
            bib_database = loads(bibtex_file.read())
        return bib_database.entries

    @staticmethod
    def _find_bib_file(files, output_dir):
        for file in files:
            if file.lower().endswith(".bib"):
                return os.path.join(output_dir, file)
        return None


class Paper:
    def __init__(self, paper_id, downloader, parser):
        """Initialize the Paper object with an ID and set the downloader and parser.

        Args:
            paper_id (str): The ID of the paper.
            downloader (PaperDownloader): The downloader instance to use.
            parser (PaperParser): The parser instance to use.
        """
        self.paper_id = paper_id
        self.downloader = downloader
        self.parser = parser
        self.metadata = {}
        self.source_file_path = None

    def fetch_and_parse(self):
        """Fetch paper source files and parse them."""
        self.source_file_path = self.downloader.download_paper(self.paper_id)
        self.metadata = self.parser.parse(self.source_file_path)

    def __str__(self):
        """Return a string representation of the paper."""
        return f"{self.paper_id}: {self.metadata.get('title', 'Unknown title')}"


class PaperCollection:
    def __init__(self):
        """Initialize an empty collection of Paper objects."""
        self.papers = {}

    def add_paper(self, paper):
        """Add a Paper to the collection.

        Args:
            paper (Paper): The paper to be added.
        """
        self.papers[paper.paper_id] = paper

    def remove_paper(self, paper_id):
        """Remove a paper from the collection by its ID.

        Args:
            paper_id (str): The ID of the paper to remove.
        """
        if paper_id in self.papers:
            del self.papers[paper_id]

    def get_paper(self, paper_id):
        """Get a paper from the collection by its ID.

        Args:
            paper_id (str): The ID of the paper to retrieve.

        Returns:
            Paper: The requested paper, or None if not found.
        """
        return self.papers.get(paper_id)
