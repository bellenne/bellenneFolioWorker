import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from folio_worker import screenshot
from test_placement import PlacementTest


class ScreenshotHelpersTest(unittest.TestCase):
    def test_blank_and_caption(self):
        self.assertTrue(screenshot.is_blank(Image.new("RGB", (800, 600), "black")))
        busy = Image.new("RGB", (800, 600), "white")
        busy.paste(Image.new("RGB", (400, 300), "navy"), (100, 100))
        self.assertFalse(screenshot.is_blank(busy))
        out = screenshot.add_caption(Image.new("RGB", (3840, 2160), "gray"), ["Файл в папке", "D:\\print\\a.jpg"])
        self.assertEqual(out.width, screenshot.MAX_WIDTH)
        self.assertGreater(out.height, round(2160 * screenshot.MAX_WIDTH / 3840))

    def test_enabled_flag(self):
        with patch.dict(os.environ, {"PLACEMENT_SCREENSHOT": "0"}):
            self.assertFalse(screenshot.enabled())
        with patch.dict(os.environ, {"PLACEMENT_SCREENSHOT": "1"}):
            self.assertTrue(screenshot.enabled())

    @unittest.skipIf(os.name == "nt", "проверка не-Windows")
    def test_not_windows(self):
        with self.assertRaises(screenshot.ScreenshotError):
            screenshot.capture_folder(Path(__file__), Path("x.jpg"), [])


class PlacementProofTest(PlacementTest):
    def setUp(self):
        super().setUp()
        os.environ["PLACEMENT_SCREENSHOT"] = "1"

    def fake_capture(self, path, out, caption):
        self.captured.append((Path(path), caption))
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), "green").save(out, "JPEG")
        return out

    def place(self, capture):
        self.captured = []
        action = self.action()
        action["posting_number"] = "0123-0001-1"
        with self.claim(action), patch.object(self.module.screenshot, "capture_folder", side_effect=capture):
            self.module.process_placement(action)
        return action

    def proofs(self):
        with self.module.connection() as con:
            return con.execute("SELECT * FROM placement_proofs ORDER BY id").fetchall()

    def test_screenshot_per_file_then_delivered(self):
        action = self.place(self.fake_capture)
        self.assertEqual([p.name for p, _ in self.captured], ["art-1.png", "art-2.jpg"])
        caption = self.captured[0][1]
        self.assertIn("art-1.png", caption[0])
        self.assertIn("Заказ 0123-0001-1 · MES mes-123 · serial 1", caption[3])
        rows = self.proofs()
        self.assertEqual([(r["serial"], r["error"]) for r in rows], [(1, None), (2, None)])
        self.assertTrue(all(Path(r["path"]).is_file() for r in rows))
        with self.module.connection() as con:
            report = con.execute("SELECT payload FROM placement_reports").fetchone()["payload"]
        self.assertIn('"screenshot": "ok"', report)

        sent = []
        def post(url, **kwargs):
            sent.append((url, kwargs))
            if len(sent) == 1:
                raise httpx.ConnectError("offline", request=httpx.Request("POST", url))
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))
        with patch.object(self.module.httpx, "post", side_effect=post):
            self.assertTrue(self.module.proof_report_one())  # сеть упала — повторим позже
            self.assertEqual(len(self.proofs()), 2)
            with self.module.connection() as con:
                con.execute("UPDATE placement_proofs SET next_at=NULL")
            self.assertTrue(self.module.proof_report_one())
            self.assertTrue(self.module.proof_report_one())
            self.assertFalse(self.module.proof_report_one())
        url, kwargs = sent[1]
        self.assertTrue(url.endswith("/folio/image-worker/jobs/123/placement-proof"))
        self.assertEqual(kwargs["data"]["operation_id"], action["operation_id"])
        self.assertEqual(kwargs["data"]["serial"], "1")
        self.assertEqual(kwargs["files"]["screenshot"][2], "image/jpeg")
        self.assertEqual(self.proofs(), [])
        self.assertFalse(any(Path(r["path"]).exists() for r in rows))  # после доставки локальная копия не нужна

    def test_failed_screenshot_is_reported_not_faked(self):
        def broken(path, out, caption):
            raise screenshot.ScreenshotError("Экран недоступен: снимок пустой")
        self.place(broken)
        self.assertEqual(sorted(p.name for p in self.prints.iterdir()), ["art-1.png", "art-2.jpg"])
        rows = self.proofs()
        self.assertTrue(all(r["path"] is None and "Экран недоступен" in r["error"] for r in rows))
        sent = []
        def post(url, **kwargs):
            sent.append(kwargs)
            return httpx.Response(404, json={"detail": "placement_operation_unknown"},
                                  request=httpx.Request("POST", url))
        with patch.object(self.module.httpx, "post", side_effect=post):
            self.assertTrue(self.module.proof_report_one())
        self.assertIsNone(sent[0]["files"])
        self.assertIn("Экран недоступен", sent[0]["data"]["error"])
        self.assertEqual(len(self.proofs()), 1)  # отклонённый снимок удалён, следующий остаётся

    def test_disabled(self):
        os.environ["PLACEMENT_SCREENSHOT"] = "0"
        self.place(self.fake_capture)
        self.assertEqual(self.captured, [])
        self.assertEqual(self.proofs(), [])


del PlacementTest  # базовые тесты запускаются из своего модуля

if __name__ == "__main__":
    unittest.main()
