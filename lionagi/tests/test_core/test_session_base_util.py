# from lionagi.core.branch.util import MessageUtil
# from lionagi.core.messages.schema import System, Instruction, Response


# import unittest
# import pandas as pd
# import json
# from datetime import datetime


# class TestCreateMessage(unittest.TestCase):

# 	def test_create_system_message(self):
# 		"""Test creating a System message."""
# 		system_info = {"system_info": "System information"}
# 		message = MessageUtil.create_message(
# 			system=system_info["system_info"]
# 			)
# 		self.assertIsInstance(message, System)
# 		self.assertEqual(message.content, system_info)

# 	def test_create_instruction_message(self):
# 		"""Test creating an Instruction message with context."""
# 		instruction_info = {"task": "Do something"}
# 		context = {"additional": "context"}
# 		message = MessageUtil.create_message(
# 			instruction=instruction_info, context=context
# 		)
# 		self.assertIsInstance(message, Instruction)
# 		self.assertEqual(message.content["instruction"], instruction_info)
# 		self.assertEqual(message.content["context"], context)

# 	def test_create_response_message(self):
# 		"""Test creating a Response message."""
# 		response_info = {"message": {"content": "This is a response"}}
# 		message = MessageUtil.create_message(response=response_info)
# 		self.assertIsInstance(message, Response)
# 		self.assertEqual(
# 			message.content["response"], response_info["message"]["content"]
# 		)

# 	def test_error_on_multiple_roles(self):
# 		"""Test error is raised when multiple roles are provided."""
# 		with self.assertRaises(ValueError):
# 			MessageUtil.create_message(
# 				system={"info": "info"}, instruction={"task": "task"}
# 			)

# 	def test_return_existing_base_message_instance(self):
# 		"""Test returning an existing BaseMessage instance if provided."""
# 		existing_message = System(system={"info": "Already created"})
# 		message = MessageUtil.create_message(system=existing_message)
# 		self.assertEqual(message.content, existing_message.content)


# class TestValidateMessages(unittest.TestCase):

# 	# def test_validate_messages_correct_format(self):
# 	#     """Test messages DataFrame with the correct format."""
# 	#     messages = pd.DataFrame({
# 	#         "node_id": ["1"],
# 	#         "role": ["user"],
# 	#         "sender": ["test"],
# 	#         "timestamp": ["2020-01-01T00:00:00"],
# 	#         "content": ['{"message": "test"}']
# 	#     })
# 	#     self.assertTrue(MessageUtil.validate_messages(messages))

# 	def test_validate_messages_incorrect_columns(self):
# 		"""Test messages DataFrame with incorrect columns raises ValueError."""
# 		messages = pd.DataFrame(
# 			{
# 				"id": ["1"], "type": ["user"], "source": ["test"],
# 				"time": ["2020-01-01T00:00:00"],
# 				"data": ['{"message": "test"}'],
# 			}
# 		)
# 		with self.assertRaises(ValueError):
# 			MessageUtil.validate_messages(messages)

# 	def test_validate_messages_null_values(self):
# 		"""Test messages DataFrame with null values raises ValueError."""
# 		messages = pd.DataFrame(
# 			{
# 				"node_id": [None], "role": ["user"], "sender": ["test"],
# 				"timestamp": ["2020-01-01T00:00:00"],
# 				"content": ['{"message": "test"}'],
# 			}
# 		)
# 		with self.assertRaises(ValueError):
# 			MessageUtil.validate_messages(messages)


# class TestSignMessage(unittest.TestCase):

# 	def test_sign_message(self):
# 		"""Test signing message content with sender."""
# 		messages = pd.DataFrame(
# 			{
# 				"node_id": ["1"], "role": ["user"], "sender": ["test"],
# 				"timestamp": ["2020-01-01T00:00:00"],
# 				"content": ["Original message"],
# 			}
# 		)
# 		sender = "system"
# 		signed_messages = MessageUtil.sign_message(messages, sender)
# 		expected_content = "Sender system: Original message"
# 		self.assertEqual(signed_messages["content"][0], expected_content)

# 	def test_sign_message_invalid_sender(self):
# 		"""Test signing message with an invalid sender raises ValueError."""
# 		messages = pd.DataFrame(
# 			{
# 				"node_id": ["1"], "role": ["user"], "sender": ["test"],
# 				"timestamp": ["2020-01-01T00:00:00"],
# 				"content": ["Original message"],
# 			}
# 		)
# 		with self.assertRaises(ValueError):
# 			MessageUtil.sign_message(messages, None)


# class TestFilterMessagesBy(unittest.TestCase):

# 	def setUp(self):
# 		self.messages = pd.DataFrame(
# 			{
# 				"node_id": ["1", "2"], "role": ["user", "assistant"],
# 				"sender": ["test", "assistant"],
# 				"timestamp": [datetime(2020, 1, 1), datetime(2020, 1, 2)],
# 				"content": ['{"message": "test"}', '{"response": "ok"}'],
# 			}
# 		)

# 	def test_filter_by_role(self):
# 		"""Test filtering messages by role."""
# 		filtered = MessageUtil.filter_messages_by(
# 			self.messages, role="assistant"
# 			)
# 		self.assertEqual(len(filtered), 1)
# 		self.assertEqual(filtered.iloc[0]["sender"], "assistant")

# 	def test_filter_by_sender(self):
# 		"""Test filtering messages by sender."""
# 		filtered = MessageUtil.filter_messages_by(
# 			self.messages, sender="test"
# 			)
# 		self.assertEqual(len(filtered), 1)
# 		self.assertEqual(filtered.iloc[0]["sender"], "test")

# 	def test_filter_by_time_range(self):
# 		"""Test filtering messages by time range."""
# 		start_time = datetime(2020, 1, 1, 12)
# 		end_time = datetime(2020, 1, 2, 12)
# 		filtered = MessageUtil.filter_messages_by(
# 			self.messages, start_time=start_time, end_time=end_time
# 		)
# 		self.assertEqual(len(filtered), 1)
# 		self.assertTrue(
# 			start_time <= filtered.iloc[0]["timestamp"] <= end_time
# 			)


# class TestRemoveMessage(unittest.TestCase):

# 	def test_remove_message(self):
# 		"""Test removing a message by node_id."""
# 		messages = pd.DataFrame(
# 			{
# 				"node_id": ["1", "2"], "role": ["user", "assistant"],
# 				"content": ["message1", "message2"],
# 			}
# 		)
# 		updated_messages = MessageUtil.remove_message(messages, "1")
# 		self.assertTrue(updated_messages)


# class TestGetMessageRows(unittest.TestCase):

# 	def test_get_message_rows(self):
# 		"""Test retrieving the last 'n' message rows based on criteria."""
# 		messages = pd.DataFrame(
# 			{
# 				"node_id": ["1", "2", "3"],
# 				"role": ["user", "assistant", "user"],
# 				"sender": ["A", "B", "A"],
# 				"content": ["message1", "message2", "message3"],
# 			}
# 		)
# 		rows = MessageUtil.get_message_rows(
# 			messages, sender="A", role="user", n=2
# 			)
# 		self.assertEqual(len(rows), 2)


# # class TestExtend(unittest.TestCase):

# # def test_extend(self):
# #     """Test extending one DataFrame with another, ensuring no duplicate 'node_id'."""
# #     df1 = pd.DataFrame({
# #         "node_id": ["1"],
# #         "role": ["user"],
# #         "sender": ["test"],
# #         "timestamp": ["2020-01-01T00:00:00"],
# #         "content": ['{"message": "test"}']
# #     })
# #     df2 = pd.DataFrame({
# #         "node_id": ["2"],
# #         "role": ["user"],
# #         "sender": ["test"],
# #         "timestamp": ["2020-01-02T00:00:00"],
# #         "content": ['{"message": "test2"}']
# #     })
# #     combined = MessageUtil.extend(df1, df2)
# #     self.assertEqual(len(combined), 2)


# class TestToMarkdownString(unittest.TestCase):

# 	def test_to_markdown_string(self):
# 		"""Test converting messages to a Markdown-formatted string."""
# 		messages = pd.DataFrame(
# 			{
# 				"node_id": ["1"], "role": ["user"],
# 				"content": [json.dumps({"instruction": "Hello, World!"})],
# 			}
# 		)
# 		markdown_str = MessageUtil.to_markdown_string(messages)
# 		self.assertIn("Hello, World!", markdown_str)


# # class TestSearchKeywords(unittest.TestCase):

# #     def test_search_keywords(self):
# #         """Test filtering DataFrame for rows containing specified keywords."""
# #         messages = pd.DataFrame(
# #             {"node_id": ["1", "2"], "content": ["Hello world", "Goodbye world"]}
# #         )
# #         filtered = MessageUtil.search_keywords(messages, "Hello")
# #         print(filtered)
# #         self.assertEqual(len(filtered), 1)


# # class TestReplaceKeyword(unittest.TestCase):

# #     def test_replace_keyword(self):
# #         """Test replacing a keyword in DataFrame's specified column."""
# #         messages = pd.DataFrame({"content": ["Hello world", "Goodbye world"]})
# #         MessageUtil.replace_keyword(messages, "world", "universe")
# #         self.assertTrue(all(messages["content"].str.contains("universe")))


# # class TestReadCsv(unittest.TestCase):

# # @patch("pandas.read_csv")
# # def test_read_csv(self, mock_read_csv):
# #     """Test reading a CSV file into a DataFrame."""
# #     mock_df = pd.DataFrame(
# #         {"node_id": ["1", "2"], "content": ["Hello, World!", "Goodbye, World!"]}
# #     )
# #     mock_read_csv.return_value = mock_df

# #     df = MessageUtil.read_csv("path/to/nonexistent/file.csv")

# #     mock_read_csv.assert_called_once_with("path/to/nonexistent/file.csv")

# #     self.assertTrue(isinstance(df, pd.DataFrame))
# #     self.assertEqual(len(df), 2)
# #     self.assertEqual(list(df.columns), ["node_id", "content"])


# # class TestReadJson(unittest.TestCase):

# #     @patch("pandas.read_json")
# #     def test_read_json(self, mock_read_json):
# #         """Test reading a JSON file into a DataFrame."""
# #         mock_df = pd.DataFrame(
# #             {"node_id": ["1", "2"], "content": ["JSON Message 1", "JSON Message 2"]}
# #         )
# #         mock_read_json.return_value = mock_df

# #         df = MessageUtil.read_json("path/to/nonexistent/file.json")

# #         mock_read_json.assert_called_once_with("path/to/nonexistent/file.json")

# #         self.assertTrue(isinstance(df, pd.DataFrame))
# #         self.assertEqual(len(df), 2)
# #         self.assertEqual(list(df.columns), ["node_id", "content"])


# # class TestRemoveLastNRows(unittest.TestCase):

# #     def test_remove_last_n_rows(self):
# #         """Test removing the last 'n' rows from a DataFrame."""
# #         messages = pd.DataFrame({"content": ["message1", "message2", "message3"]})
# #         updated = MessageUtil.remove_last_n_rows(messages, 2)
# #         self.assertEqual(len(updated), 1)


# # class TestUpdateRow(unittest.TestCase):

# #     def test_update_row(self):
# #         """Test updating a row's value for a specified column."""
# #         messages = pd.DataFrame(
# #             {"node_id": ["1", "2"], "content": ["message1", "message2"]}
# #         )
# #         success = MessageUtil.update_row(messages, 0, "node_id", "3")
# #         self.assertTrue(success)
# #         self.assertTrue("3" in messages["node_id"].values)


# if __name__ == "__main__":
# 	unittest.main()
