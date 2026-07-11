"""Outbox API Controller 集成测试。

验证基线：架构文档 12.4
  - 正常查询返回 items + next_cursor
  - 空页时 next_cursor = 传入的 after
  - limit clamp 到 1..100
  - after 默认 0
  - 鉴权失败返回 401
  - 非法 after/limit 返回 400

数据流:
  HTTP GET /v1/outbox?after=N&limit=M
    → authenticate_gateway_request
    → outbox_store.list_after(after, limit)
    → { items: [...], next_cursor: N }
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.http.outbox_controller import create_outbox_router
from app.domain.models.outbox import OutboxMessage, OutboxPage


def _make_msg(cursor: int, trigger_id: str = "timer:slot", content: str = "hello"):
    return OutboxMessage(
        cursor=cursor,
        event_id=f"evt-{cursor:03d}",
        trigger_id=f"{trigger_id}-{cursor}",
        created_at="2025-01-01T00:05:00Z",
        content=content,
        metadata={"model": "deepseek-chat"},
    )


@pytest.fixture
def mock_outbox_store():
    store = MagicMock()
    return store


@pytest.fixture
def app_and_client(mock_outbox_store):
    app = FastAPI()
    router = create_outbox_router(mock_outbox_store, "test-gateway-key")
    app.include_router(router)
    client = TestClient(app)
    return app, client


class TestOutboxQuery:
    """GET /v1/outbox 正常流程。"""

    def test_normal_query(self, app_and_client, mock_outbox_store):
        """正常查询 → 200 + items + next_cursor。"""
        msgs = [_make_msg(1), _make_msg(2), _make_msg(3)]
        mock_outbox_store.list_after.return_value = OutboxPage(
            items=msgs, next_cursor=3,
        )

        _, client = app_and_client
        resp = client.get(
            "/v1/outbox?after=0&limit=20",
            headers={"Authorization": "Bearer test-gateway-key"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3
        assert data["next_cursor"] == 3
        assert data["items"][0]["cursor"] == 1

    def test_default_after_is_zero(self, app_and_client, mock_outbox_store):
        """不传 after → 默认 0。"""
        mock_outbox_store.list_after.return_value = OutboxPage(
            items=[], next_cursor=0,
        )

        _, client = app_and_client
        resp = client.get(
            "/v1/outbox",
            headers={"Authorization": "Bearer test-gateway-key"},
        )

        assert resp.status_code == 200
        mock_outbox_store.list_after.assert_called_once_with(0, 20)

    def test_default_limit_is_20(self, app_and_client, mock_outbox_store):
        """不传 limit → 默认 20。"""
        mock_outbox_store.list_after.return_value = OutboxPage(
            items=[], next_cursor=0,
        )

        _, client = app_and_client
        resp = client.get(
            "/v1/outbox?after=5",
            headers={"Authorization": "Bearer test-gateway-key"},
        )

        assert resp.status_code == 200
        mock_outbox_store.list_after.assert_called_once_with(5, 20)

    def test_empty_page_keeps_cursor(self, app_and_client, mock_outbox_store):
        """空页 → next_cursor = 传入的 after。"""
        mock_outbox_store.list_after.return_value = OutboxPage(
            items=[], next_cursor=999,
        )

        _, client = app_and_client
        resp = client.get(
            "/v1/outbox?after=999&limit=20",
            headers={"Authorization": "Bearer test-gateway-key"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["next_cursor"] == 999

    def test_limit_passed_to_store(self, app_and_client, mock_outbox_store):
        """limit=50 传递给 store.list_after。"""
        mock_outbox_store.list_after.return_value = OutboxPage(
            items=[], next_cursor=0,
        )

        _, client = app_and_client
        resp = client.get(
            "/v1/outbox?after=0&limit=50",
            headers={"Authorization": "Bearer test-gateway-key"},
        )

        assert resp.status_code == 200
        mock_outbox_store.list_after.assert_called_once_with(0, 50)


class TestOutboxAuth:
    """鉴权。"""

    def test_missing_auth(self, app_and_client):
        """无 Authorization → 401。"""
        _, client = app_and_client
        resp = client.get("/v1/outbox")
        assert resp.status_code == 401
        assert resp.json()["error"]["type"] == "invalid_api_key"

    def test_wrong_key(self, app_and_client):
        """错误 key → 401。"""
        _, client = app_and_client
        resp = client.get(
            "/v1/outbox",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401


class TestOutboxValidation:
    """参数校验。"""

    def test_negative_after(self, app_and_client):
        """after=-1 → 400。"""
        _, client = app_and_client
        resp = client.get(
            "/v1/outbox?after=-1",
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 400

    def test_non_integer_after(self, app_and_client):
        """after=abc → 400。"""
        _, client = app_and_client
        resp = client.get(
            "/v1/outbox?after=abc",
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 400

    def test_non_integer_limit(self, app_and_client):
        """limit=xyz → 400。"""
        _, client = app_and_client
        resp = client.get(
            "/v1/outbox?limit=xyz",
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 400
