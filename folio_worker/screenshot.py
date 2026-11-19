"""Скриншот производственной папки: файл макета физически лежит в папке печати.

Воркер открывает своё окно Проводника с выделенным файлом, разворачивает его поверх остальных,
снимает монитор с этим окном (вместе с панелью задач и часами Windows), закрывает окно и добавляет внизу
полосу с данными размещения. Работает только в интерактивном сеансе Windows: при заблокированном
экране или отключённом удалённом сеансе снимок получается пустым — тогда поднимается ScreenshotError,
и снимок не выдаётся за доказательство.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageStat

EXPLORER_CLASS = "CabinetWClass"
OPEN_TIMEOUT = float(os.getenv("PLACEMENT_SCREENSHOT_OPEN_SECONDS", "15"))
SETTLE_SECONDS = float(os.getenv("PLACEMENT_SCREENSHOT_SETTLE_SECONDS", "2.5"))
MAX_WIDTH = 2560
FONTS = (
    str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_LOCK = threading.Lock()  # одно окно Проводника за раз: иначе снимки перепутаются


class ScreenshotError(RuntimeError):
    pass


def enabled() -> bool:
    return os.getenv("PLACEMENT_SCREENSHOT", "1").strip().lower() not in {"0", "false", "no", "off"}


# ---------- Windows API ----------

def _user32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                                   wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    return ctypes, wintypes, user32


def explorer_windows() -> set[int]:
    ctypes, wintypes, user32 = _user32()
    found: set[int] = set()
    buf = ctypes.create_unicode_buffer(64)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetClassNameW(hwnd, buf, 64) and buf.value == EXPLORER_CLASS:
            found.add(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return found


def open_explorer(path: Path) -> int:
    """Открывает новое окно Проводника с выделенным файлом и возвращает его HWND."""
    before = explorer_windows()
    # explorer разбирает аргументы по-своему: путь — в кавычках сразу после «/select,»
    subprocess.Popen(f'explorer.exe /select,"{path}"')
    deadline = time.monotonic() + OPEN_TIMEOUT
    while time.monotonic() < deadline:
        new = explorer_windows() - before
        if new:
            return max(new)
        time.sleep(0.2)
    raise ScreenshotError("Проводник не открыл окно папки печати")


def bring_to_front(hwnd: int) -> None:
    _, _, user32 = _user32()
    user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE: список файлов крупно и целиком
    for _ in range(5):
        # Windows разрешает перехват фокуса только после ввода: имитируем нажатие Alt
        user32.keybd_event(0x12, 0, 0, None)
        user32.keybd_event(0x12, 0, 2, None)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return
    raise ScreenshotError("Не удалось вывести окно Проводника на передний план")


def _dpi_aware() -> None:
    """Координаты окон и пиксели снимка в одном масштабе (иначе при 125–150% экран обрежется не там)."""
    import ctypes

    try:
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass


def monitor_box(hwnd: int, shot: Image.Image) -> tuple[int, int, int, int]:
    """Прямоугольник монитора с окном (вместе с панелью задач) в координатах снимка всех экранов."""
    ctypes, wintypes, user32 = _user32()

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
        return 0, 0, shot.width, shot.height
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # левый верхний угол всех экранов
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    sx, sy = (shot.width / vw, shot.height / vh) if vw and vh else (1, 1)
    r = info.rcMonitor
    box = (round((r.left - vx) * sx), round((r.top - vy) * sy), round((r.right - vx) * sx), round((r.bottom - vy) * sy))
    box = (max(0, box[0]), max(0, box[1]), min(shot.width, box[2]), min(shot.height, box[3]))
    return box if box[2] - box[0] > 100 and box[3] - box[1] > 100 else (0, 0, shot.width, shot.height)


def close_window(hwnd: int) -> None:
    _, _, user32 = _user32()
    if user32.IsWindow(hwnd):
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


# ---------- снимок ----------

def is_blank(image: Image.Image) -> bool:
    """Чёрный/однотонный кадр — экран заблокирован или удалённый сеанс не отрисовывается."""
    stat = ImageStat.Stat(image.convert("L").resize((320, 180)))
    return stat.stddev[0] < 4


def _font(size: int):
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def add_caption(image: Image.Image, lines: list[str]) -> Image.Image:
    if image.width > MAX_WIDTH:
        image = image.resize((MAX_WIDTH, round(image.height * MAX_WIDTH / image.width)))
    size = max(14, image.width // 90)
    font = _font(size)
    pad = size // 2
    band = pad * 2 + len(lines) * (size + pad)
    out = Image.new("RGB", (image.width, image.height + band), "white")
    out.paste(image.convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(out)
    y = image.height + pad
    for line in lines:
        draw.text((pad, y), line, font=font, fill="black")
        y += size + pad
    return out


def capture_folder(path: Path, out: Path, caption: list[str]) -> Path:
    """Скриншот папки с выделенным файлом path → JPEG out. Поднимает ScreenshotError."""
    if os.name != "nt":
        raise ScreenshotError("Скриншот папки доступен только в Windows")
    if not path.is_file():
        raise ScreenshotError("Файла нет в папке печати")
    with _LOCK:
        _dpi_aware()
        hwnd = open_explorer(path)
        try:
            bring_to_front(hwnd)
            time.sleep(SETTLE_SECONDS)  # Проводник дорисовывает список и выделение
            shot = ImageGrab.grab(all_screens=True)
            shot = shot.crop(monitor_box(hwnd, shot))  # только монитор с папкой: крупно и без чужих окон
        finally:
            close_window(hwnd)
    if is_blank(shot):
        raise ScreenshotError("Экран недоступен: снимок пустой (экран заблокирован или удалённый сеанс отключён)")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(".partial")
    add_caption(shot, caption).save(temporary, "JPEG", quality=85)
    temporary.replace(out)
    return out
