"""云端无状态抓取测试：验证首次初始化与跨运行去重。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from trader_watcher.cloud import CloudState, run_cloud_fetch
from trader_watcher.config import Settings
from trader_watcher.models import OrderRecord


@pytest.fixture
def fake_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """用假数据源替换 KopyonSource，避免真实请求。"""

    class FakePage:
        def __init__(self, orders: list[OrderRecord]) -> None:
            self.items = orders

    class FakeSource:
        orders: ClassVar[list[OrderRecord]] = [
            OrderRecord.from_api(
                {
                    "symbol": f"COIN{i}USDT",
                    "side": "BUY",
                    "price": 100.0,
                    "quantity": 1.0,
                    "createdAt": 1795650989796 + i * 1000,
                    "updatedAt": 1795650989796 + i * 1000,
                    "orderType": "MARKET",
                }
            )
            for i in range(3)
        ]

        def __init__(self, config: object) -> None:
            pass

        async def fetch_orders(
            self,
            trader_id: str,
            *,
            cursor: str | None = None,
            rows: int = 20,
            start_time: int | None = None,
        ) -> FakePage:
            return FakePage(list(self.orders))

        async def fetch_closed_positions(
            self,
            trader_id: str,
            *,
            cursor: str | None = None,
            rows: int = 20,
            sort_by: str = "closed",
        ) -> FakePage:
            return FakePage([])

        async def aclose(self) -> None:
            pass

    from trader_watcher import cloud

    monkeypatch.setattr(cloud, "KopyonSource", FakeSource)
    # 关闭通知，避免真实推送
    monkeypatch.setattr(cloud, "build_notifier", lambda settings: None)


@pytest.mark.asyncio
async def test_cloud_fetch_initialize_then_only_new(
    tmp_path: Path, fake_source: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次运行只初始化不推送；第二次无新增；新增记录后才标记。"""
    state_path = str(tmp_path / "state.json")
    settings = Settings(trader_ids="t1", notify_channel="")

    first = await run_cloud_fetch(settings, state_path)
    assert first["initialized"] is False  # 报告的是运行前的状态
    assert CloudState.load(state_path).initialized is True
    assert len(CloudState.load(state_path).seen) == 3

    second = await run_cloud_fetch(settings, state_path)
    assert second["initialized"] is True
    assert second["new_orders"] == 0

    # 追加一条新订单后应识别为新增
    from trader_watcher import cloud

    cloud.KopyonSource.orders.insert(
        0,
        OrderRecord.from_api(
            {
                "symbol": "NEWUSDT",
                "side": "BUY",
                "price": 1.0,
                "quantity": 1.0,
                "createdAt": 1795650989796 + 999999,
                "updatedAt": 1795650989796 + 999999,
                "orderType": "MARKET",
            }
        ),
    )
    third = await run_cloud_fetch(settings, state_path)
    assert third["new_orders"] == 1
