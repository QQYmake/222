"""M4: IntentClassifier 测试——规则层 + LLM 兜底 + 周期校准。"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from app.application.memory.intent_classifier import (
    IntentClassifier,
    IntentResult,
    QUERY_PATTERNS,
    NO_QUERY_PATTERNS,
)


class TestIntentResult:
    """IntentResult 数据合同。"""

    def test_intent_result_fields(self):
        result = IntentResult(
            label="query",
            confidence=0.9,
            source="rule",
            matched_patterns=["查一下"],
        )
        assert result.label == "query"
        assert result.confidence == 0.9
        assert result.source == "rule"
        assert result.matched_patterns == ["查一下"]

    def test_intent_result_no_match(self):
        result = IntentResult(
            label="no_query",
            confidence=0.0,
            source="default",
            matched_patterns=[],
        )
        assert result.label == "no_query"
        assert result.source == "default"


class TestRuleLayer:
    """第一层——规则分类。"""

    @pytest.fixture
    def classifier(self):
        return IntentClassifier(llm_bridge=None)

    def test_query_keyword_查一下(self, classifier):
        """查询关键词：查一下。"""
        result = classifier._rule_classify("帮我查一下上次说的那个方案")
        assert result.label == "query"
        assert result.confidence >= 0.8
        assert result.source == "rule"

    def test_query_keyword_记得(self, classifier):
        """查询关键词：记得XX吗。"""
        result = classifier._rule_classify("你记得我们上次讨论的内容吗？")
        assert result.label == "query"
        assert result.confidence >= 0.8

    def test_query_keyword_之前(self, classifier):
        """查询关键词：之前。"""
        result = classifier._rule_classify("之前你说过的那个观点")
        assert result.label == "query"
        assert result.confidence >= 0.8

    def test_query_keyword_上一次(self, classifier):
        """查询关键词：上一次。"""
        result = classifier._rule_classify("上一次我们聊到的项目")
        assert result.label == "query"
        assert result.confidence >= 0.8

    def test_no_query_casual_chat(self, classifier):
        """无查询模式：纯闲聊。"""
        result = classifier._rule_classify("今天天气真不错啊")
        assert result.label == "no_query"
        assert result.confidence >= 0.8

    def test_no_query_statement(self, classifier):
        """无查询模式：陈述。"""
        result = classifier._rule_classify("今天天气真不错啊")
        assert result.label == "no_query"
        assert result.confidence >= 0.8

    def test_ambiguous_low_confidence(self, classifier):
        """模糊文本返回低置信度。"""
        result = classifier._rule_classify("嗯，我考虑一下这个事情")
        assert result.confidence < 0.8

    def test_empty_text(self, classifier):
        """空文本返回默认 no_query 低置信度。"""
        result = classifier._rule_classify("")
        assert result.label == "no_query"
        assert result.confidence == 0.0
        assert result.source == "default"


class TestLLMFallback:
    """第二层——LLM 兜底。"""

    @pytest.fixture
    def mock_llm_bridge(self):
        bridge = AsyncMock()
        bridge.classify_intent = AsyncMock(return_value="query")
        return bridge

    @pytest.fixture
    def classifier(self, mock_llm_bridge):
        return IntentClassifier(llm_bridge=mock_llm_bridge)

    async def test_llm_fallback_triggered_on_low_confidence(self, classifier, mock_llm_bridge):
        """规则置信度 < 0.8 时触发 LLM 兜底。"""
        mock_llm_bridge.classify_intent = AsyncMock(return_value="query")
        result = await classifier.classify("嗯，我考虑一下这个事情")
        assert result.source == "llm_fallback"
        assert result.label == "query"
        assert result.confidence == 1.0
        mock_llm_bridge.classify_intent.assert_called_once()

    async def test_rule_high_confidence_skips_llm(self, classifier, mock_llm_bridge):
        """规则置信度 >= 0.8 时跳过 LLM。"""
        result = await classifier.classify("帮我查一下之前的方案")
        assert result.source == "rule"
        mock_llm_bridge.classify_intent.assert_not_called()

    async def test_llm_returns_no_query(self, classifier, mock_llm_bridge):
        """LLM 返回 no_query。"""
        mock_llm_bridge.classify_intent = AsyncMock(return_value="no_query")
        result = await classifier.classify("嗯，我考虑一下这个事情")
        assert result.label == "no_query"
        assert result.source == "llm_fallback"

    async def test_llm_fallback_on_exception(self, classifier, mock_llm_bridge):
        """LLM 异常时返回默认 no_query。"""
        mock_llm_bridge.classify_intent = AsyncMock(side_effect=Exception("LLM error"))
        result = await classifier.classify("嗯，我考虑一下这个事情")
        assert result.label == "no_query"
        assert result.source == "llm_error"

    async def test_no_llm_bridge_returns_rule_result(self):
        """无 LLM bridge 时返回规则结果（低置信度 no_query）。"""
        classifier = IntentClassifier(llm_bridge=None)
        result = await classifier.classify("嗯，我考虑一下这个事情")
        assert result.label == "no_query"
        assert result.source == "rule"


class TestCalibration:
    """第三层——周期校准（异步，不阻塞实时路由）。"""

    @pytest.fixture
    def mock_llm_bridge(self):
        bridge = AsyncMock()
        bridge.classify_intent = AsyncMock(return_value="query")
        return bridge

    @pytest.fixture
    def classifier(self, mock_llm_bridge):
        c = IntentClassifier(llm_bridge=mock_llm_bridge, calibration_interval=3)
        return c

    async def test_calibration_does_not_block_classify(self, classifier, mock_llm_bridge):
        """校准不阻塞实时路由。"""
        # 触发 3 次 classify，第 3 次应触发校准但不影响结果
        mock_llm_bridge.classify_intent = AsyncMock(return_value="query")
        for _ in range(3):
            result = await classifier.classify("帮我查一下方案")
            assert result.source == "rule"  # 规则高置信度，不走 LLM

    async def test_calibration_records_mismatch(self, classifier):
        """校准偏差被记录。"""
        classifier._calibration_interval = 1
        classifier._message_count = 0
        mock_llm = AsyncMock()
        mock_llm.classify_intent = AsyncMock(return_value="no_query")
        classifier._llm_bridge = mock_llm

        # 规则判 query，LLM 判 no_query → 偏差
        rule_result = classifier._rule_classify("查一下之前的方案")
        assert rule_result.label == "query"
        await classifier._maybe_calibrate("查一下之前的方案", rule_result)
        assert len(classifier._calibration_mismatches) >= 1

    async def test_calibration_no_mismatch_when_agree(self, classifier):
        """规则与 LLM 一致时不记录偏差。"""
        classifier._calibration_interval = 1
        classifier._message_count = 0
        mock_llm = AsyncMock()
        mock_llm.classify_intent = AsyncMock(return_value="query")
        classifier._llm_bridge = mock_llm

        rule_result = classifier._rule_classify("查一下之前的方案")
        assert rule_result.label == "query"
        await classifier._maybe_calibrate("查一下之前的方案", rule_result)
        assert len(classifier._calibration_mismatches) == 0
