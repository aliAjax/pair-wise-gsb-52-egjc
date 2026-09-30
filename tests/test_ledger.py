import threading
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


def make_service():
    temp = tempfile.TemporaryDirectory()
    service = build_service(str(Path(temp.name) / "test.db"))
    return temp, service


def activated_plan(service, minutes=100, delivered=0, reference="IEP-1"):
    data = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': minutes,
            'delivered_minutes': delivered, 'review_due_days': 15, 'goals_count': 2, 'consent': False}
    record = service.create(Actor("cm", "case_manager"), reference, data)
    record = service.act(Actor("parent", "parent_rep"), record["id"], record["version"],
                         "consent", {'guardian_confirmed': True, 'consent_scope': '个别化服务'})
    record = service.act(Actor("cm", "case_manager"), record["id"], record["version"], "activate", {})
    return record


def log(service, record, credential, minutes, date='2026-09-10', provider='SP-1'):
    fresh = service.get_record(Actor(provider, "specialist"), record["id"])
    return service.act(Actor(provider, "specialist"), fresh["id"], fresh["version"],
                       "log_service", {'credential': credential, 'service_date': date,
                                       'session_minutes': minutes, 'provider': provider})


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp, self.service = make_service()
        self.record = activated_plan(self.service, minutes=100)

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self):
        return self.service.get_record(Actor("cm", "case_manager"), self.record["id"])

    def test_duplicate_credential_returns_original_result(self):
        first = log(self.service, self.record, 'CRED-A', 60)
        self.assertEqual(first["entry"]["effective_minutes"], 60)
        self.assertFalse(first["idempotent_replay"])
        version_after_first = first["version"]

        # 断网恢复后重送同一凭据：返回原结果，版本不抬，分钟不翻倍。
        replay = log(self.service, self.record, 'CRED-A', 60)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["entry"]["id"], first["entry"]["id"])
        self.assertEqual(replay["version"], version_after_first)
        self.assertEqual(replay["ledger"]["totals"]["effective_minutes"], 60)
        self.assertEqual(len(replay["ledger"]["entries"]), 1)

    def test_over_authorization_stays_pending_with_gap_note(self):
        log(self.service, self.record, 'CRED-A', 80)
        second = log(self.service, self.record, 'CRED-B', 30)
        self.assertEqual(second["entry"]["effective_minutes"], 20)
        self.assertEqual(second["entry"]["pending_minutes"], 10)
        self.assertEqual(second["entry"]["gap_minutes"], 10)
        totals = second["ledger"]["totals"]
        self.assertEqual(totals["effective_minutes"], 100)
        self.assertEqual(totals["pending_minutes"], 10)
        pending = second["ledger"]["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["credential"], "CRED-B")
        self.assertIn("缺口10分钟", pending[0]["gap_note"])
        self.assertIn("第1版", pending[0]["gap_note"])

    def test_review_and_close_blocked_until_pending_cleared(self):
        log(self.service, self.record, 'CRED-A', 80)
        log(self.service, self.record, 'CRED-B', 30)
        with self.assertRaises(Conflict) as ctx:
            self.service.act(Actor("admin", "administrator"), self.record["id"], self.refresh()["version"],
                             "review", {'progress_note': '复查'})
        self.assertIn("待处理", str(ctx.exception))
        with self.assertRaises(Conflict):
            self.service.act(Actor("admin", "administrator"), self.record["id"], self.refresh()["version"],
                             "close", {'review_complete': True})

        # 冲销超额那笔：缺口清零，复查放行。
        self.service.act(Actor("cm", "case_manager"), self.record["id"], self.refresh()["version"],
                         "reverse_service", {'credential': 'CRED-B', 'reason': '超授权登记，暂缓'})
        reviewed = self.service.act(Actor("admin", "administrator"), self.record["id"], self.refresh()["version"],
                                    "review", {'progress_note': '复查'})
        self.assertEqual(reviewed["state"], "under_review")

    def test_reversal_keeps_original_and_frees_room_in_order(self):
        # 100授权：80 + 30(20有效/10待处理) + 10(全待处理)
        log(self.service, self.record, 'CRED-A', 80)
        log(self.service, self.record, 'CRED-B', 30)
        log(self.service, self.record, 'CRED-C', 10)
        snapshot = self.service.ledger(Actor("cm", "case_manager"), self.record["id"])["ledger"]
        self.assertEqual(snapshot["totals"]["pending_minutes"], 20)

        # 错报不能改原流水：冲销CRED-A，原笔事实保留，只是状态变reversed。
        result = self.service.act(Actor("cm", "case_manager"), self.record["id"], self.refresh()["version"],
                                  "reverse_service", {'credential': 'CRED-A', 'reason': '日期登记错误'})
        views = {e["credential"]: e for e in result["ledger"]["entries"] if e["type"] == "registration"}
        self.assertEqual(views["CRED-A"]["status"], "reversed")
        self.assertEqual(views["CRED-A"]["minutes"], 80)  # 原流水分钟不被改动
        self.assertEqual(views["CRED-A"]["reversal_reason"], "日期登记错误")
        # 释放出的80分钟按顺序吸收后续待处理：B全额30，C全额10。
        self.assertEqual(views["CRED-B"]["effective_minutes"], 30)
        self.assertEqual(views["CRED-B"]["pending_minutes"], 0)
        self.assertEqual(views["CRED-C"]["effective_minutes"], 10)
        self.assertEqual(views["CRED-C"]["pending_minutes"], 0)
        totals = result["ledger"]["totals"]
        self.assertEqual(totals["effective_minutes"], 40)
        self.assertEqual(totals["pending_minutes"], 0)
        self.assertEqual(totals["reversed_minutes"], 80)
        # 冲销本身是一笔流水。
        self.assertTrue(any(e["type"] == "reversal" for e in result["ledger"]["entries"]))

        # 冲销幂等：重复冲销只返回原冲销，不追加。
        replay = self.service.act(Actor("cm", "case_manager"), self.record["id"], self.refresh()["version"],
                                  "reverse_service", {'credential': 'CRED-A', 'reason': '又冲一次'})
        self.assertTrue(replay["idempotent_replay"])
        reversals = [e for e in replay["ledger"]["entries"] if e["type"] == "reversal"]
        self.assertEqual(len(reversals), 1)

        # 冲销不存在的凭据报错，冲销后允许用新凭据重新登记。
        from src.domain import NotFound
        with self.assertRaises(NotFound):
            self.service.act(Actor("cm", "case_manager"), self.record["id"], self.refresh()["version"],
                             "reverse_service", {'credential': 'NOPE', 'reason': 'x'})
        fixed = log(self.service, self.record, 'CRED-A-FIX', 20)
        self.assertEqual(fixed["entry"]["effective_minutes"], 20)

    def test_plan_version_change_only_constrains_new_entries(self):
        log(self.service, self.record, 'OLD-1', 80)
        reviewed = self.service.act(Actor("admin", "administrator"), self.record["id"], self.refresh()["version"],
                                    "review", {'progress_note': '复查'})
        amended = self.service.act(Actor("cm", "case_manager"), reviewed["id"], reviewed["version"], "amend",
                                   {'amendment_reason': '追加授权', 'updated_goals': ['目标A'],
                                    'service_minutes': 130})
        self.assertTrue(amended["authorization_changed"])
        self.assertEqual(amended["new_plan_version"], 2)
        self.record = self.refresh()

        # 新流水挂在第2版，吃第2版授权，不占用旧版本。
        new1 = log(self.service, self.record, 'NEW-1', 130)
        self.assertEqual(new1["entry"]["plan_version"], 2)
        self.assertEqual(new1["entry"]["effective_minutes"], 130)
        new2 = log(self.service, self.record, 'NEW-2', 10)
        self.assertEqual(new2["entry"]["effective_minutes"], 0)
        self.assertEqual(new2["entry"]["pending_minutes"], 10)
        versions = {v["plan_version"]: v for v in new2["ledger"]["versions"]}
        self.assertEqual(versions[1]["authorized_minutes"], 100)
        self.assertEqual(versions[1]["effective_minutes"], 80)
        self.assertEqual(versions[2]["authorized_minutes"], 130)
        self.assertEqual(versions[2]["effective_minutes"], 130)
        self.assertEqual(versions[2]["pending_minutes"], 10)
        totals = new2["ledger"]["totals"]
        self.assertEqual(totals["effective_minutes"], 210)
        self.assertEqual(totals["pending_minutes"], 10)
        self.assertEqual(totals["reversed_minutes"], 0)

        # 冲销旧版本流水，只在旧版本内重算，不影响第2版缺口。
        result = self.service.act(Actor("cm", "case_manager"), self.record["id"], self.refresh()["version"],
                                  "reverse_service", {'credential': 'OLD-1', 'reason': '错报'})
        versions = {v["plan_version"]: v for v in result["ledger"]["versions"]}
        self.assertEqual(versions[1]["effective_minutes"], 0)
        self.assertEqual(versions[2]["pending_minutes"], 10)

    def test_audit_timeline_reconstructs_credential_reversal_and_version(self):
        log(self.service, self.record, 'CRED-A', 80)
        self.service.act(Actor("cm", "case_manager"), self.record["id"], self.refresh()["version"],
                         "reverse_service", {'credential': 'CRED-A', 'reason': '错报'})
        self.service.act(Actor("cm", "case_manager"), self.record["id"], self.refresh()["version"],
                         "log_service", {'credential': 'CRED-B', 'service_date': '2026-09-11',
                                         'session_minutes': 50, 'provider': 'SP-2'})
        reviewed = self.service.act(Actor("admin", "administrator"), self.record["id"], self.refresh()["version"],
                                    "review", {'progress_note': '复查'})
        self.service.act(Actor("cm", "case_manager"), reviewed["id"], reviewed["version"], "amend",
                         {'amendment_reason': '追加授权', 'updated_goals': ['目标A'], 'service_minutes': 200})
        timeline = self.service.timeline(Actor("cm", "case_manager"), self.record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("log_service", actions)
        self.assertIn("reverse_service", actions)
        self.assertIn("plan_version", actions)
        log_event = next(event for event in timeline if event["action"] == "log_service" and event["details"]["credential"] == "CRED-A")
        self.assertEqual(log_event["details"]["minutes"], 80)
        reversal_event = next(event for event in timeline if event["action"] == "reverse_service")
        self.assertEqual(reversal_event["details"]["credential"], "CRED-A")
        self.assertEqual(reversal_event["details"]["reason"], "错报")
        version_event = next(event for event in timeline if event["action"] == "plan_version")
        self.assertEqual(version_event["details"]["plan_version_before"], 1)
        self.assertEqual(version_event["details"]["plan_version_after"], 2)
        self.assertEqual(version_event["details"]["authorized_minutes"], 200)

    def test_concurrent_distinct_credentials_stay_consistent(self):
        errors = []

        def worker(idx):
            try:
                credential = "CONC-%02d" % idx
                for _ in range(5):  # 乐观并发：输的一方拿新版本重试
                    record = self.service.get_record(Actor("u%s" % idx, "specialist"), self.record["id"])
                    try:
                        result = self.service.act(Actor("u%s" % idx, "specialist"), record["id"], record["version"],
                                                  "log_service", {'credential': credential,
                                                                  'service_date': '2026-09-12',
                                                                  'session_minutes': 10, 'provider': 'P%s' % idx})
                        return result
                    except Conflict:
                        continue
                raise AssertionError("worker %s exhausted retries" % idx)
            except Exception as exc:  # pragma: no cover - 测试失败诊断
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        snapshot = self.service.ledger(Actor("cm", "case_manager"), self.record["id"])["ledger"]
        registrations = [e for e in snapshot["entries"] if e["type"] == "registration"]
        self.assertEqual(len(registrations), 6)
        totals = snapshot["totals"]
        # 100授权：共登记60分钟，全部有效；无重复计数。
        self.assertEqual(totals["effective_minutes"], 60)
        self.assertEqual(totals["pending_minutes"], 0)

    def test_concurrent_same_credential_books_once(self):
        outcomes = []

        def worker():
            record = self.service.get_record(Actor("u", "specialist"), self.record["id"])
            try:
                result = self.service.act(Actor("u", "specialist"), record["id"], record["version"],
                                          "log_service", {'credential': 'DUP-1', 'service_date': '2026-09-12',
                                                          'session_minutes': 40, 'provider': 'SP'})
                outcomes.append(result)
            except Conflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = self.service.ledger(Actor("cm", "case_manager"), self.record["id"])["ledger"]
        registrations = [e for e in snapshot["entries"] if e["type"] == "registration"]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(snapshot["totals"]["effective_minutes"], 40)
        successful = [item for item in outcomes if isinstance(item, dict)]
        self.assertTrue(all(item["entry"]["id"] == successful[0]["entry"]["id"] for item in successful))
