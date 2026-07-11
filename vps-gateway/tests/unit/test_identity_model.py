"""IdentityData 模型测试。"""
import pytest
from app.domain.models.sample import SampleValidationError
from app.domain.models.identity import IdentityData, validate_identity


class TestValidateIdentity:
    def _valid_data(self):
        return {
            "name": "沉",
            "self_description": "一段自我描述",
            "values": ["价值1", "价值2"],
            "boundaries": ["边界1"],
            "relationship_definition": "与用户的关系定义",
        }

    def test_valid_identity(self):
        data = self._valid_data()
        result = validate_identity(data)
        assert result.name == "沉"
        assert result.values == ["价值1", "价值2"]

    def test_missing_name(self):
        data = self._valid_data()
        data["name"] = ""
        with pytest.raises(SampleValidationError, match="name"):
            validate_identity(data)

    def test_missing_self_description(self):
        data = self._valid_data()
        data["self_description"] = ""
        with pytest.raises(SampleValidationError, match="self_description"):
            validate_identity(data)

    def test_missing_relationship_definition(self):
        data = self._valid_data()
        data["relationship_definition"] = ""
        with pytest.raises(SampleValidationError, match="relationship_definition"):
            validate_identity(data)

    def test_values_not_list(self):
        data = self._valid_data()
        data["values"] = "not a list"
        with pytest.raises(SampleValidationError, match="values"):
            validate_identity(data)

    def test_boundaries_not_list(self):
        data = self._valid_data()
        data["boundaries"] = 123
        with pytest.raises(SampleValidationError, match="boundaries"):
            validate_identity(data)

    def test_values_contains_non_string(self):
        data = self._valid_data()
        data["values"] = ["ok", 123]
        with pytest.raises(SampleValidationError, match="values"):
            validate_identity(data)

    def test_empty_values_list_allowed(self):
        data = self._valid_data()
        data["values"] = []
        result = validate_identity(data)
        assert result.values == []

    def test_immutable(self):
        data = self._valid_data()
        result = validate_identity(data)
        with pytest.raises((AttributeError, TypeError)):
            result.name = "modified"
