"""监控服务：编排拉取、差异检测、落库与通知。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .diff import diff_snapshots
from .models import PositionEvent
from .sources.base import PositionSource, SourceError
from .storage import PositionStore

logger = logging.getLogger(__name__)

EventCallback = Callable[[PositionEvent], Awaitable[None]]


class TraderWatcher:
    """交易员仓位监控器。"""

    def __init__(
        self,
        source: PositionSource,
        store: PositionStore,
        *,
        on_event: EventCallback | None = None,
    ) -> None:
        """初始化监控器。

        Args:
            source: 持仓数据源。
            store: 持久化仓库。
            on_event: 可选的异步事件回调，用于推送通知。
        """
        self._source = source
        self._store = store
        self._on_event = on_event

    async def check_once(self, trader_id: str) -> list[PositionEvent]:
        """检查单个交易员一次，返回本次产生的事件。

        Args:
            trader_id: 交易员 id。

        Returns:
            本次变更事件列表。

        Raises:
            SourceError: 数据源异常（由调用方决定是否重试）。
        """
        snapshot = await self._source.fetch_positions(trader_id)
        previous = await self._store.load_latest_snapshot(trader_id)
        events = diff_snapshots(previous, snapshot)

        await self._store.save_snapshot(snapshot)
        await self._store.save_events(events)

        for event in events:
            logger.info(
                "[%s] %s %s %s size=%s prev=%s",
                trader_id,
                event.event.value.upper(),
                event.symbol,
                event.side.value,
                event.size,
                event.previous_size,
            )
            if self._on_event is not None:
                try:
                    await self._on_event(event)
                except Exception:
                    # 通知失败不应中断采集主流程。
                    logger.exception("事件回调执行失败: %s", event)

        return events

    async def watch(self, trader_ids: list[str], interval: int) -> None:
        """按固定间隔持续轮询所有交易员。

        Args:
            trader_ids: 交易员 id 列表。
            interval: 轮询间隔（秒）。

        Raises:
            ValueError: 交易员列表为空。
        """
        if not trader_ids:
            raise ValueError("至少配置一个交易员 id（TW_TRADER_IDS）")

        logger.info("开始监控 %d 个交易员，间隔 %ds", len(trader_ids), interval)
        while True:
            for trader_id in trader_ids:
                try:
                    await self.check_once(trader_id)
                except SourceError as exc:
                    logger.warning("采集 %s 失败: %s", trader_id, exc)
                except Exception:
                    logger.exception("处理 %s 时发生未预期错误", trader_id)
            await asyncio.sleep(interval)
