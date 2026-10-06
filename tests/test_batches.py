import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError
from tests.helpers import create_disinfected_item, sample


class BatchAggregationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = create_disinfected_item(self.service)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_same_batch_same_source_later_overrides_earlier(self):
        # 实验室同批次两次提交：后到顶掉先到
        item = sample(self.service, self.item, "B-1", "S-LAB-1", 12, "lab")
        self.assertEqual(item["status"], "sampled")
        item = sample(self.service, self.item, "B-1", "S-LAB-2", 2, "lab")
        batch = item["payload"]["sample_batches"][0]
        self.assertEqual(batch["status"], "effective")
        self.assertTrue(batch["results"][0]["superseded"])
        self.assertEqual(batch["results"][0]["superseded_by_seq"], 2)
        self.assertFalse(batch["results"][1]["superseded"])
        # 有效结果只剩后到那份，故此时可以恢复
        self.assertEqual(len(item["effective_results"]), 1)
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_different_sources_coexist_and_pending_on_disagreement(self):
        # 实验室合格、现场超标：并存，批次待判定，不能恢复
        item = sample(self.service, self.item, "B-1", "S-LAB-1", 2, "lab")
        item = sample(self.service, self.item, "B-1", "S-FIELD-1", 15, "field_operator")
        batch = item["payload"]["sample_batches"][0]
        self.assertEqual(batch["status"], "pending_judgement")
        self.assertEqual(len(batch["results"]), 2)
        self.assertFalse(batch["results"][0]["superseded"])
        self.assertFalse(batch["results"][1]["superseded"])
        self.assertEqual(len(item["effective_results"]), 0)
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "batch_pending_judgement")

    def test_different_sources_agree_both_effective(self):
        item = sample(self.service, self.item, "B-1", "S-LAB-1", 2, "lab")
        item = sample(self.service, self.item, "B-1", "S-FIELD-1", 3, "field_operator")
        self.assertEqual(item["payload"]["sample_batches"][0]["status"], "effective")
        self.assertEqual(len(item["effective_results"]), 2)

    def test_different_batches_are_separate(self):
        item = sample(self.service, self.item, "B-1", "S-1", 2, "lab")
        item = sample(self.service, self.item, "B-2", "S-2", 15, "field_operator")
        self.assertEqual(len(item["payload"]["sample_batches"]), 2)
        # B-2 独立超标，不能恢复
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "quality_not_met")

    def test_adjudication_picks_source_and_unblocks_restore(self):
        item = sample(self.service, self.item, "B-1", "S-LAB-1", 2, "lab")
        item = sample(self.service, self.item, "B-1", "S-FIELD-1", 15, "field_operator")
        item = self.service.act(item["id"], "adjudicate_batch", {
            "batch_id": "B-1", "chosen_source": "lab", "note": "现场采样受管路残留影响",
        }, "coord-1", "coordinator", item["version"])
        batch = item["payload"]["sample_batches"][0]
        self.assertEqual(batch["status"], "effective")
        self.assertTrue(batch["results"][1]["rejected"])
        self.assertEqual(item["payload"]["sample_batches"][0]["adjudication"]["chosen_source"], "lab")
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_pending_clears_when_sources_agree_after_override(self):
        item = sample(self.service, self.item, "B-1", "S-LAB-1", 2, "lab")
        item = sample(self.service, self.item, "B-1", "S-FIELD-1", 15, "field_operator")
        self.assertEqual(item["payload"]["sample_batches"][0]["status"], "pending_judgement")
        # 现场重测合格，顶掉自己先到结果，冲突消失
        item = sample(self.service, self.item, "B-1", "S-FIELD-2", 3, "field_operator")
        self.assertEqual(item["payload"]["sample_batches"][0]["status"], "effective")
        self.assertTrue(item["payload"]["sample_batches"][0]["results"][1]["superseded"])

    def test_source_role_mismatch_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            sample(self.service, self.item, "B-1", "S-1", 2, "lab", source="field")
        self.assertEqual(ctx.exception.code, "source_role_mismatch")

    def test_adjudication_reopened_by_new_other_source_result(self):
        item = sample(self.service, self.item, "B-1", "S-LAB-1", 2, "lab")
        item = sample(self.service, self.item, "B-1", "S-FIELD-1", 15, "field_operator")
        item = self.service.act(item["id"], "adjudicate_batch", {
            "batch_id": "B-1", "chosen_source": "lab",
        }, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["payload"]["sample_batches"][0]["status"], "effective")
        # 现场再次提交新结果，重新进入待判定
        item = sample(self.service, self.item, "B-1", "S-FIELD-2", 14, "field_operator")
        self.assertEqual(item["payload"]["sample_batches"][0]["status"], "pending_judgement")

    def test_no_effective_result_blocks_restore(self):
        # sampled 状态但唯一批次处于待判定：没有有效结果，不能恢复
        item = sample(self.service, self.item, "B-1", "S-LAB-1", 2, "lab")
        item = sample(self.service, self.item, "B-1", "S-FIELD-1", 15, "field_operator")
        self.assertEqual(item["status"], "sampled")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "batch_pending_judgement")


class RestorationRecalculationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = create_disinfected_item(self.service)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _restore(self, item):
        item = sample(self.service, item, "B-1", "S-1", 2, "lab")
        return self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "coordinator", item["version"])

    def test_late_failing_result_revokes_restoration_with_reason(self):
        item = self._restore(self.item)
        self.assertEqual(item["status"], "restored")
        # 晚到：现场另一批次超标
        item = sample(self.service, item, "B-2", "S-F-1", 20, "field_operator")
        self.assertEqual(item["status"], "reinspection_pending")
        self.assertIsNone(item["payload"]["restoration"])
        entry = item["payload"]["restoration_history"][0]
        self.assertTrue(entry["revoked"])
        self.assertIn("推翻恢复结论", entry["revoked"]["reason"])
        self.assertIn("B-2", entry["revoked"]["reason"])
        # 审计链记录了作废原因
        invalidate_events = [e for e in item["audit"] if "restoration_invalidated" in (e.get("payload") or {})]
        self.assertEqual(len(invalidate_events), 1)
        self.assertIn("B-2", invalidate_events[0]["payload"]["restoration_invalidated"]["reason"])

    def test_late_conflicting_batch_revokes_restoration(self):
        item = self._restore(self.item)
        # 同批次现场冲突结论 -> 待判定 -> 恢复被推翻
        item = sample(self.service, item, "B-1", "S-F-1", 20, "field_operator")
        self.assertEqual(item["status"], "reinspection_pending")
        self.assertTrue(item["payload"]["restoration_history"][0]["revoked"])

    def test_passing_late_result_keeps_restoration(self):
        item = self._restore(self.item)
        item = sample(self.service, item, "B-2", "S-2", 1, "lab")
        self.assertEqual(item["status"], "restored")
        self.assertIsNotNone(item["payload"]["restoration"])
        self.assertFalse(any(e.get("revoked") for e in item["payload"]["restoration_history"]))

    def test_can_restore_again_after_reinspection_passes(self):
        item = self._restore(self.item)
        item = sample(self.service, item, "B-2", "S-F-1", 20, "field_operator")
        self.assertEqual(item["status"], "reinspection_pending")
        # 同批次现场重测合格，顶掉超标结果，阻断消除，回到可恢复状态
        item = sample(self.service, item, "B-2", "S-F-2", 1, "field_operator")
        self.assertEqual(item["status"], "sampled")
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "coord-1", "regulator", item["version"])
        self.assertEqual(item["status"], "restored")
        self.assertEqual(len(item["payload"]["restoration_history"]), 2)
        self.assertTrue(item["payload"]["restoration_history"][0]["revoked"])
        self.assertIsNone(item["payload"]["restoration_history"][1]["revoked"])

    def test_adjudication_after_restore_can_revoke(self):
        item = self._restore(self.item)
        item = sample(self.service, item, "B-1", "S-F-1", 20, "field_operator")
        self.assertEqual(item["status"], "reinspection_pending")
        # 裁定采信现场（超标）：恢复结论仍不成立，保持待复检
        item = self.service.act(item["id"], "adjudicate_batch", {
            "batch_id": "B-1", "chosen_source": "field",
        }, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["status"], "reinspection_pending")
        self.assertIsNone(item["payload"]["restoration"])
        # 改裁实验室（合格）后可再次恢复
        # 注：改裁需实验室来源仍有有效结果；这里直接验证当前阻断为超标
        over = [r for r in item["effective_results"] if r["concentration"] > 10]
        self.assertTrue(over)

    def test_restoration_records_effective_result_snapshot(self):
        item = self._restore(self.item)
        snapshot = item["payload"]["restoration"]["effective_results"]
        self.assertEqual(snapshot[0]["batch_id"], "B-1")
        self.assertEqual(snapshot[0]["source"], "lab")


if __name__ == "__main__":
    unittest.main()
