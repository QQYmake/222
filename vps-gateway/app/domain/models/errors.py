"""上游模型调用错误类型。"""


class UpstreamError(Exception):
    """上游模型调用失败（非超时）。"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class UpstreamTimeout(Exception):
    """上游模型调用超时。"""
