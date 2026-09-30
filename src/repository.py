"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Transaction:
    """单连接写事务，所有流水计算都在同一事务内完成。"""

    def __init__(self, connection: sqlite3.Connection, now: str = None) -> None:
        self.connection = connection
        self.now = now or _now()

    def _record_row(self, record_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return row

    @staticmethod
    def record(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def get_record(self, record_id: int) -> Dict[str, Any]:
        return self.record(self._record_row(record_id))

    def versions(self, record_id: int) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM plan_versions WHERE record_id=? ORDER BY version_no", (record_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def entries(self, record_id: int) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT e.*, v.reason AS void_reason, v.created_by AS voided_by, v.created_at AS voided_at,
                   CASE WHEN v.id IS NULL THEN 0 ELSE 1 END AS voided
            FROM service_entries e
            LEFT JOIN service_voids v ON v.entry_id = e.id
            WHERE e.record_id=? ORDER BY e.id
            """,
            (record_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_entry_by_credential(self, credential: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT e.*, v.reason AS void_reason, v.created_by AS voided_by, v.created_at AS voided_at,
                   CASE WHEN v.id IS NULL THEN 0 ELSE 1 END AS voided
            FROM service_entries e
            LEFT JOIN service_voids v ON v.entry_id = e.id
            WHERE e.credential=?
            """,
            (credential,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["record"] = self.record(
            self.connection.execute("SELECT * FROM records WHERE id=?", (item["record_id"],)).fetchone()
        )
        return item

    def latest_version_no(self, record_id: int) -> int:
        row = self.connection.execute(
            "SELECT MAX(version_no) AS no FROM plan_versions WHERE record_id=?", (record_id,)
        ).fetchone()
        return int(row["no"] or 0)

    def insert_version(self, record_id: int, version_no: int, authorized_minutes: int, reason: str, actor_id: str) -> None:
        self.connection.execute(
            "INSERT INTO plan_versions(record_id,version_no,authorized_minutes,reason,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, version_no, authorized_minutes, reason, actor_id, self.now),
        )

    def insert_entry(self, record_id: int, credential: str, service_date: str, minutes: int, provider: str, plan_version: int, actor_id: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO service_entries(record_id,credential,service_date,minutes,provider,plan_version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (record_id, credential, service_date, minutes, provider, plan_version, actor_id, self.now),
        )
        return int(cursor.lastrowid)

    def get_entry(self, entry_id: int) -> Dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT e.*, v.reason AS void_reason, v.created_by AS voided_by, v.created_at AS voided_at,
                   CASE WHEN v.id IS NULL THEN 0 ELSE 1 END AS voided
            FROM service_entries e
            LEFT JOIN service_voids v ON v.entry_id = e.id
            WHERE e.id=?
            """,
            (entry_id,),
        ).fetchone()
        if row is None:
            raise NotFound("服务流水不存在")
        return dict(row)

    def find_void(self, entry_id: int) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM service_voids WHERE entry_id=?", (entry_id,)).fetchone()
        return dict(row) if row is not None else None

    def void_entry(self, entry_id: int, reason: str, actor_id: str) -> None:
        self.connection.execute(
            "INSERT INTO service_voids(entry_id,reason,created_by,created_at) VALUES(?,?,?,?)",
            (entry_id, reason, actor_id, self.now),
        )

    def save_record(self, record_id: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        row = self._record_row(record_id)
        version = int(row["version"]) + 1
        self.connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, self.now, record_id),
        )
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), self.now),
        )
        return self.record(self.connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())

    def audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, int(self._record_row(record_id)["version"]),
             json.dumps(details, ensure_ascii=False, sort_keys=True), self.now),
        )


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    version_no INTEGER NOT NULL,
                    authorized_minutes INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(record_id, version_no)
                );
                CREATE TABLE IF NOT EXISTS service_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    credential TEXT NOT NULL UNIQUE,
                    service_date TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_voids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id INTEGER NOT NULL UNIQUE REFERENCES service_entries(id) ON DELETE CASCADE,
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_plan_versions_record ON plan_versions(record_id, version_no);
                CREATE INDEX IF NOT EXISTS idx_entries_record ON service_entries(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        connection = self._connect()
        now = _now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield Transaction(connection, now)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO plan_versions(record_id,version_no,authorized_minutes,reason,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, 1, int(payload["service_minutes"]), "计划创建时授权", actor_id, now),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1,
                     json.dumps({"state": state, "plan_version": 1, "authorized_minutes": int(payload["service_minutes"])},
                                ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("reference已存在") from exc
            except Exception:
                connection.rollback()
                raise
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def plan_versions(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_versions WHERE record_id=? ORDER BY version_no", (record_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def service_entries(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.*, v.reason AS void_reason, v.created_by AS voided_by, v.created_at AS voided_at,
                       CASE WHEN v.id IS NULL THEN 0 ELSE 1 END AS voided
                FROM service_entries e
                LEFT JOIN service_voids v ON v.entry_id = e.id
                WHERE e.record_id=? ORDER BY e.id
                """,
                (record_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
