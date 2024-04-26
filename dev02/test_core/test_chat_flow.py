# from lionagi.core.flow.monoflow import
# from lionagi.core.session.branch import Branch
# from lionagi.core.action.libs import func_to_tool

# import unittest
# from unittest import IsolatedAsyncioTestCase
# from unittest.mock import patch, MagicMock, AsyncMock


# class TestChatFlowCallChatCompletion(IsolatedAsyncioTestCase):
#     async def asyncSetUp(self):
#         self.branch = MagicMock()
#         self.branch.service = MagicMock()
#         self.branch.datalogger = MagicMock()
#         self.branch.status_tracker = MagicMock(num_tasks_succeeded=0, num_tasks_failed=0)
#         self.branch.add_message = MagicMock()

#         # Mock chat completion service response
#         self.branch.service.serve_chat = AsyncMock(return_value=({}, {"choices": [{'messages':{"content": "test response"}}]}))

#     async def test_call_chatcompletion(self):
#         await ChatFlow.call_chatcompletion(self.branch, sender="user")

#         # Verify serve_chat was called with expected arguments
#         self.branch.service.serve_chat.assert_called_once()

#         # Verify the branch's datalogger and add_message were called appropriately
#         self.branch.datalogger.append.assert_called_once()
#         self.branch.add_message.assert_called_with(response={'messages': {'content': 'test response'}}, sender="user")

#         # Verify status tracker update
#         self.assertEqual(self.branch.status_tracker.num_tasks_succeeded, 1)
#         self.assertEqual(self.branch.status_tracker.num_tasks_failed, 0)


# class TestChatFlowChat(IsolatedAsyncioTestCase):

#     async def asyncSetUp(self):
#         self.branch = MagicMock()
#         self.branch.add_message = MagicMock()
#         self.branch.change_first_system_message = MagicMock()
#         self.branch.service = AsyncMock()
#         self.branch.datalogger = MagicMock()
#         self.branch.call_chatcompletion = AsyncMock()
#         self.branch.tool_manager.invoke = AsyncMock()

#         self.branch.service.serve_chat = AsyncMock(
#             return_value=({}, {"choices": [{'messages': {"content": "test response"}}]}))

#     async def test_chat_with_system_and_instruction(self):
#         instruction = "Ask about user preferences"
#         system = "System ready"
#         sender = "user"

#         await ChatFlow.chat(self.branch, instruction=instruction, sender=sender)

#         self.branch.add_message.assert_called_with(instruction=instruction, context=None, sender=sender)

#         self.branch.call_chatcompletion.assert_called_once()


# if __name__ == '__main__':
#     unittest.main()
