"""IdentityData 领域模型。

数据合同来源：架构文档 5.2 IdentitySample。
保持原始文字，不做模型润色。作为最高优先级状态拼入 system message。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from app.domain.models.sample import SampleValidationError


@dataclasses.dataclass(frozen=True)
class IdentityData:
    """核心身份、价值边界和关系定义。"""

    name: str
    self_description: str
    values: list[str]
    boundaries: list[str]
    relationship_definition: str


def validate_identity(data: dict[str, Any]) -> IdentityData:
    """校验并构造 IdentityData。

    指令:
      1. 校验 name 非空字符串
      2. 校验 self_description 非空字符串
      3. 校验 values 是字符串列表 (允许空列表)
      4. 校验 boundaries 是字符串列表 (允许空列表)
      5. 校验 relationship_definition 非空字符串
      6. 保持原始文字，不做模型润色
    """
    for field in ("name", "self_description", "relationship_definition"):
        val = data.get(field)
        if not isinstance(val, str) or not val:
            raise SampleValidationError(field, "must be non-empty string")

    for field in ("values", "boundaries"):
        val = data.get(field)
        if not isinstance(val, list):
            raise SampleValidationError(field, "must be a list")
        for item in val:
            if not isinstance(item, str):
                raise SampleValidationError(field, "all items must be strings")

    return IdentityData(
        name=data["name"],
        self_description=data["self_description"],
        values=list(data["values"]),
        boundaries=list(data["boundaries"]),
        relationship_definition=data["relationship_definition"],
    )
