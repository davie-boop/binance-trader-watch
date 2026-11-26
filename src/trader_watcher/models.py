"""领域模型：仓位快照与变更事件。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 北京时间（UTC+8）。币安网页显示的是本时区，而 API 毫秒时间戳是 UTC，
# 显示层必须显式转换，否则今天凌晨到早上的交易会被误看成"昨天"。
BEIJING_TZ = timezone(timedelta(hours=8))


def _utc_now() -> datetime:
    """返回带时区的 UTC 当前时间。"""
    return datetime.now(UTC)


def to_local(value: datetime) -> datetime:
    """把 UTC 时间转成北京时间用于显示（数据库仍保存 UTC）。

    Args:
        value: UTC 时间；若是 naive datetime，按 UTC 处理。

    Returns:
        带 +08:00 时区的时间。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BEIJING_TZ)


class Side(StrEnum):
    """仓位方向。"""

    LONG = "LONG"
    SHORT = "SHORT"


class Position(BaseModel):
    """单个持仓。字段别名对应第三方 API 的驼峰命名。"""

    model_config = ConfigDict(populate_by_name=True)

    symbol: str
    side: Side
    size: float = Field(gt=0, description="仓位数量（正数，方向由 side 表示）")
    entry_price: float = Field(alias="entryPrice", ge=0)
    mark_price: float | None = Field(default=None, alias="markPrice")
    leverage: float | None = Field(default=None, alias="leverage")
    margin_mode: str | None = Field(default=None, alias="marginMode")
    unrealized_pnl: float | None = Field(default=None, alias="unrealizedPnl")
    roi: float | None = Field(default=None, alias="roi")
    margin: float | None = Field(default=None, alias="margin")

    @property
    def key(self) -> str:
        """仓位唯一键：同一标的同一方向视为同一个仓位。"""
        return f"{self.symbol}:{self.side.value}"

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Position:
        """从 API 原始字典构造仓位，忽略未知字段。"""
        return cls.model_validate(payload)


class PositionSnapshot(BaseModel):
    """某一时刻某交易员的全部持仓。"""

    trader_id: str
    hidden: bool = False
    positions: tuple[Position, ...] = ()
    captured_at: datetime = Field(default_factory=_utc_now)


class EventType(StrEnum):
    """仓位变更类型。"""

    OPENED = "opened"  # 新开仓
    INCREASED = "increased"  # 加仓
    DECREASED = "decreased"  # 减仓（未清零）
    CLOSED = "closed"  # 平仓
    HIDDEN = "hidden"  # 交易员隐藏了持仓（不等于全平）


class PositionEvent(BaseModel):
    """一次仓位变更，可直接落库或推送。"""

    trader_id: str
    event: EventType
    symbol: str
    side: Side
    size: float
    previous_size: float | None = None
    position: Position | None = None
    captured_at: datetime = Field(default_factory=_utc_now)


class OrderSide(StrEnum):
    """订单买卖方向。"""

    BUY = "BUY"
    SELL = "SELL"


def _from_millis(value: int | None) -> datetime | None:
    """把毫秒时间戳转为 UTC datetime。

    Args:
        value: 毫秒时间戳，None 时返回 None。

    Returns:
        对应的 datetime 或 None。
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


class OrderRecord(BaseModel):
    """一笔委托/成交记录（orderHistory）。"""

    model_config = ConfigDict(populate_by_name=True)

    symbol: str
    side: OrderSide
    position_side: str = Field(alias="positionSide", default="BOTH")
    price: float
    quantity: float
    order_type: str = Field(alias="orderType")
    realized_pnl: float = Field(alias="realizedPnl", default=0.0)
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    market_type: str = Field(alias="marketType", default="UM")

    @property
    def uid(self) -> str:
        """去重键：交易所订单没有独立 id，用标的+方向+价格+数量+创建时间组合。"""
        return (
            f"{self.symbol}:{self.side.value}:{self.position_side}:"
            f"{self.price}:{self.quantity}:{int(self.created_at.timestamp() * 1000)}"
        )

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> OrderRecord:
        """从 API 原始字典构造订单记录。

        Args:
            payload: API 返回的单条订单字典，时间戳为毫秒整数。

        Returns:
            OrderRecord 实例。

        Raises:
            ValueError: 时间戳字段格式非法。
        """
        data = dict(payload)
        for field in ("createdAt", "updatedAt"):
            raw = data.get(field)
            if isinstance(raw, (int, float)):
                data[field] = _from_millis(int(raw))
        details = data.get("sourceDetails") or {}
        if details.get("marketType"):
            data["marketType"] = details["marketType"]
        return cls.model_validate(data)


class ClosedPosition(BaseModel):
    """一次完整的开平仓回合（positionHistory）。"""

    model_config = ConfigDict(populate_by_name=True)

    position_id: str = Field(alias="positionId")
    symbol: str
    side: Side
    status: str
    margin_mode: str | None = Field(alias="marginMode", default=None)
    leverage: float | None = None
    roi: float | None = None
    entry_price: float = Field(alias="entryPrice")
    close_price: float = Field(alias="closePrice")
    closed_volume: float = Field(alias="closedVolume")
    max_open_interest: float | None = Field(alias="maxOpenInterest", default=None)
    gross_pnl: float = Field(alias="grossPnl", default=0.0)
    pnl_currency: str = Field(alias="pnlCurrency", default="USDT")
    opened_at: datetime = Field(alias="openedAt")
    closed_at: datetime = Field(alias="closedAt")
    updated_at: datetime | None = Field(alias="updatedAt", default=None)
    market_type: str = Field(alias="marketType", default="UM")

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> ClosedPosition:
        """从 API 原始字典构造平仓记录。

        Args:
            payload: API 返回的单条持仓字典，时间戳为毫秒整数。

        Returns:
            ClosedPosition 实例。

        Raises:
            ValueError: 时间戳字段格式非法。
        """
        data = dict(payload)
        # PARTIALLY_CLOSED（部分平仓）记录没有 closedAt，仓位尚未完全平掉；
        # 用 updatedAt 兜底作为最近的平仓时间，避免整批数据因单条异常而失败。
        if not data.get("closedAt") and data.get("updatedAt"):
            data["closedAt"] = data["updatedAt"]
        for field in ("openedAt", "closedAt", "updatedAt"):
            raw = data.get(field)
            if isinstance(raw, (int, float)):
                data[field] = _from_millis(int(raw))
        details = data.get("sourceDetails") or {}
        if details.get("marketType"):
            data["marketType"] = details["marketType"]
        return cls.model_validate(data)
