"""ModelClient 端口接口。

数据合同来源：架构文档 6.1 AsyncModelClient。

隔离上游模型调用细节，TurnRunner 通过此端口调用模型。
v1 使用同步 ModelClient；v2 使用 AsyncModelClient，等待期间让出 HTTP 执行权。
"""
from __future__ import annotations

import abc

from app.domain.models.turn import ModelCompletionInput
from app.domain.models.chat_completion import ChatCompletionResponse


class ModelClient(abc.ABC):
    """上游模型调用端口（同步，v1 兼容）。"""

    @abc.abstractmethod
    def complete(self, input: ModelCompletionInput) -> ChatCompletionResponse:
        """调用上游模型完成对话。

        指令:
          1. 构造上游请求
          2. 使用服务器 API Key
          3. 固定 stream=false
          4. 校验响应
          5. 失败抛 UpstreamError / UpstreamTimeout
        """


class AsyncModelClient(abc.ABC):
    """上游模型调用端口（异步，v2）。

    数据合同来源：架构文档 6.1 AsyncModelClient。

    指令:
      1. 使用服务器固定上游与 API Key
      2. 异步发送 OpenAI Chat 请求
      3. 等待期间让出 HTTP 执行权
      4. 保留 assistant content、tool_calls、finish_reason
      5. 超时/协议错误转换成领域错误
    """

    @abc.abstractmethod
    async def complete(self, request: ModelCompletionInput) -> ChatCompletionResponse:
        """异步调用上游模型完成对话。"""

    @abc.abstractmethod
    async def start(self) -> None:
        """初始化共享异步 HTTP 客户端。"""

    @abc.abstractmethod
    async def close(self) -> None:
        """关闭共享异步 HTTP 客户端。"""
