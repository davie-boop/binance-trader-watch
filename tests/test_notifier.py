"""微信通知模块测试。"""

from __future__ import annotations

import httpx
import pytest

from trader_watcher.config import Settings
from trader_watcher.models import OrderRecord
from trader_watcher.notifier import (
    NotifyMessage,
    PushPlusNotifier,
    ServerChanNotifier,
    WeComNotifier,
    build_message,
    build_notifier,
)


def test_build_notifier_returns_none_when_disabled() -> None:
    """未配置渠道或凭据为空时不构建通知器。"""
    assert build_notifier(Settings(notify_channel="", notify_only_new=False)) is None
    assert build_notifier(Settings(notify_channel="pushplus", pushplus_token="")) is None


def test_build_notifier_selects_channel() -> None:
    """按配置选择对应渠道。"""
    assert isinstance(
        build_notifier(Settings(notify_channel="pushplus", pushplus_token="tok")), PushPlusNotifier
    )
    assert isinstance(
        build_notifier(Settings(notify_channel="wecom", wecom_webhook_key="k")), WeComNotifier
    )
    sct = build_notifier(Settings(notify_channel="serverchan", serverchan_key="SCT123abc"))
    assert isinstance(sct, ServerChanNotifier)
    assert "sctapi.ftqq.com" in sct._url


def test_serverchan3_url() -> None:
    """sctp 开头的 SendKey 走 Server酱³ 专用域名。"""
    notifier = build_notifier(Settings(notify_channel="serverchan", serverchan_key="sctp456tKEY"))
    assert isinstance(notifier, ServerChanNotifier)
    assert "456.push.ft07.com" in notifier._url


@pytest.mark.asyncio
async def test_pushplus_send_parses_code() -> None:
    """PushPlus code=200 视为成功，其他值返回错误描述。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "msg": "请求成功"})

    notifier = PushPlusNotifier("tok")
    await notifier._client.aclose()
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await notifier.send(NotifyMessage("t", "c")) == "ok"
    await notifier.aclose()


@pytest.mark.asyncio
async def test_wecom_send_parses_errcode() -> None:
    """企业微信 errcode 非 0 时返回错误。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 40058, "errmsg": "invalid"})

    notifier = WeComNotifier("k")
    await notifier._client.aclose()
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert "失败" in await notifier.send(NotifyMessage("t", "c"))
    await notifier.aclose()


def test_build_message_uses_beijing_time() -> None:
    """推送正文中的时间为北京时间，与币安网页一致。"""
    order = OrderRecord.from_api(
        {
            "symbol": "NVDAUSDT",
            "side": "BUY",
            "price": 144.292,
            "quantity": 2043.14,
            "createdAt": 1795626545202,  # UTC 11-25 17:09:05 -> 北京 11-26 01:09:05
            "updatedAt": 1795626545202,
            "orderType": "MARKET",
        }
    )
    from trader_watcher.live import format_order

    msg = build_message("5075281354358777856", [format_order(order)], [])
    assert "11-26 01:09:05" in msg.content
    assert "NVDAUSDT" in msg.content
    # 标题形如「熬鹰资本 MM-DD HH:MM 最新交易」，抓取时间是本地北京时间
    assert "最新交易" in msg.title
    assert len(msg.title.split(" ")) == 4
