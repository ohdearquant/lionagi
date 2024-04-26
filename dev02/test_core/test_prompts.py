# from to_do.base_prompt_field import *
# from pydantic import ValidationError

# import unittest


# class TestPromptField(unittest.TestCase):

#     def test_numeric_field_valid(self):
#         try:
#             BasePromptField(name="Age", default="30", field_type=FieldType.NUMERIC)
#             self.fail(
#                 "Expected ValidationError not raised for NUMERIC field with non-numeric default value."
#             )
#         except ValidationError:
#             pass  # Expected

#     def test_boolean_field_valid(self):
#         try:
#             BasePromptField(
#                 name="Is Adult", default="True", field_type=FieldType.BOOLEAN
#             )
#             self.fail(
#                 "Expected ValidationError not raised for BOOLEAN field with non-boolean default value."
#             )
#         except ValidationError:
#             pass  # Expected

#     def test_string_field_invalid(self):
#         try:
#             BasePromptField(name="Name", default=123, field_type=FieldType.STRING)
#             self.fail(
#                 "Expected ValidationError not raised for STRING field with non-string default value."
#             )
#         except ValidationError:
#             pass  # Expected

#     def test_datetime_field_invalid(self):
#         try:
#             BasePromptField(name="Date", default=123, field_type=FieldType.DATETIME)
#             self.fail(
#                 "Expected ValidationError not raised for DATETIME field with non-datetime-compatible default value."
#             )
#         except ValidationError:
#             pass  # Expected


# if __name__ == "__main__":
#     # unittest.main()
#     # Running the adjusted tests
#     unittest.main(argv=[""], exit=False)
