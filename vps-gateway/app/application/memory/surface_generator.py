"""SurfaceGenerator：@e 周期生成器。

定期扫描 @d 缓冲区，选材后通过 LLM 生成 @e 浮现内容，润色后写入 @e 缓冲区。
数据合同来源：V3 架构文档 6.2 _run_surface_path 和 @e 周期生成。
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.models.memory import RecallEntry

logger = logging.getLogger(__name__)


@dataclass
class SurfaceGenResult:
    """@e 生成结果。"""

    skipped: bool = True
    content: str = ""
    source_ids: list[int] = None

    def __post_init__(self):
        if self.source_ids is None:
            self.source_ids = []


class RandomSurfaceSelector:
    """随机选材器。

    从 @d 条目中随机选择 N 条作为 @e 生成素材。
    """

    def __init__(self, max_items: int = 5) -> None:
        self._max_items = max_items

    def select(self, entries: list["RecallEntry"]) -> list["RecallEntry"]:
        """随机选择最多 max_items 条 @d 条目。"""
        if not entries:
            return []

        count = min(self._max_items, len(entries))
        return random.sample(entries, count)


class SurfaceGenerator:
    """@e 周期生成器。

    依赖注入：
    - llm_bridge: LLMBridge（使用 MEM_SURF 模型配置）
    - buffer_manager: BufferManager（读取 @d，写入 @e）
    - polish_bridge: PolishBridge（润色 @e 内容）
    - selector: RandomSurfaceSelector（选材器）
    """

    def __init__(
        self,
        llm_bridge: Any | None = None,
        buffer_manager: Any | None = None,
        polish_bridge: Any | None = None,
        selector: RandomSurfaceSelector | None = None,
    ) -> None:
        self._llm_bridge = llm_bridge
        self._buffer = buffer_manager
        self._polish = polish_bridge
        self._selector = selector or RandomSurfaceSelector()

    async def generate(self) -> SurfaceGenResult:
        """执行一次 @e 生成周期。"""
        # 扫描 @d 缓冲区
        entries = await self._buffer.scan_recall_for_surface()

        if not entries:
            logger.info("surface_generated_skipped: no @d entries")
            return SurfaceGenResult(skipped=True)

        if self._llm_bridge is None:
            logger.info("surface_generated_skipped: no llm_bridge")
            return SurfaceGenResult(skipped=True)

        # 随机选材
        selected = self._selector.select(entries)
        if not selected:
            return SurfaceGenResult(skipped=True)

        source_ids = [e.id for e in selected]
        material = "\n---\n".join(e.content for e in selected if e.content)

        # LLM 生成
        try:
            raw_content = await self._llm_bridge.generate(
                prompt=f"请根据以下记忆片段，生成一段简洁的浮现内容（可以是回顾、总结或感悟）：\n\n{material}",
                context="surface_generation",
            )
        except Exception as e:
            logger.warning("surface_generate_error: %s", e)
            return SurfaceGenResult(skipped=True)

        if not raw_content or not raw_content.strip():
            logger.info("surface_generated_skipped: empty LLM output")
            return SurfaceGenResult(skipped=True)

        # 润色
        if self._polish is not None:
            try:
                polished = self._polish.polish(raw_content, context="surface_generation")
                if asyncio.iscoroutine(polished):
                    polished = await polished
            except Exception as e:
                logger.warning("surface_polish_error: %s", e)
                polished = raw_content
        else:
            polished = raw_content

        # 写入 @e
        surface_id = await self._buffer.write_surface(
            content=polished.strip(),
            raw_content=raw_content,
            surface_type="periodic",
            source_ids=source_ids,
        )

        logger.info(
            "surface_generated: surface_id=%d source_count=%d",
            surface_id, len(source_ids),
        )

        return SurfaceGenResult(
            skipped=False,
            content=polished.strip(),
            source_ids=source_ids,
        )
