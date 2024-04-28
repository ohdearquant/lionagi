import atexit
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from lionagi.libs import SysUtil, convert, nested


# TODO: there should be a global data logger, under setting


@dataclass
class DLog:
    """
    Defines a log entry structure for data processing operations.

    This class encapsulates both the input to and output from a data processing operation,
    along with an automatically generated timestamp indicating when the log entry was
    created. It aims to standardize logging across applications for easier analysis
    and debugging.

    Attributes:
        input_data: The data input to the operation. Can be of any type.
        output_data: The data output by the operation. Can be of any type.
    """

    input_data: Any
    output_data: Any

    def serialize(self, *, flatten_: bool = True, sep: str = "[^_^]") -> dict[str, Any]:
        """Serializes the DLog instance into a dictionary with an added timestamp.

        Args:
            flatten_ (bool): If True, flattens dictionary inputs for serialization.
            sep (str): Separator used in flattening nested dictionaries.

        Returns:
            A dictionary representation of the DLog instance, including 'input_data',
            'output_data', and 'timestamp'.
        """
        log_dict = {}

        def _process_data(data, field):
            try:
                data = convert.to_str(data)
                if "{" not in data:
                    log_dict[field] = convert.to_str(data)

                else:
                    with contextlib.suppress(Exception):
                        data = convert.to_dict(data)

                    if isinstance(self.input_data, dict) and flatten_:
                        log_dict[field] = convert.to_str(nested.flatten(data, sep=sep))

                    else:
                        log_dict[field] = convert.to_str(data)

            except Exception as e:
                log_dict[field] = data
                logging.error(f"Error in processing {field} to str: {e}")

        _process_data(self.input_data, "input_data")
        _process_data(self.output_data, "output_data")

        log_dict["timestamp"] = SysUtil.get_timestamp()

        return log_dict

    @classmethod
    def deserialize(
        cls,
        *,
        input_str: str,
        output_str: str,
        unflatten_: bool = True,
        sep: str = "[^_^]",
    ) -> "DLog":
        """Deserializes log entries from string representations of input and output data.

        This method reconstructs a DLog instance from serialized string data, optionally
        unflattening nested dictionary structures if they were flattened during the
        serialization process. The method is particularly useful for reading logs from
        storage formats like JSON or CSV where data is represented as strings.

        Note:
            The separator '[^_^]' is reserved and should not be used within dictionary
            keys to ensure proper structure preservation during unflattening.

        Args:
            input_str: String representation of the input data.
            output_str: String representation of the output data.
            unflatten_ (bool): Indicates whether to unflatten the string data back into
                               nested dictionaries.
            sep (str): Separator used if unflattening is performed.

        Returns:
            An instance of DLog reconstructed from the provided string data.

        Raises:
            ValueError: If deserialization or unflattening fails due to incorrect data
                        format or separator issues.
        """

        def _process_data(data):
            if unflatten_:
                try:
                    return nested.unflatten(convert.to_dict(data), sep=sep)
                except:
                    return data
            else:
                return data

        input_data = _process_data(input_str)
        output_data = _process_data(output_str)

        return cls(input_data=input_data, output_data=output_data)


class DataLogger:
    """
    Manages logging for data processing activities within an application.

    This class handles the accumulation,
    structuring, and persistence of log entries.
    It supports exporting logs to disk in both CSV and JSON formats,
    with features for
    automatic log saving at program exit and customizable file naming.

    Attributes:
        persist_path: Path to the directory for saving log files.
        log: Container for log entries.
        filename: Base name for log files.
    """

    def __init__(
        self,
        persist_path: str | Path | None = None,
        log: List[Dict] | None = None,
        filename: str | None = None,
    ) -> None:
        """
        Initializes the DataLogger with optional custom settings for log storage.

        Args:
            persist_path: The file system path for storing log files. Defaults to 'data/logs/'.
            log: Initial log entries.
            filename: Base name for exported log files.
        """
        self.persist_path = Path(persist_path) if persist_path else Path("data/logs/")
        self.log = deque(log) if log else deque()
        self.filename = filename or "log"
        atexit.register(self.save_at_exit)

    def extend(self, logs) -> None:
        """
        Extends the log deque with multiple log entries.

        This method allows for bulk addition of log entries, which can be useful for
        importing logs from external sources or consolidating logs from different parts
        of an application.

        Args:
            logs: A list of log entries, each as a dictionary conforming to the log
                  structure (e.g., containing 'input_data', 'output_data', etc.).
        """
        if len(logs) > 0:
            log1 = convert.to_list(self.log)
            log1.extend(convert.to_list(logs))
            self.log = deque(log1)

    def append(self, *, input_data: Any, output_data: Any) -> None:
        """
        Appends a new log entry from provided input and output data.

        Args:
            input_data: Input data to the operation.
            output_data: Output data from the operation.
        """
        log_entry = DLog(input_data=input_data, output_data=output_data)
        self.log.append(log_entry)

    def to_csv_file(
        self,
        filename: str = "log.csv",
        *,
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        flatten_=True,
        sep="[^_^]",
        index=False,
        random_hash_digits=3,
        **kwargs,
    ) -> None:
        """Exports log entries to a CSV file with customizable options.

        Args:
            filename: Filename for the exported CSV. Defaults to 'log.csv'.
            dir_exist_ok: If True, allows writing to an existing directory.
            timestamp: If True, appends a timestamp to the filename.
            time_prefix: If True, places the timestamp prefix before the filename.
            verbose: If True, prints a message upon successful save.
            clear: If True, clears the log deque after saving.
            flatten_: If True, flattens dictionary data for serialization.
            sep: Separator for flattening nested dictionaries.
            index: If True, includes an index column in the CSV.
            **kwargs: Additional arguments for DataFrame.to_csv().
        """

        if not filename.endswith(".csv"):
            filename += ".csv"

        filepath = SysUtil.create_path(
            self.persist_path,
            filename,
            timestamp=timestamp,
            dir_exist_ok=dir_exist_ok,
            time_prefix=time_prefix,
            random_hash_digits=random_hash_digits,
        )
        try:
            logs = [log.serialize(flatten_=flatten_, sep=sep) for log in self.log]
            df = convert.to_df(convert.to_list(logs, flatten=True))
            df.to_csv(filepath, index=index, **kwargs)
            if verbose:
                print(f"{len(self.log)} logs saved to {filepath}")
            if clear:
                self.log.clear()
        except Exception as e:
            raise ValueError(f"Error in saving to csv: {e}") from e

    def to_json_file(
        self,
        filename: str = "log.json",
        *,
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        flatten_=True,
        sep="[^_^]",
        index=False,
        random_hash_digits=3,
        **kwargs,
    ) -> None:
        """Exports log entries to a JSON file with customizable options.

        Args:
            filename: Filename for the exported JSON. Defaults to 'log.json'.
            dir_exist_ok: If True, allows writing to an existing directory.
            timestamp: If True, appends a timestamp to the filename.
            time_prefix: If True, places the timestamp prefix before the filename.
            verbose: If True, prints a message upon successful save.
            clear: If True, clears the log deque after saving.
            flatten_: If True, flattens dictionary data for serialization.
            sep: Separator for flattening nested dictionaries.
            index: If True, includes an index in the JSON.
            **kwargs: Additional arguments for DataFrame.to_json().
        """
        if not filename.endswith(".json"):
            filename += ".json"

        filepath = SysUtil.create_path(
            self.persist_path,
            filename,
            timestamp=timestamp,
            dir_exist_ok=dir_exist_ok,
            time_prefix=time_prefix,
            random_hash_digits=random_hash_digits,
        )

        try:
            logs = [log.serialize(flatten_=flatten_, sep=sep) for log in self.log]
            df = convert.to_df(convert.to_list(logs, flatten=True))
            df.to_json(filepath, index=index, **kwargs)
            if verbose:
                print(f"{len(self.log)} logs saved to {filepath}")
            if clear:
                self.log.clear()
        except Exception as e:
            raise ValueError(f"Error in saving to csv: {e}") from e

    def save_at_exit(self):
        """
        Registers an at-exit handler to ensure that any unsaved logs are automatically
        persisted to a file upon program termination. This safeguard helps prevent the
        loss of log data due to unexpected shutdowns or program exits.

        The method is configured to save the logs to a CSV file, named
        'unsaved_logs.csv', which is stored in the designated persisting directory. This
        automatic save operation is triggered only if there are unsaved logs present at
        the time of program exit.

        Note: This method does not clear the logs after saving, allowing for the
        possibility of manual.py review or recovery after the program has terminated.
        """
        if self.log:
            self.to_csv_file("unsaved_logs.csv", clear=False)
