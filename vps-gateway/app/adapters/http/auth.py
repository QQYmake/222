"""鉴权逻辑。

数据合同来源：架构文档 6.6 Chat API Controller 指令 1。
"""


class AuthenticationError(Exception):
    """API Key 鉴权失败。"""


def authenticate_gateway_request(headers: dict[str, str], api_key: str) -> None:
    """校验网关 API Key。

    指令:
      1. 提取 Authorization: Bearer {token}
      2. 与配置的 GATEWAY_API_KEY 比较
      3. 不匹配 → AuthenticationError
      4. API Key 为空配置时也拒绝 (防止误开放)
    """
    if not api_key:
        raise AuthenticationError("Gateway API key is not configured")

    auth_header = headers.get("authorization") or headers.get("Authorization")
    if not auth_header:
        raise AuthenticationError("Missing Authorization header")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0] != "Bearer":
        raise AuthenticationError("Invalid authorization scheme")

    token = parts[1]
    if token != api_key:
        raise AuthenticationError("Invalid API key")
