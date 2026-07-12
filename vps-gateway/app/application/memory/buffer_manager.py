"""BufferManager：缓冲区管理。

委托 BufferStore 端口执行实际读写，提供类型安全的薄封装。
数据合同来源：V3 架构文档 6.3 BufferManager。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.domain.models.memory import RecallEntry, SurfaceEntry
    from app.domain.ports.buffer_store import BufferStore

logger = logging.getLogger(__name__)


class BufferManager:
    """缓冲区管理器。

    @a (raw) — 每回合 after_turn 追加；2am 沉淀后清空
    @d (recall) — 检索管线写入；@e 已读即删（read_recall_latest 标记 read_at）；2am 清空
    @e (surface) — 周期生成器写入；read_surface FIFO 已读即删
    """

    def __init__(self, store: "BufferStore") -> None:
        self._store = store

    async def append_raw(
        self, role: str, content: str, platform: str, turn_id: str
    ) -> None:
        """追加 @a 原料。"""
        await self._store.append_raw(role=role, content=content, platform=platform, turn_id=turn_id)

    async def read_all_raw(self) -> list[Any]:
        """读取 @a 全量（供沉淀管线使用）。"""
        return await self._store.read_all_raw()

    async def read_all_recall(self) -> list["RecallEntry"]:
        """读取 @d 全量（供沉淀管线使用）。"""
        return await self._store.read_all_recall()

    async def read_recent_recall(self, n: int) -> list["RecallEntry"]:
        """读取最近 N 条 @d（不标记已读，不删除）。"""
        return await self._store.read_recent_recall(n)

    async def write_recall(
        self, trigger_id: str, content: str, raw_content: str, metadata: dict
    ) -> int:
        """写入 @d。返回新条目 ID。"""
        return await self._store.write_recall(trigger_id, content, raw_content, metadata)

    async def read_recall_latest(self) -> "RecallEntry | None":
        """读取最新一条 @d（标记 read_at，不删除）。"""
        return await self._store.read_recall_latest()

    async def scan_recall_for_surface(self) -> list["RecallEntry"]:
        """扫描 @d 供 @e 选材（不标记，不删除）。"""
        return await self._store.scan_recall_for_surface()

    async def write_surface(
        self, content: str, raw_content: str, surface_type: str, source_ids: list[int]
    ) -> int:
        """写入 @e。返回新条目 ID。"""
        return await self._store.write_surface(content, raw_content, surface_type, source_ids)

    async def read_surface(self) -> "SurfaceEntry | None":
        """读取并删除一条最早的 @e（FIFO）。"""
        return await self._store.read_surface()

    async def clear_raw(self) -> None:
        """清空 @a。"""
        await self._store.clear_raw()

    async def clear_recall(self) -> None:
        """清空 @d。"""
        await self._store.clear_recall()
