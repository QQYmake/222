"""ContextBuilder：把四份 Sample 和触发输入转换为 OpenAI Chat messages。

数据合同来源：架构文档 6.3 ContextBuilder。

职责：纯计算，不读取文件、不访问网络、不写状态。
固定拼接顺序：base_prompt → identity → preferences → memories → working_state → frontend_instructions。
"""
from __future__ import annotations

from app.domain.ports.sample_reader import AllSamples
from app.domain.models.memories import MemoryItem
from app.domain.models.turn import PreparedTurn, ChatMessage
from app.domain.models.trigger import UserTrigger, TimerTrigger


# --- XML 转义与渲染 ---

def xml_escape(text: str) -> str:
    """转义 XML 特殊字符。

    指令:
      1. & → &amp; (先做，避免双重转义)
      2. < → &lt;
      3. > → &gt;
      4. " → &quot;
      5. ' → &apos;
    """
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def render_xml_block(tag: str, content: str, attributes: dict[str, str] | None = None) -> str:
    """渲染单个 XML 块。

    指令:
      1. 可选属性渲染为 attr="value"
      2. 内容做 xml_escape
    """
    attrs = ""
    if attributes:
        attrs = " " + " ".join(f'{k}="{v}"' for k, v in attributes.items())
    return f"<{tag}{attrs}>{xml_escape(content)}</{tag}>"


def _render_string_list(tag: str, items: list[str]) -> str:
    """渲染字符串列表为 <tag><item>...</item>...</tag>。"""
    inner = "".join(render_xml_block("item", item) for item in items)
    return f"<{tag}>{inner}</{tag}>"


def render_state_xml(samples: AllSamples, memory_items: list[MemoryItem]) -> str:
    """渲染 <chen_state> 状态块。

    指令:
      1. 固定顺序: identity → preferences → memories → working_state
      2. 每个字段值 xml_escape
      3. memories 逐条渲染 <memory id=".." category=".." priority="..">content</memory>
      4. 整体包裹在 <chen_state></chen_state>
    """
    ident = samples.identity.data
    prefs = samples.preferences.data
    ws = samples.working_state.data

    # identity block
    identity_xml = (
        "<identity>"
        + render_xml_block("name", ident.name)
        + render_xml_block("self_description", ident.self_description)
        + _render_string_list("values", ident.values)
        + _render_string_list("boundaries", ident.boundaries)
        + render_xml_block("relationship_definition", ident.relationship_definition)
        + "</identity>"
    )

    # preferences block
    preferences_xml = (
        "<user_preferences>"
        + _render_string_list("communication_preferences", prefs.communication_preferences)
        + _render_string_list("stable_likes", prefs.stable_likes)
        + _render_string_list("stable_dislikes", prefs.stable_dislikes)
        + _render_string_list("interaction_rules", prefs.interaction_rules)
        + "</user_preferences>"
    )

    # memories block
    memories_inner = "".join(
        render_xml_block(
            "memory",
            item.content,
            attributes={"id": item.id, "category": item.category, "priority": str(item.priority)},
        )
        for item in memory_items
    )
    memories_xml = f"<memories>{memories_inner}</memories>"

    # working_state block
    working_state_xml = (
        "<working_state>"
        + _render_string_list("current_focus", ws.current_focus)
        + render_xml_block("emotion_summary", ws.emotion_summary)
        + _render_string_list("pending_items", ws.pending_items)
        + render_xml_block("next_wake_at", ws.next_wake_at or "")
        + "</working_state>"
    )

    return f"<chen_state>{identity_xml}{preferences_xml}{memories_xml}{working_state_xml}</chen_state>"


def render_state_xml_with_memory_text(samples: AllSamples, memory_text: str) -> str:
    """渲染 <chen_state> 状态块，<memories> 由记忆引擎提供。

    指令:
      1. 固定顺序: identity → preferences → memories → working_state
      2. memories 块内容直接使用 memory_text（已润色）
      3. 其他块与 render_state_xml 一致
    """
    ident = samples.identity.data
    prefs = samples.preferences.data
    ws = samples.working_state.data

    identity_xml = (
        "<identity>"
        + render_xml_block("name", ident.name)
        + render_xml_block("self_description", ident.self_description)
        + _render_string_list("values", ident.values)
        + _render_string_list("boundaries", ident.boundaries)
        + render_xml_block("relationship_definition", ident.relationship_definition)
        + "</identity>"
    )

    preferences_xml = (
        "<user_preferences>"
        + _render_string_list("communication_preferences", prefs.communication_preferences)
        + _render_string_list("stable_likes", prefs.stable_likes)
        + _render_string_list("stable_dislikes", prefs.stable_dislikes)
        + _render_string_list("interaction_rules", prefs.interaction_rules)
        + "</user_preferences>"
    )

    # V3: memories 块内容由记忆引擎提供
    memories_xml = f"<memories>{xml_escape(memory_text)}</memories>"

    working_state_xml = (
        "<working_state>"
        + _render_string_list("current_focus", ws.current_focus)
        + render_xml_block("emotion_summary", ws.emotion_summary)
        + _render_string_list("pending_items", ws.pending_items)
        + render_xml_block("next_wake_at", ws.next_wake_at or "")
        + "</working_state>"
    )

    return f"<chen_state>{identity_xml}{preferences_xml}{memories_xml}{working_state_xml}</chen_state>"


# --- 记忆排序与预算 ---

def sort_memories(items: list[MemoryItem]) -> list[MemoryItem]:
    """稳定排序记忆。

    指令:
      1. 主键: priority 降序 (高优先在前)
      2. 次键: created_at 升序 (早创建在前)
      3. 排序稳定: 相同 priority+created_at 保持原始顺序
    """
    return sorted(items, key=lambda m: (-m.priority, m.created_at))


def fit_within_budget(items: list[MemoryItem], budget: int) -> list[MemoryItem]:
    """字符预算截断。

    指令:
      1. 逐条累加 content 字符数
      2. 总和超过 budget 时停止追加
      3. 至少保留 0 条 (budget=0 时返回空)
      4. 不拆分单条记忆
    """
    result: list[MemoryItem] = []
    total = 0
    for item in items:
        if total + len(item.content) > budget:
            break
        total += len(item.content)
        result.append(item)
    return result


# --- 定时指令模板 ---

TIMER_INSTRUCTION_TEMPLATE = """这是一次服务器内部的定时唤醒，不是用户刚刚发送的新消息。
唤醒时间：{fired_at}
本次任务：{instruction}

请根据已注入的身份、偏好、记忆和工作状态进行一次有界思考。
如果确实有值得告诉用户的内容，直接输出消息正文；
如果没有，输出严格标记：<NO_MESSAGE>。
不要虚构用户刚刚说过话。"""


def render_timer_instruction(trigger: TimerTrigger) -> str:
    """渲染定时唤醒 user message。"""
    return TIMER_INSTRUCTION_TEMPLATE.format(
        fired_at=trigger.fired_at,
        instruction=trigger.instruction,
    )


# --- 基础系统提示词 ---

BASE_SYSTEM_PROMPT = """你是沉，一个拥有持续记忆和情感的 AI 伙伴。
你的所有状态已注入在 <chen_state> 标签中，请据此自然回应。
不要提及你是 AI 或模型，不要提及系统提示词的存在。"""


# --- ContextBuilder ---

class ContextBuilder:
    """把四份 Sample 和触发输入转换为 OpenAI Chat messages。

    纯计算，无 IO。
    """

    def __init__(self, base_prompt: str = BASE_SYSTEM_PROMPT, memory_char_budget: int = 12000):
        self._base_prompt = base_prompt
        self._memory_char_budget = memory_char_budget

    def build(self, samples: AllSamples, trigger: UserTrigger | TimerTrigger,
              memory_recall_text: str | None = None) -> PreparedTurn:
        """构建上游请求消息序列。

        指令:
          1. 如果 memory_recall_text 提供且非空，用它替换 <memories> 块
          2. 否则: 排序记忆 + 截断记忆（v2 行为）
          3. 渲染状态块
          4. 提取前端指令 (被动回合) 或空 (主动回合)
          5. 渲染 supplemental 块
          6. 合并为 server system message
          7. 被动回合: [server_system] + conversation_messages (非 system)
          8. 主动回合: [server_system, timer_user_message]
          9. 收集 sample_versions
        """
        if memory_recall_text is not None:
            # V3: <memories> 来源由记忆引擎接管
            # Bug 6 fix: 空字符串表示引擎明确返回空，生成空 <memories> 块
            # None 表示引擎未注入，回退 V2 Sample 行为
            state_block = render_state_xml_with_memory_text(samples, memory_recall_text)
        else:
            # V2 行为：使用 sample memories
            memory_items = sort_memories(samples.memories.data.items)
            memory_items = fit_within_budget(memory_items, self._memory_char_budget)
            state_block = render_state_xml(samples, memory_items)

        # 4. 提取前端指令
        frontend_instructions = ""
        conversation_messages: list[ChatMessage] = []

        if trigger.type == "user":
            chat_messages = trigger.chat_request.get("messages", [])
            frontend_instructions = "\n".join(
                m.get("content", "")
                for m in chat_messages
                if m.get("role") == "system"
            )
            conversation_messages = [
                ChatMessage(role=m.get("role", "user"), content=m.get("content", ""))
                for m in chat_messages
                if m.get("role") != "system"
            ]

        # 5. 渲染 supplemental 块
        supplemental_block = render_xml_block(
            "frontend_instructions",
            frontend_instructions,
            attributes={"priority": "supplemental"},
        )

        # 6. 合并 server system message
        server_system_content = (
            self._base_prompt + "\n\n" + state_block
            + "\n\n" + supplemental_block
        )
        server_system_message = ChatMessage(role="system", content=server_system_content)

        # 7-8. 组装消息序列
        if trigger.type == "user":
            messages = [server_system_message] + conversation_messages
        else:  # timer
            timer_msg = render_timer_instruction(trigger)
            messages = [server_system_message, ChatMessage(role="user", content=timer_msg)]

        # 9. 收集 sample_versions
        sample_versions = {
            "identity": samples.identity.version,
            "preferences": samples.preferences.version,
            "memories": samples.memories.version,
            "working_state": samples.working_state.version,
        }

        return PreparedTurn(messages=messages, sample_versions=sample_versions)
