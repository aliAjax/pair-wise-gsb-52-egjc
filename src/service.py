"""业务用例编排、权限检查、服务流水账与审计。"""
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository, Transaction
from .rules import DomainRules


IDEMPOTENT_ACTIONS = {"log_service", "void_service"}
LEDGER_ROUTE = {"log_service", "void_service"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def _settle(self, tx: Transaction, record_id: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        record = tx.get_record(record_id)
        base = int(record["payload"].get("initial_delivered_minutes", 0))
        return self.rules.settle(base, tx.versions(record_id), tx.entries(record_id))

    def _replay(self, tx: Transaction, record_id: int, entry_id: int, duplicate: bool) -> Dict[str, Any]:
        record = tx.get_record(record_id)
        items, totals = self._settle(tx, record_id)
        record = dict(record)
        record["payload"] = self.rules.payload_with_totals(record["payload"], totals)
        entry = next(item for item in items if item["id"] == int(entry_id))
        return {"record": record, "entry": entry, "entries": items, "totals": totals, "duplicate": duplicate}

    def ledger(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        """只读视角：按凭据还原流水、版本授权与有效/待处理/已冲销合计。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with self.repository.transaction() as tx:
            record = tx.get_record(record_id)
            items, totals = self._settle(tx, record_id)
            view = dict(record)
            view["payload"] = self.rules.payload_with_totals(record["payload"], totals)
            versions = tx.versions(record_id)
        return {"record": view, "versions": versions, "entries": items, "totals": totals}

    def plan_versions(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.plan_versions(record_id)

    def service_entries(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.service_entries(record_id)

    def _register(self, tx: Transaction, actor_id: str, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        entry_input = self.rules.validate_service_entry(data)
        existing = tx.find_entry_by_credential(entry_input["credential"])
        if existing is not None:
            # 断网重送/并发重送：凭据已存在，原结果原样返回，不再计费
            return self._replay(tx, int(existing["record_id"]), int(existing["id"]), True)
        record = tx.get_record(record_id)
        self.rules.require_transition(record, "log_service")
        versions = tx.versions(record_id)
        plan_version = max(int(v["version_no"]) for v in versions)
        entry_id = tx.insert_entry(
            record_id,
            entry_input["credential"],
            entry_input["service_date"],
            entry_input["minutes"],
            entry_input["provider"],
            plan_version,
            actor_id,
        )
        items, totals = self._settle(tx, record_id)
        item = next(i for i in items if i["id"] == entry_id)
        saved = tx.save_record(
            record_id,
            record["state"],
            self.rules.payload_with_totals(record["payload"], totals),
            actor_id,
            "log_service",
            {
                "summary": "服务流水已登记" if item["pending_minutes"] == 0 else "服务流水已登记，部分分钟进入待处理区",
                "credential": entry_input["credential"],
                "service_date": entry_input["service_date"],
                "minutes": entry_input["minutes"],
                "provider": entry_input["provider"],
                "plan_version": plan_version,
                "effective_minutes": item["effective_minutes"],
                "pending_minutes": item["pending_minutes"],
                "gap_minutes": item["gap_minutes"],
                "duplicate": False,
            },
        )
        result = self._replay(tx, record_id, entry_id, False)
        result["record"] = saved
        return result

    def _void(self, tx: Transaction, actor_id: str, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        void_input = self.rules.validate_void(data)
        existing = tx.find_entry_by_credential(void_input["credential"])
        if existing is None:
            raise ValidationError("凭据不存在，无法冲销")
        entry_id = int(existing["id"])
        if int(existing["record_id"]) != int(record_id):
            raise ValidationError("凭据不属于该支持计划")
        record = tx.get_record(record_id)
        self.rules.require_transition(record, "void_service")
        prior_effective = max(0, int(existing["minutes"])) if not int(existing["voided"]) else 0
        already = tx.find_void(entry_id)
        if already is not None:
            return self._replay(tx, record_id, entry_id, True)
        tx.void_entry(entry_id, void_input["reason"], actor_id)
        items, totals = self._settle(tx, record_id)
        tx.save_record(
            record_id,
            record["state"],
            self.rules.payload_with_totals(record["payload"], totals),
            actor_id,
            "void_service",
            {
                "summary": "服务流水已冲销，原流水保留",
                "credential": void_input["credential"],
                "entry_id": entry_id,
                "reason": void_input["reason"],
                "released_minutes": prior_effective,
                "pending_minutes": totals["pending_minutes"],
            },
        )
        return self._replay(tx, record_id, entry_id, False)

    def _generic_action(self, tx: Transaction, actor_id: str, record_id: int, expected_version: Optional[int], action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        record = tx.get_record(record_id)
        if expected_version is not None and int(record["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details: Dict[str, Any] = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if action == "amend" and int(new_payload.get("service_minutes", -1)) != int(record["payload"].get("service_minutes", -2)):
            version_no = self._bump_version(tx, record_id, int(new_payload["service_minutes"]), data.get("amendment_reason", "计划修订"), actor_id)
            details["plan_version"] = version_no
            details["authorized_minutes"] = int(new_payload["service_minutes"])
            items, totals = self._settle(tx, record_id)
            new_payload = self.rules.payload_with_totals(new_payload, totals)
        if action in {"review", "close"}:
            items, totals = self._settle(tx, record_id)
            if totals["pending_minutes"] > 0:
                raise Conflict("待处理区尚有%s分钟未清零（缺口按版本：%s），不能%s"
                               % (totals["pending_minutes"], totals["gap_by_version"], "复查" if action == "review" else "结案"))
        return tx.save_record(record_id, new_state, new_payload, actor_id, action, details)

    @staticmethod
    def _bump_version(tx: Transaction, record_id: int, authorized_minutes: int, reason: str, actor_id: str) -> int:
        version_no = tx.latest_version_no(record_id) + 1
        tx.insert_version(record_id, version_no, authorized_minutes, reason, actor_id)
        return version_no

    def act(self, actor: Actor, record_id: int, expected_version: Optional[int], action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if action == "log_service":
            return self._with_idempotent_retry(self._register, actor.user_id, record_id, data or {})
        if action == "void_service":
            return self._with_idempotent_retry(self._void, actor.user_id, record_id, data or {})

        with self.repository.transaction() as tx:
            return self._generic_action(tx, actor.user_id, record_id, expected_version, action, data or {})

    def _with_idempotent_retry(self, handler, actor_id: str, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        attempts = 0
        while True:
            try:
                with self.repository.transaction() as tx:
                    return handler(tx, actor_id, record_id, data)
            except sqlite3.IntegrityError as exc:
                # 并发提交同一凭据：一个事务插入成功，另一个撞唯一键后重试，重试必走原结果分支
                if "service_entries.credential" in str(exc) or "service_voids.entry_id" in str(exc):
                    attempts += 1
                    if attempts >= 5:
                        raise Conflict("并发提交冲突，请稍后重试") from exc
                    continue
                raise

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
