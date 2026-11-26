"""实时看板核心逻辑测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trader_watcher.live import fetch_latest, format_order
from trader_watcher.models import OrderRecord
from trader_watcher.sources.kopyon import HistoryPage
from trader_watcher.storage import PositionStore


def make_order(i: int) -> OrderRecord:
    """构造一条可预测 uid 的订单。"""
    return OrderRecord.from_api(
        {
            "symbol": f"COIN{i}USDT",
            "side": "BUY",
            "positionSide": "BOTH",
            "price": 100.0 + i,
            "quantity": 1.0,
            "createdAt": 1700000000000 + i * 1000,
            "updatedAt": 1700000000000 + i * 1000,
            "orderType": "MARKET",
        }
    )


def test_format_order_shows_beijing_time() -> None:
    """订单行显示的是北京时间，与币安网页一致。"""
    order = OrderRecord.from_api(
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": 50000.0,
            "quantity": 0.5,
            "createdAt": 1795650989796,  # UTC 11-25 23:56:29 -> 北京 11-26 07:56:29
            "updatedAt": 1795650989796,
            "orderType": "MARKET",
        }
    )
    assert format_order(order).startswith("11-26 07:56:29")


class FakeSource:
    """返回固定订单列表的假数据源。"""

    def __init__(self, orders: list[OrderRecord]) -> None:
        self.orders = orders

    async def fetch_orders(self, trader_id: str, *, cursor: str | None = None) -> HistoryPage:
        """按最新在前返回假订单（与真实接口一致）。"""
        return HistoryPage(items=list(reversed(self.orders)), total=len(self.orders), next_cursor=None)

    async def fetch_closed_positions(self, trader_id: str, *, cursor: str | None = None) -> HistoryPage:
        """返回空平仓页。"""
        return HistoryPage(items=[], total=0, next_cursor=None)


@pytest.mark.asyncio
async def test_fetch_latest_limits_and_incremental(tmp_path: Path) -> None:
    """只取最新 N 条，且首轮全部视为新增、次轮无新增。"""
    store = PositionStore(tmp_path / "live.db")
    await store.open()
    source = FakeSource([make_order(i) for i in range(10)])

    all1, new1, _, _ = await fetch_latest(source, store, "t1", 5, kind="orders")
    assert len(all1) == 5
    assert len(new1) == 5
    assert "COIN9USDT" in all1[0]

    all2, new2, _, _ = await fetch_latest(source, store, "t1", 5, kind="orders")
    assert len(all2) == 5
    assert len(new2) == 0

    await store.close()
