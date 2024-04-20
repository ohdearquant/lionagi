# from lionagi.core.branch.branch import Branch
# from lionagi.core.tool.tool_manager import ToolManager, func_to_tool
# from lionagi.core.schema import DataLogger
# from lionagi.core.branch.util import MessageUtil


# import unittest
# from unittest.mock import patch, MagicMock
# import pandas as pd
# import json
# from collections import deque


# class TestBranch(unittest.TestCase):
#     def setUp(self):
#         # Assuming no need for actual files or external services for initialization
#         self.test_messages = [
#             {
#                 "node_id": "1",
#                 "timestamp": "2021-01-01 00:00:00",
#                 "role": "system",
#                 "sender": "system",
#                 "content": json.dumps({"system_info": "System message"}),
#             },
#             {
#                 "node_id": "2",
#                 "timestamp": "2021-01-01 00:01:00",
#                 "role": "user",
#                 "sender": "user1",
#                 "content": json.dumps({"instruction": "User message"}),
#             },
#             {
#                 "node_id": "3",
#                 "timestamp": "2021-01-01 00:02:00",
#                 "role": "assistant",
#                 "sender": "assistant",
#                 "content": json.dumps({"response": "Assistant response"}),
#             },
#             {
#                 "node_id": "4",
#                 "timestamp": "2021-01-01 00:03:00",
#                 "role": "assistant",
#                 "sender": "action_request",
#                 "content": json.dumps({"action_request": "Action request"}),
#             },
#             {
#                 "node_id": "5",
#                 "timestamp": "2021-01-01 00:04:00",
#                 "role": "assistant",
#                 "sender": "action_response",
#                 "content": json.dumps({"action_response": "Action response"}),
#             },
#         ]
#         self.branch = Branch(
#             branch_name="TestBranch", messages=pd.DataFrame(self.test_messages)
#         )

#         def sample_func(param1: int) -> bool:
#             """Sample function.

#             Args:
#                 param1 (int): Description of param1.

#             Returns:
#                 bool: Description of return value.
#             """
#             return True

#         self.tool = func_to_tool(sample_func)

#     def test_initialization(self):
#         """Test the initialization of the Branch class."""
#         self.assertEqual(self.branch.branch_name, "TestBranch")
#         self.assertIsInstance(self.branch.tool_manager, ToolManager)
#         self.assertIsInstance(self.branch.datalogger, DataLogger)
#         self.assertEqual(self.branch.sender, "system")

#     def test_has_tools_property(self):
#         """Test the has_tools property."""
#         # Initially, no tools are registered
#         self.assertFalse(self.branch.has_tools)

#         # Mock tool registration
#         self.branch.register_tools(self.tool)
#         self.assertTrue(self.branch.has_tools)


#     # @patch("lionagi.core.branch.BaseBranch._from_csv")
#     # def test_from_csv(self, mock_from_csv):
#     #     """Test creating a Branch instance from a CSV file."""
#     #     filepath = "path/to/your/csvfile.csv"
#     #     Branch.from_csv(filepath=filepath, branch_name="TestBranchFromCSV")
#     #     mock_from_csv.assert_called_once_with(
#     #         filepath=filepath,
#     #         read_kwargs=None,
#     #         branch_name="TestBranchFromCSV",
#     #         service=None,
#     #         llmconfig=None,
#     #         tools=None,
#     #         datalogger=None,
#     #         persist_path=None,
#     #         tool_manager=None,
#     #     )

#     # @patch("lionagi.core.branch.BaseBranch._from_json")
#     # def test_from_json(self, mock_from_json):
#     #     """Test creating a Branch instance from a JSON file."""
#     #     filepath = "path/to/your/jsonfile.json"
#     #     Branch.from_json_string(filepath=filepath, branch_name="TestBranchFromJSON")
#     #     mock_from_json.assert_called_once_with(
#     #         filepath=filepath,
#     #         read_kwargs=None,
#     #         branch_name="TestBranchFromJSON",
#     #         service=None,
#     #         llmconfig=None,
#     #         tools=None,
#     #         datalogger=None,
#     #         persist_path=None,
#     #         tool_manager=None,
#     #     )


#     def test_messages_describe(self):
#         """Test the messages_describe method for accuracy."""
#         # Assuming self.branch has been set up with some messages
#         description = self.branch.messages_describe()
#         self.assertIn("total_messages", description)
#         self.assertIn("summary_by_role", description)
#         self.assertIn("summary_by_sender", description)
#         self.assertIn("registered_tools", description)

#     def test_merge_branch(self):
#         """Test merging another Branch instance into the current one."""
#         mes = [
#             {
#                 "node_id": "6",
#                 "timestamp": "2021-01-01 00:01:00",
#                 "role": "user",
#                 "sender": "user1",
#                 "content": json.dumps({"instruction": "User message"}),
#             }
#         ]
#         other_branch = Branch(branch_name="OtherBranch", messages=pd.DataFrame(mes))

#         original_message_count = len(self.branch.messages)
#         self.branch.merge_branch(other_branch)
#         merged_message_count = len(self.branch.messages)
#         self.assertTrue(merged_message_count > original_message_count)

#     def test_register_and_delete_tools(self):
#         """Test tool registration and deletion."""
#         self.branch.register_tools(self.tool)
#         self.assertIn("sample_func", self.branch.tool_manager.registry)
#         self.branch.delete_tools(self.tool, verbose=False)
#         self.assertNotIn("sample_func", self.branch.tool_manager.registry)

#     def test_send(self):
#         """Test sending a mail package."""
#         package = {"data": "example"}
#         self.branch.send(recipient="BranchB", category="messages", package=package)
#         self.assertEqual(len(self.branch.pending_outs), 1)
#         mail = self.branch.pending_outs[0]
#         self.assertEqual(mail.sender, "system")
#         self.assertEqual(mail.recipient, "BranchB")
#         self.assertEqual(mail.category, "messages")
#         self.assertEqual(mail.package, package)

#     # def test_is_invoked_true(self):
#     #     branch = Branch()

#     #     mock_messages = [
#     #         MessageUtil.to_json_content({"action_response": {"function": "func_name", "arguments": {}, "output": "result"}})
#     #     ]
#     #     branch.messages = pd.DataFrame(mock_messages, columns=['content'])
#     #     self.assertTrue(branch._is_invoked())

#     def test_is_invoked_false(self):
#         """Test that _is_invoked returns False when the last message is not a valid action response."""
#         self.assertFalse(self.branch._is_invoked())


# class TestBranchReceive(unittest.TestCase):
#     def setUp(self):
#         self.branch = Branch(branch_name="TestBranch")
#         # Set up a mock sender and initial pending_ins structure
#         self.sender = "MockSender"
#         self.branch.pending_ins[self.sender] = deque()

#     # @patch("lionagi.core.mail.BaseMail")
#     # @patch("lionagi.core.branch.util.MessageUtil.validate_messages")
#     # def test_receive_messages(self, mock_validate_messages, mock_base_mail):
#     #     # Prepare a mock mail package with messages
#     #     messages_df = pd.DataFrame(
#     #         [
#     #             {
#     #                 "node_id": "1",
#     #                 "timestamp": "2021-01-01 00:00:00",
#     #                 "role": "system",
#     #                 "sender": "system",
#     #                 "content": json.dumps({"system_info": "System message"}),
#     #             }
#     #         ]
#     #     )
#     #     mail_package_messages = MagicMock(category="messages", package=messages_df)
#     #     self.branch.pending_ins[self.sender].append(mail_package_messages)

#     #     # Test receiving messages
#     #     self.branch.receive(self.sender)
#     #     mock_validate_messages.assert_called_once_with(messages_df)
#     #     self.assertTrue(len(self.branch.messages) > 0)
#     #     self.assertEqual(self.branch.pending_ins, {})

#     # def test_receive_tools(self):
#     #     def sample_func(param1: int) -> bool:
#     #         """Sample function.

#     #         Args:
#     #             param1 (int): Description of param1.

#     #         Returns:
#     #             bool: Description of return value.
#     #         """
#     #         return True

#     #     tool = func_to_tool(sample_func)
#     #     mail_package_tools = MagicMock(category="tools", package=tool)
#     #     self.branch.pending_ins[self.sender].append(mail_package_tools)

#     #     # Test receiving tools
#     #     self.branch.receive(self.sender)
#     #     self.assertIn(tool, self.branch.tool_manager.registry.values())

#     def test_receive_service(self):
#         # Prepare a mock mail package with a service
#         from lionagi.libs.ln_api import BaseService

#         service = BaseService()
#         mail_package_service = MagicMock(category="provider", package=service)
#         self.branch.pending_ins[self.sender].append(mail_package_service)

#         # Test receiving service
#         self.branch.receive(self.sender)
#         self.assertEqual(self.branch.service, service)

#     def test_receive_llmconfig(self):
#         # Prepare a mock mail package with llmconfig
#         llmconfig = self.branch.llmconfig.copy()
#         mail_package_llmconfig = MagicMock(category="llmconfig", package=llmconfig)
#         self.branch.pending_ins[self.sender].append(mail_package_llmconfig)

#         # Test receiving llmconfig
#         self.branch.receive(self.sender)
#         self.assertEqual(llmconfig, self.branch.llmconfig)

#     def test_invalid_format(self):
#         # Test handling of invalid package format
#         invalid_package = MagicMock(category="messages", package="Not a DataFrame")
#         self.branch.pending_ins[self.sender].append(invalid_package)

#         with self.assertRaises(ValueError) as context:
#             self.branch.receive(self.sender)
#         self.assertTrue("Invalid messages format" in str(context.exception))

#     def test_receive_all(self):
#         messages_df = pd.DataFrame(
#             [
#                 {
#                     "node_id": "1",
#                     "timestamp": "2021-01-01 00:00:00",
#                     "role": "system",
#                     "sender": "system",
#                     "content": json.dumps({"system_info": "System message"}),
#                 }
#             ]
#         )
#         mail_package_messages = MagicMock(category="messages", package=messages_df)
#         self.branch.pending_ins[self.sender].append(mail_package_messages)

#         llmconfig = self.branch.llmconfig.copy()
#         mail_package_llmconfig = MagicMock(category="llmconfig", package=llmconfig)
#         self.branch.pending_ins[self.sender].append(mail_package_llmconfig)

#         self.branch.receive_all()
#         self.assertTrue(
#             not self.branch.pending_ins,
#             "pending_ins should be empty or contain only skipped requests",
#         )
#         self.assertTrue(..., "Additional assertions based on your implementation")


# # Chatflow: call_chatcompletion, chat, ReAct, auto_followup

# if __name__ == "__main__":
#     unittest.main()
