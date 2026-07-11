"""Sample 领域模型：SampleType、SampleEnvelope、校验函数和错误类型。

数据合同来源：架构文档 5.1 SampleEnvelope。
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any, Literal, Optional, TypeVar

T = TypeVar("T")

SampleType = Literal["identity", "preferences", "memories", "working_state"]

SampleSource = Literal["sample", "state_worker", "fallback_empty"]


class SampleValidationError(Exception):
    """Sample 结构校验失败。"""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"SampleValidationError: {field} — {reason}")


class SampleReadError(Exception):
    """Sample 读取失败统一包装。

    将文件缺失、读取失败、JSON 解析失败和结构校验失败统一包装。
    """

    def __init__(
        self,
        sample_type: SampleType,
        reason: str,
        cause: Optional[Exception] = None,
    ):
        self.sample_type = sample_type
        self.reason = reason
        self.cause = cause
        super().__init__(
            f"SampleReadError: {sample_type} — {reason}"
            + (f" (cause: {cause})" if cause else "")
        )

    @property
    def public_message(self) -> str:
        return f"Sample '{self.sample_type}' is unavailable: {self.reason}"


@dataclasses.dataclass(frozen=True)
class SampleEnvelope:
    """四份 Sample 的统一外壳。data 内容各自不同。"""

    sample_type: SampleType
    version: int
    updated_at: str
    source: SampleSource
    data: Any


def is_valid_iso8601(value: Optional[str]) -> bool:
    """检查字符串是否为合法 ISO 8601 时间。"""
    if not value or not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def validate_sample(sample: SampleEnvelope, expected_type: SampleType) -> SampleEnvelope:
    """校验 SampleEnvelope 的通用字段。

    指令:
      1. 要求 sample_type 与 expected_type 一致
      2. 正式 Sample (source != "fallback_empty") 的 version >= 1
      3. fallback_empty 的 version 固定为 0
      4. 要求 updated_at 是合法 ISO 8601
      5. 要求 data 存在
      6. 不满足时抛出 SampleValidationError
    """
    if sample.sample_type != expected_type:
        raise SampleValidationError(
            "sample_type",
            f"mismatch: expected {expected_type}, got {sample.sample_type}",
        )

    if sample.source != "fallback_empty":
        if not isinstance(sample.version, int) or sample.version < 1:
            raise SampleValidationError(
                "version",
                f"must be >= 1 for formal samples, got {sample.version}",
            )
    else:
        # fallback_empty 的 version 必须为 0
        if sample.version != 0:
            raise SampleValidationError(
                "version",
                f"fallback_empty must have version=0, got {sample.version}",
            )

    if not is_valid_iso8601(sample.updated_at):
        raise SampleValidationError(
            "updated_at",
            f"not valid ISO 8601: {sample.updated_at}",
        )

    if sample.data is None:
        raise SampleValidationError("data", "is missing")

    return sample
