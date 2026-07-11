"""测试 ContextBuilder.build() 主流程。"""
import pytest
from app.domain.models.context_builder import ContextBuilder, render_timer_instruction
from app.domain.models.trigger import UserTrigger, TimerTrigger
from app.domain.models.sample import SampleEnvelope
from app.domain.models.identity import IdentityData
from app.domain.models.preferences import PreferencesData
from app.domain.models.memories import MemoryItem, MemoriesData
from app.domain.models.working_state import WorkingStateData
from app.domain.ports.sample_reader import AllSamples


def _make_samples(memory_items=None):
    identity_data = IdentityData(
        name="沉", self_description="描述",
        values=["v1"], boundaries=["b1"],
        relationship_definition="关系",
    )
    preferences_data = PreferencesData(
        communication_preferences=["简洁"], stable_likes=["咖啡"],
        stable_dislikes=["噪音"], interaction_rules=["尊重"],
    )
    memories_data = MemoriesData(items=memory_items or [])
    working_state_data = WorkingStateData(
        current_focus=["项目"], emotion_summary="平静",
        pending_items=["待办"], next_wake_at=None,
    )

    def env(data, v=1):
        return SampleEnvelope(
            sample_type="x", version=v, updated_at="2026-01-01T00:00:00+08:00",
            source="sample", data=data,
        )

    return AllSamples(
        identity=env(identity_data, 1),
        preferences=env(preferences_data, 1),
        memories=env(memories_data, 2),
        working_state=env(working_state_data, 3),
    )


class TestBuildPassiveTurn:
    def test_first_message_is_system(self):
        samples = _make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "早上好"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert result.messages[0].role == "system"

    def test_system_contains_chen_state(self):
        samples = _make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert "<chen_state>" in result.messages[0].content

    def test_user_message_preserved(self):
        samples = _make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "早上好"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert result.messages[-1].role == "user"
        assert result.messages[-1].content == "早上好"

    def test_no_second_system_message(self):
        """客户端 system 内容不应成为并列 system message。"""
        samples = _make_samples()
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

    def test_frontend_system_in_supplemental(self):
        samples = _make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "hi"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert "你是助手" in result.messages[0].content
        assert '<frontend_instructions priority="supplemental">' in result.messages[0].content

    def test_conversation_order_preserved(self):
        samples = _make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "msg1"},
                {"role": "assistant", "content": "resp1"},
                {"role": "user", "content": "msg2"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        non_system = [m for m in result.messages if m.role != "system"]
        assert len(non_system) == 3
        assert non_system[0].content == "msg1"
        assert non_system[1].content == "resp1"
        assert non_system[2].content == "msg2"

    def test_sample_versions_collected(self):
        samples = _make_samples()
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert result.sample_versions["identity"] == 1
        assert result.sample_versions["preferences"] == 1
        assert result.sample_versions["memories"] == 2
        assert result.sample_versions["working_state"] == 3


class TestBuildTimerTurn:
    def test_two_messages(self):
        samples = _make_samples()
        trigger = TimerTrigger(
            trigger_id="timer:2026-07-12T09:00:00+08:00",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查状态",
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert len(result.messages) == 2

    def test_first_is_system(self):
        samples = _make_samples()
        trigger = TimerTrigger(
            trigger_id="timer:t1",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查",
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert result.messages[0].role == "system"

    def test_second_is_user_with_timer_instruction(self):
        samples = _make_samples()
        trigger = TimerTrigger(
            trigger_id="timer:t1",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查状态",
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert result.messages[1].role == "user"
        assert "定时唤醒" in result.messages[1].content
        assert trigger.instruction in result.messages[1].content
        assert trigger.fired_at in result.messages[1].content

    def test_timer_message_contains_no_message_marker(self):
        samples = _make_samples()
        trigger = TimerTrigger(
            trigger_id="timer:t1",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查",
        )
        cb = ContextBuilder(memory_char_budget=12000)
        result = cb.build(samples, trigger)
        assert "<NO_MESSAGE>" in result.messages[1].content


class TestRenderTimerInstruction:
    def test_contains_fired_at(self):
        trigger = TimerTrigger(
            trigger_id="timer:t1",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查",
        )
        result = render_timer_instruction(trigger)
        assert "2026-07-12T09:00:00+08:00" in result

    def test_contains_instruction(self):
        trigger = TimerTrigger(
            trigger_id="timer:t1",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查状态",
        )
        result = render_timer_instruction(trigger)
        assert "检查状态" in result

    def test_contains_no_message_marker(self):
        trigger = TimerTrigger(
            trigger_id="timer:t1",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查",
        )
        result = render_timer_instruction(trigger)
        assert "<NO_MESSAGE>" in result


class TestMemoryBudgetInBuild:
    def test_budget_applied(self):
        items = [
            MemoryItem(
                id=f"m{i}", content=f"{'x' * 200}", category="event",
                priority=float(100 - i), created_at=f"2026-01-{i+1:02d}T00:00:00+08:00",
            )
            for i in range(100)
        ]
        samples = _make_samples(memory_items=items)
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )
        cb = ContextBuilder(memory_char_budget=1000)
        result = cb.build(samples, trigger)
        # budget=1000, each item 200 chars → max 5 items
        memory_count = result.messages[0].content.count("<memory ")
        assert memory_count <= 5
