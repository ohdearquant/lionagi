from lionagi.operations.fields import Reason


class TestValidateConfidenceScore:
    def test_valid_numeric_inputs(self):
        test_cases = [
            (0.5, 0.5),  # Direct float
            (1, 1.0),  # Integer to float
            ("0.75", 0.75),  # String to float
            (0.8888, 0.889),  # Precision handling
            (0, 0.0),  # Lower bound
            (1, 1.0),  # Upper bound
        ]
        for input_val, expected in test_cases:
            assert Reason._validate_confidence(input_val) == expected

    def test_invalid_inputs(self):
        invalid_inputs = [
            "invalid",  # Non-numeric string
            {},  # Dict
            [],  # List
            -0.5,  # Below lower bound
            1.5,  # Above upper bound
        ]
        for input_val in invalid_inputs:
            assert Reason._validate_confidence(input_val) == -1

        # None is a valid value (remains None)
        assert Reason._validate_confidence(None) is None

    def test_precision_handling(self):
        test_cases = [
            (0.12345, 0.123),
            (0.9999, 1.0),
            (0.8888888, 0.889),
            (0.001001, 0.001),
        ]
        for input_val, expected in test_cases:
            assert Reason._validate_confidence(input_val) == expected


class TestReason:
    def test_default_values(self):
        model = Reason()
        assert model.title is None
        assert model.content is None
        assert model.confidence_score is None

    def test_valid_values(self):
        data = {
            "title": "Test Reason",
            "content": "This is a test reason content",
            "confidence_score": 0.85,
        }
        model = Reason(**data)
        assert model.title == data["title"]
        assert model.content == data["content"]
        assert model.confidence_score == data["confidence_score"]

    def test_confidence_score_validation(self):
        # Valid cases
        valid_scores = [0.5, 1.0, 0.0, "0.75"]
        for score in valid_scores:
            model = Reason(confidence_score=score)
            assert isinstance(model.confidence_score, float)
            assert 0 <= model.confidence_score <= 1

        # Invalid cases
        invalid_scores = [-1, 1.5, "invalid", None]
        for score in invalid_scores:
            if score is not None:  # None is allowed as it's the default
                model = Reason(confidence_score=score)
                assert model.confidence_score == -1

    def test_partial_initialization(self):
        # Only title
        model = Reason(title="Test")
        assert model.title == "Test"
        assert model.content is None
        assert model.confidence_score is None

        # Only content
        model = Reason(content="Test content")
        assert model.title is None
        assert model.content == "Test content"
        assert model.confidence_score is None

        # Only confidence_score
        model = Reason(confidence_score=0.75)
        assert model.title is None
        assert model.content is None
        assert model.confidence_score == 0.75
