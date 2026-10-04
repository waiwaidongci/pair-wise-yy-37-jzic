from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CAPACITY_ROLES, CLAIM_ROLES, CREATE_ROLES,
                    DEFAULT_DAILY_CAPACITY, DISPATCH_ROLES, ENTITY, RECORD_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers, escalation_required,
                    load_factor, priority_score, remaining_capacity,
                    response_deadline_hours, role_for_transition, validate_day,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository
        # 故障注入：仅用于测试“写入失败后从最后完成许可继续，重试不重复占名额”
        self._inject_fail_after: Optional[int] = None
        self._write_count = 0

    def _maybe_fail(self) -> None:
        if self._inject_fail_after is not None:
            self._write_count += 1
            if self._write_count >= self._inject_fail_after:
                raise RuntimeError("模拟写入失败")

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        # 整改事项变化：未执行批次立即重算并退回待派
        self._recalculate(item_id)
        return record

    def update_record_status(self, item_id: int, record_id: int,
                              payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        status = payload.get("status")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        record = self.repository.update_record_status(item_id, record_id, status)
        if record is None:
            raise NotFoundError("整改事项不存在")
        self.repository.append_audit("record_status", ENTITY, item_id, actor, {
            "record_id": record_id, "status": status,
        })
        # 整改事项变化：未执行批次立即重算并退回待派，已执行结果保留原依据
        self._recalculate(item_id)
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------- 日容量队列 ----------
    def set_capacity(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CAPACITY_ROLES)
        actor = require_text(actor, "actor", 100)
        inspector = require_text(payload.get("inspector"), "inspector", 100)
        day = validate_day(require_text(payload.get("day"), "day", 20))
        capacity = int(require_number(payload.get("capacity"), "capacity", 1))
        if capacity < 1:
            raise ValidationError("capacity必须不小于1")
        return self.repository.set_capacity(inspector, day, capacity)

    def list_capacities(self, day: str, role: str) -> list:
        self._view(role)
        day = validate_day(day)
        rows = self.repository.list_inspector_days(day)
        for row in rows:
            row["remaining"] = remaining_capacity(row["capacity"], row["assigned"])
        return rows

    def dispatch(self, day: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        day = validate_day(day)
        self._dispatch(day)
        self.repository.append_audit("dispatch", ENTITY, 0, actor, {"day": day})
        return self.queue(day, role)

    def _dispatch(self, day: str) -> None:
        """把许可、整改事项接成日容量队列：按优先级排序，按当前负荷分配名额。

        每个许可的落库都是独立事务，失败后重试从最后完成的许可继续，
        已占用名额的许可会被跳过，不重复占名额。
        """
        batch = self.repository.get_or_create_batch(day)
        candidates = self._gather_candidates(day)
        enriched = []
        for item in candidates:
            open_records = self.repository.open_record_count(item["id"])
            priority = priority_score(
                item["severity"], item["quantity"], item["threshold"], open_records)
            enriched.append((item, priority))
        enriched.sort(key=lambda pair: (-pair[1], pair[0]["id"]))
        for item, priority in enriched:
            self._dispatch_one(batch, item, priority, day)
        self._refresh_batch_status(day)

    def _gather_candidates(self, day: str) -> list:
        """待派候选：检查中且尚无活跃批次条目的许可，加上本批退回待派的条目。"""
        candidates = []
        seen = set()
        for item in self.repository.list_items(status="inspection"):
            if item["id"] in seen:
                continue
            if self.repository.find_active_batch_item(item["id"]) is None:
                seen.add(item["id"])
                candidates.append(item)
        for batch_item in self.repository.list_queued_batch_items(day):
            if batch_item["item_id"] in seen:
                continue
            seen.add(batch_item["item_id"])
            candidates.append(self.repository.get_item(batch_item["item_id"]))
        return candidates

    def _dispatch_one(self, batch: Dict[str, Any], item: Dict[str, Any],
                       priority: float, day: str) -> None:
        existing = self.repository.find_active_batch_item(item["id"])
        if existing is not None and existing["status"] in ("dispatched", "claimed", "executed"):
            # 已占用名额或已执行：重试时跳过，不重复占名额
            return
        batch_item = self.repository.upsert_queued_item(batch["id"], item["id"], day, priority)
        # 按当前负荷分配：负荷最低（剩余名额比例最高）的检查员优先
        inspector_days = self.repository.list_inspector_days(day)
        best = None
        best_load = None
        for row in inspector_days:
            if row["assigned"] >= row["capacity"]:
                continue
            load = load_factor(row["assigned"], row["capacity"])
            if best is None or load < best_load or (load == best_load and row["inspector"] < best):
                best = row["inspector"]
                best_load = load
        if best is not None:
            if self.repository.increment_assigned(best, day):
                self.repository.assign_item(batch_item["id"], best)
        # 无剩余名额：保留在待派队列（候补），不占名额
        self._maybe_fail()

    def claim(self, batch_item_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_item = self.repository.get_batch_item(batch_item_id)
        if batch_item is None:
            raise NotFoundError("检查批次条目不存在")
        if batch_item["status"] == "executed":
            raise ConflictError("该检查已执行")
        if batch_item["status"] == "queued":
            raise ConflictError("该检查尚在候补队列，暂不可认领")
        if batch_item["status"] == "claimed":
            if batch_item["claimed_by"] == actor:
                return self._queue_item_view(batch_item)  # 幂等重试
            raise ConflictError("名额已被认领", self._remaining_quota(batch_item["day"], actor))
        if not self.repository.claim_item(batch_item_id, actor):
            # 先到者已占用：后到者看到剩余名额
            raise ConflictError("手慢无，名额已被抢先认领",
                                self._remaining_quota(batch_item["day"], actor))
        return self._queue_item_view(self.repository.get_batch_item(batch_item_id))

    def execute(self, batch_item_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_item = self.repository.get_batch_item(batch_item_id)
        if batch_item is None:
            raise NotFoundError("检查批次条目不存在")
        if batch_item["status"] != "claimed":
            raise ConflictError("只能执行已认领的检查")
        item = self.repository.get_item(batch_item["item_id"])
        open_records = self.repository.open_record_count(item["id"])
        basis = {
            "priority": batch_item["priority"],
            "severity": item["severity"],
            "quantity": item["quantity"],
            "threshold": item["threshold"],
            "open_records": open_records,
            "inspector": batch_item["inspector"],
            "day": batch_item["day"],
            "claimed_by": batch_item["claimed_by"],
            "executed_by": actor,
        }
        if not self.repository.execute_item(batch_item_id, basis):
            raise ConflictError("执行失败，状态已变化")
        self._refresh_batch_status(batch_item["day"])
        return self._queue_item_view(self.repository.get_batch_item(batch_item_id))

    def queue(self, day: str, role: str) -> Dict[str, Any]:
        self._view(role)
        day = validate_day(day)
        batch = self.repository.get_batch_by_day(day)
        items = self.repository.list_batch_items(day)
        capacities = self.repository.list_inspector_days(day)
        for row in capacities:
            row["remaining"] = remaining_capacity(row["capacity"], row["assigned"])
        return {"day": day, "batch": batch, "items": items, "capacities": capacities}

    def _recalculate(self, item_id: int) -> None:
        """整改事项变化后：未执行条目立即重算优先级并退回待派，已执行保留原依据。"""
        item = self.repository.get_item(item_id)
        open_records = self.repository.open_record_count(item_id)
        priority = priority_score(
            item["severity"], item["quantity"], item["threshold"], open_records)
        days = set()
        for batch_item in self.repository.list_active_batch_items_for_item(item_id):
            days.add(batch_item["day"])
            self.repository.revert_to_queued(batch_item["id"], priority)
        for day in days:
            self.repository.update_batch_status(day, "pending")
            self._dispatch(day)  # 立即按新优先级重排（幂等，已占用条目跳过）

    def _refresh_batch_status(self, day: str) -> None:
        batch = self.repository.get_batch_by_day(day)
        if batch is None:
            return
        items = self.repository.list_batch_items(day)
        if not items:
            return
        if all(it["status"] == "executed" for it in items):
            self.repository.update_batch_status(day, "executed")
        elif any(it["status"] in ("dispatched", "claimed") for it in items):
            self.repository.update_batch_status(day, "dispatched")
        else:
            self.repository.update_batch_status(day, "pending")

    def _remaining_quota(self, day: str, actor: str) -> Dict[str, Any]:
        row = self.repository.get_inspector_day(actor, day)
        capacity = row["capacity"] if row else DEFAULT_DAILY_CAPACITY
        assigned = row["assigned"] if row else 0
        return {
            "remaining_capacity": remaining_capacity(capacity, assigned),
            "remaining_slots": self.repository.count_dispatched(day),
        }

    def _queue_item_view(self, batch_item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if batch_item is None:
            raise NotFoundError("检查批次条目不存在")
        return dict(batch_item)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        open_records = self.repository.open_record_count(item["id"])
        result = dict(item)
        result["open_records"] = open_records
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"], open_records)
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
