"""SQLite 表结构与事务访问。

records/audit_events 保存计划主单与审计时间线；
plan_versions 是只追加的授权版本表；service_entries 是只追加的服务流水（登记/冲销）。
所有会改变流水或计划版本的操作都在单条 BEGIN IMMEDIATE 事务内完成，
保证两人并发提交时，累计有效分钟与缺口一致。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound
from .rules import GATE_LABELS, allocate_ledger, apply_ledger_totals, build_ledger_summary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    plan_version INTEGER NOT NULL,
                    authorized_minutes INTEGER NOT NULL,
                    opening_minutes INTEGER NOT NULL DEFAULT 0,
                    change_reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(record_id, plan_version)
                );
                CREATE TABLE IF NOT EXISTS service_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    entry_type TEXT NOT NULL CHECK(entry_type IN ('registration','reversal')),
                    credential TEXT,
                    service_date TEXT,
                    minutes INTEGER NOT NULL,
                    provider TEXT,
                    plan_version INTEGER NOT NULL,
                    authorized_minutes INTEGER NOT NULL,
                    effective_minutes INTEGER NOT NULL DEFAULT 0,
                    pending_minutes INTEGER NOT NULL DEFAULT 0,
                    reverses_id INTEGER REFERENCES service_entries(id),
                    reversal_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_service_credential
                    ON service_entries(record_id, credential) WHERE entry_type = 'registration';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_service_reversal_target
                    ON service_entries(reverses_id) WHERE reverses_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_service_record ON service_entries(record_id, seq);
                CREATE INDEX IF NOT EXISTS idx_versions_record ON plan_versions(record_id, plan_version);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # ---- 计划主单 -----------------------------------------------------------
    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO plan_versions(record_id,plan_version,authorized_minutes,opening_minutes,change_reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, 1, int(payload["service_minutes"]), int(payload.get("delivered_minutes", 0)), "初始授权", actor_id, now),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1,
                     json.dumps({"state": state, "plan_version": 1,
                                 "authorized_minutes": int(payload["service_minutes"]),
                                 "opening_minutes": int(payload.get("delivered_minutes", 0))},
                                ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
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

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---- 流水账读取 ----------------------------------------------------------
    def _load_ledger(self, connection: sqlite3.Connection, record_id: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[int, Dict[str, Any]], Dict[int, int]]:
        version_rows = [dict(row) for row in connection.execute(
            "SELECT * FROM plan_versions WHERE record_id=? ORDER BY plan_version", (record_id,)).fetchall()]
        entry_rows = [dict(row) for row in connection.execute(
            "SELECT * FROM service_entries WHERE record_id=? ORDER BY seq", (record_id,)).fetchall()]
        registrations = [row for row in entry_rows if row["entry_type"] == "registration"]
        reversals = {
            int(row["reverses_id"]): row
            for row in entry_rows
            if row["entry_type"] == "reversal"
        }
        openings = {int(row["plan_version"]): int(row["opening_minutes"]) for row in version_rows}
        return version_rows, registrations, reversals, openings

    def _snapshot_from_connection(self, connection: sqlite3.Connection, record_id: int) -> Dict[str, Any]:
        version_rows, registrations, reversals, openings = self._load_ledger(connection, record_id)
        versions = {int(row["plan_version"]): int(row["authorized_minutes"]) for row in version_rows}
        allocation = allocate_ledger(versions, registrations, set(reversals), openings)
        return build_ledger_summary(version_rows, registrations, reversals, openings, allocation)

    def ledger_snapshot(self, record_id: int) -> Dict[str, Any]:
        self.get(record_id)
        with self._connect() as connection:
            return self._snapshot_from_connection(connection, record_id)

    def _recompute_in_tx(self, connection: sqlite3.Connection, record_id: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """在当前事务内按各计划版本重算每笔登记的有效/待处理分钟，并刷新派生列。"""
        version_rows, registrations, reversals, openings = self._load_ledger(connection, record_id)
        versions = {int(row["plan_version"]): int(row["authorized_minutes"]) for row in version_rows}
        allocation = allocate_ledger(versions, registrations, set(reversals), openings)
        for entry in registrations:
            effective, pending = allocation[int(entry["id"])]
            connection.execute(
                "UPDATE service_entries SET effective_minutes=?, pending_minutes=? WHERE id=?",
                (effective, pending, entry["id"]),
            )
        snapshot = build_ledger_summary(version_rows, registrations, reversals, openings, allocation)
        record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        payload = apply_ledger_totals(json.loads(record_row["payload"]), snapshot)
        now = _now()
        connection.execute(
            "UPDATE records SET payload=?, updated_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), now, record_id),
        )
        return payload, snapshot

    # ---- 服务登记（幂等） -----------------------------------------------------
    def post_service(
        self,
        record_id: int,
        expected_version: int,
        entry: Dict[str, Any],
        actor_id: str,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")

            # 幂等：重复凭据（断网重送/并发重送）只返回原结果，不再登记第二遍，也不抬版本。
            existing = connection.execute(
                "SELECT * FROM service_entries WHERE record_id=? AND credential=? AND entry_type='registration'",
                (record_id, entry["credential"]),
            ).fetchone()
            if existing is not None:
                # 幂等重放只读返回原结果，不抬版本、不触碰记录。
                snapshot = self._snapshot_from_connection(connection, record_id)
                entry_view = self._entry_view(existing, connection)
                result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
                response = self._row(result)
                response["entry"] = entry_view
                response["ledger"] = snapshot
                response["idempotent_replay"] = True
                return response

            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            current_version_row = connection.execute(
                "SELECT * FROM plan_versions WHERE record_id=? ORDER BY plan_version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            seq_row = connection.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM service_entries WHERE record_id=?", (record_id,)).fetchone()
            seq = int(seq_row["m"]) + 1
            cursor = connection.execute(
                """INSERT INTO service_entries
                   (record_id,seq,entry_type,credential,service_date,minutes,provider,
                    plan_version,authorized_minutes,effective_minutes,pending_minutes,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record_id, seq, "registration", entry["credential"], entry["service_date"], entry["minutes"],
                 entry["provider"], int(current_version_row["plan_version"]),
                 int(current_version_row["authorized_minutes"]), 0, 0, actor_id, now),
            )
            new_entry_id = int(cursor.lastrowid)

            payload, snapshot = self._recompute_in_tx(connection, record_id)
            version = int(expected_version) + 1
            fresh_entry = connection.execute("SELECT * FROM service_entries WHERE id=?", (new_entry_id,)).fetchone()
            entry_view = self._entry_view(fresh_entry, connection)
            allocation = entry_view["effective_minutes"], entry_view["pending_minutes"]
            now2 = _now()
            connection.execute(
                "UPDATE records SET version=?, updated_by=?, updated_at=? WHERE id=?",
                (version, actor_id, now2, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "log_service", actor_id, version,
                 json.dumps({"summary": "服务记录已登记", "credential": entry["credential"],
                             "service_date": entry["service_date"], "minutes": entry["minutes"],
                             "provider": entry["provider"],
                             "plan_version": int(current_version_row["plan_version"]),
                             "authorized_minutes": int(current_version_row["authorized_minutes"]),
                             "effective_minutes": allocation[0],
                             "pending_minutes": allocation[1],
                             "gap_minutes": allocation[1],
                             "replay": False}, ensure_ascii=False, sort_keys=True), now2),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        response = self._row(result)
        response["entry"] = entry_view
        response["ledger"] = snapshot
        response["idempotent_replay"] = False
        return response

    @staticmethod
    def _entry_view(row: sqlite3.Row, connection: sqlite3.Connection) -> Dict[str, Any]:
        reversal = connection.execute(
            "SELECT * FROM service_entries WHERE reverses_id=? AND entry_type='reversal'", (row["id"],)).fetchone()
        return {
            "id": int(row["id"]),
            "seq": int(row["seq"]),
            "type": "registration",
            "credential": row["credential"],
            "service_date": row["service_date"],
            "provider": row["provider"],
            "minutes": int(row["minutes"]),
            "plan_version": int(row["plan_version"]),
            "authorized_minutes": int(row["authorized_minutes"]),
            "effective_minutes": int(row["effective_minutes"]),
            "pending_minutes": int(row["pending_minutes"]),
            "gap_minutes": int(row["pending_minutes"]),
            "status": "reversed" if reversal is not None else "posted",
            "reversal_reason": reversal["reversal_reason"] if reversal is not None else None,
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    # ---- 冲销（不修改原流水） --------------------------------------------------
    def reverse_service(
        self,
        record_id: int,
        expected_version: int,
        credential: str,
        reason: str,
        actor_id: str,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            target = connection.execute(
                "SELECT * FROM service_entries WHERE record_id=? AND credential=? AND entry_type='registration'",
                (record_id, credential),
            ).fetchone()
            if target is None:
                connection.rollback()
                raise NotFound("凭据对应的服务记录不存在")

            existing = connection.execute(
                "SELECT * FROM service_entries WHERE reverses_id=? AND entry_type='reversal'",
                (target["id"],),
            ).fetchone()
            if existing is not None:
                # 冲销也幂等：重复冲销只返回原冲销结果，不抬版本。
                snapshot = self._snapshot_from_connection(connection, record_id)
                entry_view = self._entry_view(target, connection)
                result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
                response = self._row(result)
                response["reversal"] = dict(existing)
                response["entry"] = entry_view
                response["ledger"] = snapshot
                response["idempotent_replay"] = True
                return response

            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            seq_row = connection.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM service_entries WHERE record_id=?", (record_id,)).fetchone()
            seq = int(seq_row["m"]) + 1
            cursor = connection.execute(
                """INSERT INTO service_entries
                   (record_id,seq,entry_type,credential,service_date,minutes,provider,
                    plan_version,authorized_minutes,effective_minutes,pending_minutes,
                    reverses_id,reversal_reason,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record_id, seq, "reversal", None, target["service_date"], int(target["minutes"]),
                 target["provider"], int(target["plan_version"]), int(target["authorized_minutes"]),
                 0, 0, int(target["id"]), reason, actor_id, now),
            )
            reversal_id = int(cursor.lastrowid)

            payload, snapshot = self._recompute_in_tx(connection, record_id)
            version = int(expected_version) + 1
            now2 = _now()
            connection.execute(
                "UPDATE records SET version=?, updated_by=?, updated_at=? WHERE id=?",
                (version, actor_id, now2, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "reverse_service", actor_id, version,
                 json.dumps({"summary": "服务记录已冲销", "credential": credential,
                             "original_entry_id": int(target["id"]), "reversal_entry_id": reversal_id,
                             "minutes": int(target["minutes"]), "reason": reason,
                             "plan_version": int(target["plan_version"])}, ensure_ascii=False, sort_keys=True), now2),
            )
            reversal_row = connection.execute("SELECT * FROM service_entries WHERE id=?", (reversal_id,)).fetchone()
            target = connection.execute("SELECT * FROM service_entries WHERE id=?", (target["id"],)).fetchone()
            target_view = self._entry_view(target, connection)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        response = self._row(result)
        response["reversal"] = dict(reversal_row)
        response["entry"] = target_view
        response["ledger"] = snapshot
        response["idempotent_replay"] = False
        return response

    # ---- 计划版本变更（新授权只约束新流水） --------------------------------------
    def amend_plan(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        new_authorized_minutes: Optional[int],
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            current = connection.execute(
                "SELECT * FROM plan_versions WHERE record_id=? ORDER BY plan_version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            new_plan_version = int(current["plan_version"])
            version = int(expected_version) + 1
            if new_authorized_minutes is not None and int(new_authorized_minutes) != int(current["authorized_minutes"]):
                new_plan_version = int(current["plan_version"]) + 1
                connection.execute(
                    "INSERT INTO plan_versions(record_id,plan_version,authorized_minutes,opening_minutes,change_reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, new_plan_version, int(new_authorized_minutes), 0,
                     str(payload.get("amendment_reason", "")), actor_id, now),
                )
                payload["plan_version"] = new_plan_version
                payload["service_minutes"] = int(new_authorized_minutes)

            payload, snapshot = self._recompute_in_tx(connection, record_id)
            now2 = _now()
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now2, record_id),
            )
            audit_details = dict(details)
            audit_details["plan_version_before"] = int(current["plan_version"])
            audit_details["plan_version_after"] = new_plan_version
            if new_plan_version != int(current["plan_version"]):
                audit_details["authorized_minutes"] = int(new_authorized_minutes)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "plan_version" if new_plan_version != int(current["plan_version"]) else "amend",
                 actor_id, version, json.dumps(audit_details, ensure_ascii=False, sort_keys=True), now2),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        response = self._row(result)
        response["ledger"] = snapshot
        response["new_plan_version"] = new_plan_version
        response["authorization_changed"] = new_plan_version != int(current["plan_version"])
        return response

    # ---- 复查/结案：待处理清零闸门 ---------------------------------------------
    def mutate_gated(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        """复查/结案必须在同一事务内确认待处理分钟已清零，防止并发抢入新流水。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            version_rows, registrations, reversals, openings = self._load_ledger(connection, record_id)
            versions = {int(v["plan_version"]): int(v["authorized_minutes"]) for v in version_rows}
            allocation = allocate_ledger(versions, registrations, set(reversals), openings)
            pending_total = sum(pending for effective, pending in allocation.values())
            if pending_total > 0:
                connection.rollback()
                label = GATE_LABELS.get(action, "操作")
                raise Conflict("%s需先清零待处理记录：仍有%s分钟待处理" % (label, pending_total))

            version = int(expected_version) + 1
            payload, snapshot = self._recompute_in_tx(connection, record_id)
            now2 = _now()
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now2, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now2),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        response = self._row(result)
        response["ledger"] = snapshot
        return response

    # ---- 审计 ----------------------------------------------------------------
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
