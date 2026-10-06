import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request, error

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.http_api import build_handler
from tests.helpers import create_disinfected_item


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        repo = Repository(cls.tmp.name)
        repo.initialize()
        cls.service = Service(repo)
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(cls.service, static_dir))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        os.unlink(cls.tmp.name)

    def _call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_sample_batch_pending_restore_revocation_lifecycle(self):
        item = create_disinfected_item(self.service)
        item_id = item["id"]
        hdrs = {}

        status, body = self._call("POST", "/api/items/%d/actions" % item_id, {
            "action": "sample", "batch_id": "B-H1", "sample_id": "L1", "zone_id": "Z-1", "concentration": 2,
            "expected_version": item["version"],
        }, {"X-User-Id": "lab-1", "X-Role": "lab"})
        self.assertEqual(status, 200, body)
        version = body["version"]

        status, body = self._call("POST", "/api/items/%d/actions" % item_id, {
            "action": "restore", "all_zones_cleared": True, "expected_version": version,
        }, {"X-User-Id": "coord-1", "X-Role": "coordinator"})
        self.assertEqual(status, 200, body)
        version = body["version"]
        self.assertEqual(body["status"], "restored")

        # 晚到现场超标结果：恢复被推翻
        status, body = self._call("POST", "/api/items/%d/actions" % item_id, {
            "action": "sample", "batch_id": "B-H2", "sample_id": "F1", "zone_id": "Z-1", "concentration": 30,
            "expected_version": version,
        }, {"X-User-Id": "field-1", "X-Role": "field_operator"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "reinspection_pending")

        status, body = self._call("GET", "/api/items/%d" % item_id)
        self.assertEqual(status, 200)
        self.assertEqual({b["batch_id"] for b in body["batch_summary"]}, {"B-H1", "B-H2"})
        self.assertTrue(body["payload"]["restoration_history"][0]["revoked"])

    def test_draft_save_and_sync_endpoints(self):
        item = create_disinfected_item(self.service)
        item_id = item["id"]

        status, body = self._call("POST", "/api/items/%d/sample-drafts" % item_id, {
            "client_id": "phone-1", "batch_id": "B-OFF", "sample_id": "D1", "zone_id": "Z-1", "concentration": 2,
        }, {"X-User-Id": "field-1", "X-Role": "field_operator"})
        self.assertEqual(status, 201, body)
        self.assertTrue(body["created"])

        status, body = self._call("GET", "/api/items/%d/sample-drafts/phone-1" % item_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending")

        status, body = self._call("PUT", "/api/items/%d/sample-drafts/sync" % item_id, {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["complete"])
        self.assertEqual(body["synced"][0]["batch_id"], "B-OFF")

        status, body = self._call("PUT", "/api/sample-drafts/sync", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["complete"])

    def test_draft_endpoint_forbidden_for_analyst(self):
        item = create_disinfected_item(self.service)
        status, body = self._call("POST", "/api/items/%d/sample-drafts" % item["id"], {
            "client_id": "x", "batch_id": "B", "sample_id": "D", "zone_id": "Z-1", "concentration": 1,
        }, {"X-User-Id": "a-1", "X-Role": "analyst"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")


if __name__ == "__main__":
    unittest.main()
