from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inspector_days (
                    inspector TEXT NOT NULL,
                    day TEXT NOT NULL,
                    capacity INTEGER NOT NULL DEFAULT 5,
                    assigned INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (inspector, day)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','dispatched','executed','cancelled')),
                    basis TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    day TEXT NOT NULL,
                    inspector TEXT,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued','dispatched','claimed','executed')),
                    priority REAL NOT NULL DEFAULT 0,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    executed_at TEXT,
                    basis TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, batch_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_batch_items_active
                    ON batch_items(item_id)
                    WHERE status IN ('queued','dispatched','claimed');
                CREATE INDEX IF NOT EXISTS ix_batch_items_day_status
                    ON batch_items(day, status);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---------- 检查员当天名额 ----------
    def get_inspector_day(self, inspector: str, day: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM inspector_days WHERE inspector=? AND day=?",
                (inspector, day),
            ).fetchone()
        return dict(row) if row else None

    def list_inspector_days(self, day: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM inspector_days WHERE day=? ORDER BY inspector", (day,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_capacity(self, inspector: str, day: str, capacity: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO inspector_days(inspector, day, capacity, assigned)
                   VALUES(?,?,?,0)
                   ON CONFLICT(inspector, day) DO UPDATE SET capacity=excluded.capacity""",
                (inspector, day, int(capacity)),
            )
        row = self.get_inspector_day(inspector, day)
        assert row is not None
        return row

    def increment_assigned(self, inspector: str, day: str) -> bool:
        """原子占用一个名额；仅当当前负荷未超容量时成功。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE inspector_days SET assigned=assigned+1 WHERE inspector=? AND day=? AND assigned<capacity",
                (inspector, day),
            )
            return cur.rowcount == 1

    def decrement_assigned(self, inspector: str, day: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE inspector_days SET assigned=MAX(0,assigned-1) WHERE inspector=? AND day=?",
                (inspector, day),
            )

    # ---------- 检查批次 ----------
    def get_or_create_batch(self, day: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO batches(day, status, basis, created_at, updated_at)
                   VALUES(?,?,?,?,?)""",
                (day, "pending", "{}", now, now),
            )
            row = self.conn.execute("SELECT * FROM batches WHERE day=?", (day,)).fetchone()
        return dict(row)

    def get_batch_by_day(self, day: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM batches WHERE day=?", (day,)).fetchone()
        return dict(row) if row else None

    def update_batch_status(self, day: str, status: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE batches SET status=?, updated_at=? WHERE day=?",
                (status, utc_now(), day),
            )

    # ---------- 批次明细（队列条目） ----------
    def get_batch_item(self, batch_item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT bi.*, i.title, i.severity, i.quantity, i.threshold, i.status AS item_status
                   FROM batch_items bi JOIN items i ON i.id=bi.item_id
                   WHERE bi.id=?""",
                (batch_item_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_active_batch_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM batch_items
                   WHERE item_id=? AND status IN ('queued','dispatched','claimed')
                   ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_active_batch_items_for_item(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batch_items WHERE item_id=? AND status IN ('queued','dispatched','claimed')",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_queued_batch_items(self, day: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batch_items WHERE day=? AND status='queued' ORDER BY priority DESC, id",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_batch_items(self, day: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT bi.*, i.title, i.severity, i.quantity, i.threshold, i.status AS item_status
                   FROM batch_items bi JOIN items i ON i.id=bi.item_id
                   WHERE bi.day=? ORDER BY
                     CASE bi.status WHEN 'executed' THEN 3 WHEN 'claimed' THEN 2 WHEN 'dispatched' THEN 1 ELSE 0 END,
                     bi.priority DESC, bi.id""",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_queued_item(self, batch_id: int, item_id: int, day: str,
                            priority: float) -> Dict[str, Any]:
        """待派条目不存在则建立，已存在（待派）则刷新优先级；幂等，可安全重试。"""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM batch_items WHERE batch_id=? AND item_id=?",
                (batch_id, item_id),
            ).fetchone()
            if row is None:
                cur = self.conn.execute(
                    """INSERT INTO batch_items(batch_id, item_id, day, inspector, status,
                       priority, basis, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (batch_id, item_id, day, None, "queued", priority, "{}", utc_now()),
                )
                bi_id = int(cur.lastrowid)
            else:
                bi_id = int(row["id"])
                self.conn.execute(
                    "UPDATE batch_items SET priority=? WHERE id=? AND status='queued'",
                    (priority, bi_id),
                )
        result = self.get_batch_item(bi_id)
        assert result is not None
        return result

    def assign_item(self, batch_item_id: int, inspector: str) -> bool:
        """待派 -> 已派发（占用名额）。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE batch_items SET status='dispatched', inspector=? WHERE id=? AND status='queued'",
                (inspector, batch_item_id),
            )
            return cur.rowcount == 1

    def claim_item(self, batch_item_id: int, actor: str) -> bool:
        """已派发 -> 已认领；先到者占用，原子条件更新。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE batch_items SET status='claimed', claimed_by=?, claimed_at=? WHERE id=? AND status='dispatched'",
                (actor, utc_now(), batch_item_id),
            )
            return cur.rowcount == 1

    def revert_to_queued(self, batch_item_id: int, priority: float) -> None:
        """未执行条目退回待派：释放已占用名额，清空认领人，重算优先级。"""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM batch_items WHERE id=?", (batch_item_id,)
            ).fetchone()
            if row is None:
                return
            if row["inspector"]:
                self.conn.execute(
                    "UPDATE inspector_days SET assigned=MAX(0,assigned-1) WHERE inspector=? AND day=?",
                    (row["inspector"], row["day"]),
                )
            self.conn.execute(
                """UPDATE batch_items SET status='queued', inspector=NULL, claimed_by=NULL,
                   claimed_at=NULL, priority=? WHERE id=?""",
                (priority, batch_item_id),
            )

    def execute_item(self, batch_item_id: int, basis: Dict[str, Any]) -> bool:
        """已认领 -> 已执行；保留原依据快照。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE batch_items SET status='executed', executed_at=?, basis=? WHERE id=? AND status='claimed'",
                (utc_now(), json.dumps(basis, ensure_ascii=False, sort_keys=True), batch_item_id),
            )
            return cur.rowcount == 1

    def count_dispatched(self, day: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM batch_items WHERE day=? AND status='dispatched'",
                (day,),
            ).fetchone()
        return int(row["n"])

    def all_items_executed(self, batch_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM batch_items WHERE batch_id=? AND status!='executed'",
                (batch_id,),
            ).fetchone()
        return int(row["n"]) == 0

    def update_record_status(self, item_id: int, record_id: int, status: str) -> Optional[Dict[str, Any]]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET status=? WHERE id=? AND item_id=?",
                (status, record_id, item_id),
            )
            if cur.rowcount == 0:
                return None
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row else None
