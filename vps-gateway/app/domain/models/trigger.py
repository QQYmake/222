"""Trigger 数据模型。

数据合同来源：架构文档 5.8 Trigger。

UserTrigger 由 HTTP Controller 构造，request_id 唯一。
TimerTrigger 由 Scheduler 构造，trigger_id 格式 "timer:{slot_start_iso}" 作为幂等键。
"""
from __future__ import annotations

import dataclasses
from typing import Any, Literal


@dataclasses.dataclass(frozen=True)
class UserTrigger:
    """被动回合触发器。"""

    request_id: str
    chat_request: dict[str, Any]
    type: Literal["user"] = "user"


@dataclasses.dataclass(frozen=True)
class TimerTrigger:
    """主动回合触发器。trigger_id 是幂等键。"""

    trigger_id: str
    fired_at: str
    instruction: str
    type: Literal["timer"] = "timer"


TurnTrigger = UserTrigger | TimerTrigger
