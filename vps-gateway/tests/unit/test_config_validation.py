"""M5 Task 2: 配置校验测试。"""
import os
import pytest

from app.infrastructure.config import Config


class TestConfigValidation:
    """Config.validate() 启动时校验。"""

    def _make_config(self, **overrides):
        defaults = dict(
            gateway_host="127.0.0.1",
            gateway_port=8000,
            gateway_api_key="test-key",
            upstream_base_url="https://api.example.com",
            upstream_api_key="sk-test",
            upstream_model="test-model",
            upstream_timeout_seconds=30,
            upstream_token_limit_field="max_completion_tokens",
            sample_directory="./samples",
            memory_char_budget=12000,
            outbox_database_path="./data/outbox.sqlite3",
            active_turn_enabled=True,
            active_turn_interval_minutes=60,
            active_turn_instruction="test",
            wake_jobs_database_path="./data/wake_jobs.sqlite3",
            default_temperature=0.7,
            default_max_output_tokens=1200,
            memory_enabled=False,
            memory_db_path="./data/memory.sqlite3",
            memory_retrieval_timeout=20.0,
            memory_surface_interval=3600.0,
            memory_consolidation_hour=2,
            mem_embed_type="local",
            mem_embed_model="paraphrase-multilingual-MiniLM-L12-v2",
            mem_intent_base_url="",
            mem_intent_api_key="",
            mem_intent_model="",
            mem_gen_base_url="",
            mem_gen_api_key="",
            mem_gen_model="",
            mem_surf_base_url="",
            mem_surf_api_key="",
            mem_surf_model="",
            mem_extract_base_url="",
            mem_extract_api_key="",
            mem_extract_model="",
            mem_persona_base_url="",
            mem_persona_api_key="",
            mem_persona_model="",
            mem_saga_base_url="",
            mem_saga_api_key="",
            mem_saga_model="",
            mem_polish_base_url="",
            mem_polish_api_key="",
            mem_polish_model="",
        )
        defaults.update(overrides)
        return Config(**defaults)

    def test_valid_config_passes(self):
        config = self._make_config()
        config.validate()  # 不抛异常

    def test_empty_model_rejected(self):
        config = self._make_config(upstream_model="")
        with pytest.raises(ValueError, match="UPSTREAM_MODEL"):
            config.validate()

    def test_empty_api_key_rejected(self):
        config = self._make_config(upstream_api_key="")
        with pytest.raises(ValueError, match="UPSTREAM_API_KEY"):
            config.validate()

    def test_active_turn_interval_zero_rejected(self):
        config = self._make_config(active_turn_enabled=True, active_turn_interval_minutes=0)
        with pytest.raises(ValueError, match="ACTIVE_TURN_INTERVAL"):
            config.validate()

    def test_active_turn_disabled_interval_zero_ok(self):
        """active_turn_enabled=False 时 interval=0 不校验。"""
        config = self._make_config(active_turn_enabled=False, active_turn_interval_minutes=0)
        config.validate()  # 不抛异常
