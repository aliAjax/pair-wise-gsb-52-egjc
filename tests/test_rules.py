import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["missing_minutes"], 480)
        self.assertEqual(prepared["compliance_rate"], 20.0)
        self.assertFalse(prepared["review_overdue"])
        self.assertEqual(prepared["initial_delivered_minutes"], 120)

    def test_consent_action(self):
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, "consent", {'guardian_confirmed': True, 'consent_scope': '个别化服务'})
        self.assertEqual(state, "consented")
        self.assertTrue(payload["consent"])

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["goals_count"] = 0
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

    def test_service_entry_validation(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_service_entry({'credential': 'X', 'service_date': '2026-9-1', 'minutes': 30, 'provider': 'SP-1'})
        with self.assertRaises(ValidationError):
            self.rules.validate_service_entry({'credential': 'X', 'service_date': '2026-09-01', 'minutes': 0, 'provider': 'SP-1'})
        valid = self.rules.validate_service_entry({'credential': 'X', 'service_date': '2026-09-01', 'minutes': 30, 'provider': 'SP-1'})
        self.assertEqual(valid["minutes"], 30)

    def test_settle_splits_overrun_and_isolates_versions(self):
        versions = [
            {'version_no': 1, 'authorized_minutes': 100},
            {'version_no': 2, 'authorized_minutes': 50},
        ]
        entries = [
            {'id': 1, 'credential': 'a', 'service_date': '2026-09-01', 'minutes': 60, 'provider': 'P1', 'plan_version': 1, 'voided': False},
            {'id': 2, 'credential': 'b', 'service_date': '2026-09-02', 'minutes': 60, 'provider': 'P2', 'plan_version': 1, 'voided': False},
            {'id': 3, 'credential': 'c', 'service_date': '2026-09-03', 'minutes': 40, 'provider': 'P3', 'plan_version': 2, 'voided': False},
        ]
        items, totals = self.rules.settle(0, versions, entries)
        # v1池100：a全入60，b入40留20待处理
        self.assertEqual([(i['credential'], i['effective_minutes'], i['pending_minutes']) for i in items],
                         [('a', 60, 0), ('b', 40, 20), ('c', 40, 0)])
        self.assertEqual(totals['effective_minutes'], 140)
        self.assertEqual(totals['pending_minutes'], 20)
        self.assertEqual(totals['gap_by_version'], {'1': 20})
        self.assertEqual(totals['current_remaining_minutes'], 10)

        # 冲销a：原流水保留为voided，释放60，b前移填满v1
        entries[0]['voided'] = True
        items, totals = self.rules.settle(0, versions, entries)
        self.assertEqual(items[0]['status'], 'voided')
        self.assertEqual([(i['credential'], i['effective_minutes'], i['pending_minutes']) for i in items],
                         [('a', 0, 0), ('b', 60, 0), ('c', 40, 0)])
        self.assertEqual(totals['voided_minutes'], 60)
        self.assertEqual(totals['pending_minutes'], 0)
