"""SampleReader 和 SamplePublisher 端口接口测试。"""
import pytest
from app.domain.ports.sample_reader import (
    SampleReader,
    AllSamples,
    EMPTY_PREFERENCES,
    EMPTY_MEMORIES,
    EMPTY_WORKING_STATE,
)
from app.domain.ports.sample_publisher import SamplePublisher
from app.domain.models.sample import SampleEnvelope
from app.domain.models.identity import IdentityData
from app.domain.models.preferences import PreferencesData
from app.domain.models.memories import MemoriesData
from app.domain.models.working_state import WorkingStateData


def _make_envelope(sample_type, data, version=1, source="sample"):
    return SampleEnvelope(
        sample_type=sample_type,
        version=version,
        updated_at="2025-01-01T00:00:00+08:00",
        source=source,
        data=data,
    )


class TestSampleReaderAbstract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            SampleReader()

    def test_has_read_method(self):
        assert hasattr(SampleReader, "read")

    def test_has_read_all_method(self):
        assert hasattr(SampleReader, "read_all")


class TestAllSamples:
    def test_has_four_fields(self):
        identity = _make_envelope("identity", IdentityData(
            name="沉", self_description="desc",
            values=[], boundaries=[], relationship_definition="rel"
        ))
        prefs = _make_envelope("preferences", PreferencesData(
            [], [], [], []
        ))
        mems = _make_envelope("memories", MemoriesData(items=[]))
        ws = _make_envelope("working_state", WorkingStateData(
            current_focus=[], emotion_summary="", pending_items=[], next_wake_at=None
        ))
        all_samples = AllSamples(
            identity=identity,
            preferences=prefs,
            memories=mems,
            working_state=ws,
        )
        assert all_samples.identity.data.name == "沉"
        assert all_samples.preferences.data.communication_preferences == []
        assert all_samples.memories.data.items == []
        assert all_samples.working_state.data.next_wake_at is None


class TestEmptyConstants:
    def test_empty_preferences(self):
        assert EMPTY_PREFERENCES["communication_preferences"] == []
        assert EMPTY_PREFERENCES["stable_likes"] == []
        assert EMPTY_PREFERENCES["stable_dislikes"] == []
        assert EMPTY_PREFERENCES["interaction_rules"] == []

    def test_empty_memories(self):
        assert EMPTY_MEMORIES["items"] == []

    def test_empty_working_state(self):
        assert EMPTY_WORKING_STATE["current_focus"] == []
        assert EMPTY_WORKING_STATE["emotion_summary"] == ""
        assert EMPTY_WORKING_STATE["pending_items"] == []
        assert EMPTY_WORKING_STATE["next_wake_at"] is None


class TestSamplePublisher:
    def test_publish_raises_not_implemented(self):
        """SamplePublisher 是预留接口，调用时抛 NotImplementedError。"""
        # 需要一个具体子类来测试 publish 方法
        class FakePublisher(SamplePublisher):
            pass

        fake = FakePublisher()
        envelope = _make_envelope("identity", IdentityData(
            name="沉", self_description="desc",
            values=[], boundaries=[], relationship_definition="rel"
        ))
        with pytest.raises(NotImplementedError):
            fake.publish(envelope)
