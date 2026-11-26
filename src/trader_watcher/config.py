"""配置管理：全部通过环境变量注入，避免把密钥写进代码。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置。

    环境变量前缀为 ``TW_``，例如 ``TW_KOPYON_API_KEY=xxx``。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TW_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 第三方聚合 API（Kopyon）的密钥；免费额度 10000 次/月，足够个人使用。
    kopyon_api_key: str = Field(default="", description="Kopyon API Key")
    kopyon_base_url: str = Field(
        default="https://api.kopyon.com/v5/binance-copy-trading",
        description="Copy Trading 数据接口根地址",
    )

    # 用逗号分隔的交易员 portfolio id，例如 "123,456"。
    trader_ids: str = Field(default="", description="逗号分隔的 trader/portfolio id")
    trader_labels: str = Field(
        default="", description="交易员别名映射 JSON，如 {\"507...\":\"熬鹰资本\"}"
    )
    poll_interval: int = Field(default=60, ge=5, description="轮询间隔（秒）")
    db_path: Path = Field(default=Path("data/trader_positions.db"), description="SQLite 数据库路径")

    # 微信通知（可选）。channel: pushplus / serverchan / wecom / none
    notify_channel: str = Field(default="", description="通知渠道，留空表示不推送")
    notify_only_new: bool = Field(
        default=False, description="true 只在有新记录时推送；false 则每次抓取都推送最新 N 条"
    )
    pushplus_token: str = Field(default="", description="PushPlus token")
    serverchan_key: str = Field(default="", description="Server酱 SendKey（SCT/sctp 开头）")
    wecom_webhook_key: str = Field(default="", description="企业微信群机器人 webhook 的 key")

    @field_validator("trader_ids", mode="after")
    @classmethod
    def _strip_ids(cls, value: str) -> str:
        """去掉 id 列表中的空白字符，保持原始逗号分隔格式。"""
        return ",".join(part.strip() for part in value.split(",") if part.strip())

    @property
    def trader_id_list(self) -> list[str]:
        """返回清洗后的交易员 id 列表。"""
        return [part.strip() for part in self.trader_ids.split(",") if part.strip()]

    @property
    def trader_label_map(self) -> dict[str, str]:
        """返回交易员 id -> 别名映射；JSON 非法时返回空字典。"""
        try:
            data = json.loads(self.trader_labels)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """数据源连接参数。"""

    api_key: str
    base_url: str
    timeout: float = 10.0
    max_retries: int = 3
