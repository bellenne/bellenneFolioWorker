import importlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image


class PlacementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prints = self.root / "prints"
        self.prints.mkdir()
        values = {
            "DATA_DIR": str(self.root / "work" / "data"),
            "SPOOL_DIR": str(self.root / "work" / "spool"),
            "PRINT_SHARE_PATH": str(self.prints),
            "WORKER_API_KEY": "test-key",
            "FOLIO_POLL_URL": "https://one.example.ru/folio/image-worker/next",
            "IMAGE_PROVIDER": "mock",
            "NANOBANANA_MODE": "mock",
        }
        self.env = patch.dict(os.environ, values)
        self.env.start()
        self.addCleanup(self.env.stop)
        from folio_worker import app
        self.module = importlib.reload(app)
        no_thread = patch.multiple(self.module, work_loop=lambda: None, report_loop=lambda: None)
        no_thread.start()
        self.addCleanup(no_thread.stop)
        self.client = TestClient(self.module.app)
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        image = io.BytesIO()
        Image.new("RGB", (200, 200), "blue").save(image, "PNG")
        response = self.client.post("/v1/jobs", headers={"Authorization": "Bearer test-key"},
                                    data={"prompt": "Test", "width_cm": "4", "height_cm": "4"},
                                    files={"image": ("ozon.png", image.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 202, response.text)
        self.job_id = response.json()["id"]
        self.module.process(self.module.get_job(self.job_id))
        self.source = self.root / "work" / "data" / self.job_id / "print.jpg"
        self.assertEqual(self.module.get_job(self.job_id)["status"], "completed")
        self.assertTrue(self.source.is_file())
        self.assertFalse(list(self.prints.iterdir()))
        with self.module.connection() as con:
            con.execute("UPDATE jobs SET folio_job_id=123 WHERE id=?", (self.job_id,))

    def action(self, files=None, pending=None, operation=None, token=None):
        files = files or [
            {"serial": 1, "instance_code": "copy-1", "layout_key": "art-1", "filename": "art-1.png"},
            {"serial": 2, "instance_code": "copy-2", "layout_key": "art-2", "filename": "art-2.jpg"},
        ]
        return {"action": "place_print_files", "id": 123, "worker_id": self.job_id,
                "worker_instance_id": self.module.installation_id(), "item_id": 42,
                "layout_version": "preview-version-1", "operation_id": operation or str(uuid.uuid4()),
                "claim_token": token or str(uuid.uuid4()), "lease_until": "2099-01-01T00:00:00+00:00",
                "external_order_id": "mes-123", "source_print_file": str(self.source),
                "destination": {"kind": "worker_print_directory"}, "print_files": files,
                "results": {}, "pending_serials": pending or [item["serial"] for item in files]}

    def claim(self, action):
        def get(url, **kwargs):
            self.assertEqual(kwargs["headers"]["X-Folio-Worker-Id"], self.module.installation_id())
            self.assertEqual(kwargs["headers"]["X-Folio-Session-Id"], self.module.SESSION_ID)
            return httpx.Response(200, json=action, request=httpx.Request("GET", url))
        return patch.object(self.module.httpx, "get", side_effect=get)

    def test_generation_preview_then_multiple_files_and_report_retry(self):
        preview = self.root / "work" / "data" / self.job_id / "preview.jpg"
        with Image.open(preview) as image:
            self.assertEqual(max(image.size), 1000)
        sent_generation = []
        def generation_post(url, **kwargs):
            sent_generation.append(kwargs)
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))
        with patch.object(self.module.httpx, "post", side_effect=generation_post):
            self.assertTrue(self.module.report_one())
        self.assertEqual(sent_generation[0]["data"]["print_file"], str(self.source))
        self.assertEqual(sent_generation[0]["files"]["preview"][2], "image/jpeg")
        self.assertFalse(list(self.prints.iterdir()))

        action = self.action()
        with self.claim(action):
            self.module.process_placement(action)
        self.assertEqual(sorted(path.name for path in self.prints.iterdir()), ["art-1.png", "art-2.jpg"])
        with Image.open(self.prints / "art-1.png") as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (945, 945))
            self.assertAlmostEqual(image.info["dpi"][0], 150, delta=1)
        with Image.open(self.prints / "art-2.jpg") as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(tuple(round(x) for x in image.info["dpi"]), (150, 150))
        self.assertTrue(self.source.is_file())

        calls = []
        def report(url, **kwargs):
            calls.append(kwargs["json"])
            if len(calls) == 1:
                raise httpx.ConnectError("offline", request=httpx.Request("POST", url))
            return httpx.Response(200, json={"ok": True, "placement_state": "completed"},
                                  request=httpx.Request("POST", url))
        with patch.object(self.module.httpx, "post", side_effect=report):
            self.assertTrue(self.module.placement_report_one())
            with self.module.connection() as con:
                con.execute("UPDATE placement_reports SET next_at=NULL")
            self.assertTrue(self.module.placement_report_one())
        self.assertEqual(calls[0], calls[1])
        self.assertEqual({item["status"] for item in calls[0]["files"]}, {"completed"})
        self.assertEqual(calls[0]["action"], "place_print_files")
        with self.module.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM placement_reports").fetchone()[0], 0)

    def test_restart_repeat_and_foreign_file_conflict(self):
        action = self.action()
        foreign = self.prints / "art-2.jpg"
        foreign.write_bytes(b"another order")
        with self.claim(action):
            self.module.process_placement(action)
        with self.module.connection() as con:
            first = json.loads(con.execute("SELECT payload FROM placement_reports").fetchone()[0])
        self.assertEqual([item["status"] for item in first["files"]], ["completed", "failed"])
        self.assertEqual(foreign.read_bytes(), b"another order")
        placed = self.prints / "art-1.png"
        original_hash = self.module.file_digest(placed)
        with patch.object(self.module.httpx, "post", return_value=httpx.Response(
                200, json={"ok": True, "placement_state": "partial"},
                request=httpx.Request("POST", "https://one.example.ru/folio/image-worker/jobs/123"))):
            self.module.placement_report_one()
        foreign.unlink()  # An operator has removed the unrelated conflicting file.

        old_id = self.module.installation_id()
        self.module = importlib.reload(self.module)
        self.assertEqual(self.module.installation_id(), old_id)
        retry = self.action(files=action["print_files"], pending=[2], operation=action["operation_id"])
        with self.claim(retry):
            self.module.process_placement(retry)
        self.assertEqual(self.module.file_digest(placed), original_hash)
        self.assertTrue(foreign.is_file())
        with self.module.connection() as con:
            second = json.loads(con.execute("SELECT payload FROM placement_reports").fetchone()[0])
        self.assertEqual([item["serial"] for item in second["files"]], [2])
        self.assertEqual(second["files"][0]["status"], "completed")

    def test_missing_source_and_lost_claim_never_report_success(self):
        action = self.action(files=[{"serial": 1, "instance_code": "copy-1",
                                     "layout_key": "art-1", "filename": "art-1.jpg"}])
        self.source.unlink()
        with self.claim(action):
            self.module.process_placement(action)
        with self.module.connection() as con:
            report = json.loads(con.execute("SELECT payload FROM placement_reports").fetchone()[0])
        self.assertEqual(report["files"][0]["status"], "failed")
        self.assertFalse(list(self.prints.iterdir()))
        with self.module.connection() as con:
            con.execute("DELETE FROM placement_reports")
        Image.new("RGB", (945, 945), "blue").save(self.source, "JPEG", dpi=(150, 150))
        lost = httpx.Response(409, json={"detail": "stale"},
                              request=httpx.Request("GET", "https://one.example.ru/folio/image-worker/next"))
        with patch.object(self.module.httpx, "get", return_value=lost), \
             self.assertRaises(self.module.PlacementLeaseLost):
            self.module.process_placement(action)
        self.assertFalse(list(self.prints.iterdir()))

    def test_repeat_claim_uses_manifest_and_rejects_other_version(self):
        action = self.action(files=[{"serial": 1, "instance_code": "copy-1",
                                     "layout_key": "art-1", "filename": "art-1.png"}])
        with self.claim(action):
            self.module.process_placement(action)
        target = self.prints / "art-1.png"
        original = self.module.file_digest(target)
        with self.module.connection() as con:
            con.execute("DELETE FROM placement_reports")  # Simulate a lost local report after publication.
        with self.claim(action):
            self.module.process_placement(action)
        self.assertEqual(self.module.file_digest(target), original)
        with self.module.connection() as con:
            repeated = json.loads(con.execute("SELECT payload FROM placement_reports").fetchone()[0])
        self.assertEqual(repeated["files"][0]["status"], "completed")

        with self.module.connection() as con:
            con.execute("DELETE FROM placement_reports")
        stale = {**action, "layout_version": "another-preview"}
        with self.assertRaisesRegex(ValueError, "Другая операция или версия"):
            self.module.process_placement(stale)
        foreign = {**action, "worker_instance_id": str(uuid.uuid4())}
        with self.assertRaisesRegex(ValueError, "другому воркеру"):
            self.module.process_placement(foreign)
        self.assertEqual(self.module.file_digest(target), original)
