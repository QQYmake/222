"""SampleReader 端口接口与 AllSamples。

数据合同来源：架构文档 6.1 FileSampleRepository / SampleReader 接口。

依赖方向：HTTP/Scheduler 适配器 → 应用层 TurnRunner → 领域接口 (此处)
→ 本地文件等基础设施实现 (FileSampleRepository)。
"""
from __future__ import annotations

import abc
import dataclasses
from typing import Any, Optional, TypeVar

from app.domain.models.sample import SampleEnvelope, SampleType

T = TypeVar("T")

# --- 降级用的空数据 ---

EMPTY_PREFERENCES: dict[str, Any] = {
    "communication_preferences": [],
    "stable_likes": [],
    "stable_dislikes": [],
    "interaction_rules": [],
}

EMPTY_MEMORIES: dict[str, Any] = {
    "items": [],
}

EMPTY_WORKING_STATE: dict[str, Any] = {
    "current_focus": [],
    "emotion_summary": "",
    "pending_items": [],
    "next_wake_at": None,
}


@dataclasses.dataclass(frozen=True)
class AllSamples:
    """四份 Sample 的聚合容器。"""

    identity: SampleEnvelope
    preferences: SampleEnvelope
    memories: SampleEnvelope
    working_state: SampleEnvelope


class SampleReader(abc.ABC):
    """Sample 读取端口接口。

    职责：从本地 JSON 文件读取四份稳定 Sample；
    不计算状态、不调用模型、不修改文件。

    指令:
      1. 根据固定映射选择文件，禁止由外部请求直接传入任意路径
      2. 读取完整文件
      3. 解析 JSON
      4. 执行类型和字段校验
      5. 返回不可变对象
    """

    @abc.abstractmethod
    def read(self, sample_type: SampleType) -> SampleEnvelope:
        """读取单份 Sample。失败时抛 SampleReadError。"""

    @abc.abstractmethod
    def read_all(self) -> AllSamples:
        """读取全部四份 Sample。

        identity 失败时抛 SampleReadError (不降级)。
        其余三类失败时降级为空值。
        """
