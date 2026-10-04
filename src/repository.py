from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, PermissionDenied
from .rules import ID_PREFIX, BATCH_STATUSES, QUEUE_STATES, STATES


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
                CREATE TABLE IF NOT EXISTS inspectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    daily_capacity INTEGER NOT NULL DEFAULT 4 CHECK(daily_capacity BETWEEN 1 AND 1000),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inspection_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    day TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued','claimed','executed')),
                    priority INTEGER NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    threshold REAL NOT NULL,
                    open_records INTEGER NOT NULL DEFAULT 0,
                    inspector_id INTEGER REFERENCES inspectors(id),
                    result TEXT,
                    basis_frozen INTEGER NOT NULL DEFAULT 0,
                    claimed_at TEXT,
                    executed_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, day)
                );
                CREATE INDEX IF NOT EXISTS ix_batches_day ON inspection_batches(day, status, priority);
                CREATE TABLE IF NOT EXISTS schedule_cursors (
                    day TEXT PRIMARY KEY,
                    last_item_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
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

    def create_inspector(self, name: str, daily_capacity: int,
                         actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO inspectors(name, daily_capacity, active, created_by, created_at)
                       VALUES(?,?,1,?,?)""",
                    (name, daily_capacity, actor, now),
                )
                inspector_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("检查员姓名已存在") from exc
        return self.get_inspector(inspector_id)

    def get_inspector(self, inspector_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM inspectors WHERE id=?", (inspector_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("检查员不存在")
        return dict(row)

    def list_inspectors(self, active_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM inspectors"
        params: tuple = ()
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def enqueue_one(self, item: Dict[str, Any], open_records: int, day: str,
                    actor: str) -> Optional[int]:
        # 一个许可在同一天只入队一次；本事务同时推进游标，失败回滚后可从游标继续
        now = utc_now()
        from .rules import queue_basis
        basis = queue_basis(item["severity"], item["quantity"],
                            item["threshold"], open_records)
        with self._lock, self.conn:
            inserted = self.conn.execute(
                """INSERT OR IGNORE INTO inspection_batches(item_id, day, status, priority,
                   severity, quantity, threshold, open_records, created_by, created_at, updated_at)
                   VALUES(?,?,'queued',?,?,?,?,?,?,?,?)""",
                (item["id"], day, basis["priority"], item["severity"], item["quantity"],
                 item["threshold"], open_records, actor, now, now),
            ).rowcount
            self.conn.execute(
                """INSERT INTO schedule_cursors(day, last_item_id, updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(day) DO UPDATE SET last_item_id=excluded.last_item_id,
                   updated_at=excluded.updated_at
                   WHERE excluded.last_item_id>schedule_cursors.last_item_id""",
                (day, item["id"], now),
            )
            if inserted == 0:
                return None
            return self.conn.execute(
                "SELECT id FROM inspection_batches WHERE item_id=? AND day=?",
                (item["id"], day),
            ).fetchone()["id"]

    def scan_queue_items(self, day: str, after_id: int) -> List[Dict[str, Any]]:
        states = ",".join("?" for _ in QUEUE_STATES)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT i.* FROM items i
                    WHERE i.id>? AND i.status IN ({states})
                    ORDER BY i.id LIMIT 500""",
                (after_id, *QUEUE_STATES),
            ).fetchall()
        return [dict(row) for row in rows]

    def scan_catchup_items(self, day: str) -> List[Dict[str, Any]]:
        # 扫描之后新增/改状态的许可：符合入队状态但当天尚无批次
        states = ",".join("?" for _ in QUEUE_STATES)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT i.* FROM items i
                    WHERE i.status IN ({states})
                    AND NOT EXISTS(SELECT 1 FROM inspection_batches b
                                   WHERE b.item_id=i.id AND b.day=?)
                    ORDER BY i.id""",
                (*QUEUE_STATES, day),
            ).fetchall()
        return [dict(row) for row in rows]

    def schedule_cursor(self, day: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT last_item_id FROM schedule_cursors WHERE day=?", (day,)
            ).fetchone()
        return int(row["last_item_id"]) if row else 0

    def list_day_batches(self, day: str) -> List[Dict[str, Any]]:
        # 队列顺序：优先级（严重度+申报量+未关闭整改）降序，同级按创建先后
        with self._lock:
            rows = self.conn.execute(
                """SELECT b.*, it.title AS item_title FROM inspection_batches b
                   JOIN items it ON it.id=b.item_id
                   WHERE b.day=? ORDER BY b.priority DESC, b.item_id ASC""",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT b.*, it.title AS item_title FROM inspection_batches b
                   JOIN items it ON it.id=b.item_id WHERE b.id=?""",
                (batch_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("检查批次不存在")
        return dict(row)

    def inspector_loads(self, day: str) -> List[Dict[str, Any]]:
        # 当前负荷只统计当天未释放的认领（claimed）与已执行批次
        with self._lock:
            rows = self.conn.execute(
                """SELECT s.id, s.name, s.daily_capacity,
                   COUNT(b.id) AS load FROM inspectors s
                   LEFT JOIN inspection_batches b ON b.inspector_id=s.id
                       AND b.day=? AND b.status IN ('claimed','executed')
                   WHERE s.active=1 GROUP BY s.id ORDER BY s.id""",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_batch(self, batch_id: int, inspector_id: int,
                    actor: str) -> Dict[str, Any]:
        # 先到先得：条件更新保证两名监管员并发认领时只有一人占名额
        now = utc_now()
        with self._lock, self.conn:
            inspector = self.conn.execute(
                "SELECT * FROM inspectors WHERE id=? AND active=1",
                (inspector_id,),
            ).fetchone()
            if inspector is None:
                raise NotFoundError("检查员不存在")
            batch = self.conn.execute(
                "SELECT * FROM inspection_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("检查批次不存在")
            if batch["status"] != "queued":
                raise ConflictError("该批次已被认领或已执行")
            load = int(self.conn.execute(
                """SELECT COUNT(*) AS n FROM inspection_batches
                   WHERE day=? AND inspector_id=? AND status IN ('claimed','executed')""",
                (batch["day"], inspector_id),
            ).fetchone()["n"])
            if load >= int(inspector["daily_capacity"]):
                raise ConflictError("该检查员当天名额已满")
            cur = self.conn.execute(
                """UPDATE inspection_batches SET status='claimed', inspector_id=?,
                   claimed_at=?, updated_at=? WHERE id=? AND status='queued'""",
                (inspector_id, now, now, batch_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("该批次已被认领或已执行")
        return self.get_batch(batch_id)

    def claim_next(self, day: str, inspector_id: int, actor: str) -> Optional[Dict[str, Any]]:
        # 按当前负荷取队首待派批次
        batch_id = None
        with self._lock, self.conn:
            inspector = self.conn.execute(
                "SELECT * FROM inspectors WHERE id=? AND active=1",
                (inspector_id,),
            ).fetchone()
            if inspector is None:
                raise NotFoundError("检查员不存在")
            load = int(self.conn.execute(
                """SELECT COUNT(*) AS n FROM inspection_batches
                   WHERE day=? AND inspector_id=? AND status IN ('claimed','executed')""",
                (day, inspector_id),
            ).fetchone()["n"])
            if load >= int(inspector["daily_capacity"]):
                raise ConflictError("该检查员当天名额已满")
            row = self.conn.execute(
                """SELECT id FROM inspection_batches WHERE day=? AND status='queued'
                   ORDER BY priority DESC, item_id ASC LIMIT 1""",
                (day,),
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            cur = self.conn.execute(
                """UPDATE inspection_batches SET status='claimed', inspector_id=?,
                   claimed_at=?, updated_at=? WHERE id=? AND status='queued'""",
                (inspector_id, now, now, row["id"]),
            )
            if cur.rowcount == 0:
                raise ConflictError("该批次已被认领或已执行")
            batch_id = int(row["id"])
        return self.get_batch(batch_id)

    def dispatch_next(self, day: str, actor: str) -> Optional[Dict[str, Any]]:
        # 自动派单：在名额未满的检查员中按当前负荷选剩余名额最多者，取队首批次
        from .rules import choose_inspector
        with self._lock, self.conn:
            loads = self.inspector_loads(day)
            choice = choose_inspector(loads, default_capacity=4)
            if choice is None:
                return None
            inspector_id = choice["id"]
            row = self.conn.execute(
                """SELECT id FROM inspection_batches WHERE day=? AND status='queued'
                   ORDER BY priority DESC, item_id ASC LIMIT 1""",
                (day,),
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            cur = self.conn.execute(
                """UPDATE inspection_batches SET status='claimed', inspector_id=?,
                   claimed_at=?, updated_at=? WHERE id=? AND status='queued'""",
                (inspector_id, now, now, row["id"]),
            )
            if cur.rowcount == 0:
                raise ConflictError("该批次已被认领或已执行")
            batch_id = int(row["id"])
        batch = self.get_batch(batch_id)
        batch["assigned_inspector_id"] = inspector_id
        return batch

    def execute_batch(self, batch_id: int, inspector_id: int, result: str,
                      actor: str) -> Dict[str, Any]:
        # 已执行：结果与依据快照永久保留
        now = utc_now()
        with self._lock, self.conn:
            batch = self.conn.execute(
                "SELECT * FROM inspection_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("检查批次不存在")
            if batch["status"] == "executed":
                raise ConflictError("该批次已执行，结果按原依据保留")
            if batch["status"] != "claimed" or int(batch["inspector_id"]) != int(inspector_id):
                raise PermissionDenied("只有认领该批次的检查员可以执行")
            self.conn.execute(
                """UPDATE inspection_batches SET status='executed', result=?,
                   basis_frozen=1, executed_at=?, updated_at=? WHERE id=?""",
                (result, now, now, batch_id),
            )
        return self.get_batch(batch_id)

    def reset_unexecuted_for_item(self, item_id: int, basis: Dict[str, Any]) -> int:
        # 整改事项变化：未执行批次立即重算依据并退回待派；已执行结果保留原依据
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE inspection_batches SET status='queued', priority=?, severity=?,
                   quantity=?, threshold=?, open_records=?, inspector_id=NULL, claimed_at=NULL,
                   updated_at=? WHERE item_id=? AND status!='executed'""",
                (basis["priority"], basis["severity"], basis["quantity"],
                 basis["threshold"], basis["open_records"], now, item_id),
            )
            return int(cur.rowcount)

    def close_record(self, record_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=? AND status='open'",
                (record_id,),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM records WHERE id=?", (record_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("整改事项不存在")
                raise ConflictError("整改事项已关闭")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
        return dict(row)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("整改事项不存在")
        return dict(row)

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
