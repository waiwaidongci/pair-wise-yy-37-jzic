import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.rules import TRANSITION_ROLES
from src.service import Service


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "q.db"))
        self.service = Service(self.repo)
        self.day = "2026-10-05"
        self.service.set_capacity(
            {"inspector": "insp-a", "day": self.day, "capacity": 2},
            "mgr", "compliance_manager")
        self.service.set_capacity(
            {"inspector": "insp-b", "day": self.day, "capacity": 2},
            "mgr", "compliance_manager")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _inspection_item(self, title, severity, quantity, threshold, external_ref,
                         open_record=False):
        item = self.service.create_item(
            {"title": title, "description": "d", "severity": severity,
             "quantity": quantity, "threshold": threshold,
             "external_ref": external_ref},
            "creator", "applicant")
        cur = item
        for target in ("submitted", "inspection"):
            cur = self.service.transition(
                cur["id"], target, cur["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        if open_record:
            self.service.add_record(
                cur["id"], {"kind": "rectification", "detail": "整改",
                            "status": "open", "external_ref": external_ref + "-R"},
                "recorder", "applicant")
        return cur

    def _dispatch(self):
        return self.service.dispatch(self.day, "mgr", "compliance_manager")

    def test_dispatch_prioritizes_urgent_before_general(self):
        # 紧急（严重度+超量+未关闭整改）应排在一般许可之前
        self._inspection_item("一般", "low", 1, 10, "G1")
        self._inspection_item("紧急", "critical", 10, 1, "U1", open_record=True)
        queue = self._dispatch()
        priorities = [it["priority"] for it in queue["items"]]
        self.assertEqual(priorities, sorted(priorities, reverse=True))
        self.assertEqual(queue["items"][0]["item_id"],
                         self._find_item("U1")["id"])

    def test_dispatch_load_balances_by_current_load(self):
        for k in range(3):
            self._inspection_item(f"许可{k}", "high", 5, 1, f"L{k}")
        queue = self._dispatch()
        assigned = {c["inspector"]: c["assigned"] for c in queue["capacities"]}
        # 3 个许可分给 2 个检查员，负荷应尽量均衡（2+1）
        self.assertEqual(sorted(assigned.values()), [1, 2])
        for it in queue["items"]:
            if it["status"] == "dispatched":
                self.assertIsNotNone(it["inspector"])

    def test_claim_first_come_first_served_loser_sees_remaining_quota(self):
        self._inspection_item("许可", "high", 5, 1, "C1")
        queue = self._dispatch()
        batch_item_id = queue["items"][0]["id"]
        barrier = threading.Barrier(2)
        results = {}

        def claim(name):
            barrier.wait()
            try:
                r = self.service.claim(batch_item_id, name, "inspector")
                results[name] = ("ok", r["status"])
            except ConflictError as exc:
                results[name] = ("fail", exc.message, exc.detail)

        t1 = threading.Thread(target=claim, args=("insp-a",))
        t2 = threading.Thread(target=claim, args=("insp-b",))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [n for n, r in results.items() if r[0] == "ok"]
        fails = [n for n, r in results.items() if r[0] == "fail"]
        self.assertEqual(len(oks), 1)  # 先到者占用
        self.assertEqual(len(fails), 1)  # 后到者看到剩余名额
        loser = results[fails[0]]
        self.assertIn("remaining_capacity", loser[2])
        self.assertIn("remaining_slots", loser[2])

    def test_recalc_reverts_unexecuted_and_retains_executed_basis(self):
        # A：中严重度+低量+1 条未关闭整改（优先级 4），派给 insp-a
        item_a = self._inspection_item("A", "medium", 1, 10, "A1", open_record=True)
        # B：高严重度超量（优先级 10），派给 insp-b 并执行
        self._inspection_item("B", "high", 5, 1, "B1")
        queue = self._dispatch()
        a = next(it for it in queue["items"] if it["item_id"] == item_a["id"])
        b = next(it for it in queue["items"] if it["item_id"] == self._find_item("B1")["id"])
        self.assertEqual(a["priority"], 4)
        # 执行 B：保留原依据
        self.service.claim(b["id"], "insp-b", "inspector")
        executed = self.service.execute(b["id"], "insp-b", "inspector")
        self.assertEqual(executed["status"], "executed")
        # 关闭 A 的整改：优先级下降，未执行条目重算并退回待派
        recs = self.service.list_records(item_a["id"], "viewer")
        self.service.update_record_status(
            item_a["id"], recs[0]["id"], {"status": "closed"},
            "recorder", "applicant")
        queue = self.service.queue(self.day, "viewer")
        a_after = next(it for it in queue["items"] if it["item_id"] == item_a["id"])
        b_after = next(it for it in queue["items"] if it["item_id"] == self._find_item("B1")["id"])
        self.assertEqual(a_after["priority"], 3)  # 重算后优先级下降
        self.assertEqual(b_after["status"], "executed")  # 已执行结果保留
        self.assertIn("priority", b_after["basis"])  # 原依据仍在

    def test_resumable_dispatch_continues_without_double_occupancy(self):
        self.service.set_capacity(
            {"inspector": "insp-a", "day": self.day, "capacity": 5},
            "mgr", "compliance_manager")
        for k in range(5):
            self._inspection_item(f"许可{k}", "high", 5, 1, f"R{k}")
        self.service._inject_fail_after = 2
        with self.assertRaises(RuntimeError):
            self._dispatch()
        queue = self.service.queue(self.day, "viewer")
        self.assertEqual(
            len([it for it in queue["items"] if it["status"] == "dispatched"]), 2)
        self.assertEqual(sum(c["assigned"] for c in queue["capacities"]), 2)
        # 重试：从最后完成的许可继续，不重复占名额
        self.service._inject_fail_after = None
        queue = self._dispatch()
        self.assertEqual(
            len([it for it in queue["items"] if it["status"] == "dispatched"]), 5)
        self.assertEqual(sum(c["assigned"] for c in queue["capacities"]), 5)
        for it in queue["items"]:
            self.assertEqual(
                len(self.repo.list_active_batch_items_for_item(it["item_id"])), 1)

    def test_waitlist_when_capacity_insufficient(self):
        # 总容量 4，但有 5 个紧急许可：超出的留在待派（候补）
        for k in range(5):
            self._inspection_item(f"许可{k}", "critical", 10, 1, f"W{k}")
        queue = self._dispatch()
        dispatched = [it for it in queue["items"] if it["status"] == "dispatched"]
        queued = [it for it in queue["items"] if it["status"] == "queued"]
        self.assertEqual(len(dispatched), 4)
        self.assertEqual(len(queued), 1)

    def test_capacity_set_and_view(self):
        caps = self.service.list_capacities(self.day, "viewer")
        self.assertEqual(len(caps), 2)
        self.service.set_capacity(
            {"inspector": "insp-c", "day": self.day, "capacity": 3},
            "mgr", "compliance_manager")
        caps = self.service.list_capacities(self.day, "viewer")
        self.assertEqual(len(caps), 3)
        c = next(c for c in caps if c["inspector"] == "insp-c")
        self.assertEqual(c["capacity"], 3)
        self.assertEqual(c["remaining"], 3)

    def _find_item(self, external_ref):
        for item in self.service.list_items("viewer"):
            if item.get("external_ref") == external_ref:
                return item
        raise AssertionError(f"item {external_ref} not found")


if __name__ == "__main__":
    unittest.main()
