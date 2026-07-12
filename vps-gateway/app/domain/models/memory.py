"""记忆领域模型：MemoryRecall, MemorySurface, IntentResult, RecallEntry, SurfaceEntry。

数据合同来源：V3 架构文档第 5 节。
"""
from __future__ import annotations

import dataclasses
from typing import Optional, Any


@dataclasses.dataclass(frozen=True)
class MemoryRecall:
    """记忆召回结果。

    数据合同来源：架构文档 5.1 MemoryRecall。
    """
    mode: str  # "query" | "no_query" | "new_window" | "degraded"
    text: str  # 润色后的记忆文本；degraded 时为空字符串
    source_recall_ids: list[int]  # 关联的 @d 缓存条目 ID 列表
    metadata: dict[str, Any]  # intent/confidence/retrieval_tracks/scorer_stats/timeout

    def __post_init__(self):
        if self.mode == "degraded" and self.text:
            raise ValueError("degraded mode text must be empty string")
        if self.mode == "no_query" and self.source_recall_ids:
            raise ValueError("no_query mode source_recall_ids must be empty")


@dataclasses.dataclass(frozen=True)
class MemorySurface:
    """@e 浮现条目。

    数据合同来源：架构文档 5.2 MemorySurface。
    """
    id: int
    content: str
    raw_content: str
    surface_type: str  # "association" | "impression"
    source_recall_ids: list[int]
    created_at: str

    def __post_init__(self):
        if self.surface_type not in ("association", "impression"):
            raise ValueError(f"surface_type must be association or impression, got: {self.surface_type}")
        if not self.source_recall_ids:
            raise ValueError("source_recall_ids must have at least 1 element")


@dataclasses.dataclass(frozen=True)
class IntentResult:
    """意图分类结果。

    数据合同来源：架构文档 5.3 IntentResult。
    """
    label: str  # "query" | "no_query"
    confidence: float  # 0.0-1.0
    matched_patterns: list[str]
    source: str  # "rule" | "llm_fallback" | "calibration"
    intent_type: Optional[str] = None  # "fact" | "summary" | "long_term" | "semantic" (仅 query 时)

    def __post_init__(self):
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got: {self.confidence}")
        if self.source == "rule" and self.label == "query" and not self.matched_patterns:
            raise ValueError("rule source with query label must have non-empty matched_patterns")
        if self.label == "no_query" and self.intent_type is not None:
            raise ValueError("no_query label must have intent_type=None")


@dataclasses.dataclass(frozen=True)
class RecallEntry:
    """@d 缓冲条目。"""
    id: int
    trigger_id: str
    content: str  # 润色后的最终文本
    raw_content: str  # 未润色原始内容
    metadata: Optional[dict[str, Any]]
    created_at: str
    read_at: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class SurfaceEntry:
    """@e 浮现条目（读取时返回）。"""
    id: int
    content: str
    raw_content: str
    surface_type: str
    source_recall_ids: list[int]
    created_at: str
