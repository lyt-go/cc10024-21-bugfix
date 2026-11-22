"""运行归属与去重采样集成测试。

覆盖场景：
- 运行乙先有一份现成快照；运行甲首次确认时自己有一份捕获仍在写入，
  且乙的同页面早已写好——甲的报告不得借用乙的页面。
- 甲的首个成功页迟到，确认窗口之后才落成，随后还有回采产生的相同正文。
- 空样本保留可核对的等待/中断原因；已交付报告保持原样本范围；
  新的独立确认才把后续补采列入另一期，并区分新增页面与原确认捕获。
- 未被正式确认（部分成功）的报告可以在重新确认时继续。
"""
from __future__ import annotations

import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app import persistence as persist


def setUpModule() -> None:
    for sub in ("runs", "snapshots", "reports", "exports"):
        path = persist.data_path(sub, "")
        if os.path.isdir(path):
            for name in os.listdir(path):
                target = os.path.join(path, name)
                if os.path.isfile(target):
                    os.remove(target)


def _create_run(code: str) -> dict:
    from app.services.run_history import RunHistoryService
    entry, missing = RunHistoryService().create_entry({
        "记录编号": code,
        "关联计划": "SCRI-AUTO-0001",
        "开始时间": "2026-11-22T09:00:00",
    })
    assert entry is not None, missing
    return entry


def _capture(run_id, url: str, body: str, *, capture_id: str | None = None,
             status: str = "complete", captured_at: str | None = None) -> dict:
    from app.services.page_snapshot import PageSnapshotService
    return PageSnapshotService().capture_snapshot(
        url, f"hash-{capture_id or body[:8]}", body, run_id,
        capture_id=capture_id, status=status, captured_at=captured_at,
    )


def _dedup_receipts(run_id: int) -> list[dict]:
    return [
        record for record in persist.read_jsonl("reports", "dedup_report.jsonl")
        if str(record.get("run_id")) == str(run_id)
    ]


def _all_capture_ids(reports: list[dict]) -> list[str]:
    ids: list[str] = []
    for report in reports:
        ids.extend(str(item) for item in report.get("capture_ids", []))
    return ids


class RunCaptureAttributionTest(unittest.TestCase):
    def test_other_run_pages_never_counted_and_late_captures_go_to_new_period(self) -> None:
        from app.services.run_history import RunHistoryService

        svc = RunHistoryService()

        # 运行乙：先有一份现成的同页面快照（已落成）。
        run_b = _create_run("RUN-B-001")
        shared_b = _capture(
            run_b["id"], "https://shared.example.com/p", "shared page body",
            capture_id="B-shared", captured_at="2026-11-22T09:00:05",
        )

        # 运行甲：首次确认时，自己的一份同页面捕获仍在写入。
        run_a = _create_run("RUN-A-001")
        writing_a = _capture(
            run_a["id"], "https://shared.example.com/p", "shared page body",
            capture_id="A-shared-writing", status="writing",
            captured_at="2026-11-22T09:00:10",
        )

        # 甲首次正式确认：样本为空，只记录等待中的捕获原因，绝不借乙的页面补足。
        entry_a, message = svc.run_action(run_a["id"], "确认完成")
        self.assertIsNotNone(entry_a, message)

        detail = svc.get_run_detail(run_a["id"])
        reports = detail["reports"]
        self.assertEqual(len(reports), 1)
        first = reports[0]
        self.assertEqual(first["period"], 1)
        self.assertTrue(first["delivered"])
        self.assertEqual(first["sampled"], 0)
        self.assertEqual(first["capture_ids"], [])
        self.assertEqual(first["duplicates"], 0)
        self.assertEqual(first["empty_reason"], "captures_in_progress")
        self.assertIn("A-shared-writing", first["empty_reason_detail"])

        # 首次确认期间：甲的详情里看不到乙的任何捕获，空样本原因可核对。
        self.assertEqual(detail["captures"], [])
        self.assertEqual(
            [item["capture_id"] for item in detail["waiting_captures"]],
            ["A-shared-writing"],
        )
        self.assertEqual(detail["verified_count"], 0)

        first_receipt = _dedup_receipts(run_a["id"])[0]
        self.assertEqual(first_receipt["sampled"], 0)
        self.assertEqual(first_receipt["sources"], [])
        self.assertEqual(first_receipt["empty_reason"], "captures_in_progress")
        self.assertNotIn(str(shared_b["capture_id"]), str(first_receipt))
        self.assertNotIn("B-shared", _all_capture_ids(reports))

        # 确认窗口之后：甲的捕获陆续落成。先是仍在写入的那份，再是迟到的成功页，
        # 最后是同一正文的回采捕获（归属甲）。
        from app.services.page_snapshot import PageSnapshotService
        snap_svc = PageSnapshotService()
        snap_svc.finish_capture("A-shared-writing", "shared page body")
        _capture(run_a["id"], "https://a.example.com/late", "late first page body",
                 capture_id="A-late-1", captured_at="2026-11-22T09:01:00")
        _capture(run_a["id"], "https://a.example.com/late?retry=1", "late first page body",
                 capture_id="A-late-recollect", captured_at="2026-11-22T09:02:00")

        # 已交付的旧报告保持原样本范围：仍为 0 页，不被迟到捕获覆写。
        self.assertEqual(svc.get_latest_report(run_a["id"], period=1), first)
        self.assertEqual(first["sampled"], 0)
        self.assertEqual(first["capture_ids"], [])

        # 新的独立确认：把归属甲的后续补采列入另一期。
        again, message = svc.run_action(run_a["id"], "确认完成")
        self.assertIsNotNone(again, message)

        reports = svc.get_run_reports(run_a["id"])
        self.assertEqual([r["period"] for r in reports], [1, 2])
        self.assertEqual(reports[0], first)  # 旧期次原样保留
        second = reports[1]
        self.assertEqual(second["mode"], "new_period")
        self.assertTrue(second["delivered"])
        self.assertEqual(second["sampled"], 3)
        self.assertEqual(
            second["capture_ids"],
            ["A-shared-writing", "A-late-1", "A-late-recollect"],
        )
        # 去重按页面正文执行：回采正文与迟到成功页相同，记为重复并指向实际捕获。
        self.assertEqual(second["unique"], 2)
        self.assertEqual(second["duplicates"], 1)
        recollect_page = next(
            page for page in second["pages"] if page["capture_id"] == "A-late-recollect"
        )
        self.assertEqual(recollect_page["result"], "duplicate")
        self.assertEqual(recollect_page["duplicate_of"], "A-late-1")
        self.assertEqual(recollect_page["duplicate_scope"], "same_period")
        self.assertTrue(all(page["verified"] for page in second["pages"]))
        # 回执来源是甲自己的持久化捕获文件，不是全局目录前五份。
        self.assertTrue(all("_r" in source for source in second["sources"]))
        self.assertNotIn("B-shared", second["capture_ids"])
        self.assertNotIn(shared_b["_file"], second["sources"])

        # 新一期能区分新增页面和原确认捕获。
        self.assertEqual(
            second["新增页面"],
            ["A-shared-writing", "A-late-1"],
        )
        self.assertEqual(second["原确认捕获"], [])
        self.assertIn("A-late-1", _all_capture_ids([second]))
        self.assertNotIn("B-shared", _all_capture_ids(reports))

        # 乙自己的确认只算自己的页面，甲的捕获不会串过去。
        entry_b, message = svc.run_action(run_b["id"], "确认完成")
        self.assertIsNotNone(entry_b, message)
        b_reports = svc.get_run_reports(run_b["id"])
        self.assertEqual(len(b_reports), 1)
        self.assertEqual(b_reports[0]["capture_ids"], ["B-shared"])
        self.assertEqual(b_reports[0]["sampled"], 1)
        self.assertEqual(b_reports[0]["duplicates"], 0)
        self.assertNotIn("A-late-1", b_reports[0]["capture_ids"])
        self.assertEqual(svc.get_run_detail(run_b["id"])["verified_count"], 1)

    def test_provisional_report_continues_on_confirm(self) -> None:
        from app.services.run_history import RunHistoryService
        from app.services.page_snapshot import PageSnapshotService

        svc = RunHistoryService()
        run = _create_run("RUN-C-001")
        _capture(run["id"], "https://c.example.com/1", "c one",
                 capture_id="C-writing", status="writing",
                 captured_at="2026-11-22T09:00:10")

        # 尚未正式确认：报告为待确认，样本为空并保留等待原因。
        entry, message = svc.run_action(run["id"], "标记部分完成")
        self.assertIsNotNone(entry, message)
        provisional = svc.get_latest_report(run["id"])
        self.assertEqual(provisional["period"], 1)
        self.assertFalse(provisional["delivered"])
        self.assertEqual(provisional["status"], "待确认")
        self.assertTrue(provisional["continuable"])
        self.assertEqual(provisional["empty_reason"], "captures_in_progress")

        # 捕获落成后再确认：同一期继续，样本补齐并转为已交付。
        PageSnapshotService().finish_capture("C-writing", "c one")
        _capture(run["id"], "https://c.example.com/2", "c two", capture_id="C-two",
                 captured_at="2026-11-22T09:00:20")

        entry, message = svc.run_action(run["id"], "确认完成")
        self.assertIsNotNone(entry, message)
        reports = svc.get_run_reports(run["id"])
        self.assertEqual(len(reports), 1)
        final = reports[0]
        self.assertEqual(final["period"], 1)
        self.assertEqual(final["mode"], "continuation")
        self.assertTrue(final["delivered"])
        self.assertEqual(final["capture_ids"], ["C-writing", "C-two"])
        self.assertEqual(final["sampled"], 2)
        self.assertEqual(final["unique"], 2)
        self.assertEqual(final["duplicates"], 0)

        # 去重回执同样带捕获身份与来源。
        receipts = _dedup_receipts(run["id"])
        self.assertGreaterEqual(len(receipts), 2)
        latest = receipts[-1]
        self.assertEqual(latest["capture_ids"], ["C-writing", "C-two"])
        self.assertEqual(latest["sources"], [item["_file"] for item in [
            *_capture_file_lookup("C-writing"), *_capture_file_lookup("C-two"),
        ]])
        for page in latest["pages"]:
            self.assertTrue(page["capture_id"])
            self.assertTrue(page["source_file"].startswith("page_r"))

    def test_report_ignores_unowned_and_legacy_global_snapshots(self) -> None:
        """全局目录里的历史快照/其他运行的快照不会补足本次运行的样本。"""
        from app.services.run_history import RunHistoryService

        svc = RunHistoryService()
        run = _create_run("RUN-D-001")

        # 旧式无运行归属的快照，以及别的运行的快照，都不能进入本次运行。
        persist.write_json("snapshots", "page_legacy_hash.json", {
            "page_url": "https://legacy.example.com/1",
            "dom_hash": "legacy_hash",
            "raw_dom_length": 12,
            "dom_preview": "legacy body",
            "timestamp": "2026-09-01T00:00:00",
        })
        _capture(999999, "https://other.example.com/1", "other run body",
                 capture_id="other-run")

        entry, message = svc.run_action(run["id"], "确认完成")
        self.assertIsNotNone(entry, message)
        report = svc.get_latest_report(run["id"])
        self.assertEqual(report["sampled"], 0)
        self.assertEqual(report["capture_ids"], [])
        self.assertEqual(report["empty_reason"], "no_captures_for_run")
        self.assertEqual(svc.get_run_detail(run["id"])["verified_count"], 0)


def _capture_file_lookup(capture_id: str) -> list[dict]:
    """按捕获身份找到持久化文件，供来源断言使用。"""
    for name in persist.list_capture_files():
        payload = persist.read_json("snapshots", name)
        if payload and payload.get("capture_id") == capture_id:
            return [{**payload, "_file": name}]
    return []


if __name__ == "__main__":
    unittest.main(verbosity=2)
