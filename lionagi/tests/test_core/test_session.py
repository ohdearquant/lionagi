# from lionagi.core.branch.branch import Branch
# from lionagi.core.session.session import Session

# import unittest
# from unittest.mock import patch, call, MagicMock
# import pandas as pd
# import json
# from datetime import datetime


# class TestSession(unittest.TestCase):

# 	def setUp(self):
# 		mock_branch = MagicMock()

# 		mock_branch.to_csv_file = MagicMock()
# 		mock_branch.to_json_file = MagicMock()

# 		mock_datalogger = MagicMock()
# 		mock_datalogger.to_csv_file = MagicMock()
# 		mock_datalogger.to_json_file = MagicMock()

# 		# Assign the mocked datalogger to the mock_branch
# 		mock_branch.datalogger = mock_datalogger

# 		self.branch1 = mock_branch(name="branch1")
# 		self.branch1.messages = pd.DataFrame(
# 			[{
# 				"node_id": "1", "timestamp": "2021-01-01 00:00:00",
# 				"role": "system", "sender": "system",
# 				"content": json.dumps({"system_info": "System message"}),
# 			}]
# 		)
# 		self.branch2 = mock_branch(name="branch2")
# 		self.branch1.messages = pd.DataFrame(
# 			[{
# 				"node_id": "2", "timestamp": "2021-01-01 00:01:00",
# 				"role": "user", "sender": "user1",
# 				"content": json.dumps({"instruction": "User message"}),
# 			}]
# 		)

# 		branches = {"branch1": self.branch1, "branch2": self.branch2}

# 		self.session = Session(
# 			branches=branches, default_branch_name="branch1",
# 			default_branch=branches["branch1"], )
# 		self.session.mail_manager = MagicMock()

# 	# def test_from_csv_initialization(self):
# 	#     """Test Session initialization from a CSV file."""
# 	#     mock_df = pd.DataFrame(
# 	#         {
# 	#             "node_id": ["1", "2"],
# 	#             "timestamp": [datetime(2021, 1, 1), datetime(2021, 1, 1)],
# 	#             "role": ["system", "user"],
# 	#             "sender": ["system", "user1"],
# 	#             "content": [
# 	#                 json.dumps({"system_info": "System message"}),
# 	#                 json.dumps({"instruction": "User message"}),
# 	#             ],
# 	#         }
# 	#     )
# 	#     filepath = "path/to/mock.csv"

# 	#     with patch("pandas.read_csv", return_value=mock_df) as mock_read_csv:
# 	#         session = Session.from_csv(filepath)
# 	#         mock_read_csv.assert_called_once_with(filepath)
# 	#         pd.testing.assert_frame_equal(session.messages, mock_df)

# 	# def test_from_json_initialization(self):
# 	#     """Test Session initialization from a CSV file."""
# 	#     mock_df = pd.DataFrame(
# 	#         {
# 	#             "node_id": ["1", "2"],
# 	#             "timestamp": [datetime(2021, 1, 1), datetime(2021, 1, 1)],
# 	#             "role": ["system", "user"],
# 	#             "sender": ["system", "user1"],
# 	#             "content": [
# 	#                 json.dumps({"system_info": "System message"}),
# 	#                 json.dumps({"instruction": "User message"}),
# 	#             ],
# 	#         }
# 	#     )
# 	#     filepath = "path/to/mock.json"

# 	#     with patch("pandas.read_json", return_value=mock_df) as mock_read_json:
# 	#         session = Session.from_json(filepath)
# 	#         mock_read_json.assert_called_once_with(filepath)
# 	#         pd.testing.assert_frame_equal(session.messages, mock_df)

# 	def test_to_csv_file(self):
# 		"""Ensure to_csv_file calls each branch's to_csv_file method."""
# 		filename = "test_export.csv"
# 		self.session.to_csv_file(filename=filename)

# 		for name, branch in self.session.branches.items():
# 			# Verify it was called twice
# 			self.assertEqual(branch.to_csv_file.call_count, 2)

# 			# Verify the arguments of the last call
# 			expected_filename = f"{name}_{filename}"
# 			self.assertIn(
# 				expected_filename,
# 				["branch1_test_export.csv", "branch2_test_export.csv"], )

# 	def test_to_json_file(self):
# 		"""Ensure to_json_file calls each branch's to_json_file method."""
# 		filename = "test_export.json"
# 		self.session.to_json_file(filename=filename)

# 		for name, branch in self.session.branches.items():
# 			# Verify it was called twice
# 			self.assertEqual(branch.to_json_file.call_count, 2)

# 			# Verify the arguments of the last call
# 			expected_filename = f"{name}_{filename}"
# 			self.assertIn(
# 				expected_filename,
# 				["branch1_test_export.json", "branch2_test_export.json"], )

# 	def test_log_to_csv(self):
# 		"""Ensure log_to_csv calls each branch's log_to_csv method."""
# 		filename = "test_export.csv"
# 		self.session.log_to_csv(filename=filename)

# 		for name, branch in self.session.branches.items():
# 			# Verify it was called twice
# 			self.assertEqual(branch.log_to_csv.call_count, 2)

# 			# Verify the arguments of the last call
# 			expected_filename = f"{name}_{filename}"
# 			self.assertIn(
# 				expected_filename,
# 				["branch1_test_export.csv", "branch2_test_export.csv"], )

# 	def test_log_to_json(self):
# 		"""Ensure log_to_json calls each branch's log_to_json method."""
# 		filename = "test_export.json"
# 		self.session.log_to_json(filename=filename)

# 		for name, branch in self.session.branches.items():
# 			# Verify it was called twice
# 			self.assertEqual(branch.log_to_json.call_count, 2)

# 			# Verify the arguments of the last call
# 			expected_filename = f"{name}_{filename}"
# 			self.assertIn(
# 				expected_filename,
# 				["branch1_test_export.json", "branch2_test_export.json"], )

# 	def test_all_messages(self):
# 		"""Test aggregation of all messages across branches."""
# 		expected_df = pd.concat(
# 			[self.branch1.messages, self.branch2.messages], ignore_index=True
# 		)

# 		actual_df = self.session.all_messages

# 		pd.testing.assert_frame_equal(actual_df, expected_df)

# 	def test_new_branch_creation(self):
# 		"""Test creating a new branch successfully."""
# 		branch_name = "test_branch"
# 		self.session.new_branch(branch_name=branch_name)
# 		self.assertIn(branch_name, self.session.branches)

# 	def test_new_branch_duplicate_name(self):
# 		"""Test error handling for duplicate branch names."""
# 		branch_name = "test_branch"
# 		self.session.new_branch(branch_name=branch_name)
# 		with self.assertRaises(ValueError):
# 			self.session.new_branch(branch_name=branch_name)

# 	def test_get_branch_by_name(self):
# 		"""Test retrieving a branch by its name."""
# 		branch_name = "test_branch"
# 		self.session.new_branch(branch_name=branch_name)
# 		branch = self.session.get_branch(branch_name)
# 		self.assertIsInstance(branch, Branch)

# 	def test_get_branch_invalid_name(self):
# 		"""Test error handling for invalid branch names."""
# 		with self.assertRaises(ValueError):
# 			self.session.get_branch("nonexistent_branch")

# 	def test_change_default_branch(self):
# 		"""Test changing the default branch."""
# 		branch_name = "new_default"
# 		self.session.new_branch(branch_name=branch_name)
# 		self.session.change_default_branch(branch_name)
# 		self.assertEqual(self.session.default_branch_name, branch_name)

# 	def test_delete_branch(self):
# 		"""Test deleting a branch."""
# 		branch_name = "test_branch"
# 		self.session.new_branch(branch_name=branch_name)
# 		self.session.delete_branch(branch_name)
# 		self.assertNotIn(branch_name, self.session.branches)

# 	def test_delete_default_branch_error(self):
# 		"""Test error when trying to delete the default branch."""
# 		with self.assertRaises(ValueError):
# 			self.session.delete_branch(self.session.default_branch_name)

# 	def test_merge_branch(self):
# 		"""Test merging two branches."""
# 		from_branch = "source_branch"
# 		to_branch = "target_branch"
# 		self.session.new_branch(branch_name=from_branch)
# 		self.session.new_branch(branch_name=to_branch)
# 		self.session.merge_branch(
# 			from_=from_branch, to_branch=to_branch, del_=True
# 			)
# 		self.assertIn(to_branch, self.session.branches)
# 		self.assertNotIn(from_branch, self.session.branches)

# 	def test_collect_from_specified_branches(self):
# 		"""Test collecting requests from specified branches."""
# 		self.session.collect(from_=["branch1"])
# 		self.assertEqual(self.session.mail_manager.collect.call_count, 1)

# 	def test_collect_from_all_branches(self):
# 		"""Test collecting requests from all branches."""
# 		self.session.collect()
# 		self.assertEqual(self.session.mail_manager.collect.call_count, 2)

# 	def test_send_to_specified_branches(self):
# 		"""Test sending requests to specified branches."""
# 		self.session.send(to_=["branch_1"])
# 		self.assertEqual(self.session.mail_manager.send.call_count, 1)

# 	def test_send_to_all_branches(self):
# 		"""Test sending requests to all branches."""
# 		self.session.send()
# 		self.assertEqual(self.session.mail_manager.send.call_count, 2)

# 	def test_collect_send_all_without_receive_all(self):
# 		"""Test collecting and sending requests across all branches without receiving."""
# 		self.session.collect_send_all()
# 		self.assertEqual(self.session.mail_manager.collect.call_count, 2)
# 		self.assertEqual(self.session.mail_manager.send.call_count, 2)
# 		self.branch1.receive_all.assert_not_called()
# 		self.branch2.receive_all.assert_not_called()

# 	def test_collect_send_all_with_receive_all(self):
# 		"""Test collecting and sending requests across all branches with receiving."""
# 		self.session.collect_send_all(receive_all=True)
# 		self.branch1.receive_all.assert_called()
# 		self.branch2.receive_all.assert_called()


# if __name__ == "__main__":
# 	unittest.main()
