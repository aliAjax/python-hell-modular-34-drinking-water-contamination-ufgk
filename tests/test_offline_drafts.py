import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError
from tests.helpers import create_disinfected_item


def draft_body(client_id, batch_id, sample_id, concentration):
    return {
        "client_id": client_id,
        "batch_id": batch_id,
        "sample_id": sample_id,
        "zone_id": "Z-1",
        "concentration": concentration,
    }


class OfflineDraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = create_disinfected_item(self.service)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_draft_saved_while_offline_and_merged_by_batch_on_sync(self):
        # 外勤断网：同一批次现场两份草稿 + 另一批次实验室一份
        self.service.save_sample_draft(self.item["id"], draft_body("dev-1", "B-1", "S-F-1", 15,), "field-1", "field_operator")
        self.service.save_sample_draft(self.item["id"], draft_body("dev-2", "B-1", "S-F-2", 2), "field-1", "field_operator")
        self.service.save_sample_draft(self.item["id"], draft_body("lab-1", "B-2", "S-L-1", 1), "lab-1", "lab")
        drafts = self.service.repository.list_drafts(self.item["id"])
        self.assertEqual([d["status"] for d in drafts], ["pending", "pending", "pending"])

        # 回网按批次合并：组内按产生顺序，B-1 先到超标被后到合格顶掉
        report = self.service.sync_drafts(self.item["id"])
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["synced"]), 3)

        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["status"], "sampled")
        by_id = {b["batch_id"]: b for b in item["payload"]["sample_batches"]}
        b1 = by_id["B-1"]
        self.assertEqual(b1["status"], "effective")
        self.assertTrue(b1["results"][0]["superseded"])
        self.assertFalse(b1["results"][1]["superseded"])
        self.assertEqual(by_id["B-2"]["status"], "effective")
        # 同步后草稿标记为 synced，可查
        self.assertTrue(all(d["status"] == "synced" for d in item["drafts"]))

    def test_sync_failure_resumes_from_failed_batch(self):
        # 两份合法草稿 + 一份非法（浓度缺失/负数等领域错误）
        self.service.save_sample_draft(self.item["id"], draft_body("ok-1", "B-1", "S-1", 1), "lab-1", "lab")
        bad = draft_body("bad-1", "B-2", "S-BAD", 5)
        del bad["concentration"]
        self.service.repository.save_draft(
            self.item["id"], "bad-1", "B-2", "lab-1", "lab", {"batch_id": "B-2", "sample_id": "S-BAD", "zone_id": "Z-1"}
        )
        self.service.save_sample_draft(self.item["id"], draft_body("ok-2", "B-3", "S-2", 1), "lab-1", "lab")

        report = self.service.sync_drafts(self.item["id"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["failed"]["batch_id"], "B-2")
        self.assertEqual(report["failed"]["error_code"], "invalid_number")
        self.assertEqual(report["resume_from"]["batch_id"], "B-2")
        # B-1 已合并，B-3 未被触碰（失败后从该批次续传）
        synced = {d["batch_id"] for d in self.service.repository.list_drafts(self.item["id"], "synced")}
        self.assertEqual(synced, {"B-1"})
        pending = {d["batch_id"] for d in self.service.repository.list_drafts(self.item["id"], "pending")}
        self.assertEqual(pending, {"B-3"})
        failed_drafts = self.service.repository.list_drafts(self.item["id"], "failed")
        self.assertEqual(failed_drafts[0]["client_id"], "bad-1")

        # 修复失败草稿后重新同步：从 B-2 续传，再传 B-3
        self.service.repository.mark_draft(failed_drafts[0]["id"], "pending")
        self.service.repository.save_draft(
            self.item["id"], "bad-1", "B-2", "lab-1", "lab",
            {"batch_id": "B-2", "sample_id": "S-BAD", "zone_id": "Z-1", "concentration": 3},
        )
        report = self.service.sync_drafts(self.item["id"])
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["synced"]), 2)
        item = self.service.get_item(self.item["id"])
        self.assertEqual({b["batch_id"] for b in item["payload"]["sample_batches"]}, {"B-1", "B-2", "B-3"})

    def test_draft_save_is_idempotent_by_client_id(self):
        self.service.save_sample_draft(self.item["id"], draft_body("dev-1", "B-1", "S-1", 9), "field-1", "field_operator")
        result = self.service.save_sample_draft(self.item["id"], draft_body("dev-1", "B-1", "S-1", 2), "field-1", "field_operator")
        self.assertFalse(result["created"])
        self.assertEqual(result["draft"]["payload"]["concentration"], 2.0)
        self.assertEqual(len(self.service.repository.list_drafts(self.item["id"])), 1)

    def test_sync_is_idempotent_after_completion(self):
        self.service.save_sample_draft(self.item["id"], draft_body("dev-1", "B-1", "S-1", 2), "lab-1", "lab")
        first = self.service.sync_drafts(self.item["id"])
        self.assertTrue(first["complete"])
        second = self.service.sync_drafts(self.item["id"])
        self.assertTrue(second["complete"])
        self.assertEqual(second["synced"], [])
        item = self.service.get_item(self.item["id"])
        self.assertEqual(len(item["payload"]["sample_batches"][0]["results"]), 1)

    def test_draft_permission(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.save_sample_draft(self.item["id"], draft_body("x", "B-1", "S-1", 1), "a", "analyst")
        self.assertEqual(ctx.exception.status, 403)

    def test_draft_merged_with_live_result_avoids_version_overwrite(self):
        # 回网前在线结果已先入库（版本前移），草稿合并时不得覆盖它
        item = self.service.act(self.item["id"], "sample", {
            "batch_id": "B-9", "sample_id": "S-LIVE", "zone_id": "Z-1", "concentration": 8,
        }, "lab-1", "lab", self.item["version"])
        self.service.save_sample_draft(self.item["id"], draft_body("dev-1", "B-9", "S-F-1", 9), "field-1", "field_operator")
        report = self.service.sync_drafts(self.item["id"])
        self.assertTrue(report["complete"])
        item = self.service.get_item(self.item["id"])
        b9 = item["payload"]["sample_batches"][0]
        sources = sorted(r["source"] for r in b9["results"])
        self.assertEqual(sources, ["field", "lab"])


if __name__ == "__main__":
    unittest.main()
