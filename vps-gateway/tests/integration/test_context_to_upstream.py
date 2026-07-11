"""M2 集成验证：AllSamples + Trigger → ContextBuilder → Mock ModelClient。"""
import pytest
from app.domain.models.context_builder import ContextBuilder
from app.domain.models.trigger import UserTrigger, TimerTrigger
from app.domain.models.sample import SampleEnvelope
from app.domain.models.identity import IdentityData
from app.domain.models.preferences import PreferencesData
from app.domain.models.memories import MemoryItem, MemoriesData
from app.domain.models.working_state import WorkingStateData
from app.domain.ports.sample_reader import AllSamples
from app.adapters.samples.file_sample_repository import FileSampleRepository


class TestPassiveTurnFullFlow:
    def _make_samples(self, identity_data=None, memory_items=None):
        if identity_data is None:
            identity_data = IdentityData(
                name="沉", self_description="测试描述",
                values=["价值1"], boundaries=["边界1"],
                relationship_definition="关系定义",
            )
        preferences_data = PreferencesData(
            communication_preferences=["简洁"], stable_likes=["咖啡"],
            stable_dislikes=["噪音"], interaction_rules=["尊重"],
        )
        memories_data = MemoriesData(items=memory_items or [])
        working_state_data = WorkingStateData(
            current_focus=["项目A"], emotion_summary="平静",
            pending_items=["待办1"], next_wake_at=None,
        )

        def env(data, v=1):
            return SampleEnvelope(
                sample_type="x", version=v, updated_at="2026-01-01T00:00:00+08:00",
                source="sample", data=data,
            )

        return AllSamples(
            identity=env(identity_data, 1),
            preferences=env(preferences_data, 1),
            memories=env(memories_data, 1),
            working_state=env(working_state_data, 1),
        )

    def test_messages_structure(self):
        samples = self._make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "早上好"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)

        assert result.messages[0].role == "system"
        assert "<chen_state>" in result.messages[0].content
        assert "沉" in result.messages[0].content
        assert result.messages[-1].role == "user"
        assert result.messages[-1].content == "早上好"

    def test_no_second_system_message(self):
        samples = self._make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "hi"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)

        system_count = sum(1 for m in result.messages if m.role == "system")
        assert system_count == 1

    def test_sample_versions_collected(self):
        samples = self._make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)

        assert "identity" in result.sample_versions
        assert "preferences" in result.sample_versions
        assert "memories" in result.sample_versions
        assert "working_state" in result.sample_versions


class TestTimerTurnFullFlow:
    def _make_samples(self):
        identity_data = IdentityData(
            name="沉", self_description="描述",
            values=["v1"], boundaries=["b1"],
            relationship_definition="关系",
        )
        preferences_data = PreferencesData(
            communication_preferences=[], stable_likes=[],
            stable_dislikes=[], interaction_rules=[],
        )
        memories_data = MemoriesData(items=[])
        working_state_data = WorkingStateData(
            current_focus=[], emotion_summary="",
            pending_items=[], next_wake_at=None,
        )

        def env(data, v=1):
            return SampleEnvelope(
                sample_type="x", version=v, updated_at="2026-01-01T00:00:00+08:00",
                source="sample", data=data,
            )

        return AllSamples(
            identity=env(identity_data, 1),
            preferences=env(preferences_data, 1),
            memories=env(memories_data, 1),
            working_state=env(working_state_data, 1),
        )

    def test_message_structure(self):
        samples = self._make_samples()
        trigger = TimerTrigger(
            trigger_id="timer:2026-07-12T09:00:00+08:00",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查状态",
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)

        assert len(result.messages) == 2
        assert result.messages[0].role == "system"
        assert result.messages[1].role == "user"
        assert "定时唤醒" in result.messages[1].content
        assert trigger.instruction in result.messages[1].content


class TestXmlEscapingInState:
    def test_special_characters_escaped(self):
        identity_data = IdentityData(
            name="<script>alert(1)</script>",
            self_description="test & test <tag>",
            values=["a&b"],
            boundaries=["c<d"],
            relationship_definition="ok",
        )
        preferences_data = PreferencesData(
            communication_preferences=[], stable_likes=[],
            stable_dislikes=[], interaction_rules=[],
        )
        memories_data = MemoriesData(items=[])
        working_state_data = WorkingStateData(
            current_focus=[], emotion_summary="",
            pending_items=[], next_wake_at=None,
        )

        def env(data, v=1):
            return SampleEnvelope(
                sample_type="x", version=v, updated_at="2026-01-01T00:00:00+08:00",
                source="sample", data=data,
            )

        samples = AllSamples(
            identity=env(identity_data, 1),
            preferences=env(preferences_data, 1),
            memories=env(memories_data, 1),
            working_state=env(working_state_data, 1),
        )
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)

        system_content = result.messages[0].content
        # Raw script tags must not appear (they should be escaped)
        assert "<script>" not in system_content.replace("<chen_state>", "").replace(
            "<frontend_instructions", ""
        ).replace("</chen_state>", "").replace("</frontend_instructions>", "")
        assert "&lt;script&gt;" in system_content


class TestMemoryBudgetApplied:
    def test_budget_limits_memory_count(self):
        items = [
            MemoryItem(
                id=f"m{i}",
                content="x" * 200,
                category="event",
                priority=float(100 - i),
                created_at=f"2026-01-{i + 1:02d}T00:00:00+08:00",
            )
            for i in range(100)
        ]
        identity_data = IdentityData(
            name="沉", self_description="desc",
            values=[], boundaries=[], relationship_definition="rel",
        )
        preferences_data = PreferencesData(
            communication_preferences=[], stable_likes=[],
            stable_dislikes=[], interaction_rules=[],
        )
        memories_data = MemoriesData(items=items)
        working_state_data = WorkingStateData(
            current_focus=[], emotion_summary="",
            pending_items=[], next_wake_at=None,
        )

        def env(data, v=1):
            return SampleEnvelope(
                sample_type="x", version=v, updated_at="2026-01-01T00:00:00+08:00",
                source="sample", data=data,
            )

        samples = AllSamples(
            identity=env(identity_data, 1),
            preferences=env(preferences_data, 1),
            memories=env(memories_data, 1),
            working_state=env(working_state_data, 1),
        )
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=1000)
        result = cb.build(samples, trigger)

        memory_count = result.messages[0].content.count("<memory ")
        assert memory_count <= 5


class TestRealSamplesIntegration:
    """用真实 Sample 文件验证完整流程。"""

    def test_real_samples_build(self):
        repo = FileSampleRepository("./samples")
        samples = repo.read_all()

        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "早上好，沉。"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)

        assert result.messages[0].role == "system"
        assert "<chen_state>" in result.messages[0].content
        assert result.messages[-1].content == "早上好，沉。"
        assert len(result.sample_versions) == 4
