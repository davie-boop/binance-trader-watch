"""模型解析测试。"""

from datetime import UTC, datetime

from trader_watcher.models import Position, Side, to_local


def test_position_alias_parsing() -> None:
    """API 驼峰字段能正确映射到 snake_case 属性。"""
    payload = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "size": 1.5,
        "entryPrice": 50000.0,
        "markPrice": 51000.0,
        "leverage": 20,
        "marginMode": "ISOLATED",
        "unrealizedPnl": 1500.0,
        "roi": 0.02,
        "margin": 3750.0,
        "extraField": "ignored",
    }
    position = Position.from_api(payload)
    assert position.side is Side.LONG
    assert position.entry_price == 50000.0
    assert position.unrealized_pnl == 1500.0
    assert position.key == "BTCUSDT:LONG"


def test_to_local_converts_utc_to_beijing() -> None:
    """UTC 时间显示时加 8 小时，今天凌晨的交易不再被误认为昨天。"""
    utc = datetime(2026, 11, 25, 23, 56, 29, tzinfo=UTC)
    assert to_local(utc).strftime("%Y-%m-%d %H:%M:%S") == "2026-11-26 07:56:29"
    assert to_local(datetime(2026, 11, 25, 17, 9, 5, tzinfo=UTC)).strftime("%m-%d %H:%M") == "11-26 01:09"


def test_position_rejects_negative_size() -> None:
    """仓位数量必须为正数。"""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Position(symbol="BTCUSDT", side=Side.LONG, size=-1, entryPrice=100)
