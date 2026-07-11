"""OpenAI 兼容错误格式。

数据合同来源：架构文档 6.6 / 7.1 错误映射。
"""
from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error(status: int, error_type: str, message: str) -> JSONResponse:
    """构造 OpenAI 兼容错误响应。

    格式: { "error": { "message": "...", "type": "...", "code": ... } }
    """
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": status,
            }
        },
    )
