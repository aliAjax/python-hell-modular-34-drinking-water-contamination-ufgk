import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.audit import canonical_json
from src import batches


LEGACY_SCHEMA = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    stable_key TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(entity_type, stable_key)
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(item_id, source_type, external_id),
    FOREIGN KEY(item_id) REFERENCES items(id)
);
CREATE TABLE actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id)
);
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    event_type TEXT NOT NULL,
    actor TEXT,
    role TEXT,
    payload TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def build_legacy_db(path, items):
    """items: 列表，每项 (status, payload)。返回写入后的 item id 列表。"""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    ids = []
    for status, payload in items:
        conn.execute(
            "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at)"
            " VALUES('water_contamination',?, ?,1,?,'a','analyst','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')",
            (payload.get("_key", "k-%d" % len(ids)), status, canonical_json(payload)),
        )
        ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    return ids


def restored_legacy_payload():
    return {
        "source_id": "SRC-OLD",
        "contaminant": "nitrate",
        "detected_at": "2026-09-01T00:00:00+00:00",
        "concentration": 20,
        "limit": 10,
        "zone_ids": ["Z-1"],
        "sample_results": [{"sample_id": "OLD-1", "zone_id": "Z-1", "concentration": 2}],
        "restoration": {"actor": "coord-old", "note": "原恢复结论", "at": "2026-09-02T00:00:00+00:00"},
    }


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)  # Repository.initialize 需要自己建库

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def _open_repo(self):
        repo = Repository(self.tmp.name)
        repo.initialize()
        return repo

    def test_legacy_samples_get_backfilled_batch(self):
        payload = {
            "source_id": "S", "contaminant": "c", "detected_at": "2026-09-01T00:00:00+00:00",
            "concentration": 20, "limit": 10, "zone_ids": ["Z-1"],
            "sample_results": [
                {"sample_id": "OLD-1", "zone_id": "Z-1", "concentration": 2},
                {"sample_id": "OLD-2", "zone_id": "Z-1", "concentration": 3},
            ],
        }
        item_id = build_legacy_db(self.tmp.name, [("sampled", payload)])[0]

        repo = self._open_repo()
        item = repo.get_item(item_id)
        batches_data = item["payload"]["sample_batches"]
        self.assertEqual(len(batches_data), 1)
        batch = batches_data[0]
        self.assertEqual(batch["batch_id"], "LEGACY-%d" % item_id)
        self.assertEqual(batch["status"], "effective")
        self.assertTrue(batch["legacy"])
        self.assertEqual([r["sample_id"] for r in batch["results"]], ["OLD-1", "OLD-2"])
        self.assertTrue(all(r["source"] == "legacy" for r in batch["results"]))
        # 有效结果可用于规则判定
        self.assertEqual(len(batches.effective_results(item["payload"])), 2)
        # 审计链记录了补齐事件
        types = [e["event_type"] for e in repo.audit_trail(item_id)]
        self.assertIn("batches_backfilled", types)

    def test_legacy_restoration_remains_queryable(self):
        item_id = build_legacy_db(self.tmp.name, [("restored", restored_legacy_payload())])[0]
        repo = self._open_repo()
        item = repo.get_item(item_id)
        # 状态与原恢复结论保留
        self.assertEqual(item["status"], "restored")
        self.assertEqual(item["payload"]["restoration"]["actor"], "coord-old")
        self.assertEqual(item["payload"]["restoration"]["note"], "原恢复结论")
        # 恢复历史可查，且未被作废
        history = item["payload"]["restoration_history"]
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["legacy"])
        self.assertIsNone(history[0]["revoked"])
        self.assertEqual(history[0]["note"], "原恢复结论")
        # 动作/审计接口仍能看到完整链路
        service = Service(repo)
        full = service.get_item(item_id)
        self.assertEqual(full["batch_summary"][0]["legacy"], True)
        self.assertEqual(len(full["effective_results"]), 1)

    def test_legacy_restoration_can_be_revoked_by_late_result(self):
        item_id = build_legacy_db(self.tmp.name, [("restored", restored_legacy_payload())])[0]
        repo = self._open_repo()
        service = Service(repo)
        item = service.get_item(item_id)
        # 晚到一批超标结果：历史恢复结论作废并写明原因，原记录仍可查
        item = service.act(item_id, "sample", {
            "batch_id": "B-NEW", "sample_id": "NEW-1", "zone_id": "Z-1", "concentration": 50,
        }, "field-1", "field_operator", item["version"])
        self.assertEqual(item["status"], "reinspection_pending")
        history = item["payload"]["restoration_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["actor"], "coord-old")
        self.assertIn("推翻恢复结论", history[0]["revoked"]["reason"])
        self.assertIn("B-NEW", history[0]["revoked"]["reason"])

    def test_migration_idempotent_across_restarts(self):
        payload = restored_legacy_payload()
        item_id = build_legacy_db(self.tmp.name, [("restored", payload)])[0]
        repo = self._open_repo()
        # 再次初始化不应重复补齐
        repo = self._open_repo()
        types = [e["event_type"] for e in repo.audit_trail(item_id)]
        self.assertEqual(types.count("batches_backfilled"), 1)
        item = repo.get_item(item_id)
        self.assertEqual(len(item["payload"]["sample_batches"]), 1)
        self.assertEqual(len(item["payload"]["restoration_history"]), 1)

    def test_legacy_item_without_samples_untouched(self):
        payload = {
            "source_id": "S", "contaminant": "c", "detected_at": "2026-09-01T00:00:00+00:00",
            "concentration": 20, "limit": 10, "zone_ids": ["Z-1"],
        }
        item_id = build_legacy_db(self.tmp.name, [("verified", payload)])[0]
        repo = self._open_repo()
        item = repo.get_item(item_id)
        self.assertNotIn("sample_batches", item["payload"])
        self.assertNotIn("batches_backfilled", [e["event_type"] for e in repo.audit_trail(item_id)])


if __name__ == "__main__":
    unittest.main()
