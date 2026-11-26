#!/usr/bin/env python3
"""外部 cron 兜底：触发 GitHub Actions 的 workflow_dispatch。

用法：
    python cron_dispatch.py --dry-run
    TW_GITHUB_TOKEN=xxx python cron_dispatch.py
    或把 token 写入同目录 .cron.env（已在 .gitignore 中忽略）：
        TW_GITHUB_TOKEN=github_pat_xxx

GitHub 官方文档：workflow_dispatch 接口成功时返回 HTTP 204。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_REPO = "davie-boop/binance-trader-watch"
DEFAULT_WORKFLOW = "fetch.yml"
DEFAULT_REF = "main"
API_VERSION = "2022-11-28"
BEIJING_TZ = timezone(timedelta(hours=8))


def load_env_file(path: Path) -> dict[str, str]:
    """读取简单的 KEY=VALUE 环境文件；不存在时返回空字典。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_token(env_file: Path | None) -> str | None:
    """按环境变量、指定文件、默认 .cron.env 的顺序查找 token。"""
    token = os.getenv("TW_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        return token.strip()

    candidates: list[Path] = []
    if env_file:
        candidates.append(env_file)
    else:
        candidates.append(Path(__file__).resolve().parent / ".cron.env")

    for path in candidates:
        token = load_env_file(path).get("TW_GITHUB_TOKEN")
        if token:
            return token
    return None


def mask(token: str | None) -> str:
    """脱敏显示 token，避免日志泄露。"""
    if not token:
        return "<未设置>"
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def build_request(
    repo: str,
    workflow: str,
    ref: str,
    inputs: dict[str, str] | None = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """构造 GitHub workflow_dispatch 请求；inputs 会作为 workflow 输入传入。"""
    url = (
        "https://api.github.com/repos/"
        f"{quote(repo, safe='/')}/actions/workflows/"
        f"{quote(workflow, safe='')}/dispatches"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "binance-trader-watch-cron/1.0",
    }
    payload: dict[str, Any] = {"ref": ref}
    clean_inputs = {key: value for key, value in (inputs or {}).items() if value}
    if clean_inputs:
        payload["inputs"] = clean_inputs
    return url, headers, payload


def now_beijing() -> str:
    """返回北京时间字符串，便于核对 cron 是否按时触发。"""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S %z")


def dispatch_workflow(
    token: str,
    *,
    repo: str = DEFAULT_REPO,
    workflow: str = DEFAULT_WORKFLOW,
    ref: str = DEFAULT_REF,
    inputs: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[bool, str]:
    """执行一次 workflow_dispatch 请求，供 CLI 与 GUI 定时器共用。

    Args:
        token: GitHub PAT。
        repo: owner/repo。
        workflow: workflow 文件名。
        ref: 分支或标签。
        inputs: 传给 workflow_dispatch 的输入（如 trader_ids / trader_labels）。
        timeout: 请求超时秒数。

    Returns:
        (是否成功, 中文结果消息)。网络异常或 HTTP 非 204 均视为失败。
    """
    url, headers, payload = build_request(repo, workflow, ref, inputs)
    headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        return False, f"网络请求失败：{exc}"

    if response.status_code == 204:
        return True, "触发成功：GitHub Actions 已收到抓取请求。"

    detail = response.text.strip()
    message = f"GitHub API 返回 HTTP {response.status_code}"
    if detail:
        message += f"：{detail[:300]}"
    if response.status_code in (401, 403):
        message += "（token 无效、过期或权限不足）"
    elif response.status_code == 404:
        message += "（仓库或 workflow 名错误，或 token 无权访问）"
    elif response.status_code == 422:
        message += "（workflow_dispatch 未启用或 ref 分支不存在）"
    return False, message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="触发 binance-trader-watch 的 GitHub Actions 抓取任务")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub 仓库，形如 owner/repo")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="workflow 文件名，默认 fetch.yml")
    parser.add_argument("--ref", default=DEFAULT_REF, help="分支或标签，默认 main")
    parser.add_argument("--env-file", type=Path, default=None, help="额外的 token 文件，默认读取脚本旁 .cron.env")
    parser.add_argument("--trader-ids", default=None, help="逗号分隔的交易员 ID，传入云端一次性抓取")
    parser.add_argument("--trader-labels", default=None, help="交易员别名映射 JSON，用于推送标题")
    parser.add_argument("--timeout", type=float, default=20.0, help="请求超时秒数，默认 20")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求内容，不真正触发")
    args = parser.parse_args(argv)

    inputs = {
        "trader_ids": args.trader_ids or "",
        "trader_labels": args.trader_labels or "",
    }
    token = resolve_token(args.env_file)
    url, headers, payload = build_request(args.repo, args.workflow, args.ref, inputs)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    safe_headers = dict(headers)
    if "Authorization" in safe_headers:
        safe_headers["Authorization"] = f"Bearer {mask(token)}"

    print(f"[{now_beijing()}] 请求地址：{url}")
    print(f"[{now_beijing()}] Headers：{safe_headers}")
    print(f"[{now_beijing()}] Body：{payload}")

    if args.dry_run:
        print("dry-run 完成，未发送真实请求。")
        return 0

    if not token:
        print("错误：未找到 GitHub token。请设置 TW_GITHUB_TOKEN 或写入 .cron.env。", file=sys.stderr)
        return 2

    ok, message = dispatch_workflow(
        token,
        repo=args.repo,
        workflow=args.workflow,
        ref=args.ref,
        inputs=inputs,
        timeout=args.timeout,
    )
    if ok:
        print(f"[{now_beijing()}] {message}")
        return 0
    print(f"错误：{message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
