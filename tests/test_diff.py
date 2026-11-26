"""diff_snapshots 单元测试。"""

from datetime import UTC, datetime

from trader_watcher.diff import diff_snapshots
from trader_watcher.models import EventType, Position, PositionSnapshot, Side

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_position(size: float = 10.0, symbol: str = "BTCUSDT", side: Side = Side.LONG) -> Position:
    """构造测试仓位。"""
    return Position(symbol=symbol, side=side, size=size, entryPrice=50000)


def make_snapshot(*positions: Position, trader_id: str = "t1", hidden: bool = False) -> PositionSnapshot:
    """构造测试快照。"""
    return PositionSnapshot(
        trader_id=trader_id,
        hidden=hidden,
        positions=positions,
        captured_at=NOW,
    )


def test_first_snapshot_emits_opened() -> None:
    """首次采集时所有仓位都算新开仓。"""
    events = diff_snapshots(None, make_snapshot(make_position()))
    assert len(events) == 1
    assert events[0].event is EventType.OPENED
    assert events[0].previous_size is None


def test_new_position() -> None:
    """新增仓位产生 OPENED。"""
    old = make_snapshot(make_position(10))
    new = make_snapshot(make_position(10), make_position(5, "ETHUSDT"))
    events = diff_snapshots(old, new)
    assert [e.event for e in events] == [EventType.OPENED]
    assert events[0].symbol == "ETHUSDT"


def test_closed_position() -> None:
    """仓位消失产生 CLOSED，size 记录为 0。"""
    events = diff_snapshots(make_snapshot(make_position(10)), make_snapshot())
    assert len(events) == 1
    assert events[0].event is EventType.CLOSED
    assert events[0].size == 0.0
    assert events[0].previous_size == 10.0


def test_increased_and_decreased() -> None:
    """仓位变大/变小分别产生 INCREASED / DECREASED。"""
    increased = diff_snapshots(make_snapshot(make_position(10)), make_snapshot(make_position(15)))
    decreased = diff_snapshots(make_snapshot(make_position(10)), make_snapshot(make_position(4)))
    assert increased[0].event is EventType.INCREASED
    assert decreased[0].event is EventType.DECREASED


def test_tiny_change_is_ignored() -> None:
    """微小波动不应产生事件。"""
    events = diff_snapshots(make_snapshot(make_position(10)), make_snapshot(make_position(10.001)))
    assert events == []


def test_hidden_is_not_closed() -> None:
    """隐藏持仓不能被误判为平仓。"""
    events = diff_snapshots(
        make_snapshot(make_position(10)),
        make_snapshot(hidden=True),
    )
    assert len(events) == 1
    assert events[0].event is EventType.HIDDEN


def test_long_and_short_are_different_keys() -> None:
    """同标的多空分开跟踪。"""
    old = make_snapshot(make_position(10, side=Side.LONG))
    new = make_snapshot(make_position(10, side=Side.LONG), make_position(3, side=Side.SHORT))
    events = diff_snapshots(old, new)
    assert len(events) == 1
    assert events[0].side is Side.SHORT
