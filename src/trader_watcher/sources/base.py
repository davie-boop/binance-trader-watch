"""数据源抽象：业务逻辑只依赖此协议，不依赖具体厂商。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import PositionSnapshot


class SourceError(RuntimeError):
    """数据源请求或解析失败。"""


@runtime_checkable
class PositionSource(Protocol):
    """持仓数据源协议。

    任何实现只需满足这两个异步方法，即可插入监控服务。
    """

    async def fetch_positions(self, trader_id: str) -> PositionSnapshot:
        """拉取指定交易员的最新持仓快照。

        Args:
            trader_id: 交易员 / portfolio id。

        Returns:
            持仓快照。

        Raises:
            SourceError: 网络失败、鉴权失败或响应格式异常。
        """
        ...

    async def aclose(self) -> None:
        """释放底层连接资源。"""
        ...
