"""
This module contains the Branch class, which represents a branch in a conversation tree.
"""

from abc import ABC
from collections import deque
from pathlib import Path
from typing import Any, Callable, TypeVar, Union

from dotenv import load_dotenv

from lionagi.libs import BaseService, StatusTracker, SysUtil, convert, dataframe
from lionagi.core.generic import BaseNode, DataLogger, DLog
from ..generic import Mail
from ..tool import TOOL_TYPE, Tool, ToolManager, func_to_tool
from ..message import BaseMessage, Instruction, Response, System
from ..message.base import MessageField
from .util import MessageUtil
from .mixin import BranchFlowMixin

load_dotenv()


T = TypeVar("T", bound=Tool)
BRANCH_COLUMNS = [i.value for i in MessageField]


class BaseBranch(BaseNode, ABC):

    def __init__(
        self,
        messages: dataframe.ln_DataFrame | None = None,
        datalogger: DataLogger | None = None,
        persist_path: str | Path | None = None,
        name=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if isinstance(messages, dataframe.ln_DataFrame):
            if MessageUtil.validate_messages(messages):
                self.messages = messages
            else:
                raise ValueError("Invalid messages format")
        else:
            self.messages = dataframe.ln_DataFrame(columns=BRANCH_COLUMNS)

        self.datalogger = datalogger or DataLogger(persist_path=persist_path)
        self.name = name

    def add_message(
        self,
        system: dict | list | System | None = None,
        instruction: dict | list | Instruction | None = None,
        context: str | dict[str, Any] | None = None,
        response: dict | list | BaseMessage | None = None,
        output_fields=None,
        recipient=None,
        **kwargs,
    ) -> None:
        _msg = MessageUtil.create_message(
            system=system,
            instruction=instruction,
            context=context,
            response=response,
            output_fields=output_fields,
            recipient=recipient,
            **kwargs,
        )

        if isinstance(_msg, System):
            self.system_node = _msg

        # sourcery skip: merge-nested-ifs
        if isinstance(_msg, Instruction):
            if recipient is None and self.name is not None:
                _msg.recipient = self.name

        if isinstance(_msg, Response):
            if "action_response" in _msg.content.keys():
                if recipient is None and self.name is not None:
                    _msg.recipient = self.name
                if recipient is not None and self.name is None:
                    _msg.recipient = recipient
            if "response" in _msg.content.keys() and self.name is not None:
                _msg.sender = self.name

        setattr(_msg, "node_id", _msg.id_)
        _msg.content = _msg.msg_content
        self.messages.loc[len(self.messages)] = _msg.to_pd_series()

    @property
    def last_message(self) -> dataframe.ln_DataFrame:
        return MessageUtil.get_message_rows(self.messages, n=1, from_="last")

    @property
    def responses(self) -> dataframe.ln_DataFrame:
        return convert.to_df(self.messages[self.messages.role == "assistant"])

    @property
    def describe(self) -> dict[str, Any]:

        return {
            "total_messages": len(self.messages),
            "summary_by_role": self._info(),
            "messages": [msg.to_dict() for _, msg in self.messages.iterrows()][
                : len(self.messages) - 1 if len(self.messages) < 5 else 5
            ],
        }

    @classmethod
    def _from_csv(cls, filename: str, read_kwargs=None, **kwargs) -> "BaseBranch":
        read_kwargs = {} if read_kwargs is None else read_kwargs
        messages = dataframe.read_csv(filename, **read_kwargs)
        return cls(messages=messages, **kwargs)

    @classmethod
    def from_csv(cls, **kwargs) -> "BaseBranch":
        return cls._from_csv(**kwargs)

    @classmethod
    def from_json_string(cls, **kwargs) -> "BaseBranch":
        return cls._from_json(**kwargs)

    @classmethod
    def _from_json(cls, filename: str, read_kwargs=None, **kwargs) -> "BaseBranch":
        read_kwargs = {} if read_kwargs is None else read_kwargs
        messages = dataframe.read_json(filename, **read_kwargs)
        return cls(messages=messages, **kwargs)

    def to_csv_file(
        self,
        filename: str | Path = "messages.csv",
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        **kwargs,
    ) -> None:

        if not filename.endswith(".csv"):
            filename += ".csv"

        filename = SysUtil.create_path(
            self.datalogger.persist_path,
            filename,
            timestamp=timestamp,
            dir_exist_ok=dir_exist_ok,
            time_prefix=time_prefix,
        )

        try:
            self.messages.to_csv(filename, **kwargs)
            if verbose:
                print(f"{len(self.messages)} messages saved to {filename}")
            if clear:
                self.clear_messages()
        except Exception as e:
            raise ValueError(f"Error in saving to csv: {e}") from e

    def to_json_file(
        self,
        filename: str | Path = "messages.json",
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        **kwargs,
    ) -> None:
        if not filename.endswith(".json"):
            filename += ".json"

        filename = SysUtil.create_path(
            self.datalogger.persist_path,
            filename,
            timestamp=timestamp,
            dir_exist_ok=dir_exist_ok,
            time_prefix=time_prefix,
        )

        try:
            self.messages.to_json(
                filename, orient="records", lines=True, date_format="iso", **kwargs
            )
            if verbose:
                print(f"{len(self.messages)} messages saved to {filename}")
            if clear:
                self.clear_messages()
        except Exception as e:
            raise ValueError(f"Error in saving to json: {e}") from e

    def log_to_csv(
        self,
        filename: str | Path = "log.csv",
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        flatten_=True,
        sep="[^_^]",
        **kwargs,
    ) -> None:
        self.datalogger.to_csv_file(
            filename=filename,
            dir_exist_ok=dir_exist_ok,
            timestamp=timestamp,
            time_prefix=time_prefix,
            verbose=verbose,
            clear=clear,
            flatten_=flatten_,
            sep=sep,
            **kwargs,
        )

    def log_to_json(
        self,
        filename: str | Path = "log.json",
        dir_exist_ok: bool = True,
        timestamp: bool = True,
        time_prefix: bool = False,
        verbose: bool = True,
        clear: bool = True,
        flatten_=True,
        sep="[^_^]",
        **kwargs,
    ) -> None:
        self.datalogger.to_json_file(
            filename=filename,
            dir_exist_ok=dir_exist_ok,
            timestamp=timestamp,
            time_prefix=time_prefix,
            verbose=verbose,
            clear=clear,
            flatten_=flatten_,
            sep=sep,
            **kwargs,
        )

    def load_log(self, filename, flattened=True, sep="[^_^]", verbose=True, **kwargs):
        df = ""
        try:
            if filename.endswith(".csv"):
                df = dataframe.read_csv(filename, **kwargs)

            elif filename.endswith(".json"):
                df = dataframe.read_json(filename, **kwargs)

            for _, row in df.iterrows():
                self.datalogger.log.append(
                    DLog.deserialize(
                        input_str=row.input_data,
                        output_str=row.output_data,
                        unflatten_=flattened,
                        sep=sep,
                    )
                )

            if verbose:
                print(f"Loaded {len(df)} logs from {filename}")
        except Exception as e:
            raise ValueError(f"Error in loading log: {e}") from e

    def remove_message(self, node_id: str) -> None:
        MessageUtil.remove_message(self.messages, node_id)

    def update_message(self, node_id: str, column: str, value: Any) -> bool:

        index = self.messages[self.messages["node_id"] == node_id].index[0]

        return dataframe.update_row(
            self.messages, row=index, column=column, value=value
        )

    def change_first_system_message(
        self, system: str | dict[str, Any] | System, sender: str | None = None
    ) -> None:

        if len(self.messages[self.messages["role"] == "system"]) == 0:
            raise ValueError("There is no system message in the messages.")

        if not isinstance(system, (str, dict, System)):
            raise ValueError("Input cannot be converted into a system message.")

        if isinstance(system, (str, dict)):
            system = System(system, sender=sender)

        if isinstance(system, System):
            system.timestamp = SysUtil.get_timestamp()
            sys_index = self.messages[self.messages.role == "system"].index
            self.messages.loc[sys_index[0]] = system.to_pd_series()

    def rollback(self, steps: int) -> None:

        self.messages = dataframe.remove_last_n_rows(self.messages, steps)

    def clear_messages(self) -> None:
        self.messages = dataframe.ln_DataFrame(columns=self._columns)

    def replace_keyword(
        self,
        keyword: str,
        replacement: str,
        column: str = "content",
        case_sensitive: bool = False,
    ) -> None:

        dataframe.replace_keyword(
            self.messages,
            keyword,
            replacement,
            column=column,
            case_sensitive=case_sensitive,
        )

    def search_keywords(
        self,
        keywords: str | list[str],
        case_sensitive: bool = False,
        reset_index: bool = False,
        dropna: bool = False,
    ) -> dataframe.ln_DataFrame:
        return dataframe.search_keywords(
            self.messages,
            keywords,
            case_sensitive=case_sensitive,
            reset_index=reset_index,
            dropna=dropna,
        )

    def extend(self, messages: dataframe.ln_DataFrame, **kwargs) -> None:

        self.messages = MessageUtil.extend(self.messages, messages, **kwargs)

    def filter_by(
        self,
        role: str | None = None,
        sender: str | None = None,
        start_time=None,
        end_time=None,
        content_keywords: str | list[str] | None = None,
        case_sensitive: bool = False,
    ) -> dataframe.ln_DataFrame:

        return MessageUtil.filter_messages_by(
            self.messages,
            role=role,
            sender=sender,
            start_time=start_time,
            end_time=end_time,
            content_keywords=content_keywords,
            case_sensitive=case_sensitive,
        )

    def _info(self, use_sender: bool = False) -> dict[str, int]:

        messages = self.messages["sender"] if use_sender else self.messages["role"]
        result = messages.value_counts().to_dict()
        result["total"] = len(self.messages)
        return result

    def _to_chatcompletion_message(
        self, with_sender: bool = False
    ) -> list[dict[str, Any]]:
        message = []

        for _, row in self.messages.iterrows():
            content_ = row["content"]
            if content_.startswith("Sender"):
                content_ = content_.split(":", 1)[1]
                
            out = {"role": row["role"], "content": content_}
            if with_sender:
                out["content"] = f"Sender {row['sender']}: {content_}"

            message.append(out)
        return message


    # for backward compatibility
    # will be deprecated in future versions
    @property
    def info(self) -> dict[str, Any]:
        """
        Summarizes branch information, including message counts by role.

        Returns:
                A dictionary containing counts of messages categorized by their role.
        """

        return self._info()


class Branch(BaseBranch, BranchFlowMixin):
    def __init__(
        self,
        name: str | None = None,
        system: dict | list | System | None = None,
        messages: dataframe.ln_DataFrame | None = None,
        service: BaseService | None = None,
        sender: str | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        tools: list[Callable | Tool] | None = None,
        datalogger: None | DataLogger = None,
        persist_path: str | Path | None = None,  # instruction_sets=None,
        tool_manager: ToolManager | None = None,
        **kwargs,
    ):

        super().__init__(
            messages=messages,
            datalogger=datalogger,
            persist_path=persist_path,
            name=name,
            **kwargs,
        )

        self.sender = sender or "system"
        self.tool_manager = tool_manager or ToolManager()

        if tools:
            try:
                tools_ = []
                _tools = convert.to_list(tools)
                for i in _tools:
                    if isinstance(i, Tool):
                        tools_.append(i)
                    else:
                        tools_.append(func_to_tool(i))

                self.register_tools(tools_)
            except Exception as e:
                raise TypeError(f"Error in registering tools: {e}") from e

        self.service, self.llmconfig = self._add_service(service, llmconfig)
        self.status_tracker = StatusTracker()

        # add instruction sets
        # self.instruction_sets = instruction_sets

        self.pending_ins = {}
        self.pending_outs = deque()

        if system is not None:
            self.add_message(system=system)

    @classmethod
    def from_csv(
        cls,
        filepath,
        name: str | None = None,
        service: BaseService | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        tools: TOOL_TYPE | None = None,
        datalogger: None | DataLogger = None,
        persist_path: str | Path | None = None,  # instruction_sets=None,
        tool_manager: ToolManager | None = None,
        read_kwargs=None,
        **kwargs,
    ) -> "Branch":
        return cls._from_csv(
            filepath=filepath,
            read_kwargs=read_kwargs,
            name=name,
            service=service,
            llmconfig=llmconfig,
            tools=tools,
            datalogger=datalogger,
            persist_path=persist_path,
            # instruction_sets=instruction_sets,
            tool_manager=tool_manager,
            **kwargs,
        )

    @classmethod
    def from_json_string(
        cls,
        filepath,
        name: str | None = None,
        service: BaseService | None = None,
        llmconfig: dict[str, str | int | dict] | None = None,
        tools: TOOL_TYPE | None = None,
        datalogger: None | DataLogger = None,
        persist_path: str | Path | None = None,  # instruction_sets=None,
        tool_manager: ToolManager | None = None,
        read_kwargs=None,
        **kwargs,
    ) -> "Branch":
        return cls._from_json(
            filepath=filepath,
            read_kwargs=read_kwargs,
            name=name,
            service=service,
            llmconfig=llmconfig,
            tools=tools,
            datalogger=datalogger,
            persist_path=persist_path,
            # instruction_sets=instruction_sets,
            tool_manager=tool_manager,
            **kwargs,
        )

    def messages_describe(self) -> dict[str, Any]:
        return dict(
            total_messages=len(self.messages),
            summary_by_role=self._info(),
            summary_by_sender=self._info(use_sender=True),
            # instruction_sets=self.instruction_sets,
            registered_tools=self.tool_manager.registry,
            messages=[msg.to_dict() for _, msg in self.messages.iterrows()],
        )

    @property
    def has_tools(self) -> bool:
        return self.tool_manager.registry != {}

    # todo: also update other attributes
    def merge_branch(self, branch: "Branch", update: bool = True) -> None:
        message_copy = branch.messages.copy()
        self.messages = self.messages.merge(message_copy, how="outer")
        self.datalogger.extend(branch.datalogger.log)

        if update:
            # self.instruction_sets.update(branch.instruction_sets)
            self.tool_manager.registry.update(branch.tool_manager.registry)
        else:
            for key, value in branch.instruction_sets.items():
                if key not in self.instruction_sets:
                    self.instruction_sets[key] = value

            for key, value in branch.tool_manager.registry.items():
                if key not in self.tool_manager.registry:
                    self.tool_manager.registry[key] = value

    # ----- tool manager methods ----- #
    def register_tools(
        self, tools: Union[Tool, list[Tool | Callable], Callable]
    ) -> None:
        if not isinstance(tools, list):
            tools = [tools]
        self.tool_manager.register_tools(tools=tools)

    def delete_tools(
        self,
        tools: Union[T, list[T], str, list[str]],
        verbose: bool = True,
    ) -> bool:
        if not isinstance(tools, list):
            tools = [tools]
        if convert.is_same_dtype(tools, str):
            for act_ in tools:
                if act_ in self.tool_manager.registry:
                    self.tool_manager.registry.pop(act_)
            if verbose:
                print("tools successfully deleted")
            return True
        elif convert.is_same_dtype(tools, Tool):
            for act_ in tools:
                if act_.schema_["function"]["name"] in self.tool_manager.registry:
                    self.tool_manager.registry.pop(act_.schema_["function"]["name"])
            if verbose:
                print("tools successfully deleted")
            return True
        if verbose:
            print("tools deletion failed")
        return False

    def send(self, recipient_id: str, category: str, package: Any) -> None:
        mail = Mail(
            sender_id=self.id_,
            recipient_id=recipient_id,
            category=category,
            package=package,
        )
        self.pending_outs.append(mail)

    def receive(
        self,
        sender: str,
        messages: bool = True,
        tools: bool = True,
        service: bool = True,
        llmconfig: bool = True,
    ) -> None:
        skipped_requests = deque()
        if sender not in self.pending_ins:
            raise ValueError(f"No package from {sender}")
        while self.pending_ins[sender]:
            mail_ = self.pending_ins[sender].popleft()

            if mail_.category == "messages" and messages:
                if not isinstance(mail_.package, dataframe.ln_DataFrame):
                    raise ValueError("Invalid messages format")
                MessageUtil.validate_messages(mail_.package)
                self.messages = self.messages.merge(mail_.package, how="outer")

            elif mail_.category == "tools" and tools:
                if not isinstance(mail_.package, Tool):
                    raise ValueError("Invalid tools format")
                self.tool_manager.register_tools([mail_.package])

            elif mail_.category == "provider" and service:
                from lionagi.libs.ln_api import BaseService

                if not isinstance(mail_.package, BaseService):
                    raise ValueError("Invalid provider format")
                self.service = mail_.package

            elif mail_.category == "llmconfig" and llmconfig:
                if not isinstance(mail_.package, dict):
                    raise ValueError("Invalid llmconfig format")
                self.llmconfig.update(mail_.package)

            else:
                skipped_requests.append(mail_)

        self.pending_ins[sender] = skipped_requests
        if self.pending_ins[sender] == deque():
            self.pending_ins.pop(sender)

    def receive_all(self) -> None:
        """
        Receives all pending mails and updates the branch accordingly.
        """
        for key in list(self.pending_ins.keys()):
            self.receive(key)

    @staticmethod
    def _add_service(service, llmconfig):
        from lionagi.integrations.provider.oai import OpenAIService

        if service is None:
            try:
                from lionagi.integrations.provider import Services

                service = Services.OpenAI()

            except:
                raise ValueError("No available service")
        if llmconfig is None:
            if isinstance(service, OpenAIService):
                from lionagi.integrations.config import oai_schema

                llmconfig = oai_schema["chat/completions"]["config"]
            else:
                llmconfig = {}
        return service, llmconfig

    def _is_invoked(self) -> bool:
        """
        Check if the conversation has been invoked with an action response.

        Returns:
                bool: True if the conversation has been invoked, False otherwise.

        """
        content = self.messages.iloc[-1]["content"]
        try:
            if convert.to_dict(content)["action_response"].keys() >= {
                "function",
                "arguments",
                "output",
            }:
                return True
        except Exception:
            return False
        return False
