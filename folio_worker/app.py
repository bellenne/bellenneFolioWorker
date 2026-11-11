"""HTTP contract and durable single-worker image pipeline."""
from __future__ import annotations

import hashlib
import hmac
import base64
import binascii
import io
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from folio_worker import __version__

DATA = Path(os.getenv("DATA_DIR", "data"))
SPOOL = Path(os.getenv("SPOOL_DIR", "spool"))
PRINT_SHARE_PATH = os.getenv("PRINT_SHARE_PATH", "").strip()
DB = DATA / "jobs.sqlite3"
API_KEY = os.getenv("WORKER_API_KEY", "")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
POLL_PATH = "/folio/image-worker/next"
FOLIO_POLL_URL = os.getenv("FOLIO_POLL_URL", "").strip()
FOLIO_BASE = (FOLIO_POLL_URL[:-len(POLL_PATH)] if FOLIO_POLL_URL.endswith(POLL_PATH)
              else os.getenv("FOLIO_PUBLIC_BASE_URL", "").rstrip("/"))
if not FOLIO_POLL_URL:
    FOLIO_POLL_URL = f"{FOLIO_BASE}{POLL_PATH}"
FOLIO_HOST = (urlparse(FOLIO_BASE).hostname or "").lower()
NANO_KEY = os.getenv("NANOBANANA_API_KEY", "")
MODE = os.getenv("NANOBANANA_MODE", "live")
PROVIDER = os.getenv("IMAGE_PROVIDER", "mock" if MODE == "mock" else "nanobanana").strip().lower()
AITUNNEL_KEY = os.getenv("AITUNNEL_API_KEY", "")
AITUNNEL_MODEL = "hy-image-v3.5-preview"
AITUNNEL_MODELS = {
    "flux-3-image", "gemini-nano-banana-2.1", "seedream-5-0-pro", AITUNNEL_MODEL,
    "seedream-5-0-lite", "seedream-5-0-flash", "seedream-4.5", "flux-3-image-klein",
}
AITUNNEL_PRIMARY_BASE = "https://api.aitunnel.ru/v1"
AITUNNEL_MIRROR_BASE = "https://ru-api.aitunnel.ru/v1"
AITUNNEL_BASE_URL = os.getenv("AITUNNEL_BASE_URL", AITUNNEL_PRIMARY_BASE).rstrip("/")
AITUNNEL_URL = f"{AITUNNEL_BASE_URL}/images/generations"
AITUNNEL_MIRROR_URL = f"{AITUNNEL_MIRROR_BASE}/images/generations"
AITUNNEL_CONNECT_TIMEOUT = int(os.getenv("AITUNNEL_CONNECT_TIMEOUT_SECONDS", "60"))
AITUNNEL_READ_TIMEOUT = int(os.getenv("AITUNNEL_READ_TIMEOUT_SECONDS", "3600"))
MAX_BYTES = int(os.getenv("MAX_UPLOAD_MB", "30")) * 1024 * 1024
NANO_TIMEOUT = int(os.getenv("NANO_TIMEOUT_SECONDS", "3600"))
INPUT_HOSTS = {x.strip().lower() for x in os.getenv("INPUT_ALLOWED_HOSTS", "").split(",") if x.strip()} or {FOLIO_HOST}
VENDOR_TOKEN = hmac.new(API_KEY.encode(), b"folio-nanobanana-callback", hashlib.sha256).hexdigest() if API_KEY else ""
NANO_CALLBACK_URL = os.getenv("NANOBANANA_CALLBACK_URL") or (
    f"{FOLIO_BASE}/folio/image-worker/vendor-callback/{VENDOR_TOKEN}" if FOLIO_BASE and VENDOR_TOKEN else ""
)
NANO_BASE = "https://api.nanobananaapi.ai/api/v1/nanobanana"
STOP = threading.Event()
SESSION_ID = str(uuid.uuid4())
INSTALLATION_ID: str | None = None


class AIRequestOutcomeUnknown(RuntimeError):
    """The billable POST may have reached a model; no automatic replay."""


class PlacementLeaseLost(RuntimeError):
    """Do not write or report against a stale placement claim."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, *, job_id: str | None = None, **fields) -> None:
    """Log operational metadata only; never log prompts, URLs or API keys."""
    details = [f"{key}={value}" for key, value in fields.items() if value is not None]
    prefix = f" job={job_id}" if job_id else ""
    suffix = " " + " ".join(details) if details else ""
    local_time = datetime.now(timezone(timedelta(hours=3))).isoformat(timespec="seconds")
    print(f"{local_time} |{prefix} {event}{suffix}", flush=True)


def retry_delay(attempt: int) -> int:
    return min(5 * 2 ** min(attempt - 1, 4), 60)


def network_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {str(exc)[:180]}"


def installation_id() -> str:
    global INSTALLATION_ID
    if INSTALLATION_ID:
        return INSTALLATION_ID
    path = DATA.parent / "worker-installation-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            with path.open("x", encoding="ascii") as stream:
                stream.write(str(uuid.uuid4()))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
    INSTALLATION_ID = str(uuid.UUID(path.read_text(encoding="ascii").strip()))
    return INSTALLATION_ID


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


@contextmanager
def connection():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def update(job_id: str, **fields):
    fields["updated_at"] = now()
    if "status" in fields:
        fields.update(report_attempts=0, report_next_at=None,
                      report_blocked_status=None, report_error=None)
    with connection() as con:
        con.execute(
            f"UPDATE jobs SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
            (*fields.values(), job_id),
        )
    if "status" in fields:
        log_event("status", job_id=job_id, value=fields["status"])


def get_job(job_id: str):
    with connection() as con:
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def require_key(authorization: str | None):
    if not API_KEY or not authorization or not hmac.compare_digest(authorization, f"Bearer {API_KEY}"):
        raise HTTPException(401, "Invalid worker API key")


def public_job(row):
    result = {
        "id": row["id"], "status": row["status"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "error": row["error"],
        "width_cm": row["width_cm"], "height_cm": row["height_cm"],
        "dpi": 150, "pixel_width": row["pixel_width"], "pixel_height": row["pixel_height"],
        "input_count": row["input_count"], "preview_url": None, "print_file": row["print_file"],
    }
    if row["status"] == "completed":
        result["preview_url"] = f"{PUBLIC_BASE}/v1/jobs/{row['id']}/preview" if PUBLIC_BASE else f"/v1/jobs/{row['id']}/preview"
    return result


def image_pixels(cm: float) -> int:
    return round(cm / 2.54 * 150)


def provider_error() -> str | None:
    if PROVIDER == "aitunnel":
        if not AITUNNEL_KEY:
            return "AITUNNEL_API_KEY is not configured"
        if AITUNNEL_BASE_URL not in {AITUNNEL_PRIMARY_BASE, AITUNNEL_MIRROR_BASE}:
            return "AITUNNEL_BASE_URL must be an official AI Tunnel API host"
        if AITUNNEL_CONNECT_TIMEOUT <= 0 or AITUNNEL_READ_TIMEOUT <= 0:
            return "AI Tunnel timeouts must be positive"
        return None
    if PROVIDER == "mock":
        return None
    if PROVIDER != "nanobanana":
        return "Unsupported IMAGE_PROVIDER"
    if MODE == "live" and not NANO_KEY:
        return "Nano Banana key is required"
    if MODE == "live" and NANO_CALLBACK_URL and not NANO_CALLBACK_URL.startswith("https://"):
        return "Nano Banana callback URL must use HTTPS"
    if MODE == "live" and not (NANO_CALLBACK_URL or PUBLIC_BASE.startswith("https://")):
        return "A public Nano Banana callback URL is required"
    return None


LABEL_MARGIN_CM = 0.5
LABEL_FONT_CM = 0.5  # about 14 pt
LABEL_FONTS = (
    str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def print_margins_cm(width_cm: float, height_cm: float) -> tuple[float, float]:
    """Return mirror and white margins per side from the longer canvas side."""
    longest = max(width_cm, height_cm)
    if longest <= 60:
        return 2.5, 3.5
    if longest <= 90:
        return 4.5, 5.0
    return 4.5, 7.0


def label_text(value) -> str:
    """One printable line: an order field must not break the print sheet."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())[:255]


def _label_font(size: int) -> ImageFont.FreeTypeFont:
    # Pillow's built-in font has no Cyrillic, and articles can contain it.
    for path in LABEL_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise RuntimeError("Не найден шрифт для подписи на печатном макете")


def draw_print_label(sheet: Image.Image, text: str, *, bottom: bool) -> None:
    """Centre the text on a white band, LABEL_MARGIN_CM from the sheet edge."""
    draw = ImageDraw.Draw(sheet)
    margin = image_pixels(LABEL_MARGIN_CM)
    size = image_pixels(LABEL_FONT_CM)
    while True:
        font = _label_font(size)
        left, top, right, low = draw.textbbox((0, 0), text, font=font)
        # A long article gets smaller rather than cut: it must stay readable in full.
        if right - left <= sheet.width - 2 * margin or size == 1:
            break
        size -= 1
    y = sheet.height - margin - low if bottom else margin - top
    draw.text(((sheet.width - (right - left)) // 2 - left, y), text, font=font, fill="black")


def _split_padding(inner: int, outer: int) -> tuple[int, int]:
    padding = outer - inner
    return padding // 2, padding - padding // 2


def _paste_mirrored(canvas: Image.Image, page: Image.Image, x: int, y: int,
                    left: int, top: int, right: int, bottom: int) -> None:
    """Paste the page and mirrored edges directly onto the final print sheet."""
    center_x, center_y = x + left, y + top
    canvas.paste(page, (center_x, center_y))

    def paste_part(part: Image.Image, position: tuple[int, int], flip: str | None) -> None:
        try:
            if flip:
                reflected = ImageOps.mirror(part) if flip == "horizontal" else ImageOps.flip(part)
                try:
                    canvas.paste(reflected, position)
                finally:
                    reflected.close()
            else:
                canvas.paste(part, position)
        finally:
            part.close()

    # First fill both horizontal edges of the visible canvas. For an unusually
    # small painting, alternate mirrored and original strips until covered.
    for margin, before in ((left, True), (right, False)):
        remaining = margin
        position = center_x if before else center_x + page.width
        reflected = True
        while remaining:
            count = min(remaining, page.width)
            if before:
                part = page.crop((0 if reflected else page.width - count, 0,
                                  count if reflected else page.width, page.height))
                position -= count
            else:
                part = page.crop((page.width - count if reflected else 0, 0,
                                  page.width if reflected else count, page.height))
            paste_part(part, (position, center_y), "horizontal" if reflected else None)
            if not before:
                position += count
            remaining -= count
            reflected = not reflected

    # Reflect the entire horizontal band vertically; this also mirrors corners.
    band_width = left + page.width + right
    for margin, before in ((top, True), (bottom, False)):
        remaining = margin
        position = center_y if before else center_y + page.height
        reflected = True
        while remaining:
            count = min(remaining, page.height)
            if before:
                source_y = center_y if reflected else center_y + page.height - count
                position -= count
            else:
                source_y = center_y + page.height - count if reflected else center_y
            part = canvas.crop((x, source_y, x + band_width, source_y + count))
            paste_part(part, (x, position), "vertical" if reflected else None)
            if not before:
                position += count
            remaining -= count
            reflected = not reflected


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for path in (DATA, SPOOL):
        path.mkdir(parents=True, exist_ok=True)
    with connection() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            prompt TEXT NOT NULL, width_cm REAL NOT NULL, height_cm REAL NOT NULL,
            pixel_width INTEGER NOT NULL, pixel_height INTEGER NOT NULL,
            input_token TEXT NOT NULL, callback_token TEXT NOT NULL, webhook_url TEXT NOT NULL,
            provider_task_id TEXT, print_file TEXT, error TEXT, vendor_image_url TEXT,
            vendor_image_urls TEXT, input_count INTEGER NOT NULL DEFAULT 1,
            folio_job_id INTEGER UNIQUE, reported_status TEXT,
            provider TEXT NOT NULL DEFAULT 'nanobanana',
            model TEXT NOT NULL DEFAULT 'hy-image-v3.5-preview',
            aitunnel_request_started_at TEXT)""")
        columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        if "vendor_image_url" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN vendor_image_url TEXT")
        if "vendor_image_urls" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN vendor_image_urls TEXT")
        if "input_count" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN input_count INTEGER NOT NULL DEFAULT 1")
        if "folio_job_id" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN folio_job_id INTEGER")
        if "reported_status" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN reported_status TEXT")
        if "provider" not in columns:
            # In-flight jobs created before the migration must finish with the
            # provider that created their durable provider_task_id.
            con.execute("ALTER TABLE jobs ADD COLUMN provider TEXT NOT NULL DEFAULT 'nanobanana'")
        if "aitunnel_request_started_at" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN aitunnel_request_started_at TEXT")
        if "prompt_mode" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN prompt_mode TEXT NOT NULL DEFAULT 'direct'")
        if "model" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN model TEXT NOT NULL DEFAULT 'hy-image-v3.5-preview'")
        for name, definition in (("report_attempts", "INTEGER NOT NULL DEFAULT 0"),
                                 ("report_next_at", "TEXT"), ("report_blocked_status", "TEXT"),
                                 ("report_error", "TEXT"), ("print_size", "INTEGER"),
                                 ("print_sha256", "TEXT"),
                                 ("skip_generation", "INTEGER NOT NULL DEFAULT 0"),
                                 ("offer_id", "TEXT NOT NULL DEFAULT ''"),
                                 ("order_number", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_folio_job_id ON jobs(folio_job_id)")
        con.execute("""CREATE TABLE IF NOT EXISTS placement_manifest (
            operation_id TEXT NOT NULL, serial INTEGER NOT NULL, job_id TEXT NOT NULL,
            layout_version TEXT NOT NULL, filename TEXT NOT NULL, saved_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY(operation_id,serial))""")
        con.execute("""CREATE TABLE IF NOT EXISTS placement_reports (
            operation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT, blocked INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS placement_binding (
            job_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
            layout_version TEXT NOT NULL, source_sha256 TEXT NOT NULL)""")
    installation_id()
    STOP.clear()
    log_event("worker starting", provider=PROVIDER, model=AITUNNEL_MODEL if PROVIDER == "aitunnel" else None,
              folio_host=FOLIO_HOST)
    threading.Thread(target=work_loop, daemon=True, name="folio-worker").start()
    threading.Thread(target=report_loop, daemon=True, name="folio-reporter").start()
    try:
        yield
    finally:
        STOP.set()


app = FastAPI(title="FolioImageWorker", version=__version__, lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True, "provider": PROVIDER, "generation_ready": provider_error() is None}


@app.post("/v1/jobs", status_code=202)
async def create_job(
    image: list[UploadFile] | None = File(default=None), image_url: list[str] | None = Form(default=None),
    prompt: str = Form(...), width_cm: float = Form(...),
    height_cm: float = Form(...), webhook_url: str = Form(""), model: str = Form(AITUNNEL_MODEL),
    offer_id: str = Form(""), order_number: str = Form(""),
    authorization: str | None = Header(default=None),
):
    require_key(authorization)
    if not 1 <= width_cm <= 300 or not 1 <= height_cm <= 300:
        raise HTTPException(422, "Dimensions must be 1..300 cm")
    if image_pixels(width_cm) * image_pixels(height_cm) > 100_000_000:
        raise HTTPException(422, "Print layout exceeds 100 million pixels")
    if not prompt.strip() or len(prompt) > 4000:
        raise HTTPException(422, "prompt must contain 1..4000 characters")
    if bool(image) == bool(image_url):
        raise HTTPException(422, "Provide images or image_urls, not both")
    count = len(image or image_url or [])
    if not 1 <= count <= 8:
        raise HTTPException(422, "Provide 1..8 images")
    if error := provider_error():
        raise HTTPException(503, error)
    if model not in AITUNNEL_MODELS or (PROVIDER not in {"aitunnel", "mock"} and model != AITUNNEL_MODEL):
        raise HTTPException(422, "Unsupported image model for this worker provider")
    if PROVIDER == "nanobanana" and MODE == "live" and image and not PUBLIC_BASE.startswith("https://"):
        raise HTTPException(503, "Uploaded images require a public HTTPS worker URL")
    blobs = []
    if image_url:
        for url in image_url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.hostname.lower() not in INPUT_HOSTS:
                raise HTTPException(422, "image_url must be public HTTPS on an allowed host")
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                for url in image_url:
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise HTTPException(413, "Image too large")
                            chunks.append(chunk)
                    blobs.append(b"".join(chunks))
        except httpx.HTTPError:
            raise HTTPException(422, "Could not download image_url")
    else:
        blobs = [await item.read(MAX_BYTES + 1) for item in image]
    for blob in blobs:
        if len(blob) > MAX_BYTES:
            raise HTTPException(413, "Image too large")
        try:
            with Image.open(io.BytesIO(blob)) as im:
                im.verify()
            with Image.open(io.BytesIO(blob)) as im:
                if im.format not in ("PNG", "JPEG", "WEBP"):
                    raise HTTPException(415, "Use PNG, JPEG or WebP")
                if im.width * im.height > 100_000_000:
                    raise HTTPException(413, "Too many pixels")
        except (UnidentifiedImageError, OSError):
            raise HTTPException(415, "Invalid image")
    job_id = str(uuid.uuid4())
    job_dir = DATA / job_id
    job_dir.mkdir()
    for index, blob in enumerate(blobs):
        with Image.open(io.BytesIO(blob)) as im:
            normalized = ImageOps.exif_transpose(im).convert("RGB")
            suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[im.format]
            (job_dir / f"source-{index + 1}{suffix}").write_bytes(blob)
            normalized.save(job_dir / ("input.png" if index == 0 else f"input-{index}.png"), "PNG")
            log_event("input stored", job_id=job_id, image=index + 1,
                      image_px=f"{normalized.width}x{normalized.height}", bytes=len(blob))
    created = now()
    with connection() as con:
        con.execute("""INSERT INTO jobs (id,status,created_at,updated_at,prompt,width_cm,height_cm,
            pixel_width,pixel_height,input_token,callback_token,webhook_url,provider_task_id,
            print_file,error,vendor_image_url,vendor_image_urls,input_count,provider,model)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            job_id, "queued", created, created, prompt.strip(), width_cm, height_cm,
            image_pixels(width_cm), image_pixels(height_cm), secrets.token_urlsafe(32),
            secrets.token_urlsafe(32), webhook_url, None, None, None,
            image_url[0] if image_url else None, json.dumps(image_url) if image_url else None, count, PROVIDER, model,
        ))
        con.execute("UPDATE jobs SET offer_id=?,order_number=? WHERE id=?",
                    (label_text(offer_id), label_text(order_number), job_id))
    log_event("job accepted", job_id=job_id, source="api", provider=PROVIDER,
              images=count, print_px=f"{image_pixels(width_cm)}x{image_pixels(height_cm)}")
    return public_job(get_job(job_id))


def folio_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}",
            "X-Folio-Worker-Id": installation_id(), "X-Folio-Session-Id": SESSION_ID}


def pull_one() -> bool:
    parsed_poll = urlparse(FOLIO_POLL_URL)
    if (parsed_poll.scheme != "https" or parsed_poll.path != POLL_PATH
            or parsed_poll.query or parsed_poll.fragment or parsed_poll.username or parsed_poll.password
            or not API_KEY):
        return False
    response = httpx.get(FOLIO_POLL_URL,
                         headers={**folio_headers(), "X-Folio-Image-Models": "1", "X-Folio-Prompt-Planner": "1",
                                  "X-Folio-Image-Provider": PROVIDER}, timeout=20, follow_redirects=False)
    if response.status_code == 204:
        return False
    response.raise_for_status()
    job = response.json()
    action = job.get("action", "generate")
    if action == "place_print_files":
        process_placement(job)
        return True
    if action != "generate":
        raise ValueError("Folio returned an unknown action")
    if error := provider_error():
        raise RuntimeError(f"Невозможно принять генерацию: {error}")
    job_id = str(uuid.UUID(str(job["worker_id"])))
    folio_job_id = int(job["id"])
    if folio_job_id < 1:
        raise ValueError("Folio returned an invalid job ID")
    log_event("folio job claimed", job_id=job_id, folio_job=folio_job_id)
    existing = get_job(job_id)
    if existing:
        if existing["prompt_mode"] != str(job.get("prompt_mode", "direct")):
            raise ValueError("Folio returned a conflicting prompt mode")
        if existing["model"] != str(job.get("model") or AITUNNEL_MODEL):
            raise ValueError("Folio returned a conflicting image model for the same job")
        if existing["folio_job_id"] is None:
            with connection() as con:
                con.execute("UPDATE jobs SET folio_job_id=?,reported_status=NULL WHERE id=?",
                            (folio_job_id, job_id))
        elif existing["folio_job_id"] != folio_job_id:
            raise ValueError("Folio returned a conflicting job ID")
        # A job accepted before the upgrade still gets its labels if it is not printed yet.
        with connection() as con:
            con.execute("UPDATE jobs SET offer_id=?,order_number=? WHERE id=? AND print_file IS NULL",
                        (label_text(job.get("offer_id")), label_text(job.get("order_number")), job_id))
        log_event("job already accepted", job_id=job_id, folio_job=folio_job_id,
                  status=existing["status"])
        return False
    urls = job["image_urls"]
    width_cm, height_cm = float(job["width_cm"]), float(job["height_cm"])
    prompt = str(job["prompt"])
    model = str(job.get("model") or AITUNNEL_MODEL)
    prompt_mode = str(job.get("prompt_mode", "direct"))
    if prompt_mode not in {"direct", "vision"}:
        raise ValueError("Folio returned an invalid prompt mode")
    if (not isinstance(urls, list) or not 1 <= len(urls) <= 8
            or not 1 <= width_cm <= 300 or not 1 <= height_cm <= 300
            or image_pixels(width_cm) * image_pixels(height_cm) > 100_000_000
            or model not in AITUNNEL_MODELS
            or (PROVIDER not in {"aitunnel", "mock"} and model != AITUNNEL_MODEL)
            or not 1 <= len(prompt.strip()) <= 4000):
        raise ValueError("Folio returned invalid image job")
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != FOLIO_HOST or parsed.username or parsed.password:
            raise ValueError("Folio returned an invalid image URL")
    blobs = []
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for url in urls:
            with client.stream("GET", url) as image_response:
                image_response.raise_for_status()
                chunks, size = [], 0
                for chunk in image_response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("Folio image is too large")
                    chunks.append(chunk)
                blob = b"".join(chunks)
            if len(blob) > MAX_BYTES:
                raise ValueError("Folio image is too large")
            blobs.append(blob)
    folder = DATA / job_id
    folder.mkdir(exist_ok=True)
    for index, blob in enumerate(blobs):
        with Image.open(io.BytesIO(blob)) as image:
            image.verify()
        with Image.open(io.BytesIO(blob)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP"):
                raise ValueError("Folio image format is unsupported")
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
            (folder / f"source-{index + 1}{suffix}").write_bytes(blob)
            normalized.save(folder / ("input.png" if index == 0 else f"input-{index}.png"), "PNG")
            log_event("input stored", job_id=job_id, image=index + 1,
                      image_px=f"{normalized.width}x{normalized.height}", bytes=len(blob))
    stamp = now()
    with connection() as con:
        con.execute("""INSERT OR IGNORE INTO jobs (id,status,created_at,updated_at,prompt,width_cm,height_cm,
            pixel_width,pixel_height,input_token,callback_token,webhook_url,provider_task_id,
            print_file,error,vendor_image_url,vendor_image_urls,input_count,folio_job_id,reported_status,provider,model)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            job_id, "queued", stamp, stamp, prompt.strip(), width_cm, height_cm,
            image_pixels(width_cm), image_pixels(height_cm), secrets.token_urlsafe(32),
            secrets.token_urlsafe(32), "", None, None, None, urls[0], json.dumps(urls),
            len(urls), folio_job_id, None, PROVIDER, model,
        ))
        con.execute("UPDATE jobs SET prompt_mode=?,skip_generation=?,offer_id=?,order_number=? WHERE id=?",
                    (prompt_mode, int(job.get("skip_generation") is True),
                     label_text(job.get("offer_id")), label_text(job.get("order_number")), job_id))
    log_event("job accepted", job_id=job_id, source="folio", folio_job=folio_job_id,
              provider=PROVIDER, images=len(urls), print_px=f"{image_pixels(width_cm)}x{image_pixels(height_cm)}")
    return True


def report_one() -> bool:
    """Deliver one due result; a stale or unavailable job cannot starve others."""
    with connection() as con:
        row = con.execute("SELECT * FROM jobs WHERE folio_job_id IS NOT NULL "
                          "AND (reported_status IS NULL OR reported_status!=status) "
                          "AND (report_blocked_status IS NULL OR report_blocked_status!=status) "
                          "AND (report_next_at IS NULL OR report_next_at<=?) "
                          "ORDER BY created_at LIMIT 1", (now(),)).fetchone()
    if row is None:
        return False
    log_event("folio status sending", job_id=row["id"], folio_job=row["folio_job_id"],
              status=row["status"], preview=row["status"] == "completed")
    try:
        data = {"worker_id": row["id"], "status": row["status"],
                "print_file": row["print_file"] or "", "error": row["error"] or ""}
        files = None
        if row["status"] == "completed":
            files = {"preview": ("preview.jpg", (DATA / row["id"] / "preview.jpg").read_bytes(), "image/jpeg")}
        response = httpx.post(f"{FOLIO_BASE}/folio/image-worker/jobs/{row['folio_job_id']}",
                              headers=folio_headers(), data=data, files=files,
                              timeout=30, follow_redirects=False)
        response.raise_for_status()
    except Exception as exc:
        blocked = False
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                detail = exc.response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            blocked = ((exc.response.status_code == 409 and detail == "image_worker_result_conflict")
                       or (exc.response.status_code == 404 and detail == "image_worker_job_unknown"))
        attempts = row["report_attempts"] + 1
        delay = retry_delay(attempts)
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        with connection() as con:
            con.execute("UPDATE jobs SET report_attempts=?,report_next_at=?,report_blocked_status=?,report_error=? "
                        "WHERE id=? AND status=?", (attempts, None if blocked else next_at,
                        row["status"] if blocked else None, network_error(exc), row["id"], row["status"]))
        log_event("folio status superseded" if blocked else "folio status retry", job_id=row["id"],
                  attempt=attempts, reason=network_error(exc), next_seconds=None if blocked else delay)
        return True
    with connection() as con:
        con.execute("UPDATE jobs SET reported_status=?,report_attempts=0,report_next_at=NULL,report_error=NULL "
                    "WHERE id=? AND status=?", (row["status"], row["id"], row["status"]))
    log_event("folio status delivered", job_id=row["id"], status=row["status"], http=response.status_code)
    return True


def report_loop():
    failures = 0
    while not STOP.is_set():
        try:
            delivered = placement_report_one() or report_one()
            failures = 0
            STOP.wait(2 if delivered else 3)
        except Exception as exc:
            failures += 1
            delay = retry_delay(failures)
            log_event("folio reporter retry", attempt=failures, reason=network_error(exc), next_seconds=delay)
            STOP.wait(delay)


def safe_print_name(filename: object) -> str:
    if (not isinstance(filename, str) or not filename or len(filename) > 255
            or filename.endswith((" ", ".")) or filename in {".", ".."}
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in filename)
            or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", filename)):
        raise ValueError("Недопустимое имя печатного файла от MES")
    if Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
        raise ValueError("Неподдерживаемый формат печатного файла от MES")
    return filename


def placement_action(action: dict) -> tuple[sqlite3.Row, list[dict], list[int]]:
    if action.get("action") != "place_print_files" or type(action.get("id")) is not int:
        raise ValueError("Некорректное действие размещения")
    job_id = str(uuid.UUID(str(action.get("worker_id"))))
    operation_id = str(uuid.UUID(str(action.get("operation_id"))))
    str(uuid.UUID(str(action.get("claim_token"))))
    if (action.get("worker_instance_id") != installation_id()
            or not isinstance(action.get("layout_version"), str) or not action["layout_version"]
            or action.get("destination") != {"kind": "worker_print_directory"}):
        raise ValueError("Размещение адресовано другому воркеру или назначению")
    row = get_job(job_id)
    if (row is None or row["folio_job_id"] != action["id"] or row["status"] != "completed"
            or row["print_file"] != action.get("source_print_file")):
        raise ValueError("Устаревшая версия или неизвестный исходный макет")
    files = action.get("print_files")
    pending = action.get("pending_serials")
    if (not isinstance(files, list) or not files or not isinstance(pending, list)
            or len(pending) > len(files)):
        raise ValueError("Некорректный список файлов MES")
    serials = [item.get("serial") for item in files if isinstance(item, dict)]
    if (len(serials) != len(files) or any(type(s) is not int or s <= 0 for s in serials)
            or len(serials) != len(set(serials)) or any(type(s) is not int for s in pending)
            or len(pending) != len(set(pending)) or not set(pending).issubset(serials)):
        raise ValueError("Некорректные serial файлов MES")
    names = [item.get("filename") for item in files]
    if any(not isinstance(name, str) for name in names) or len({name.casefold() for name in names}) != len(names):
        raise ValueError("Повторяющиеся имена файлов MES")
    with connection() as con:
        bound = con.execute("SELECT * FROM placement_binding WHERE job_id=?", (job_id,)).fetchone()
        if bound and (bound["operation_id"] != operation_id
                      or bound["layout_version"] != action["layout_version"]
                      or bound["source_sha256"] != (row["print_sha256"] or "")):
            raise ValueError("Другая операция или версия уже закреплена за макетом")
        if not bound:
            con.execute("INSERT INTO placement_binding VALUES(?,?,?,?)",
                        (job_id, operation_id, action["layout_version"], row["print_sha256"] or ""))
    return row, files, pending


def renew_placement_claim(action: dict) -> None:
    try:
        response = httpx.get(FOLIO_POLL_URL, headers={**folio_headers(), "X-Folio-Image-Models": "1",
                                "X-Folio-Prompt-Planner": "1", "X-Folio-Image-Provider": PROVIDER},
                             timeout=20, follow_redirects=False)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise PlacementLeaseLost(f"Не удалось подтвердить захват: {network_error(exc)}") from exc
    current = response.json() if response.status_code == 200 else None
    if (not isinstance(current, dict) or current.get("action") != "place_print_files"
            or current.get("id") != action["id"]
            or current.get("operation_id") != action["operation_id"]
            or current.get("layout_version") != action["layout_version"]
            or current.get("worker_id") != action["worker_id"]):
        raise PlacementLeaseLost("Захват размещения утрачен или версия устарела")
    action["claim_token"] = current["claim_token"]


def placement_cache(row: sqlite3.Row, action: dict, serial: int, filename: str) -> Path:
    source = Path(row["print_file"])
    if not source.is_file():
        raise FileNotFoundError("Исходный печатный JPG отсутствует в рабочей папке")
    source_size, source_hash = file_digest(source)
    if row["print_sha256"] and (source_hash != row["print_sha256"] or source_size != row["print_size"]):
        raise RuntimeError("Исходный печатный JPG изменился после генерации")
    folder = DATA / row["id"] / "placement" / action["operation_id"]
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower()
    output = folder / f"{serial}{suffix}"
    temporary = folder / f"{serial}.{uuid.uuid4().hex}.partial"
    try:
        if suffix in {".jpg", ".jpeg"} and source.suffix.lower() in {".jpg", ".jpeg"}:
            shutil.copyfile(source, temporary)
        else:
            with Image.open(source) as image:
                page = image.convert("RGB")
                try:
                    if suffix == ".png":
                        page.save(temporary, format="PNG", dpi=(150, 150))
                    elif suffix in {".tif", ".tiff"}:
                        page.save(temporary, format="TIFF", compression="tiff_lzw", dpi=(150, 150))
                    else:
                        page.save(temporary, format="JPEG", quality=95, subsampling=0, dpi=(150, 150))
                finally:
                    page.close()
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def place_one(row: sqlite3.Row, action: dict, item: dict) -> dict:
    serial = item["serial"]
    filename = item.get("filename")
    result = {"serial": serial, "filename": filename}
    try:
        safe_print_name(filename)
        if (not isinstance(item.get("layout_key"), str)
                or filename.rsplit(".", 1)[0] != item["layout_key"]):
            raise ValueError("Имя файла не соответствует layout_key от MES")
        if not PRINT_SHARE_PATH or not Path(PRINT_SHARE_PATH).is_absolute():
            raise RuntimeError("Производственная папка не настроена")
        folder = Path(PRINT_SHARE_PATH)
        if not folder.is_dir():
            raise OSError("Производственная папка недоступна")
        destination = folder / filename
        cache = placement_cache(row, action, serial, filename)
        size, digest = file_digest(cache)
        with connection() as con:
            manifest = con.execute("SELECT * FROM placement_manifest WHERE operation_id=? AND serial=?",
                                   (action["operation_id"], serial)).fetchone()
            if manifest:
                if (manifest["job_id"] != row["id"] or manifest["layout_version"] != action["layout_version"]
                        or manifest["filename"] != filename or manifest["saved_path"] != str(destination)
                        or manifest["size_bytes"] != size or manifest["sha256"] != digest):
                    raise RuntimeError("Манифест размещения не совпадает с текущей операцией")
            else:
                if destination.exists():
                    raise FileExistsError("Файл с таким именем уже существует и не принадлежит операции")
                con.execute("INSERT INTO placement_manifest VALUES(?,?,?,?,?,?,?,?,?)",
                            (action["operation_id"], serial, row["id"], action["layout_version"],
                             filename, str(destination), size, digest, "intent"))
        if destination.exists():
            actual_size, actual_hash = file_digest(destination)
            if actual_size != size or actual_hash != digest:
                raise FileExistsError("Существующий файл отличается от манифеста операции")
        else:
            temporary = folder / f".folio-{uuid.uuid4().hex}.partial"
            try:
                with cache.open("rb") as source, temporary.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                if file_digest(temporary) != (size, digest):
                    raise OSError("Неполная копия печатного файла")
                renew_placement_claim(action)
                if os.name == "nt":
                    os.rename(temporary, destination)  # Windows refuses to replace an existing file.
                else:
                    os.link(temporary, destination)  # Exclusive publication on POSIX.
            finally:
                temporary.unlink(missing_ok=True)
            if file_digest(destination) != (size, digest):
                raise OSError("Контрольная сумма после размещения не совпала")
        with connection() as con:
            con.execute("UPDATE placement_manifest SET state='completed' WHERE operation_id=? AND serial=?",
                        (action["operation_id"], serial))
        result.update(status="completed", saved_path=str(destination), size_bytes=size, sha256=digest)
        log_event("print placed", job_id=row["id"], operation=action["operation_id"],
                  serial=serial, filename=filename, bytes=size)
    except PlacementLeaseLost:
        raise
    except Exception as exc:
        result.update(status="failed", error=network_error(exc)[:1000])
        log_event("print placement failed", job_id=row["id"], operation=action["operation_id"],
                  serial=serial, filename=filename, reason=network_error(exc))
    return result


def process_placement(action: dict) -> None:
    row, files, pending = placement_action(action)
    operation_id = action["operation_id"]
    with connection() as con:
        queued = con.execute("SELECT payload FROM placement_reports WHERE operation_id=?", (operation_id,)).fetchone()
        if queued:
            previous = json.loads(queued["payload"])
            if isinstance(previous, list):
                previous = previous[0]
            if previous["claim_token"] == action["claim_token"]:
                return  # The reporter retries the durable payload without copying again.
            con.execute("DELETE FROM placement_reports WHERE operation_id=?", (operation_id,))
    by_serial = {item["serial"]: item for item in files}
    updates = []
    for serial in pending:
        if STOP.is_set():
            return
        renew_placement_claim(action)
        updates.append(place_one(row, action, by_serial[serial]))
    payloads = []
    chunk = []
    common = {"action": "place_print_files", "operation_id": operation_id,
              "layout_version": action["layout_version"], "claim_token": action["claim_token"]}
    for update in updates:
        candidate = chunk + [update]
        if chunk and len(json.dumps({**common, "status": "running", "files": candidate},
                                    ensure_ascii=False).encode("utf-8")) > 60_000:
            payloads.append({**common, "status": "running", "files": chunk})
            chunk = [update]
        else:
            chunk = candidate
    payloads.append({**common, "status": "finished", "files": chunk})
    payload = payloads[0] if len(payloads) == 1 else payloads
    with connection() as con:
        con.execute("INSERT INTO placement_reports(operation_id,job_id,payload) VALUES(?,?,?) "
                    "ON CONFLICT(operation_id) DO UPDATE SET payload=excluded.payload,attempts=0,next_at=NULL,blocked=0",
                    (operation_id, row["id"], json.dumps(payload, ensure_ascii=False)))
    log_event("placement report queued", job_id=row["id"], operation=operation_id,
              completed=sum(item["status"] == "completed" for item in updates),
              failed=sum(item["status"] == "failed" for item in updates))


def placement_report_one() -> bool:
    with connection() as con:
        row = con.execute("SELECT * FROM placement_reports WHERE blocked=0 AND (next_at IS NULL OR next_at<=?) "
                          "ORDER BY rowid LIMIT 1", (now(),)).fetchone()
    if row is None:
        return False
    stored = json.loads(row["payload"])
    payload = stored[0] if isinstance(stored, list) else stored
    try:
        response = httpx.post(f"{FOLIO_BASE}/folio/image-worker/jobs/{get_job(row['job_id'])['folio_job_id']}",
                              headers=folio_headers(), json=payload, timeout=30, follow_redirects=False)
        response.raise_for_status()
        if response.json().get("ok") is not True:
            raise ValueError("Folio не подтвердил отчёт размещения")
    except Exception as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        if status in {404, 409, 422}:
            with connection() as con:
                con.execute("DELETE FROM placement_reports WHERE operation_id=? AND payload=?",
                            (row["operation_id"], row["payload"]))
            log_event("placement report rejected", job_id=row["job_id"],
                      operation=row["operation_id"], reason=network_error(exc))
        else:
            attempts = row["attempts"] + 1
            due = (datetime.now(timezone.utc) + timedelta(seconds=retry_delay(attempts))).isoformat()
            with connection() as con:
                con.execute("UPDATE placement_reports SET attempts=?,next_at=? WHERE operation_id=? AND payload=?",
                            (attempts, due, row["operation_id"], row["payload"]))
            log_event("placement report retry", job_id=row["job_id"],
                      operation=row["operation_id"], attempt=attempts, reason=network_error(exc))
        return True
    with connection() as con:
        if isinstance(stored, list) and len(stored) > 1:
            remaining = stored[1:]
            con.execute("UPDATE placement_reports SET payload=?,attempts=0,next_at=NULL WHERE operation_id=? AND payload=?",
                        (json.dumps(remaining[0] if len(remaining) == 1 else remaining, ensure_ascii=False),
                         row["operation_id"], row["payload"]))
        else:
            con.execute("DELETE FROM placement_reports WHERE operation_id=? AND payload=?",
                        (row["operation_id"], row["payload"]))
    log_event("placement report delivered", job_id=row["job_id"], operation=row["operation_id"],
              state=response.json().get("placement_state"))
    return True


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, authorization: str | None = Header(default=None)):
    require_key(authorization)
    row = get_job(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    return public_job(row)


@app.get("/v1/jobs/{job_id}/preview")
def preview(job_id: str, authorization: str | None = Header(default=None)):
    require_key(authorization)
    row = get_job(job_id)
    if not row or row["status"] != "completed":
        raise HTTPException(404, "Preview not ready")
    return FileResponse(DATA / job_id / "preview.jpg", media_type="image/jpeg")


@app.get("/vendor-input/{job_id}/{token}")
def vendor_input(job_id: str, token: str):
    return vendor_input_at(job_id, token, 0)


@app.get("/vendor-input/{job_id}/{token}/{index}")
def vendor_input_at(job_id: str, token: str, index: int):
    row = get_job(job_id)
    if not row or not hmac.compare_digest(row["input_token"], token) or row["status"] in ("completed", "failed") or not 0 <= index < row["input_count"]:
        raise HTTPException(404)
    return FileResponse(DATA / job_id / ("input.png" if index == 0 else f"input-{index}.png"), media_type="image/png")


@app.post("/vendor-callback/{job_id}/{token}")
async def vendor_callback(job_id: str, token: str):
    # The provider requires a callback URL. Polling remains the source of truth and
    # recovers callbacks lost during restarts. The random URL token limits spoofing.
    row = get_job(job_id)
    if not row or not hmac.compare_digest(row["callback_token"], token):
        raise HTTPException(404)
    return {"received": True}


ASPECT_RATIOS = (("1:1", 1, 1), ("2:3", 2, 3), ("3:2", 3, 2),
                 ("3:4", 3, 4), ("4:3", 4, 3), ("4:5", 4, 5),
                 ("5:4", 5, 4), ("9:16", 9, 16), ("16:9", 16, 9),
                 ("21:9", 21, 9))
AITUNNEL_ASPECT_RATIOS = (("1:1", 1, 1), ("1:2", 1, 2), ("1:4", 1, 4),
                          ("2:1", 2, 1), ("2:3", 2, 3), ("3:2", 3, 2),
                          ("3:4", 3, 4), ("4:1", 4, 1), ("4:3", 4, 3),
                          ("4:5", 4, 5), ("5:4", 5, 4), ("9:16", 9, 16),
                          ("16:9", 16, 9))
AITUNNEL_MODEL_CAPS = {
    "seedream-5-0-lite": ("4K", tuple(r for r in AITUNNEL_ASPECT_RATIOS
                                    if r[0] not in {"1:4", "4:1"})),
    "seedream-5-0-flash": ("2K", tuple(r for r in AITUNNEL_ASPECT_RATIOS
                                     if r[0] not in {"1:4", "4:1"})),
    "seedream-4.5": ("4K", tuple(r for r in AITUNNEL_ASPECT_RATIOS
                               if r[0] not in {"1:4", "4:1"})),
    "hy-image-v3.5-preview": ("4K", AITUNNEL_ASPECT_RATIOS),
    # Flux 3 Image Klein: 4K, генерация и правка по референсам; есть 5:7 / 7:5 (ровно 50×70), нет 1:4 / 4:1
    "flux-3-image-klein": ("4K", tuple(r for r in AITUNNEL_ASPECT_RATIOS if r[0] not in {"1:4", "4:1"})
                           + (("5:7", 5, 7), ("7:5", 7, 5))),
    "flux-3-image": ("4K", tuple(r for r in AITUNNEL_ASPECT_RATIOS
                                if r[0] not in {"1:4", "4:1"})),
    "gemini-nano-banana-2.1": ("4K", tuple(r for r in AITUNNEL_ASPECT_RATIOS
                                       if r[0] not in {"1:2", "2:1"})),
    "seedream-5-0-pro": ("2K", tuple(r for r in AITUNNEL_ASPECT_RATIOS
                                if r[0] not in {"1:4", "4:1"})),
}


def closest_aspect_ratio(width_cm: float, height_cm: float, ratios) -> str:
    target = width_cm / height_cm
    return min(ratios, key=lambda ratio: abs(math.log(target / (ratio[1] / ratio[2]))))[0]


def nano_aspect_ratio(width_cm: float, height_cm: float) -> str:
    return closest_aspect_ratio(width_cm, height_cm, ASPECT_RATIOS)


def nano_request(row):
    job_id = row["id"]
    urls = (json.loads(row["vendor_image_urls"]) if row["vendor_image_urls"] else
            [row["vendor_image_url"]] if row["vendor_image_url"] else
            [f"{PUBLIC_BASE}/vendor-input/{job_id}/{row['input_token']}/{i}" for i in range(row["input_count"])])
    callback = NANO_CALLBACK_URL or f"{PUBLIC_BASE}/vendor-callback/{job_id}/{row['callback_token']}"
    return "generate-pro", {"prompt": row["prompt"], "imageUrls": urls,
                            "resolution": "4K",
                            "aspectRatio": nano_aspect_ratio(row["width_cm"], row["height_cm"]),
                            "callBackUrl": callback}


def aitunnel_references(row) -> list[dict]:
    """Send validated local copies; provider-side fetching of Folio URLs can time out."""
    folder = DATA / row["id"]
    urls = [
        "data:image/png;base64," + base64.b64encode(
            (folder / ("input.png" if index == 0 else f"input-{index}.png")).read_bytes()
        ).decode("ascii")
        for index in range(row["input_count"])
    ]
    return [{"type": "image_url", "image_url": {"url": url}} for url in urls]


def begin_aitunnel_request(job_id: str) -> bool:
    """Persist the billable POST boundary before sending anything to AI Tunnel."""
    with connection() as con:
        changed = con.execute(
            "UPDATE jobs SET aitunnel_request_started_at=? "
            "WHERE id=? AND provider='aitunnel' AND aitunnel_request_started_at IS NULL",
            (now(), job_id),
        ).rowcount
    return changed == 1


def planned_prompt(row):
    from . import prompt_planner
    try:
        result = prompt_planner.prepare(row, DATA / row["id"], aitunnel_references(row),
            key=AITUNNEL_KEY, base=AITUNNEL_BASE_URL, mirror=AITUNNEL_MIRROR_BASE,
            connect_timeout=AITUNNEL_CONNECT_TIMEOUT, read_timeout=AITUNNEL_READ_TIMEOUT)
        receipt = json.loads((DATA / row["id"] / "prompt-plan.json").read_text(encoding="utf-8"))
        log_event("prompt ready", job_id=row["id"], model=prompt_planner.MODEL,
                  cost_rub=receipt.get("cost_rub"))
        return result
    except prompt_planner.PlannerOutcomeUnknown as exc:
        raise AIRequestOutcomeUnknown(str(exc)) from exc


def image_geometry_prompt(row, prompt):
    """Explain reference/target geometry; never stretch pixels to enforce a ratio."""
    with Image.open(DATA / row["id"] / "input.png") as source:
        width, height = source.size
    return prompt + (
        f"\nТехнические требования: первый исходник {width}×{height} пикселей; "
        f"выходной кадр {row['width_cm']}×{row['height_cm']} см. "
        "Разница форматов не означает растяжение изображения: сохраняй пропорции головы, "
        "лица и тела, масштабируй человека равномерно по обеим осям. "
        "Для изменения пропорций кадра расширяй окружение, а не сужай или вытягивай человека. "
        "Если художественная стилизация явно не запрошена, сохрани фотографический вид "
        "без мазков кисти, фактуры холста и эффекта рисунка."
    )


def aitunnel_result(row) -> Path:
    """Generate once, except for safe connection and provider-confirmed 504 retries."""
    output = DATA / row["id"] / "generated.png"
    if output.exists():
        return output
    if not AITUNNEL_KEY:
        raise RuntimeError("AITUNNEL_API_KEY is not configured")
    if AITUNNEL_BASE_URL not in {AITUNNEL_PRIMARY_BASE, AITUNNEL_MIRROR_BASE}:
        raise RuntimeError("AITUNNEL_BASE_URL must be an official AI Tunnel API host")
    if AITUNNEL_CONNECT_TIMEOUT <= 0 or AITUNNEL_READ_TIMEOUT <= 0:
        raise RuntimeError("AI Tunnel timeouts must be positive")
    model = row["model"] if "model" in row.keys() else AITUNNEL_MODEL
    if model not in AITUNNEL_MODEL_CAPS:
        raise RuntimeError("Unsupported AI Tunnel image model")
    if model in {"flux-3-image", "gemini-nano-banana-2.1", "seedream-5-0-pro"}:
        raise RuntimeError("Модель задания исключена из бюджетного режима. Выберите Seedream Lite, Flash, 4.5 или HY Image в Folio и создайте новое задание. Платный запрос не отправлен.")
    resolution, ratios = AITUNNEL_MODEL_CAPS[model]
    payload = {
        "model": model,
        "prompt": image_geometry_prompt(row, planned_prompt(row) if "prompt_mode" in row.keys() and row["prompt_mode"] == "vision" else row["prompt"]),
        "n": 1,
        "resolution": resolution,
        "aspect_ratio": closest_aspect_ratio(row["width_cm"], row["height_cm"], ratios),
        "input_references": aitunnel_references(row),
    }
    if not begin_aitunnel_request(row["id"]):
        raise RuntimeError("AI Tunnel request outcome is unknown after restart; do not retry automatically")
    started = time.monotonic()
    log_event("generation request started", job_id=row["id"], provider="aitunnel",
              model=model, resolution=resolution, aspect=payload["aspect_ratio"],
              images=len(payload["input_references"]), reference_mode="inline")
    targets = ([AITUNNEL_URL, AITUNNEL_MIRROR_URL, AITUNNEL_MIRROR_URL]
               if AITUNNEL_BASE_URL == AITUNNEL_PRIMARY_BASE else [AITUNNEL_URL] * 3)
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=AITUNNEL_CONNECT_TIMEOUT,
                                                read=AITUNNEL_READ_TIMEOUT, write=120, pool=20),
                          follow_redirects=False) as client:
            for attempt, target in enumerate(targets, start=1):
                response_started = False
                try:
                    with client.stream("POST", target,
                                       headers={"Authorization": f"Bearer {AITUNNEL_KEY}"}, json=payload) as response:
                        response_started = True
                        if response.status_code != 200:
                            if response.status_code == 504:
                                if attempt == len(targets) or STOP.is_set():
                                    raise RuntimeError(
                                        f"AI Tunnel вернул HTTP 504 после {attempt} попыток; "
                                        "генерация не принята"
                                    )
                                delay = retry_delay(attempt)
                                log_event("generation gateway retry", job_id=row["id"],
                                          attempt=attempt, http_status=504,
                                          next_host=urlparse(targets[attempt]).hostname,
                                          next_seconds=delay)
                                STOP.wait(delay)
                                continue
                            if response.status_code >= 500:
                                raise AIRequestOutcomeUnknown(
                                    f"AI Tunnel вернул HTTP {response.status_code}: результат генерации неизвестен. "
                                    "Проверьте историю запросов в AI Tunnel перед созданием нового задания."
                                )
                            raise RuntimeError(f"AI Tunnel rejected image request (HTTP {response.status_code})")
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 145 * 1024 * 1024:
                                raise RuntimeError("AI Tunnel image response is too large")
                            chunks.append(chunk)
                    break
                except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                    if response_started:
                        raise AIRequestOutcomeUnknown(
                            "Соединение с AI Tunnel прервалось после начала ответа; повторять задание автоматически нельзя"
                        ) from exc
                    if attempt == len(targets) or STOP.is_set():
                        raise RuntimeError(
                            "Не удалось подключиться к AI Tunnel через основной и резервный адреса; запрос не отправлен"
                        ) from exc
                    delay = retry_delay(attempt)
                    log_event("generation connection retry", job_id=row["id"],
                              attempt=attempt, reason=type(exc).__name__,
                              next_host=urlparse(targets[attempt]).hostname, next_seconds=delay)
                    STOP.wait(delay)
    except httpx.TimeoutException as exc:
        raise AIRequestOutcomeUnknown(
            "Истекло ожидание ответа AI Tunnel; запрос мог быть оплачен. Повторять автоматически нельзя"
        ) from exc
    except httpx.TransportError as exc:
        raise AIRequestOutcomeUnknown(
            "Ответ AI Tunnel потерян; запрос мог быть оплачен. Повторять автоматически нельзя"
        ) from exc
    try:
        body = json.loads(b"".join(chunks))
        data = body["data"]
        encoded = data[0]["b64_json"] if isinstance(data, list) and len(data) == 1 else None
        if not isinstance(encoded, str) or len(encoded) > 140 * 1024 * 1024:
            raise ValueError("missing or oversized image")
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) > 100 * 1024 * 1024:
            raise ValueError("oversized image")
        with Image.open(io.BytesIO(decoded)) as image:
            image.load()
            normalized = ImageOps.exif_transpose(image).convert("RGB")
        temporary = output.with_suffix(".partial")
        try:
            normalized.save(temporary, "PNG")
            temporary.replace(output)
        finally:
            normalized.close()
            temporary.unlink(missing_ok=True)
    except (KeyError, IndexError, TypeError, ValueError, binascii.Error, OSError, UnidentifiedImageError) as exc:
        raise RuntimeError("AI Tunnel returned an invalid image") from exc
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    log_event("generation completed", job_id=row["id"],
              elapsed_seconds=round(time.monotonic() - started, 1),
              image_px=f"{normalized.width}x{normalized.height}",
              cost_rub=usage.get("cost_rub"))
    return output


def nano_http(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """Retry connection failures; a timed-out POST response may already have created a paid task."""
    for attempt in range(5):
        try:
            return client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            if (attempt == 4 or STOP.is_set()
                    or (method == "POST" and isinstance(exc, httpx.ReadTimeout))):
                raise
            print(f"Nano Banana connection retry {attempt + 1}/4: {type(exc).__name__}", flush=True)
            STOP.wait(min(2 ** attempt, 8))
    raise RuntimeError("Nano Banana connection retries exhausted")


def nano_result(row) -> Path:
    job_id = row["id"]
    output = DATA / job_id / "generated.png"
    if output.exists():
        return output
    if MODE == "mock":
        output.write_bytes((DATA / job_id / "input.png").read_bytes())
        return output
    headers = {"Authorization": f"Bearer {NANO_KEY}"}
    with httpx.Client(timeout=30) as client:
        task_id = row["provider_task_id"]
        if not task_id:
            endpoint, payload = nano_request(row)
            response = nano_http(client, "POST", f"{NANO_BASE}/{endpoint}", headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 200 or not body.get("data", {}).get("taskId"):
                raise RuntimeError(f"Nano Banana rejected task: {body.get('message') or body.get('msg') or 'unknown error'}")
            task_id = body["data"]["taskId"]
            update(job_id, provider_task_id=task_id, status="generating")
        deadline = time.monotonic() + NANO_TIMEOUT
        while time.monotonic() < deadline and not STOP.is_set():
            response = nano_http(client, "GET", f"{NANO_BASE}/record-info", headers=headers, params={"taskId": task_id})
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 200:
                raise RuntimeError(f"Nano Banana status error: {body.get('message') or body.get('msg') or 'unknown error'}")
            data = body.get("data") or {}
            flag = data.get("successFlag")
            if flag == 1:
                image_url = (data.get("response") or {}).get("resultImageUrl")
                if not image_url or urlparse(image_url).scheme != "https":
                    raise RuntimeError("Nano Banana returned no HTTPS result URL")
                with client.stream("GET", image_url) as stream:
                    stream.raise_for_status()
                    chunks, size = [], 0
                    for chunk in stream.iter_bytes():
                        size += len(chunk)
                        if size > 100 * 1024 * 1024:
                            raise RuntimeError("Generated image too large")
                        chunks.append(chunk)
                with Image.open(io.BytesIO(b"".join(chunks))) as image:
                    image.load()
                    ImageOps.exif_transpose(image).convert("RGB").save(output, "PNG")
                return output
            if flag in (2, 3):
                raise RuntimeError(data.get("errorMessage") or "Nano Banana generation failed")
            STOP.wait(30)
    raise RuntimeError("Nano Banana task timed out")


def compose(row, source: Path) -> Path:
    width, height = row["pixel_width"], row["pixel_height"]
    with Image.open(source) as image:
        # The preview remains the requested painting, without print margins.
        page = ImageOps.fit(image.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
    out = DATA / row["id"] / "print.jpg"
    try:
        preview_size = tuple(max(1, round(side * 1000 / max(width, height))) for side in (width, height))
        preview = page.resize(preview_size, Image.Resampling.LANCZOS)
        try:
            preview.save(DATA / row["id"] / "preview.jpg", format="JPEG", quality=85)
        finally:
            preview.close()

        mirror_cm, white_cm = print_margins_cm(row["width_cm"], row["height_cm"])
        mirrored_width = image_pixels(row["width_cm"] + 2 * mirror_cm)
        mirrored_height = image_pixels(row["height_cm"] + 2 * mirror_cm)
        print_width = image_pixels(row["width_cm"] + 2 * (mirror_cm + white_cm))
        print_height = image_pixels(row["height_cm"] + 2 * (mirror_cm + white_cm))
        mirror_left, mirror_right = _split_padding(width, mirrored_width)
        mirror_top, mirror_bottom = _split_padding(height, mirrored_height)
        white_left, _ = _split_padding(mirrored_width, print_width)
        white_top, _ = _split_padding(mirrored_height, print_height)

        print_page = Image.new("RGB", (print_width, print_height), "white")
        try:
            _paste_mirrored(print_page, page, white_left, white_top,
                            mirror_left, mirror_top, mirror_right, mirror_bottom)
            if row["offer_id"]:
                draw_print_label(print_page, f"Артикул: {row['offer_id']}", bottom=False)
            if row["order_number"]:
                draw_print_label(print_page, f"Заказ Ozon: {row['order_number']}", bottom=True)
            if not row["offer_id"] or not row["order_number"]:
                log_event("print label data missing", job_id=row["id"])
            print_page.save(out, format="JPEG", quality=95, subsampling=0, dpi=(150, 150))
        finally:
            print_page.close()
    finally:
        page.close()
    return out


def process(row):
    job_id = row["id"]
    started = time.monotonic()
    log_event("job processing started", job_id=job_id, provider=row["provider"],
              folio_job=row["folio_job_id"])
    try:
        if row["status"] == "webhook_pending":
            update(job_id, status="completed" if row["print_file"] else "failed")
            return
        if row["skip_generation"]:
            # Folio decided the photo is good enough: no paid request is made.
            generated = DATA / job_id / "input.png"
            log_event("generation skipped", job_id=job_id, folio_job=row["folio_job_id"])
        else:
            update(job_id, status="generating")
            provider = row["provider"]
            if provider == "aitunnel":
                generated = aitunnel_result(get_job(job_id))
            elif provider == "mock":
                generated = DATA / job_id / "generated.png"
                if not generated.exists():
                    generated.write_bytes((DATA / job_id / "input.png").read_bytes())
            elif provider == "nanobanana":
                generated = nano_result(get_job(job_id))
            else:
                raise RuntimeError("Unsupported image provider for job")
        update(job_id, status="composing")
        log_event("print composing", job_id=job_id)
        print_source = compose(get_job(job_id), generated)
        log_event("print composed", job_id=job_id, bytes=print_source.stat().st_size)
        size, digest = file_digest(print_source)
        update(job_id, status="completed", print_file=str(print_source.resolve()),
               print_size=size, print_sha256=digest)
        log_event("job completed", job_id=job_id, elapsed_seconds=round(time.monotonic() - started, 1))
    except AIRequestOutcomeUnknown as exc:
        update(job_id, status="unknown", error=str(exc)[:500])
        log_event("generation outcome unknown", job_id=job_id, reason=network_error(exc),
                  elapsed_seconds=round(time.monotonic() - started, 1))
    except Exception as exc:
        update(job_id, status="failed", error=str(exc)[:500])
        log_event("job failed", job_id=job_id, reason=network_error(exc),
                  elapsed_seconds=round(time.monotonic() - started, 1))


def work_loop():
    failures = 0
    last_idle_log = time.monotonic()
    while not STOP.is_set():
        stage = "job selection"
        try:
            with connection() as con:
                row = con.execute("SELECT * FROM jobs WHERE status NOT IN ('completed','failed','unknown') ORDER BY created_at LIMIT 1").fetchone()
            if row:
                stage = "job processing"
                # A missing key after a restart is a configuration pause, not
                # a processing failure. Keep the durable job queued until the
                # operator restores the key and restarts the worker.
                if ((row["provider"] == "aitunnel" and not AITUNNEL_KEY)
                        or (row["provider"] == "nanobanana" and MODE == "live" and not NANO_KEY)):
                    STOP.wait(5)
                    continue
                process(row)
                failures = 0
            else:
                stage = "folio polling"
                pulled = pull_one()
                if failures:
                    log_event("folio polling recovered", previous_failures=failures)
                    failures = 0
                if not pulled:
                    if time.monotonic() - last_idle_log >= 60:
                        log_event("folio queue empty")
                        last_idle_log = time.monotonic()
                    STOP.wait(5)
        except Exception as exc:
            failures += 1
            delay = retry_delay(failures)
            log_event(f"{stage} retry", attempt=failures,
                      reason=network_error(exc), next_seconds=delay)
            STOP.wait(delay)
