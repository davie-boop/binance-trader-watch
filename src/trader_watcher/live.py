"""实时交易记录看板：持续轮询，只展示最新 N 条，支持运行时切换交易员。"""

from __future__ import annotations

import asyncio
from typing import Any

from .models import ClosedPosition, OrderRecord, to_local
from .sources.kopyon import KopyonSource
from .storage import PositionStore


def format_order(order: OrderRecord) -> str:
    """把订单格式化为单行摘要。

    Args:
        order: 订单记录。

    Returns:
        形如 ``"10:23:01 BUY  BTCUSDT  price=50000 qty=0.5"`` 的字符串。
    """
    ts = to_local(order.created_at).strftime("%m-%d %H:%M:%S")
    return (
        f"{ts}  {order.side.value:<4} {order.symbol:<14} "
        f"price={order.price:g} qty={order.quantity:g} type={order.order_type}"
    )


def format_closed(position: ClosedPosition) -> str:
    """把平仓回合格式化为单行摘要。

    Args:
        position: 平仓记录。

    Returns:
        含 ROI 与盈亏的单行字符串。
    """
    ts = to_local(position.closed_at).strftime("%m-%d %H:%M:%S")
    roi = (position.roi or 0) * 100
    return (
        f"{ts}  {position.symbol:<14} {position.side.value:<5} "
        f"entry={position.entry_price:g} close={position.close_price:g} "
        f"ROI={roi:+.1f}% PnL={position.gross_pnl:g} {position.pnl_currency}"
    )


async def fetch_latest(
    source: KopyonSource,
    store: PositionStore,
    trader_id: str,
    limit: int,
    kind: str = "all",
) -> tuple[list[str], list[str], list[str], list[str]]:
    """拉取并落库最新记录，同时返回全部最新行与其中新增的行。

    接口每页固定 20 条且按时间倒序，取第一页前 ``limit`` 条即可。
    ``save_*`` 用唯一键去重；这里在写入前先查已有 uid 做差集，
    保证新增行只包含真正的新记录。

    Args:
        source: 数据源。
        store: 存储层。
        trader_id: 交易员 id。
        limit: 只取最新的 N 条。
        kind: orders / positions / all，决定拉取哪类记录。

    Returns:
        (全部订单行, 新增订单行, 全部平仓行, 新增平仓行)。

    Raises:
        SourceError: 数据源请求失败。
    """
    orders: list[OrderRecord] = []
    closed: list[ClosedPosition] = []
    def newest_first(items: list[Any], key: str) -> list[Any]:
        """按时间戳字段倒序排列，确保取到的是真正最新的 N 条。"""
        return sorted(items, key=lambda x: getattr(x, key), reverse=True)

    if kind == "all":
        order_page, closed_page = await asyncio.gather(
            source.fetch_orders(trader_id),
            source.fetch_closed_positions(trader_id),
        )
        orders = newest_first(order_page.items, "created_at")[:limit]
        closed = newest_first(closed_page.items, "closed_at")[:limit]
    elif kind == "orders":
        orders = newest_first((await source.fetch_orders(trader_id)).items, "created_at")[:limit]
    elif kind == "positions":
        closed = newest_first((await source.fetch_closed_positions(trader_id)).items, "closed_at")[:limit]

    all_orders = [format_order(o) for o in orders]
    all_closed = [format_closed(c) for c in closed]

    known_orders = await store.known_order_uids(trader_id, {o.uid for o in orders})
    new_orders = [line for o, line in zip(orders, all_orders) if o.uid not in known_orders]

    known_closed = await store.known_closed_ids(trader_id, {c.position_id for c in closed})
    new_closed = [line for c, line in zip(closed, all_closed) if c.position_id not in known_closed]

    if orders:
        await store.save_orders(trader_id, orders)
    if closed:
        await store.save_closed_positions(trader_id, closed)

    return all_orders, new_orders, all_closed, new_closed


async def listen_stdin(queue: asyncio.Queue[str | None]) -> None:
    """在单个后台线程持续读取键盘输入，放入队列。

    只在启动时调用一次，避免每轮创建线程导致输入被旧线程抢走。

    Args:
        queue: 存放输入行的异步队列；EOF 时放入 None。
    """
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, input, "")
        except (EOFError, KeyboardInterrupt):
            await queue.put(None)
            return
        await queue.put(line)


async def next_command(queue: asyncio.Queue[str | None], timeout: float) -> str | None:
    """等待用户指令，超时返回 None（表示继续轮询）。

    Args:
        queue: 输入队列。
        timeout: 等待秒数。

    Returns:
        输入字符串；超时返回 None；EOF/退出返回空串。
    """
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except TimeoutError:
        return None
