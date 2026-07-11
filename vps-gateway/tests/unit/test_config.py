"""Config 模块测试。"""
import os
import pytest
from app.infrastructure.config import Config


class TestConfigLoad:
    """Config.load_from_env() 测试。"""

    def test_loads_sample_directory_from_env(self, monkeypatch):
        """SAMPLE_DIRECTORY 从环境变量读取。"""
        monkeypatch.setenv("SAMPLE_DIRECTORY", "/tmp/my-samples")
        config = Config.load_from_env()
        assert config.sample_directory == "/tmp/my-samples"

    def test_sample_directory_has_default(self, monkeypatch):
        """SAMPLE_DIRECTORY 缺失时使用默认值。"""
        monkeypatch.delenv("SAMPLE_DIRECTORY", raising=False)
        config = Config.load_from_env()
        assert config.sample_directory == "./samples"

    def test_loads_gateway_config(self, monkeypatch):
        """网关配置正确读取。"""
        monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
        monkeypatch.setenv("GATEWAY_PORT", "9001")
        config = Config.load_from_env()
        assert config.gateway_host == "0.0.0.0"
        assert config.gateway_port == 9001

    def test_upstream_token_limit_field_valid(self, monkeypatch):
        """UPSTREAM_TOKEN_LIMIT_FIELD 合法值通过。"""
        monkeypatch.setenv("UPSTREAM_TOKEN_LIMIT_FIELD", "max_completion_tokens")
        config = Config.load_from_env()
        assert config.upstream_token_limit_field == "max_completion_tokens"

    def test_upstream_token_limit_field_old_format(self, monkeypatch):
        """旧字段 max_tokens 也合法。"""
        monkeypatch.setenv("UPSTREAM_TOKEN_LIMIT_FIELD", "max_tokens")
        config = Config.load_from_env()
        assert config.upstream_token_limit_field == "max_tokens"

    def test_upstream_token_limit_field_invalid_raises(self, monkeypatch):
        """非法 UPSTREAM_TOKEN_LIMIT_FIELD 拒绝启动。"""
        monkeypatch.setenv("UPSTREAM_TOKEN_LIMIT_FIELD", "something_else")
        with pytest.raises(ValueError, match="UPSTREAM_TOKEN_LIMIT_FIELD"):
            Config.load_from_env()

    def test_active_turn_enabled_true(self, monkeypatch):
        """ACTIVE_TURN_ENABLED=true 正确解析。"""
        monkeypatch.setenv("ACTIVE_TURN_ENABLED", "true")
        config = Config.load_from_env()
        assert config.active_turn_enabled is True

    def test_active_turn_enabled_false(self, monkeypatch):
        """ACTIVE_TURN_ENABLED=false 正确解析。"""
        monkeypatch.setenv("ACTIVE_TURN_ENABLED", "false")
        config = Config.load_from_env()
        assert config.active_turn_enabled is False

    def test_memory_char_budget_int(self, monkeypatch):
        """MEMORY_CHAR_BUDGET 解析为 int。"""
        monkeypatch.setenv("MEMORY_CHAR_BUDGET", "8000")
        config = Config.load_from_env()
        assert config.memory_char_budget == 8000

    def test_config_is_immutable(self, monkeypatch):
        """Config 对象不可变。"""
        monkeypatch.setenv("SAMPLE_DIRECTORY", "/tmp/x")
        config = Config.load_from_env()
        with pytest.raises((AttributeError, TypeError)):
            config.sample_directory = "/other"
