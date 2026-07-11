"""测试鉴权逻辑。"""
import pytest
from app.adapters.http.auth import authenticate_gateway_request, AuthenticationError


class TestAuthenticate:
    def test_valid_key(self):
        authenticate_gateway_request(
            {"authorization": "Bearer valid-key"},
            "valid-key",
        )

    def test_missing_header(self):
        with pytest.raises(AuthenticationError):
            authenticate_gateway_request({}, "valid-key")

    def test_wrong_scheme(self):
        with pytest.raises(AuthenticationError):
            authenticate_gateway_request(
                {"authorization": "Basic valid-key"},
                "valid-key",
            )

    def test_wrong_key(self):
        with pytest.raises(AuthenticationError):
            authenticate_gateway_request(
                {"authorization": "Bearer wrong-key"},
                "valid-key",
            )

    def test_empty_server_key_rejected(self):
        """空配置的 API Key 应拒绝所有请求，防止误开放。"""
        with pytest.raises(AuthenticationError):
            authenticate_gateway_request(
                {"authorization": "Bearer anything"},
                "",
            )

    def test_case_insensitive_header(self):
        authenticate_gateway_request(
            {"Authorization": "Bearer valid-key"},
            "valid-key",
        )
