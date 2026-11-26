"""SQLite 持久化：保存每次快照和每次变更事件。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from .models import (
    ClosedPosition,
    EventType,
    OrderRecord,
    Position,
    PositionEvent,
    PositionSnapshot,
    Side,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_meta (
    trader_id   TEXT PRIMARY KEY,
    hidden      INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id     TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    size          REAL NOT NULL,
    entry_price   REAL NOT NULL,
    mark_price    REAL,
    leverage      REAL,
    unrealized_pnl REAL,
    roi           REAL,
    margin        REAL,
    captured_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_trader_time
    ON position_snapshots(trader_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS position_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id     TEXT NOT NULL,
    event         TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    size          REAL NOT NULL,
    previous_size REAL,
    payload       TEXT,
    captured_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_trader_time
    ON position_events(trader_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS trade_orders (
    uid           TEXT PRIMARY KEY,
    trader_id     TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    position_side TEXT NOT NULL,
    price         REAL NOT NULL,
    quantity      REAL NOT NULL,
    order_type    TEXT NOT NULL,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    market_type   TEXT NOT NULL DEFAULT 'UM',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    synced_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_trader_time
    ON trade_orders(trader_id, created_at DESC);

CREATE TABLE IF NOT EXISTS closed_positions (
    position_id       TEXT PRIMARY KEY,
    trader_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    status            TEXT NOT NULL,
    margin_mode       TEXT,
    leverage          REAL,
    roi               REAL,
    entry_price       REAL NOT NULL,
    close_price       REAL NOT NULL,
    closed_volume     REAL NOT NULL,
    max_open_interest REAL,
    gross_pnl         REAL NOT NULL DEFAULT 0,
    pnl_currency      TEXT NOT NULL DEFAULT 'USDT',
    market_type       TEXT NOT NULL DEFAULT 'UM',
    opened_at         TEXT NOT NULL,
    closed_at         TEXT NOT NULL,
    updated_at        TEXT,
    synced_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_closed_trader_time
    ON closed_positions(trader_id, closed_at DESC);
"""


def _sync_now() -> datetime:
    """返回当前 UTC 时间，用于记录同步时刻。"""
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    """把时间转成带时区的 ISO 字符串。"""
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    """解析 ISO 时间字符串。"""
    return datetime.fromisoformat(value)


class PositionStore:
    """基于 aiosqlite 的仓位仓库。"""

    def __init__(self, db_path: Path) -> None:
        """初始化仓库。

        Args:
            db_path: SQLite 文件路径，父目录会自动创建。
        """
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """打开连接并建表。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        """关闭连接。"""
        if self._db is not None:
            await self._db.close()
            self._db = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """提供事务上下文，保证快照与事件原子写入。

        Yields:
            数据库连接。

        Raises:
            RuntimeError: 未调用 ``open()`` 就使用仓库。
        """
        if self._db is None:
            raise RuntimeError("请先调用 PositionStore.open()")
        try:
            yield self._db
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

    async def save_snapshot(self, snapshot: PositionSnapshot) -> None:
        """保存一次快照（含 hidden 标记）。

        Args:
            snapshot: 待保存的快照。
        """
        captured = _iso(snapshot.captured_at)
        async with self.transaction() as db:
            await db.execute(
                """INSERT INTO snapshot_meta(trader_id, hidden, captured_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(trader_id) DO UPDATE SET
                       hidden = excluded.hidden,
                       captured_at = excluded.captured_at""",
                (snapshot.trader_id, int(snapshot.hidden), captured),
            )
            if snapshot.positions:
                await db.executemany(
                    """INSERT INTO position_snapshots(
                           trader_id, symbol, side, size, entry_price, mark_price,
                           leverage, unrealized_pnl, roi, margin, captured_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            snapshot.trader_id,
                            p.symbol,
                            p.side.value,
                            p.size,
                            p.entry_price,
                            p.mark_price,
                            p.leverage,
                            p.unrealized_pnl,
                            p.roi,
                            p.margin,
                            captured,
                        )
                        for p in snapshot.positions
                    ],
                )

    async def load_latest_snapshot(self, trader_id: str) -> PositionSnapshot | None:
        """读取某交易员最近一次快照。

        Args:
            trader_id: 交易员 id。

        Returns:
            最近快照；无历史记录时返回 None。
        """
        assert self._db is not None, "请先调用 PositionStore.open()"
        async with self._db.execute(
            "SELECT hidden, captured_at FROM snapshot_meta WHERE trader_id = ?",
            (trader_id,),
        ) as cursor:
            meta = await cursor.fetchone()
        if meta is None:
            return None

        hidden, captured = bool(meta[0]), meta[1]
        async with self._db.execute(
            """SELECT symbol, side, size, entry_price, mark_price, leverage,
                      unrealized_pnl, roi, margin
               FROM position_snapshots
               WHERE trader_id = ? AND captured_at = ?""",
            (trader_id, captured),
        ) as cursor:
            rows = await cursor.fetchall()

        positions = tuple(
            Position(
                symbol=row[0],
                side=Side(row[1]),
                size=row[2],
                entry_price=row[3],
                mark_price=row[4],
                leverage=row[5],
                unrealized_pnl=row[6],
                roi=row[7],
                margin=row[8],
            )
            for row in rows
        )
        return PositionSnapshot(
            trader_id=trader_id,
            hidden=hidden,
            positions=positions,
            captured_at=_parse(captured),
        )

    async def save_events(self, events: list[PositionEvent]) -> None:
        """批量保存事件。

        Args:
            events: 事件列表。
        """
        if not events:
            return
        async with self.transaction() as db:
            await db.executemany(
                """INSERT INTO position_events(
                       trader_id, event, symbol, side, size, previous_size, payload, captured_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        e.trader_id,
                        e.event.value,
                        e.symbol,
                        e.side.value,
                        e.size,
                        e.previous_size,
                        e.position.model_dump_json() if e.position else None,
                        _iso(e.captured_at),
                    )
                    for e in events
                ],
            )

    async def recent_events(self, trader_id: str, limit: int = 20) -> list[PositionEvent]:
        """查询最近事件。

        Args:
            trader_id: 交易员 id。
            limit: 返回条数。

        Returns:
            按时间倒序排列的事件列表。
        """
        assert self._db is not None, "请先调用 PositionStore.open()"
        async with self._db.execute(
            """SELECT trader_id, event, symbol, side, size, previous_size, payload, captured_at
               FROM position_events WHERE trader_id = ?
               ORDER BY captured_at DESC, id DESC LIMIT ?""",
            (trader_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            PositionEvent(
                trader_id=row[0],
                event=EventType(row[1]),
                symbol=row[2],
                side=Side(row[3]),
                size=row[4],
                previous_size=row[5],
                position=Position.model_validate_json(row[6]) if row[6] else None,
                captured_at=_parse(row[7]),
            )
            for row in rows
        ]

    async def save_orders(self, trader_id: str, orders: list[OrderRecord]) -> int:
        """批量写入订单记录，已存在的自动跳过。

        Args:
            trader_id: 交易员 id。
            orders: 订单列表。

        Returns:
            本次实际新增的条数。
        """
        if not orders:
            return 0
        synced = _iso(_sync_now())
        rows = [
            (
                o.uid,
                trader_id,
                o.symbol,
                o.side.value,
                o.position_side,
                o.price,
                o.quantity,
                o.order_type,
                o.realized_pnl,
                o.market_type,
                _iso(o.created_at),
                _iso(o.updated_at),
                synced,
            )
            for o in orders
        ]
        async with self.transaction() as db:
            before = db.total_changes
            await db.executemany(
                """INSERT OR IGNORE INTO trade_orders(
                       uid, trader_id, symbol, side, position_side, price, quantity,
                       order_type, realized_pnl, market_type, created_at, updated_at, synced_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return db.total_changes - before

    async def save_closed_positions(
        self,
        trader_id: str,
        positions: list[ClosedPosition],
    ) -> int:
        """批量写入平仓回合，已存在的自动跳过。

        Args:
            trader_id: 交易员 id。
            positions: 平仓记录列表。

        Returns:
            本次实际新增的条数。
        """
        if not positions:
            return 0
        synced = _iso(_sync_now())
        rows = [
            (
                p.position_id,
                trader_id,
                p.symbol,
                p.side.value,
                p.status,
                p.margin_mode,
                p.leverage,
                p.roi,
                p.entry_price,
                p.close_price,
                p.closed_volume,
                p.max_open_interest,
                p.gross_pnl,
                p.pnl_currency,
                p.market_type,
                _iso(p.opened_at),
                _iso(p.closed_at),
                _iso(p.updated_at) if p.updated_at else None,
                synced,
            )
            for p in positions
        ]
        async with self.transaction() as db:
            before = db.total_changes
            await db.executemany(
                """INSERT OR IGNORE INTO closed_positions(
                       position_id, trader_id, symbol, side, status, margin_mode, leverage,
                       roi, entry_price, close_price, closed_volume, max_open_interest,
                       gross_pnl, pnl_currency, market_type, opened_at, closed_at,
                       updated_at, synced_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return db.total_changes - before

    async def known_order_uids(self, trader_id: str, candidates: set[str]) -> set[str]:
        """返回候选 uid 中已入库的那些。

        Args:
            trader_id: 交易员 id。
            candidates: 待检查的 uid 集合。

        Returns:
            已在数据库中的 uid 子集。
        """
        if not candidates:
            return set()
        assert self._db is not None, "请先调用 PositionStore.open()"
        placeholders = ",".join("?" for _ in candidates)
        async with self._db.execute(
            f"SELECT uid FROM trade_orders WHERE uid IN ({placeholders})",
            tuple(candidates),
        ) as cursor:
            rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def known_closed_ids(self, trader_id: str, candidates: set[str]) -> set[str]:
        """返回候选 position_id 中已入库的那些。

        Args:
            trader_id: 交易员 id。
            candidates: 待检查的 position_id 集合。

        Returns:
            已在数据库中的 position_id 子集。
        """
        if not candidates:
            return set()
        assert self._db is not None, "请先调用 PositionStore.open()"
        placeholders = ",".join("?" for _ in candidates)
        async with self._db.execute(
            f"SELECT position_id FROM closed_positions WHERE position_id IN ({placeholders})",
            tuple(candidates),
        ) as cursor:
            rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def recent_orders(self, trader_id: str, limit: int = 20) -> list[OrderRecord]:
        """查询最近的订单记录。

        Args:
            trader_id: 交易员 id。
            limit: 返回条数。

        Returns:
            按创建时间倒序排列的订单列表。
        """
        assert self._db is not None, "请先调用 PositionStore.open()"
        async with self._db.execute(
            """SELECT symbol, side, position_side, price, quantity, order_type,
                      realized_pnl, market_type, created_at, updated_at
               FROM trade_orders WHERE trader_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (trader_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            OrderRecord(
                symbol=r[0],
                side=r[1],
                positionSide=r[2],
                price=r[3],
                quantity=r[4],
                orderType=r[5],
                realizedPnl=r[6],
                marketType=r[7],
                createdAt=_parse(r[8]),
                updatedAt=_parse(r[9]),
            )
            for r in rows
        ]

    async def recent_closed_positions(
        self,
        trader_id: str,
        limit: int = 20,
    ) -> list[ClosedPosition]:
        """查询最近的平仓回合。

        Args:
            trader_id: 交易员 id。
            limit: 返回条数。

        Returns:
            按平仓时间倒序排列的列表。
        """
        assert self._db is not None, "请先调用 PositionStore.open()"
        async with self._db.execute(
            """SELECT position_id, symbol, side, status, margin_mode, leverage, roi,
                      entry_price, close_price, closed_volume, max_open_interest,
                      gross_pnl, pnl_currency, market_type, opened_at, closed_at, updated_at
               FROM closed_positions WHERE trader_id = ?
               ORDER BY closed_at DESC LIMIT ?""",
            (trader_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ClosedPosition(
                positionId=r[0],
                symbol=r[1],
                side=r[2],
                status=r[3],
                marginMode=r[4],
                leverage=r[5],
                roi=r[6],
                entryPrice=r[7],
                closePrice=r[8],
                closedVolume=r[9],
                maxOpenInterest=r[10],
                grossPnl=r[11],
                pnlCurrency=r[12],
                marketType=r[13],
                openedAt=_parse(r[14]),
                closedAt=_parse(r[15]),
                updatedAt=_parse(r[16]) if r[16] else None,
            )
            for r in rows
        ]
