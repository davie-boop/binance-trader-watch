"""触发脚本测试：确认交易员列表作为 workflow inputs 传出。"""

from __future__ import annotations

from cron_dispatch import build_request


def test_build_request_without_inputs() -> None:
    """不传交易员时请求体只含 ref，云端会回落到 Secret。"""
    _, _, payload = build_request("owner/repo", "fetch.yml", "main")
    assert payload == {"ref": "main"}


def test_build_request_with_trader_inputs() -> None:
    """传入的交易员 ID 与别名映射进入 inputs。"""
    inputs = {"trader_ids": "111,222", "trader_labels": '{"111":"熬鹰资本","222":"爽翻翻"}'}
    _, _, payload = build_request("owner/repo", "fetch.yml", "main", inputs)
    assert payload == {"ref": "main", "inputs": inputs}


def test_build_request_drops_empty_inputs() -> None:
    """inputs 全为空字符串时不发送空 inputs 字段。"""
    _, _, payload = build_request(
        "owner/repo", "fetch.yml", "main", {"trader_ids": "", "trader_labels": ""}
    )
    assert payload == {"ref": "main"}
