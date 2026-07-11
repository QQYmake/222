"""PreferencesData 领域模型。

数据合同来源：架构文档 5.3 PreferencesSample。
按原始条目读取，不自动推断新偏好。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from app.domain.models.sample import SampleValidationError


@dataclasses.dataclass(frozen=True)
class PreferencesData:
    """用户偏好和长期交互习惯。"""

    communication_preferences: list[str]
    stable_likes: list[str]
    stable_dislikes: list[str]
    interaction_rules: list[str]


def validate_preferences(data: dict[str, Any]) -> PreferencesData:
    """校验并构造 PreferencesData。

    指令:
      1. 按原始条目读取，不自动推断新偏好
      2. 四个字段均为字符串列表，允许空列表
    """
    for field in (
        "communication_preferences",
        "stable_likes",
        "stable_dislikes",
        "interaction_rules",
    ):
        val = data.get(field)
        if not isinstance(val, list):
            raise SampleValidationError(field, "must be a list")
        for item in val:
            if not isinstance(item, str):
                raise SampleValidationError(field, "all items must be strings")

    return PreferencesData(
        communication_preferences=list(data["communication_preferences"]),
        stable_likes=list(data["stable_likes"]),
        stable_dislikes=list(data["stable_dislikes"]),
        interaction_rules=list(data["interaction_rules"]),
    )
