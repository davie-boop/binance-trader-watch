"""实时交易记录看板：持续轮询，只展示最新 N 条，支持运行时切换交易员。"""

from __future__ import annotations

import asyncio
import time

from .models import ClosedPosition, OrderRecord, to_local
from .sources.kopyon import KopyonSource
from .storage import PositionStore

# 限价单可能挂单几天后才成交，按更新时间取最近订单时向前看一周
ORDER_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000


def format_order(order: OrderRecord) -> str:
    """把订单格式化为单行摘要。

    时间显示的是最后成交/变更时间（币安页面看到的成交时间），
    若与下单时间不一致则额外括号标注下单时间，避免误以为数据是旧的。

    Args:
        order: 订单记录。

    Returns:
        形如 ``"00:43:57 BUY  SOXLUSDT  price=25 qty=101.41 (下单 11-24 22:52:17)"`` 的字符串。
    """
    ts = to_local(order.updated_at).strftime("%m-%d %H:%M:%S")
    placed = ""
    if order.created_at != order.updated_at:
        placed = f" (下单 {to_local(order.created_at):%m-%d %H:%M:%S})"
    return (
        f"{ts}  {order.side.value:<4} {order.symbol:<14} "
        f"price={order.price:g} qty={order.quantity:g} type={order.order_type}{placed}"
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


async def collect_recent_orders(
    source: KopyonSource,
    trader_id: str,
    limit: int = 20,
) -> list[OrderRecord]:
    """按最后成交时间返回最近的订单。

    数据源默认按“下单时间”倒序返回第一页，但限价单可能挂单几天后才成交。
    因此额外拉一次最近一周有成交的订单，合并去重后按 ``updated_at`` 倒序，
    才能与币安页面看到的“最新成交”一致。

    Args:
        source: 数据源。
        trader_id: 交易员 id。
        limit: 最多返回多少条。

    Returns:
        按最后成交时间倒序排列的订单列表。
    """
    now_ms = int(time.time() * 1000)
    placed, recently_filled = await asyncio.gather(
        source.fetch_orders(trader_id),
        source.fetch_orders(trader_id, start_time=now_ms - ORDER_LOOKBACK_MS),
    )
    merged = {o.uid: o for o in [*placed.items, *recently_filled.items]}
    return sorted(
        merged.values(),
        key=lambda o: (o.updated_at, o.created_at),
        reverse=True,
    )[:limit]


async def collect_recent_closed(
    source: KopyonSource,
    trader_id: str,
    limit: int = 20,
) -> list[ClosedPosition]:
    """按平仓时间返回最近的持仓回合（显式要求服务端按 closed 排序）。"""
    page = await source.fetch_closed_positions(trader_id, sort_by="closed")
    return sorted(page.items, key=lambda c: c.closed_at, reverse=True)[:limit]


async def fetch_latest(
    source: KopyonSource,
    store: PositionStore,
    trader_id: str,
    limit: int,
    kind: str = "all",
) -> tuple[list[str], list[str], list[str], list[str]]:
    """拉取并落库最新记录，同时返回全部最新行与其中新增的行。

    订单按最后成交时间取最新 N 条，平仓按平仓时间取最新 N 条。
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

    if kind == "all":
        orders, closed = await asyncio.gather(
            collect_recent_orders(source, trader_id, limit),
            collect_recent_closed(source, trader_id, limit),
        )
    elif kind == "orders":
        orders = await collect_recent_orders(source, trader_id, limit)
    elif kind == "positions":
        closed = await collect_recent_closed(source, trader_id, limit)

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
