"""Kopyon 数据源：聚合币安跟单公开页面上的带单交易员数据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..config import SourceConfig
from ..models import ClosedPosition, OrderRecord, Position, PositionSnapshot
from .base import SourceError


@dataclass(slots=True)
class HistoryPage:
    """一页历史记录及其游标。"""

    items: list[Any]
    total: int
    next_cursor: str | None


def _is_retryable(exc: BaseException) -> bool:
    """只重试网络抖动、429 限流和 5xx，避免对 4xx 做无意义重试。

    Args:
        exc: 捕获到的异常。

    Returns:
        是否应该重试。
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class KopyonSource:
    """通过 REST 拉取交易员持仓、订单与平仓记录。

    注意：币安跟单页面本身约每 5 分钟刷新一次持仓，
    轮询间隔设成 30~60 秒已接近数据源上限。
    """

    def __init__(self, config: SourceConfig) -> None:
        """初始化 HTTP 客户端。

        Args:
            config: 数据源连接参数。
        """
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"X-API-KEY": config.api_key} if config.api_key else {},
            timeout=config.timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """带重试的 GET 请求。

        Args:
            path: 接口路径。
            params: 查询参数。

        Returns:
            解析后的 JSON 字典。

        Raises:
            SourceError: 鉴权失败、请求失败或 JSON 解析失败。
        """
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._config.max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    response = await self._client.get(path, params=params)
                    response.raise_for_status()
                    return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise SourceError("API Key 无效或无权限，请检查 TW_KOPYON_API_KEY") from exc
            # 服务端常在 4xx 响应体里给出具体原因（如组合已关闭、参数不合法），
            # 透传出来便于用户直接判断，而不是只看到裸露的状态码。
            detail = ""
            try:
                body = exc.response.json()
                err = body.get("error") or {}
                detail = err.get("message") or ""
            except ValueError:
                pass
            suffix = f": {detail}" if detail else ""
            raise SourceError(f"请求失败: HTTP {exc.response.status_code}{suffix}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"请求 {path} 失败: {exc}") from exc
        raise SourceError(f"请求 {path} 失败")  # pragma: no cover

    async def fetch_positions(self, trader_id: str) -> PositionSnapshot:
        """拉取交易员当前持仓。

        Args:
            trader_id: 跟单 portfolio id（在交易员主页 URL 中）。

        Returns:
            持仓快照；若交易员隐藏持仓，返回 ``hidden=True`` 的空快照。

        Raises:
            SourceError: 请求失败或响应格式异常。
        """
        payload = await self._get_json("/openPositions", {"traderId": trader_id})
        data = payload.get("data") or {}
        raw_positions = data.get("positions") or []
        try:
            positions = tuple(Position.from_api(item) for item in raw_positions)
        except Exception as exc:
            raise SourceError(f"持仓数据格式异常: {exc}") from exc

        return PositionSnapshot(
            trader_id=trader_id,
            hidden=bool(data.get("hidden", False)),
            positions=positions,
        )

    async def fetch_orders(
        self,
        trader_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> HistoryPage[OrderRecord]:
        """拉取订单/成交记录（单页）。

        Args:
            trader_id: 跟单 portfolio id。
            cursor: 分页游标，首次传 None。
            page_size: 每页条数。

        Returns:
            含订单列表、总数和下一页游标的结果。

        Raises:
            SourceError: 请求失败或响应格式异常。
        """
        params: dict[str, Any] = {"traderId": trader_id}
        if cursor:
            params["cursor"] = cursor
        payload = await self._get_json("/orderHistory", params)
        data = payload.get("data") or {}
        try:
            items = [OrderRecord.from_api(item) for item in data.get("orders") or []]
        except Exception as exc:
            raise SourceError(f"订单数据格式异常: {exc}") from exc

        return HistoryPage(
            items=items,
            total=int(data.get("total", 0)),
            next_cursor=data.get("nextCursor") or None,
        )

    async def fetch_closed_positions(
        self,
        trader_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> HistoryPage[ClosedPosition]:
        """拉取已平仓的完整持仓回合（单页）。

        Args:
            trader_id: 跟单 portfolio id。
            cursor: 分页游标，首次传 None。
            page_size: 每页条数。

        Returns:
            含平仓列表、总数和下一页游标的结果。

        Raises:
            SourceError: 请求失败或响应格式异常。
        """
        params: dict[str, Any] = {"traderId": trader_id}
        if cursor:
            params["cursor"] = cursor
        payload = await self._get_json("/positionHistory", params)
        data = payload.get("data") or {}
        try:
            items = [ClosedPosition.from_api(item) for item in data.get("positions") or []]
        except Exception as exc:
            raise SourceError(f"平仓数据格式异常: {exc}") from exc

        return HistoryPage(
            items=items,
            total=int(data.get("total", 0)),
            next_cursor=data.get("nextCursor") or None,
        )

    async def aclose(self) -> None:
        """关闭 HTTP 连接池。"""
        await self._client.aclose()
