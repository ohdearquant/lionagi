# from lionagi.core.branch.base_branch import BaseBranch
# from lionagi.core.branch.util import MessageUtil
# from lionagi.core.messages.schema import System
# from lionagi.core.schema import DataLogger

# import unittest
# from unittest.mock import patch, MagicMock
# import pandas as pd
# from datetime import datetime
# import json


# class TestBaseBranch(unittest.TestCase):

#     def setUp(self):
#         # Patching DataLogger to avoid filesystem interactions during tests
#         self.patcher = patch(
#             "lionagi.core.branch.base_branch.DataLogger", autospec=True
#         )
#         self.MockDataLogger = self.patcher.start()
#         self.addCleanup(self.patcher.stop)

#         self.branch = BaseBranch()
#         # Example messages to populate the DataFrame for testing
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
#         self.branch = BaseBranch(messages=pd.DataFrame(self.test_messages))

#     def test_init_with_empty_messages(self):
#         """Test __init__ method with no messages provided."""
#         branch = BaseBranch()
#         self.assertTrue(branch.messages.empty)

#     def test_init_with_given_messages(self):
#         """Test __init__ method with provided messages DataFrame."""
#         messages = pd.DataFrame(
#             [
#                 [
#                     "0",
#                     datetime(2024, 1, 1),
#                     "system",
#                     "system",
#                     json.dumps({"system_info": "Hi"}),
#                 ]
#             ],
#             columns=["node_id", "timestamp", "role", "sender", "content"],
#         )
#         branch = BaseBranch(messages=messages)
#         self.assertFalse(branch.messages.empty)

#     def test_add_message(self):
#         """Test adding a message."""
#         message_info = {"info": "Test Message"}
#         with patch.object(
#             MessageUtil, "create_message", return_value=System(system=message_info)
#         ) as mocked_create_message:
#             self.branch.add_message(system=message_info)
#             mocked_create_message.assert_called_once()
#             self.assertEqual(len(self.branch.messages), 6)

#     def test_chat_messages_without_sender(self):
#         """Test chat_messages property without including sender information."""
#         chat_messages = self.branch.chat_messages
#         self.assertEqual(len(chat_messages), 5)
#         self.assertNotIn("Sender", chat_messages[0]["content"])

#     def test_chat_messages_with_sender(self):
#         """Test retrieving chat messages with sender information included."""
#         chat_messages_with_sender = self.branch.chat_messages_with_sender
#         expected_prefixes = [
#             "Sender system: ",
#             "Sender user1: ",
#             "Sender assistant: ",
#             "Sender action_request: ",
#             "Sender action_response: ",
#         ]

#         # Verify the number of messages returned matches the number added
#         self.assertEqual(len(chat_messages_with_sender), len(self.test_messages))

#         # Check each message for correct sender prefix in content
#         for i, message_dict in enumerate(chat_messages_with_sender):
#             self.assertTrue(
#                 message_dict["content"].startswith(expected_prefixes[i]),
#                 msg=f"Message content does not start with expected sender prefix. Found: {message_dict['content']}",
#             )

#         # Optionally, verify the content matches after removing the prefix
#         for i, message_dict in enumerate(chat_messages_with_sender):
#             prefix, content = message_dict["content"].split(": ", 1)
#             self.assertEqual(
#                 content,
#                 self.test_messages[i]["content"],
#                 msg=f"Message content does not match after removing sender prefix. Found: {content}, Expected: {self.test_messages[i]['content']}",
#             )

#     def test_last_message(self):
#         """Test retrieving the last message."""
#         last_message = self.branch.last_message
#         self.assertEqual(
#             json.loads(last_message.iloc[0]["content"]),
#             {"action_response": "Action response"},
#         )

#     def test_last_message_content(self):
#         """Test retrieving the content of the last message."""
#         content = self.branch.last_message_content
#         self.assertEqual(content, {"action_response": "Action response"})

#     def test_first_system_message(self):
#         """Test retrieving the first 'system' message."""
#         first_system_message = self.branch.first_system
#         self.assertEqual(
#             json.loads(first_system_message.iloc[0]["content"]),
#             {"system_info": "System message"},
#         )

#     def test_last_response(self):
#         """Test retrieving the last 'assistant' message."""
#         last_response = self.branch.last_response
#         self.assertEqual(last_response.iloc[0]["sender"], "action_response")

#     def test_last_response_content(self):
#         """Test extracting content of the last 'assistant' message."""
#         content = self.branch.last_response_content
#         self.assertEqual(content, {"action_response": "Action response"})

#     def test_action_request_messages(self):
#         """Test filtering messages sent by 'action_request'."""
#         action_requests = self.branch.action_request
#         self.assertTrue(all(action_requests["sender"] == "action_request"))

#     def test_action_response_messages(self):
#         """Test filtering messages sent by 'action_response'."""
#         action_responses = self.branch.action_response
#         self.assertTrue(all(action_responses["sender"] == "action_response"))

#     def test_responses(self):
#         """Test filtering of 'assistant' role messages."""
#         responses = self.branch.responses
#         # Verify that all returned messages have the 'assistant' role
#         self.assertTrue(all(responses["role"] == "assistant"))
#         # Optionally, check the count matches the expected number of 'assistant' messages
#         expected_count = sum(
#             1 for msg in self.test_messages if msg["role"] == "assistant"
#         )
#         self.assertEqual(len(responses), expected_count)

#     def test_assistant_responses(self):
#         """Test filtering of 'assistant' messages excluding action requests/responses."""
#         assistant_responses = self.branch.assistant_responses
#         # Verify that no returned messages are from 'action_request' or 'action_response' senders
#         self.assertTrue(all(assistant_responses["sender"] != "action_request"))
#         self.assertTrue(all(assistant_responses["sender"] != "action_response"))
#         # Verify that all returned messages have the 'assistant' role
#         self.assertTrue(all(assistant_responses["role"] == "assistant"))

#     def test_info(self):
#         """Test summarization of message counts by role."""
#         info = self.branch.info
#         # Verify that the dictionary contains keys for each role and 'total'
#         self.assertIn("assistant", info)
#         self.assertIn("user", info)
#         self.assertIn("system", info)
#         self.assertIn("total", info)
#         # Optionally, verify the counts match expected values
#         for role in ["assistant", "user", "system"]:
#             expected_count = sum(1 for msg in self.test_messages if msg["role"] == role)
#             self.assertEqual(info[role], expected_count)
#         self.assertEqual(info["total"], len(self.test_messages))

#     def test_sender_info(self):
#         """Test summarization of message counts by sender."""
#         sender_info = self.branch.sender_info
#         # Verify that the dictionary contains keys for each sender and the counts match
#         for sender in set(msg["sender"] for msg in self.test_messages):
#             expected_count = sum(
#                 1 for msg in self.test_messages if msg["sender"] == sender
#             )
#             self.assertEqual(sender_info.get(sender, 0), expected_count)

#     def test_describe(self):
#         """Test detailed description of the branch."""
#         description = self.branch.describe
#         # Verify that the description contains expected keys
#         self.assertIn("total_messages", description)
#         self.assertIn("summary_by_role", description)
#         self.assertIn("messages", description)
#         # Optionally, verify the accuracy of the values
#         self.assertEqual(description["total_messages"], len(self.test_messages))
#         self.assertEqual(len(description["messages"]), min(5, len(self.test_messages)))

#     @patch("lionagi.libs.ln_dataframe.read_csv")
#     def test_from_csv(cls, mock_read_csv):
#         # Define a mock return value for read_csv
#         mock_messages_df = pd.DataFrame(
#             {
#                 "node_id": ["1", "2"],
#                 "timestamp": [datetime(2021, 1, 1), datetime(2021, 1, 1)],
#                 "role": ["system", "user"],
#                 "sender": ["system", "user1"],
#                 "content": [
#                     json.dumps({"system_info": "System message"}),
#                     json.dumps({"instruction": "User message"}),
#                 ],
#             }
#         )
#         mock_read_csv.return_value = mock_messages_df

#         # Call the from_csv class method
#         branch = BaseBranch.from_csv(filename="dummy.csv")

#         # Verify that read_csv was called correctly
#         mock_read_csv.assert_called_once_with("dummy.csv")

#         # Verify that the branch instance contains the correct messages
#         pd.testing.assert_frame_equal(branch.messages, mock_messages_df)

#     @patch("lionagi.libs.ln_dataframe.read_json")
#     def test_from_json(cls, mock_read_csv):
#         # Define a mock return value for read_csv
#         mock_messages_df = pd.DataFrame(
#             {
#                 "node_id": ["1", "2"],
#                 "timestamp": [datetime(2021, 1, 1), datetime(2021, 1, 1)],
#                 "role": ["system", "user"],
#                 "sender": ["system", "user1"],
#                 "content": [
#                     json.dumps({"system_info": "System message"}),
#                     json.dumps({"instruction": "User message"}),
#                 ],
#             }
#         )
#         mock_read_csv.return_value = mock_messages_df

#         # Call the from_csv class method
#         branch = BaseBranch.from_json_string(filename="dummy.json")

#         # Verify that read_csv was called correctly
#         mock_read_csv.assert_called_once_with("dummy.json")

#         # Verify that the branch instance contains the correct messages
#         pd.testing.assert_frame_equal(branch.messages, mock_messages_df)

#     @patch(
#         "lionagi.libs.sys_util.SysUtil.create_path",
#         return_value="path/to/messages.csv",
#     )
#     @patch.object(pd.DataFrame, "to_csv")
#     def test_to_csv(self, mock_to_csv, mock_create_path):
#         self.branch.datalogger = MagicMock()
#         self.branch.datalogger.persist_path = "data/logs/"

#         self.branch.to_csv_file("messages.csv", verbose=False, clear=False)

#         mock_create_path.assert_called_once_with(
#             self.branch.datalogger.persist_path,
#             "messages.csv",
#             timestamp=True,
#             dir_exist_ok=True,
#             time_prefix=False,
#         )
#         mock_to_csv.assert_called_once_with("path/to/messages.csv")

#         # Verify that messages are not cleared after exporting
#         assert not self.branch.messages.empty

#     @patch(
#         "lionagi.libs.sys_util.SysUtil.create_path",
#         return_value="path/to/messages.json",
#     )
#     @patch.object(pd.DataFrame, "to_json")
#     def test_to_json(self, mock_to_json, mock_create_path):
#         self.branch.datalogger = MagicMock()
#         self.branch.datalogger.persist_path = "data/logs/"

#         self.branch.to_json_file("messages.json", verbose=False, clear=False)

#         mock_create_path.assert_called_once_with(
#             self.branch.datalogger.persist_path,
#             "messages.json",
#             timestamp=True,
#             dir_exist_ok=True,
#             time_prefix=False,
#         )
#         mock_to_json.assert_called_once_with(
#             "path/to/messages.json", orient="records", lines=True, date_format="iso"
#         )

#         # Verify that messages are not cleared after exporting
#         assert not self.branch.messages.empty

#     # def test_log_to_csv(self):
#     #     self.branch.log_to_csv('log.csv', verbose=False, clear=False)

#     #     self.branch.datalogger.to_csv_file.assert_called_once_with(filename='log.csv', dir_exist_ok=True, timestamp=True,
#     #                                             time_prefix=False, verbose=False, clear=False)

#     # def test_log_to_json(self):
#     #     branch = BaseBranch()
#     #     branch.log_to_json('log.json', verbose=False, clear=False)

#     #     self.branch.datalogger.to_json_file.assert_called_once_with(filename='log.json', dir_exist_ok=True, timestamp=True,
#     #                                              time_prefix=False, verbose=False, clear=False)

#     def test_remove_message(self):
#         """Test removing a message from the branch based on its node ID."""
#         initial_length = len(self.branch.messages)
#         node_id_to_remove = "2"
#         self.branch.remove_message(node_id_to_remove)
#         final_length = len(self.branch.messages)
#         self.assertNotIn(node_id_to_remove, self.branch.messages["node_id"].tolist())
#         self.assertEqual(final_length, initial_length - 1)

#     def test_update_message(self):
#         """Test updating a specific message's column identified by node_id with a new value."""
#         node_id_to_update = "2"
#         new_value = "Updated content"
#         self.branch.update_message(node_id_to_update, "content", new_value)
#         updated_value = self.branch.messages.loc[
#             self.branch.messages["node_id"] == node_id_to_update, "content"
#         ].values[0]
#         self.assertEqual(updated_value, new_value)

#     # def test_change_first_system_message(self):
#     #     """Test updating the first system message with new content and/or sender."""
#     #     new_system_content = {"system_info": "Updated system message"}
#     #     self.branch.change_first_system_message(new_system_content['system_info'])
#     #     first_system_message_content = self.branch.messages.loc[self.branch.messages['role'] == 'system', 'content'].iloc[0]
#     #     self.assertIn(json.dumps({"system_info": "Updated system message"}), first_system_message_content)

#     def test_rollback(self):
#         """Test removing the last 'n' messages from the branch."""
#         steps_to_remove = 2
#         initial_length = len(self.branch.messages)
#         self.branch.rollback(steps_to_remove)
#         final_length = len(self.branch.messages)
#         self.assertEqual(final_length, initial_length - steps_to_remove)

#     def test_clear_messages(self):
#         """Test clearing all messages from the branch."""
#         self.branch.clear_messages()
#         self.assertTrue(self.branch.messages.empty)

#     def test_replace_keyword(self):
#         """Test replacing occurrences of a specified keyword with a replacement string."""
#         keyword = "Assistant response"
#         replacement = "Helper feedback"
#         self.branch.replace_keyword(keyword, replacement)
#         self.assertTrue(
#             any(replacement in message for message in self.branch.messages["content"])
#         )

#     def test_search_keywords(self):
#         """Test filtering messages by a specified keyword or list of keywords."""
#         keyword_to_search = "Assistant"
#         filtered_messages = self.branch.search_keywords(keyword_to_search)
#         self.assertTrue(
#             all(
#                 keyword_to_search in content for content in filtered_messages["content"]
#             )
#         )

#     def test_extend(self):
#         """Test extending branch messages with additional messages."""
#         additional_messages = pd.DataFrame(
#             [
#                 {
#                     "node_id": "6",
#                     "timestamp": datetime(2021, 1, 1),
#                     "role": "user",
#                     "sender": "user2",
#                     "content": json.dumps({"instruction": "Another user message"}),
#                 }
#             ]
#         )
#         initial_length = len(self.branch.messages)
#         self.branch.extend(additional_messages)
#         self.assertEqual(len(self.branch.messages), initial_length + 1)

#     def test_filter_by(self):
#         """Test filtering messages by various criteria."""
#         # Filtering by role
#         filtered_messages = self.branch.filter_by(role="user")
#         self.assertTrue(
#             all(msg["role"] == "user" for _, msg in filtered_messages.iterrows())
#         )


# if __name__ == "__main__":
#     unittest.main()
