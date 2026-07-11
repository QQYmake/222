"""WorkingStateData 模型测试。"""
import pytest
from app.domain.models.sample import SampleValidationError
from app.domain.models.working_state import WorkingStateData, validate_working_state


class TestValidateWorkingState:
    def _valid_data(self):
        return {
            "current_focus": ["当前关注1"],
            "emotion_summary": "情绪概述",
            "pending_items": ["待办1"],
            "next_wake_at": None,
        }

    def test_valid_working_state(self):
        data = self._valid_data()
        result = validate_working_state(data)
        assert result.current_focus == ["当前关注1"]
        assert result.emotion_summary == "情绪概述"

    def test_next_wake_at_null(self):
        data = self._valid_data()
        result = validate_working_state(data)
        assert result.next_wake_at is None

    def test_next_wake_at_valid_iso(self):
        data = self._valid_data()
        data["next_wake_at"] = "2025-06-01T12:00:00+08:00"
        result = validate_working_state(data)
        assert result.next_wake_at == "2025-06-01T12:00:00+08:00"

    def test_next_wake_at_invalid(self):
        data = self._valid_data()
        data["next_wake_at"] = "bad"
        with pytest.raises(SampleValidationError, match="next_wake_at"):
            validate_working_state(data)

    def test_current_focus_not_list(self):
        data = self._valid_data()
        data["current_focus"] = "not a list"
        with pytest.raises(SampleValidationError, match="current_focus"):
            validate_working_state(data)

    def test_pending_items_not_list(self):
        data = self._valid_data()
        data["pending_items"] = 123
        with pytest.raises(SampleValidationError, match="pending_items"):
            validate_working_state(data)

    def test_empty_focus_allowed(self):
        data = self._valid_data()
        data["current_focus"] = []
        result = validate_working_state(data)
        assert result.current_focus == []

    def test_immutable(self):
        data = self._valid_data()
        result = validate_working_state(data)
        with pytest.raises((AttributeError, TypeError)):
            result.emotion_summary = "changed"
