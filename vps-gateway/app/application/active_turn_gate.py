"""ActiveTurnGate：主动回合互斥门。

数据合同来源：架构文档 6.10 ActiveTurnGate。

指令：
  1. Gate 空闲：立即占用并返回 true
  2. Gate 已占用：立即返回 false
  3. 禁止等待 Gate
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ActiveTurnGate:
    """主动回合互斥门。

    架构不变量 7：两个主动回合永远不能并行；冲突任务直接 expired。
    架构不变量 16：构造函数不得创建后台任务。
    """

    def __init__(self):
        self._locked = False

    async def try_acquire(self) -> bool:
        """非阻塞获取 Gate。

        返回 True 表示成功占用，False 表示已被占用。
        绝不等待。
        """
        if self._locked:
            logger.info("active_turn_gate_busy")
            return False
        self._locked = True
        logger.info("active_turn_gate_acquired")
        return True

    def release(self) -> None:
        """释放 Gate。"""
        self._locked = False
        logger.info("active_turn_gate_released")

    @property
    def is_locked(self) -> bool:
        """当前是否被占用。"""
        return self._locked
