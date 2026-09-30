import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules, allocate_ledger, build_ledger_summary


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}
FLOW = [
    ('consent', 'parent_rep', {'guardian_confirmed': True, 'consent_scope': '个别化服务'}, 'consented'),
    ('activate', 'case_manager', {}, 'active'),
    ('log_service', 'specialist', {'credential': 'CRED-001', 'service_date': '2026-09-01', 'session_minutes': 60, 'provider': 'SP-3'}, 'active'),
    ('review', 'administrator', {'progress_note': '阶段复盘'}, 'under_review'),
    ('amend', 'case_manager', {'amendment_reason': '调整目标', 'updated_goals': ['目标A', '目标B']}, 'active'),
    ('close', 'administrator', {'review_complete': True}, 'closed'),
]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["missing_minutes"], 480)
        self.assertEqual(prepared["compliance_rate"], 20.0)
        self.assertFalse(prepared["review_overdue"])

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertTrue(payload["consent"])

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["goals_count"] = 0
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

    def test_service_entry_requires_credential_and_date(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_service_entry({'service_date': '2026-09-01', 'session_minutes': 30, 'provider': 'P'})
        with self.assertRaises(ValidationError):
            self.rules.validate_service_entry({'credential': 'C', 'service_date': 'not-a-date', 'session_minutes': 30, 'provider': 'P'})

    def test_allocate_splits_over_authorization_into_pending(self):
        versions = {1: 100}
        openings = {1: 0}
        entries = [
            {'id': 1, 'seq': 1, 'plan_version': 1, 'minutes': 80},
            {'id': 2, 'seq': 2, 'plan_version': 1, 'minutes': 30},
        ]
        allocation = allocate_ledger(versions, entries, set(), openings)
        self.assertEqual(allocation[1], (80, 0))
        self.assertEqual(allocation[2], (20, 10))

    def test_old_entries_recalculated_against_old_version_after_change(self):
        # 旧版本授权100：80有效 + 第二笔20有效10待处理；新版本授权30只约束新流水。
        version_rows = [
            {'plan_version': 1, 'authorized_minutes': 100},
            {'plan_version': 2, 'authorized_minutes': 30},
        ]
        registrations = [
            {'id': 1, 'seq': 1, 'plan_version': 1, 'minutes': 80, 'credential': 'A', 'service_date': '2026-09-01', 'provider': 'P', 'authorized_minutes': 100, 'created_by': 'u', 'created_at': 't1'},
            {'id': 2, 'seq': 2, 'plan_version': 1, 'minutes': 30, 'credential': 'B', 'service_date': '2026-09-02', 'provider': 'P', 'authorized_minutes': 100, 'created_by': 'u', 'created_at': 't2'},
            {'id': 3, 'seq': 3, 'plan_version': 2, 'minutes': 30, 'credential': 'C', 'service_date': '2026-09-03', 'provider': 'P', 'authorized_minutes': 30, 'created_by': 'u', 'created_at': 't3'},
        ]
        allocation = allocate_ledger({1: 100, 2: 30}, registrations, set(), {1: 0, 2: 0})
        self.assertEqual(allocation[1], (80, 0))
        self.assertEqual(allocation[2], (20, 10))
        self.assertEqual(allocation[3], (30, 0))

        # 冲销第一笔后，旧版本腾出80分钟，第二笔的待处理10按原版本顺序被吸收。
        allocation = allocate_ledger({1: 100, 2: 30}, registrations, {1}, {1: 0, 2: 0})
        self.assertEqual(allocation[1], (0, 0))
        self.assertEqual(allocation[2], (30, 0))
        self.assertEqual(allocation[3], (30, 0))
        snapshot = build_ledger_summary(
            version_rows, registrations,
            {1: {'id': 9, 'seq': 4, 'reverses_id': 1, 'reversal_reason': '错报', 'minutes': 80, 'plan_version': 1, 'created_by': 'u', 'created_at': 't4'}},
            {1: 0, 2: 0}, allocation,
        )
        self.assertEqual(snapshot['totals']['effective_minutes'], 60)
        self.assertEqual(snapshot['totals']['pending_minutes'], 0)
        self.assertEqual(snapshot['totals']['reversed_minutes'], 80)
