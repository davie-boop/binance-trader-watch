"""云端无状态抓取：用可持久化的状态文件去重，适配 GitHub Actions 每次全新环境。

云端每次运行都是干净的容器，本地 SQLite 不会保留，因此这里改用
一个 JSON 状态文件记录已推送过的记录 uid。该文件会被 workflow 提交回仓库，
实现跨运行去重。首次运行时只记录当前记录、不推送，避免把历史全推一遍。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Settings, SourceConfig
from .live import collect_recent_closed, collect_recent_orders, format_closed, format_order
from .models import ClosedPosition, OrderRecord, to_local
from .notifier import build_message, build_notifier
from .sources.kopyon import KopyonSource

# 默认状态文件放在项目根，随仓库提交（不在 .gitignore 中）
DEFAULT_STATE_PATH = "cloud_state.json"
# 状态里最多保留多少条已推送 uid，防止文件无限增长
MAX_SEEN = 300


@dataclass(slots=True)
class CloudState:
    """跨运行去重的持久化状态。"""

    seen: list[str] = field(default_factory=list)
    initialized: bool = False
    # 按交易员记录是否已首次初始化：新加入的交易员首次只记录历史，不推送
    initialized_traders: list[str] = field(default_factory=list)
    updated_at: str = ""

    @classmethod
    def load(cls, path: str) -> CloudState:
        """从 JSON 文件读取状态；文件不存在时返回空状态。"""
        try:
            with open(path, encoding="utf-8") as f:
                data: dict[str, Any] = json.load(f)
        except (FileNotFoundError, ValueError):
            return cls()
        return cls(
            seen=list(data.get("seen") or []),
            initialized=bool(data.get("initialized", False)),
            initialized_traders=list(data.get("initialized_traders") or []),
            updated_at=str(data.get("updated_at", "")),
        )

    def save(self, path: str) -> None:
        """把状态写回 JSON 文件，seen 只保留最近 MAX_SEEN 条。"""
        self.seen = self.seen[-MAX_SEEN:]
        self.updated_at = to_local(datetime.now(UTC)).isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "seen": self.seen,
                    "initialized": self.initialized,
                    "initialized_traders": list(dict.fromkeys(self.initialized_traders)),
                    "updated_at": self.updated_at,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )


def merge_state_files(local_path: str, remote_path: str, output_path: str) -> int:
    """合并两份去重状态文件，返回合并后的 uid 数量。

    GitHub Actions 多个 run 可能基于同一旧 commit 各自更新状态文件。提交前
    把「本轮结果」与「远程最新版本」合并：seen 取并集（远程在前、本轮新增在后），
    initialized 取或，updated_at 取较新的一个，seen 仍受 MAX_SEEN 限制。

    Args:
        local_path: 本轮运行写出的状态文件。
        remote_path: 远程分支上最新的状态文件（可不存在）。
        output_path: 合并结果写入路径。

    Returns:
        合并后的 uid 条数。
    """

    def _read(path: str) -> dict[str, Any]:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    local = _read(local_path)
    remote = _read(remote_path)
    combined = list(remote.get("seen") or []) + list(local.get("seen") or [])
    seen = list(dict.fromkeys(combined))[-MAX_SEEN:]
    initialized_traders = list(
        dict.fromkeys(
            list(remote.get("initialized_traders") or [])
            + list(local.get("initialized_traders") or [])
        )
    )
    merged = {
        "seen": seen,
        "initialized": bool(remote.get("initialized") or local.get("initialized")),
        "initialized_traders": initialized_traders,
        "updated_at": max(
            str(remote.get("updated_at") or ""),
            str(local.get("updated_at") or ""),
        ),
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return len(seen)


def _order_uid(order: OrderRecord) -> str:
    """订单去重键，加前缀避免与平仓 uid 冲突。"""
    return f"order:{order.uid}"


def _closed_uid(position: ClosedPosition) -> str:
    """平仓去重键，用 positionId。"""
    return f"closed:{position.position_id}"


async def run_cloud_fetch(settings: Settings, state_path: str = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """执行一次云端抓取：去重后推送新记录，并更新状态文件。

    Args:
        settings: 运行时配置。
        state_path: 状态文件路径。

    Returns:
        本次执行摘要（初始化标记、新订单/平仓数、推送结果）。

    Raises:
        SourceError: 数据源请求失败。
    """
    state = CloudState.load(state_path)
    initialized_ids = set(state.initialized_traders)
    labels = settings.trader_label_map
    source = KopyonSource(
        SourceConfig(api_key=settings.kopyon_api_key, base_url=settings.kopyon_base_url)
    )
    notifier = build_notifier(settings)
    summary: dict[str, Any] = {
        "initialized": state.initialized,
        "new_orders": 0,
        "new_closed": 0,
        "push": "disabled",
        "traders": len(settings.trader_id_list),
        "new_traders": 0,
    }

    try:
        for tid in settings.trader_id_list:
            orders, closed = await asyncio.gather(
                collect_recent_orders(source, tid, 20),
                collect_recent_closed(source, tid, 20),
            )
            uids = [_order_uid(o) for o in orders] + [_closed_uid(c) for c in closed]
            seen = set(state.seen)

            if tid not in initialized_ids:
                # 新加入的交易员：首次只记录历史，避免刚添加就推送几十条旧记录
                initialized_ids.add(tid)
                state.initialized = True
                summary["new_traders"] += 1
                for uid in uids:
                    if uid not in seen:
                        state.seen.append(uid)
                        seen.add(uid)
                continue

            new_orders = [o for o in orders if _order_uid(o) not in seen]
            new_closed = [c for c in closed if _closed_uid(c) not in seen]
            summary["new_orders"] += len(new_orders)
            summary["new_closed"] += len(new_closed)

            if not (new_orders or new_closed):
                continue

            # 标记已见
            for uid in uids:
                if uid not in seen:
                    state.seen.append(uid)
                    seen.add(uid)

            if notifier is not None:
                msg = build_message(
                    tid,
                    [format_order(o) for o in new_orders],
                    [format_closed(c) for c in new_closed],
                    labels.get(tid, tid),
                )
                summary["push"] = await notifier.send(msg)
    finally:
        state.initialized_traders = list(initialized_ids)
        state.save(state_path)
        if notifier is not None:
            await notifier.aclose()
        await source.aclose()

    return summary
