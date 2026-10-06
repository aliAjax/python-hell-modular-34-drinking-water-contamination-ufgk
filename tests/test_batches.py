import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class BatchAggregationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1"],
            "population": 5000,
        }, "analyst-1", "analyst")
        self._advance_to_disinfected()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _advance_to_disinfected(self):
        item = self.item
        item = self.service.act(item["id"], "verify", {"sample_count": 2}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
        item = self.service.act(item["id"], "switch_source", {"alternate_source_id": "ALT-1"}, "coord-1", "coordinator", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-1"}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": "Z-1", "completed": True}, "field-1", "field_operator", item["version"])
        self.item = item

    def _sample(self, batch_id, source_type, sample_id, concentration, role, actor=None):
        actor = actor or ("lab-1" if role == "lab" else "field-1")
        return self.service.act(
            self.item["id"], "sample",
            {"batch_id": batch_id, "sample_id": sample_id, "zone_id": "Z-1", "concentration": concentration},
            actor, role, self.item["version"],
        )

    def test_same_batch_same_source_later_overwrites(self):
        self.item = self._sample("B-1", "lab", "S-1", 5, "lab")
        self.item = self._sample("B-1", "lab", "S-2", 8, "lab")
        batch = self.item["payload"]["sampling_batches"]["B-1"]
        self.assertEqual(batch["results"]["lab"]["sample_id"], "S-2")
        self.assertEqual(batch["results"]["lab"]["concentration"], 8)
        self.assertEqual(batch["status"], "consistent")

    def test_same_batch_different_sources_coexist_pending_judgment(self):
        self.item = self._sample("B-1", "lab", "S-1", 2, "lab")
        self.item = self._sample("B-1", "field", "S-2", 12, "field_operator")
        batch = self.item["payload"]["sampling_batches"]["B-1"]
        self.assertEqual(batch["results"]["lab"]["concentration"], 2)
        self.assertEqual(batch["results"]["field"]["concentration"], 12)
        self.assertEqual(batch["status"], "pending_judgment")

    def test_judge_adopts_source_and_restores(self):
        self.item = self._sample("B-1", "lab", "S-1", 2, "lab")
        self.item = self._sample("B-1", "field", "S-2", 12, "field_operator")
        # 待判定批次有效浓度取最保守值，恢复被拒
        with self.assertRaises(DomainError) as context:
            self.service.act(self.item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", self.item["version"])
        self.assertEqual(context.exception.code, "quality_not_met")
        # 判定采用 lab 来源后，有效浓度回落，可恢复
        self.item = self.service.act(self.item["id"], "judge", {"batch_id": "B-1", "adopt_source": "lab"}, "coord-1", "coordinator", self.item["version"])
        self.assertEqual(self.item["payload"]["sampling_batches"]["B-1"]["status"], "consistent")
        self.item = self.service.act(self.item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", self.item["version"])
        self.assertEqual(self.item["status"], "restored")

    def test_late_result_overturns_completed_restoration(self):
        self.item = self._sample("B-1", "lab", "S-1", 2, "lab")
        self.item = self.service.act(self.item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", self.item["version"])
        self.assertEqual(self.item["status"], "restored")
        # 恢复完成后，现场晚到一份超标结果，推翻结论
        self.item = self._sample("B-1", "field", "S-2", 12, "field_operator")
        self.assertEqual(self.item["status"], "sampled")
        self.assertIsNone(self.item["payload"]["restoration"])
        history = self.item["payload"]["restoration_history"]
        self.assertEqual(len(history), 1)
        self.assertIn("超过限值", history[0]["revoke_reason"])
        # 审计里能查到退回原因
        revoked = [e for e in self.item["audit"] if e["event_type"] == "sample" and e["payload"].get("recovery_revoked")]
        self.assertTrue(revoked)

    def test_late_result_within_limit_keeps_restoration(self):
        self.item = self._sample("B-1", "lab", "S-1", 2, "lab")
        self.item = self.service.act(self.item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", self.item["version"])
        self.item = self._sample("B-2", "lab", "S-2", 3, "lab")
        self.assertEqual(self.item["status"], "restored")
        self.assertIsNotNone(self.item["payload"]["restoration"])


class DraftOfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        item = self.service.create_item({
            "source_id": "SRC-2",
            "contaminant": "bacteria",
            "detected_at": "2026-09-27T07:00:00+00:00",
            "concentration": 30,
            "limit": 10,
            "zone_ids": ["Z-1"],
            "population": 1000,
        }, "analyst-1", "analyst")
        for action, payload, role in [
            ("verify", {"sample_count": 1}, "analyst"),
            ("advise", {"notice_id": "N-2", "kind": "boil", "message": "煮沸"}, "dispatcher"),
            ("switch_source", {"alternate_source_id": "ALT-2"}, "coordinator"),
            ("flush", {"zone_id": "Z-1"}, "field_operator"),
            ("disinfect", {"zone_id": "Z-1", "completed": True}, "field_operator"),
        ]:
            item = self.service.act(item["id"], action, payload, "actor-1", role, item["version"])
        self.item = item

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _draft(self, draft_id, results):
        return self.service.submit_draft(
            self.item["id"],
            {"client_draft_id": draft_id, "results": results},
            "field-1", "field_operator",
        )

    def test_draft_merges_by_batch_and_retries_are_idempotent(self):
        summary = self._draft("draft-1", [
            {"batch_id": "B-1", "sample_id": "S-1", "zone_id": "Z-1", "concentration": 2},
            {"batch_id": "B-2", "sample_id": "S-2", "zone_id": "Z-1", "concentration": 3},
        ])
        self.assertEqual(len(summary["applied"]), 2)
        self.assertEqual(summary["status"], "sampled")
        item = self.service.get_item(self.item["id"])
        self.assertIn("B-1", item["payload"]["sampling_batches"])
        self.assertIn("B-2", item["payload"]["sampling_batches"])
        # 同一份草稿重复提交：全部跳过，不重复归集
        retry = self._draft("draft-1", [
            {"batch_id": "B-1", "sample_id": "S-1", "zone_id": "Z-1", "concentration": 2},
            {"batch_id": "B-2", "sample_id": "S-2", "zone_id": "Z-1", "concentration": 3},
        ])
        self.assertEqual(len(retry["applied"]), 0)
        self.assertEqual(len(retry["skipped"]), 2)

    def test_draft_resumes_from_failed_batch(self):
        self._draft("draft-1", [
            {"batch_id": "B-1", "sample_id": "S-1", "zone_id": "Z-1", "concentration": 2},
        ])
        # 回网后续传：复用同一草稿号，B-1 已提交应跳过，只补新的 B-3
        summary = self._draft("draft-1", [
            {"batch_id": "B-1", "sample_id": "S-1", "zone_id": "Z-1", "concentration": 2},
            {"batch_id": "B-3", "sample_id": "S-3", "zone_id": "Z-1", "concentration": 4},
        ])
        self.assertEqual(len(summary["applied"]), 1)
        self.assertEqual(summary["applied"][0]["batch_id"], "B-3")
        self.assertEqual(len(summary["skipped"]), 1)
        self.assertEqual(summary["skipped"][0]["batch_id"], "B-1")
        status = self.service.draft_status(self.item["id"], "draft-1", "field-1", "field_operator")
        self.assertEqual([row["batch_id"] for row in status["applied"]], ["B-1", "B-3"])


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_legacy_sample_results_backfilled_into_batches(self):
        item = self.service.create_item({
            "source_id": "SRC-3",
            "contaminant": "lead",
            "detected_at": "2026-09-20T07:00:00+00:00",
            "concentration": 15,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2"],
            "population": 800,
        }, "analyst-1", "analyst")
        # 模拟升级前的旧数据：扁平 sample_results，无 sampling_batches
        conn = self.repo.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM items WHERE id=?", (item["id"],)).fetchone()
            payload = __import__("json").loads(row["payload"])
            payload.pop("sampling_batches", None)
            payload["restoration"] = {"actor": "coord-1", "note": "旧恢复记录", "at": "2026-09-21T00:00:00+00:00"}
            payload["sample_results"] = [
                {"sample_id": "S-1", "zone_id": "Z-1", "concentration": 5},
                {"sample_id": "S-2", "zone_id": "Z-2", "concentration": 3},
            ]
            conn.execute("UPDATE items SET payload=? WHERE id=?", (__import__("json").dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), item["id"]))
            conn.execute("COMMIT")
        finally:
            conn.close()

        migrated = self.repo.migrate_legacy_data()
        self.assertEqual(migrated, 1)
        migrated_again = self.repo.migrate_legacy_data()
        self.assertEqual(migrated_again, 0)

        item = self.service.get_item(item["id"])
        batches = item["payload"]["sampling_batches"]
        self.assertEqual(set(batches.keys()), {"S-1", "S-2"})
        self.assertEqual(batches["S-1"]["results"]["lab"]["concentration"], 5)
        self.assertEqual(batches["S-2"]["results"]["lab"]["concentration"], 3)
        self.assertNotIn("sample_results", item["payload"])
        # 原恢复记录仍可查
        self.assertEqual(item["payload"]["restoration"]["note"], "旧恢复记录")


if __name__ == "__main__":
    unittest.main()
