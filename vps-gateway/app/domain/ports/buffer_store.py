"""BufferStore 端口：@a/@d/@e 缓冲存储抽象接口。

数据合同来源：V3 架构文档 6.3 BufferStore。
"""
from __future__ import annotations

import abc
from typing import Optional
from app.domain.models.memory import RecallEntry, SurfaceEntry


class BufferStore(abc.ABC):
    """缓冲存储端口。

    @a (buffer_raw): 跨平台跨窗口，2am 清空
    @d (buffer_recall): 已读不删，仅 2am 清空
    @e (buffer_surface): 已读即删 (FIFO)
    """

    @abc.abstractmethod
    async def append_raw(self, role: str, content: str, platform: str, turn_id: str) -> None:
        """追加 @a 原料。"""
        ...

    @abc.abstractmethod
    async def read_all_raw(self) -> list[dict]:
        """读取 @a 全量。"""
        ...

    @abc.abstractmethod
    async def read_recent_recall(self, n: int) -> list[RecallEntry]:
        """读取最近 N 条 @d（不标记已读，不删除）。"""
        ...

    @abc.abstractmethod
    async def write_recall(
        self, trigger_id: str, content: str, raw_content: str, metadata: dict
    ) -> int:
        """写入 @d，返回自增 ID。"""
        ...

    @abc.abstractmethod
    async def read_recall_latest(self) -> Optional[RecallEntry]:
        """读取最新一条 @d（标记 read_at，不删除）。"""
        ...

    @abc.abstractmethod
    async def read_recall_by_id(self, recall_id: int) -> Optional[RecallEntry]:
        """按 ID 读取 @d（标记 read_at，不删除）。"""
        ...

    @abc.abstractmethod
    async def scan_recall_for_surface(self) -> list[RecallEntry]:
        """扫描 @d 供 @e 选材（不标记，不删除）。"""
        ...

    @abc.abstractmethod
    async def read_all_recall(self) -> list[RecallEntry]:
        """读取 @d 全量。"""
        ...

    @abc.abstractmethod
    async def write_surface(
        self, content: str, raw_content: str, surface_type: str, source_ids: list[int]
    ) -> int:
        """写入 @e，返回自增 ID。"""
        ...

    @abc.abstractmethod
    async def read_surface(self) -> Optional[SurfaceEntry]:
        """读取并删除一条最早的 @e（FIFO）。"""
        ...

    @abc.abstractmethod
    async def clear_raw(self) -> None:
        """清空 @a。"""
        ...

    @abc.abstractmethod
    async def clear_raw_up_to(self, max_id: int) -> None:
        """清空 @a 中 id <= max_id 的记录（水位清理）。"""
        ...

    @abc.abstractmethod
    async def clear_recall(self) -> None:
        """清空 @d。"""
        ...

    @abc.abstractmethod
    async def clear_recall_up_to(self, max_id: int) -> None:
        """清空 @d 中 id <= max_id 的记录（水位清理）。"""
        ...
