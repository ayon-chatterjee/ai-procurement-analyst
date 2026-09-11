from __future__ import annotations

import os
import tempfile
import unittest

from rfq_copilot.fields import new_field_set
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.schema import RFQ, AICallRecord, Completeness, LineItem, Message, MessageRole, RFQStatus


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "nested", "dir", "rfq.db")
        self.repo = RFQRepository(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_is_idempotent_and_creates_parent_dirs(self):
        self.assertTrue(os.path.exists(self.path))
        RFQRepository(self.path)  # second init must not fail

    def test_save_get_list_delete(self):
        a = RFQ(id="rfq_a", title="A", product="Cartons", category="Packaging", fields=new_field_set(), status=RFQStatus.IN_PROGRESS, turn=1)
        a.line_items.append(LineItem(id="LINE-001", product="Carton", quantity=2000.0))
        a.completeness = Completeness(score=55, ready_to_send=False)
        b = RFQ(id="rfq_b", title="B", product="Brackets", category="Metal parts", fields=new_field_set(), status=RFQStatus.READY, turn=3)
        b.completeness = Completeness(score=100, ready_to_send=True)
        self.repo.save_rfq(a)
        self.repo.save_rfq(b)
        got = self.repo.get_rfq("rfq_a")
        self.assertEqual(got.to_dict(), a.to_dict())
        rows = self.repo.list_rfqs()
        self.assertEqual([r.id for r in rows], ["rfq_b", "rfq_a"])  # most recently updated first
        self.assertEqual(rows[0].readiness_score, 100)
        self.assertTrue(rows[0].ready_to_send)
        self.assertEqual(rows[1].line_item_count, 1)
        # upsert updates indexed columns
        a.completeness.score = 90
        a.status = RFQStatus.READY
        self.repo.save_rfq(a)
        rows = self.repo.list_rfqs()
        self.assertEqual(rows[0].id, "rfq_a")
        self.assertEqual(rows[0].readiness_score, 90)
        self.assertEqual(rows[0].status, RFQStatus.READY)
        self.repo.delete_rfq("rfq_a")
        self.assertIsNone(self.repo.get_rfq("rfq_a"))

    def test_messages_and_ai_calls_with_cascade(self):
        rfq = RFQ(id="rfq_m", fields=new_field_set(), status=RFQStatus.DRAFT)
        self.repo.save_rfq(rfq)
        self.repo.add_message(Message(id="msg_1", rfq_id="rfq_m", turn=1, role=MessageRole.BUYER, kind="request", content="I need carton boxes.", payload={"text": "x"}))
        self.repo.add_message(Message(id="msg_2", rfq_id="rfq_m", turn=1, role=MessageRole.ASSISTANT, kind="assistant", content="Sure."))
        self.repo.add_ai_call(AICallRecord(id="call_1", rfq_id="rfq_m", turn=1, call_type="first_turn", provider="claude_cli", model="sonnet",
                                           prompt_version="v", prompt_hash="abc", prompt_chars=10, duration_ms=5, ok=True, schema_valid=True,
                                           raw_response='{"structured_output": {}}'))
        msgs = self.repo.list_messages("rfq_m")
        self.assertEqual([m.id for m in msgs], ["msg_1", "msg_2"])
        self.assertEqual(msgs[0].payload, {"text": "x"})
        self.assertEqual(self.repo.get_message("msg_2").role, MessageRole.ASSISTANT)
        calls = self.repo.list_ai_calls("rfq_m")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].ok)
        self.repo.delete_rfq("rfq_m")
        self.assertEqual(self.repo.list_messages("rfq_m"), [])
        self.assertEqual(self.repo.list_ai_calls("rfq_m"), [])

    def test_survives_reopen(self):
        rfq = RFQ(id="rfq_p", title="Persist me", fields=new_field_set())
        self.repo.save_rfq(rfq)
        again = RFQRepository(self.path)
        self.assertEqual(again.get_rfq("rfq_p").title, "Persist me")


if __name__ == "__main__":
    unittest.main()
