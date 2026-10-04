from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    INSPECTOR_MANAGE_ROLES, INSPECTOR_ROLES, RECORD_ROLES,
                    SCHEDULE_ROLES, VIEW_ROLES, completion_blockers,
                    escalation_required, normalize_day, priority_score,
                    queue_basis, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

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
        # 整改事项变化：未执行批次立即重算并退回待派；已执行结果保留原依据
        if status == "open":
            self._recompute_item_batches(item_id, actor, reason="record_added")
        return record

    def close_record(self, record_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        record = self.repository.close_record(record_id, actor)
        self.repository.append_audit("record_close", ENTITY, record["item_id"], actor, {
            "record_id": record_id,
        })
        self._recompute_item_batches(record["item_id"], actor, reason="record_closed")
        return record

    def _recompute_item_batches(self, item_id: int, actor: str, reason: str) -> int:
        item = self.repository.get_item(item_id)
        open_records = self.repository.open_record_count(item_id)
        basis = queue_basis(item["severity"], item["quantity"],
                            item["threshold"], open_records)
        changed = self.repository.reset_unexecuted_for_item(item_id, basis)
        if changed:
            self.repository.append_audit("batch_recompute", "检查批次", item_id, actor, {
                "reason": reason, "open_records": open_records,
                "priority": basis["priority"], "batches_returned": changed,
            })
        return changed

    def create_inspector(self, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, INSPECTOR_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        capacity = int(require_number(payload.get("daily_capacity", 4),
                                      "daily_capacity", 1))
        if capacity > 1000:
            from .domain import ValidationError
            raise ValidationError("daily_capacity不能超过1000")
        inspector = self.repository.create_inspector(name, capacity, actor)
        self.repository.append_audit("inspector_create", "检查员", inspector["id"], actor, {
            "name": name, "daily_capacity": capacity,
        })
        return inspector

    def list_inspectors(self, role: str) -> list:
        self._view(role)
        return self.repository.list_inspectors()

    def build_day_queue(self, day: Optional[str], actor: str, role: str) -> Dict[str, Any]:
        # 许可、整改事项和检查批次接成日容量队列；可重复调用，断点续传不重复
        ensure_role(role, SCHEDULE_ROLES)
        actor = require_text(actor, "actor", 100)
        day = normalize_day(day)
        enqueued = 0
        after_id = self.repository.schedule_cursor(day)
        # 断点扫描：从最后完成许可继续
        while True:
            items = self.repository.scan_queue_items(day, after_id)
            if not items:
                break
            for item in items:
                batch_id = self.repository.enqueue_one(
                    item, self.repository.open_record_count(item["id"]), day, actor)
                after_id = item["id"]
                if batch_id is not None:
                    enqueued += 1
        # 扫描之后新增/改状态的许可补入（唯一索引兜底不重复占名额）
        for item in self.repository.scan_catchup_items(day):
            if self.repository.enqueue_one(
                    item, self.repository.open_record_count(item["id"]), day, actor):
                enqueued += 1
        self.repository.append_audit("queue_build", "检查批次", 0, actor, {
            "day": day, "enqueued": enqueued,
        })
        return self.day_schedule(day, role)

    def day_schedule(self, day: Optional[str], role: str) -> Dict[str, Any]:
        self._view(role)
        day = normalize_day(day)
        batches = self.repository.list_day_batches(day)
        loads = self.repository.inspector_loads(day)
        inspectors = [{
            "id": row["id"], "name": row["name"],
            "daily_capacity": int(row["daily_capacity"]),
            "load": int(row["load"]),
            "remaining": int(row["daily_capacity"]) - int(row["load"]),
        } for row in loads]
        queue = [{
            "id": b["id"], "item_id": b["item_id"], "item_title": b["item_title"],
            "day": b["day"], "status": b["status"], "priority": b["priority"],
            "severity": b["severity"], "quantity": b["quantity"],
            "threshold": b["threshold"], "open_records": b["open_records"],
            "inspector_id": b["inspector_id"], "result": b["result"],
            "basis_frozen": bool(b["basis_frozen"]),
            "claimed_at": b["claimed_at"], "executed_at": b["executed_at"],
        } for b in batches]
        return {"day": day, "queue": queue, "inspectors": inspectors,
                "remaining_slots": sum(max(0, i["remaining"]) for i in inspectors)}

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_batch(batch_id)

    def claim_batch(self, batch_id: int, inspector_id: int, actor: str,
                    role: str) -> Dict[str, Any]:
        # 两名监管员同时认领：先到者占用，后到者收到冲突并可查看剩余名额
        ensure_role(role, INSPECTOR_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(inspector_id, int) or inspector_id < 1:
            from .domain import ValidationError
            raise ValidationError("inspector_id必须是正整数")
        batch = self.repository.claim_batch(batch_id, inspector_id, actor)
        self.repository.append_audit("batch_claim", "检查批次", batch_id, actor, {
            "item_id": batch["item_id"], "inspector_id": inspector_id,
            "day": batch["day"], "priority": batch["priority"],
        })
        return batch

    def claim_next(self, day: Optional[str], inspector_id: int, actor: str,
                   role: str) -> Optional[Dict[str, Any]]:
        # 按当前负荷自动从队首取一个待派批次
        ensure_role(role, INSPECTOR_ROLES)
        actor = require_text(actor, "actor", 100)
        day = normalize_day(day)
        if not isinstance(inspector_id, int) or inspector_id < 1:
            from .domain import ValidationError
            raise ValidationError("inspector_id必须是正整数")
        batch = self.repository.claim_next(day, inspector_id, actor)
        if batch is not None:
            self.repository.append_audit("batch_claim", "检查批次", batch["id"], actor, {
                "item_id": batch["item_id"], "inspector_id": inspector_id,
                "day": day, "priority": batch["priority"], "auto": True,
            })
        return batch

    def dispatch_next(self, day: Optional[str], actor: str,
                      role: str) -> Optional[Dict[str, Any]]:
        # 监管员派单：名额按当前负荷分配给剩余名额最多的检查员
        ensure_role(role, SCHEDULE_ROLES)
        actor = require_text(actor, "actor", 100)
        day = normalize_day(day)
        batch = self.repository.dispatch_next(day, actor)
        if batch is not None:
            self.repository.append_audit("batch_dispatch", "检查批次", batch["id"], actor, {
                "item_id": batch["item_id"],
                "inspector_id": batch["assigned_inspector_id"],
                "day": day, "priority": batch["priority"],
            })
        return batch

    def execute_batch(self, batch_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, INSPECTOR_ROLES)
        actor = require_text(actor, "actor", 100)
        result = require_text(payload.get("result"), "result")
        inspector_id = payload.get("inspector_id")
        if not isinstance(inspector_id, int) or inspector_id < 1:
            from .domain import ValidationError
            raise ValidationError("inspector_id必须是正整数")
        before = self.repository.get_batch(batch_id)
        batch = self.repository.execute_batch(batch_id, inspector_id, result, actor)
        # 已执行结果保留原依据：审计记录冻结时的依据
        self.repository.append_audit("batch_execute", "检查批次", batch_id, actor, {
            "item_id": batch["item_id"], "inspector_id": inspector_id,
            "day": batch["day"], "basis": {
                "priority": before["priority"], "severity": before["severity"],
                "quantity": before["quantity"], "threshold": before["threshold"],
                "open_records": before["open_records"],
            },
        })
        return batch

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

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
