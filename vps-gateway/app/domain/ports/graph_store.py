"""GraphStore 端口：图谱存储抽象接口。

数据合同来源：V3 架构文档 6.5 GraphStore。
"""
from __future__ import annotations

import abc
from typing import Any


class GraphStore(abc.ABC):
    """图谱存储端口。

    SQLite 递归 CTE 替代 Neo4j Cypher。
    """

    @abc.abstractmethod
    async def query_events(self, entity: str, max_hops: int) -> list[dict[str, Any]]:
        """用递归 CTE 从 relations 表做 1-max_hops 跳遍历，返回关联事件。"""
        ...

    @abc.abstractmethod
    async def query_episodes(self, intent_type: str) -> list[dict[str, Any]]:
        """按意图类型查询剧情。"""
        ...

    @abc.abstractmethod
    async def query_sagas(self, status: str) -> list[dict[str, Any]]:
        """按状态查询主线。"""
        ...

    @abc.abstractmethod
    async def query_plans(self) -> list[dict[str, Any]]:
        """查询计划类型事件。"""
        ...

    @abc.abstractmethod
    async def write_event(self, event: dict[str, Any]) -> None:
        """写入事件及其关系。"""
        ...

    @abc.abstractmethod
    async def write_episode(self, episode: dict[str, Any]) -> None:
        """写入剧情。"""
        ...

    @abc.abstractmethod
    async def write_saga(self, saga: dict[str, Any]) -> None:
        """写入主线。"""
        ...
