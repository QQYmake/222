"""PersonaStore 端口：人格存储抽象接口。

数据合同来源：V3 架构文档 5.8 人格存储。
"""
from __future__ import annotations

import abc
from typing import Any, Optional


class PersonaStore(abc.ABC):
    """人格存储端口。"""

    @abc.abstractmethod
    async def write_profile(
        self,
        actor_id: str,
        big_five: dict[str, float],
        efstb: dict[str, float],
        aliases: list[str],
    ) -> None:
        """写入或更新人格画像。"""
        ...

    @abc.abstractmethod
    async def read_profile(self, actor_id: str) -> Optional[dict[str, Any]]:
        """读取人格画像。不存在返回 None。"""
        ...

    @abc.abstractmethod
    async def write_observation(
        self,
        actor_id: str,
        observation: str,
        raw_observation: str,
        source_episode_ids: list[str],
    ) -> None:
        """写入人格观察。"""
        ...

    @abc.abstractmethod
    async def read_observations(self, actor_id: str) -> list[dict[str, Any]]:
        """读取某角色的人格观察列表。"""
        ...
