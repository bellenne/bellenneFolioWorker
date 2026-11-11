"""Windows desktop launcher for FolioImageWorker; no Docker or Python install needed."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import winreg
from pathlib import Path
from urllib.parse import urlsplit

from folio_worker import __version__


PORT = 8765
CONTAINER = "bellennefolioimageworker-folio-worker-1"
API_BASES = ("https://api.aitunnel.ru/v1", "https://ru-api.aitunnel.ru/v1")
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "FolioImageWorker"


def autostart_command() -> str:
    if not getattr(sys, "frozen", False):
        raise ValueError("Автозапуск доступен в собранном EXE")
    return f'"{Path(sys.executable).resolve()}"'


def autostart_enabled() -> bool:
    if not getattr(sys, "frozen", False):
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            command, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
    except FileNotFoundError:
        return False
    return command == autostart_command()


def set_autostart(enabled: bool) -> None:
    if enabled:
        command = autostart_command()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, AUTOSTART_NAME)
        except FileNotFoundError:
            pass


def bind_entry_clipboard(entry, tk):
    """Make paste work consistently in packaged Tk on Windows."""
    def paste(_event=None):
        try:
            value = entry.clipboard_get()
            if entry.selection_present():
                entry.delete("sel.first", "sel.last")
            entry.insert("insert", value)
        except tk.TclError:
            pass
        return "break"

    def select_all(_event=None):
        entry.select_range(0, "end")
        entry.icursor("end")
        return "break"

    for sequence in ("<Control-v>", "<Control-V>", "<Shift-Insert>"):
        entry.bind(sequence, paste)
    entry.bind("<Control-a>", select_all)
    entry.bind("<Control-A>", select_all)

    menu = tk.Menu(entry, tearoff=False)
    menu.add_command(label="Вставить", command=paste)
    menu.add_command(label="Выделить всё", command=select_all)

    def show_menu(event):
        entry.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    entry.bind("<Button-3>", show_menu)
    return paste


def app_home() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "FolioImageWorker"


def legacy_root() -> Path | None:
    anchor = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    for candidate in (anchor, *anchor.parents[:3]):
        if (candidate / "data" / "jobs.sqlite3").exists() and (candidate / ".env").exists():
            return candidate
    return None


def legacy_env(root: Path | None) -> dict[str, str]:
    if root is None:
        return {}
    result = {}
    for line in (root / ".env").read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def default_settings() -> dict[str, str]:
    root = legacy_root()
    previous = legacy_env(root)
    work = root or app_home()
    return {
        "folio_poll_url": previous.get("FOLIO_POLL_URL", "https://one.customcraft-mes.ru/folio/image-worker/next"),
        "worker_api_key": previous.get("WORKER_API_KEY", ""),
        "aitunnel_api_key": previous.get("AITUNNEL_API_KEY", ""),
        "aitunnel_base_url": previous.get("AITUNNEL_BASE_URL", API_BASES[0]),
        "provider": previous.get("IMAGE_PROVIDER", "aitunnel"),
        "output_dir": previous.get("PRINT_SHARE_PATH") or str(app_home().parent / "FolioImageWorkerPrints"),
        "work_dir": str(work),
        "nanobanana_api_key": previous.get("NANOBANANA_API_KEY", ""),
    }


def config_path() -> Path:
    return app_home() / "config.json"


def load_settings(path: Path | None = None) -> dict[str, str] | None:
    path = path or config_path()
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Файл настроек повреждён")
    return {key: str(value) for key, value in data.items()}


def normalize_output_dir(value: str) -> str:
    """Use a native Windows UNC path even when a pasted address uses / separators."""
    value = value.strip()
    if value.startswith(("//", "\\\\")):
        normalized = "\\\\" + value.lstrip("/\\").rstrip("/\\").replace("/", "\\")
        parts = normalized[2:].split("\\")
        if len(parts) < 2 or not parts[0] or not parts[1] or any(not part for part in parts):
            raise ValueError("UNC-путь должен содержать сервер и имя сетевой папки")
        return normalized
    return value


def validate_settings(settings: dict[str, str], *, check_write: bool = True) -> dict[str, str]:
    config = {key: str(value).strip() for key, value in settings.items()}
    config["output_dir"] = normalize_output_dir(config.get("output_dir", ""))
    url = urlsplit(config.get("folio_poll_url", ""))
    if (url.scheme != "https" or not url.hostname or url.path != "/folio/image-worker/next"
            or url.query or url.fragment or url.username or url.password):
        raise ValueError("Укажите HTTPS-ссылку опроса из настроек Folio")
    if not config.get("worker_api_key"):
        raise ValueError("Укажите общий ключ Folio и воркера")
    if config.get("provider") not in {"aitunnel", "mock"}:
        raise ValueError("Режим должен быть aitunnel или mock")
    if config["provider"] == "aitunnel" and not config.get("aitunnel_api_key"):
        raise ValueError("Укажите ключ AI Tunnel")
    if config.get("aitunnel_base_url") not in API_BASES:
        raise ValueError("Выберите официальный адрес AI Tunnel")
    work = Path(config.get("work_dir", ""))
    output = Path(config.get("output_dir", ""))
    if not work.is_absolute() or str(work).startswith("\\\\"):
        raise ValueError("Рабочая папка должна быть локальным абсолютным путём")
    if not output.is_absolute():
        raise ValueError("Производственная папка должна быть локальным или UNC-путём")
    if work == output:
        raise ValueError("Для рабочих файлов и печатных JPG укажите разные папки")
    if check_write:
        work.mkdir(parents=True, exist_ok=True)
        if str(output).startswith("\\\\"):
            if not output.is_dir():
                raise ValueError(f"Сетевая папка недоступна под текущей учётной записью Windows: {output}")
        else:
            output.mkdir(parents=True, exist_ok=True)
        for folder in (work, output):
            probe = folder / f".folio-write-test-{uuid.uuid4().hex}"
            try:
                probe.write_bytes(b"ok")
            except OSError as exc:
                raise ValueError(f"Нет доступа на запись в папку: {folder}") from exc
            finally:
                probe.unlink(missing_ok=True)
    return config


def save_settings(settings: dict[str, str], path: Path | None = None) -> dict[str, str]:
    config = validate_settings(settings)
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return config


def service_log(settings: dict[str, str]) -> Path:
    return Path(settings["work_dir"]) / "logs" / "worker.log"


def health() -> bool:
    try:
        import httpx
        response = httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1, trust_env=False)
        return response.status_code == 200 and response.json().get("ok") is True
    except Exception:
        return False


def stop_docker_worker() -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
                                capture_output=True, text=True, timeout=5, creationflags=flags)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if result.returncode == 0 and result.stdout.strip().lower() == "true":
        stopped = subprocess.run(["docker", "stop", CONTAINER], capture_output=True,
                                 text=True, timeout=40, creationflags=flags)
        if stopped.returncode:
            raise RuntimeError("Не удалось остановить Docker-версию воркера")


def service_main(path: Path) -> int:
    settings = validate_settings(load_settings(path) or {}, check_write=False)
    work = Path(settings["work_dir"])
    work.mkdir(parents=True, exist_ok=True)
    import msvcrt
    lock = (work / "service.lock").open("a+b")
    try:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return 1
    log_path = service_log(settings)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path = work / "stop.flag"
    stop_path.unlink(missing_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as stream:
        sys.stdout = stream
        sys.stderr = stream
        os.environ.update({
            "DATA_DIR": str(work / "data"),
            "SPOOL_DIR": str(work / "spool"),
            "PRINT_SHARE_PATH": settings["output_dir"],
            "FOLIO_POLL_URL": settings["folio_poll_url"],
            "WORKER_API_KEY": settings["worker_api_key"],
            "AITUNNEL_API_KEY": settings.get("aitunnel_api_key", ""),
            "AITUNNEL_BASE_URL": settings["aitunnel_base_url"],
            "NANOBANANA_API_KEY": settings.get("nanobanana_api_key", ""),
            "NANOBANANA_MODE": "live",
            "IMAGE_PROVIDER": settings["provider"],
        })
        import uvicorn
        stopped = threading.Event()
        server = uvicorn.Server(uvicorn.Config("folio_worker.app:app", host="127.0.0.1",
                                              port=PORT, log_level="info", access_log=False))

        def watch_stop() -> None:
            while not stopped.wait(1):
                if stop_path.exists():
                    server.should_exit = True
                    return

        threading.Thread(target=watch_stop, daemon=True, name="stop-watcher").start()
        print("FolioImageWorker: native service started", flush=True)
        try:
            server.run()
        finally:
            stopped.set()
            stop_path.unlink(missing_ok=True)
            print("FolioImageWorker: native service stopped", flush=True)
    lock.close()
    return 0


def gui_main() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"FolioImageWorker {__version__}")
    root.geometry("830x580")
    root.minsize(710, 430)
    root.withdraw()
    settings = load_settings()

    def settings_dialog(current: dict[str, str] | None) -> dict[str, str] | None:
        dialog = tk.Toplevel(root)
        dialog.title("Настройки FolioImageWorker")
        dialog.geometry("760x420")
        dialog.transient(root)
        dialog.grab_set()
        values = current or default_settings()
        fields = [
            ("Ссылка опроса Folio", "folio_poll_url", None),
            ("Общий ключ Folio", "worker_api_key", "*"),
            ("Ключ AI Tunnel", "aitunnel_api_key", "*"),
            ("Рабочая папка: исходники Ozon, генерация, превью (локальная)", "work_dir", "folder"),
            ("Производственная папка после согласования (локальная или UNC)", "output_dir", "folder"),
            ("Ключ Nano Banana для старых заданий", "nanobanana_api_key", "*"),
        ]
        vars_ = {key: tk.StringVar(value=values.get(key, "")) for _, key, _ in fields}
        vars_["provider"] = tk.StringVar(value=values.get("provider", "aitunnel"))
        vars_["aitunnel_base_url"] = tk.StringVar(value=values.get("aitunnel_base_url", API_BASES[0]))
        result = {"value": None}
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        for index, (label, key, kind) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", pady=5)
            entry = ttk.Entry(frame, textvariable=vars_[key], show="*" if kind == "*" else "")
            entry.grid(row=index, column=1, sticky="ew", padx=8)
            bind_entry_clipboard(entry, tk)
            if kind == "folder":
                def browse(name=key):
                    selected = filedialog.askdirectory(parent=dialog)
                    if selected:
                        vars_[name].set(selected)
                ttk.Button(frame, text="Выбрать", command=browse).grid(row=index, column=2)
        row = len(fields)
        ttk.Label(frame, text="Режим").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=vars_["provider"], values=("aitunnel", "mock"),
                     state="readonly").grid(row=row, column=1, sticky="ew", padx=8)
        row += 1
        ttk.Label(frame, text="Адрес AI Tunnel").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=vars_["aitunnel_base_url"], values=API_BASES,
                     state="readonly").grid(row=row, column=1, sticky="ew", padx=8)

        def save() -> None:
            try:
                result["value"] = save_settings({key: value.get() for key, value in vars_.items()})
            except (OSError, ValueError) as exc:
                messagebox.showerror("Настройки", str(exc), parent=dialog)
                return
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=row + 1, column=0, columnspan=3, sticky="e", pady=12)
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right", padx=5)
        ttk.Button(buttons, text="Сохранить", command=save).pack(side="right", padx=5)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        root.wait_window(dialog)
        return result["value"]

    if settings is None:
        # A transient of a withdrawn Tk root is withdrawn too on Windows.
        # Show the parent before opening the first-run settings dialog.
        root.deiconify()
        settings = settings_dialog(None)
        if settings is None:
            root.destroy()
            return 0
    root.deiconify()
    status = tk.StringVar(value="Остановлен")
    layout = ttk.Frame(root, padding=10)
    layout.pack(fill="both", expand=True)
    ttk.Label(layout, textvariable=status).pack(anchor="w")
    buttons = ttk.Frame(layout)
    buttons.pack(fill="x", pady=8)
    options = ttk.Frame(layout)
    options.pack(fill="x", pady=(0, 8))
    process: subprocess.Popen | None = None
    log_offset = 0

    def refresh() -> None:
        nonlocal log_offset
        alive = health()
        status.set("Работает: опрашивает Folio" if alive else "Остановлен")
        start_button.configure(state="disabled" if alive else "normal")
        stop_button.configure(state="normal" if alive else "disabled")
        edit_button.configure(state="disabled" if alive else "normal")
        path = service_log(settings)
        if path.exists():
            size = path.stat().st_size
            if size < log_offset:
                log_offset = 0
            with path.open("rb") as stream:
                stream.seek(log_offset)
                chunk = stream.read(64 * 1024)
                log_offset = stream.tell()
            if chunk:
                logbox.configure(state="normal")
                logbox.insert("end", chunk.decode("utf-8", errors="replace"))
                lines = int(logbox.index("end-1c").split(".")[0])
                if lines > 500:
                    logbox.delete("1.0", f"{lines - 500}.0")
                logbox.see("end")
                logbox.configure(state="disabled")
        root.after(1000, refresh)

    def start() -> None:
        nonlocal process
        if health():
            return
        try:
            validate_settings(settings)
            stop_docker_worker()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            command = ([sys.executable] if getattr(sys, "frozen", False)
                       else [sys.executable, str(Path(__file__).resolve())])
            command += ["--service", "--config", str(config_path())]
            process = subprocess.Popen(command, creationflags=flags,
                                       cwd=str(Path(settings["work_dir"])))
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("Запуск", str(exc), parent=root)

    def stop() -> None:
        work = Path(settings["work_dir"])
        (work / "stop.flag").write_text("stop", encoding="utf-8")

    def edit() -> None:
        nonlocal settings, log_offset
        changed = settings_dialog(settings)
        if changed is not None:
            settings = changed
            log_offset = 0
            logbox.configure(state="normal")
            logbox.delete("1.0", "end")
            logbox.configure(state="disabled")

    def open_output() -> None:
        try:
            path = Path(normalize_output_dir(settings["output_dir"]))
            if not path.is_dir():
                raise OSError(f"Производственная папка недоступна: {path}")
            os.startfile(str(path))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Производственная папка", str(exc), parent=root)

    def open_work() -> None:
        try:
            os.startfile(settings["work_dir"])
        except OSError as exc:
            messagebox.showerror("Рабочая папка", str(exc), parent=root)

    def toggle_autostart() -> None:
        try:
            set_autostart(not autostart_enabled())
            autostart_button.configure(text="Автозапуск: выключить" if autostart_enabled()
                                       else "Автозапуск: включить")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Автозапуск", str(exc), parent=root)

    start_button = ttk.Button(buttons, text="Запустить", command=start)
    start_button.pack(side="left", padx=4)
    stop_button = ttk.Button(buttons, text="Остановить", command=stop)
    stop_button.pack(side="left", padx=4)
    edit_button = ttk.Button(buttons, text="Настройки", command=edit)
    edit_button.pack(side="left", padx=4)
    ttk.Button(buttons, text="Открыть папку JPG", command=open_output).pack(side="left", padx=4)
    ttk.Button(buttons, text="Открыть рабочую папку", command=open_work).pack(side="left", padx=4)
    autostart_button = ttk.Button(options, text="Автозапуск: выключить" if autostart_enabled()
                                  else "Автозапуск: включить", command=toggle_autostart)
    autostart_button.pack(side="left", padx=4)
    logbox = tk.Text(layout, wrap="none", state="disabled")
    logbox.pack(fill="both", expand=True)

    def close() -> None:
        if health() and not messagebox.askyesno("Выход", "Остановить воркер и закрыть окно?", parent=root):
            return
        if health():
            stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(1000, refresh)
    root.after(200, start)
    root.mainloop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--config", type=Path, default=config_path())
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-output", type=Path)
    args = parser.parse_args()
    if args.self_test:
        import fastapi, httpx, PIL.Image, uvicorn  # noqa: F401
        import tkinter as tk
        from tkinter import ttk
        from folio_worker import app as worker_app
        probe_sheet = PIL.Image.new("L", (400, 100), 255)
        worker_app.draw_print_label(probe_sheet, "Артикул: ТЕСТ-1", bottom=False)
        if probe_sheet.getextrema()[0] > 100:
            raise RuntimeError("Print label is not drawn")
        probe_root = tk.Tk()
        probe_root.withdraw()
        try:
            entry = ttk.Entry(probe_root)
            entry.insert(0, "old")
            entry.select_range(0, "end")
            entry.clipboard_get = lambda: "new"
            bind_entry_clipboard(entry, tk)()
            if entry.get() != "new":
                raise RuntimeError("Text insertion is unavailable")
        finally:
            probe_root.destroy()
        message = "FolioImageWorker runtime OK"
        if args.self_test_output:
            args.self_test_output.write_text(message, encoding="utf-8")
        else:
            print(message)
        return 0
    if args.service:
        return service_main(args.config)
    if args.stop:
        settings = load_settings(args.config)
        if settings:
            (Path(settings["work_dir"]) / "stop.flag").write_text("stop", encoding="utf-8")
        return 0
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
