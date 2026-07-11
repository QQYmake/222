"""ModelClient 端口接口。

数据合同来源：架构文档 6.4 OpenAIUpstreamClient。

隔离上游模型调用细节，TurnRunner 通过此端口调用模型。
"""
from __future__ import annotations

import abc

from app.domain.models.turn import ModelCompletionInput
from app.domain.models.chat_completion import ChatCompletionResponse


class ModelClient(abc.ABC):
    """上游模型调用端口。"""

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
