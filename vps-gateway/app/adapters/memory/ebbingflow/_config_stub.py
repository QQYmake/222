"""Vendored ebbingflow config stub.

提供 ebbingflow 组件所需配置的默认值。
实际运行时配置通过构造注入（vendored 组件不引用 vps-gateway config.py）。
启动时从环境变量读取真实凭据。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MemoryConfig:
    time_decay_half_life_days: int = 7
    max_recall_results: int = 10
    recall_timeout_seconds: float = 5.0
    event_confidence_threshold: float = 0.6


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


def _build_memory_llm_config() -> MemoryLLMConfig:
    """从环境变量构建记忆 LLM 配置。"""
    return MemoryLLMConfig(
        intent_base_url=os.getenv("MEM_INTENT_BASE_URL", ""),
        intent_api_key=os.getenv("MEM_INTENT_API_KEY", ""),
        intent_model=os.getenv("MEM_INTENT_MODEL", ""),
        gen_base_url=os.getenv("MEM_GEN_BASE_URL", ""),
        gen_api_key=os.getenv("MEM_GEN_API_KEY", ""),
        gen_model=os.getenv("MEM_GEN_MODEL", ""),
        surf_base_url=os.getenv("MEM_SURF_BASE_URL", ""),
        surf_api_key=os.getenv("MEM_SURF_API_KEY", ""),
        surf_model=os.getenv("MEM_SURF_MODEL", ""),
        extract_base_url=os.getenv("MEM_EXTRACT_BASE_URL", ""),
        extract_api_key=os.getenv("MEM_EXTRACT_API_KEY", ""),
        extract_model=os.getenv("MEM_EXTRACT_MODEL", ""),
        persona_base_url=os.getenv("MEM_PERSONA_BASE_URL", ""),
        persona_api_key=os.getenv("MEM_PERSONA_API_KEY", ""),
        persona_model=os.getenv("MEM_PERSONA_MODEL", ""),
        saga_base_url=os.getenv("MEM_SAGA_BASE_URL", ""),
        saga_api_key=os.getenv("MEM_SAGA_API_KEY", ""),
        saga_model=os.getenv("MEM_SAGA_MODEL", ""),
        polish_base_url=os.getenv("MEM_POLISH_BASE_URL", ""),
        polish_api_key=os.getenv("MEM_POLISH_API_KEY", ""),
        polish_model=os.getenv("MEM_POLISH_MODEL", ""),
    )


def _build_embed_config() -> EmbedConfig:
    return EmbedConfig(
        embed_type=os.getenv("MEM_EMBED_TYPE", "local"),
        embed_model=os.getenv("MEM_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"),
        openai_base_url=os.getenv("MEM_EMBED_OPENAI_BASE_URL"),
        openai_api_key=os.getenv("MEM_EMBED_OPENAI_API_KEY"),
    )


# Singleton instances — 从环境变量初始化
memory_config = MemoryConfig()
memory_llm_config = _build_memory_llm_config()
embed_config = _build_embed_config()
identity_config = IdentityConfig()
llm_config = LLMConfig(
    base_url=os.getenv("MEM_GEN_BASE_URL", ""),
    api_key=os.getenv("MEM_GEN_API_KEY", ""),
    model=os.getenv("MEM_GEN_MODEL", ""),
)
neo4j_config = Neo4jConfig()
