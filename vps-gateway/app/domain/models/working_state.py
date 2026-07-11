"""WorkingStateData 领域模型。

数据合同来源：架构文档 5.5 WorkingStateSample。
首版只读，不根据模型输出自动修改。
"""
from __future__ import annotations

import dataclasses
from typing import Any, Optional

from app.domain.models.sample import SampleValidationError, is_valid_iso8601


@dataclasses.dataclass(frozen=True)
class WorkingStateData:
    """当前关注、情绪、未完成事项和建议唤醒时间。"""

    current_focus: list[str]
    emotion_summary: str
    pending_items: list[str]
    next_wake_at: Optional[str]


def validate_working_state(data: dict[str, Any]) -> WorkingStateData:
    """校验并构造 WorkingStateData。

    指令:
      1. 校验 current_focus 为字符串列表
      2. 校验 emotion_summary 为字符串 (允许空字符串)
      3. 校验 pending_items 为字符串列表
      4. next_wake_at: null 合法；非 null 时必须为合法 ISO 8601
      5. 无值时由固定 heartbeat 兜底 (M5 实现，M1 只读取)
    """
    for field in ("current_focus", "pending_items"):
        val = data.get(field)
        if not isinstance(val, list):
            raise SampleValidationError(field, "must be a list")
        for item in val:
            if not isinstance(item, str):
                raise SampleValidationError(field, "all items must be strings")

    emotion = data.get("emotion_summary", "")
    if not isinstance(emotion, str):
        raise SampleValidationError("emotion_summary", "must be a string")

    next_wake = data.get("next_wake_at")
    if next_wake is not None:
        if not is_valid_iso8601(next_wake):
            raise SampleValidationError(
                "next_wake_at", "must be valid ISO 8601 or null"
            )

    return WorkingStateData(
        current_focus=list(data.get("current_focus", [])),
        emotion_summary=emotion,
        pending_items=list(data.get("pending_items", [])),
        next_wake_at=next_wake,
    )
