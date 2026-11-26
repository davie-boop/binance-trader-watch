"""快照差异检测：把两次持仓快照转换为离散事件。"""

from __future__ import annotations

from .models import EventType, Position, PositionEvent, PositionSnapshot, Side

# 浮点比较阈值：涨跌小于 0.01% 视为抖动，不产生事件。
SIZE_EPSILON = 1e-4


def _significantly_different(old: float, new: float) -> bool:
    """判断仓位数量是否有实质性变化。

    Args:
        old: 旧仓位数量。
        new: 新仓位数量。

    Returns:
        变化幅度超过阈值返回 True。
    """
    return abs(new - old) / max(abs(old), 1e-9) > SIZE_EPSILON


def diff_snapshots(
    previous: PositionSnapshot | None,
    current: PositionSnapshot,
) -> list[PositionEvent]:
    """对比两次快照，生成仓位变更事件。

    Args:
        previous: 上一次快照；首次采集时传 None，此时只会产生 OPENED 事件。
        current: 当前快照。

    Returns:
        按 symbol 排序的事件列表。

    Notes:
        交易员隐藏持仓时只发一个 HIDDEN 事件，不把消失的仓位误判为平仓。
    """
    if current.hidden:
        return [
            PositionEvent(
                trader_id=current.trader_id,
                event=EventType.HIDDEN,
                symbol="*",
                side=Side.LONG,
                size=0.0,
                captured_at=current.captured_at,
            )
        ]

    previous_map = {p.key: p for p in previous.positions} if previous else {}
    current_map = {p.key: p for p in current.positions}
    events: list[PositionEvent] = []

    for key in sorted(previous_map.keys() | current_map.keys()):
        old: Position | None = previous_map.get(key)
        new: Position | None = current_map.get(key)

        if old is None and new is not None:
            event_type = EventType.OPENED
        elif old is not None and new is None:
            event_type = EventType.CLOSED
        elif old is not None and new is not None and _significantly_different(old.size, new.size):
            event_type = EventType.INCREASED if new.size > old.size else EventType.DECREASED
        else:
            continue

        reference = new or old
        assert reference is not None  # 两个都为 None 的分支已在上面 continue
        events.append(
            PositionEvent(
                trader_id=current.trader_id,
                event=event_type,
                symbol=reference.symbol,
                side=reference.side,
                size=new.size if new else 0.0,
                previous_size=old.size if old else None,
                position=new,
                captured_at=current.captured_at,
            )
        )

    return events
