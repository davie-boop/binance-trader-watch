"""命令行入口：once / watch / events。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import typer

from .config import Settings, SourceConfig
from .live import fetch_latest, listen_stdin, next_command
from .models import PositionEvent, to_local
from .notifier import NotifyMessage, build_message, build_notifier
from .service import TraderWatcher
from .sources.base import SourceError
from .sources.kopyon import KopyonSource
from .storage import PositionStore

app = typer.Typer(help="币安跟单交易员仓位监控", no_args_is_help=True)


def _configure_logging(verbose: bool) -> None:
    """配置日志格式。

    Args:
        verbose: 是否输出 DEBUG 级别日志。
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


def _build(settings: Settings) -> tuple[KopyonSource, PositionStore]:
    """根据配置构建数据源和仓库。

    Args:
        settings: 运行时配置。

    Returns:
        (数据源, 仓库) 元组。

    Raises:
        typer.Exit: 缺少 API Key 时退出。
    """
    if not settings.kopyon_api_key:
        typer.echo(
            "提示：未设置 TW_KOPYON_API_KEY，将使用每 IP 每天 10 次的匿名额度。"
            "频繁使用请在 .env 中配置免费 Key。",
            err=True,
        )

    source = KopyonSource(
        SourceConfig(
            api_key=settings.kopyon_api_key,
            base_url=settings.kopyon_base_url,
        )
    )
    store = PositionStore(settings.db_path)
    return source, store


async def _print_event(event: PositionEvent) -> None:
    """把事件打印到终端（示例回调，可替换为推送）。

    Args:
        event: 仓位变更事件。
    """
    typer.echo(
        f"  -> {event.event.value.upper():9} {event.symbol:12} {event.side.value:5} "
        f"size={event.size:g} prev={event.previous_size}"
    )


@app.command()
def once(
    trader_id: str | None = typer.Option(None, help="单个交易员 id，默认使用配置中的全部"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """只检查一次，打印本次事件。"""
    _configure_logging(verbose)
    settings = Settings()

    async def run() -> None:
        source, store = _build(settings)
        await store.open()
        try:
            watcher = TraderWatcher(source, store, on_event=_print_event)
            ids = [trader_id] if trader_id else settings.trader_id_list
            if not ids:
                typer.echo("错误：请设置 TW_TRADER_IDS 或传入 --trader-id", err=True)
                raise typer.Exit(code=2)
            for tid in ids:
                typer.echo(f"检查交易员 {tid}:")
                try:
                    events = await watcher.check_once(tid)
                except SourceError as exc:
                    # 数据源问题（如组合已关闭、限流）给出简洁提示并继续下一个，
                    # 不抛出整段 traceback。
                    typer.echo(f"  跳过：{exc}", err=True)
                    continue
                if not events:
                    typer.echo("  无变化")
        finally:
            await store.close()
            await source.aclose()

    asyncio.run(run())


@app.command()
def watch(
    interval: int | None = typer.Option(None, help="轮询间隔（秒），默认取配置值"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """持续轮询，Ctrl+C 退出。"""
    _configure_logging(verbose)
    settings = Settings()

    async def run() -> None:
        source, store = _build(settings)
        await store.open()
        try:
            watcher = TraderWatcher(source, store, on_event=_print_event)
            await watcher.watch(settings.trader_id_list, interval or settings.poll_interval)
        except KeyboardInterrupt:
            typer.echo("已停止")
        finally:
            await store.close()
            await source.aclose()

    asyncio.run(run())


@app.command("events")
def list_events(
    trader_id: str = typer.Option(..., help="交易员 id"),
    limit: int = typer.Option(20, min=1, max=200),
) -> None:
    """查看数据库里记录的历史事件。"""
    _configure_logging(False)
    settings = Settings()

    async def run() -> None:
        store = PositionStore(settings.db_path)
        await store.open()
        try:
            for event in await store.recent_events(trader_id, limit):
                typer.echo(
                    f"{to_local(event.captured_at):%Y-%m-%d %H:%M:%S} "
                    f"{event.event.value.upper():9} {event.symbol:12} "
                    f"{event.side.value:5} size={event.size:g} prev={event.previous_size}"
                )
        finally:
            await store.close()

    asyncio.run(run())


@app.command("trades")
def fetch_trades(
    trader_id: str | None = typer.Option(
        None, help="交易员 portfolio id；不传则抓取 .env 中 TW_TRADER_IDS 的全部交易员"
    ),
    kind: str = typer.Option("all", help="抓取类型：orders / positions / all"),
    pages: int = typer.Option(3, min=1, max=50, help="最多抓取页数，每页约 20 条"),
    limit: int | None = typer.Option(
        None, min=1, max=20, help="只抓最近 N 条（按时间倒序取前 N 条）；不传则按 --pages 翻页"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """增量抓取订单与平仓记录，自动去重后写入 SQLite。"""
    _configure_logging(verbose)
    settings = Settings()
    kind = kind.lower()
    if kind not in ("orders", "positions", "all"):
        typer.echo("错误：kind 只能是 orders / positions / all", err=True)
        raise typer.Exit(code=2)

    async def fetch_one(tid: str, source: KopyonSource, store: PositionStore) -> None:
        """抓取单个交易员的记录，分页直到没有新增或无下一页。"""
        typer.echo(f"=== 交易员 {tid} ===")

        # --limit 模式：只抓按时间倒序的最新 N 条（供定时任务调用，省额度）
        if limit is not None:
            try:
                all_orders, new_orders, all_closed, new_closed = await fetch_latest(
                    source, store, tid, limit, kind
                )
            except SourceError as exc:
                typer.echo(f"  抓取失败，跳过：{exc}", err=True)
                return
            if kind in ("orders", "all"):
                typer.echo(f"  最近 {len(all_orders)} 条订单（新增 {len(new_orders)}）：")
                for line in new_orders or all_orders[:limit]:
                    typer.echo(f"    {line}")
            if kind in ("positions", "all"):
                typer.echo(f"  最近 {len(all_closed)} 条平仓（新增 {len(new_closed)}）：")
                for line in new_closed or all_closed[:limit]:
                    typer.echo(f"    {line}")

            # 微信推送：默认只推新增；notify_only_new=false 时每次都推最新 N 条。
            notifier = build_notifier(settings)
            if notifier is not None:
                push_orders = new_orders if settings.notify_only_new else all_orders
                push_closed = new_closed if settings.notify_only_new else all_closed
                if push_orders or push_closed:
                    msg = build_message(tid, list(push_orders), list(push_closed))
                    result = await notifier.send(msg)
                    typer.echo(f"  微信推送：{result}")
                else:
                    typer.echo("  微信推送：无新记录，跳过")
                await notifier.aclose()
            return

        if kind in ("orders", "all"):
            cursor = None
            total_new = 0
            for page in range(1, pages + 1):
                try:
                    result = await source.fetch_orders(tid, cursor=cursor)
                except SourceError as exc:
                    typer.echo(f"  订单抓取失败，跳过：{exc}", err=True)
                    break
                new = await store.save_orders(tid, result.items)
                total_new += new
                typer.echo(
                    f"  订单 第{page}页: 拉取 {len(result.items)} 条，新增 {new} 条 "
                    f"(总计 {result.total})"
                )
                if not result.next_cursor or new == 0:
                    break
                cursor = result.next_cursor
            typer.echo(f"  订单合计新增: {total_new}")

        if kind in ("positions", "all"):
            cursor = None
            total_new = 0
            for page in range(1, pages + 1):
                try:
                    result = await source.fetch_closed_positions(tid, cursor=cursor)
                except SourceError as exc:
                    typer.echo(f"  平仓抓取失败，跳过：{exc}", err=True)
                    break
                new = await store.save_closed_positions(tid, result.items)
                total_new += new
                typer.echo(
                    f"  平仓 第{page}页: 拉取 {len(result.items)} 条，新增 {new} 条 "
                    f"(总计 {result.total})"
                )
                if not result.next_cursor or new == 0:
                    break
                cursor = result.next_cursor
            typer.echo(f"  平仓合计新增: {total_new}")

    async def run() -> None:
        source, store = _build(settings)
        await store.open()
        try:
            ids = [trader_id] if trader_id else settings.trader_id_list
            if not ids:
                typer.echo("错误：请设置 TW_TRADER_IDS 或传入 --trader-id", err=True)
                raise typer.Exit(code=2)
            for tid in ids:
                await fetch_one(tid, source, store)
        finally:
            await store.close()
            await source.aclose()

    asyncio.run(run())


@app.command("cloud-fetch")
def cloud_fetch(
    state_file: str = typer.Option("cloud_state.json", help="状态文件路径（云端会提交回仓库）"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """云端抓取：用状态文件去重，只推送新记录（供 GitHub Actions 调用）。"""
    _configure_logging(verbose)
    settings = Settings()

    async def run() -> None:
        from .cloud import run_cloud_fetch

        summary = await run_cloud_fetch(settings, state_file)
        typer.echo(
            f"云端抓取完成：初始化={summary['initialized']} "
            f"新订单={summary['new_orders']} 新平仓={summary['new_closed']} "
            f"推送={summary['push']}"
        )

    asyncio.run(run())


@app.command("test-notify")
def test_notify() -> None:
    """发送一条测试消息，验证微信通知渠道是否配置正确。"""
    settings = Settings()
    notifier = build_notifier(settings)
    if notifier is None:
        typer.echo(
            f"未启用通知：TW_NOTIFY_CHANNEL={settings.notify_channel or '(空)'}，"
            "或对应凭据为空。请先在 .env 中配置。",
            err=True,
        )
        raise typer.Exit(code=2)

    async def run() -> None:
        try:
            result = await notifier.send(
                NotifyMessage(
                    title="熬鹰资本 通知测试",
                    content="### 微信通知测试\n\n如果你看到这条消息，说明推送配置成功。",
                )
            )
        finally:
            await notifier.aclose()
        typer.echo(f"推送结果：{result}")
        if result != "ok":
            raise typer.Exit(code=1)

    asyncio.run(run())


@app.command("live")
def live_trades(
    trader_id: str | None = typer.Option(
        None, help="起始交易员 id；不传则使用 .env 中第一个"
    ),
    limit: int = typer.Option(5, min=1, max=20, help="每轮显示最新的 N 条"),
    interval: int = typer.Option(10, min=3, max=3600, help="轮询间隔（秒）"),
    kind: str = typer.Option("all", help="orders / positions / all"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """实时看板：持续抓取最新交易记录，可随时切换交易员。

    运行时按键：数字=切换到第 N 个交易员，q=退出，回车=立即刷新。
    """
    _configure_logging(verbose)
    settings = Settings()
    kind = kind.lower()
    if kind not in ("orders", "positions", "all"):
        typer.echo("错误：kind 只能是 orders / positions / all", err=True)
        raise typer.Exit(code=2)

    ids = settings.trader_id_list
    if not ids:
        typer.echo("错误：请设置 TW_TRADER_IDS 或传入 --trader-id", err=True)
        raise typer.Exit(code=2)
    current = ids.index(trader_id) if trader_id and trader_id in ids else 0

    def show_menu() -> None:
        typer.echo("=" * 64)
        typer.echo("交易员列表（输入数字切换，q 退出，回车立即刷新）：")
        for i, tid in enumerate(ids, 1):
            marker = ">" if i - 1 == current else " "
            typer.echo(f" {marker} {i}. {tid}")
        typer.echo(f"显示最新 {limit} 条 | 轮询 {interval}s | 类型 {kind}")
        typer.echo("=" * 64)

    async def run() -> None:
        nonlocal current
        source, store = _build(settings)
        await store.open()
        show_menu()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        listener = asyncio.create_task(listen_stdin(queue))
        show_all = True  # 首轮、切换交易员后显示全量最新 N 条
        try:
            while True:
                tid = ids[current]
                typer.echo(f"\n--- [{current + 1}/{len(ids)}] {tid}  {_now_str()} ---")
                try:
                    all_orders, new_orders, all_closed, new_closed = await fetch_latest(
                        source, store, tid, limit, kind=kind
                    )
                except SourceError as exc:
                    typer.echo(f"  抓取失败：{exc}", err=True)
                    all_orders, new_orders, all_closed, new_closed = [], [], [], []

                if kind in ("orders", "all"):
                    lines = all_orders if show_all else new_orders
                    prefix = "[最新]" if show_all else "[新订单]"
                    if lines:
                        for line in lines:
                            typer.echo(f"  {prefix} {line}")
                    else:
                        typer.echo("  订单：无新记录")
                if kind in ("positions", "all"):
                    lines = all_closed if show_all else new_closed
                    prefix = "[最新]" if show_all else "[新平仓]"
                    if lines:
                        for line in lines:
                            typer.echo(f"  {prefix} {line}")
                    else:
                        typer.echo("  平仓：无新记录")

                if show_all:
                    typer.echo("  （显示最新记录，之后每次只显示新增）")
                    show_all = False

                cmd = await next_command(queue, interval)
                if cmd is None:
                    continue
                if cmd == "" or cmd.lower() in ("q", "quit", "exit"):
                    typer.echo("已退出")
                    break
                cmd = cmd.strip()
                if cmd.isdigit() and 1 <= int(cmd) <= len(ids):
                    current = int(cmd) - 1
                    show_all = True
                    show_menu()
                # 其他输入忽略，下一轮继续轮询
        finally:
            listener.cancel()
            await store.close()
            await source.aclose()

    asyncio.run(run())


def _now_str() -> str:
    """返回当前北京时间字符串，用于看板标题。"""
    from .models import to_local

    return to_local(datetime.now(UTC)).strftime("%H:%M:%S")


@app.command("order-list")
def order_list(
    trader_id: str = typer.Option(..., help="交易员 portfolio id"),
    limit: int = typer.Option(20, min=1, max=200),
) -> None:
    """查看已存的订单记录。"""
    settings = Settings()

    async def run() -> None:
        store = PositionStore(settings.db_path)
        await store.open()
        try:
            for o in await store.recent_orders(trader_id, limit):
                typer.echo(
                    f"{to_local(o.created_at):%Y-%m-%d %H:%M:%S} {o.symbol:14} {o.side.value:4} "
                    f"price={o.price:g} qty={o.quantity:g} type={o.order_type} "
                    f"pnl={o.realized_pnl:g}"
                )
        finally:
            await store.close()

    asyncio.run(run())


@app.command("position-list")
def position_list(
    trader_id: str = typer.Option(..., help="交易员 portfolio id"),
    limit: int = typer.Option(20, min=1, max=200),
) -> None:
    """查看已存的平仓回合。"""
    settings = Settings()

    async def run() -> None:
        store = PositionStore(settings.db_path)
        await store.open()
        try:
            for p in await store.recent_closed_positions(trader_id, limit):
                roi_pct = (p.roi or 0) * 100
                typer.echo(
                    f"{to_local(p.closed_at):%Y-%m-%d %H:%M:%S} {p.symbol:14} {p.side.value:5} "
                    f"entry={p.entry_price:g} close={p.close_price:g} "
                    f"roi={roi_pct:+.1f}% pnl={p.gross_pnl:g} {p.pnl_currency}"
                )
        finally:
            await store.close()

    asyncio.run(run())


if __name__ == "__main__":
    app()
