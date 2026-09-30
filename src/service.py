"""业务用例编排、权限检查与审计。

服务流水相关动作为：
- log_service：凭据幂等登记，按登记当时计划版本占用授权，超额入待处理区。
- reverse_service：错报只能追加冲销原因，原流水保持不变。
- amend：计划版本变更（新授权分钟）只约束新流水，旧流水按原版本重算。
- review/close：必须待处理分钟清零方可执行。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


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

    def ledger(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        snapshot = self.repository.ledger_snapshot(record_id)
        return {"record": record, "ledger": snapshot}

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValidationError("expected_version必须是整数")
        if action == "log_service":
            return self._log_service(actor, record_id, expected_version, data or {})
        if action == "reverse_service":
            return self._reverse_service(actor, record_id, expected_version, data or {})

        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}

        if action == "amend":
            new_authorized_minutes = self.rules.amend_authorization(data or {})
            return self.repository.amend_plan(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                new_authorized_minutes=new_authorized_minutes,
                details=details,
            )

        if action in {"review", "close"}:
            return self.repository.mutate_gated(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details=details,
            )

        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    def _log_service(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        self.rules.require_transition(record, "log_service")
        entry = self.rules.validate_service_entry(data)
        return self.repository.post_service(
            record_id=record_id,
            expected_version=int(expected_version),
            entry=entry,
            actor_id=actor.user_id,
        )

    def _reverse_service(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        self.rules.require_transition(record, "reverse_service")
        credential, reason = self.rules.validate_reversal(data)
        return self.repository.reverse_service(
            record_id=record_id,
            expected_version=int(expected_version),
            credential=credential,
            reason=reason,
            actor_id=actor.user_id,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
