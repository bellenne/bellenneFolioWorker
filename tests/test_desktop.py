import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import MagicMock, patch

import folio_desktop as desktop


class DesktopSettingsTest(unittest.TestCase):
    def settings(self, root: Path) -> dict[str, str]:
        return {
            "folio_poll_url": "https://one.example.ru/folio/image-worker/next",
            "worker_api_key": "shared-secret",
            "aitunnel_api_key": "aitunnel-secret",
            "aitunnel_base_url": desktop.API_BASES[0],
            "provider": "aitunnel",
            "work_dir": str(root / "state"),
            "output_dir": str(root / "prints"),
            "nanobanana_api_key": "",
        }

    def test_first_run_settings_create_writable_output_and_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            settings = self.settings(root)
            self.assertEqual(desktop.save_settings(settings, path), settings)
            self.assertTrue((root / "prints").is_dir())
            self.assertEqual(desktop.load_settings(path), settings)
            self.assertEqual(desktop.service_log(settings), root / "state" / "logs" / "worker.log")

    def test_rejects_bad_folio_url_and_relative_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            settings["folio_poll_url"] = "http://one.example.ru/folio/image-worker/next"
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                desktop.validate_settings(settings, check_write=False)
            settings["folio_poll_url"] = "https://one.example.ru/folio/image-worker/next"
            settings["output_dir"] = "relative"
            with self.assertRaisesRegex(ValueError, "UNC"):
                desktop.validate_settings(settings, check_write=False)

    def test_windows_config_accepts_unc_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            settings["output_dir"] = r"\\server\prints"
            self.assertEqual(desktop.validate_settings(settings, check_write=False), settings)

    def test_pasted_unc_with_forward_slashes_is_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            settings["output_dir"] = "//192.168.2.99/макеты на печать/МОДУЛЬНЫЕ КАРТИНЫ/"
            normalized = desktop.validate_settings(settings, check_write=False)
            self.assertEqual(normalized["output_dir"],
                             r"\\192.168.2.99\макеты на печать\МОДУЛЬНЫЕ КАРТИНЫ")
            self.assertTrue(Path(normalized["output_dir"]).is_absolute())

    def test_work_and_print_folders_must_differ(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            settings["output_dir"] = settings["work_dir"]
            with self.assertRaisesRegex(ValueError, "разные папки"):
                desktop.validate_settings(settings, check_write=False)

    def test_autostart_uses_current_user_registry(self):
        key = MagicMock()
        key.__enter__.return_value = key
        with patch.object(desktop.sys, "frozen", True, create=True), \
             patch.object(desktop.sys, "executable", r"C:\Program Files\FolioImageWorker\FolioImageWorker.exe"), \
             patch.object(desktop.winreg, "CreateKey", return_value=key) as create, \
             patch.object(desktop.winreg, "OpenKey", return_value=key) as open_key, \
             patch.object(desktop.winreg, "SetValueEx") as write, \
             patch.object(desktop.winreg, "DeleteValue") as delete, \
             patch.object(desktop.winreg, "QueryValueEx", return_value=(desktop.autostart_command(), desktop.winreg.REG_SZ)):
            desktop.set_autostart(True)
            create.assert_called_once_with(desktop.winreg.HKEY_CURRENT_USER, desktop.AUTOSTART_KEY)
            write.assert_called_once_with(key, desktop.AUTOSTART_NAME, 0, desktop.winreg.REG_SZ,
                                          desktop.autostart_command())
            self.assertTrue(desktop.autostart_enabled())
            desktop.set_autostart(False)
            open_key.assert_called_with(desktop.winreg.HKEY_CURRENT_USER, desktop.AUTOSTART_KEY,
                                        0, desktop.winreg.KEY_SET_VALUE)
            delete.assert_called_once_with(key, desktop.AUTOSTART_NAME)

    def test_settings_entry_pastes_over_selected_text(self):
        root = tk.Tk()
        try:
            entry = ttk.Entry(root)
            entry.pack()
            entry.insert(0, "старое значение")
            entry.select_range(0, "end")
            paste = desktop.bind_entry_clipboard(entry, tk)
            with patch.object(entry, "clipboard_get", return_value=r"\\server\prints"):
                self.assertEqual(paste(), "break")
            self.assertEqual(entry.get(), r"\\server\prints")
            self.assertTrue(entry.bind("<Control-v>"))
            self.assertTrue(entry.bind("<Button-3>"))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
