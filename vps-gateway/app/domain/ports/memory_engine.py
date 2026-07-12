"""MemoryPort 端口：记忆引擎抽象接口。

数据合同来源：V3 架构文档 6.1 MemoryPort。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.models.turn import ChatMessage
    from app.domain.models.trigger import TurnTrigger
    from app.domain.models.chat_completion import ChatCompletionResponse


@dataclass
class MemoryRecall:
    """recall() 返回的数据合同。

    mode: recall 路径 — query | no_query | new_window | degraded
    text: 注入 system message 的文本（已润色）
    source_recall_ids: 关联的 @d 缓存条目 ID 列表
    """

    mode: str = "degraded"
    text: str = ""
    source_recall_ids: list[int] = field(default_factory=list)


@dataclass
class MemoryEngineConfig:
    """MemoryEngine 配置。"""

    db_path: str = ":memory:"
    retrieval_timeout: float = 20.0
    surface_interval: float = 3600.0
    consolidation_hour: int = 2
    enabled: bool = True
    embed_type: str = "local"
    embed_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    # 8 模型配置（base_url / api_key / model）
    intent_model_config: dict | None = None
    gen_model_config: dict | None = None
    surf_model_config: dict | None = None
    extract_model_config: dict | None = None
    persona_model_config: dict | None = None
    saga_model_config: dict | None = None
    polish_model_config: dict | None = None
    embed_model_config: dict | None = None


class MemoryPort(abc.ABC):
    """记忆引擎端口。

    TurnRunner 只通过此端口与记忆引擎交互。
    """

    @abc.abstractmethod
    async def recall(
        self, trigger: "TurnTrigger", raw_messages: list["ChatMessage"]
    ) -> MemoryRecall:
        """回合开始前调用。返回记忆注入内容。"""
        ...

    @abc.abstractmethod
    async def after_turn(
        self,
        raw_messages: list["ChatMessage"],
        response: "ChatCompletionResponse",
        turn_id: str,
        trigger: "TurnTrigger | None" = None,
    ) -> None:
        """回合结束后调用。追加 @a 原料。"""
        ...

    @abc.abstractmethod
    async def start_background_tasks(self) -> None:
        """启动 @e 周期生成器和 2am 沉淀定时器。"""
        ...

    @abc.abstractmethod
    async def stop_background_tasks(self) -> None:
        """停止后台任务。"""
        ...

    @abc.abstractmethod
    async def recall_as_tool(self, query: str) -> str:
        """memory_recall 工具调用入口。触发 @4 流程，返回润色后的 @d 内容。"""
        ...
