"""云端状态文件合并测试：并发运行提交前的并集与去重。"""

from __future__ import annotations

import json
from pathlib import Path

from trader_watcher.cloud import MAX_SEEN, merge_state_files


def _write(path: Path, data: dict[str, object]) -> None:
    """把字典写成 JSON 状态文件。"""
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_merge_union_dedup_and_metadata(tmp_path: Path) -> None:
    """远程与本轮 seen 取并集去重，initialized 取或，updated_at 取较新。"""
    remote = tmp_path / "remote.json"
    local = tmp_path / "local.json"
    output = tmp_path / "out.json"
    _write(remote, {"seen": ["a", "b"], "initialized": True,
                    "initialized_traders": ["t1"], "updated_at": "2026-11-26T10:00:00+08:00"})
    _write(local, {"seen": ["b", "c"], "initialized": True,
                   "initialized_traders": ["t2"], "updated_at": "2026-11-26T10:15:00+08:00"})

    count = merge_state_files(str(local), str(remote), str(output))
    data = json.loads(output.read_text(encoding="utf-8"))

    assert count == 3
    assert data["seen"] == ["a", "b", "c"]
    assert data["initialized"] is True
    assert data["initialized_traders"] == ["t1", "t2"]
    assert data["updated_at"] == "2026-11-26T10:15:00+08:00"
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_merge_missing_remote_file(tmp_path: Path) -> None:
    """远程状态文件不存在时，仍能保留本轮结果。"""
    local = tmp_path / "local.json"
    output = tmp_path / "out.json"
    _write(local, {"seen": ["x"], "initialized": True, "updated_at": "2026-11-26T11:00:00+08:00"})

    count = merge_state_files(str(local), str(tmp_path / "missing.json"), str(output))
    data = json.loads(output.read_text(encoding="utf-8"))

    assert count == 1
    assert data["seen"] == ["x"]
    assert data["initialized"] is True


def test_merge_caps_seen(tmp_path: Path) -> None:
    """合并后 seen 不超过 MAX_SEEN，且保留最新的一批。"""
    remote = tmp_path / "remote.json"
    local = tmp_path / "local.json"
    output = tmp_path / "out.json"
    _write(remote, {"seen": [f"r{i}" for i in range(MAX_SEEN + 20)], "initialized": True})
    _write(local, {"seen": ["new-a", "new-b"], "initialized": True})

    count = merge_state_files(str(local), str(remote), str(output))
    data = json.loads(output.read_text(encoding="utf-8"))

    assert count == MAX_SEEN
    assert len(data["seen"]) == MAX_SEEN
    assert data["seen"][-2:] == ["new-a", "new-b"]
