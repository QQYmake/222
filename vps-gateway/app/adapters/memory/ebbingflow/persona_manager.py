"""PersonaManager 适配模块——Neo4j → SQLitePersonaStore。

适配自 ebbingflow memory/identity/manager.py：
  - Neo4j driver → 注入的 PersonaStore（SQLite）
  - Big Five/EFSTB 逻辑不变
  - 配置通过构造注入，不引用 vps-gateway config.py

注意：纯逻辑辅助函数（_extract_demographic_updates 等）直接复用。
Neo4j 查询替换为 PersonaStore 的 read/write 方法。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.adapters.memory.ebbingflow.identity_canonical import canonicalize_entity

logger = logging.getLogger(__name__)

# === 直接复用的常量 ===
ASSISTANT_ROLE_REWRITE_FIELDS = {
    "name", "role", "age", "gender", "persona", "relationship_to_user",
}
ASSISTANT_ROLE_CANDIDATES = [
    "secretary", "butler", "assistant", "companion", "私人助理", "助理", "管家", "秘书",
]
ASSISTANT_STYLE_HINTS = ["gentle", "calm", "charming", "warm"]
USER_ROLE_CANDIDATES = [
    "engineer", "developer", "designer", "product manager", "student", "teacher", "manager",
]
USER_PERSONALITY_MARKERS = ["I", "E"]
VALID_MBTI_LABELS = {
    "INTJ", "INTP", "INFJ", "INFP", "ISTJ", "ISTP", "ISFJ", "ISFP",
    "ENTJ", "ENTP", "ENFJ", "ENFP", "ESTJ", "ESTP", "ESFJ", "ESFP",
    "I", "E",
}
PERSONALITY_SOURCE_WEIGHTS = {
    "self_report": 1.2, "direct_user": 1.15, "behavior_observation": 1.08,
    "observation_extractor": 1.0, "episode_rollup": 0.95,
    "reported_by_others": 0.82, "fallback": 0.72,
}
NOISE_TEXT_HINTS_STRONG = (
    "just kidding", "kidding", "joke", "sarcasm",
    "开玩笑", "玩笑", "故意", "试探", "反话",
)
NOISE_TEXT_HINTS_WEAK = (
    "maybe", "probably", "guess", "可能", "大概", "也许", "猜",
)


# === 直接复用的辅助函数 ===

def _normalize_relationship_to_user(value: str, role_hint: str = "") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("user_") or raw in {"userassistant", "user_assistant"}:
        return "assistant_to_user"
    if raw in {"assistant_to_user", "assistant-user", "assistanttouser"}:
        return "assistant_to_user"
    return raw


def _extract_demographic_updates(text: str) -> dict:
    if not text:
        return {}
    raw = str(text).strip()
    updates = {}
    for pattern in [r"age[:\s]*(\d{1,3})", r"(\d{1,3})\s*years?\s*old"]:
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            updates["age"] = match.group(1)
            break
    gender_map = {"female": "female", "woman": "female", "male": "male", "man": "male"}
    for token, normalized in gender_map.items():
        if re.search(rf"\b{re.escape(token)}\b", raw, re.IGNORECASE):
            updates["gender"] = normalized
            break
    for pattern in [
        r"(?:occupation|job|role)[:\s]+([A-Za-z][A-Za-z0-9 _-]{1,40})",
        r"(?:i am|i'm|working as)\s+(?:a|an)?\s*([A-Za-z][A-Za-z0-9 _-]{1,40})",
    ]:
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if value and value.upper() not in {"I", "E"}:
                updates["occupation"] = value
                break
    return updates


def _extract_user_biography_hint(text: str, occupation: str = "") -> str:
    if not text:
        return ""
    raw = str(text).strip()
    for marker in ["我叫", "我的名字", "我是", "i am", "i'm", "my name is"]:
        if marker in raw.lower():
            match = re.search(rf"{re.escape(marker)}\s*([\u4e00-\u9fff]{2,4}|[A-Za-z]{2,20})", raw, re.IGNORECASE)
            if match:
                return match.group(1).strip().rstrip("。，.,！!？?")
    return ""


def _extract_user_personality_marker(text: str) -> dict:
    if not text:
        return {}
    raw = str(text).strip().upper()
    for label in VALID_MBTI_LABELS:
        if re.search(rf"\b{re.escape(label)}\b", raw):
            return {"mbti": label}
    return {}


def _extract_user_social_personality_hint(text: str) -> dict:
    if not text:
        return {}
    raw = str(text).strip().lower()
    if any(w in raw for w in ["外向", "开朗", "健谈", "extrovert", "outgoing"]):
        return {"social_orientation": "extrovert"}
    if any(w in raw for w in ["内向", "安静", "沉默", "introvert", "quiet"]):
        return {"social_orientation": "introvert"}
    return {}


def _infer_user_role(*texts: str) -> str:
    combined = " ".join(texts).lower()
    for role in USER_ROLE_CANDIDATES:
        if role in combined:
            return role
    return ""


def _normalize_assistant_gender(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"female", "woman", "女", "女性"}:
        return "female"
    if raw in {"male", "man", "男", "男性"}:
        return "male"
    return ""


def _infer_assistant_role(*texts: str) -> str:
    combined = " ".join(texts).lower()
    for role in ASSISTANT_ROLE_CANDIDATES:
        if role in combined:
            return role
    return ""


def _looks_like_role_label(text: str) -> bool:
    if not text:
        return False
    return any(text.lower() == r or text.lower() in r for r in ASSISTANT_ROLE_CANDIDATES)


def _looks_like_style_descriptor(text: str) -> bool:
    if not text:
        return False
    return any(text.lower() == s for s in ASSISTANT_STYLE_HINTS)


def _sanitize_assistant_role_updates(raw_updates: dict) -> dict:
    clean = {}
    for k, v in raw_updates.items():
        if k in ASSISTANT_ROLE_REWRITE_FIELDS and v:
            clean[k] = str(v).strip()
    return clean


def extract_assistant_role_rewrite(text: str) -> dict:
    if not text:
        return {}
    updates = {}
    role = _infer_assistant_role(text)
    if role:
        updates["role"] = role
    gender = _normalize_assistant_gender(text)
    if gender:
        updates["gender"] = gender
    return _sanitize_assistant_role_updates(updates)


def extract_user_self_name(text: str) -> str:
    return _extract_user_biography_hint(text)


def extract_user_profile_rewrite(text: str) -> dict:
    updates = {}
    updates.update(_extract_demographic_updates(text))
    updates.update(_extract_user_personality_marker(text))
    updates.update(_extract_user_social_personality_hint(text))
    name = extract_user_self_name(text)
    if name:
        updates["name"] = name
    role = _infer_user_role(text)
    if role:
        updates["occupation"] = role
    return {k: v for k, v in updates.items() if v}


def _is_user_self_profile_statement(text: str) -> bool:
    if not text:
        return False
    raw = str(text).strip().lower()
    markers = ["我叫", "我是", "我在", "我做", "我学", "i am", "i'm", "i work", "i study"]
    return any(m in raw for m in markers)


def extract_external_entity_names(text: str) -> List[str]:
    if not text:
        return []
    names = re.findall(r"[\u4e00-\u9fff]{2,4}|[A-Z][a-z]{2,}", text)
    return [n for n in names if n not in {"我", "你", "他", "她", "它"}]


# === 适配后的 PersonaManager ===

class PersonaManager:
    """人格管理器（SQLite 适配版）。

    适配自 ebbingflow PersonaManager：
    - Neo4j driver → 注入的 PersonaStore
    - Big Five/EFSTB 逻辑保留
    """

    def __init__(self, persona_store=None):
        self._store = persona_store

    async def close(self):
        pass

    def _canonicalize(self, name: str) -> str:
        return canonicalize_entity(name)

    @staticmethod
    def _clamp01(value: Any, default: float = 0.5) -> float:
        try:
            v = float(value)
            return max(0.0, min(1.0, v))
        except (TypeError, ValueError):
            return default

    async def get_user_profile(self, uid: str) -> Optional[Dict[str, Any]]:
        """读取用户人格画像。"""
        if self._store is None:
            return None
        try:
            profile = await self._store.read_profile(uid)
            if profile is None:
                return None
            big_five = json.loads(profile.get("big_five", "{}"))
            efstb = json.loads(profile.get("efstb", "{}"))
            aliases = json.loads(profile.get("aliases", "[]"))
            return {
                "actor_id": profile["actor_id"],
                "big_five": big_five,
                "efstb": efstb,
                "aliases": aliases,
                "updated_at": profile.get("updated_at"),
            }
        except Exception as exc:
            logger.warning("[PersonaManager] get_user_profile failed: %s", exc)
            return None

    async def write_profile(
        self,
        actor_id: str,
        big_five: Dict[str, float],
        efstb: Dict[str, float],
        aliases: List[str],
    ) -> None:
        """写入/更新人格画像。"""
        if self._store is None:
            return
        await self._store.write_profile(actor_id, big_five, efstb, aliases)

    async def write_observation(
        self,
        actor_id: str,
        observation: str,
        raw_observation: str,
        source_episode_ids: List[str],
    ) -> None:
        """写入人格观察。"""
        if self._store is None:
            return
        await self._store.write_observation(
            actor_id, observation, raw_observation, source_episode_ids
        )

    async def read_observations(self, actor_id: str) -> List[Dict[str, Any]]:
        """读取人格观察列表。"""
        if self._store is None:
            return []
        return await self._store.read_observations(actor_id)

    async def apply_user_profile_rewrite(self, uid: str, updates: dict) -> dict:
        """应用用户画像更新。"""
        if not updates:
            return {}
        existing = await self.get_user_profile(uid) or {
            "actor_id": uid,
            "big_five": {},
            "efstb": {},
            "aliases": [],
        }
        big_five = existing.get("big_five", {})
        efstb = existing.get("efstb", {})
        aliases = existing.get("aliases", [])

        if "name" in updates:
            name = updates["name"]
            if name and name not in aliases:
                aliases.append(name)

        if self._store is not None:
            await self._store.write_profile(uid, big_five, efstb, aliases)
        return {"updated_fields": list(updates.keys())}
