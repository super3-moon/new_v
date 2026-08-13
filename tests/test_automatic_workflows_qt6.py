from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import vmd_style_tool as core
from automatic_workflows_qt6 import AutomationStyleDialog
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QPushButton
from vmd_style_tool_qt6 import MainWindow


class AutomaticWorkflowInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_config = core.CONFIG_FILE
        self.original_custom_styles = core.CUSTOM_STYLES_FILE
        core.CONFIG_FILE = Path(self.temp_dir.name) / "config.json"
        core.CUSTOM_STYLES_FILE = Path(self.temp_dir.name) / "custom.json"
        core.CONFIG_FILE.write_text(
            json.dumps(
                {
                    "multiwfn_exe": "missing-multiwfn.exe",
                    "vmd_exe": "missing-vmd.exe",
                    "output_dir": self.temp_dir.name,
                    "theme": "light",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        core.CONFIG_FILE = self.original_config
        core.CUSTOM_STYLES_FILE = self.original_custom_styles
        self.temp_dir.cleanup()

    def test_workspace_has_independent_automatic_workflow_module(self) -> None:
        window = MainWindow()
        try:
            window.resize(1260, 780)
            window.show()
            window.nav_automation_btn.click()
            self.app.processEvents()
            self.assertEqual(window.nav_automation_btn.text(), "全自动流程")
            self.assertEqual(window.nav_batch_btn.text(), "批量 Multiwfn")
            self.assertEqual(window.stack.currentIndex(), window.automation_page_index)
            self.assertTrue(window.nav_automation_btn.isChecked())
            self.assertFalse(window.nav_batch_btn.isChecked())
            self.assertFalse(window.page_header.isVisible())
            self.assertEqual(window.automation_page.page_stack.currentIndex(), 0)
            texts = "\n".join(
                label.text() for label in window.automation_page.findChildren(QLabel)
            )
            self.assertIn("表面静电势图", texts)
            self.assertIn("2 个流程", texts)
            self.assertIn("分子轨道能级图", texts)
        finally:
            window.close()

    def test_configuration_and_results_are_scrollable_and_labels_fit(self) -> None:
        window = MainWindow()
        try:
            window.resize(1260, 780)
            window.show()
            window._show_automation_page()
            page = window.automation_page
            start = next(
                button
                for button in page.findChildren(QPushButton)
                if button.text() == "开始配置"
            )
            start.click()
            self.app.processEvents()
            self.assertEqual(page.page_stack.currentIndex(), 1)
            self.assertIsInstance(page.page_stack.widget(0), QScrollArea)
            self.assertIsInstance(page.configuration_scroll, QScrollArea)
            self.assertIsInstance(page.results_scroll, QScrollArea)
            self.assertEqual(
                page.style_snapshot["style"]["surface_mode"], "volume_mapped"
            )
            settings = page._settings()
            self.assertEqual(settings["rho_iso"], "0.001")
            self.assertEqual((settings["width"], settings["height"]), (1600, 1200))
            clipped = [
                label.text()
                for label in page.findChildren(QLabel)
                if label.isVisible()
                and label.text().strip()
                and not label.wordWrap()
                and label.sizeHint().width() > label.width() + 4
            ]
            self.assertEqual(clipped, [])
        finally:
            window.close()

    def test_style_dialog_only_lists_esp_compatible_schemes(self) -> None:
        dialog = AutomationStyleDialog()
        try:
            self.assertGreater(len(dialog.styles), 0)
            self.assertTrue(
                all(style.get("surface_mode") == "volume_mapped" for style in dialog.styles)
            )
            dialog.mode = "split"
            selection = dialog.selection()
            self.assertEqual(selection["style"]["surface_mode"], "volume_mapped")
            self.assertEqual(len(selection["hash"]), 64)
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
