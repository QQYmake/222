"""测试 ContextBuilder 的 XML 渲染和记忆排序/预算。"""
import pytest
from datetime import datetime

from app.domain.models.sample import SampleEnvelope
from app.domain.models.identity import IdentityData
from app.domain.models.preferences import PreferencesData
from app.domain.models.memories import MemoryItem, MemoriesData
from app.domain.models.working_state import WorkingStateData
from app.domain.ports.sample_reader import AllSamples
from app.domain.models.context_builder import (
    xml_escape,
    render_xml_block,
    sort_memories,
    fit_within_budget,
    render_state_xml,
)


class TestXmlEscape:
    def test_ampersand(self):
        assert xml_escape("a&b") == "a&amp;b"

    def test_less_than(self):
        assert xml_escape("a<b") == "a&lt;b"

    def test_greater_than(self):
        assert xml_escape("a>b") == "a&gt;b"

    def test_quote(self):
        assert xml_escape('a"b') == "a&quot;b"

    def test_apostrophe(self):
        assert xml_escape("a'b") == "a&apos;b"

    def test_all_special(self):
        assert xml_escape('<>&"\'') == "&lt;&gt;&amp;&quot;&apos;"

    def test_no_special(self):
        assert xml_escape("hello") == "hello"

    def test_empty(self):
        assert xml_escape("") == ""

    def test_ampersand_first(self):
        """& must be escaped first to avoid double-escaping."""
        assert xml_escape("<&>") == "&lt;&amp;&gt;"


class TestRenderXmlBlock:
    def test_simple(self):
        result = render_xml_block("name", "沉")
        assert result == "<name>沉</name>"

    def test_with_attributes(self):
        result = render_xml_block("memory", "content", {"id": "m1", "priority": "0.9"})
        assert 'id="m1"' in result
        assert 'priority="0.9"' in result
        assert ">content</memory>" in result

    def test_escapes_content(self):
        result = render_xml_block("tag", "<script>")
        assert "&lt;script&gt;" in result

    def test_empty_content(self):
        result = render_xml_block("tag", "")
        assert result == "<tag></tag>"


class TestSortMemories:
    def _make(self, mid, priority, created_at, content="x"):
        return MemoryItem(id=mid, content=content, category="event",
                          priority=priority, created_at=created_at)

    def test_priority_descending(self):
        items = [
            self._make("m1", 0.1, "2026-01-01T00:00:00+08:00"),
            self._make("m2", 0.9, "2026-01-02T00:00:00+08:00"),
            self._make("m3", 0.5, "2026-01-03T00:00:00+08:00"),
        ]
        result = sort_memories(items)
        assert result[0].id == "m2"
        assert result[1].id == "m3"
        assert result[2].id == "m1"

    def test_same_priority_created_at_ascending(self):
        items = [
            self._make("m1", 0.5, "2026-01-03T00:00:00+08:00"),
            self._make("m2", 0.5, "2026-01-01T00:00:00+08:00"),
            self._make("m3", 0.5, "2026-01-02T00:00:00+08:00"),
        ]
        result = sort_memories(items)
        assert result[0].id == "m2"
        assert result[1].id == "m3"
        assert result[2].id == "m1"

    def test_stable_sort(self):
        """Same priority + same created_at → original order preserved."""
        items = [
            self._make("m1", 0.5, "2026-01-01T00:00:00+08:00"),
            self._make("m2", 0.5, "2026-01-01T00:00:00+08:00"),
        ]
        result = sort_memories(items)
        assert result[0].id == "m1"
        assert result[1].id == "m2"

    def test_empty_list(self):
        assert sort_memories([]) == []


class TestFitWithinBudget:
    def _make(self, mid, content):
        return MemoryItem(id=mid, content=content, category="event",
                          priority=0.5, created_at="2026-01-01T00:00:00+08:00")

    def test_all_fit(self):
        items = [self._make("m1", "aaa"), self._make("m2", "bbb")]
        result = fit_within_budget(items, 100)
        assert len(result) == 2

    def test_budget_excludes_last(self):
        items = [self._make("m1", "aaa"), self._make("m2", "bbb"), self._make("m3", "ccc")]
        result = fit_within_budget(items, 5)
        assert len(result) == 1
        assert result[0].id == "m1"

    def test_zero_budget(self):
        items = [self._make("m1", "aaa")]
        result = fit_within_budget(items, 0)
        assert result == []

    def test_exact_fit(self):
        items = [self._make("m1", "aaa"), self._make("m2", "bbb")]
        result = fit_within_budget(items, 6)
        assert len(result) == 2

    def test_does_not_split(self):
        """A single item exceeding budget is excluded entirely."""
        items = [self._make("m1", "aaaaaaaaaa")]
        result = fit_within_budget(items, 5)
        assert result == []


class TestRenderStateXml:
    def _make_samples(self, identity_data=None, memories_items=None):
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
        if memories_items is None:
            memories_items = []
        memories_data = MemoriesData(items=memories_items)
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

    def test_contains_chen_state_tag(self):
        samples = self._make_samples()
        result = render_state_xml(samples, [])
        assert "<chen_state>" in result
        assert "</chen_state>" in result

    def test_contains_identity(self):
        samples = self._make_samples()
        result = render_state_xml(samples, [])
        assert "<identity>" in result
        assert "沉" in result

    def test_contains_preferences(self):
        samples = self._make_samples()
        result = render_state_xml(samples, [])
        assert "<user_preferences>" in result
        assert "咖啡" in result

    def test_contains_memories(self):
        items = [MemoryItem(id="m1", content="记忆内容", category="event",
                            priority=0.9, created_at="2026-01-01T00:00:00+08:00")]
        samples = self._make_samples(memories_items=items)
        result = render_state_xml(samples, items)
        assert "<memories>" in result
        assert "记忆内容" in result

    def test_contains_working_state(self):
        samples = self._make_samples()
        result = render_state_xml(samples, [])
        assert "<working_state>" in result
        assert "平静" in result

    def test_order_is_identity_first(self):
        samples = self._make_samples()
        result = render_state_xml(samples, [])
        id_pos = result.index("<identity>")
        pref_pos = result.index("<user_preferences>")
        assert id_pos < pref_pos

    def test_xml_escaping_in_identity(self):
        """Special characters in identity must be escaped."""
        identity = IdentityData(
            name="<script>", self_description="ok",
            values=[], boundaries=[], relationship_definition="ok",
        )
        samples = self._make_samples(identity_data=identity)
        result = render_state_xml(samples, [])
        assert "<script>" not in result.replace("<chen_state>", "").replace("</chen_state>", "")
        assert "&lt;script&gt;" in result

    def test_empty_memories(self):
        samples = self._make_samples()
        result = render_state_xml(samples, [])
        assert "<memories>" in result
        assert "</memories>" in result
