"""特殊教育支持计划合规领域规则、服务流水账与计划版本授权。

服务记录采用只追加的流水账模型：
- 每笔登记带服务凭据（credential）、日期、分钟、提供者；凭据在计划内唯一，重复提交只返回原结果。
- 每笔登记挂在“登记当时”的计划版本上，只消耗该版本的授权分钟；超出部分进入待处理区并写明缺口。
- 计划版本变更后，旧流水始终按原版本授权重算，新授权只约束新版本下的新流水。
- 错报不得修改原流水，只能追加一笔带原因的冲销，再用新凭据重新登记。
"""
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, integer, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {
    'consent': {'parent_rep'},
    'activate': {'case_manager'},
    'log_service': {'case_manager', 'specialist'},
    'reverse_service': {'case_manager', 'specialist'},
    'review': {'administrator'},
    'amend': {'case_manager'},
    'close': {'administrator'},
}
TRANSITIONS = {
    'consent': {'draft': 'consented'},
    'activate': {'consented': 'active'},
    'log_service': {'active': 'active'},
    'reverse_service': {'active': 'active'},
    'review': {'active': 'under_review'},
    'amend': {'under_review': 'active'},
    'close': {'active': 'closed', 'under_review': 'closed'},
}
GATE_LABELS = {'review': '复查', 'close': '结案'}


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

    # ---- 计划创建 ----------------------------------------------------------
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
        authorized = int(p["service_minutes"])
        delivered = int(p["delivered_minutes"])
        p["plan_version"] = 1
        p["total_authorized_minutes"] = authorized
        p["missing_minutes"] = max(0, authorized - delivered)
        p["pending_minutes"] = 0
        p["reversed_minutes"] = 0
        p["compliance_rate"] = round(delivered / authorized * 100, 2)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    # ---- 状态转换 ----------------------------------------------------------
    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

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
            changes["updated_goals"] = text_list(data, "updated_goals", 1)
            changes["goals_count"] = len(changes["updated_goals"])
            changes["plan_status"] = "active"
            summary = "计划已修订"
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 流水账输入校验 ------------------------------------------------------
    @staticmethod
    def parse_service_date(value: str) -> str:
        try:
            day = datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError) as exc:
            raise ValidationError("service_date必须是YYYY-MM-DD格式的日期") from exc
        if day > date.today():
            raise ValidationError("服务日期不能晚于今天")
        return value

    def validate_service_entry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        credential = text(data, "credential")
        if len(credential) > 128:
            raise ValidationError("credential长度不能超过128")
        return {
            "credential": credential,
            "service_date": self.parse_service_date(text(data, "service_date")),
            "minutes": integer(data, "session_minutes", 1),
            "provider": text(data, "provider"),
        }

    def validate_reversal(self, payload: Dict[str, Any]) -> Tuple[str, str]:
        data = dict(payload or {})
        credential = text(data, "credential")
        reason = text(data, "reason")
        if len(reason) > 500:
            raise ValidationError("reason长度不能超过500")
        return credential, reason

    def amend_authorization(self, data: Dict[str, Any]) -> Optional[int]:
        """修订时可选的新授权分钟；不传表示沿用当前计划版本。"""
        if data is None or "service_minutes" not in data or data.get("service_minutes") is None:
            return None
        return integer(data, "service_minutes", 1)


# ---- 纯计算：流水账授权分配与重算 -------------------------------------------
def allocate_ledger(
    versions: Dict[int, int],
    entries: List[Dict[str, Any]],
    reversed_ids: set,
    openings: Dict[int, int],
) -> Dict[int, Tuple[int, int]]:
    """按计划版本分别分配授权池。

    每个版本独立记账：期初分钟 + 本版本未冲销登记的有效分钟不得超过该版本授权。
    按登记先后（seq）占用授权，占满后其余分钟落入待处理区。任何登记被冲销后，
    释放出的授权会按顺序让给后续待处理流水——即“按原版本重算”。
    返回 {entry_id: (effective_minutes, pending_minutes)}。
    """
    used = defaultdict(int)
    for plan_version in versions:
        used[plan_version] = int(openings.get(plan_version, 0))
    allocation: Dict[int, Tuple[int, int]] = {}
    for entry in sorted(entries, key=lambda item: item["seq"]):
        entry_id = int(entry["id"])
        if entry_id in reversed_ids:
            allocation[entry_id] = (0, 0)
            continue
        plan_version = int(entry["plan_version"])
        room = max(0, int(versions[plan_version]) - used[plan_version])
        effective = min(int(entry["minutes"]), room)
        pending = int(entry["minutes"]) - effective
        allocation[entry_id] = (effective, pending)
        used[plan_version] += effective
    return allocation


def build_ledger_summary(
    version_rows: List[Dict[str, Any]],
    registrations: List[Dict[str, Any]],
    reversals: Dict[int, Dict[str, Any]],
    openings: Dict[int, int],
    allocation: Dict[int, Tuple[int, int]],
) -> Dict[str, Any]:
    """把不可变流水事实汇总为版本授权表、逐笔视图、待处理缺口和三档分钟合计。"""
    per_version: Dict[int, Dict[str, Any]] = {}
    for row in sorted(version_rows, key=lambda item: item["plan_version"]):
        plan_version = int(row["plan_version"])
        per_version[plan_version] = {
            "plan_version": plan_version,
            "authorized_minutes": int(row["authorized_minutes"]),
            "opening_minutes": int(openings.get(plan_version, 0)),
            "effective_minutes": 0,
            "pending_minutes": 0,
            "gap_minutes": 0,
        }

    entry_views: List[Dict[str, Any]] = []
    for entry in sorted(registrations, key=lambda item: item["seq"]):
        entry_id = int(entry["id"])
        plan_version = int(entry["plan_version"])
        reversal = reversals.get(entry_id)
        effective, pending = allocation[entry_id]
        if reversal is None:
            per_version[plan_version]["effective_minutes"] += effective
            per_version[plan_version]["pending_minutes"] += pending
        entry_views.append({
            "id": entry_id,
            "seq": int(entry["seq"]),
            "type": "registration",
            "credential": entry["credential"],
            "service_date": entry["service_date"],
            "provider": entry["provider"],
            "minutes": int(entry["minutes"]),
            "plan_version": plan_version,
            "authorized_minutes": int(entry["authorized_minutes"]),
            "effective_minutes": effective,
            "pending_minutes": pending,
            "status": "reversed" if reversal is not None else "posted",
            "reversal_reason": reversal["reversal_reason"] if reversal is not None else None,
            "reversed_by": reversal["created_by"] if reversal is not None else None,
            "reversed_at": reversal["created_at"] if reversal is not None else None,
            "created_by": entry["created_by"],
            "created_at": entry["created_at"],
        })

    for reversal in sorted(reversals.values(), key=lambda item: item["seq"]):
        entry_views.append({
            "id": int(reversal["id"]),
            "seq": int(reversal["seq"]),
            "type": "reversal",
            "reverses_id": int(reversal["reverses_id"]),
            "reversal_reason": reversal["reversal_reason"],
            "minutes": int(reversal["minutes"]),
            "plan_version": reversal["plan_version"],
            "created_by": reversal["created_by"],
            "created_at": reversal["created_at"],
        })
    entry_views.sort(key=lambda item: item["seq"])

    pending_items: List[Dict[str, Any]] = []
    for view in entry_views:
        if view["type"] != "registration" or view["status"] == "reversed" or view["pending_minutes"] <= 0:
            continue
        version_info = per_version[view["plan_version"]]
        pending_items.append({
            "credential": view["credential"],
            "service_date": view["service_date"],
            "provider": view["provider"],
            "minutes": view["minutes"],
            "effective_minutes": view["effective_minutes"],
            "pending_minutes": view["pending_minutes"],
            "plan_version": view["plan_version"],
            "authorized_minutes": version_info["authorized_minutes"],
            "gap_minutes": view["pending_minutes"],
            "gap_note": "超出第%s版计划授权（%s分钟），缺口%s分钟，停留在待处理区" % (
                view["plan_version"], version_info["authorized_minutes"], view["pending_minutes"]
            ),
        })

    totals = {
        "plan_version": max(per_version) if per_version else 1,
        "authorized_minutes": sum(item["authorized_minutes"] for item in per_version.values()),
        "opening_minutes": sum(item["opening_minutes"] for item in per_version.values()),
        "effective_minutes": sum(item["effective_minutes"] for item in per_version.values()),
        "pending_minutes": sum(item["pending_minutes"] for item in per_version.values()),
        "reversed_minutes": sum(
            int(item["minutes"]) for item in registrations if int(item["id"]) in reversals
        ),
        "registered_minutes": sum(int(item["minutes"]) for item in registrations),
        "pending_count": len(pending_items),
    }
    for info in per_version.values():
        info["gap_minutes"] = info["pending_minutes"]
    return {
        "versions": [per_version[key] for key in sorted(per_version)],
        "entries": entry_views,
        "pending": pending_items,
        "totals": totals,
    }


def apply_ledger_totals(payload: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """把流水账汇总回写到计划展示字段（派生缓存，事实以流水表为准）。"""
    totals = snapshot["totals"]
    current = next(
        (item for item in snapshot["versions"] if item["plan_version"] == totals["plan_version"]),
        {"authorized_minutes": 0, "opening_minutes": 0, "effective_minutes": 0},
    )
    delivered = totals["opening_minutes"] + totals["effective_minutes"]
    authorized = totals["authorized_minutes"]
    missing = sum(
        max(0, item["authorized_minutes"] - item["opening_minutes"] - item["effective_minutes"])
        for item in snapshot["versions"]
    )
    payload["plan_version"] = totals["plan_version"]
    payload["service_minutes"] = current["authorized_minutes"]
    payload["total_authorized_minutes"] = authorized
    payload["delivered_minutes"] = delivered
    payload["missing_minutes"] = missing
    payload["pending_minutes"] = totals["pending_minutes"]
    payload["pending_count"] = totals["pending_count"]
    payload["reversed_minutes"] = totals["reversed_minutes"]
    payload["compliance_rate"] = round(delivered / authorized * 100, 2) if authorized else 0.0
    return payload
