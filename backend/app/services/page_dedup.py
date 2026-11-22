"""页面去重领域服务：页面内容相似度检测与去重，消费page_snapshot快照数据，为export_channel提供去重结果。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app import persistence as persist

# 参与内容指纹的字段，按优先级取第一个有值的。
_CONTENT_KEYS = ("content", "raw_dom", "dom", "html", "text", "dom_preview", "content_hash", "dom_hash")


def page_content(page: dict[str, Any]) -> str:
    """取页面的可比较正文；没有正文时退回页面身份字段，保证结果稳定可复现。"""
    for key in _CONTENT_KEYS:
        value = page.get(key)
        if value not in (None, ""):
            return str(value)
    identity = {
        "page_url": page.get("page_url") or page.get("url") or "",
        "page_version": page.get("页面版本") or page.get("page_version") or "",
    }
    return str(identity)


class PageDedupService(BaseModuleService):
    MODULE = "page_dedup"
    ENTITY = "去重规则"
    OPERATION_TERM = "去重"
    LIST_FIELDS = ["规则编号", "去重维度", "特征字段", "相似算法", "指纹长度", "时间窗口", "冲突策略", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "去重维度", "特征字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def compute_fingerprint(self, content: str, rule_id: int | None) -> str:
        """根据去重规则计算内容的指纹。"""
        rule = store.find(self.MODULE, rule_id) if rule_id is not None else None
        algorithm = rule.get("相似算法", "md5") if rule else "md5"
        if algorithm == "sha256":
            return hashlib.sha256(content.encode()).hexdigest()[:32]
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def active_rule_id(self) -> int | None:
        """取第一条已生效的去重规则；没有规则时返回 None（使用默认指纹）。"""
        for row in store.rows(self.MODULE):
            if row.get("status") == "已生效":
                return int(row.get("id"))
        return None

    def deduplicate_pages(self, pages: list[dict[str, Any]], rule_id: int) -> dict[str, Any]:
        """对页面列表执行去重，返回去重结果和统计。"""
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for page in pages:
            fp = self.compute_fingerprint(str(page), rule_id)
            if fp in seen:
                duplicates.append({**page, "fingerprint": fp})
            else:
                seen.add(fp)
                unique.append({**page, "fingerprint": fp})
        result = {
            "rule_id": rule_id,
            "total": len(pages),
            "unique": len(unique),
            "duplicates": len(duplicates),
            "timestamp": persist.now_iso(),
        }
        persist.append_jsonl("reports", "dedup_report.jsonl", result)
        return result

    # ------------------------------------------------------------------
    # 运行样本去重：按页面正文判定重复，并把捕获身份写进回执
    # ------------------------------------------------------------------
    def deduplicate_run_sample(
        self,
        pages: list[dict[str, Any]],
        *,
        run_id: Any,
        period: int,
        rule_id: int | None = None,
        trigger: str = "run_history_confirm",
        empty_reason: str | None = None,
        sample_source: str = "persisted_page_snapshots",
        baseline_fingerprints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """对一次运行取到的页面样本去重，产出可回查的去重回执。

        每个页面条目都携带 capture_id / source_file / fingerprint，重复项还会记录
        duplicate_of 指向首次出现的捕获身份，因此回执能对应到实际捕获而不是全局文件。
        baseline_fingerprints 为更早期次已确认过的正文指纹，用于识别回采产生的相同正文。
        """
        if rule_id is None:
            rule_id = self.active_rule_id()
        baseline = {str(k): v for k, v in (baseline_fingerprints or {}).items()}

        seen: dict[str, dict[str, Any]] = {}
        receipts: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        cross_period_duplicates = 0

        for page in pages:
            fp = self.compute_fingerprint(page_content(page), rule_id)
            receipt = {
                "capture_id": page.get("capture_id"),
                "page_url": page.get("page_url") or page.get("url") or "",
                "fingerprint": fp,
                "source_file": page.get("_file") or page.get("source_file", ""),
                "source": sample_source,
                "captured_at": page.get("captured_at") or page.get("timestamp", ""),
                "dom_hash": page.get("dom_hash", ""),
                "content_hash": page.get("content_hash", page.get("dom_hash", "")),
                "verified": bool(page.get("_verified", True)),
            }
            if fp in seen:
                receipt["result"] = "duplicate"
                receipt["duplicate_of"] = seen[fp]["capture_id"]
                receipt["duplicate_scope"] = "same_period"
                duplicates.append(receipt)
            elif fp in baseline:
                receipt["result"] = "duplicate"
                receipt["duplicate_of"] = baseline[fp]
                receipt["duplicate_scope"] = "earlier_period"
                duplicates.append(receipt)
                cross_period_duplicates += 1
            else:
                receipt["result"] = "unique"
                receipt["duplicate_of"] = None
                receipt["duplicate_scope"] = None
                seen[fp] = receipt
            receipts.append(receipt)

        result = {
            "trigger": trigger,
            "run_id": run_id,
            "period": period,
            "report_period": period,
            "rule_id": rule_id,
            "report_id": f"RUN-REPORT-{run_id}-P{period}",
            "total": len(pages),
            "sampled": len(pages),
            "sample_count": len(pages),
            "snapshots_checked": len(pages),
            "unique": len([item for item in receipts if item["result"] == "unique"]),
            "duplicates": len(duplicates),
            "cross_period_duplicates": cross_period_duplicates,
            "capture_ids": [item["capture_id"] for item in receipts],
            "sampled_capture_ids": [item["capture_id"] for item in receipts],
            "new_capture_ids": [item["capture_id"] for item in receipts if item["result"] == "unique"],
            "duplicate_capture_ids": [item["capture_id"] for item in duplicates],
            "sources": [item["source_file"] for item in receipts],
            "sample_source": sample_source,
            "sample_range": [item["capture_id"] for item in receipts],
            "pages": receipts,
            "empty_reason": empty_reason,
            "timestamp": persist.now_iso(),
        }
        persist.append_jsonl("reports", "dedup_report.jsonl", result)
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 去重报告消费
        reports = persist.read_jsonl("reports", "dedup_report.jsonl")
        base["dedup_reports"] = len(reports)
        # 关联的导出配置
        channels = store.rows("export_channel")
        base["export_channels"] = len(channels)
        return base
