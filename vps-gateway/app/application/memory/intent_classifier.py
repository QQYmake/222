"""IntentClassifier：意图分类器。

三层置信度路由：
1. 规则分类 (0ms) — 匹配关键词模式
2. LLM 兜底 (~1s) — 规则置信度 < 0.8 时触发
3. 周期校准 (异步, 不阻塞) — 每 N 条消息随机抽 1 条跑 LLM 对比

数据合同来源：V3 架构文档 6.4 IntentClassifier。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 查询关键词模式
QUERY_PATTERNS: list[str] = [
    r"查一下",
    r"记得.*吗",
    r"之前",
    r"上一次",
    r"上次",
    r"那天",
    r"XX天前",
    r"XX时候",
    r"什么时候",
    r"什么时间",
    r"回忆",
    r"想起来",
    r"提到过",
    r"说过",
    r"问过",
    r"聊过",
    r"讨论过",
    r"告诉过",
]

# 无查询模式（纯闲聊/陈述/感叹）
NO_QUERY_PATTERNS: list[str] = [
    r"^[好的嗯哦啊哈是的对]+$",
    r"今天天气",
    r"真不错",
    r"太.*了",
    r"好耶",
    r"哈哈",
    r"嘿嘿",
    r"好的[！!。.]?$",
    r"知道了",
    r"明白",
    r"懂了",
]


@dataclass
class IntentResult:
    """意图分类结果。"""

    label: str = "no_query"  # "query" | "no_query"
    confidence: float = 0.0
    source: str = "default"  # "rule" | "llm_fallback" | "llm_error" | "default"
    matched_patterns: list[str] = field(default_factory=list)


class IntentClassifier:
    """意图分类器。

    依赖注入：
    - llm_bridge: LLMBridge（用于 LLM 兜底和校准）
    - calibration_interval: 每 N 条消息触发一次校准
    """

    def __init__(
        self,
        llm_bridge: Any | None = None,
        calibration_interval: int = 20,
    ) -> None:
        self._llm_bridge = llm_bridge
        self._calibration_interval = calibration_interval
        self._message_count = 0
        self._calibration_mismatches: list[dict] = []
        self._compiled_query = [re.compile(p, re.IGNORECASE) for p in QUERY_PATTERNS]
        self._compiled_no_query = [re.compile(p, re.IGNORECASE) for p in NO_QUERY_PATTERNS]

    def _rule_classify(self, text: str) -> IntentResult:
        """第一层——规则分类。"""
        if not text or not text.strip():
            return IntentResult(label="no_query", confidence=0.0, source="default")

        matched_query: list[str] = []
        matched_no_query: list[str] = []

        for i, pattern in enumerate(self._compiled_query):
            if pattern.search(text):
                matched_query.append(QUERY_PATTERNS[i])

        for i, pattern in enumerate(self._compiled_no_query):
            if pattern.search(text):
                matched_no_query.append(NO_QUERY_PATTERNS[i])

        # 查询关键词命中
        if matched_query:
            return IntentResult(
                label="query",
                confidence=0.9,
                source="rule",
                matched_patterns=matched_query,
            )

        # 无查询模式命中
        if matched_no_query:
            return IntentResult(
                label="no_query",
                confidence=0.85,
                source="rule",
                matched_patterns=matched_no_query,
            )

        # 未命中任何模式，低置信度
        return IntentResult(
            label="no_query",
            confidence=0.3,
            source="rule",
            matched_patterns=[],
        )

    async def _llm_fallback(self, text: str) -> IntentResult:
        """第二层——LLM 兜底。"""
        if self._llm_bridge is None:
            return IntentResult(
                label="no_query",
                confidence=0.0,
                source="default",
            )

        try:
            label = await self._llm_bridge.classify_intent(text)
            if label not in ("query", "no_query"):
                label = "no_query"
            return IntentResult(
                label=label,
                confidence=1.0,
                source="llm_fallback",
            )
        except Exception as e:
            logger.warning("intent_llm_fallback_error: %s", e)
            return IntentResult(
                label="no_query",
                confidence=0.0,
                source="llm_error",
            )

    async def _maybe_calibrate(self, text: str, rule_result: IntentResult) -> None:
        """第三层——周期校准（异步，不阻塞实时路由）。"""
        self._message_count += 1
        if self._message_count % self._calibration_interval != 0:
            return
        if self._llm_bridge is None:
            return

        try:
            llm_label = await self._llm_bridge.classify_intent(text)
            if llm_label not in ("query", "no_query"):
                llm_label = "no_query"

            if llm_label != rule_result.label:
                self._calibration_mismatches.append({
                    "text_preview": text[:50],
                    "rule_label": rule_result.label,
                    "llm_label": llm_label,
                    "rule_confidence": rule_result.confidence,
                })
                logger.info(
                    "intent_calibration_mismatch: rule=%s llm=%s",
                    rule_result.label, llm_label,
                )
        except Exception as e:
            logger.warning("intent_calibration_error: %s", e)

    async def classify(self, text: str) -> IntentResult:
        """分类入口。"""
        rule_result = self._rule_classify(text)

        # 高置信度直接返回
        if rule_result.confidence >= 0.8:
            # 周期校准（异步，不阻塞）
            asyncio.create_task(self._maybe_calibrate(text, rule_result))
            return rule_result

        # 低置信度走 LLM 兜底
        if self._llm_bridge is None:
            # 无 LLM bridge，直接返回规则结果（低置信度）
            asyncio.create_task(self._maybe_calibrate(text, rule_result))
            return rule_result

        llm_result = await self._llm_fallback(text)
        # 校准仍以规则结果为基准
        asyncio.create_task(self._maybe_calibrate(text, rule_result))
        return llm_result

    @property
    def calibration_mismatches(self) -> list[dict]:
        """获取校准偏差记录。"""
        return self._calibration_mismatches
