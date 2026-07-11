"""网关启动入口。

数据输入: 环境变量（.env 或系统环境）
数据输出: uvicorn 服务实例

指令:
  1. 加载 .env（如存在）
  2. 从环境变量构造 Config
  3. 校验配置
  4. 创建 FastAPI 应用
  5. uvicorn 启动监听
"""
from __future__ import annotations

import os
import sys

# 加载 .env 文件（如存在，不覆盖已有环境变量）
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(_env_path):
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

from app.infrastructure.config import Config
from app.application.app import create_app

config = Config.load_from_env()
config.validate()
app = create_app(config)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=config.gateway_host,
        port=config.gateway_port,
        log_config=None,
    )
