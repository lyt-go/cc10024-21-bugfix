"""共享文件持久化：跨模块数据产物读写。
data/runs/     → 运行历史JSONL
data/snapshots/ → 页面快照JSON
data/reports/   → 质量报告JSON
data/exports/   → 导出文件
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", "data"))


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def data_path(sub: str, filename: str) -> str:
    p = os.path.join(_DATA_ROOT, sub, filename)
    _ensure_dir(os.path.dirname(p))
    return p


def write_json(sub: str, filename: str, data: dict[str, Any]) -> str:
    """原子写入：先写同目录临时文件再替换，避免并发读取拿到写了一半的JSON。"""
    path = data_path(sub, filename)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return path


def read_json(sub: str, filename: str) -> dict[str, Any] | None:
    path = data_path(sub, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # 捕获文件可能正在落盘：读不到就当作本次不可回查，绝不拿别的内容顶上。
        return None


def append_jsonl(sub: str, filename: str, record: dict[str, Any]) -> str:
    path = data_path(sub, filename)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path


def read_jsonl(sub: str, filename: str) -> list[dict[str, Any]]:
    path = data_path(sub, filename)
    if not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def list_files(sub: str, suffix: str = "") -> list[str]:
    path = os.path.join(_DATA_ROOT, sub)
    if not os.path.isdir(path):
        return []
    names = sorted(os.listdir(path))
    if suffix:
        names = [n for n in names if n.endswith(suffix)]
    return names


def list_capture_files() -> list[str]:
    """只列出可回查的页面捕获文件，忽略原子写入残留的临时文件。"""
    return [
        name for name in list_files("snapshots", ".json")
        if name.startswith("page_") and not name.startswith(".tmp_")
    ]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")