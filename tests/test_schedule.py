import threading
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service

DAY = "2026-10-04"


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.insp_a = self.service.create_inspector(
            {"name": "赵检查员", "daily_capacity": 2}, "mgr", "compliance_manager")
        self.insp_b = self.service.create_inspector(
            {"name": "钱检查员", "daily_capacity": 3}, "mgr", "compliance_manager")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _submit(self, title, severity, quantity, threshold, ref,
                open_records=0):
        item = self.service.create_item(
            {"title": title, "description": "排期测试", "severity": severity,
             "quantity": quantity, "threshold": threshold, "external_ref": ref},
            "applicant", "applicant")
        item = self.service.transition(item["id"], "submitted", item["version"],
                                       "applicant", "applicant")
        for i in range(open_records):
            self.service.add_record(item["id"],
                                    {"kind": "rectification", "detail": f"整改{i}",
                                     "status": "open", "external_ref": f"{ref}-R{i}"},
                                    "ins", "inspector")
        return item

    def test_priority_queue_and_load_dispatch(self):
        # 一般许可先建、紧急许可后建：紧急仍应排到队首
        ordinary = self._submit("一般许可", "low", 1, 100, "Q-1")
        urgent = self._submit("紧急许可", "critical", 120, 100, "Q-2")
        with_open = self._submit("带未关闭整改", "medium", 5, 10, "Q-3",
                                 open_records=3)
        schedule = self.service.build_day_queue(DAY, "mgr", "compliance_manager")
        order = [b["item_id"] for b in schedule["queue"]]
        self.assertEqual(order[0], urgent["id"])
        self.assertIn(ordinary["id"], order[1:])
        # 未关闭整改提升了带整改批次的优先级
        q_open = next(b for b in schedule["queue"] if b["item_id"] == with_open["id"])
        q_ordinary = next(b for b in schedule["queue"] if b["item_id"] == ordinary["id"])
        self.assertGreater(q_open["priority"], q_ordinary["priority"])
        self.assertEqual(schedule["remaining_slots"], 5)
        # 名额按当前负荷分配：钱检查员（容量3）剩余名额更多，自动派给她
        dispatched = self.service.dispatch_next(DAY, "mgr", "compliance_manager")
        self.assertEqual(dispatched["item_id"], urgent["id"])
        self.assertEqual(dispatched["assigned_inspector_id"], self.insp_b["id"])

    def test_concurrent_claim_first_wins_and_remaining_slots(self):
        item = self._submit("并发许可", "high", 50, 10, "C-1")
        self.service.build_day_queue(DAY, "mgr", "compliance_manager")
        batch = self.repo.list_day_batches(DAY)[0]
        winners = []

        def claim(inspector_id):
            try:
                self.service.claim_batch(batch["id"], inspector_id,
                                         f"reg-{inspector_id}", "inspector")
                winners.append(inspector_id)
            except ConflictError:
                pass

        threads = [threading.Thread(target=claim, args=(self.insp_a["id"],)),
                   threading.Thread(target=claim, args=(self.insp_b["id"],))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        claimed = self.service.get_batch(batch["id"], "viewer")
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["inspector_id"], winners[0])
        # 后到者看到的剩余名额已扣减
        schedule = self.service.day_schedule(DAY, "viewer")
        self.assertEqual(schedule["remaining_slots"], 4)

    def test_capacity_full_second_inspector_still_served(self):
        for i in range(4):
            self._submit(f"许可{i}", "high", 50, 10, f"CAP-{i}")
        self.service.build_day_queue(DAY, "mgr", "compliance_manager")
        # 赵检查员容量2：占满后第三个认领被拒
        queue = self.service.day_schedule(DAY, "viewer")["queue"]
        self.service.claim_batch(queue[0]["id"], self.insp_a["id"], "a", "inspector")
        self.service.claim_batch(queue[1]["id"], self.insp_a["id"], "a", "inspector")
        with self.assertRaises(ConflictError):
            self.service.claim_batch(queue[2]["id"], self.insp_a["id"], "a", "inspector")
        # 钱检查员还有名额，继续认领不被挤出
        got = self.service.claim_batch(queue[2]["id"], self.insp_b["id"], "b", "inspector")
        self.assertEqual(got["inspector_id"], self.insp_b["id"])

    def test_recompute_after_rectification_change_executed_kept(self):
        item = self._submit("变化许可", "low", 1, 100, "RC-1")
        self.service.build_day_queue(DAY, "mgr", "compliance_manager")
        batch = self.repo.list_day_batches(DAY)[0]
        # 已执行批次：执行时依据冻结
        self.service.claim_batch(batch["id"], self.insp_a["id"], "a", "inspector")
        executed = self.service.execute_batch(
            batch["id"], {"inspector_id": self.insp_a["id"], "result": "现场无异常"},
            "a", "inspector")
        self.assertTrue(executed["basis_frozen"])
        basis_before = (executed["priority"], executed["open_records"])
        # 整改事项变化：新增未关闭整改
        self.service.add_record(item["id"],
                                {"kind": "rectification", "detail": "新发现问题",
                                 "status": "open", "external_ref": "RC-1-NEW"},
                                "ins", "inspector")
        after = self.service.get_batch(batch["id"], "viewer")
        self.assertEqual(after["status"], "executed")
        self.assertEqual(after["result"], "现场无异常")
        self.assertEqual((after["priority"], after["open_records"]), basis_before)
        # 另一未执行批次：立即重算并退回待派
        day2 = "2026-10-05"
        self.service.build_day_queue(day2, "mgr", "compliance_manager")
        b2 = self.repo.list_day_batches(day2)[0]
        self.service.claim_batch(b2["id"], self.insp_b["id"], "b", "inspector")
        self.service.add_record(item["id"],
                                {"kind": "rectification", "detail": "再发现问题",
                                 "status": "open", "external_ref": "RC-1-NEW2"},
                                "ins", "inspector")
        reset = self.service.get_batch(b2["id"], "viewer")
        self.assertEqual(reset["status"], "queued")
        self.assertIsNone(reset["inspector_id"])
        self.assertEqual(reset["open_records"], 2)
        self.assertGreater(reset["priority"], b2["priority"])
        # 关闭整改后再次重算，优先级回落，名额释放
        record = self.repo.list_records(item["id"])[-1]
        self.service.close_record(record["id"], "b", "inspector")
        closed = self.service.get_batch(b2["id"], "viewer")
        self.assertEqual(closed["open_records"], 1)

    def test_resume_from_last_completed_permit_after_failure(self):
        items = [self._submit(f"许可{i}", "medium", 10, 100, f"RS-{i}")
                 for i in range(5)]
        calls = {"n": 0}
        original = self.repo.enqueue_one

        def flaky(item, open_records, day, actor):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("模拟写入失败")
            return original(item, open_records, day, actor)

        self.repo.enqueue_one = flaky
        with self.assertRaises(RuntimeError):
            self.service.build_day_queue(DAY, "mgr", "compliance_manager")
        self.repo.enqueue_one = original
        # 失败后重试：从最后完成许可继续，不重复占名额
        schedule = self.service.build_day_queue(DAY, "mgr", "compliance_manager")
        queued_ids = sorted(b["item_id"] for b in schedule["queue"])
        self.assertEqual(queued_ids, sorted(i["id"] for i in items))
        self.assertEqual(len(queued_ids), 5)
        # 每个许可每天只有一个批次，重试未产生重复
        counts = {}
        for b in self.repo.list_day_batches(DAY):
            counts[b["item_id"]] = counts.get(b["item_id"], 0) + 1
        self.assertTrue(all(c == 1 for c in counts.values()))
        # 游标已推进到最后扫描位置
        self.assertGreaterEqual(self.repo.schedule_cursor(DAY), items[-1]["id"])

    def test_late_submitted_permit_joins_same_day_queue(self):
        early = self._submit("早先许可", "low", 1, 100, "LATE-1")
        self.service.build_day_queue(DAY, "mgr", "compliance_manager")
        # 建队之后才有新许可进入待检查状态
        late = self._submit("后到紧急许可", "critical", 200, 100, "LATE-2")
        schedule = self.service.build_day_queue(DAY, "mgr", "compliance_manager")
        ids = [b["item_id"] for b in schedule["queue"]]
        self.assertIn(early["id"], ids)
        self.assertIn(late["id"], ids)
        self.assertEqual(ids[0], late["id"])


if __name__ == "__main__":
    unittest.main()
