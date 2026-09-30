"""服务流水账：凭据幂等、授权拆分、版本隔离、冲销、闸门与并发。"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'student_id': 'S-200', 'disability': 'hearing', 'service_minutes': 100,
               'delivered_minutes': 0, 'review_due_days': 15, 'goals_count': 3, 'consent': False}


def service_entry(credential, minutes, provider='SP-1', date='2026-09-10'):
    return {'credential': credential, 'service_date': date, 'minutes': minutes, 'provider': provider}


def activate(service):
    record = service.create(Actor('cm', 'case_manager'), 'IEP-29001', CREATE_DATA)
    record = service.act(Actor('parent', 'parent_rep'), record['id'], record['version'],
                         'consent', {'guardian_confirmed': True, 'consent_scope': '语言治疗'})
    record = service.act(Actor('cm', 'case_manager'), record['id'], record['version'], 'activate', {})
    return record


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = activate(self.service)
        self.rid = self.record['id']

    def tearDown(self):
        self.temp.cleanup()

    def log(self, data, role='specialist', actor='sp-1'):
        return self.service.act(Actor(actor, role), self.rid, None, 'log_service', data)

    def void(self, credential, reason='错报', actor='cm'):
        return self.service.act(Actor(actor, 'case_manager'), self.rid, None,
                                'void_service', {'credential': credential, 'reason': reason})

    def test_idempotent_credential_returns_original(self):
        result = self.log(service_entry('C-1', 30, 'SP-1'))
        self.assertFalse(result['duplicate'])
        self.assertEqual(result['entry']['effective_minutes'], 30)
        again = self.log(service_entry('C-1', 999, 'SP-9'))
        self.assertTrue(again['duplicate'])
        # 重送内容不被采用，原结果原样返回
        self.assertEqual(again['entry']['minutes'], 30)
        self.assertEqual(again['entry']['provider'], 'SP-1')
        self.assertEqual(again['totals']['effective_minutes'], 30)
        ledger = self.service.ledger(Actor('cm', 'case_manager'), self.rid)
        self.assertEqual(len(ledger['entries']), 1)

    def test_overrun_splits_with_gap(self):
        self.log(service_entry('C-1', 60))
        result = self.log(service_entry('C-2', 60))
        self.assertEqual(result['entry']['status'], 'partial')
        self.assertEqual(result['entry']['effective_minutes'], 40)
        self.assertEqual(result['entry']['pending_minutes'], 20)
        self.assertEqual(result['entry']['gap_minutes'], 20)
        ledger = self.service.ledger(Actor('cm', 'case_manager'), self.rid)
        self.assertEqual(ledger['totals']['effective_minutes'], 100)
        self.assertEqual(ledger['totals']['pending_minutes'], 20)
        self.assertEqual(ledger['totals']['gap_minutes'], 20)
        self.assertEqual(ledger['totals']['gap_by_version'], {'1': 20})

    def test_version_change_isolates_old_entries(self):
        self.log(service_entry('C-1', 100))
        self.log(service_entry('C-2', 20))  # 待处理20
        # 待处理未清零不能复查，先冲销C-2
        self.void('C-2', '重复登记')
        current = self.service.get_record(Actor('cm', 'case_manager'), self.rid)
        record = self.service.act(Actor('admin-x', 'administrator'), self.rid, current['version'],
                                  'review', {'progress_note': '复查'})
        record = self.service.act(Actor('cm', 'case_manager'), self.rid, record['version'], 'amend',
                                  {'amendment_reason': '新学年授权调整', 'new_service_minutes': 50})
        self.assertEqual(record['payload']['service_minutes'], 50)
        versions = self.service.plan_versions(Actor('cm', 'case_manager'), self.rid)
        self.assertEqual([(v['version_no'], v['authorized_minutes']) for v in versions], [(1, 100), (2, 50)])
        # 新流水按v2授权，只有50
        result = self.log(service_entry('C-3', 60))
        self.assertEqual(result['entry']['plan_version'], 2)
        self.assertEqual(result['entry']['effective_minutes'], 50)
        self.assertEqual(result['entry']['pending_minutes'], 10)
        ledger = self.service.ledger(Actor('cm', 'case_manager'), self.rid)
        # C-1仍锁定v1的100，没有被v2的50压缩
        by_cred = {e['credential']: e for e in ledger['entries']}
        self.assertEqual(by_cred['C-1']['effective_minutes'], 100)
        self.assertEqual(by_cred['C-1']['plan_version'], 1)
        self.assertEqual(ledger['totals']['gap_by_version'], {'2': 10})

    def test_void_keeps_original_and_reregisters(self):
        self.log(service_entry('C-1', 60))
        result = self.void('C-1', '日期错报')
        self.assertTrue(result['entry']['status'] == 'voided')
        self.assertEqual(result['entry']['void_reason'], '日期错报')
        self.assertEqual(result['totals']['voided_minutes'], 60)
        self.assertEqual(result['totals']['effective_minutes'], 0)
        # 原凭据不可复活，重新登记用新凭据
        self.log(service_entry('C-1-FIX', 60, 'SP-1'))
        ledger = self.service.ledger(Actor('cm', 'case_manager'), self.rid)
        self.assertEqual([e['credential'] for e in ledger['entries']], ['C-1', 'C-1-FIX'])
        # 重复冲销返回同一结果
        again = self.void('C-1', '再次冲销')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['entry']['void_reason'], '日期错报')

    def test_void_promotes_later_entry_into_released_room(self):
        self.log(service_entry('C-1', 80))
        self.log(service_entry('C-2', 40))  # 20有效20待处理
        self.void('C-1', '错报')
        ledger = self.service.ledger(Actor('cm', 'case_manager'), self.rid)
        by_cred = {e['credential']: e for e in ledger['entries']}
        self.assertEqual(by_cred['C-2']['effective_minutes'], 40)
        self.assertEqual(by_cred['C-2']['pending_minutes'], 0)
        self.assertEqual(ledger['totals']['effective_minutes'], 40)
        self.assertEqual(ledger['totals']['pending_minutes'], 0)

    def test_review_and_close_blocked_until_pending_clear(self):
        self.log(service_entry('C-1', 60))
        self.log(service_entry('C-2', 60))  # 20待处理
        with self.assertRaises(Conflict):
            self.service.act(Actor('admin-x', 'administrator'), self.rid, self.record['version'],
                             'review', {'progress_note': '复查'})
        # 冲销C-2后待处理清零，v1占用60，可以复查
        self.void('C-2', '多报')
        current = self.service.get_record(Actor('cm', 'case_manager'), self.rid)
        review = self.service.act(Actor('admin-x', 'administrator'), self.rid, current['version'],
                                  'review', {'progress_note': '复查'})
        # under_review 状态不能结案，需先amend回active
        review = self.service.act(Actor('cm', 'case_manager'), self.rid, review['version'], 'amend',
                                  {'amendment_reason': '目标微调'})
        closed = self.service.act(Actor('admin-x', 'administrator'), self.rid, review['version'],
                                  'close', {'review_complete': True})
        self.assertEqual(closed['state'], 'closed')

    def test_concurrent_same_credential_counts_once(self):
        barrier = threading.Barrier(3)
        results = [None, None]

        def submit(idx):
            barrier.wait()
            results[idx] = self.service.act(Actor('sp-%d' % idx, 'specialist'), self.rid, None,
                                            'log_service', service_entry('C-RACE', 30))

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        ledger = self.service.ledger(Actor('cm', 'case_manager'), self.rid)
        self.assertEqual(len(ledger['entries']), 1)
        self.assertEqual(ledger['totals']['effective_minutes'], 30)
        self.assertEqual(ledger['totals']['pending_minutes'], 0)
        dup_flags = sorted(bool(r['duplicate']) for r in results)
        self.assertEqual(dup_flags, [False, True])

    def test_concurrent_different_credentials_split_consistently(self):
        barrier = threading.Barrier(3)
        results = [None, None]

        def submit(idx):
            barrier.wait()
            results[idx] = self.service.act(Actor('sp-%d' % idx, 'specialist'), self.rid, None,
                                            'log_service', service_entry('CC-%d' % idx, 60))

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        ledger = self.service.ledger(Actor('cm', 'case_manager'), self.rid)
        self.assertEqual(len(ledger['entries']), 2)
        self.assertEqual(ledger['totals']['effective_minutes'], 100)
        self.assertEqual(ledger['totals']['pending_minutes'], 20)
        effective = sorted(e['effective_minutes'] for e in ledger['entries'])
        self.assertEqual(effective, [40, 60])

    def test_audit_timeline_restores_by_credential(self):
        self.log(service_entry('C-1', 30, provider='SP-7'))
        self.void('C-1', '凭据错报')
        timeline = self.service.timeline(Actor('cm', 'case_manager'), self.rid)
        log_event = next(e for e in timeline if e['action'] == 'log_service')
        void_event = next(e for e in timeline if e['action'] == 'void_service')
        self.assertEqual(log_event['details']['credential'], 'C-1')
        self.assertEqual(log_event['details']['provider'], 'SP-7')
        self.assertEqual(log_event['details']['plan_version'], 1)
        self.assertEqual(void_event['details']['credential'], 'C-1')
        self.assertEqual(void_event['details']['reason'], '凭据错报')

    def test_void_unknown_credential_rejected(self):
        with self.assertRaises(ValidationError):
            self.void('NOT-EXIST', '原因')

    def test_invalid_entry_rejected(self):
        with self.assertRaises(ValidationError):
            self.log({'credential': 'X', 'service_date': 'bad', 'minutes': 30, 'provider': 'P'})
        with self.assertRaises(ValidationError):
            self.log({'credential': '', 'service_date': '2026-09-01', 'minutes': 30, 'provider': 'P'})
