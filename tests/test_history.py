"""订单与平仓记录模型测试。"""

from datetime import UTC, datetime

from trader_watcher.models import ClosedPosition, OrderRecord, OrderSide, Side


def test_order_record_from_api() -> None:
    """订单时间戳从毫秒正确转换，未知字段被忽略。"""
    payload = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "positionSide": "BOTH",
        "price": 50000.0,
        "quantity": 0.5,
        "createdAt": 1700000000123,
        "updatedAt": 1700000000456,
        "orderType": "MARKET",
        "realizedPnl": 0,
        "sourceDetails": {"marketType": "UM"},
        "unknown": "ignored",
    }
    order = OrderRecord.from_api(payload)
    assert order.side is OrderSide.BUY
    assert order.price == 50000.0
    assert order.created_at == datetime.fromtimestamp(1700000000.123, tz=UTC)
    assert order.market_type == "UM"
    assert "BTCUSDT:BUY" in order.uid


def test_order_uid_is_stable() -> None:
    """同一笔订单两次解析得到相同的去重键。"""
    payload = {
        "symbol": "ETHUSDT",
        "side": "SELL",
        "positionSide": "BOTH",
        "price": 3000.0,
        "quantity": 2.0,
        "createdAt": 1700000000000,
        "updatedAt": 1700000000000,
        "orderType": "LIMIT",
    }
    assert OrderRecord.from_api(payload).uid == OrderRecord.from_api(payload).uid


def test_closed_position_from_api() -> None:
    """平仓回合字段解析正确。"""
    payload = {
        "positionId": "123456",
        "symbol": "SOLUSDT",
        "side": "LONG",
        "status": "CLOSED",
        "marginMode": "CROSS",
        "leverage": 10,
        "roi": 0.15,
        "entryPrice": 100.0,
        "closePrice": 115.0,
        "openedAt": 1700000000000,
        "closedAt": 1700003600000,
        "updatedAt": 1700003600000,
        "closedVolume": 50.0,
        "maxOpenInterest": 50.0,
        "grossPnl": 750.0,
        "pnlCurrency": "USDT",
        "sourceDetails": {"marketType": "CM"},
    }
    pos = ClosedPosition.from_api(payload)
    assert pos.position_id == "123456"
    assert pos.side is Side.LONG
    assert pos.market_type == "CM"
    assert pos.gross_pnl == 750.0
    assert pos.closed_at == datetime.fromtimestamp(1700003600, tz=UTC)


def test_partially_closed_position_falls_back_to_updated_at() -> None:
    """部分平仓（PARTIALLY_CLOSED）无 closedAt 时，用 updatedAt 兜底。"""
    payload = {
        "positionId": "999",
        "symbol": "QQQUSDT",
        "side": "LONG",
        "status": "PARTIALLY_CLOSED",
        "entryPrice": 100.0,
        "closePrice": 115.0,
        "openedAt": 1700000000000,
        "updatedAt": 1700001800000,
        "closedVolume": 10.0,
        "grossPnl": 150.0,
        "sourceDetails": {"marketType": "UM"},
    }
    pos = ClosedPosition.from_api(payload)
    assert pos.status == "PARTIALLY_CLOSED"
    assert pos.closed_at == datetime.fromtimestamp(1700001800, tz=UTC)
