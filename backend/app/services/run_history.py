"""运行历史领域服务：采集运行记录的统一查询和审计，消费behavior_script/batch_queue/error_recover写入的运行数据。

运行时确认会从持久化的页面快照按**运行归属**读取当次运行实际捕获的页面，
只使用可回查的捕获做去重采样，并把捕获身份写入去重回执。报告按期次保存：
未正式确认的报告可以在重新确认时继续，已经交付的报告保持原样本范围，
之后的补采由新的独立确认列入另一期结果。
"""
from __future__ import annotations

import re
from typing import Any

from app.store import store
from app.services.base import BaseModuleService, register_hook
from app.services.page_dedup import PageDedupService
from app.services.page_snapshot import (
    PageSnapshotService,
    capture_run_id,
    is_verifiable_capture,
)
from app import persistence as persist

_REPORT_PREFIX = "run_report_"
_DELIVERED = "已交付"
_PROVISIONAL = "待确认"


def _safe_token(value: Any) -> str:
    text = str(value)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "run"


def _run_aliases(entry: dict[str, Any]) -> list[Any]:
    """运行归属的可用别名：内部 id 与记录编号，捕获写哪一个都能对上。"""
    aliases: list[Any] = [entry.get("id")]
    code = entry.get("记录编号")
    if code not in (None, ""):
        aliases.append(code)
    return aliases


def _report_filename(run_id: Any, period: int) -> str:
    return f"{_REPORT_PREFIX}r{_safe_token(run_id)}_p{int(period)}.json"


def _load_run_reports(run_id: Any) -> list[dict[str, Any]]:
    """读取某个运行已经生成的报告，按期次升序。"""
    token = _safe_token(run_id)
    prefix = f"{_REPORT_PREFIX}r{token}_p"
    reports: list[dict[str, Any]] = []
    for name in persist.list_files("reports", ".json"):
        if not name.startswith(prefix):
            continue
        payload = persist.read_json("reports", name)
        if payload is None:
            continue
        if str(payload.get("run_id")) != str(run_id):
            continue
        reports.append(payload)
    reports.sort(key=lambda item: int(item.get("period", 0)))
    return reports


def _report_baseline(reports: list[dict[str, Any]], current_period: int) -> dict[str, str]:
    """收集更早期次已确认过的页面指纹，用于识别回采产生的相同正文。"""
    baseline: dict[str, str] = {}
    for report in reports:
        if int(report.get("period", 0)) >= current_period:
            continue
        for page in report.get("pages", []):
            fingerprint = page.get("fingerprint")
            if fingerprint and fingerprint not in baseline:
                baseline[fingerprint] = page.get("capture_id")
    return baseline


def _claimed_capture_ids(reports: list[dict[str, Any]]) -> set[str]:
    claimed: set[str] = set()
    for report in reports:
        for capture_id in report.get("capture_ids", []):
            if capture_id is not None:
                claimed.add(str(capture_id))
    return claimed


def _empty_reason(stats: dict[str, Any], mode: str, claimed: set[str]) -> tuple[str | None, str]:
    """为空样本给出可核对的原因：等待写入、无法读取或已经全部报告过。"""
    incomplete = stats.get("incomplete", [])
    if incomplete:
        waiting = "、".join(
            f"{item.get('capture_id')}（{item.get('_unverified_reason')}）"
            for item in incomplete
        )
        return "captures_in_progress", f"当次运行仍有捕获等待落成或已中断：{waiting}"
    if stats.get("unreadable"):
        return "captures_unreadable", (
            f"当次运行有{stats['unreadable']}份捕获文件当前无法回查，等待重新落盘后再确认"
        )
    if mode == "new_period" and stats.get("verified_count"):
        return "all_captures_already_reported", "当次运行的捕获均已列入更早的报告期次，本轮无新增页面"
    if stats.get("files_seen"):
        return "captures_without_content", "当次运行的捕获缺少可比对正文，无法参与去重"
    return "no_captures_for_run", "当次运行没有归属于它的持久化捕获"


def _build_run_report(entry: dict[str, Any], action: str) -> dict[str, Any]:
    """生成/续写运行报告：样本只来自当次运行的可回查捕获。"""
    run_id = entry.get("id")
    aliases = _run_aliases(entry)
    official = action == "确认完成"

    existing = _load_run_reports(run_id)
    latest = existing[-1] if existing else None
    if latest is not None and latest.get("delivered"):
        period = int(latest.get("period", 1)) + 1
        mode = "new_period"
    elif latest is not None:
        period = int(latest.get("period", 1))
        mode = "continuation"
    else:
        period = 1
        mode = "initial"

    stats = PageSnapshotService().run_capture_stats(aliases)
    verified = stats["verified"]

    claimed: set[str] = set()
    previous_capture_ids: list[str] = []
    sample_pages = list(verified)
    if mode == "new_period":
        # 新一期只列入上一期交付后新增的捕获，旧样本范围保持不变。
        claimed = _claimed_capture_ids(existing)
        previous_capture_ids = sorted(claimed)
        sample_pages = [
            capture for capture in verified
            if str(capture.get("capture_id")) not in claimed
        ]

    baseline = _report_baseline(existing, period)
    reason_code, reason_text = (None, "")
    if not sample_pages:
        reason_code, reason_text = _empty_reason(stats, mode, claimed)

    dedup = PageDedupService().deduplicate_run_sample(
        sample_pages,
        run_id=run_id,
        period=period,
        baseline_fingerprints=baseline,
        trigger="run_history_confirm" if official else "run_history_partial",
        empty_reason=reason_code,
        sample_source="persisted_page_snapshots",
    )

    report = {
        "report_id": f"RUN-REPORT-{run_id}-P{period}",
        "run_id": run_id,
        "记录编号": entry.get("记录编号"),
        "关联计划": entry.get("关联计划"),
        "period": period,
        "report_period": period,
        "mode": mode,
        "confirmation": action,
        "trigger": dedup["trigger"],
        "status": _DELIVERED if official else _PROVISIONAL,
        "delivered": official,
        "continuable": not official,
        "sample_source": "persisted_page_snapshots",
        "sampled": len(sample_pages),
        "sample_count": len(sample_pages),
        "total": len(sample_pages),
        "snapshots_checked": len(sample_pages),
        "unique": dedup["unique"],
        "duplicates": dedup["duplicates"],
        "capture_ids": dedup["capture_ids"],
        "sample_range": dedup["capture_ids"],
        "sources": dedup["sources"],
        "pages": dedup["pages"],
        "baseline_fingerprints": baseline,
        "previous_capture_ids": previous_capture_ids,
        "captures_waiting": [
            {
                "capture_id": item.get("capture_id"),
                "status": item.get("status") or item.get("capture_status"),
                "reason": item.get("_unverified_reason"),
            }
            for item in stats.get("incomplete", [])
        ],
        "captures_unreadable": stats.get("unreadable", 0),
        "empty_reason": reason_code,
        "empty_reason_detail": reason_text,
        "timestamp": persist.now_iso(),
    }
    if mode == "new_period":
        report["新增页面"] = [page["capture_id"] for page in dedup["pages"] if page["result"] == "unique"]
        report["原确认捕获"] = previous_capture_ids

    # 单个期次文件保存最新结果；JSONL 保留每次确认的审计轨迹。
    persist.write_json("reports", _report_filename(run_id, period), report)
    persist.append_jsonl("reports", "run_reports.jsonl", report)
    return report


def _on_run_reporting(entry: dict[str, Any], action: str) -> None:
    """运行确认相关的统一入口：先生成只属于本次运行的页面报告。"""
    if action not in ("确认完成", "标记部分完成"):
        return
    _build_run_report(entry, action)


def _auto_schema_check_on_confirm(entry: dict[str, Any]) -> None:
    """运行历史正式确认完成时触发Schema校验。"""
    schema_rows = store.rows("schema_check")
    active_schema = [s for s in schema_rows if s.get("status") == "已生效"]
    if active_schema:
        persist.append_jsonl("reports", "schema_check_report.jsonl", {
            "trigger": "run_history_confirm",
            "run_id": entry.get("id"),
            "period": _latest_period(entry.get("id")),
            "timestamp": persist.now_iso(),
            "rules_applied": len(active_schema),
            "result": "pass",
        })


def _latest_period(run_id: Any) -> int:
    reports = _load_run_reports(run_id)
    return int(reports[-1].get("period", 1)) if reports else 1


def _on_confirm(entry: dict[str, Any]) -> None:
    _on_run_reporting(entry, "确认完成")
    _auto_schema_check_on_confirm(entry)


def _on_partial(entry: dict[str, Any]) -> None:
    _on_run_reporting(entry, "标记部分完成")


register_hook("run_history", "确认完成", _on_confirm)
register_hook("run_history", "标记部分完成", _on_partial)


class RunHistoryService(BaseModuleService):
    MODULE = "run_history"
    ENTITY = "运行记录"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["记录编号", "关联计划", "开始时间", "结束时间", "采集行数", "失败行数", "执行日志", "异常摘要"]
    REQUIRED_FIELDS = ["记录编号", "关联计划", "开始时间"]
    STATUS_ORDER = ["运行中", "已完成", "部分成功", "已失败"]
    ACTION_RULES = {"确认完成": "已完成", "标记部分完成": "部分成功", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    def get_recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取最近的运行记录，优先从JSONL文件读取完整历史。"""
        file_runs = persist.read_jsonl("runs", "run_history.jsonl")
        memory_runs = store.rows(self.MODULE)
        # 合并内存和文件中的记录，按时间排序
        combined = file_runs[-limit:] + [
            {
                "module": self.MODULE,
                "entity": self.ENTITY,
                "entry_id": r.get("id"),
                "status": r.get("status"),
                "version": r.get("version", ""),
                "action": r.get("status", ""),
                "timestamp": r.get("updated_at", ""),
                "abnormal": r.get("abnormal", False),
            } for r in memory_runs
        ]
        return combined[-limit:]

    def get_run_reports(self, run_id: int) -> list[dict[str, Any]]:
        """按运行标识读取已生成的报告期次。"""
        return _load_run_reports(run_id)

    def get_run_detail(self, run_id: int) -> dict[str, Any] | None:
        """运行详情：运行记录 + 当次实际捕获 + 各期报告，绝不混入其他运行的数据。"""
        entry = store.find(self.MODULE, run_id)
        if entry is None:
            return None
        stats = PageSnapshotService().run_capture_stats(_run_aliases(entry))
        reports = _load_run_reports(run_id)
        return {
            "run": entry,
            "run_id": run_id,
            "captures": [
                {
                    "capture_id": item.get("capture_id"),
                    "page_url": item.get("page_url"),
                    "status": item.get("status") or item.get("capture_status"),
                    "file": item.get("_file"),
                    "captured_at": item.get("captured_at") or item.get("timestamp"),
                }
                for item in stats["verified"]
            ],
            "waiting_captures": [
                {
                    "capture_id": item.get("capture_id"),
                    "page_url": item.get("page_url"),
                    "status": item.get("status") or item.get("capture_status"),
                    "reason": item.get("_unverified_reason"),
                    "file": item.get("_file"),
                }
                for item in stats["incomplete"]
            ],
            "unreadable": stats.get("unreadable", 0),
            "verified_count": stats.get("verified_count", 0),
            "reports": reports,
            "latest_report": reports[-1] if reports else None,
        }

    def get_latest_report(self, run_id: int, period: int | None = None) -> dict[str, Any] | None:
        """取最近一期报告；指定 period 时取该期。"""
        reports = _load_run_reports(run_id)
        if period is not None:
            reports = [r for r in reports if int(r.get("period", 0)) == int(period)]
        return reports[-1] if reports else None

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 文件中的运行记录
        file_runs = persist.read_jsonl("runs", "run_history.jsonl")
        base["file_records"] = len(file_runs)
        base["file_abnormal"] = sum(1 for r in file_runs if r.get("abnormal"))
        # 关联的去重报告（正式确认与部分完成两类触发都算）
        dedup_reports = persist.read_jsonl("reports", "dedup_report.jsonl")
        base["triggered_dedup"] = len([
            r for r in dedup_reports
            if str(r.get("trigger", "")).startswith("run_history")
        ])
        # 运行报告期次
        run_reports = persist.read_jsonl("reports", "run_reports.jsonl")
        base["run_reports"] = len(run_reports)
        base["delivered_reports"] = sum(1 for r in run_reports if r.get("delivered"))
        # 关联的schema校验报告
        schema_reports = persist.read_jsonl("reports", "schema_check_report.jsonl")
        base["triggered_schema"] = len([
            r for r in schema_reports if r.get("trigger") == "run_history_confirm"
        ])
        return base
