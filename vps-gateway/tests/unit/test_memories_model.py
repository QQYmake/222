"""MemoriesData 模型测试。"""
import pytest
from app.domain.models.sample import SampleValidationError
from app.domain.models.memories import MemoriesData, validate_memories


class TestValidateMemories:
    def _valid_item(self, id="mem_001", priority=10):
        return {
            "id": id,
            "content": "记忆内容",
            "category": "category_a",
            "priority": priority,
            "created_at": "2025-01-01T00:00:00+08:00",
        }

    def _valid_data(self):
        return {"items": [self._valid_item()]}

    def test_valid_memories(self):
        data = self._valid_data()
        result = validate_memories(data)
        assert len(result.items) == 1
        assert result.items[0].id == "mem_001"

    def test_empty_items_allowed(self):
        data = {"items": []}
        result = validate_memories(data)
        assert result.items == []

    def test_missing_items(self):
        data = {}
        with pytest.raises(SampleValidationError, match="items"):
            validate_memories(data)

    def test_items_not_list(self):
        data = {"items": "not a list"}
        with pytest.raises(SampleValidationError, match="items"):
            validate_memories(data)

    def test_duplicate_id(self):
        data = {
            "items": [
                self._valid_item(id="mem_001"),
                self._valid_item(id="mem_001"),
            ]
        }
        with pytest.raises(SampleValidationError, match="duplicate"):
            validate_memories(data)

    def test_missing_content(self):
        item = self._valid_item()
        item["content"] = ""
        data = {"items": [item]}
        with pytest.raises(SampleValidationError, match="content"):
            validate_memories(data)

    def test_missing_category(self):
        item = self._valid_item()
        item["category"] = ""
        data = {"items": [item]}
        with pytest.raises(SampleValidationError, match="category"):
            validate_memories(data)

    def test_missing_priority(self):
        item = self._valid_item()
        del item["priority"]
        data = {"items": [item]}
        with pytest.raises(SampleValidationError, match="priority"):
            validate_memories(data)

    def test_invalid_created_at(self):
        item = self._valid_item()
        item["created_at"] = "bad-date"
        data = {"items": [item]}
        with pytest.raises(SampleValidationError, match="created_at"):
            validate_memories(data)

    def test_immutable(self):
        data = self._valid_data()
        result = validate_memories(data)
        with pytest.raises((AttributeError, TypeError)):
            result.items = []
