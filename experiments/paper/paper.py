class Paper:

    def __init__(
        self,
        entry_id=None,
        updated=None,
        published=None,
        title="",
        authors=[],
        summary="",
        comment="",
        journal_ref="",
        doi="",
        primary_category="",
        categories=[],
        links=[],
    ):
        self.entry_id = entry_id
        self.updated = updated
        self.published = published
        self.title = title
        self.authors = authors
        self.summary = summary
        self.comment = comment
        self.journal_ref = journal_ref
        self.doi = doi
        self.primary_category = primary_category
        self.categories = categories
        self.links = links
        self.bib_info = None
        self.source_file_path = None
        self.latex_content = None

    @staticmethod
    def result_to_dict(result):
        dict_ = {
            "entry_id": result.entry_id,
            "updated": result.updated,
            "published": result.published,
            "title": result.title,
            "authors": [author.name for author in result.authors],
            "summary": result.summary,
            "comment": result.comment,
            "journal_ref": result.journal_ref,
            "doi": result.doi,
            "primary_category": result.primary_category,
            "categories": result.categories,
            "links": result.links,
        }
        return dict_

    @classmethod
    def download_and_populate(cls, result, dirpath=None, filename=None):
        result_dict_ = cls.result_to_dict(result)

        dirpath = dirpath if dirpath else os.path.join(os.getcwd(), "arxiv_downloads/")
        extract_path = os.path.join(dirpath, f"{result_dict_['entry_id']}/")

        if not os.path.exists(extract_path):
            os.makedirs(extract_path, exist_ok=True)

        filepath = ArxivSearch().download(
            result, dirpath=dirpath, filename=filename, kind="source"
        )
        IOUtil.decompress_tar_gz(file_path=filepath, extract_path=extract_path)

        self = cls(**result_dict_)
        self.source_file_path = filepath

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
