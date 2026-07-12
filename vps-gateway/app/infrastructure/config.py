"""应用配置：从环境变量加载，启动时校验。"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Optional

_VALID_TOKEN_LIMIT_FIELDS = {"max_completion_tokens", "max_tokens"}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes", "on")


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class Config:
    """不可变配置对象。本地与 VPS 使用同一套代码，只替换环境变量。"""

    # --- 网关 ---
    gateway_host: str
    gateway_port: int
    gateway_api_key: str

    # --- 上游模型 ---
    upstream_base_url: str
    upstream_api_key: str
    upstream_model: str
    upstream_timeout_seconds: int
    upstream_token_limit_field: str

    # --- Sample ---
    sample_directory: str
    memory_char_budget: int

    # --- Outbox ---
    outbox_database_path: str

    # --- 主动回合 ---
    active_turn_enabled: bool
    active_turn_interval_minutes: int
    active_turn_instruction: str

    # --- 唤醒任务存储 ---
    wake_jobs_database_path: str

    # --- 默认参数 ---
    default_temperature: float
    default_max_output_tokens: int

    # --- v3 记忆引擎 ---
    memory_enabled: bool
    memory_db_path: str
    memory_retrieval_timeout: float
    memory_surface_interval: float
    memory_consolidation_hour: int
    mem_embed_type: str
    mem_embed_model: str
    mem_intent_base_url: str
    mem_intent_api_key: str
    mem_intent_model: str
    mem_gen_base_url: str
    mem_gen_api_key: str
    mem_gen_model: str
    mem_surf_base_url: str
    mem_surf_api_key: str
    mem_surf_model: str
    mem_extract_base_url: str
    mem_extract_api_key: str
    mem_extract_model: str
    mem_persona_base_url: str
    mem_persona_api_key: str
    mem_persona_model: str
    mem_saga_base_url: str
    mem_saga_api_key: str
    mem_saga_model: str
    mem_polish_base_url: str
    mem_polish_api_key: str
    mem_polish_model: str

    @classmethod
    def load_from_env(cls) -> "Config":
        """从环境变量加载配置。

        启动时校验固定上游模型与 UPSTREAM_TOKEN_LIMIT_FIELD 的组合；
        配置不明确时拒绝启动，而不是运行中猜测字段。
        """
        token_limit_field = os.environ.get(
            "UPSTREAM_TOKEN_LIMIT_FIELD", "max_completion_tokens"
        )
        if token_limit_field not in _VALID_TOKEN_LIMIT_FIELDS:
            raise ValueError(
                f"UPSTREAM_TOKEN_LIMIT_FIELD must be one of "
                f"{_VALID_TOKEN_LIMIT_FIELDS}, got: {token_limit_field}"
            )

        return cls(
            gateway_host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
            gateway_port=_parse_int(os.environ.get("GATEWAY_PORT", "8000"), 8000),
            gateway_api_key=os.environ.get("GATEWAY_API_KEY", ""),
            upstream_base_url=os.environ.get("UPSTREAM_BASE_URL", ""),
            upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
            upstream_model=os.environ.get("UPSTREAM_MODEL", ""),
            upstream_timeout_seconds=_parse_int(
                os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "30"), 30
            ),
            upstream_token_limit_field=token_limit_field,
            sample_directory=os.environ.get("SAMPLE_DIRECTORY", "./samples"),
            memory_char_budget=_parse_int(
                os.environ.get("MEMORY_CHAR_BUDGET", "12000"), 12000
            ),
            outbox_database_path=os.environ.get(
                "OUTBOX_DATABASE_PATH", "./data/outbox.sqlite3"
            ),
            active_turn_enabled=_parse_bool(
                os.environ.get("ACTIVE_TURN_ENABLED", "true")
            ),
            active_turn_interval_minutes=_parse_int(
                os.environ.get("ACTIVE_TURN_INTERVAL_MINUTES", "60"), 60
            ),
            active_turn_instruction=os.environ.get(
                "ACTIVE_TURN_INSTRUCTION",
                "检查当前状态，判断是否有值得主动告诉用户的内容。",
            ),
            wake_jobs_database_path=os.environ.get(
                "WAKE_JOBS_DATABASE_PATH", "./data/wake_jobs.sqlite3"
            ),
            default_temperature=_parse_float(
                os.environ.get("DEFAULT_TEMPERATURE", "0.7"), 0.7
            ),
            default_max_output_tokens=_parse_int(
                os.environ.get("DEFAULT_MAX_OUTPUT_TOKENS", "1200"), 1200
            ),
            # --- v3 记忆引擎 ---
            memory_enabled=_parse_bool(
                os.environ.get("MEMORY_ENABLED", "false")
            ),
            memory_db_path=os.environ.get(
                "MEMORY_DB_PATH", "./data/memory.sqlite3"
            ),
            memory_retrieval_timeout=_parse_float(
                os.environ.get("MEMORY_RETRIEVAL_TIMEOUT", "20.0"), 20.0
            ),
            memory_surface_interval=_parse_float(
                os.environ.get("MEMORY_SURFACE_INTERVAL", "3600.0"), 3600.0
            ),
            memory_consolidation_hour=_parse_int(
                os.environ.get("MEMORY_CONSOLIDATION_HOUR", "2"), 2
            ),
            mem_embed_type=os.environ.get("MEM_EMBED_TYPE", "local"),
            mem_embed_model=os.environ.get(
                "MEM_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
            ),
            mem_intent_base_url=os.environ.get("MEM_INTENT_BASE_URL", ""),
            mem_intent_api_key=os.environ.get("MEM_INTENT_API_KEY", ""),
            mem_intent_model=os.environ.get("MEM_INTENT_MODEL", ""),
            mem_gen_base_url=os.environ.get("MEM_GEN_BASE_URL", ""),
            mem_gen_api_key=os.environ.get("MEM_GEN_API_KEY", ""),
            mem_gen_model=os.environ.get("MEM_GEN_MODEL", ""),
            mem_surf_base_url=os.environ.get("MEM_SURF_BASE_URL", ""),
            mem_surf_api_key=os.environ.get("MEM_SURF_API_KEY", ""),
            mem_surf_model=os.environ.get("MEM_SURF_MODEL", ""),
            mem_extract_base_url=os.environ.get("MEM_EXTRACT_BASE_URL", ""),
            mem_extract_api_key=os.environ.get("MEM_EXTRACT_API_KEY", ""),
            mem_extract_model=os.environ.get("MEM_EXTRACT_MODEL", ""),
            mem_persona_base_url=os.environ.get("MEM_PERSONA_BASE_URL", ""),
            mem_persona_api_key=os.environ.get("MEM_PERSONA_API_KEY", ""),
            mem_persona_model=os.environ.get("MEM_PERSONA_MODEL", ""),
            mem_saga_base_url=os.environ.get("MEM_SAGA_BASE_URL", ""),
            mem_saga_api_key=os.environ.get("MEM_SAGA_API_KEY", ""),
            mem_saga_model=os.environ.get("MEM_SAGA_MODEL", ""),
            mem_polish_base_url=os.environ.get("MEM_POLISH_BASE_URL", ""),
            mem_polish_api_key=os.environ.get("MEM_POLISH_API_KEY", ""),
            mem_polish_model=os.environ.get("MEM_POLISH_MODEL", ""),
        )

    def validate(self) -> None:
        """启动时校验配置完整性。

        指令:
          1. UPSTREAM_MODEL 为空时拒绝启动
          2. ACTIVE_TURN_ENABLED=true 时 INTERVAL >= 1
          3. UPSTREAM_API_KEY 为空时拒绝启动
        """
        if not self.upstream_model:
            raise ValueError("UPSTREAM_MODEL must not be empty")
        if not self.upstream_api_key:
            raise ValueError("UPSTREAM_API_KEY must not be empty")
        if self.active_turn_enabled and self.active_turn_interval_minutes < 1:
            raise ValueError(
                "ACTIVE_TURN_INTERVAL_MINUTES must be >= 1 when active turn is enabled"
            )
