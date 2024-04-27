# from lionagi.core.tool.tool_manager import ToolManager
# from lionagi.core.schema import Tool

# import unittest
# from unittest.mock import patch, AsyncMock
# import asyncio
# import json


# class TestToolManager(unittest.TestCase):
# 	def setUp(self):
# 		self.manager = ToolManager()
# 		self.tool = Tool(
# 			func=lambda x: x, schema_={"function": {"name": "test_func"}}
# 			)

# 	def test_register_and_check_tool(self):
# 		"""Test registering a tool and checking its existence."""
# 		self.manager._register_tool(self.tool)
# 		self.assertTrue(self.manager.name_existed("test_func"))

# 	def test_register_tool_type_error(self):
# 		"""Test that registering a non-Tool object raises a TypeError."""
# 		with self.assertRaises(TypeError):
# 			self.manager._register_tool("not_a_tool")

# 	def test_name_not_existed(self):
# 		"""Test querying a non-registered tool's existence."""
# 		self.assertFalse(self.manager.name_existed("non_existent_func"))


# class TestToolInvocation(unittest.TestCase):

# 	def setUp(self):
# 		self.manager = ToolManager()

# 		async def async_tool_func(x):
# 			return x

# 		self.async_tool = Tool(
# 			func=async_tool_func,
# 			schema_={"function": {"name": "async_test_func"}}
# 		)
# 		self.sync_tool = Tool(
# 			func=lambda x: x, schema_={"function": {"name": "sync_test_func"}}
# 		)

# 	# @patch('lionagi.core.tool.tool_manager', return_value=False)  # def test_invoke_sync_tool(self, mock_is_coroutine):  #     """Test invoking a synchronous tool."""  #     self.manager._register_tool(self.sync_tool)  #     result = asyncio.run(self.manager.invoke(('sync_test_func', {'x': 10})))  #     self.assertEqual(result, 10)

# 	# @patch('lionagi.core.tool.tool_manager', return_value=True)  # def test_invoke_async_tool(self, mock_call_handler, mock_is_coroutine):  #     """Test invoking an asynchronous tool."""  #     mock_call_handler.return_value = 10  #     self.manager._register_tool(self.async_tool)  #     result = asyncio.run(self.manager.invoke(('async_test_func', {'x': 10})))  #     self.assertEqual(result, 10)


# class TestFunctionCallExtraction(unittest.TestCase):

# 	def setUp(self):
# 		self.manager = ToolManager()

# 	def test_get_function_call_valid(self):
# 		"""Test extracting a valid function call."""
# 		response = {
# 			"action": "action_test_func", "arguments": json.dumps({"x": 10})
# 		}
# 		func_call = self.manager.get_function_call(response)
# 		self.assertEqual(func_call, ("test_func", {"x": 10}))

# 	def test_get_function_call_invalid(self):
# 		"""Test handling an invalid function call."""
# 		with self.assertRaises(ValueError):
# 			self.manager.get_function_call({})


# class TestToolParser(unittest.TestCase):

# 	def setUp(self):
# 		self.manager = ToolManager()
# 		self.tool = Tool(
# 			func=lambda x: x, schema_={"function": {"name": "test_func"}}
# 			)
# 		self.manager._register_tool(self.tool)

# 	def test_tool_parser_single_tool(self):
# 		"""Test parsing a single tool name."""
# 		parsed = self.manager.parse_tool("test_func")
# 		self.assertIn("tools", parsed)
# 		self.assertEqual(len(parsed["tools"]), 1)
# 		self.assertEqual(parsed["tools"][0]["function"]["name"], "test_func")

# 	def test_tool_parser_unregistered_tool(self):
# 		"""Test parsing an unregistered tool name."""
# 		with self.assertRaises(ValueError):
# 			self.manager.parse_tool("unregistered_func")


# if __name__ == "__main__":
# 	unittest.main()
