# from lionagi.core.mail.mail_manager import MailManager
# from lionagi.core.mail.schema import BaseMail

# import unittest
# from unittest.mock import patch
# from collections import deque


# class MockSource:
#     def __init__(self):
#         self.pending_outs = deque()
#         self.pending_ins = {}


# class TestMailManager(unittest.TestCase):

#     def setUp(self):
#         self.sources = {"source1": MockSource(), "source2": MockSource()}
#         self.manager = MailManager(sources=self.sources)

#     def test_add_source(self):
#         """Test adding a new source."""
#         self.manager.add_source({"new_source": MockSource()})
#         self.assertIn("new_source", self.manager.sources)

#     def test_add_source_existing_name(self):
#         """Test adding a source with an existing name raises ValueError."""
#         with self.assertRaises(ValueError):
#             self.manager.add_source({"source1": MockSource()})

#     def test_delete_source(self):
#         """Test deleting an existing source."""
#         self.manager.delete_source("source1")
#         self.assertNotIn("source1", self.manager.sources)

#     def test_delete_nonexistent_source(self):
#         """Test deleting a non-existent source raises ValueError."""
#         with self.assertRaises(ValueError):
#             self.manager.delete_source("nonexistent_source")

#     def test_collect_from_nonexistent_sender(self):
#         """Test collecting mail from a non-existent sender raises ValueError."""
#         with self.assertRaises(ValueError):
#             self.manager.collect("nonexistent_sender")

#     def test_send_to_nonexistent_recipient(self):
#         """Test sending mail to a non-existent recipient raises ValueError."""
#         with self.assertRaises(ValueError):
#             self.manager.send("nonexistent_recipient")

#     @patch("lionagi.core.mail.mail_manager.BaseMail")
#     def test_create_mail(self, mock_base_mail):
#         """Test creating mail using the static method."""
#         mail = MailManager.create_mail("sender", "recipient", "messages", "package")
#         mock_base_mail.assert_called_once_with(
#             "sender", "recipient", "messages", "package"
#         )

#     # def test_collect_and_send_mail(self):
#     #     """Test collecting and sending mail between existing sources."""
#     #     # Setup: Simulate pending outs in source1
#     #     mock_mail = BaseMail("source1", "source2", "messages", "package")
#     #     self.sources["source1"].pending_outs.append(mock_mail)

#     #     # Collect mail from source1
#     #     self.manager.collect("source1")
#     #     # Send mail to source2
#     #     self.manager.send("source2")

#     #     # Verify that source2 received the mail
#     #     self.assertTrue(any(self.sources["source2"].pending_ins))


# if __name__ == "__main__":
#     unittest.main()
