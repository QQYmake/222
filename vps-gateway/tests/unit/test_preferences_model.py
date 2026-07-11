"""PreferencesData 模型测试。"""
import pytest
from app.domain.models.sample import SampleValidationError
from app.domain.models.preferences import PreferencesData, validate_preferences


class TestValidatePreferences:
    def _valid_data(self):
        return {
            "communication_preferences": ["偏好1"],
            "stable_likes": ["喜欢1"],
            "stable_dislikes": ["不喜欢1"],
            "interaction_rules": ["规则1"],
        }

    def test_valid_preferences(self):
        data = self._valid_data()
        result = validate_preferences(data)
        assert result.communication_preferences == ["偏好1"]
        assert result.stable_likes == ["喜欢1"]

    def test_communication_preferences_not_list(self):
        data = self._valid_data()
        data["communication_preferences"] = "not a list"
        with pytest.raises(SampleValidationError, match="communication_preferences"):
            validate_preferences(data)

    def test_stable_likes_not_list(self):
        data = self._valid_data()
        data["stable_likes"] = 42
        with pytest.raises(SampleValidationError, match="stable_likes"):
            validate_preferences(data)

    def test_stable_dislikes_not_list(self):
        data = self._valid_data()
        data["stable_dislikes"] = None
        with pytest.raises(SampleValidationError, match="stable_dislikes"):
            validate_preferences(data)

    def test_interaction_rules_not_list(self):
        data = self._valid_data()
        data["interaction_rules"] = "rule"
        with pytest.raises(SampleValidationError, match="interaction_rules"):
            validate_preferences(data)

    def test_empty_lists_allowed(self):
        data = {
            "communication_preferences": [],
            "stable_likes": [],
            "stable_dislikes": [],
            "interaction_rules": [],
        }
        result = validate_preferences(data)
        assert result.communication_preferences == []

    def test_immutable(self):
        data = self._valid_data()
        result = validate_preferences(data)
        with pytest.raises((AttributeError, TypeError)):
            result.stable_likes = []
