"""SamplePublisher 预留接口。

数据合同来源：架构文档 6.2 SamplePublisher（预留接口）。

首版不提供实现、不提供 HTTP 入口、也不由任何首版模块调用。
未来实现必须校验版本并以原子方式发布；
任何失败都必须保留上一份有效 Sample。
"""
from __future__ import annotations

import abc

from app.domain.models.sample import SampleEnvelope


class SamplePublisher:
    """预留接口：未来供长驻状态计算区原子发布新 Sample。

    首版不提供实现、不提供 HTTP 入口、也不由任何首版模块调用。
    """

    def publish(self, sample: SampleEnvelope) -> int:
        """发布新 Sample 并返回新版本号。

        预留合同:
          1. 校验 sample 结构
          2. 校验 version > 当前版本
          3. 原子写入文件
          4. 返回新版本号
          5. 失败时保留旧文件

        首版不实现具体逻辑，直接抛 NotImplementedError。
        """
        raise NotImplementedError("SamplePublisher is reserved for phase 2")
