"""FileSampleRepository: 从本地 JSON 文件读取四份只读 Sample。

数据合同来源：架构文档 6.1 FileSampleRepository。

职责：从本地 JSON 文件读取四份稳定 Sample；
不计算状态、不调用模型、不修改文件。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

from app.domain.models.sample import (
    SampleEnvelope,
    SampleReadError,
    SampleType,
    SampleValidationError,
    validate_sample,
)
from app.domain.models.identity import validate_identity
from app.domain.models.preferences import validate_preferences
from app.domain.models.memories import validate_memories
from app.domain.models.working_state import validate_working_state
from app.domain.ports.sample_reader import (
    SampleReader,
    AllSamples,
    EMPTY_PREFERENCES,
    EMPTY_MEMORIES,
    EMPTY_WORKING_STATE,
)
from app.infrastructure.logging import get_logger

# 固定路径映射：禁止由外部请求直接传入任意路径
_FILE_NAMES: dict[SampleType, str] = {
    "identity": "identity.sample.json",
    "preferences": "preferences.sample.json",
    "memories": "memories.sample.json",
    "working_state": "working_state.sample.json",
}

# 各 sample_type 对应的校验函数
_VALIDATORS: dict[SampleType, Callable[[dict[str, Any]], Any]] = {
    "identity": validate_identity,
    "preferences": validate_preferences,
    "memories": validate_memories,
    "working_state": validate_working_state,
}

# 各 sample_type 对应的空数据（降级用）
_EMPTY_DATA: dict[SampleType, dict[str, Any]] = {
    "preferences": EMPTY_PREFERENCES,
    "memories": EMPTY_MEMORIES,
    "working_state": EMPTY_WORKING_STATE,
}


class FileSampleRepository(SampleReader):
    """从本地 JSON 文件读取四份稳定 Sample 的适配器。

    依赖方向：实现 SampleReader 端口，依赖 config (路径) 和 domain models (校验)。
    """

    def __init__(self, sample_directory: str):
        self._sample_directory = sample_directory
        self._logger = get_logger("file_sample_repository")

    def read(self, sample_type: SampleType) -> SampleEnvelope:
        """读取单份 Sample。

        指令:
          1. 根据固定映射选择文件，禁止外部传入任意路径
          2. 读取完整文件
          3. 解析 JSON
          4. 执行类型和字段校验
          5. 返回不可变对象
          6. 失败时抛 SampleReadError
        """
        file_name = _FILE_NAMES[sample_type]
        path = os.path.join(self._sample_directory, file_name)

        # 1. 读取完整文件
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError as e:
            raise SampleReadError(sample_type, reason="missing", cause=e)
        except OSError as e:
            raise SampleReadError(sample_type, reason="io_error", cause=e)

        # 2. 解析 JSON
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            raise SampleReadError(sample_type, reason="invalid_json", cause=e)

        # 3. 校验 Envelope 通用字段
        try:
            envelope = SampleEnvelope(
                sample_type=parsed.get("sample_type"),
                version=parsed.get("version"),
                updated_at=parsed.get("updated_at"),
                source=parsed.get("source"),
                data=parsed.get("data"),
            )
            validate_sample(envelope, expected_type=sample_type)
        except SampleValidationError as e:
            raise SampleReadError(sample_type, reason="invalid_structure", cause=e)

        # 4. 校验 data 字段并构造领域模型
        validator = _VALIDATORS[sample_type]
        try:
            validated_data = validator(parsed["data"])
        except SampleValidationError as e:
            raise SampleReadError(sample_type, reason="invalid_structure", cause=e)

        # 5. 返回不可变对象
        return SampleEnvelope(
            sample_type=envelope.sample_type,
            version=envelope.version,
            updated_at=envelope.updated_at,
            source=envelope.source,
            data=validated_data,
        )

    def read_or_empty(self, sample_type: SampleType, empty_data: dict[str, Any]) -> SampleEnvelope:
        """降级读取：失败时返回空 Envelope。

        指令:
          1. 尝试调用 read()
          2. 失败时记录 optional_sample_degraded 警告
          3. 对空数据运行校验，构造领域模型 (保持与正式 Sample 一致的 data 类型)
          4. 返回 fallback_empty Envelope (version=0)
        """
        try:
            return self.read(sample_type)
        except SampleReadError as e:
            self._logger.warning(
                "optional_sample_degraded",
                extra={"sample_type": sample_type, "reason": e.reason},
            )
            # 对空数据运行校验器，确保 data 字段类型与正式 Sample 一致
            validator = _VALIDATORS[sample_type]
            validated_empty = validator(empty_data)
            return SampleEnvelope(
                sample_type=sample_type,
                version=0,
                updated_at=datetime.now(timezone.utc).isoformat(),
                source="fallback_empty",
                data=validated_empty,
            )

    def read_all(self) -> AllSamples:
        """读取全部四份 Sample。

        指令:
          1. identity 不降级，失败直接抛 SampleReadError
          2. preferences 降级为空偏好
          3. memories 降级为空记忆
          4. working_state 降级为空状态
          5. 返回 AllSamples
        """
        # identity 不降级
        identity = self.read("identity")

        # 三类可选 Sample 降级为空
        preferences = self.read_or_empty("preferences", EMPTY_PREFERENCES)
        memories = self.read_or_empty("memories", EMPTY_MEMORIES)
        working_state = self.read_or_empty("working_state", EMPTY_WORKING_STATE)

        return AllSamples(
            identity=identity,
            preferences=preferences,
            memories=memories,
            working_state=working_state,
        )
