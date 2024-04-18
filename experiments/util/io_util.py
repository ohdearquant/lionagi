import csv
import json
import os
import tempfile
import tarfile
from collections.abc import Iterable
from typing import Any, Dict, List


class IOUtil:
    """
    Utility class for reading and writing data from/to files in various formats.
    """

    @staticmethod
    def read_csv(filepath: str) -> List[Dict[str, Any]]:
        """
        Reads a CSV file and returns its contents as a list of dictionaries.

        Args:
            filepath (str): The path to the CSV file to be read.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries, each representing a row in the CSV file.
        """
        with open(filepath, "r", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            return list(reader)

    @staticmethod
    def read_jsonl(filepath: str) -> List[Any]:
        """
        Reads a JSON Lines file and returns its contents as a list.

        Args:
            filepath (str): The path to the JSON Lines file to be read.

        Returns:
            List[Any]: A list where each element is a JSON object from a line in the file.
        """
        with open(filepath, "r") as f:
            return [json.loads(line) for line in f]

    @staticmethod
    def write_json(data: List[Dict[str, Any]], filepath: str) -> None:
        """
        Writes a list of dictionaries to a JSON file.

        Args:
            data (List[Dict[str, Any]]): The data to be written to the JSON file.
            filepath (str): The path where the JSON file will be saved.

        Returns:
            None: This function does not return anything.
        """
        with open(filepath, "w") as json_file:
            json.dump(data, json_file, indent=4)

    @staticmethod
    def read_json(filepath: str) -> Any:
        """
        Reads a JSON file and returns its content.

        Args:
            filepath (str): The path to the JSON file to be read.

        Returns:
            Any: The content of the JSON file.
        """
        with open(filepath, "r") as json_file:
            return json.load(json_file)

    @staticmethod
    def merge_csv_files(filepaths: List[str], output_filepath: str) -> None:
        """
        Merges multiple CSV files into a single CSV file.

        Args:
            filepaths (List[str]): A list of file paths to the CSV files to be merged.
            output_filepath (str): The path where the merged CSV file will be saved.

        Returns:
            None: This function does not return anything.
        """
        merged_data = []
        fieldnames = set()
        for filepath in filepaths:
            with open(filepath, "r", newline="") as csv_file:
                reader = csv.DictReader(csv_file)
                if reader.fieldnames is not None:
                    fieldnames.update(reader.fieldnames)
                    for row in reader:
                        merged_data.append(row)
        fieldnames = list(fieldnames)
        with open(output_filepath, "w", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(merged_data)

    @staticmethod
    def to_csv(
        input: List[Dict[str, Any]], filepath: str, file_exist_ok: bool = False
    ) -> None:
        """
        Writes a list of dictionaries to a CSV file.

        Args:
            input: A list of dictionaries where each dictionary represents a row in the CSV.
            filepath: The path to the CSV file to write to.
            file_exist_ok: If True, creates the directory for the file if it does not exist.

        Raises:
            FileNotFoundError: If the directory does not exist and file_exist_ok is False.

        Examples:
            >>> data = [{'name': 'Alice', 'age': 30}, {'name': 'Bob', 'age': 25}]
            >>> to_csv(data, 'people.csv', file_exist_ok=True)
        """
        if not input:
            return
        if (
            not os.path.exists(os.path.dirname(filepath))
            and os.path.dirname(filepath) != ""
        ):
            if file_exist_ok:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
            else:
                raise FileNotFoundError(
                    f"The directory {os.path.dirname(filepath)} does not exist."
                )

        with open(filepath, "w", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=input[0].keys())
            writer.writeheader()
            for row in input:
                writer.writerow(row)

    @staticmethod
    def append_to_jsonl(data: Any, filepath: str) -> None:
        """
        Appends a data entry to a JSON Lines file.

        Args:
            data: The data to append, which can be any JSON serializable object.
            filepath: The path to the JSON Lines file to append to.

        Examples:
            >>> append_to_jsonl({'name': 'Charlie', 'age': 35}, 'people.jsonl')
        """
        json_string = json.dumps(data)
        with open(filepath, "a") as f:
            f.write(json_string + "\n")

    @staticmethod
    def to_temp(input: Any) -> tempfile.NamedTemporaryFile:
        """
        Writes the given input to a temporary file in JSON format.

        Args:
            input: The input data to write to the file. Can be a string or an iterable.

        Returns:
            A NamedTemporaryFile object representing the temporary file.

        Raises:
            TypeError: If the input data is not JSON serializable.

        Examples:
            >>> temp_file = to_temp("test string")
            >>> with open(temp_file.name, 'r') as file:
            ...     content = json.load(file)
            >>> content
            ["test string"]

            >>> temp_file = to_temp(["test", "string"])
            >>> with open(temp_file.name, 'r') as file:
            ...     content = json.load(file)
            >>> content
            ["test", "string"]
        """
        if isinstance(input, str):
            input = [input]
        elif isinstance(input, Iterable):
            input = [item for item in input if item is not None]

        temp_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
        try:
            json.dump(input, temp_file)
        except TypeError as e:
            temp_file.close()  # Ensuring file closure before raising error
            raise TypeError(f"Data provided is not JSON serializable: {e}")
        temp_file.close()
        return temp_file

    @staticmethod
    def decompress_tar_gz(file_path, extract_path="."):
        """
        Decompresses a .tar.gz file to a specified directory.

        :param file_path: Path to the .tar.gz file
        :param extract_path: Directory where the contents will be extracted
        """
        # Open the tar.gz file
        with tarfile.open(file_path, "r:gz") as tar:
            # Extract all the contents into the specified directory
            tar.extractall(path=extract_path)
