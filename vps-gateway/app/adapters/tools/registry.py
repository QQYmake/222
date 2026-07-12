"""ToolRegistry: VPS 内部工具注册表。

数据合同来源：架构文档 6.2 ToolRegistry。

职责:
  1. 校验工具名唯一
  2. 根据运行环境过滤测试工具
  3. 向模型输出允许工具的 Schema
  4. 按名称解析执行器

约束:
  - 只有服务器启动代码可以注册工具
  - 前端请求中的 tools、tool_choice 不作为可信工具来源
  - 模型只能看到 Registry 当前允许暴露的工具
"""
from __future__ import annotations

from app.domain.models.tool import ToolDefinition, ToolExecutor
from app.infrastructure.logging import get_logger


class ToolRegistry:
    """VPS 内部工具注册表。"""

    def __init__(self, test_tools_enabled: bool = False):
        self._definitions: dict[str, ToolDefinition] = {}
        self._executors: dict[str, ToolExecutor] = {}
        self._test_tools_enabled = test_tools_enabled
        self._logger = get_logger("tool_registry")

    def register(self, definition: ToolDefinition, executor: ToolExecutor) -> None:
        """注册一个工具。

        指令:
          1. 校验工具名唯一
          2. 保存定义和执行器
        """
        if definition.name in self._definitions:
            raise ValueError(f"duplicate tool name: {definition.name}")
        self._definitions[definition.name] = definition
        self._executors[definition.name] = executor
        self._logger.info("tool_registered", extra={
            "tool_name": definition.name,
            "enabled_in_production": definition.enabled_in_production,
        })

    def schemas(self) -> list[dict]:
        """返回允许暴露给模型的工具 Schema 列表。

        指令:
          1. 生产模式下过滤 enabled_in_production=False 的工具
          2. 格式为 OpenAI tools 数组
        """
        result: list[dict] = []
        for name, td in self._definitions.items():
            if not self._test_tools_enabled and not td.enabled_in_production:
                continue
            result.append({
                "type": "function",
                "function": {
                    "name": td.name,
                    "description": td.description,
                    "parameters": td.parameters,
                },
            })
        return result

    def resolve(self, name: str) -> ToolExecutor | None:
        """按名称解析执行器。

        返回 None 表示工具不存在或当前环境不可用。
        """
        td = self._definitions.get(name)
        if td is None:
            return None
        # 测试工具在生产模式下不可用
        if not self._test_tools_enabled and not td.enabled_in_production:
            return None
        return self._executors.get(name)

    def get_definition(self, name: str) -> ToolDefinition | None:
        """按名称获取工具定义。"""
        return self._definitions.get(name)
