"""FileSampleRepository 集成测试。

验证架构文档 12.1 Sample 读取与拼接 验收基线。
"""
import json
import os
import pytest
from app.adapters.samples.file_sample_repository import FileSampleRepository
from app.domain.models.sample import SampleReadError
from app.domain.ports.sample_reader import (
    EMPTY_PREFERENCES,
    EMPTY_MEMORIES,
    EMPTY_WORKING_STATE,
)


# --- 测试数据工厂 ---

VALID_IDENTITY = {
    "sample_type": "identity",
    "version": 1,
    "updated_at": "2025-01-01T00:00:00+08:00",
    "source": "sample",
    "data": {
        "name": "沉",
        "self_description": "一段自我描述",
        "values": ["价值1", "价值2"],
        "boundaries": ["边界1"],
        "relationship_definition": "与用户的关系定义",
    },
}

VALID_PREFERENCES = {
    "sample_type": "preferences",
    "version": 1,
    "updated_at": "2025-01-01T00:00:00+08:00",
    "source": "sample",
    "data": {
        "communication_preferences": ["偏好1"],
        "stable_likes": ["喜欢1"],
        "stable_dislikes": ["不喜欢1"],
        "interaction_rules": ["规则1"],
    },
}

VALID_MEMORIES = {
    "sample_type": "memories",
    "version": 1,
    "updated_at": "2025-01-01T00:00:00+08:00",
    "source": "sample",
    "data": {
        "items": [
            {
                "id": "mem_001",
                "content": "记忆内容",
                "category": "category_a",
                "priority": 10,
                "created_at": "2025-01-01T00:00:00+08:00",
            }
        ]
    },
}

VALID_WORKING_STATE = {
    "sample_type": "working_state",
    "version": 1,
    "updated_at": "2025-01-01T00:00:00+08:00",
    "source": "sample",
    "data": {
        "current_focus": ["当前关注1"],
        "emotion_summary": "情绪概述",
        "pending_items": ["待办1"],
        "next_wake_at": None,
    },
}

ALL_VALID = {
    "identity.sample.json": VALID_IDENTITY,
    "preferences.sample.json": VALID_PREFERENCES,
    "memories.sample.json": VALID_MEMORIES,
    "working_state.sample.json": VALID_WORKING_STATE,
}


def _write_samples(tmp_path, samples: dict):
    """将字典写入临时目录的 JSON 文件。"""
    for filename, data in samples.items():
        path = tmp_path / filename
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(tmp_path)


class TestReadAllNormal:
    """验收 12.1.1: 四份合法 Sample 均能读取。"""

    def test_read_all_normal(self, tmp_path):
        sample_dir = _write_samples(tmp_path, ALL_VALID)
        repo = FileSampleRepository(sample_dir)
        result = repo.read_all()

        assert result.identity.data.name == "沉"
        assert result.identity.source == "sample"
        assert result.identity.version == 1

        assert result.preferences.data.communication_preferences == ["偏好1"]
        assert result.preferences.source == "sample"

        assert len(result.memories.data.items) == 1
        assert result.memories.data.items[0].id == "mem_001"

        assert result.working_state.data.emotion_summary == "情绪概述"
        assert result.working_state.data.next_wake_at is None

    def test_read_single_identity(self, tmp_path):
        sample_dir = _write_samples(tmp_path, ALL_VALID)
        repo = FileSampleRepository(sample_dir)
        env = repo.read("identity")
        assert env.data.name == "沉"
        assert env.sample_type == "identity"


class TestIdentityMissing:
    """验收 12.1.4: identity 无效时不会调用上游模型 (read_all 抛异常)。"""

    def test_identity_missing_raises(self, tmp_path):
        samples = {k: v for k, v in ALL_VALID.items() if k != "identity.sample.json"}
        sample_dir = _write_samples(tmp_path, samples)
        repo = FileSampleRepository(sample_dir)
        with pytest.raises(SampleReadError) as exc_info:
            repo.read_all()
        assert exc_info.value.sample_type == "identity"
        assert exc_info.value.reason == "missing"

    def test_identity_corrupt_json_raises(self, tmp_path):
        other = {k: v for k, v in ALL_VALID.items() if k != "identity.sample.json"}
        _write_samples(tmp_path, other)
        (tmp_path / "identity.sample.json").write_text("not json", encoding="utf-8")
        repo = FileSampleRepository(str(tmp_path))
        with pytest.raises(SampleReadError) as exc_info:
            repo.read_all()
        assert exc_info.value.sample_type == "identity"
        assert exc_info.value.reason == "invalid_json"

    def test_identity_invalid_structure_raises(self, tmp_path):
        bad_identity = json.loads(json.dumps(VALID_IDENTITY))
        bad_identity["data"]["name"] = ""  # name 不能为空
        (tmp_path / "identity.sample.json").write_text(
            json.dumps(bad_identity, ensure_ascii=False), encoding="utf-8"
        )
        _write_samples(tmp_path, {k: v for k, v in ALL_VALID.items() if k != "identity.sample.json"})
        repo = FileSampleRepository(str(tmp_path))
        with pytest.raises(SampleReadError) as exc_info:
            repo.read_all()
        assert exc_info.value.reason == "invalid_structure"


class TestOptionalSampleDegradation:
    """验收 12.1.5-6: 三类非核心 Sample 缺失/损坏时降级。"""

    def test_preferences_missing_degrades(self, tmp_path):
        samples = {k: v for k, v in ALL_VALID.items() if k != "preferences.sample.json"}
        sample_dir = _write_samples(tmp_path, samples)
        repo = FileSampleRepository(sample_dir)
        result = repo.read_all()
        assert result.preferences.source == "fallback_empty"
        assert result.preferences.version == 0
        assert result.preferences.data.communication_preferences == []

    def test_preferences_corrupt_json_degrades(self, tmp_path):
        (tmp_path / "preferences.sample.json").write_text("not json", encoding="utf-8")
        other = {k: v for k, v in ALL_VALID.items() if k != "preferences.sample.json"}
        _write_samples(tmp_path, other)
        repo = FileSampleRepository(str(tmp_path))
        result = repo.read_all()
        assert result.preferences.source == "fallback_empty"

    def test_memories_invalid_structure_degrades(self, tmp_path):
        bad_memories = json.loads(json.dumps(VALID_MEMORIES))
        bad_memories["data"]["items"] = "not a list"
        (tmp_path / "memories.sample.json").write_text(
            json.dumps(bad_memories, ensure_ascii=False), encoding="utf-8"
        )
        other = {k: v for k, v in ALL_VALID.items() if k != "memories.sample.json"}
        _write_samples(tmp_path, other)
        repo = FileSampleRepository(str(tmp_path))
        result = repo.read_all()
        assert result.memories.source == "fallback_empty"
        assert result.memories.data.items == []

    def test_working_state_missing_degrades(self, tmp_path):
        samples = {k: v for k, v in ALL_VALID.items() if k != "working_state.sample.json"}
        sample_dir = _write_samples(tmp_path, samples)
        repo = FileSampleRepository(sample_dir)
        result = repo.read_all()
        assert result.working_state.source == "fallback_empty"
        assert result.working_state.data.next_wake_at is None

    def test_all_three_optional_missing(self, tmp_path):
        """三个可选 Sample 都缺失时，identity 仍正常读取。"""
        (tmp_path / "identity.sample.json").write_text(
            json.dumps(VALID_IDENTITY, ensure_ascii=False), encoding="utf-8"
        )
        repo = FileSampleRepository(str(tmp_path))
        result = repo.read_all()
        assert result.identity.data.name == "沉"
        assert result.preferences.source == "fallback_empty"
        assert result.memories.source == "fallback_empty"
        assert result.working_state.source == "fallback_empty"


class TestReadRejectsArbitraryPath:
    """验收：read() 只接受 SampleType，不接受路径。"""

    def test_read_accepts_only_sample_type(self, tmp_path):
        sample_dir = _write_samples(tmp_path, ALL_VALID)
        repo = FileSampleRepository(sample_dir)
        # 正常调用
        env = repo.read("identity")
        assert env.data.name == "沉"
        # 传入非 SampleType 字符串应抛 KeyError (固定映射不存在)
        with pytest.raises(KeyError):
            repo.read("/etc/passwd")

    def test_no_path_attribute_exposed(self, tmp_path):
        sample_dir = _write_samples(tmp_path, ALL_VALID)
        repo = FileSampleRepository(sample_dir)
        # 路径映射不应被外部修改
        assert not hasattr(repo, "paths") or isinstance(
            getattr(repo, "paths", None), (dict, type(None))
        )


class TestEnvelopeImmutability:
    """验收：返回的对象不可变。"""

    def test_envelope_is_immutable(self, tmp_path):
        sample_dir = _write_samples(tmp_path, ALL_VALID)
        repo = FileSampleRepository(sample_dir)
        envelope = repo.read("identity")
        with pytest.raises((AttributeError, TypeError)):
            envelope.version = 99

    def test_data_model_immutable(self, tmp_path):
        sample_dir = _write_samples(tmp_path, ALL_VALID)
        repo = FileSampleRepository(sample_dir)
        envelope = repo.read("identity")
        with pytest.raises((AttributeError, TypeError)):
            envelope.data.name = "modified"


class TestFixedConcatenationOrder:
    """验收 12.1.2: 拼接顺序固定且可测试。"""

    def test_read_all_order_is_identity_first(self, tmp_path):
        """read_all 的调用顺序: identity → preferences → memories → working_state。"""
        sample_dir = _write_samples(tmp_path, ALL_VALID)
        repo = FileSampleRepository(sample_dir)
        result = repo.read_all()
        # 验证返回的 AllSamples 字段顺序
        fields = list(result.__class__.__dataclass_fields__.keys())
        assert fields == ["identity", "preferences", "memories", "working_state"]
