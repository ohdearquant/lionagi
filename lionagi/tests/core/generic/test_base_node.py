import unittest
from pydantic import BaseModel
from pandas import Series
import pandas as pd
from lionagi.core.generic.node import BaseNode

class TestBaseNode(unittest.TestCase):

    def test_basic_properties(self):
        node = BaseNode(content="Hello World")
        self.assertIsInstance(node.id_, str)
        self.assertEqual(len(node.id_), 32)
        self.assertEqual(node.content, "Hello World")
        self.assertEqual(node.metadata, {})

    def test_serialization_json(self):
        node = BaseNode(content="Hello World")
        self.assertIn('id_', node.to_json_str())

    def test_serialization_dict(self):
        node = BaseNode(content="Hello World")
        result = node.to_dict()
        self.assertIn('id_', result)
        self.assertEqual(result['content'], "Hello World")

    def test_serialization_xml(self):
        node = BaseNode(content="Hello World")
        xml_str = node.to_xml()
        self.assertIn('<BaseNode>', xml_str)
        self.assertIn('</BaseNode>', xml_str)

    def test_conversion_to_pandas_series(self):
        node = BaseNode(content="Hello World")
        series = node.to_pd_series()
        self.assertIsInstance(series, Series)
        self.assertEqual(series['content'], "Hello World")

    def test_field_management(self):
        node = BaseNode(content="Hello World")
        node._add_field("new_field", annotation=str, default="default value")
        self.assertIn('new_field', node._field_annotations)
        self.assertEqual(getattr(node, 'new_field', None), "default value")

    def test_creation_from_dict(self):
        data = {"a": 1, "b": 2}
        node = BaseNode.from_obj(data)
        self.assertIn('a', node.to_dict())

    def test_creation_from_json_str(self):
        json_str = '{"a": 1, "b": 2}'
        node = BaseNode.from_obj(json_str)
        self.assertIn('a', node.to_dict())

    def test_creation_from_fuzzy_json_str(self):
        json_str = '{"name": "John", "age": 30, "city": ["New York", "DC", "LA"]'
        node = BaseNode.from_obj(json_str, fuzzy_parse=True)
        self.assertIn('name', node.to_dict())

    def test_creation_from_pandas_series(self):
        series = pd.Series({"a": 1, "b": 2})
        node = BaseNode.from_obj(series)
        self.assertIn('a', node.to_dict())

    def test_creation_from_pandas_dataframe(self):
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]}, index=["row1", "row2"])
        nodes = BaseNode.from_obj(df)
        self.assertIsInstance(nodes, list)
        self.assertEqual(len(nodes), 2)

    def test_creation_from_pydantic_model(self):
        class CustomModel(BaseModel):
            a: int
            b: str

        custom_model = CustomModel(a=1, b="hello")
        node = BaseNode.from_obj(custom_model)
        self.assertIn('a', node.to_dict())

    def test_metadata_manipulation(self):
        node = BaseNode()
        node.meta_insert("key", "value")
        self.assertEqual(node.metadata['key'], "value")
        node.meta_change_key("key", "new_key")
        self.assertIn("new_key", node.metadata)
        node.meta_merge({"additional_key": "additional_value"})
        self.assertIn("additional_key", node.metadata)

if __name__ == '__main__':
    unittest.main()
