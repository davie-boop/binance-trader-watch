"""熬鹰资本交易监控 - 图形界面入口（基于 pywebview，界面用 HTML 渲染）。

双击 exe 即可使用：抓取最新交易、测试微信推送、查看历史、自动刷新。
配置从 exe 同目录的 .env 读取，数据库也在同目录 data/ 下。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import webview


def app_dir() -> Path:
    """返回程序所在目录（打包后为 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = app_dir()
os.chdir(BASE)
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "src"))


def load_env() -> Path | None:
    """把 .env 里的 TW_* 配置注入环境变量，返回找到的路径。"""
    env_path = BASE / ".env"
    if not env_path.is_file():
        return None
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("TW_") and value:
            os.environ.setdefault(key, value)
    if not os.environ.get("TW_DB_PATH"):
        os.environ["TW_DB_PATH"] = str(BASE / "data" / "trader_positions.db")
    return env_path


ENV_PATH = load_env()

from cron_dispatch import dispatch_workflow, load_env_file
from trader_watcher.config import Settings, SourceConfig
from trader_watcher.live import fetch_latest
from trader_watcher.models import to_local
from trader_watcher.notifier import NotifyMessage, build_message, build_notifier
from trader_watcher.sources.kopyon import KopyonSource, TraderInfo
from trader_watcher.storage import PositionStore

Emit = Any  # JS 端的回调对象
CRON_ENV_FILE = BASE / ".cron.env"
SCHEDULE_CONFIG_FILE = BASE / "schedule_config.json"
DEFAULT_SCHEDULE_MINUTES = 15
MIN_SCHEDULE_MINUTES = 5
MAX_SCHEDULE_MINUTES = 1440


def load_schedule_interval() -> int:
    """读取上次保存的定时间隔；缺省或非法值时回落到 15 分钟。"""
    try:
        data = json.loads(SCHEDULE_CONFIG_FILE.read_text(encoding="utf-8"))
        value = int(data.get("interval_min", DEFAULT_SCHEDULE_MINUTES))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_SCHEDULE_MINUTES
    if MIN_SCHEDULE_MINUTES <= value <= MAX_SCHEDULE_MINUTES:
        return value
    return DEFAULT_SCHEDULE_MINUTES


def save_schedule_interval(minutes: int) -> None:
    """把定时间隔保存到程序目录，下次启动自动恢复。"""
    try:
        SCHEDULE_CONFIG_FILE.write_text(
            json.dumps({"interval_min": minutes}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # 只读目录时不影响本次运行


def _now() -> str:
    return to_local(datetime.now(UTC)).strftime("%H:%M:%S")


def _local_now() -> datetime:
    """返回带 UTC+8 时区的当前时间，供定时器倒计时使用。"""
    return to_local(datetime.now(UTC))


def _github_token() -> str | None:
    """优先读环境变量，其次读 exe 同目录 .cron.env。"""
    token = os.getenv("TW_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token and token.strip():
        return token.strip()
    return load_env_file(CRON_ENV_FILE).get("TW_GITHUB_TOKEN") or None


# ---- 交易员列表（独立于 .env，支持别名与当前选中项）----

TRADERS_FILE = BASE / "traders.json"


def _default_name(tid: str, index: int) -> str:
    """给交易员起默认别名：熬鹰资本保留原名，其他用序号。"""
    if tid == "5075281354358777856":
        return "熬鹰资本"
    return f"交易员{index + 1}"


def load_traders() -> dict[str, Any]:
    """读取交易员列表；首次从 .env 的 TW_TRADER_IDS 导入。"""
    if TRADERS_FILE.is_file():
        try:
            data = json.loads(TRADERS_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("traders"), list):
                return data
        except (ValueError, OSError):
            pass
    settings = Settings()
    items = [
        {"id": tid, "name": _default_name(tid, i)}
        for i, tid in enumerate(settings.trader_id_list)
    ]
    data: dict[str, Any] = {
        "traders": items,
        "current": items[0]["id"] if items else "",
    }
    save_traders(data)
    return data


def save_traders(data: dict[str, Any]) -> None:
    TRADERS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---- 交易员搜索缓存（榜单接口不支持服务端搜索，本地缓存后过滤）----

SEARCH_CACHE = BASE / "traders_cache.json"


def load_search_cache() -> list[dict[str, Any]]:
    if SEARCH_CACHE.is_file():
        try:
            data = json.loads(SEARCH_CACHE.read_text(encoding="utf-8"))
            # v1 缓存把 roi 误乘了 100，直接丢弃，避免显示 7092% 这类错误值
            if data.get("version") == 2 and isinstance(data.get("traders"), list):
                return data["traders"]
        except (ValueError, OSError):
            pass
    return []


def save_search_cache(items: list[dict[str, Any]]) -> None:
    SEARCH_CACHE.write_text(
        json.dumps({"version": 2, "traders": items}, ensure_ascii=False), encoding="utf-8"
    )


def _info_to_dict(info: TraderInfo) -> dict[str, Any]:
    return {
        "id": info.trader_id,
        "name": info.name,
        "roi": round(info.roi, 1),  # 接口返回的 roi 已是百分数，不能再乘 100
        "copiers": info.copiers,
    }


def _sources(settings: Settings) -> tuple[KopyonSource, PositionStore]:
    source = KopyonSource(
        SourceConfig(api_key=settings.kopyon_api_key, base_url=settings.kopyon_base_url)
    )
    return source, PositionStore(settings.db_path)


async def _task_fetch(emit: Emit, tid: str, label: str, limit: int = 3) -> None:
    settings = Settings()
    if not tid:
        emit("错误：未选择交易员，请先在顶部选择或到设置里添加。")
        return
    source, store = _sources(settings)
    await store.open()
    try:
        emit(f"=== {label}（{tid}）===")
        all_o, new_o, all_c, new_c = await fetch_latest(source, store, tid, limit, "all")
        emit(f"最近 {len(all_o)} 条订单（新增 {len(new_o)}）：")
        for line in (new_o or all_o[:limit]) or ["  （无记录）"]:
            emit(f"  {line}")
        emit(f"最近 {len(all_c)} 条平仓（新增 {len(new_c)}）：")
        for line in (new_c or all_c[:limit]) or ["  （无记录）"]:
            emit(f"  {line}")

        notifier = build_notifier(settings)
        if notifier is None:
            emit("微信推送：未启用渠道")
        else:
            push_o = new_o if settings.notify_only_new else all_o
            push_c = new_c if settings.notify_only_new else all_c
            if push_o or push_c:
                msg = build_message(tid, list(push_o), list(push_c), label)
                emit(f"微信推送：{await notifier.send(msg)}")
            else:
                emit("微信推送：无新记录，跳过")
            await notifier.aclose()
    finally:
        await store.close()
        await source.aclose()
    emit(f"[{_now()}] 抓取完成")


async def _task_test_notify(emit: Emit) -> None:
    settings = Settings()
    notifier = build_notifier(settings)
    if notifier is None:
        emit(
            f"未启用通知：TW_NOTIFY_CHANNEL={settings.notify_channel or '(空)'}，"
            "或对应凭据为空。请检查 .env。"
        )
        return
    try:
        result = await notifier.send(
            NotifyMessage(
                title="熬鹰资本 通知测试",
                content="### 微信通知测试\n\n如果你看到这条消息，说明推送配置成功。",
            )
        )
        emit(f"推送结果：{result}")
    finally:
        await notifier.aclose()


async def _task_history(emit: Emit, tid: str, label: str, limit: int = 10) -> None:
    settings = Settings()
    if not tid:
        emit("错误：未选择交易员。")
        return
    store = PositionStore(settings.db_path)
    await store.open()
    try:
        emit(f"=== {label}（{tid}）历史订单 ===")
        rows = await store.recent_orders(tid, limit)
        if not rows:
            emit("  （无记录，先点“抓取最新交易”）")
        for o in rows:
            emit(
                f"  {to_local(o.created_at):%m-%d %H:%M:%S} {o.symbol:14} "
                f"{o.side.value:4} price={o.price:g} qty={o.quantity:g}"
            )
        emit(f"=== {label}（{tid}）历史平仓 ===")
        rows_c = await store.recent_closed_positions(tid, limit)
        if not rows_c:
            emit("  （无记录）")
        for p in rows_c:
            roi = (p.roi or 0) * 100
            emit(
                f"  {to_local(p.closed_at):%m-%d %H:%M:%S} {p.symbol:14} "
                f"{p.side.value:5} ROI={roi:+.1f}% PnL={p.gross_pnl:g} {p.pnl_currency}"
            )
    finally:
        await store.close()


async def _task_search(emit: Emit, keyword: str, pages: int = 10) -> list[dict[str, Any]]:
    """搜索交易员：纯数字走 ID 验证；名称走榜单缓存本地过滤。

    Returns:
        匹配的交易员字典列表，供界面渲染。
    """
    settings = Settings()
    keyword = keyword.strip()
    if not keyword:
        emit("请输入交易员名称或 ID。")
        return []

    # 情况一：纯数字 ID —— 直接验证有效性
    if keyword.isdigit():
        source = KopyonSource(
            SourceConfig(api_key=settings.kopyon_api_key, base_url=settings.kopyon_base_url)
        )
        try:
            emit(f"正在验证交易员 ID {keyword} …")
            try:
                ok = await source.check_trader(keyword)
            except Exception:  # noqa: BLE001 - API 对无效 ID 返回 422，视为无效
                ok = False
            if ok:
                cached = next(
                    (t for t in load_search_cache() if t["id"] == keyword), None
                )
                result = [{
                    "id": keyword,
                    "name": cached["name"] if cached else "（未收录名称，可直接使用）",
                    "roi": cached.get("roi", 0) if cached else 0,
                    "copiers": cached.get("copiers", 0) if cached else 0,
                }]
                emit(f"ID 有效：{keyword}")
                return result
            emit("该 ID 无效或交易员已停止带单（The current portfolio is closed）。")
            return []
        finally:
            await source.aclose()

    # 情况二：名称 —— 优先用数据源的服务端搜索接口，失败时回退本地榜单缓存
    cache = load_search_cache()
    source = KopyonSource(
        SourceConfig(api_key=settings.kopyon_api_key, base_url=settings.kopyon_base_url)
    )
    matches: list[dict[str, Any]] = []
    try:
        seen: set[str] = set()
        cached_ids = {t["id"] for t in cache}
        for page in range(1, pages + 1):
            try:
                result_page = await source.search_traders_by_name(keyword, page=page)
            except Exception as exc:  # noqa: BLE001 - 单页失败时回退本地缓存
                emit(f"  服务端搜索第 {page} 页失败：{exc}")
                break
            for info in result_page.items:
                if not info.trader_id or info.trader_id in seen:
                    continue
                seen.add(info.trader_id)
                row = _info_to_dict(info)
                matches.append(row)
                if info.trader_id not in cached_ids:
                    cache.append(row)
                    cached_ids.add(info.trader_id)
            if not result_page.next_cursor:
                break
        save_search_cache(cache)
        if matches:
            emit(f"服务端搜索找到 {len(matches)} 位匹配「{keyword}」的交易员。")
    finally:
        await source.aclose()

    if not matches:
        kw = keyword.lower()
        matches = [t for t in cache if kw in t["name"].lower() or kw in t["id"]]
        matches.sort(key=lambda t: t.get("roi") or 0, reverse=True)
        emit(f"服务端无结果，本地缓存找到 {len(matches)} 位匹配「{keyword}」的交易员。")
    return matches[:30]


class Api:
    """暴露给 JS 调用的接口。每个方法在后台线程跑异步任务，回调写回界面。"""

    def __init__(self) -> None:
        # 用双下划线前缀，避免 pywebview 把 window 对象暴露给 JS（会导致递归序列化报错）
        self.__window: Any = None
        self.busy = False
        # 本地定时任务：窗口开着时按间隔触发云端 Actions
        self._schedule_lock = threading.Lock()
        self._schedule_stop = threading.Event()
        self._schedule_running = False
        self._schedule_interval_min = load_schedule_interval()
        self._schedule_next_at: datetime | None = None
        self._schedule_last = ""

    def bind_window(self, window: Any) -> None:
        """绑定 WebView 窗口对象（不暴露给 JS）。"""
        self.__window = window

    def _emit(self, line: str) -> None:
        if self.__window is not None:
            self.__window.evaluate_js(f"window.appendLine({_js_str(line)})")

    def _run(self, coro_factory: Any) -> str:
        if self.busy:
            self._emit("请稍候，上一个任务还在运行。")
            return "busy"
        self.busy = True
        if self.__window is not None:
            self.__window.evaluate_js("window.setBusy(true)")

        def worker() -> None:
            try:
                asyncio.run(coro_factory(self._emit))
            except Exception as exc:  # noqa: BLE001 - 界面需显示所有异常
                self._emit(f"[错误] {type(exc).__name__}: {exc}")
            finally:
                self.busy = False
                if self.__window is not None:
                    self.__window.evaluate_js("window.setBusy(false)")

        threading.Thread(target=worker, daemon=True).start()
        return "started"

    def fetch(self, tid: str, label: str) -> str:
        self._emit(f"\n[{_now()}] 开始抓取 {label} 最新 3 条…")
        return self._run(lambda emit: _task_fetch(emit, tid, label, 3))

    def test_notify(self) -> str:
        self._emit(f"\n[{_now()}] 发送测试微信…")
        return self._run(_task_test_notify)

    def history(self, tid: str, label: str) -> str:
        self._emit(f"\n[{_now()}] 读取 {label} 历史记录…")
        return self._run(lambda emit: _task_history(emit, tid, label, 10))

    # ---- 本地定时任务（JS 调用）----

    def _schedule_loop(self, token: str, interval_min: int) -> None:
        """后台线程：先立即触发一次，之后每隔 interval_min 分钟触发一次。"""
        while not self._schedule_stop.is_set():
            ok, message = dispatch_workflow(token)
            self._emit(f"[{_now()}] [本地定时] {message}")
            with self._schedule_lock:
                self._schedule_last = message
                self._schedule_next_at = _local_now() + timedelta(minutes=interval_min)

            # 401/403/404/422 属于配置错误，继续重试没有意义，自动停止
            if not ok and any(code in message for code in ("401", "403", "404", "422")):
                self._emit(f"[{_now()}] [本地定时] 配置类错误，自动停止。请检查 token 与仓库配置。")
                break
            if self._schedule_stop.wait(interval_min * 60):
                break

        with self._schedule_lock:
            self._schedule_running = False
            self._schedule_next_at = None
        self._emit(f"[{_now()}] [本地定时] 已停止。")

    def start_schedule(self, interval_min: int) -> str:
        """启动本地定时器；token 从环境变量或 exe 同目录 .cron.env 读取。"""
        try:
            interval = int(interval_min)
        except (TypeError, ValueError):
            return "错误：间隔必须是数字"
        if interval < MIN_SCHEDULE_MINUTES or interval > MAX_SCHEDULE_MINUTES:
            return (
                f"错误：间隔需在 {MIN_SCHEDULE_MINUTES}-{MAX_SCHEDULE_MINUTES} 分钟之间，"
                f"建议 {DEFAULT_SCHEDULE_MINUTES} 分钟（与云端一致）"
            )

        token = _github_token()
        if not token:
            return "错误：未找到 GitHub token，请把 TW_GITHUB_TOKEN 写入程序目录 .cron.env"

        save_schedule_interval(interval)
        with self._schedule_lock:
            if self._schedule_running:
                return "定时任务已在运行"
            self._schedule_stop.clear()
            self._schedule_running = True
            self._schedule_interval_min = interval
            self._schedule_next_at = _local_now()
            self._schedule_last = "启动中…"

        threading.Thread(
            target=self._schedule_loop, args=(token, interval), daemon=True
        ).start()
        return f"已启动：每 {interval} 分钟触发一次云端抓取，立即执行第一次。"

    def stop_schedule(self) -> str:
        """停止本地定时器。"""
        with self._schedule_lock:
            if not self._schedule_running:
                return "定时任务未运行"
            self._schedule_stop.set()
        return "正在停止定时任务…"

    def schedule_status(self) -> dict[str, Any]:
        """返回定时器状态，供界面每秒刷新倒计时。"""
        token_ready = bool(_github_token())
        with self._schedule_lock:
            running = self._schedule_running
            interval = self._schedule_interval_min
            next_at = self._schedule_next_at
            last = self._schedule_last
        remain = (
            max(0, int((next_at - _local_now()).total_seconds()))
            if next_at is not None
            else 0
        )
        return {
            "running": running,
            "token_ready": token_ready,
            "interval": interval,
            "next": next_at.strftime("%H:%M:%S") if next_at else "",
            "remain": remain,
            "last": last,
        }

    def trigger_once(self) -> str:
        """不启动定时器，只立即触发一次云端抓取。"""
        token = _github_token()
        if not token:
            return "错误：未找到 GitHub token，请把 TW_GITHUB_TOKEN 写入程序目录 .cron.env"

        def worker() -> None:
            _, message = dispatch_workflow(token)
            self._emit(f"[{_now()}] [立即触发] {message}")
            with self._schedule_lock:
                self._schedule_last = message

        threading.Thread(target=worker, daemon=True).start()
        return "已提交立即触发请求，结果会显示在下方。"

    # ---- 交易员搜索（JS 调用）----

    def search_traders(self, keyword: str, pages: int) -> str:
        """异步搜索，结果通过 window.showSearchResults 回调渲染。"""
        async def job(emit: Emit) -> None:
            results = await _task_search(emit, keyword, pages)
            if self.__window is not None:
                self.__window.evaluate_js(
                    f"window.showSearchResults({_js_str(results)})"
                )

        return self._run(lambda emit: job(emit))

    def get_search_cache_size(self) -> int:
        """返回本地榜单缓存条数，供界面显示。"""
        return len(load_search_cache())

    # ---- 交易员列表管理（JS 调用）----

    def get_traders(self) -> dict[str, Any]:
        """返回交易员列表和当前选中项。"""
        return load_traders()

    def select_trader(self, tid: str) -> str:
        """切换当前交易员，保存到列表文件。"""
        data = load_traders()
        if not any(t["id"] == tid for t in data["traders"]):
            return "错误：交易员不存在"
        data["current"] = tid
        save_traders(data)
        return "已切换"

    def add_trader(self, tid: str, name: str) -> str:
        """添加交易员，id 去重。"""
        tid, name = tid.strip(), name.strip()
        if not tid:
            return "错误：交易员 ID 不能为空"
        if not tid.isdigit():
            return "错误：交易员 ID 应为纯数字"
        data = load_traders()
        if any(t["id"] == tid for t in data["traders"]):
            return "错误：该交易员已存在"
        data["traders"].append(
            {"id": tid, "name": name or _default_name(tid, len(data["traders"]))}
        )
        if not data["current"]:
            data["current"] = tid
        save_traders(data)
        return "已添加"

    def remove_trader(self, tid: str) -> str:
        """删除交易员；若删的是当前项则自动切到第一个。"""
        data = load_traders()
        data["traders"] = [t for t in data["traders"] if t["id"] != tid]
        if data["current"] == tid:
            data["current"] = data["traders"][0]["id"] if data["traders"] else ""
        save_traders(data)
        return "已删除"

    def rename_trader(self, tid: str, name: str) -> str:
        """修改交易员别名。"""
        data = load_traders()
        for t in data["traders"]:
            if t["id"] == tid:
                t["name"] = name.strip() or t["name"]
                save_traders(data)
                return "已改名"
        return "错误：交易员不存在"

    def config_info(self) -> str:
        """返回界面顶部显示的配置摘要。"""
        settings = Settings()
        data = load_traders()
        current = next(
            (t for t in data["traders"] if t["id"] == data.get("current")), None
        )
        who = f"{current['name']}（{current['id']}）" if current else "(未配置交易员)"
        channel = settings.notify_channel or "(未启用)"
        env = str(ENV_PATH) if ENV_PATH else f"{BASE / '.env'}（未找到）"
        return f"当前交易员：{who}    微信渠道：{channel}    配置：{env}"

    def needs_setup(self) -> bool:
        """首次运行判断：缺 API Key 或交易员 ID 时需要引导配置。"""
        settings = Settings()
        return not settings.kopyon_api_key.strip() or not bool(settings.trader_id_list)

    def get_config(self) -> dict[str, str]:
        """返回当前配置供表单回填（仅本机可见）。"""
        s = Settings()
        return {
            "api_key": s.kopyon_api_key,
            "trader_ids": s.trader_ids,
            "notify_channel": s.notify_channel,
            "pushplus_token": s.pushplus_token,
            "serverchan_key": s.serverchan_key,
            "wecom_webhook_key": s.wecom_webhook_key,
            "notify_only_new": "true" if s.notify_only_new else "false",
        }

    def save_config(
        self,
        api_key: str,
        trader_ids: str,
        notify_channel: str,
        pushplus_token: str,
        serverchan_key: str,
        wecom_webhook_key: str,
        notify_only_new: bool,
    ) -> str:
        """把表单配置写入 exe 同目录的 .env，并让当前进程立即生效。"""
        api_key = api_key.strip()
        trader_ids = trader_ids.strip()
        notify_channel = notify_channel.strip()
        if not api_key:
            return "错误：API Key 不能为空"
        if not trader_ids:
            return "错误：交易员 ID 不能为空"
        if notify_channel not in ("", "pushplus", "serverchan", "wecom"):
            return "错误：通知渠道无效"

        values: dict[str, str] = {
            "TW_KOPYON_API_KEY": api_key,
            "TW_KOPYON_BASE_URL": "https://api.kopyon.com/v5/binance-copy-trading",
            "TW_TRADER_IDS": trader_ids,
            "TW_DB_PATH": str(BASE / "data" / "trader_positions.db"),
            "TW_NOTIFY_CHANNEL": notify_channel,
            "TW_NOTIFY_ONLY_NEW": "true" if notify_only_new else "false",
            "TW_PUSHPLUS_TOKEN": pushplus_token.strip(),
            "TW_SERVERCHAN_KEY": serverchan_key.strip(),
            "TW_WECOM_WEBHOOK_KEY": wecom_webhook_key.strip(),
        }
        lines = [
            "# 由程序界面生成，请勿随意删除 TW_ 开头的行",
            "# API Key：https://www.kopyon.com/ 注册后获取（免费额度 10000 次/月）",
        ]
        for key, value in values.items():
            lines.append(f"{key}={value}")
        (BASE / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 覆盖当前进程环境变量（load_env 用 setdefault，旧值不会自动更新）
        for key, value in values.items():
            os.environ[key] = value

        # 同步交易员列表：保留已有别名，补进 .env 里新出现的交易员
        data = load_traders()
        known = {t["id"] for t in data["traders"]}
        for i, tid in enumerate(t.strip() for t in trader_ids.split(",") if t.strip()):
            if tid not in known:
                data["traders"].append({"id": tid, "name": _default_name(tid, i)})
                known.add(tid)
        if not any(t["id"] == data.get("current") for t in data["traders"]):
            data["current"] = data["traders"][0]["id"] if data["traders"] else ""
        save_traders(data)
        return "配置已保存并立即生效。"


def _js_str(text: str) -> str:
    """把 Python 字符串转成安全的 JS 字符串字面量。"""
    import json

    return json.dumps(text, ensure_ascii=False)


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>熬鹰资本交易监控</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; margin: 0;
         background: #f4f6f9; color: #1f2933; }
  header { background: linear-gradient(135deg,#1e3a5f,#2d6a8e); color: #fff;
           padding: 18px 24px; }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header p { margin: 6px 0 0; font-size: 12px; opacity: .85; }
  main { padding: 18px 24px; }
  .buttons { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
  button { border: 0; border-radius: 6px; padding: 9px 16px; font-size: 13px;
           cursor: pointer; background: #2d6a8e; color: #fff; transition: .15s; }
  button:hover { background: #1e3a5f; }
  button.secondary { background: #6b7280; }
  button.secondary:hover { background: #4b5563; }
  button.ghost { background: rgba(255,255,255,.18); padding: 6px 12px; font-size: 12px; }
  button.ghost:hover { background: rgba(255,255,255,.30); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  #status { font-size: 12px; color: #0a7a3a; margin-bottom: 8px; }
  .header-row { display: flex; justify-content: space-between; align-items: center; }
  #setupPanel { background: #fff; border: 1px solid #cbd5e0; border-radius: 8px;
                padding: 16px; margin-bottom: 16px; }
  #setupPanel h2 { margin: 0 0 12px; font-size: 15px; color: #1e3a5f; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 18px; }
  .field label { display: block; font-size: 12px; color: #4b5563; margin-bottom: 4px; }
  .field input, .field select { width: 100%; padding: 7px 9px; border: 1px solid #cbd5e0;
                border-radius: 5px; font-size: 13px; font-family: inherit; }
  .field.wide { grid-column: 1 / -1; }
  .setup-actions { margin-top: 14px; display: flex; gap: 10px; align-items: center; }
  .setup-note { font-size: 11px; color: #6b7280; }
  .trader-bar { background: #fff; border: 1px solid #cbd5e0; border-radius: 8px;
                padding: 10px 14px; margin-bottom: 14px; display: flex; gap: 10px;
                align-items: center; flex-wrap: wrap; }
  .trader-bar label { font-size: 13px; font-weight: 600; color: #1e3a5f; }
  .trader-bar select { flex: 1; min-width: 220px; padding: 7px 9px; border: 1px solid #cbd5e0;
                border-radius: 5px; font-size: 13px; font-family: inherit; }
  .trader-bar button { font-size: 12px; padding: 7px 12px; }
  #traderPanel { background: #fff; border: 1px solid #cbd5e0; border-radius: 8px;
                 padding: 14px; margin-bottom: 14px; }
  #traderPanel h2 { margin: 0 0 10px; font-size: 14px; color: #1e3a5f; }
  .trader-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  .trader-row input { padding: 6px 8px; border: 1px solid #cbd5e0; border-radius: 5px;
                      font-size: 12px; font-family: inherit; }
  .trader-row .name { flex: 1; }
  .trader-row .id { flex: 2; font-family: Consolas, monospace; }
  .trader-row button { font-size: 11px; padding: 5px 10px; }
  #searchPanel { background: #fff; border: 1px solid #cbd5e0; border-radius: 8px;
                 padding: 14px; margin-bottom: 14px; }
  #searchPanel h2 { margin: 0 0 10px; font-size: 14px; color: #1e3a5f; }
  .search-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .search-bar input { flex: 1; min-width: 200px; padding: 7px 9px; border: 1px solid #cbd5e0;
                      border-radius: 5px; font-size: 13px; font-family: inherit; }
  .search-bar select { padding: 6px; border: 1px solid #cbd5e0; border-radius: 5px; font-size: 12px; }
  #searchResults { margin-top: 10px; max-height: 260px; overflow: auto; }
  #schedulePanel { background: #fff; border: 1px solid #cbd5e0; border-radius: 8px;
                   padding: 14px; margin-bottom: 14px; }
  #schedulePanel h2 { margin: 0 0 10px; font-size: 14px; color: #1e3a5f; }
  .schedule-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .schedule-bar input { width: 90px; padding: 7px 9px; border: 1px solid #cbd5e0;
                        border-radius: 5px; font-size: 13px; font-family: inherit; }
  #scheduleStatus { font-size: 12px; color: #1e3a5f; font-weight: 600; }
  .result-row { display: flex; gap: 10px; align-items: center; padding: 6px 8px;
                border-bottom: 1px solid #eef2f5; font-size: 12px; }
  .result-row .rname { flex: 1; font-weight: 600; }
  .result-row .rid { font-family: Consolas, monospace; color: #4b5563; }
  .result-row .rmeta { color: #6b7280; min-width: 110px; }
  .result-row button { font-size: 11px; padding: 4px 10px; }
  #out { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
         padding: 14px; min-height: 380px; max-height: calc(100vh - 230px);
         overflow: auto; font-family: Consolas, "Courier New", monospace;
         font-size: 12.5px; line-height: 1.65; white-space: pre-wrap;
         word-break: break-all; }
  .hint { font-size: 11px; color: #6b7280; margin-top: 10px; }
</style>
</head>
<body>
<header>
  <div class="header-row">
    <h1>交易记录抓取工具</h1>
    <button class="ghost" onclick="openSetup()">⚙ 设置</button>
  </div>
  <p id="cfg">配置读取中…</p>
</header>
<main>
  <div class="trader-bar">
    <label>当前交易员：</label>
    <select id="traderSelect" onchange="onTraderChange()" disabled><option>加载中…</option></select>
    <button onclick="toggleSearchPanel()">查找交易员</button>
    <button class="secondary" onclick="toggleTraderPanel()">管理交易员</button>
  </div>
  <section id="searchPanel" style="display:none">
    <h2>查找交易员（输入名称模糊搜索，或输入 ID 精确验证）</h2>
    <div class="search-bar">
      <input id="searchKeyword" placeholder="如：熬鹰 / 星辰 / 5075281354358777856" onkeydown="if(event.key==='Enter')doSearch()">
      <select id="searchPages">
        <option value="5">搜索范围：前100条结果</option>
        <option value="10" selected>前200条结果</option>
        <option value="30">前600条结果</option>
      </select>
      <button onclick="doSearch()">搜索</button>
      <span class="setup-note" id="cacheSize"></span>
    </div>
    <div id="searchResults"></div>
  </section>
  <section id="traderPanel" style="display:none">
    <h2>交易员列表（可添加、改名、删除；当前选择会自动保存）</h2>
    <div id="traderRows"></div>
    <div class="trader-row" style="margin-top:12px; border-top:1px solid #e2e8f0; padding-top:12px">
      <input class="name" id="newTraderName" placeholder="别名，如：张三（可留空）">
      <input class="id" id="newTraderId" placeholder="交易员 ID（portfolioId 数字）">
      <button onclick="addTrader()">添加</button>
    </div>
  </section>
  <section id="schedulePanel">
    <h2>本地定时任务（触发云端 GitHub Actions）</h2>
    <div class="schedule-bar">
      <label>间隔（分钟）</label>
      <input id="scheduleInterval" type="number" min="5" max="1440" value="15">
      <button id="btnScheduleStart" onclick="startSchedule()">启动定时</button>
      <button id="btnScheduleStop" class="secondary" onclick="stopSchedule()" disabled>停止</button>
      <button id="btnScheduleOnce" onclick="triggerOnce()">立即触发一次</button>
      <span id="scheduleStatus">状态读取中…</span>
    </div>
    <div class="setup-note" style="margin-top:8px">
      默认 15 分钟（与云端一致，最小 5 分钟），设置会记住。token 从本程序目录的 .cron.env 读取；启动后立刻触发一次，之后按间隔重复。窗口关闭即停止，不会写入系统任务计划。
      请勿同时开多个窗口或同时启用 cron-job.org，避免重复触发。
    </div>
  </section>
  <section id="setupPanel" style="display:none">
    <h2>配置（只需填一次，保存在本程序目录）</h2>
    <div class="grid">
      <div class="field">
        <label>Kopyon API Key *（https://www.kopyon.com/ 注册获取）</label>
        <input id="cfgApiKey" type="text" placeholder="kpy_...">
      </div>
      <div class="field">
        <label>交易员 ID *（币安跟单 portfolioId）</label>
        <input id="cfgTrader" type="text" placeholder="多个用英文逗号分隔">
      </div>
      <div class="field">
        <label>微信通知渠道</label>
        <select id="cfgChannel" onchange="toggleTokens()">
          <option value="">不推送</option>
          <option value="pushplus">PushPlus（推荐）</option>
          <option value="serverchan">Server酱</option>
          <option value="wecom">企业微信群机器人</option>
        </select>
      </div>
      <div class="field">
        <label><input id="cfgOnlyNew" type="checkbox" checked> 只在有新交易时推送</label>
      </div>
      <div class="field wide" id="rowPushplus">
        <label>PushPlus token（微信扫码登录 https://www.pushplus.plus/ → 一对一推送 → 复制 token）</label>
        <input id="cfgPushplus" type="text" placeholder="消息 token">
      </div>
      <div class="field wide" id="rowServerchan" style="display:none">
        <label>Server酱 SendKey（https://sct.ftqq.com/）</label>
        <input id="cfgServerchan" type="text" placeholder="SCT... 或 sctp...">
      </div>
      <div class="field wide" id="rowWecom" style="display:none">
        <label>企业微信群机器人 webhook 的 key</label>
        <input id="cfgWecom" type="text" placeholder="webhook 地址中 key= 后面的部分">
      </div>
    </div>
    <div class="setup-actions">
      <button onclick="saveSetup()">保存配置</button>
      <button class="secondary" onclick="closeSetup()">取消</button>
      <span class="setup-note">配置仅保存在你自己的电脑，不会随程序发给别人。</span>
    </div>
  </section>
  <div class="buttons">
    <button id="btnFetch" onclick="doFetch()">抓取最新交易</button>
    <button id="btnTest" onclick="doTest()">测试微信推送</button>
    <button id="btnHistory" onclick="doHistory()">查看历史</button>
    <button class="secondary" onclick="clearOut()">清空显示</button>
  </div>
  <div id="status">就绪</div>
  <div id="out"></div>
  <div class="hint">订单时间按“最后成交时间”显示（与币安页面一致），限价单若与下单时间不同会额外标注“下单时间”；平仓按平仓时间排序。可用上方“本地定时任务”在本窗口开着时按间隔触发云端抓取；关闭窗口即停止，关机后仍依赖云端或 cron-job.org。</div>
</main>
<script>
  const out = document.getElementById('out');
  const status = document.getElementById('status');
  window.appendLine = function(line) {
    out.textContent += line + '\n';
    out.scrollTop = out.scrollHeight;
  };
  window.setBusy = function(busy) {
    status.textContent = busy ? '运行中…' : '就绪';
    status.style.color = busy ? '#d68910' : '#0a7a3a';
    ['btnFetch','btnTest','btnHistory'].forEach(id => {
      document.getElementById(id).disabled = busy;
    });
  };
  function clearOut() { out.textContent = ''; }

  // ---- 查找交易员 ----
  function toggleSearchPanel() {
    const p = document.getElementById('searchPanel');
    const opening = p.style.display === 'none';
    p.style.display = opening ? '' : 'none';
    if (opening) {
      window.pywebview.api.get_search_cache_size().then(function(n) {
        document.getElementById('cacheSize').textContent =
          n ? '已缓存 ' + n + ' 位交易员（优先走服务端搜索）' : '首次搜索将使用服务端搜索';
      });
    }
  }
  async function doSearch() {
    const kw = document.getElementById('searchKeyword').value.trim();
    if (!kw) { appendLine('请输入名称或 ID。'); return; }
    const pages = parseInt(document.getElementById('searchPages').value, 10);
    document.getElementById('searchResults').innerHTML = '<div class="result-row">搜索中…</div>';
    await window.pywebview.api.search_traders(kw, pages);
  }
  window.showSearchResults = function(results) {
    const box = document.getElementById('searchResults');
    box.innerHTML = '';
    if (!results.length) {
      box.innerHTML = '<div class="result-row">没有找到匹配的交易员。</div>';
      return;
    }
    results.forEach(function(t) {
      const row = document.createElement('div');
      row.className = 'result-row';
      row.innerHTML =
        '<span class="rname">' + escapeAttr(t.name) + '</span>' +
        '<span class="rid">' + escapeAttr(t.id) + '</span>' +
        '<span class="rmeta">ROI ' + (t.roi || 0) + '% · 跟单 ' + (t.copiers || 0) + '</span>' +
        '<button onclick="addFromSearch(\'' + t.id + '\',\'' + escapeAttr(t.name).replace(/'/g,"\\'") + '\')">添加</button>';
      box.appendChild(row);
    });
    window.pywebview.api.get_search_cache_size().then(function(n) {
      document.getElementById('cacheSize').textContent = '已缓存 ' + n + ' 位交易员';
    });
  };
  async function addFromSearch(id, name) {
    const r = await window.pywebview.api.add_trader(id, name);
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] 添加交易员 ' + name + '：' + r);
    if (r === '已添加') await loadTraders();
  }

  // ---- 交易员切换 ----
  function currentTrader() {
    const sel = document.getElementById('traderSelect');
    const id = sel.value;
    const opt = sel.options[sel.selectedIndex];
    return { id: id, label: opt ? opt.textContent.split('（')[0] : id || '(未选择)' };
  }
  async function loadTraders() {
    try {
      const data = await window.pywebview.api.get_traders();
      const sel = document.getElementById('traderSelect');
      sel.innerHTML = '';
      if (!data.traders.length) {
        sel.innerHTML = '<option value="">(未配置，请到设置添加)</option>';
      } else {
        data.traders.forEach(function(t) {
          const o = document.createElement('option');
          o.value = t.id;
          o.textContent = t.name + '（' + t.id + '）';
          sel.appendChild(o);
        });
        sel.value = data.current || data.traders[0].id;
      }
      sel.disabled = false;
      renderTraderRows(data.traders);
      await refreshCfg();
    } catch (e) { appendLine('加载交易员列表失败: ' + e); }
  }
  function renderTraderRows(traders) {
    const box = document.getElementById('traderRows');
    box.innerHTML = '';
    traders.forEach(function(t) {
      const row = document.createElement('div');
      row.className = 'trader-row';
      row.innerHTML =
        '<input class="name" value="' + escapeAttr(t.name) + '">' +
        '<input class="id" value="' + escapeAttr(t.id) + '" readonly>' +
        '<button onclick="renameTrader(\'' + t.id + '\', this.parentNode.querySelector(\'.name\').value)">改名</button>' +
        '<button class="secondary" onclick="removeTrader(\'' + t.id + '\')">删除</button>';
      box.appendChild(row);
    });
  }
  function escapeAttr(s) {
    return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
  }
  async function onTraderChange() {
    const t = currentTrader();
    if (!t.id) return;
    const r = await window.pywebview.api.select_trader(t.id);
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] 已切换到 ' + t.label + '（' + r + '）');
    await refreshCfg();
  }
  function toggleTraderPanel() {
    const p = document.getElementById('traderPanel');
    p.style.display = p.style.display === 'none' ? '' : 'none';
  }
  async function addTrader() {
    const id = document.getElementById('newTraderId').value;
    const name = document.getElementById('newTraderName').value;
    const r = await window.pywebview.api.add_trader(id, name);
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] ' + r);
    if (r === '已添加') {
      document.getElementById('newTraderId').value = '';
      document.getElementById('newTraderName').value = '';
      await loadTraders();
    }
  }
  async function renameTrader(id, name) {
    const r = await window.pywebview.api.rename_trader(id, name);
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] ' + r);
    if (r === '已改名') await loadTraders();
  }
  async function removeTrader(id) {
    if (!confirm('确定删除这个交易员？')) return;
    const r = await window.pywebview.api.remove_trader(id);
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] ' + r);
    if (r === '已删除') await loadTraders();
  }

  async function doFetch() {
    const t = currentTrader();
    await window.pywebview.api.fetch(t.id, t.label);
  }
  async function doTest() { await window.pywebview.api.test_notify(); }
  async function doHistory() {
    const t = currentTrader();
    await window.pywebview.api.history(t.id, t.label);
  }

  // ---- 本地定时任务 ----
  function formatRemain(sec) {
    sec = Math.max(0, parseInt(sec || 0, 10));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m > 0 ? m + '分' + s + '秒' : s + '秒';
  }
  async function refreshScheduleStatus() {
    try {
      const s = await window.pywebview.api.schedule_status();
      const el = document.getElementById('scheduleStatus');
      if (!s.token_ready) {
        el.textContent = '未启动 · 缺少 .cron.env 中的 token';
      } else if (s.running) {
        el.textContent = '运行中 · 间隔 ' + s.interval + ' 分钟 · 下次 ' +
                         s.next + '（' + formatRemain(s.remain) + '）';
      } else {
        el.textContent = '未启动' + (s.last ? ' · 上次：' + s.last : '');
      }
      document.getElementById('btnScheduleStart').disabled = s.running || !s.token_ready;
      document.getElementById('btnScheduleStop').disabled = !s.running;
      document.getElementById('btnScheduleOnce').disabled = !s.token_ready;
    } catch (e) { /* pywebview 尚未就绪时忽略 */ }
  }
  async function startSchedule() {
    const n = parseInt(document.getElementById('scheduleInterval').value, 10);
    const r = await window.pywebview.api.start_schedule(n);
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] ' + r);
    await refreshScheduleStatus();
  }
  async function stopSchedule() {
    const r = await window.pywebview.api.stop_schedule();
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] ' + r);
    await refreshScheduleStatus();
  }
  async function triggerOnce() {
    const r = await window.pywebview.api.trigger_once();
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] ' + r);
    await refreshScheduleStatus();
  }
  setInterval(refreshScheduleStatus, 1000);

  function toggleTokens() {
    const ch = document.getElementById('cfgChannel').value;
    document.getElementById('rowPushplus').style.display = ch === 'pushplus' ? '' : 'none';
    document.getElementById('rowServerchan').style.display = ch === 'serverchan' ? '' : 'none';
    document.getElementById('rowWecom').style.display = ch === 'wecom' ? '' : 'none';
  }
  async function refreshCfg() {
    try {
      const info = await window.pywebview.api.config_info();
      document.getElementById('cfg').textContent = info;
    } catch (e) { document.getElementById('cfg').textContent = '配置读取失败: ' + e; }
  }
  async function openSetup() {
    try {
      const c = await window.pywebview.api.get_config();
      document.getElementById('cfgApiKey').value = c.api_key || '';
      document.getElementById('cfgTrader').value = c.trader_ids || '';
      document.getElementById('cfgChannel').value = c.notify_channel || '';
      document.getElementById('cfgPushplus').value = c.pushplus_token || '';
      document.getElementById('cfgServerchan').value = c.serverchan_key || '';
      document.getElementById('cfgWecom').value = c.wecom_webhook_key || '';
      document.getElementById('cfgOnlyNew').checked = c.notify_only_new !== 'false';
      toggleTokens();
      document.getElementById('setupPanel').style.display = '';
    } catch (e) { appendLine('读取配置失败: ' + e); }
  }
  function closeSetup() { document.getElementById('setupPanel').style.display = 'none'; }
  async function saveSetup() {
    const r = await window.pywebview.api.save_config(
      document.getElementById('cfgApiKey').value,
      document.getElementById('cfgTrader').value,
      document.getElementById('cfgChannel').value,
      document.getElementById('cfgPushplus').value,
      document.getElementById('cfgServerchan').value,
      document.getElementById('cfgWecom').value,
      document.getElementById('cfgOnlyNew').checked
    );
    appendLine('[' + new Date().toLocaleTimeString('zh-CN') + '] ' + r);
    if (r.startsWith('配置已保存')) {
      closeSetup();
      await loadTraders();
    }
  }
  window.addEventListener('pywebviewready', async function() {
    await loadTraders();
    try {
      const saved = await window.pywebview.api.schedule_status();
      document.getElementById('scheduleInterval').value = saved.interval;
    } catch (e) { /* 保留 HTML 默认值 */ }
    await refreshScheduleStatus();
    appendLine('欢迎使用交易记录监控。');
    try {
      const need = await window.pywebview.api.needs_setup();
      if (need) {
        appendLine('首次使用：请在「⚙ 设置」里填写 API Key 和交易员 ID 后保存。');
        await openSetup();
      } else {
        appendLine('提示：顶部下拉框可随时切换交易员；“抓取最新交易”只抓当前选中的交易员。');
      }
    } catch (e) { appendLine('配置检查失败: ' + e); }
    appendLine('-'.repeat(70));
  });
</script>
</body>
</html>
"""


def main() -> None:
    api = Api()
    window = webview.create_window(
        "熬鹰资本交易监控",
        html=HTML,
        js_api=api,
        width=980,
        height=680,
        min_size=(720, 480),
    )
    api.bind_window(window)
    webview.start()


if __name__ == "__main__":
    main()
