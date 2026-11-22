"""页面快照领域服务：页面DOM快照的采集与存储，消费login_mgr凭证和canvas_config配置。

一份捕获从落盘起就带有**捕获身份**（capture_id）和**运行归属**（run_id），
文件名也带运行前缀，避免不同运行抓到的同一页面互相覆盖。确认运行时只读取
当前运行、且已经完整落成的捕获；仍在写入或读取失败的文件不会拿来充数。
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app import persistence as persist
# 完整落成、可供回查的捕获状态。
_COMPLETE_STATUSES = {
    "complete", "completed", "finalized", "done", "ok",
    "已落成", "已完成", "已存档", "已采集",
}
# 仍在写入或已经中断的捕获状态。
_INCOMPLETE_STATUSES = {
    "writing", "pending", "capturing", "in_progress", "running",
    "写入中", "采集中", "待落成", "已中断", "aborted", "failed", "已失败",
}
_RUN_ID_KEYS = ("run_id", "运行标识", "关联运行", "run_entry_id", "run_key", "run")
_CONTENT_KEYS = ("content", "raw_dom", "dom", "html", "text", "dom_preview", "dom_hash")


def _norm_run_id(value: Any) -> str | None:
    """运行标识统一成字符串比较，兼容 1 与 "1" 两种写法。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _run_id_set(value: Any) -> set[str]:
    """把单个运行标识或一组别名统一成可匹配的字符串集合。"""
    if isinstance(value, (list, tuple, set, frozenset)):
        return {token for item in value if (token := _norm_run_id(item)) is not None}
    token = _norm_run_id(value)
    return {token} if token is not None else set()


def capture_run_id(capture: dict[str, Any]) -> str | None:
    """从捕获记录中取出运行归属，兼容中英文字段名。"""
    for key in _RUN_ID_KEYS:
        value = capture.get(key)
        normalized = _norm_run_id(value)
        if normalized is not None:
            return normalized
    return None


def capture_status(capture: dict[str, Any]) -> str:
    value = capture.get("status")
    if value is None:
        value = capture.get("capture_status")
    if value is None:
        value = capture.get("捕获状态")
    if value is None:
        return "complete"
    return str(value).strip().casefold()


def _incomplete_flag(capture: dict[str, Any]) -> bool:
    """兼容用布尔标记表达写入中/未完成的情况。"""
    for key in ("writing", "in_progress", "pending", "写入中"):
        if capture.get(key) is True:
            return True
    for key in ("complete", "completed", "finalized", "已落成"):
        if capture.get(key) is False:
            return True
    return False


def _has_content(capture: dict[str, Any]) -> bool:
    for key in _CONTENT_KEYS:
        value = capture.get(key)
        if value not in (None, ""):
            return True
    return bool(capture.get("raw_dom_length"))


def is_verifiable_capture(capture: dict[str, Any]) -> bool:
    """可回查 = 状态为完整落成 + 有正文/指纹可参与去重。"""
    status = capture_status(capture)
    if status in _INCOMPLETE_STATUSES or _incomplete_flag(capture):
        return False
    if status in _COMPLETE_STATUSES:
        return _has_content(capture)
    # 兼容没有状态字段的历史捕获：只要正文可读且没有未完成标记就视为已落成。
    return _has_content(capture)


def _safe_token(value: Any) -> str:
    text = str(value)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "capture"


class PageSnapshotService(BaseModuleService):
    MODULE = "page_snapshot"
    ENTITY = "页面快照"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["快照编号", "采集来源", "页面版本", "DOM摘要", "快照时间", "快照类型", "数据量", "回收时间"]
    REQUIRED_FIELDS = ["快照编号", "采集来源", "页面版本"]
    STATUS_ORDER = ["草稿", "已存档", "已清洗", "已回收"]
    ACTION_RULES = {"标记存档": "已存档", "确认清洗": "已清洗", "启动回收": "已回收"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------
    def capture_snapshot(
        self,
        page_url: str,
        dom_hash: str,
        raw_dom: str,
        run_id: Any = None,
        *,
        content: str | None = None,
        status: str = "complete",
        capture_id: str | None = None,
        captured_at: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """采集一份页面快照并落盘。

        run_id 是运行归属；不传时会接受 run_identifier/run_entry_id/关联运行 等别名。
        status="writing" 可登记一份尚在写入的捕获，之后用 finish_capture 落成。
        文件名带运行前缀与唯一捕获身份，同一页面被多次采集不会互相覆盖。
        """
        if run_id is None:
            for alias in ("run_identifier", "run_entry_id", "关联运行", "运行标识", "run_key"):
                if alias in kwargs:
                    run_id = kwargs.pop(alias)
                    break

        run_token = f"r{_norm_run_id(run_id)}" if _norm_run_id(run_id) else "unassigned"
        capture_id = str(capture_id) if capture_id else (
            time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        filename = f"page_{_safe_token(run_token)}_{_safe_token(capture_id)}.json"

        body = raw_dom if content is None else content
        snapshot: dict[str, Any] = {
            "capture_id": capture_id,
            "run_id": int(run_id) if _norm_run_id(run_id) and str(run_id).strip().isdigit() else run_id,
            "run_key": _norm_run_id(run_id),
            "page_url": page_url,
            "dom_hash": dom_hash,
            "content_hash": dom_hash,
            "content": body,
            "raw_dom_length": len(body or ""),
            "dom_preview": (body or "")[:500],
            "captured_at": captured_at or persist.now_iso(),
            "timestamp": captured_at or persist.now_iso(),
            "status": status,
            "capture_status": status,
        }
        persist.write_json("snapshots", filename, snapshot)
        return {**snapshot, "_file": filename}

    def begin_capture(
        self,
        page_url: str,
        dom_hash: str,
        run_id: Any = None,
        *,
        capture_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """登记一份正在写入的捕获：此时可见但不可回查。"""
        return self.capture_snapshot(
            page_url, dom_hash, "", run_id,
            status="writing", capture_id=capture_id, **kwargs,
        )

    def finish_capture(self, capture_id: Any, raw_dom: str, *, content: str | None = None) -> dict[str, Any] | None:
        """把仍在写入的捕获落成完整捕获，保留原有运行归属与捕获身份。"""
        normalized = _norm_run_id(capture_id)
        for name in persist.list_capture_files():
            payload = persist.read_json("snapshots", name)
            if not payload:
                continue
            if _norm_run_id(payload.get("capture_id")) != normalized:
                continue
            body = raw_dom if content is None else content
            payload.update({
                "content": body,
                "raw_dom_length": len(body or ""),
                "dom_preview": (body or "")[:500],
                "status": "complete",
                "capture_status": "complete",
                "captured_at": payload.get("captured_at") or persist.now_iso(),
            })
            persist.write_json("snapshots", name, payload)
            return {**payload, "_file": name}
        return None

    def abort_capture(self, capture_id: Any, reason: str = "capture_interrupted") -> dict[str, Any] | None:
        """把一份捕获标记为中断，之后不会进入任何运行报告。"""
        normalized = _norm_run_id(capture_id)
        for name in persist.list_capture_files():
            payload = persist.read_json("snapshots", name)
            if not payload:
                continue
            if _norm_run_id(payload.get("capture_id")) != normalized:
                continue
            payload.update({
                "status": "aborted",
                "capture_status": "aborted",
                "abort_reason": reason,
            })
            persist.write_json("snapshots", name, payload)
            return {**payload, "_file": name}
        return None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_captures(self, run_id: Any = None, *, include_unverifiable: bool = True) -> list[dict[str, Any]]:
        """读取持久化的页面捕获。

        给出 run_id 时只返回归属于该运行的捕获；也接受一组别名（如同时匹配
        entry id 与记录编号），命中任一即算归属本运行。
        """
        expected = _run_id_set(run_id)
        captures: list[dict[str, Any]] = []
        for name in persist.list_capture_files():
            payload = persist.read_json("snapshots", name)
            if not payload:
                continue
            owner = capture_run_id(payload)
            if expected and owner not in expected:
                continue
            verified = is_verifiable_capture(payload)
            if not include_unverifiable and not verified:
                continue
            captures.append({
                **payload,
                "_file": name,
                "_run_id": owner,
                "_verified": verified,
            })
        captures.sort(key=lambda item: (
            str(item.get("captured_at") or item.get("timestamp") or ""),
            str(item.get("capture_id") or item.get("_file") or ""),
        ))
        return captures

    def list_run_captures(self, run_id: Any, *, verified_only: bool = True) -> list[dict[str, Any]]:
        """运行报告只允许读取当次运行的可回查捕获。"""
        return self.list_captures(run_id, include_unverifiable=not verified_only)

    def run_capture_stats(self, run_id: Any) -> dict[str, Any]:
        """统计当次运行的捕获：已落成、仍在写入、无法回查，供空样本原因核对。"""
        expected = _run_id_set(run_id)
        verified: list[dict[str, Any]] = []
        incomplete: list[dict[str, Any]] = []
        unreadable = 0
        seen_files = 0

        for name in persist.list_capture_files():
            payload = persist.read_json("snapshots", name)
            if not payload:
                # 文件存在但读不出来：文件名带运行前缀时才能确认归属。
                if expected and any(f"_r{_safe_token(token)}_" in name for token in expected):
                    unreadable += 1
                continue
            owner = capture_run_id(payload)
            if expected and owner not in expected:
                continue
            seen_files += 1
            record = {**payload, "_file": name, "_run_id": owner}
            if is_verifiable_capture(payload):
                verified.append(record)
            else:
                status = capture_status(payload)
                if status in ("aborted", "interrupted", "failed", "已中断", "已失败"):
                    reason = "capture_interrupted"
                elif status in _INCOMPLETE_STATUSES or _incomplete_flag(payload):
                    reason = "capture_in_progress"
                else:
                    reason = "capture_without_content"
                record["_unverified_reason"] = reason
                incomplete.append(record)

        verified.sort(key=lambda item: (
            str(item.get("captured_at") or item.get("timestamp") or ""),
            str(item.get("capture_id") or ""),
        ))
        incomplete.sort(key=lambda item: (
            str(item.get("captured_at") or item.get("timestamp") or ""),
            str(item.get("capture_id") or ""),
        ))
        return {
            "run_id": run_id,
            "verified": verified,
            "incomplete": incomplete,
            "unreadable": unreadable,
            "files_seen": seen_files,
            "captures_seen": seen_files,
            "verified_count": len(verified),
            "incomplete_count": len(incomplete),
        }

    # ------------------------------------------------------------------
    # 登记入口：带运行归属的页面快照登记也会落成一份可回查捕获
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None or missing:
            return entry, missing
        run_id = None
        for key in _RUN_ID_KEYS:
            if _norm_run_id(values.get(key)) is not None:
                run_id = values.get(key)
                break
        if run_id is None:
            return entry, missing
        body = values.get("raw_dom") or values.get("content") or values.get("DOM摘要") or ""
        dom_hash = str(values.get("dom_hash") or values.get("DOM摘要") or entry.get("快照编号", ""))
        self.capture_snapshot(
            str(values.get("采集来源") or entry.get("采集来源") or ""),
            dom_hash,
            str(body),
            run_id,
            status="complete",
            capture_id=f"row-{entry.get('id')}",
        )
        return entry, missing

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        snap_files = persist.list_capture_files()
        base["snapshot_files"] = len(snap_files)
        # 关联的登录凭证
        credentials = store.rows("login_mgr")
        snaps = store.rows(self.MODULE)
        sites = {r.get("采集来源") for r in snaps}
        base["sites_with_credentials"] = sum(1 for c in credentials if c.get("目标站点") in sites)
        # 捕获归属与可回查统计
        verified = incomplete = unassigned = 0
        for name in snap_files:
            payload = persist.read_json("snapshots", name)
            if not payload:
                continue
            if capture_run_id(payload) is None:
                unassigned += 1
            elif is_verifiable_capture(payload):
                verified += 1
            else:
                incomplete += 1
        base["verified_captures"] = verified
        base["incomplete_captures"] = incomplete
        base["unassigned_captures"] = unassigned
        return base
