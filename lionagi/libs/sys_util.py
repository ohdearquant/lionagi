"""
MIT License

Copyright (c) 2023 HaiyangLi quantocean.li@gmail.com

Permission is hereby granted, free of charge, to any person 
obtaining a copy of this software and associated documentation 
files (the "Software"), to deal in the Software without 
restriction, including without limitation the rights to use, 
copy, modify, merge, publish, distribute, sublicense, and/or 
sell copies of the Software, and to permit persons to whom 
the Software is furnished to do so, subject to the following 
conditions:

The above copyright notice and this permission notice shall be 
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, 
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES 
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND 
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS 
BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN 
ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN 
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE 
SOFTWARE.
"""

import copy
import importlib
import logging
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

_timestamp_syms = ["-", ":", "."]

PATH_TYPE = str | Path


class SysUtil:

    @staticmethod
    def sleep(delay: float) -> None:
        """
        Pauses execution for a specified duration.

        Args:
                delay (float): The amount of time, in seconds, to pause execution.
        """
        time.sleep(delay)

    @staticmethod
    def get_now(datetime_: bool = False, tz=None) -> float | datetime:
        """Returns the current time either as a Unix timestamp or a datetime object.

        Args:
                datetime_ (bool): If True, returns a datetime object; otherwise, returns a Unix timestamp.

        Returns:
                Union[float, datetime.datetime]: The current time as a Unix timestamp or a datetime object.
        """

        if not datetime_:
            return time.time()
        config_ = {}
        if tz:
            config_["tz"] = tz if isinstance(tz, timezone) else timezone.utc
        return datetime.now(**config_)

    @staticmethod
    def change_dict_key(dict_: dict[Any, Any], old_key: str, new_key: str) -> None:
        """Safely changes a key in a dictionary if the old key exists.

        Args:
                dict_ (Dict[Any, Any]): The dictionary in which to change the key.
                old_key (str): The old key to be changed.
                new_key (str): The new key to replace the old key.

        Returns:
                None
        """
        if old_key in dict_:
            dict_[new_key] = dict_.pop(old_key)

    @staticmethod
    def get_timestamp(tz: timezone = timezone.utc, sep: str = "_") -> str:
        """Returns a timestamp string with optional custom separators and timezone.

        Args:
                tz (timezone): The timezone for the timestamp.
                sep (str): The separator to use in the timestamp string, replacing '-', ':', and '.'.

        Returns:
                str: A string representation of the current timestamp.
        """
        str_ = datetime.now(tz=tz).isoformat()
        if sep is not None:
            for sym in _timestamp_syms:
                str_ = str_.replace(sym, sep)
        return str_

    @staticmethod
    def is_schema(dict_: dict[Any, Any], schema: dict[Any, type]) -> bool:
        """Validates if the given dictionary matches the expected schema types."""
        return all(
            isinstance(dict_.get(key), expected_type)
            for key, expected_type in schema.items()
        )

    @staticmethod
    def create_copy(input_: Any, num: int = 1) -> Any | list[Any]:
        """Creates deep copies of the input, either as a single copy or a list of copies.

        Args:
                input_ (Any): The input to be copied.
                num (int): The number of copies to create.

        Returns:
                Union[Any, List[Any]]: A single copy of the input or a list of deep copies.
        """
        if num < 1:
            raise ValueError(f"'num' must be a positive integer: {num}")
        return (
            copy.deepcopy(input_)
            if num == 1
            else [copy.deepcopy(input_) for _ in range(num)]
        )

    @staticmethod
    def create_id(n: int = 32) -> str:
        """
        Generates a unique identifier based on the current time and random bytes.

        Args:
                n (int): The length of the generated identifier.

        Returns:
                str: A unique identifier string.
        """
        current_time = datetime.now().isoformat().encode("utf-8")
        random_bytes = os.urandom(42)
        return sha256(current_time + random_bytes).hexdigest()[:n]

    @staticmethod
    def get_bins(input_: list[str], upper: int | None = 2000) -> list[list[int]]:
        """Organizes indices of strings into bins based on a cumulative upper limit.

        Args:
                input_ (List[str]): The list of strings to be binned.
                upper (int): The cumulative length upper limit for each bin.

        Returns:
                List[List[int]]: A list of bins, each bin is a list of indices from the input list.
        """
        current = 0
        bins = []
        current_bin = []
        for idx, item in enumerate(input_):
            if current + len(item) < upper:
                current_bin.append(idx)
                current += len(item)
            else:
                bins.append(current_bin)
                current_bin = [idx]
                current = len(item)
        if current_bin:
            bins.append(current_bin)
        return bins

    @staticmethod
    def get_cpu_architecture() -> str:
        """Returns a string identifying the CPU architecture.

        This method categorizes some architectures as 'apple_silicon'.

        Returns:
                str: A string identifying the CPU architecture ('apple_silicon' or 'other_cpu').
        """
        arch: str = platform.machine().lower()
        return "apple_silicon" if "arm" in arch or "aarch64" in arch else "other_cpu"

    @staticmethod
    def install_import(
        package_name: str,
        module_name: str = None,
        import_name: str = None,
        pip_name: str = None,
    ) -> None:
        """Attempts to import a package, installing it with pip if not found.

        This method tries to import a specified module or attribute. If the import fails, it attempts
        to install the package using pip and then retries the import.

        Args:
                package_name: The base name of the package to import.
                module_name: The submodule name to import from the package, if applicable. Defaults to None.
                import_name: The specific name to import from the module or package. Defaults to None.
                pip_name: The pip package name if different from `package_name`. Defaults to None.

        Prints a message indicating success or attempts installation if the import fails.
        """
        pip_name: str = pip_name or package_name
        full_import_path: str = (
            f"{package_name}.{module_name}" if module_name else package_name
        )

        try:
            if import_name:
                module = __import__(full_import_path, fromlist=[import_name])
                getattr(module, import_name)
            else:
                __import__(full_import_path)
            print(f"Successfully imported {import_name or full_import_path}.")
        except ImportError:
            print(
                f"Module {full_import_path} or attribute {import_name} not found. Installing {pip_name}..."
            )
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])

            # Retry the import after installation
            if import_name:
                module = __import__(full_import_path, fromlist=[import_name])
                getattr(module, import_name)
            else:
                __import__(full_import_path)

    @staticmethod
    def import_module(module_path: str):
        return importlib.import_module(module_path)

    @staticmethod
    def is_package_installed(package_name: str) -> bool:
        """Checks if a package is currently installed.

        Args:
                package_name: The name of the package to check.

        Returns:
                A boolean indicating whether the package is installed.
        """
        package_spec = importlib.util.find_spec(package_name)
        return package_spec is not None

    @staticmethod
    def check_import(
        package_name: str,
        module_name: str | None = None,
        import_name: str | None = None,
        pip_name: str | None = None,
        attempt_install: bool = True,
        error_message: str = "",
    ) -> None:
        """Checks if a package is installed; if not, attempts to install and import it.

        This method first checks if a package is installed using `is_package_installed`. If not found,
        it attempts to install the package using `install_import` and then retries the import.

        Args:
                package_name: The name of the package to check and potentially install.
                module_name: The submodule name to import from the package, if applicable. Defaults to None.
                import_name: The specific name to import from the module or package. Defaults to None.
                pip_name: The pip package name if different from `package_name`. Defaults to None.
                attempt_install: If attempt to install the package if uninstalled. Defaults to True.
                error_message: Error message when the package is not installed and not attempt to install.
        """
        try:
            if not SysUtil.is_package_installed(package_name):
                print("check")
                if attempt_install:
                    logging.info(
                        f"Package {package_name} not found. Attempting to install."
                    )
                    SysUtil.install_import(
                        package_name, module_name, import_name, pip_name
                    )
                else:
                    logging.info(f"Package {package_name} not found. {error_message}")
                    raise ImportError(
                        f"Package {package_name} not found. {error_message}"
                    )
        except ImportError as e:  # More specific exception handling
            logging.error(f"Failed to import {package_name}. Error: {e}")
            raise ValueError(f"Failed to import {package_name}. Error: {e}") from e

    @staticmethod
    def list_installed_packages() -> list:
        """list all installed packages using importlib.metadata."""
        return [dist.metadata["Name"] for dist in importlib.metadata.distributions()]

    @staticmethod
    def uninstall_package(package_name: str) -> None:
        """Uninstall a specified package."""
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "uninstall", package_name, "-y"]
            )
            print(f"Successfully uninstalled {package_name}.")
        except subprocess.CalledProcessError as e:
            print(f"Failed to uninstall {package_name}. Error: {e}")

    @staticmethod
    def update_package(package_name: str) -> None:
        """Update a specified package."""
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--upgrade", package_name]
            )
            print(f"Successfully updated {package_name}.")
        except subprocess.CalledProcessError as e:
            print(f"Failed to update {package_name}. Error: {e}")

    @staticmethod
    def clear_dir(
        dir_path: Path | str, recursive: bool = False, exclude: list[str] = None
    ) -> None:
        """
        Clears all files (and, if recursive, directories) in the specified directory,
        excluding files that match any pattern in the exclude list.

        Args:
                dir_path (Union[Path, str]): The path to the directory to clear.
                recursive (bool): If True, clears directories recursively. Defaults to False.
                exclude (List[str]): A list of string patterns to exclude from deletion. Defaults to None.

        Raises:
                FileNotFoundError: If the specified directory does not exist.
        """
        dir_path = Path(dir_path)
        if not dir_path.exists():
            raise FileNotFoundError(
                f"The specified directory {dir_path} does not exist."
            )

        exclude = exclude or []
        exclude_pattern = re.compile("|".join(exclude)) if exclude else None

        for file_path in dir_path.iterdir():
            if exclude_pattern and exclude_pattern.search(file_path.name):
                logging.info(f"Excluded from deletion: {file_path}")
                continue

            if recursive and file_path.is_dir():
                SysUtil.clear_dir(file_path, recursive=True, exclude=exclude)
            elif file_path.is_file() or file_path.is_symlink():
                try:
                    file_path.unlink()
                    logging.info(f"Successfully deleted {file_path}")
                except Exception as e:
                    logging.error(f"Failed to delete {file_path}. Reason: {e}")
                    raise

    @staticmethod
    def split_path(path: Path | str) -> tuple[Path, str]:
        """
        Splits a path into its directory and filename components.

        Args:
                path (Union[Path, str]): The path to split.

        Returns:
                Tuple[Path, str]: A tuple containing the directory and filename.
        """
        path = Path(path)
        return path.parent, path.name

    @staticmethod
    def create_path(
        directory: Path | str,
        filename: str,
        timestamp: bool = True,
        dir_exist_ok: bool = True,
        time_prefix: bool = False,
        custom_timestamp_format: str | None = None,
        random_hash_digits=0,
    ) -> Path:
        """
        Creates a path with an optional timestamp in the specified directory.

        Args:
                directory (Union[Path, str]): The directory where the file will be located.
                filename (str): The filename. Must include a valid extension.
                timestamp (bool): If True, adds a timestamp to the filename. Defaults to True.
                dir_exist_ok (bool): If True, does not raise an error if the directory exists. Defaults to True.
                time_prefix (bool): If True, adds the timestamp as a prefix; otherwise, as a suffix. Defaults to False.
                custom_timestamp_format (str): A custom format for the timestamp. Defaults to "%Y%m%d%H%M%S".

        Returns:
                Path: The full path to the file.

        Raises:
                ValueError: If the filename is invalid.
        """
        directory = Path(directory)
        if not re.match(r"^[\w,\s-]+\.[A-Za-z]{1,5}$", filename):
            raise ValueError(
                "Invalid filename. Ensure it doesn't contain illegal characters and has a valid extension."
            )

        name, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
        ext = f".{ext}" if ext else ""

        timestamp_str = ""
        if timestamp:
            timestamp_format = custom_timestamp_format or "%Y%m%d%H%M%S"
            timestamp_str = datetime.now().strftime(timestamp_format)
            filename = (
                f"{timestamp_str}_{name}" if time_prefix else f"{name}_{timestamp_str}"
            )
        else:
            filename = name

        random_hash = (
            "-" + SysUtil.create_id(random_hash_digits)
            if random_hash_digits > 0
            else ""
        )

        full_filename = f"{filename}{random_hash}{ext}"
        full_path = directory / full_filename
        full_path.parent.mkdir(parents=True, exist_ok=dir_exist_ok)

        return full_path

    @staticmethod
    def list_files(dir_path: Path | str, extension: str = None) -> list[Path]:
        dir_path = Path(dir_path)
        if not dir_path.is_dir():
            raise NotADirectoryError(f"{dir_path} is not a directory.")
        if extension:
            return list(dir_path.glob(f"*.{extension}"))
        else:
            return list(dir_path.glob("*"))

    @staticmethod
    def copy_file(src: Path | str, dest: Path | str) -> None:
        from shutil import copy2

        src, dest = Path(src), Path(dest)
        if not src.is_file():
            raise FileNotFoundError(f"{src} does not exist or is not a file.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy2(src, dest)

    @staticmethod
    def get_size(path: Path | str) -> int:
        path = Path(path)
        if path.is_file():
            return path.stat().st_size
        elif path.is_dir():
            return sum(f.stat().st_size for f in path.glob("**/*") if f.is_file())
        else:
            raise FileNotFoundError(f"{path} does not exist.")

    @staticmethod
    def save_to_file(
        text,
        directory: Path | str,
        filename: str,
        timestamp: bool = True,
        dir_exist_ok: bool = True,
        time_prefix: bool = False,
        custom_timestamp_format: str | None = None,
        random_hash_digits=0,
        verbose=True,
    ):
        file_path = SysUtil.create_path(
            directory=directory,
            filename=filename,
            timestamp=timestamp,
            dir_exist_ok=dir_exist_ok,
            time_prefix=time_prefix,
            custom_timestamp_format=custom_timestamp_format,
            random_hash_digits=random_hash_digits,
        )

        with open(file_path, "w") as file:
            file.write(text)

        if verbose:
            print(f"Text saved to: {file_path}")

        return True
