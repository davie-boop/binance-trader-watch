"""微信通知模块：把最新交易记录通过 PushPlus / Server酱 / 企业微信机器人推送到微信。

渠道选择由 ``TW_NOTIFY_CHANNEL`` 决定（pushplus / serverchan / wecom）；
未配置渠道或缺少对应凭据时静默跳过，不影响抓取主流程。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from .config import Settings
from .models import to_local

# 各渠道成功判定与接口地址
PUSHPLUS_URL = "https://www.pushplus.plus/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/{key}.send"
SERVERCHAN3_URL = "https://{uid}.push.ft07.com/send/{key}.send"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


@dataclass(slots=True, frozen=True)
class NotifyMessage:
    """一条推送消息，标题 + Markdown 正文。"""

    title: str
    content: str


def build_message(
    trader_id: str,
    order_lines: list[str],
    closed_lines: list[str],
    trader_name: str | None = None,
) -> NotifyMessage:
    """把已格式化（北京时间）的记录行包装成微信推送消息。

    Args:
        trader_id: 交易员 portfolio id。
        order_lines: 订单文本行，来自 live.format_order。
        closed_lines: 平仓文本行，来自 live.format_closed。
        trader_name: 交易员别名，缺省时显示"熬鹰资本"。

    Returns:
        标题与 Markdown 正文。
    """
    name = trader_name or "熬鹰资本"
    now = to_local(datetime.now(UTC)).strftime("%m-%d %H:%M")
    title = f"{name} {now} 最新交易"
    lines = [f"### {name}（{trader_id}）", f"抓取时间：{now}", ""]

    if order_lines:
        lines.append(f"**最新 {len(order_lines)} 笔订单**")
        lines.extend(f"- {line}" for line in order_lines)
        lines.append("")

    if closed_lines:
        lines.append(f"**最新 {len(closed_lines)} 笔平仓**")
        lines.extend(f"- {line}" for line in closed_lines)

    return NotifyMessage(title=title, content="\n".join(lines).rstrip())


class BaseNotifier:
    """通知渠道基类。"""

    async def send(self, message: NotifyMessage) -> str:
        """发送消息，返回 ``ok`` 或错误描述。子类实现。"""
        raise NotImplementedError

    async def aclose(self) -> None:
        """关闭 HTTP 客户端。子类按需实现。"""


class PushPlusNotifier(BaseNotifier):
    """PushPlus 推送加：微信公众号直接推到个人微信。"""

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(timeout=15.0)

    async def send(self, message: NotifyMessage) -> str:
        """POST 到 PushPlus 发送消息接口，code=200 视为成功。"""
        try:
            response = await self._client.post(
                PUSHPLUS_URL,
                json={
                    "token": self._token,
                    "title": message.title,
                    "content": message.content,
                    "template": "markdown",
                    "channel": "wechat",
                },
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return f"PushPlus 请求失败: {exc}"
        if body.get("code") != 200:
            return f"PushPlus 推送失败: {body.get('msg') or body}"
        return "ok"

    async def aclose(self) -> None:
        await self._client.aclose()


class ServerChanNotifier(BaseNotifier):
    """Server酱：兼容 Turbo(SCT) 与 Server酱³(sctp) 两种 SendKey。"""

    def __init__(self, key: str) -> None:
        self._key = key
        if key.startswith("sctp") and "t" in key[4:]:
            uid = key[4:].split("t", 1)[0]
            self._url = SERVERCHAN3_URL.format(uid=uid, key=key)
        else:
            self._url = SERVERCHAN_URL.format(key=key)
        self._client = httpx.AsyncClient(timeout=15.0)

    async def send(self, message: NotifyMessage) -> str:
        """POST 到 Server酱，title 不含换行，正文放 desp。"""
        try:
            response = await self._client.post(
                self._url,
                data={"title": message.title, "desp": message.content},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return f"Server酱 请求失败: {exc}"
        if body.get("code") != 0:
            return f"Server酱 推送失败: {body.get('message') or body}"
        return "ok"

    async def aclose(self) -> None:
        await self._client.aclose()


class WeComNotifier(BaseNotifier):
    """企业微信群机器人：通过 webhook key 推送 Markdown 到群。"""

    def __init__(self, key: str) -> None:
        self._url = f"{WECOM_URL}?key={key}"
        self._client = httpx.AsyncClient(timeout=15.0)

    async def send(self, message: NotifyMessage) -> str:
        """POST 到企业微信机器人 webhook，errcode=0 视为成功。"""
        try:
            response = await self._client.post(
                self._url,
                json={"msgtype": "markdown", "markdown": {"content": message.content}},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return f"企业微信 请求失败: {exc}"
        if body.get("errcode") != 0:
            return f"企业微信 推送失败: {body.get('errmsg') or body}"
        return "ok"

    async def aclose(self) -> None:
        await self._client.aclose()


def build_notifier(settings: Settings) -> BaseNotifier | None:
    """根据配置构建通知渠道；未启用或凭据缺失时返回 None。

    Args:
        settings: 运行时配置。

    Returns:
        通知器实例，或 None（表示不推送）。
    """
    channel = settings.notify_channel.strip().lower()
    if channel in ("", "none", "off"):
        return None
    if channel == "pushplus":
        if not settings.pushplus_token:
            return None
        return PushPlusNotifier(settings.pushplus_token)
    if channel == "serverchan":
        if not settings.serverchan_key:
            return None
        return ServerChanNotifier(settings.serverchan_key)
    if channel == "wecom":
        if not settings.wecom_webhook_key:
            return None
        return WeComNotifier(settings.wecom_webhook_key)
    return None
