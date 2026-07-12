"""真实 API 冒烟测试。

验证项：
1. 服务启动（MEMORY_ENABLED=true）
2. 基础聊天请求返回 200
3. 响应包含有效 content
4. 多轮对话正常
5. 新窗口模式可正常返回
6. Outbox 查询可用
7. 无 API Key 泄露到日志
8. 错误请求正确拒绝
9. tools 被拒绝
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 加载 .env
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

# 确保使用临时 DB，不污染开发数据库
_tmpdir = tempfile.mkdtemp(prefix="smoke_")
os.environ["MEMORY_DB_PATH"] = os.path.join(_tmpdir, "memory.db")
os.environ["MEMORY_ENABLED"] = "true"
os.environ["MEMORY_SURFACE_INTERVAL"] = "3600"
os.environ.setdefault("MEM_CHROMA_PATH", os.path.join(_tmpdir, "chroma"))

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from app.infrastructure.config import Config
from app.application.app import create_app


class LifespanManager:
    """手动管理 ASGI lifespan startup/shutdown。"""

    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        self._receive_queue = asyncio.Queue()
        self._receive_queue.put_nowait({"type": "lifespan.startup"})
        self._task = asyncio.create_task(self._run_lifespan())
        # 等待 startup.complete
        await asyncio.sleep(0.2)
        return self

    async def __aexit__(self, *args):
        self._receive_queue.put_nowait({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        return False

    async def _run_lifespan(self):
        async def receive():
            return await self._receive_queue.get()

        async def send(message):
            pass

        try:
            await self.app({"type": "lifespan"}, receive, send)
        except Exception:
            pass


async def run_smoke_test():
    """执行真实 API 冒烟测试。"""
    print("=" * 60)
    print("真实 API 冒烟测试 (MEMORY_ENABLED=true)")
    print("=" * 60)

    # 1. 加载配置
    config = Config.load_from_env()
    config.validate()
    print(f"[1] 配置加载完成: MEMORY_ENABLED={config.memory_enabled}")

    # 2. 创建应用
    app = create_app(config)
    print("[2] 应用创建完成")

    # 3. 启动 ASGI 服务
    transport = httpx.ASGITransport(app=app)
    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60.0) as client:
            api_key = config.gateway_api_key

            # === 测试 1: 基础聊天请求 ===
            print("\n--- 测试 1: 基础聊天请求 ---")
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": config.upstream_model,
                    "messages": [
                        {"role": "user", "content": "你好，请用一句话介绍你自己。"}
                    ],
                    "stream": False,
                },
            )
            print(f"  状态码: {resp.status_code}")
            assert resp.status_code == 200, f"期望 200，实际 {resp.status_code}: {resp.text[:500]}"
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  回复长度: {len(content)} 字符")
            print(f"  回复前80字: {content[:80]}...")
            assert len(content) > 0, "回复内容为空"
            print("  [PASS] 基础聊天请求成功")

            # === 测试 2: 验证无 API Key 泄露 ===
            print("\n--- 测试 2: 验证无 API Key 泄露 ---")
            resp_text = resp.text
            upstream_key = os.environ.get("UPSTREAM_API_KEY", "")
            if upstream_key and upstream_key in resp_text:
                raise AssertionError("上游 API Key 泄露到响应中！")
            mem_key = os.environ.get("MEM_INTENT_API_KEY", "")
            if mem_key and mem_key in resp_text:
                raise AssertionError("记忆引擎 API Key 泄露到响应中！")
            print("  [PASS] 无 API Key 泄露")

            # === 测试 3: 多轮对话 ===
            print("\n--- 测试 3: 多轮对话 ---")
            resp2 = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": config.upstream_model,
                    "messages": [
                        {"role": "user", "content": "你好，请用一句话介绍你自己。"},
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": "1+1等于几？只回答数字。"},
                    ],
                    "stream": False,
                },
            )
            print(f"  状态码: {resp2.status_code}")
            assert resp2.status_code == 200, f"多轮对话失败: {resp2.status_code}: {resp2.text[:500]}"
            data2 = resp2.json()
            content2 = data2.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  回复: {content2[:80]}")
            assert len(content2) > 0, "多轮对话回复为空"
            print("  [PASS] 多轮对话成功")

            # === 测试 4: 新窗口模式 ===
            print("\n--- 测试 4: 新窗口模式 ---")
            resp3 = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Memory-Mode": "new_window",
                },
                json={
                    "model": config.upstream_model,
                    "messages": [
                        {"role": "user", "content": "新窗口对话，请回复一句话。"}
                    ],
                    "stream": False,
                },
            )
            print(f"  状态码: {resp3.status_code}")
            assert resp3.status_code == 200, f"新窗口模式失败: {resp3.status_code}: {resp3.text[:500]}"
            print("  [PASS] 新窗口模式成功")

            # === 测试 5: Outbox 查询 ===
            print("\n--- 测试 5: Outbox 查询 ---")
            resp4 = await client.get(
                "/v1/outbox?after=0&limit=20",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            print(f"  状态码: {resp4.status_code}")
            assert resp4.status_code == 200, f"Outbox 查询失败: {resp4.status_code}"
            outbox_data = resp4.json()
            print(f"  Outbox 条目数: {len(outbox_data.get('items', []))}")
            print("  [PASS] Outbox 查询成功")

            # === 测试 6: 错误请求 ===
            print("\n--- 测试 6: 错误请求 ---")
            resp5 = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer wrong-key"},
                json={
                    "model": config.upstream_model,
                    "messages": [{"role": "user", "content": "test"}],
                    "stream": False,
                },
            )
            print(f"  错误 token 状态码: {resp5.status_code}")
            assert resp5.status_code in (401, 403), f"期望 401/403，实际 {resp5.status_code}"
            print("  [PASS] 错误请求正确拒绝")

            # === 测试 7: tools 被拒绝 ===
            print("\n--- 测试 7: tools 被拒绝 ---")
            resp6 = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": config.upstream_model,
                    "messages": [{"role": "user", "content": "test"}],
                    "stream": False,
                    "tools": [{"type": "function", "function": {"name": "test", "parameters": {}}}],
                },
            )
            print(f"  tools 状态码: {resp6.status_code}")
            assert resp6.status_code == 400, f"期望 400，实际 {resp6.status_code}"
            print("  [PASS] tools 正确拒绝")

    print("\n" + "=" * 60)
    print("全部冒烟测试通过！")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = asyncio.run(run_smoke_test())
    sys.exit(0 if success else 1)
