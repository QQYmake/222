"""MemoriesData 领域模型。

数据合同来源：架构文档 5.4 MemoriesSample。
校验唯一 id；排序和字符预算由 ContextBuilder 在 M2 执行。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from app.domain.models.sample import SampleValidationError, is_valid_iso8601


@dataclasses.dataclass(frozen=True)
class MemoryItem:
    """单条记忆。"""

    id: str
    content: str
    category: str
    priority: float
    created_at: str


@dataclasses.dataclass(frozen=True)
class MemoriesData:
    """记忆列表。顺序与文件一致，未排序。"""

    items: list[MemoryItem]


def validate_memories(data: dict[str, Any]) -> MemoriesData:
    """校验并构造 MemoriesData。

    指令:
      1. 校验 items 是列表 (允许空列表)
      2. 校验每条记忆具有唯一 id
      3. 校验 content, category 为非空字符串
      4. 校验 priority 为数字
      5. 校验 created_at 为合法 ISO 8601
      6. 排序和字符预算由 ContextBuilder 在 M2 执行
    """
    items = data.get("items")
    if items is None:
        raise SampleValidationError("items", "is missing")
    if not isinstance(items, list):
        raise SampleValidationError("items", "must be a list")

    seen_ids: set[str] = set()
    parsed: list[MemoryItem] = []

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise SampleValidationError(f"items[{i}]", "must be an object")

        for field in ("id", "content", "category"):
            val = item.get(field)
            if not isinstance(val, str) or not val:
                raise SampleValidationError(
                    f"items[{i}].{field}", "must be non-empty string"
                )

        if item["id"] in seen_ids:
            raise SampleValidationError(
                f"items[{i}].id", f"duplicate id: {item['id']}"
            )
        seen_ids.add(item["id"])

        priority = item.get("priority")
        if not isinstance(priority, (int, float)) or isinstance(priority, bool):
            raise SampleValidationError(f"items[{i}].priority", "must be a number")

        created_at = item.get("created_at")
        if not is_valid_iso8601(created_at):
            raise SampleValidationError(
                f"items[{i}].created_at", "must be valid ISO 8601"
            )

        parsed.append(
            MemoryItem(
                id=item["id"],
                content=item["content"],
                category=item["category"],
                priority=float(priority),
                created_at=item["created_at"],
            )
        )

    return MemoriesData(items=parsed)
