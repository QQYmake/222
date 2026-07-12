"""Vendored ebbingflow config stub.

提供 ebbingflow 组件所需配置的默认值。
实际运行时配置通过构造注入（vendored 组件不引用 vps-gateway config.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MemoryConfig:
    time_decay_half_life_days: int = 7
    max_recall_results: int = 10
    recall_timeout_seconds: float = 5.0


@dataclass
class MemoryLLMConfig:
    intent_base_url: str = ""
    intent_api_key: str = ""
    intent_model: str = ""
    gen_base_url: str = ""
    gen_api_key: str = ""
    gen_model: str = ""
    surf_base_url: str = ""
    surf_api_key: str = ""
    surf_model: str = ""
    extract_base_url: str = ""
    extract_api_key: str = ""
    extract_model: str = ""
    persona_base_url: str = ""
    persona_api_key: str = ""
    persona_model: str = ""
    saga_base_url: str = ""
    saga_api_key: str = ""
    saga_model: str = ""
    polish_base_url: str = ""
    polish_api_key: str = ""
    polish_model: str = ""


@dataclass
class EmbedConfig:
    embed_type: str = "local"
    embed_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    openai_base_url: Optional[str] = None
    openai_api_key: Optional[str] = None


@dataclass
class IdentityConfig:
    user_id: str = "user"
    assistant_id: str = "assistant"
    user_name: str = "用户"
    assistant_name: str = "AI助手"


@dataclass
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@dataclass
class Neo4jConfig:
    uri: str = ""
    user: str = ""
    password: str = ""


# Singleton instances with defaults
memory_config = MemoryConfig()
memory_llm_config = MemoryLLMConfig()
embed_config = EmbedConfig()
identity_config = IdentityConfig()
llm_config = LLMConfig()
neo4j_config = Neo4jConfig()
