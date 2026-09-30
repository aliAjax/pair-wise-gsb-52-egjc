import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}
SERVICE_A = {'credential': 'SV-2026-0001', 'service_date': '2026-09-10', 'minutes': 60, 'provider': 'SP-3'}
FLOW = [('consent', 'parent_rep', {'guardian_confirmed': True, 'consent_scope': '个别化服务'}, 'consented'), ('activate', 'case_manager', {}, 'active'), ('review', 'administrator', {'progress_note': '阶段复盘'}, 'under_review'), ('amend', 'case_manager', {'amendment_reason': '调整目标', 'updated_goals': ['目标A', '目标B']}, 'active'), ('close', 'administrator', {'review_complete': True}, 'closed')]


def register(service, actor, record_id, data):
    result = service.act(actor, record_id, None, 'log_service', data)
    return result['record']


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "case_manager"), "IEP-28001", CREATE_DATA)
        self.assertEqual(record["state"], "draft")
        for action, role, data, expected_state in FLOW:
            if action == 'review':
                # 复查前登记一笔服务并冲销，保证待处理区清零
                register(self.service, Actor("operator", "specialist"), record["id"], SERVICE_A)
                self.service.act(Actor("operator", "specialist"), record["id"], None,
                                 'void_service', {'credential': SERVICE_A['credential'], 'reason': '测试冲销'})
            record = self.service.get_record(Actor("operator", role), record["id"])
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "case_manager"), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("log_service", actions)
        self.assertIn("void_service", actions)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])
