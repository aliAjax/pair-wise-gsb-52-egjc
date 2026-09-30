"""特殊教育支持计划合规领域规则与状态转换。"""
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {'consent': {'parent_rep'}, 'activate': {'case_manager'}, 'log_service': {'case_manager', 'specialist'}, 'void_service': {'case_manager', 'specialist'}, 'review': {'administrator'}, 'amend': {'case_manager'}, 'close': {'administrator'}}
TRANSITIONS = {'consent': {'draft': 'consented'}, 'activate': {'consented': 'active'}, 'log_service': {'active': 'active'}, 'void_service': {'active': 'active', 'under_review': 'under_review'}, 'review': {'active': 'under_review'}, 'amend': {'under_review': 'active'}, 'close': {'active': 'closed', 'under_review': 'closed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        integer(p, "review_due_days", 0)
        integer(p, "goals_count", 1)
        boolean(p, "consent")
        if p["delivered_minutes"] > p["service_minutes"]:
            raise ValidationError("已提供服务不能超过计划服务")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
        p["compliance_rate"] = round(int(p["delivered_minutes"]) / int(p["service_minutes"]) * 100, 2)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        # 期初已交付分钟是v1授权的不可变基线，之后所有变化都走服务流水
        p["initial_delivered_minutes"] = int(p["delivered_minutes"])
        p["effective_minutes"] = int(p["delivered_minutes"])
        p["pending_minutes"] = 0
        p["voided_minutes"] = 0
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def validate_service_entry(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """登记一笔服务流水的输入校验：凭据、日期、分钟、提供者缺一不可。"""
        data = data or {}
        credential = text(data, "credential")
        service_date = text(data, "service_date")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", service_date):
            raise ValidationError("service_date必须是YYYY-MM-DD格式")
        try:
            datetime.strptime(service_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationError("service_date必须是有效日期") from exc
        minutes = integer(data, "minutes", 1)
        provider = text(data, "provider")
        return {"credential": credential, "service_date": service_date, "minutes": minutes, "provider": provider}

    def validate_void(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = data or {}
        return {"credential": text(data, "credential"), "reason": text(data, "reason")}

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "consent":
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            summary = "监护人同意已记录"
        elif action == "activate":
            if not p.get("consent"):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            summary = "支持计划生效"
        elif action == "review":
            changes["progress_note"] = text(data, "progress_note")
            changes["review_overdue"] = False
            summary = "进入计划复查"
        elif action == "amend":
            changes["amendment_reason"] = text(data, "amendment_reason")
            if "updated_goals" in data and data.get("updated_goals") is not None:
                changes["updated_goals"] = text_list(data, "updated_goals", 1)
                changes["goals_count"] = len(changes["updated_goals"])
            if "new_service_minutes" in data and data.get("new_service_minutes") is not None:
                changes["service_minutes"] = integer(data, "new_service_minutes", 1)
            changes["plan_status"] = "active"
            summary = "计划已修订"
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    @staticmethod
    def settle(base_minutes: int, versions: List[Dict[str, Any]], entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """按计划版本重算流水。

        每个授权版本独立成池，流水按登记顺序入池，池满后超出部分留在待处理区。
        旧流水始终按其登记时锁定的版本授权重算，新授权版本只接收新流水。
        冲销不删除原流水，只把它移出有效/待处理合计并释放池内空间。
        """
        auth_by_version = {int(v["version_no"]): int(v["authorized_minutes"]) for v in versions}
        if not auth_by_version:
            raise Conflict("计划缺少授权版本")
        first_no = min(auth_by_version)
        used = {first_no: int(base_minutes)}
        gap_by_version: Dict[int, int] = {}
        items: List[Dict[str, Any]] = []
        total_voided = 0
        for raw in sorted(entries, key=lambda e: int(e["id"])):
            pv = int(raw["plan_version"])
            minutes = int(raw["minutes"])
            is_void = bool(raw.get("voided"))
            if is_void:
                effective = 0
                pending = 0
                total_voided += minutes
            else:
                cap = auth_by_version.get(pv, 0)
                occupied = used.get(pv, int(base_minutes) if pv == first_no else 0)
                room = max(0, cap - occupied)
                effective = min(minutes, room)
                pending = minutes - effective
                used[pv] = occupied + effective
                if pending:
                    gap_by_version[pv] = gap_by_version.get(pv, 0) + pending
            if is_void:
                status = "voided"
            elif effective == 0:
                status = "pending"
            elif pending:
                status = "partial"
            else:
                status = "effective"
            items.append({
                "id": int(raw["id"]),
                "credential": raw["credential"],
                "service_date": raw["service_date"],
                "minutes": minutes,
                "provider": raw["provider"],
                "plan_version": pv,
                "registered_by": raw.get("created_by", ""),
                "registered_at": raw.get("created_at", ""),
                "effective_minutes": effective,
                "pending_minutes": pending,
                "gap_minutes": pending,
                "status": status,
                "void_reason": raw.get("void_reason") or "",
                "voided_by": raw.get("voided_by") or "",
                "voided_at": raw.get("voided_at") or "",
            })
        current_no = max(auth_by_version)
        current_auth = auth_by_version[current_no]
        current_used = used.get(current_no, 0)
        total_effective = int(base_minutes) + sum(int(i["effective_minutes"]) for i in items)
        total_pending = sum(int(i["pending_minutes"]) for i in items)
        totals = {
            "effective_minutes": total_effective,
            "pending_minutes": total_pending,
            "voided_minutes": total_voided,
            "gap_minutes": total_pending,
            "pending_count": sum(1 for i in items if i["pending_minutes"] > 0),
            "current_version": current_no,
            "current_authorized_minutes": current_auth,
            "current_used_minutes": current_used,
            "current_remaining_minutes": max(0, current_auth - current_used),
            "gap_by_version": {str(k): v for k, v in sorted(gap_by_version.items())},
        }
        return items, totals

    @staticmethod
    def payload_with_totals(payload: Dict[str, Any], totals: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p["delivered_minutes"] = totals["effective_minutes"]
        p["effective_minutes"] = totals["effective_minutes"]
        p["pending_minutes"] = totals["pending_minutes"]
        p["voided_minutes"] = totals["voided_minutes"]
        p["missing_minutes"] = totals["current_remaining_minutes"]
        p["compliance_rate"] = round(totals["effective_minutes"] / totals["current_authorized_minutes"] * 100, 2)
        return p
