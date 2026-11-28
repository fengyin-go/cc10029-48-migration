"""持久化切换专项测试：存量迁移、回执核对、幂等分发/认领、回滚与业务连续性。

对应切换日要求：
- 保留纪要 M-23、诊改任务、备课评议的原编号与来源；
- 待认领事项不重发、不重复派单，分发中断重试取回原结果；
- 同一份待办在任务入口与待办入口认领资格一致；
- 验证不通过可恢复服务，已受理的诊改任务和评议邀请仍可继续办理；
- 掉线纪要与正常备课签阅对照：同一条任务能认领，评议待办能接到有原备课出处的邀请。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app import database as db
from app.services.lesson_prep import LessonPrepService
from app.services.migration import migration_service
from app.services.peer_review import PeerReviewService
from app.services.review_meeting import ReviewMeetingService
from app.services.workflow import workflow_service
from app.store import store


def legacy_export() -> dict:
    """教务处从旧内存工作台导出的存量记录（含 M-23 在途分发场景）。"""
    return {
        "source": "old-memory-workbench",
        "exported_at": "2026-11-28T08:00:00",
        "records": {
            "review_meeting": [
                {
                    "id": 23,
                    "status": "已签认",
                    "pending": False,
                    "abnormal": False,
                    "source_code": "old-memory-workbench",
                    "source_ref": "M-23",
                    "会议主题": "期中教学诊改例会",
                    "参会人员": "教研室主任、全体教师",
                    "召开日期": "2026-11-27",
                    "决议事项": "会后需落实课堂改进任务，由教研室分发",
                    "分歧观点": "",
                    "记录人员": "教务处",
                    "下次会议": "2026-12-05",
                    "备注说明": "分发回执尚未返回的在途纪要",
                }
            ],
            "improve_task": [
                {
                    "id": 7,
                    "status": "待认领",
                    "pending": True,
                    "abnormal": False,
                    "source_code": "old-memory-workbench",
                    "source_ref": "T-7",
                    "任务描述": "会议决议：期中教学诊改例会 [meeting:23]",
                    "承接方案": "会后需落实课堂改进任务，由教研室分发",
                    "执行人员": "",
                    "起止日期": "2026-11-28至2026-12-15",
                    "验收标准": "课堂互动频次提升",
                    "督办人员": "教研室主任",
                    "任务权重": "高",
                    "备注说明": "旧工作台已派待认领项，切换后不得重发",
                }
            ],
            "lesson_prep": [
                {
                    "id": 5,
                    "status": "已签阅",
                    "pending": False,
                    "abnormal": False,
                    "source_code": "old-memory-workbench",
                    "source_ref": "LP-5",
                    "备课章节": "第3章 栈与队列",
                    "主备教师": "杨老师",
                    "参与教师": "李老师、王老师",
                    "备课日期": "2026-11-26",
                    "研讨要点": "队列应用案例",
                    "教学资源": "课件、习题",
                    "课后反思": "需要改进教学主线并开展同行评议",
                    "备注说明": "",
                }
            ],
            "peer_review": [
                {
                    "id": 4,
                    "status": "待汇总",
                    "pending": True,
                    "abnormal": False,
                    "source_code": "old-memory-workbench",
                    "source_ref": "PR-4",
                    "被评教师": "杨老师",
                    "评议维度": "备课教学质量",
                    "得分": "",
                    "评议意见": "由备课记录 #5 课后反思触发 [lesson_prep:5]",
                    "评议人员": "",
                    "评议日期": "",
                    "是否匿名": "否",
                    "备注说明": "备课签阅后自动生成评议请求",
                }
            ],
        },
        "todos": [
            {
                "module": "improve_task",
                "entry_id": 7,
                "title": "会议决议：期中教学诊改例会",
                "origin_module": "review_meeting",
                "origin_ref": 23,
                "source_code": "old-memory-workbench",
                "source_ref": "M-23",
                "status": "待认领",
            },
            {
                "module": "peer_review",
                "entry_id": 4,
                "title": "备课记录 #5 触发的同行评议邀请",
                "origin_module": "lesson_prep",
                "origin_ref": 5,
                "source_code": "old-memory-workbench",
                "source_ref": "LP-5",
                "status": "待认领",
            },
        ],
    }


class TestMigrationImportAndReceipt:
    def test_import_preserves_original_ids_and_sources(self):
        receipt = migration_service.import_export(legacy_export(), batch_id="b-1")

        assert receipt["verification_passed"] is True
        assert receipt["status"] == "success"
        # 原编号保持不变
        assert store.find("review_meeting", 23)["source_ref"] == "M-23"
        assert store.find("improve_task", 7)["source_ref"] == "T-7"
        assert store.find("lesson_prep", 5)["source_ref"] == "LP-5"
        assert store.find("peer_review", 4)["source_ref"] == "PR-4"
        for module, ref in (
            ("review_meeting", "M-23"),
            ("improve_task", "T-7"),
            ("lesson_prep", "LP-5"),
            ("peer_review", "PR-4"),
        ):
            row = store.find(module, {"M-23": 23, "T-7": 7, "LP-5": 5, "PR-4": 4}[ref])
            assert row["source_code"] == "old-memory-workbench"
            assert row["source_ref"] == ref

    def test_receipt_record_counts_are_comparable(self):
        payload = legacy_export()
        receipt = migration_service.import_export(payload, batch_id="b-2")

        counts = receipt["counts"]
        assert counts["expected"] == {m: len(rows) for m, rows in payload["records"].items()}
        assert counts["imported"] == counts["expected"]
        assert counts["total_expected"] == counts["total_imported"] == 4
        # 跨模块关联全部可解析（meeting:23、lesson_prep:5）
        assert receipt["links"]["total"] >= 2
        assert receipt["links"]["unresolved"] == 0
        markers = {d["marker"] for d in receipt["links"]["details"]}
        assert {"meeting:23", "lesson_prep:5"} <= markers

    def test_receipt_file_and_backup_are_written(self):
        from app.config import settings as app_settings

        receipt = migration_service.import_export(legacy_export(), batch_id="b-3")
        receipt_path = Path(app_settings.receipt_dir) / "b-3.json"
        assert receipt_path.exists()
        backup = Path(receipt["backup_file"])
        assert backup.exists()
        # 备份是可打开的 SQLite 库，且只含导入前数据（不含本批 T-7）
        backup_conn = sqlite3.connect(backup)
        try:
            rows = backup_conn.execute(
                "SELECT COUNT(*) FROM entries WHERE module='improve_task' AND id=7"
            ).fetchone()[0]
            assert rows == 0
            assert backup_conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] > 0
        finally:
            backup_conn.close()

    def test_reimport_does_not_resend_or_duplicate(self):
        migration_service.import_export(legacy_export(), batch_id="b-4a")
        todo_count_first = len(store.list_todos())
        task_count_first = len(store.rows("improve_task"))

        # 教务处重复导出/再次导入同一批存量
        receipt = migration_service.import_export(legacy_export(), batch_id="b-4b")
        assert receipt["verification_passed"] is True
        assert len(store.list_todos()) == todo_count_first
        assert len(store.rows("improve_task")) == task_count_first
        assert store.find("improve_task", 7)["source_ref"] == "T-7"


class TestInFlightDispatchAndClaim:
    def test_m23_distribution_retry_returns_original_result(self):
        migration_service.import_export(legacy_export(), batch_id="b-5")
        before_tasks = len(store.rows("improve_task"))

        first = workflow_service.dispatch(
            "review_meeting", 23, "分发给教师",
            dispatch_key="dist:review_meeting:23:分发给教师",
        )
        assert first["ok"] is True and first["idempotent_replay"] is False
        assert store.find("review_meeting", 23)["status"] == "已分发"
        # 旧工作台派生的 T-7 已带 meeting:23 标记，不得再派一项
        assert len(store.rows("improve_task")) == before_tasks

        # 分发者中断后用同一幂等键重试：取回原处理结果，不重复执行、不重复派单
        retry = workflow_service.dispatch(
            "review_meeting", 23, "分发给教师",
            dispatch_key="dist:review_meeting:23:分发给教师",
        )
        assert retry["ok"] is True and retry["idempotent_replay"] is True
        assert len(store.rows("improve_task")) == before_tasks
        assert store.find("review_meeting", 23)["status"] == "已分发"
        assert store.get_dispatch("dist:review_meeting:23:分发给教师") is not None

    def test_claim_eligibility_is_shared_between_entries(self):
        migration_service.import_export(legacy_export(), batch_id="b-6")

        # 切换后打开同一条任务：任务入口与待办入口都显示可认领
        assert workflow_service.eligibility("improve_task", 7)["can_claim"] is True
        todos = workflow_service.list_todos(module="improve_task", status="待认领")
        assert any(t["entry_id"] == 7 and t["can_claim"] is True for t in todos)

        # 从待办入口受理
        result = workflow_service.claim(
            "improve_task", 7, teacher="周老师",
            todo_code="review_meeting:23:improve_task",
        )
        assert result["ok"] is True and result["idempotent_replay"] is False
        assert store.find("improve_task", 7)["status"] == "执行中"

        # 两边入口同步：已受理，资格被占用
        eligibility = workflow_service.eligibility("improve_task", 7)
        assert eligibility["can_claim"] is False and "已受理" in eligibility["reason"]
        pending = workflow_service.list_todos(module="improve_task", status="待认领")
        assert all(t["entry_id"] != 7 for t in pending)
        claimed = store.get_todo("review_meeting:23:improve_task")
        assert claimed["status"] == "已认领" and claimed["assignee"] == "周老师"

        # 中断重试受理：取回原结果，不能再认领一次
        replay = workflow_service.claim("improve_task", 7, teacher="周老师")
        assert replay["ok"] is True and replay["idempotent_replay"] is True
        assert len(store.list_claims("improve_task")) == 1

    def test_peer_review_invitation_keeps_lesson_prep_origin_and_continues(self):
        migration_service.import_export(legacy_export(), batch_id="b-7")

        todos = workflow_service.list_todos(module="peer_review")
        invitation = next(t for t in todos if t["entry_id"] == 4)
        # 评议待办带有原备课出处
        assert invitation["origin_module"] == "lesson_prep"
        assert invitation["origin_ref"] == "5"
        assert invitation["source_ref"] == "LP-5"

        accepted = workflow_service.claim(
            "peer_review", 4, teacher="李老师",
            todo_code="lesson_prep:5:peer_review",
        )
        assert accepted["ok"] is True
        # 评议邀请只登记受理，不跳过业务状态，后续仍可继续办理
        assert store.find("peer_review", 4)["status"] == "待汇总"
        review_svc = PeerReviewService()
        entry, _ = review_svc.run_action(4, "汇总评议")
        assert entry["status"] == "已汇总"


class TestRollbackAndContinuity:
    def test_failed_verification_auto_rolls_back_and_restores_service(self):
        bad_payload = legacy_export()
        bad_payload["records"]["improve_task"][0]["任务描述"] = (
            "引用不存在的纪要 [meeting:999]"
        )
        receipt = migration_service.import_export(bad_payload, batch_id="b-8")

        assert receipt["verification_passed"] is False
        assert receipt["status"] == "rolled_back"
        assert receipt["links"]["unresolved"] >= 1
        # 导入的记录已撤回，服务仍可用（种子数据还在）
        assert store.find("review_meeting", 23) is None
        assert store.find("improve_task", 7) is None
        batches = {b["batch_id"]: b["status"] for b in migration_service.list_batches()}
        assert batches["b-8"] == "rolled_back"

    def test_rollback_keeps_confirmed_claims_and_invitations(self):
        migration_service.import_export(legacy_export(), batch_id="b-9")
        workflow_service.claim("improve_task", 7, teacher="周老师",
                               todo_code="review_meeting:23:improve_task")
        workflow_service.claim("peer_review", 4, teacher="李老师",
                               todo_code="lesson_prep:5:peer_review")

        result = migration_service.rollback("b-9", reason="切换验证不通过人工回滚")
        assert result["status"] == "rolled_back"
        # 业务记录撤回，但受理台账与待办保留，可继续办理
        assert store.find("improve_task", 7) is None
        assert result["ledger_preserved"]["claims"] >= 2
        assert store.find_claim_for_entry("improve_task", 7) is not None
        assert store.get_todo("lesson_prep:5:peer_review") is not None

        # 重新导入同一份存量：记录恢复，受理关系自动接续，不会被当作新待办再派
        receipt = migration_service.import_export(legacy_export(), batch_id="b-9r")
        assert receipt["verification_passed"] is True
        assert store.find("improve_task", 7) is not None
        assert workflow_service.eligibility("improve_task", 7)["can_claim"] is False
        assert len(store.list_claims("improve_task")) == 1
        # 评议邀请仍可继续办理
        entry, _ = PeerReviewService().run_action(4, "汇总评议")
        assert entry["status"] == "已汇总"

    def test_data_survives_store_reload_like_process_restart(self):
        migration_service.import_export(legacy_export(), batch_id="b-10")
        workflow_service.claim("improve_task", 7, teacher="周老师",
                               todo_code="review_meeting:23:improve_task")

        # 模拟进程重启：重新引导内存视图，数据全部来自 SQLite
        store.bootstrap()
        assert store.find("improve_task", 7)["status"] == "执行中"
        assert store.find("review_meeting", 23)["source_ref"] == "M-23"
        assert store.find_claim_for_entry("improve_task", 7) is not None

        # 再做一次写操作并重启，验证写回持久化
        ReviewMeetingService().run_action(23, "分发给教师")
        store.bootstrap()
        assert store.find("review_meeting", 23)["status"] == "已分发"


class TestNormalLessonPrepContrast:
    def test_normal_prep_signoff_creates_claimable_invitation_with_origin(self):
        """正常备课签阅（对照掉线纪要）：评议邀请带原备课出处，可从评议待办受理。"""
        prep_svc = LessonPrepService()
        prep, _ = prep_svc.create_entry({
            "备课章节": "第7章 排序",
            "主备教师": "陈老师",
            "参与教师": "赵老师、孙老师",
            "课后反思": "教学节奏需要改进，建议组织同行评议",
        })
        entry, message = prep_svc.run_action(prep["id"], "签阅记录")
        assert entry is not None, message

        todos = workflow_service.list_todos(module="peer_review", status="待认领")
        invitation = next(
            t for t in todos
            if t["origin_module"] == "lesson_prep" and t["origin_ref"] == str(prep["id"])
        )
        review_entry = store.find("peer_review", invitation["entry_id"])
        assert f"lesson_prep:{prep['id']}" in review_entry["评议意见"]
        assert invitation["can_claim"] is True

        accepted = workflow_service.claim(
            "peer_review", invitation["entry_id"], teacher="赵老师",
            todo_code=invitation["todo_code"],
        )
        assert accepted["ok"] is True
        # 受理后资格同步关闭，重试不重复受理
        assert workflow_service.eligibility(
            "peer_review", invitation["entry_id"]
        )["can_claim"] is False
        replay = workflow_service.claim("peer_review", invitation["entry_id"], teacher="赵老师")
        assert replay["idempotent_replay"] is True

    def test_acceptance_offline_minutes_and_normal_prep_end_to_end(self):
        """验收主场景：掉线纪要 M-23 + 正常备课签阅，切换后同任务可认领、邀请有出处。"""
        migration_service.import_export(legacy_export(), batch_id="b-11")

        # 一份已分发但掉线的纪要：重试分发幂等，随后打开同一条任务并认领
        workflow_service.dispatch(
            "review_meeting", 23, "分发给教师",
            dispatch_key="dist:review_meeting:23:分发给教师",
        )
        task = workflow_service.claim("improve_task", 7, teacher="周老师")
        assert task["ok"] is True
        assert store.find("improve_task", 7)["status"] == "执行中"

        # 一份正常备课签阅触发评议
        prep_svc = LessonPrepService()
        prep, _ = prep_svc.create_entry({
            "备课章节": "第9章 查找",
            "主备教师": "钱老师",
            "参与教师": "周老师、吴老师",
            "课后反思": "案例设计待改进，需同行评议",
        })
        prep_svc.run_action(prep["id"], "签阅记录")

        # 从评议待办接到有原备课出处的邀请
        todos = workflow_service.list_todos(module="peer_review", status="待认领")
        invitation = next(
            t for t in todos if t["origin_ref"] == str(prep["id"])
        )
        assert invitation["origin_module"] == "lesson_prep"
        assert store.find("lesson_prep", prep["id"]) is not None
        accepted = workflow_service.claim(
            "peer_review", invitation["entry_id"], teacher="吴老师",
            todo_code=invitation["todo_code"],
        )
        assert accepted["ok"] is True
        # 两类待办都没有重复项
        assert len(store.list_todos()) == len({t["todo_code"] for t in store.list_todos()})


class TestTransactionAtomicity:
    def test_failed_dispatch_keeps_no_ledger_and_original_status(self):
        """分发业务校验失败时，整体回滚：状态不变、不写台账、不派待办。"""
        migration_service.import_export(legacy_export(), batch_id="b-12")
        # 直接尝试把已签认纪要再签认会被业务规则拦截（草稿/待签认才可签认）
        result = workflow_service.dispatch(
            "review_meeting", 23, "签认纪要",
            dispatch_key="dist:review_meeting:23:签认纪要",
        )
        assert result["ok"] is False
        assert store.find("review_meeting", 23)["status"] == "已签认"
        assert store.get_dispatch("dist:review_meeting:23:签认纪要") is None

    def test_dispatch_and_claim_commit_together_with_ledger(self):
        migration_service.import_export(legacy_export(), batch_id="b-13")
        result = workflow_service.dispatch(
            "review_meeting", 23, "分发给教师",
            dispatch_key="dist:review_meeting:23:分发给教师",
        )
        assert result["ok"] is True
        # 台账、业务状态、待办在同一事务落库
        assert store.get_dispatch("dist:review_meeting:23:分发给教师") is not None
        assert store.find("review_meeting", 23)["status"] == "已分发"
        assert store.get_todo("review_meeting:23:improve_task") is not None

