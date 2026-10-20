import io
import base64
import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
import uuid

import httpx

from fastapi.testclient import TestClient
from PIL import Image


class PipelineTest(unittest.TestCase):
    def test_geometry_instruction_does_not_resize_the_reference(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / 'job'
            folder.mkdir()
            path = folder / 'input.png'
            Image.new('RGB', (900, 1200)).save(path)
            original = path.read_bytes()
            with patch.object(module, 'DATA', Path(root)):
                text = module.image_geometry_prompt({'id': 'job', 'width_cm': 40, 'height_cm': 60}, 'Без изменений')
            self.assertIn('900×1200', text)
            self.assertIn('равномерно по обеим осям', text)
            self.assertEqual(path.read_bytes(), original)

    def test_aitunnel_uses_only_supported_aspect_ratios(self):
        from folio_worker import app as module
        self.assertEqual(module.closest_aspect_ratio(210, 90, module.AITUNNEL_ASPECT_RATIOS), "2:1")
        self.assertEqual(module.closest_aspect_ratio(40, 60, module.AITUNNEL_ASPECT_RATIOS), "2:3")

    def test_aitunnel_edits_references_at_4k_and_saves_image(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "job"
            folder.mkdir()
            source = io.BytesIO()
            Image.new("RGB", (24, 36), "red").save(source, "PNG")
            (folder / "input.png").write_bytes(source.getvalue())
            second = io.BytesIO()
            Image.new("RGB", (24, 36), "blue").save(second, "PNG")
            (folder / "input-1.png").write_bytes(second.getvalue())
            row = {"id": "job", "prompt": "Replace the background", "width_cm": 40,
                   "height_cm": 60, "vendor_image_urls": json.dumps([
                       "https://one.example.ru/photo/one", "https://one.example.ru/photo/two",
                   ]), "vendor_image_url": None, "input_count": 2}
            calls = []
            def handler(request):
                calls.append(request)
                self.assertEqual(str(request.url), module.AITUNNEL_URL)
                self.assertEqual(request.headers["authorization"], "Bearer test-aitunnel-key")
                payload = json.loads(request.content)
                self.assertEqual(payload["model"], "hy-image-v3.5-preview")
                self.assertEqual((payload["resolution"], payload["aspect_ratio"], payload["n"]),
                                 ("4K", "2:3", 1))
                self.assertEqual([item["image_url"]["url"] for item in payload["input_references"]],
                                 ["data:image/png;base64," + base64.b64encode(blob).decode()
                                  for blob in (source.getvalue(), second.getvalue())])
                return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(source.getvalue()).decode()}],
                                                 "usage": {"cost_rub": 6.3}})
            client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch.object(module, "DATA", Path(root)), \
                 patch.object(module, "AITUNNEL_KEY", "test-aitunnel-key"), \
                 patch.object(module, "begin_aitunnel_request", return_value=True), \
                 patch.object(module.httpx, "Client", return_value=client):
                result = module.aitunnel_result(row)
            self.assertEqual(len(calls), 1)
            with Image.open(result) as generated:
                self.assertEqual(generated.size, (24, 36))
                self.assertEqual(generated.getpixel((0, 0)), (255, 0, 0))

    def test_aitunnel_uses_snapshot_model_and_its_supported_resolution(self):
        from folio_worker import app as module
        source = io.BytesIO()
        Image.new("RGB", (24, 36), "blue").save(source, "PNG")
        selected = [
            ("seedream-5-0-lite", "4K"),
            ("seedream-5-0-flash", "2K"),
            ("seedream-4.5", "4K"),
            ("hy-image-v3.5-preview", "4K"),
        ]
        for model, resolution in selected:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as root:
                folder = Path(root) / "job"
                folder.mkdir()
                (folder / "input.png").write_bytes(source.getvalue())
                row = {"id": "job", "model": model, "prompt": "Сохраните человека и фон",
                       "width_cm": 40, "height_cm": 60,
                       "vendor_image_urls": json.dumps(["https://one.example.ru/photo/one"]),
                       "vendor_image_url": None, "input_count": 1}
                sent = []
                def handler(request):
                    sent.append(json.loads(request.content))
                    return httpx.Response(200, json={"data": [{"b64_json":
                        base64.b64encode(source.getvalue()).decode()}]})
                client = httpx.Client(transport=httpx.MockTransport(handler))
                with patch.object(module, "DATA", Path(root)), \
                     patch.object(module, "AITUNNEL_KEY", "test-key"), \
                     patch.object(module, "begin_aitunnel_request", return_value=True), \
                     patch.object(module.httpx, "Client", return_value=client):
                    self.assertTrue(module.aitunnel_result(row).is_file())
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]["model"], model)
                self.assertEqual(sent[0]["resolution"], resolution)
                self.assertEqual(sent[0]["aspect_ratio"], "2:3")

    def test_expensive_models_are_blocked_before_billable_request(self):
        from folio_worker import app as module
        for model in ("flux-3-image", "gemini-nano-banana-2.1", "seedream-5-0-pro"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as root, \
                 patch.object(module, "DATA", Path(root)), \
                 patch.object(module, "AITUNNEL_KEY", "test-key"), \
                 patch.object(module, "begin_aitunnel_request") as begin, \
                 patch.object(module.httpx, "Client") as client:
                with self.assertRaisesRegex(RuntimeError, "бюджетного режима"):
                    module.aitunnel_result({"id": "job", "model": model})
                begin.assert_not_called()
                client.assert_not_called()

    def test_aitunnel_lost_response_is_not_retried(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "job"
            folder.mkdir()
            Image.new("RGB", (2, 2), "blue").save(folder / "input.png")
            row = {"id": "job", "prompt": "Edit", "width_cm": 40,
                   "height_cm": 60, "vendor_image_urls": None,
                   "vendor_image_url": None, "input_count": 1}
            calls = []
            def timeout(request):
                calls.append(request)
                payload = json.loads(request.content)
                self.assertTrue(payload["input_references"][0]["image_url"]["url"].startswith("data:image/png;base64,"))
                raise httpx.ReadTimeout("response lost", request=request)
            client = httpx.Client(transport=httpx.MockTransport(timeout))
            with patch.object(module, "DATA", Path(root)), \
                 patch.object(module, "AITUNNEL_KEY", "test-aitunnel-key"), \
                 patch.object(module, "begin_aitunnel_request", return_value=True), \
                 patch.object(module.httpx, "Client", return_value=client):
                with self.assertRaisesRegex(module.AIRequestOutcomeUnknown, "Повторять автоматически нельзя"):
                    module.aitunnel_result(row)
            self.assertEqual(len(calls), 1)
            self.assertFalse((folder / "generated.png").exists())

    def test_aitunnel_retries_before_connection_only(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "job"
            folder.mkdir()
            Image.new("RGB", (2, 2), "blue").save(folder / "input.png")
            row = {"id": "job", "prompt": "Edit", "width_cm": 40,
                   "height_cm": 60, "vendor_image_urls": None,
                   "vendor_image_url": None, "input_count": 1}
            image = io.BytesIO()
            Image.new("RGB", (3, 4), "green").save(image, "PNG")
            calls = []
            def handler(request):
                calls.append(request)
                if len(calls) == 1:
                    raise httpx.ConnectTimeout("not connected", request=request)
                return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image.getvalue()).decode()}]})
            client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch.object(module, "DATA", Path(root)), \
                 patch.object(module, "AITUNNEL_KEY", "test-aitunnel-key"), \
                 patch.object(module, "begin_aitunnel_request", return_value=True) as begin, \
                 patch.object(module, "retry_delay", return_value=0), \
                 patch.object(module.httpx, "Client", return_value=client):
                result = module.aitunnel_result(row)
            self.assertEqual(result, folder / "generated.png")
            self.assertEqual(len(calls), 2)
            self.assertEqual(str(calls[0].url), module.AITUNNEL_URL)
            self.assertEqual(str(calls[1].url), module.AITUNNEL_MIRROR_URL)
            begin.assert_called_once_with("job")

    def test_aitunnel_http_504_retries_on_mirror(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "job"
            folder.mkdir()
            Image.new("RGB", (2, 2), "blue").save(folder / "input.png")
            row = {"id": "job", "prompt": "Edit", "width_cm": 40,
                   "height_cm": 60, "vendor_image_urls": None,
                   "vendor_image_url": None, "input_count": 1}
            calls = []
            image = io.BytesIO()
            Image.new("RGB", (3, 4), "green").save(image, "PNG")
            def gateway_timeout(request):
                calls.append(request)
                if len(calls) == 1:
                    return httpx.Response(504, text="Gateway Timeout")
                return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image.getvalue()).decode()}]})
            client = httpx.Client(transport=httpx.MockTransport(gateway_timeout))
            with patch.object(module, "DATA", Path(root)), \
                 patch.object(module, "AITUNNEL_KEY", "test-aitunnel-key"), \
                 patch.object(module, "begin_aitunnel_request", return_value=True), \
                 patch.object(module, "retry_delay", return_value=0), \
                 patch.object(module.httpx, "Client", return_value=client):
                self.assertEqual(module.aitunnel_result(row), folder / "generated.png")
            self.assertEqual([str(call.url) for call in calls],
                             [module.AITUNNEL_URL, module.AITUNNEL_MIRROR_URL])
            self.assertTrue((folder / "generated.png").exists())

    def test_aitunnel_http_504_stops_after_three_attempts(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "job"
            folder.mkdir()
            Image.new("RGB", (2, 2), "blue").save(folder / "input.png")
            row = {"id": "job", "prompt": "Edit", "width_cm": 40,
                   "height_cm": 60, "vendor_image_urls": None,
                   "vendor_image_url": None, "input_count": 1}
            calls = []
            def gateway_timeout(request):
                calls.append(request)
                return httpx.Response(504, text="Gateway Timeout")
            client = httpx.Client(transport=httpx.MockTransport(gateway_timeout))
            with patch.object(module, "DATA", Path(root)), \
                 patch.object(module, "AITUNNEL_KEY", "test-aitunnel-key"), \
                 patch.object(module, "begin_aitunnel_request", return_value=True), \
                 patch.object(module, "retry_delay", return_value=0), \
                 patch.object(module.httpx, "Client", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "HTTP 504 после 3 попыток"):
                    module.aitunnel_result(row)
            self.assertEqual([str(call.url) for call in calls],
                             [module.AITUNNEL_URL, module.AITUNNEL_MIRROR_URL,
                              module.AITUNNEL_MIRROR_URL])
            self.assertFalse((folder / "generated.png").exists())

    def test_aitunnel_other_5xx_does_not_retry(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "job"
            folder.mkdir()
            Image.new("RGB", (2, 2), "blue").save(folder / "input.png")
            row = {"id": "job", "prompt": "Edit", "width_cm": 40,
                   "height_cm": 60, "vendor_image_urls": None,
                   "vendor_image_url": None, "input_count": 1}
            calls = []
            def bad_gateway(request):
                calls.append(request)
                return httpx.Response(502, text="Bad Gateway")
            client = httpx.Client(transport=httpx.MockTransport(bad_gateway))
            with patch.object(module, "DATA", Path(root)), \
                 patch.object(module, "AITUNNEL_KEY", "test-aitunnel-key"), \
                 patch.object(module, "begin_aitunnel_request", return_value=True), \
                 patch.object(module.httpx, "Client", return_value=client):
                with self.assertRaisesRegex(module.AIRequestOutcomeUnknown, "HTTP 502"):
                    module.aitunnel_result(row)
            self.assertEqual([str(call.url) for call in calls], [module.AITUNNEL_URL])

    def test_aitunnel_rejects_nonofficial_base_before_paid_request(self):
        from folio_worker import app as module
        with patch.object(module, "PROVIDER", "aitunnel"), \
             patch.object(module, "AITUNNEL_KEY", "test-key"), \
             patch.object(module, "AITUNNEL_BASE_URL", "https://example.invalid/v1"):
            self.assertIn("official AI Tunnel", module.provider_error())

    def test_uncertain_generation_pauses_worker_job(self):
        from folio_worker import app as module
        row = {"id": "job", "status": "queued", "provider": "aitunnel", "folio_job_id": 12}
        changes = []
        with patch.object(module, "get_job", return_value=row), \
             patch.object(module, "update", side_effect=lambda _job_id, **fields: changes.append(fields)), \
             patch.object(module, "aitunnel_result", side_effect=module.AIRequestOutcomeUnknown("HTTP 502")):
            module.process(row)
        self.assertEqual([change["status"] for change in changes], ["generating", "unknown"])
        self.assertEqual(changes[-1]["error"], "HTTP 502")

    def test_aitunnel_post_boundary_survives_restart(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "jobs.sqlite3"
            with patch.object(module, "DB", database):
                with module.connection() as con:
                    con.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, provider TEXT, aitunnel_request_started_at TEXT)")
                    con.execute("INSERT INTO jobs(id,provider) VALUES('job','aitunnel')")
                self.assertTrue(module.begin_aitunnel_request("job"))
                self.assertFalse(module.begin_aitunnel_request("job"))
                with module.connection() as con:
                    self.assertIsNotNone(con.execute(
                        "SELECT aitunnel_request_started_at FROM jobs WHERE id='job'"
                    ).fetchone()[0])

    def test_print_margins_follow_longer_side(self):
        from folio_worker import app as module
        self.assertEqual(module.print_margins_cm(40, 60), (2.5, 3.5))
        self.assertEqual(module.print_margins_cm(60, 60), (2.5, 3.5))
        self.assertEqual(module.print_margins_cm(40, 60.1), (4.5, 5.0))
        self.assertEqual(module.print_margins_cm(40, 90), (4.5, 5.0))
        self.assertEqual(module.print_margins_cm(40, 90.1), (4.5, 7.0))
        self.assertEqual(module.print_margins_cm(40, 91), (4.5, 7.0))

    def test_mirror_repeats_edges_on_all_sides_and_corners(self):
        from folio_worker import app as module
        page = Image.new("RGB", (3, 2))
        colors = [
            (255, 0, 0), (0, 255, 0), (0, 0, 255),
            (255, 255, 0), (255, 0, 255), (0, 255, 255),
        ]
        page.putdata(colors)
        mirrored = Image.new("RGB", (9, 6), "white")
        module._paste_mirrored(mirrored, page, 0, 0, 4, 3, 2, 1)
        try:
            self.assertEqual(mirrored.size, (9, 6))
            self.assertEqual([mirrored.getpixel((x, 3)) for x in range(9)],
                             [colors[x] for x in (2, 2, 1, 0, 0, 1, 2, 2, 1)])
            self.assertEqual(mirrored.getpixel((4, 2)), colors[0])
            self.assertEqual(mirrored.getpixel((0, 0)), colors[5])
            self.assertEqual(mirrored.getpixel((8, 5)), colors[4])
        finally:
            mirrored.close()
            page.close()

    def test_40_by_60_print_is_52_by_72_with_unchanged_preview(self):
        from folio_worker import app as module
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            folder = data / "job"
            folder.mkdir(parents=True)
            source = Path(root) / "source.png"
            Image.new("RGB", (400, 600), "blue").save(source)
            row = {"id": "job", "width_cm": 40, "height_cm": 60,
                   "pixel_width": module.image_pixels(40),
                   "pixel_height": module.image_pixels(60)}
            with patch.object(module, "DATA", data):
                output = module.compose(row, source)
            with Image.open(output) as printed:
                self.assertEqual(printed.size, (module.image_pixels(52), module.image_pixels(72)))
                self.assertEqual(tuple(round(value) for value in printed.info["dpi"]), (150, 150))
                self.assertEqual(printed.getpixel((0, 0)), (255, 255, 255))
                self.assertEqual(printed.getpixel((printed.width // 2, printed.height // 2)), (0, 0, 255))
                white_left = (printed.width - module.image_pixels(45)) // 2
                white_top = (printed.height - module.image_pixels(65)) // 2
                self.assertEqual(printed.getpixel((white_left - 1, printed.height // 2)), (255, 255, 255))
                self.assertEqual(printed.getpixel((white_left, printed.height // 2)), (0, 0, 255))
                self.assertEqual(printed.getpixel((printed.width // 2, white_top - 1)), (255, 255, 255))
                self.assertEqual(printed.getpixel((printed.width // 2, white_top)), (0, 0, 255))
            with Image.open(folder / "preview.jpg") as preview:
                self.assertEqual(max(preview.size), 1000)
                self.assertEqual(preview.size, (667, 1000))

    def test_topaz_only_when_long_side_is_short_by_3000_pixels(self):
        from folio_worker import app as module
        self.assertFalse(module.needs_topaz((4096, 2731), (5906, 5906)))
        self.assertFalse(module.needs_topaz((1000, 1000), (3999, 1000)))
        self.assertTrue(module.needs_topaz((1000, 1000), (4000, 1000)))

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "generated.png"
            Image.new("RGB", (1000, 1000), "blue").save(source)
            spool = root / "spool"
            output = spool / "upscale" / "upscaled.png"
            output.parent.mkdir(parents=True)
            Image.new("RGB", (4000, 1000), "blue").save(output)
            with patch.object(module, "SPOOL", spool), patch.object(module, "update", lambda *_args, **_kwargs: None), \
                 patch.object(module, "STOP", threading.Event()):
                self.assertEqual(module.topaz_result({"id": "skip", "pixel_width": 3999,
                                                       "pixel_height": 1000}, source), source)
                self.assertFalse((spool / "skip").exists())
                self.assertEqual(module.topaz_result({"id": "upscale", "pixel_width": 4000,
                                                       "pixel_height": 1000}, source), output)
                request = json.loads((spool / "upscale" / "request.json").read_text())
                self.assertEqual(request["scale"], 4)

    def test_nano_retries_connection_failures_without_repeating_ambiguous_post(self):
        from folio_worker import app as module

        class NoWait:
            def is_set(self): return False
            def wait(self, _seconds): return False

        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) < 3:
                raise httpx.ConnectError("DNS unavailable", request=request)
            return httpx.Response(200, json={"code": 200})

        with patch.object(module, "STOP", NoWait()), httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(module.nano_http(client, "POST", "https://api.example.test/generate").status_code, 200)
        self.assertEqual(len(calls), 3)

        ambiguous = []
        def timeout(request):
            ambiguous.append(request)
            raise httpx.ReadTimeout("response lost", request=request)

        with patch.object(module, "STOP", NoWait()), httpx.Client(transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                module.nano_http(client, "POST", "https://api.example.test/generate")
        self.assertEqual(len(ambiguous), 1)

    def test_folio_pull_and_result_upload(self):
        from folio_worker import app as module
        real_report_loop = module.report_loop
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            with patch.object(module, "DATA", root / "data"), patch.object(module, "SPOOL", root / "spool"), \
                 patch.object(module, "DB", root / "data" / "jobs.sqlite3"), \
                 patch.object(module, "FOLIO_BASE", "https://one.example.ru"), \
                 patch.object(module, "FOLIO_POLL_URL", "https://one.example.ru/folio/image-worker/next"), \
                 patch.object(module, "FOLIO_HOST", "one.example.ru"), \
                 patch.object(module, "API_KEY", "pull-key"), \
                 patch.object(module, "PROVIDER", "mock"), \
                 patch.object(module, "work_loop", lambda: module.STOP.wait(30)), \
                 patch.object(module, "report_loop", lambda: module.STOP.wait(30)):
                with TestClient(module.app):
                    worker_id = str(uuid.uuid4())
                    job = {"id": 23, "worker_id": worker_id, "prompt": "Новый фон",
                           "width_cm": 4, "height_cm": 4,
                           "image_urls": ["https://one.example.ru/folio/image-worker/input/token/0"]}
                    image = io.BytesIO()
                    Image.new("RGB", (300, 300), "blue").save(image, "PNG")

                    class FakeResponse:
                        def __enter__(self): return self
                        def __exit__(self, *_): pass
                        def raise_for_status(self): pass
                        def iter_bytes(self): yield image.getvalue()

                    class FakeClient:
                        def __init__(self, **_): pass
                        def __enter__(self): return self
                        def __exit__(self, *_): pass
                        def stream(self, *_): return FakeResponse()

                    def fetch(url, **kwargs):
                        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer pull-key", "X-Folio-Image-Models": "1", "X-Folio-Prompt-Planner": "1", "X-Folio-Image-Provider": "mock"})
                        return httpx.Response(200, json=job, request=httpx.Request("GET", url))

                    with patch.object(module.httpx, "get", fetch), patch.object(module.httpx, "Client", FakeClient):
                        self.assertTrue(module.pull_one())
                        self.assertFalse(module.pull_one())
                    self.assertEqual(module.get_job(worker_id)["folio_job_id"], 23)
                    self.assertEqual(module.get_job(worker_id)["provider"], "mock")
                    self.assertTrue((root / "data" / worker_id / "input.png").exists())
                    Image.new("RGB", (1000, 667), "blue").save(root / "data" / worker_id / "preview.jpg", "JPEG")
                    module.update(worker_id, status="completed", print_file="local.tif")
                    sent = []

                    def upload(url, **kwargs):
                        sent.append((url, kwargs))
                        module.STOP.set()
                        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

                    with patch.object(module.httpx, "post", upload):
                        real_report_loop()
                    self.assertEqual(sent[0][0], "https://one.example.ru/folio/image-worker/jobs/23")
                    self.assertEqual(sent[0][1]["data"]["worker_id"], worker_id)
                    self.assertEqual(sent[0][1]["files"]["preview"][2], "image/jpeg")
                    self.assertEqual(module.get_job(worker_id)["reported_status"], "completed")

    def test_stale_or_failing_report_does_not_block_next_preview(self):
        from folio_worker import app as module
        for http_status in (409, 503):
            with self.subTest(http_status=http_status), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                (root / "ready").mkdir()
                Image.new("RGB", (1000, 667), "blue").save(root / "ready" / "preview.jpg", "JPEG")
                with patch.object(module, "DATA", root), patch.object(module, "DB", root / "jobs.sqlite3"):
                    with module.connection() as con:
                        con.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,folio_job_id INTEGER,status TEXT,"
                                    "reported_status TEXT,created_at TEXT,print_file TEXT,error TEXT,"
                                    "report_attempts INTEGER DEFAULT 0,report_next_at TEXT,"
                                    "report_blocked_status TEXT,report_error TEXT)")
                        con.execute("INSERT INTO jobs(id,folio_job_id,status,created_at) VALUES('stale',16,'unknown','1')")
                        con.execute("INSERT INTO jobs(id,folio_job_id,status,created_at,print_file) "
                                    "VALUES('ready',17,'completed','2','print.tif')")
                    sent = []
                    def upload(url, **kwargs):
                        sent.append(kwargs["data"]["worker_id"])
                        if sent[-1] == "stale":
                            return httpx.Response(http_status, json={"detail": "image_worker_result_conflict"},
                                                  request=httpx.Request("POST", url))
                        self.assertEqual(kwargs["files"]["preview"][2], "image/jpeg")
                        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))
                    with patch.object(module.httpx, "post", upload):
                        self.assertTrue(module.report_one())
                        self.assertTrue(module.report_one())
                        self.assertFalse(module.report_one())
                    self.assertEqual(sent, ["stale", "ready"])
                    self.assertIsNone(module.get_job("stale")["reported_status"])
                    self.assertEqual(module.get_job("ready")["reported_status"], "completed")
                    stale = module.get_job("stale")
                    self.assertEqual(stale["report_blocked_status"], "unknown" if http_status == 409 else None)
                    self.assertEqual(stale["report_error"], f"HTTP {http_status}")

    def test_mock_job_to_print(self):
        with tempfile.TemporaryDirectory() as root:
            os.environ.update({
                "DATA_DIR": str(Path(root) / "data"),
                "SPOOL_DIR": str(Path(root) / "spool"),
                "PRINT_SHARE_PATH": r"\\server\prints",
                "WORKER_API_KEY": "test-key",
                "PUBLIC_BASE_URL": "https://worker.example.com",
                "FOLIO_PUBLIC_BASE_URL": "https://one.example.ru",
                "NANOBANANA_MODE": "mock",
            })
            from folio_worker import app as module
            module = importlib.reload(module)

            self.assertTrue(module.NANO_CALLBACK_URL.startswith(
                "https://one.example.ru/folio/image-worker/vendor-callback/"))
            module.pull_one = lambda: False

            image = Image.new("RGB", (300, 300), "blue")
            payload = io.BytesIO()
            image.save(payload, "PNG")
            with TestClient(module.app) as client:
                response = client.post("/v1/jobs", headers={"Authorization": "Bearer test-key"},
                    data={"prompt": "Change background", "width_cm": "4", "height_cm": "4"},
                    files={"image": ("test.png", payload.getvalue(), "image/png")})
                self.assertEqual(response.status_code, 202, response.text)
                job_id = response.json()["id"]
                folder = Path(root) / "spool" / job_id
                deadline = time.monotonic() + 15
                while not (folder / "print_request.json").exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertTrue((folder / "print_request.json").exists())
                self.assertFalse((folder / "request.json").exists())
                from topaz_agent import upload_print
                share = Path(root) / "share"
                share.mkdir()
                upload_print(folder, share)
                while time.monotonic() < deadline:
                    status = client.get(f"/v1/jobs/{job_id}", headers={"Authorization": "Bearer test-key"}).json()
                    if status["status"] in ("completed", "failed"):
                        break
                    time.sleep(.1)
                self.assertEqual(status["status"], "completed", status)
                self.assertEqual(status["print_file"], str(share / f"{job_id}.tif"))
                with Image.open(share / f"{job_id}.tif") as print_image:
                    # 4 cm canvas + 2×(2.5 cm mirror + 3.5 cm white) = 16 cm.
                    self.assertEqual(print_image.size, (945, 945))
                    self.assertEqual(tuple(round(x) for x in print_image.info["dpi"]), (150, 150))
                    self.assertEqual(print_image.getpixel((0, 0)), (255, 255, 255))
                    self.assertEqual(print_image.getpixel((472, 472)), (0, 0, 255))
                preview = client.get(f"/v1/jobs/{job_id}/preview", headers={"Authorization": "Bearer test-key"})
                self.assertEqual(preview.status_code, 200)
                with Image.open(io.BytesIO(preview.content)) as preview_image:
                    self.assertEqual(max(preview_image.size), 1000)

                local = Path(root) / "local" / job_id
                local.mkdir(parents=True)
                (local / "print_request.json").write_text(json.dumps({"job_id": job_id}))
                (local / "print.tif").write_bytes((share / f"{job_id}.tif").read_bytes())
                upload_print(local, None)
                self.assertEqual((local / "print_uploaded.ok").read_text(), str((local / "print.tif").resolve()))

                class FakeResponse:
                    def __enter__(self): return self
                    def __exit__(self, *_): pass
                    def raise_for_status(self): pass
                    def iter_bytes(self): yield payload.getvalue()

                class FakeClient:
                    def __init__(self, **_): pass
                    def __enter__(self): return self
                    def __exit__(self, *_): pass
                    def stream(self, *_): return FakeResponse()

                module.PUBLIC_BASE = ""
                module.INPUT_HOSTS = {"images.example.com"}
                module.NANO_CALLBACK_URL = "https://example.com/nano-callback"
                with patch.object(module.httpx, "Client", FakeClient):
                    local = client.post("/v1/jobs", headers={"Authorization": "Bearer test-key"},
                        data={"image_url": "https://images.example.com/photo.png", "prompt": "Restyle",
                              "width_cm": "4", "height_cm": "4"})
                self.assertEqual(local.status_code, 202, local.text)
                self.assertEqual(module.get_job(local.json()["id"])["vendor_image_url"],
                                 "https://images.example.com/photo.png")
                single_endpoint, single_request = module.nano_request(module.get_job(local.json()["id"]))
                self.assertEqual(single_endpoint, "generate-pro")
                self.assertEqual(single_request["resolution"], "4K")
                self.assertEqual(single_request["aspectRatio"], "1:1")
                self.assertEqual(module.nano_aspect_ratio(40, 60), "2:3")
                self.assertEqual(module.nano_aspect_ratio(60, 40), "3:2")

                def multi_request(count):
                    fields = [("image_url", f"https://images.example.com/{i}.png") for i in range(count)]
                    fields += [("prompt", "Make one collage"), ("width_cm", "4"),
                               ("height_cm", "4")]
                    with patch.object(module.httpx, "Client", FakeClient):
                        return client.post("/v1/jobs", headers={"Authorization": "Bearer test-key",
                            "Content-Type": "application/x-www-form-urlencoded"}, content=urlencode(fields))

                eight = multi_request(8)
                self.assertEqual(eight.status_code, 202, eight.text)
                row = module.get_job(eight.json()["id"])
                self.assertEqual(row["input_count"], 8)
                endpoint, request = module.nano_request(row)
                self.assertEqual(endpoint, "generate-pro")
                self.assertEqual(len(request["imageUrls"]), 8)
                self.assertEqual(request["resolution"], "4K")
                self.assertEqual(request["aspectRatio"], "1:1")
                self.assertEqual(multi_request(9).status_code, 422)


if __name__ == "__main__":
    unittest.main()
