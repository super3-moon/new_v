from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import vmd_style_tool as core
from multiwfn_recorder_qt6 import MultiwfnRecorderDialog
from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
)
from style_parameter_dialog_qt6 import StyleParameterDialog
from vmd_style_tool_qt6 import MainWindow, preferred_window_size


class QtInterfaceSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_config = core.CONFIG_FILE
        self.original_custom_styles = core.CUSTOM_STYLES_FILE
        core.CONFIG_FILE = Path(self.temp_dir.name) / "config.json"
        core.CUSTOM_STYLES_FILE = Path(self.temp_dir.name) / "custom_styles.json"
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

    def test_default_window_size_is_compact_and_screen_aware(self) -> None:
        self.assertEqual(preferred_window_size(1536, 816), (998, 701))
        self.assertEqual(preferred_window_size(1920, 1040), (1100, 760))
        self.assertEqual(preferred_window_size(1024, 640), (960, 620))

        window = MainWindow()
        try:
            self.assertEqual((window.minimumWidth(), window.minimumHeight()), (960, 620))
            self.assertLessEqual(window.width(), 1100)
            self.assertLessEqual(window.height(), 760)
        finally:
            window.close()

    def test_compact_layout_reflows_without_hiding_primary_actions(self) -> None:
        window = MainWindow()
        try:
            window.resize(1000, 700)
            window.show()
            self.app.processEvents()

            self.assertTrue(window.path_section.isHidden())
            self.assertTrue(window.log_section.isHidden())
            self.assertEqual(window.split_inner.orientation(), Qt.Vertical)

            window._show_custom_import()
            window._set_import_page(1)
            self.app.processEvents()
            self.assertTrue(window.btn_ai_recognize.isVisible())
            self.assertGreaterEqual(
                window.btn_ai_recognize.width(),
                window.btn_ai_recognize.sizeHint().width() - 2,
            )

            window._show_batch_page()
            self.app.processEvents()
            self.assertEqual(window.batch_page.task_splitter.orientation(), Qt.Vertical)
            self.assertEqual(window.batch_page.result_splitter.orientation(), Qt.Vertical)
        finally:
            window.close()

    def tearDown(self) -> None:
        core.CONFIG_FILE = self.original_config
        core.CUSTOM_STYLES_FILE = self.original_custom_styles
        self.temp_dir.cleanup()

    def test_search_sort_theme_and_output_controls_initialize(self) -> None:
        window = MainWindow()
        try:
            self.assertGreater(window.material_filter_combo.count(), 1)
            self.assertEqual(window.style_sort_combo.count(), 4)
            self.assertEqual(window.out_dir_edit.text(), self.temp_dir.name)
            self.assertGreaterEqual(window.batch_page.preset_combo.count(), 3)

            window._show_batch_page()
            self.assertEqual(window.stack.currentIndex(), window.batch_page_index)
            self.assertTrue(window.nav_batch_btn.isChecked())
            self.assertTrue(window.page_header.isHidden())
            self.assertIn("100", window.batch_page.sequence_editor.text())

            window.style_search_edit.setText("glossy")
            self.assertGreater(len(window.bundle_grid.cards), 0)
            self.assertLess(len(window.bundle_grid.cards), len(window.bundle_styles))

            window.dark_mode = True
            window._apply_styles()
            self.assertIn("#101722", window.styleSheet())
        finally:
            window.close()

    def test_ai_recognition_worker_completes_without_blocking_ui(self) -> None:
        window = MainWindow()
        image_path = core.STYLE_DIR / "07_glossy_default.jpg"
        window.ai_image_edit.setText(str(image_path))
        try:
            with mock.patch.object(
                core, "recognize_ai_style_from_image", return_value={}
            ):
                window._recognize_ai_style()
                self.assertIsNotNone(window.ai_thread)
                self.assertFalse(window.btn_ai_recognize.isEnabled())
                deadline = time.monotonic() + 3.0
                while window.ai_thread is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
            self.assertIsNone(window.ai_thread)
            self.assertTrue(window.btn_ai_recognize.isEnabled())
            self.assertIsNotNone(window.ai_current_guess)
        finally:
            window.close()

    def test_batch_workspace_labels_fit_and_transitions_are_animated(self) -> None:
        window = MainWindow()
        try:
            window.resize(1260, 780)
            window.show()
            window._show_batch_page()
            window.batch_page.workspace_tabs.setCurrentIndex(1)
            self.app.processEvents()

            self.assertEqual(window.batch_page.workspace_tabs.count(), 3)
            self.assertFalse(window.batch_page.advanced_container.isVisible())
            window.batch_page.advanced_toggle.setChecked(True)
            self.app.processEvents()
            self.assertTrue(window.batch_page.advanced_container.isVisible())
            self.assertIsNotNone(window._page_animation)
            self.assertIsNotNone(window.batch_page._tab_animation)

            clipped = []
            for label in window.batch_page.findChildren(QLabel):
                if (
                    label.isVisible()
                    and label.text().strip()
                    and not label.wordWrap()
                    and label.sizeHint().width() > label.width() + 4
                ):
                    clipped.append(label.text())
            self.assertEqual(clipped, [])
        finally:
            window.close()

    def test_batch_pages_scroll_and_empty_result_actions_are_interactive(self) -> None:
        window = MainWindow()
        try:
            window.resize(1260, 780)
            window.show()
            window._show_batch_page()
            page = window.batch_page
            self.app.processEvents()

            for index in range(page.workspace_tabs.count()):
                self.assertIsInstance(page.workspace_tabs.widget(index), QScrollArea)

            page.workspace_tabs.setCurrentIndex(1)
            self.app.processEvents()
            self.assertGreater(page.template_scroll.verticalScrollBar().maximum(), 0)
            self.assertFalse(page.manual_output_container.isVisible())
            self.assertTrue(page.common_output_checks["structure"].isChecked())

            page.common_output_checks["cube"].setChecked(True)
            self.assertIn("*.cub", [rule.pattern for rule in page._parse_output_rules()])
            page.common_output_checks["cube"].setChecked(False)
            self.assertNotIn("*.cub", [rule.pattern for rule in page._parse_output_rules()])

            page.sequence_editor.set_text("1\n2")
            page.sequence_editor.raw_edit.moveCursor(QTextCursor.End)
            page.sequence_editor.blank_button.click()
            self.app.processEvents()
            self.assertEqual(page.sequence_editor.text(), "1\n2\n")

            output_rows = page.output_rules_table.rowCount()
            page._add_output_rule_row("density.cub", "${stem}_density.cub")
            self.assertEqual(page.output_rules_table.rowCount(), output_rows + 1)

            variable_rows = page.variables_table.rowCount()
            page.add_variable_button.click()
            self.app.processEvents()
            self.assertEqual(page.variables_table.rowCount(), variable_rows + 1)

            page.workspace_tabs.setCurrentIndex(2)
            self.app.processEvents()
            self.assertEqual(page.result_stack.currentIndex(), 0)
            page.empty_back_button.click()
            self.app.processEvents()
            self.assertEqual(page.workspace_tabs.currentIndex(), 0)

            page._populate_queue([Path(self.temp_dir.name) / "sample.fch"])
            self.assertEqual(page.result_stack.currentIndex(), 1)
            self.assertEqual(page.queue_table.rowCount(), 1)
        finally:
            window.close()

    def test_multiwfn_recorder_preserves_blank_steps_and_detects_outputs(self) -> None:
        root = Path(self.temp_dir.name)
        fake_exe = root / "Multiwfn.exe"
        input_file = root / "sample.fch"
        fake_exe.write_bytes(b"")
        input_file.write_text("sample", encoding="utf-8")
        dialog = MultiwfnRecorderDialog(
            fake_exe, input_file, auto_start=False
        )
        try:
            dialog.recorded_commands = ["5", "", "2", "q"]
            self.assertEqual(dialog.sequence_text, "5\n\n2\nq\n")
            output = root / "density.cub"
            output.write_text("cube", encoding="utf-8")
            dialog._detect_generated_files()
            self.assertEqual(dialog.generated_files, [output.resolve()])
        finally:
            dialog.close()

    def test_recorder_enter_records_exactly_one_command(self) -> None:
        root = Path(self.temp_dir.name)
        fake_multiwfn = root / "fake_multiwfn.py"
        fake_multiwfn.write_text(
            "import sys\n"
            "print('ready', flush=True)\n"
            "for line in sys.stdin:\n"
            "    value = line.rstrip('\\r\\n')\n"
            "    print('got:' + value, flush=True)\n"
            "    if value == 'q':\n"
            "        raise SystemExit(0)\n",
            encoding="utf-8",
        )
        dialog = MultiwfnRecorderDialog(
            Path(sys.executable), fake_multiwfn, auto_start=True
        )
        try:
            dialog.show()
            deadline = time.monotonic() + 4
            while (
                dialog.process.state() != QProcess.Running
                and time.monotonic() < deadline
            ):
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(dialog.process.state(), QProcess.Running)

            dialog.command_edit.setText("200")
            QTest.keyClick(dialog.command_edit, Qt.Key_Return)
            self.app.processEvents()
            self.assertEqual(dialog.recorded_commands, ["200"])

            dialog.command_edit.setText("q")
            QTest.keyClick(dialog.command_edit, Qt.Key_Return)
            deadline = time.monotonic() + 4
            while (
                dialog.process.state() != QProcess.NotRunning
                and time.monotonic() < deadline
            ):
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(dialog.recorded_commands, ["200", "q"])
            self.assertEqual(dialog._last_exit_code, 0)
            self.assertTrue(dialog.use_button.isEnabled())
        finally:
            dialog.close()

    def test_workspace_navigation_and_clear_flow_toolbar(self) -> None:
        window = MainWindow()
        try:
            def visible_clipped_labels() -> list[str]:
                return [
                    label.text()
                    for label in window.findChildren(QLabel)
                    if label.isVisible()
                    and label.text().strip()
                    and not label.wordWrap()
                    and label.sizeHint().width() > label.width() + 4
                ]

            window.resize(1260, 780)
            window.show()
            self.app.processEvents()

            self.assertEqual(window.nav_style_btn.text(), "绘图方案")
            self.assertTrue(window.nav_style_btn.isChecked())
            self.assertTrue(window.filter_bar.isVisible())
            self.assertTrue(window.style_mode_section.isVisible())
            self.assertFalse(window.nav_custom_btn.isChecked())
            self.assertFalse(window.nav_batch_btn.isChecked())

            window._show_direct_workflow()
            self.app.processEvents()
            self.assertEqual(window.stack.currentIndex(), window.direct_page_index)
            self.assertFalse(window.style_mode_section.isVisible())
            self.assertEqual(window.direct_page.back_button.text(), "返回绘图方案")
            window.direct_page.back_button.click()
            self.app.processEvents()
            self.assertTrue(window.style_mode_section.isVisible())

            window.nav_custom_btn.click()
            self.app.processEvents()
            self.assertEqual(window.stack.currentIndex(), window.custom_page_index)
            self.assertEqual(window.main_title.text(), "自定义风格")
            self.assertFalse(window.filter_bar.isVisible())
            self.assertFalse(window.style_mode_section.isVisible())
            self.assertTrue(window.nav_custom_btn.isChecked())
            self.assertEqual(visible_clipped_labels(), [])

            window.nav_batch_btn.click()
            self.app.processEvents()
            self.assertEqual(window.stack.currentIndex(), window.batch_page_index)
            self.assertTrue(window.nav_batch_btn.isChecked())
            self.assertFalse(window.page_header.isVisible())
            self.assertFalse(window.style_mode_section.isVisible())
            page = window.batch_page
            toolbar_buttons = [
                button.text()
                for button in page.findChildren(QPushButton)
                if button.isVisible()
            ]
            self.assertNotIn("查看或编辑流程", toolbar_buttons)
            self.assertNotIn("流程管理", toolbar_buttons)
            self.assertEqual(
                [
                    page.new_preset_button.text(),
                    page.copy_preset_button.text(),
                    page.import_preset_button.text(),
                    page.export_preset_button.text(),
                ],
                ["+", "复制", "导入", "导出流程"],
            )
            self.assertEqual(page.new_preset_button.accessibleName(), "新建空白流程")
            self.assertIn("JSON", page.import_preset_button.toolTip())
            self.assertIn("JSON", page.export_preset_button.toolTip())
            self.assertEqual(page.workspace_tabs.count(), 3)

            page.new_preset_button.click()
            self.app.processEvents()
            self.assertEqual(page.workspace_tabs.currentIndex(), 1)
            self.assertEqual(page.sequence_editor.text(), "")
            self.assertIn("2026.7.11", page.version_edit.text())
            self.assertIn(".xyz", page._current_extensions())
            self.assertEqual(
                page.preset_combo.currentData(), page.DRAFT_PRESET_ID
            )
            self.assertEqual(page.preset_summary_label.text(), "未保存草稿")
            with mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.Discard
            ):
                page.copy_preset_button.click()
            self.app.processEvents()
            self.assertTrue(page.preset_name_edit.text().endswith("副本"))
            self.assertEqual(
                page.preset_combo.currentData(), page.DRAFT_PRESET_ID
            )
            self.assertEqual(visible_clipped_labels(), [])

            window.nav_style_btn.click()
            self.app.processEvents()
            self.assertEqual(window.stack.currentIndex(), window._style_stack_index())
            self.assertTrue(window.page_header.isVisible())
            self.assertTrue(window.filter_bar.isVisible())
            self.assertTrue(window.style_mode_section.isVisible())
            self.assertTrue(window.nav_style_btn.isChecked())
            self.assertEqual(visible_clipped_labels(), [])
        finally:
            window.close()

    def test_batch_unsaved_switch_guard_drop_and_trial_continuation(self) -> None:
        window = MainWindow()
        try:
            window.resize(1260, 780)
            window.show()
            window._show_batch_page()
            page = window.batch_page
            self.app.processEvents()

            original_id = str(page.preset_combo.currentData())
            original_name = page.preset_name_edit.text()
            target_index = 1 if page.preset_combo.count() > 1 else 0
            target_id = str(page.preset_combo.itemData(target_index))
            page.preset_name_edit.setText(original_name + " 临时修改")
            self.assertEqual(page.preset_summary_label.text(), "有未保存修改")

            with mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.Cancel
            ):
                page.preset_combo.setCurrentIndex(target_index)
            self.app.processEvents()
            self.assertEqual(str(page.preset_combo.currentData()), original_id)
            self.assertTrue(page.preset_name_edit.text().endswith("临时修改"))

            with mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.Discard
            ):
                page.preset_combo.setCurrentIndex(target_index)
            self.app.processEvents()
            self.assertEqual(str(page.preset_combo.currentData()), target_id)
            self.assertFalse(page._editor_dirty)

            first = Path(self.temp_dir.name) / "first.fch"
            second = Path(self.temp_dir.name) / "second.fch"
            third = Path(self.temp_dir.name) / "third.fch"
            ignored = Path(self.temp_dir.name) / "ignored.bin"
            for path in (first, second, third, ignored):
                path.write_text("test", encoding="utf-8")

            page._append_files([first])
            page.file_table.item(0, 0).setCheckState(Qt.Unchecked)
            page._append_files([second])
            self.assertEqual(
                page.file_table.item(0, 0).checkState(), Qt.Unchecked
            )
            page._handle_dropped_paths([third.parent])
            self.assertIn(third.resolve(), page.files)
            self.assertNotIn(ignored.resolve(), page.files)

            page.file_table.item(0, 0).setCheckState(Qt.Checked)
            page.workspace_tabs.setCurrentIndex(2)
            self.app.processEvents()
            page._active_run_mode = "trial"
            with mock.patch.object(QMessageBox, "information"):
                page._on_worker_finished(
                    {
                        "run_dir": self.temp_dir.name,
                        "success": 1,
                        "failed": 0,
                        "cancelled": 0,
                        "summary": "试运行通过",
                    },
                    None,
                )
            self.assertTrue(page.continue_batch_button.isVisible())
            self.assertIn(
                str(len(page._enabled_files())),
                page.continue_batch_button.text(),
            )
            with mock.patch.object(page, "_start_run") as start_run:
                page._continue_full_batch()
                start_run.assert_called_once_with(False)
        finally:
            window.close()

    def test_direct_workflow_uses_input_folder_and_forces_vmd_output_there(self) -> None:
        window = MainWindow()
        try:
            cube = Path(self.temp_dir.name) / "sample.cub"
            cube.write_text("cube", encoding="utf-8")
            fake_vmd = Path(self.temp_dir.name) / "vmd.exe"
            fake_vmd.write_bytes(b"")
            window.vmd_edit.setText(str(fake_vmd))

            window._show_direct_workflow()
            self.assertEqual(window.stack.currentIndex(), window.direct_page_index)
            self.assertEqual(window.main_title.text(), "直接绘图")
            self.assertFalse(window.style_action_bar.isVisible())

            page = window.direct_page
            page.set_source_file(str(cube))
            page.iso_edit.setText("0.05")
            expected_dir = str(Path(self.temp_dir.name).resolve())
            self.assertEqual(page.output_dir_edit.text(), expected_dir)
            self.assertEqual(page.start_button.text(), "在 VMD 中直接绘图")
            direct_scroll = page.findChild(QScrollArea, "directWorkflowScroll")
            self.assertIsNotNone(direct_scroll)

            window.resize(1260, 780)
            window.show()
            self.app.processEvents()
            clipped = [
                label.text()
                for label in page.findChildren(QLabel)
                if (
                    label.isVisible()
                    and label.text().strip()
                    and not label.wordWrap()
                    and label.sizeHint().width() > label.width() + 4
                )
            ]
            self.assertEqual(clipped, [])

            fake_process = mock.Mock()
            fake_process.poll.return_value = None
            with mock.patch(
                "direct_workflow_qt6.subprocess.Popen", return_value=fake_process
            ) as popen:
                page.start_workflow()
            _, kwargs = popen.call_args
            self.assertEqual(kwargs["cwd"], expected_dir)
            self.assertEqual(kwargs["env"]["A_DIR"], expected_dir)
            self.assertEqual(kwargs["env"]["CUBE_FILE"], str(cube.resolve()))
            self.assertTrue(Path(popen.call_args.args[0][2]).is_file())

            page.process_timer.stop()
            page.vmd_process = None
            page.cleanup()
        finally:
            window.close()

    def test_direct_workflow_opens_visible_multiwfn_for_non_cube_input(self) -> None:
        window = MainWindow()
        try:
            source = Path(self.temp_dir.name) / "sample.wfn"
            source.write_text("wavefunction", encoding="utf-8")
            fake_multi = Path(self.temp_dir.name) / "Multiwfn.exe"
            fake_multi.write_bytes(b"")
            window.multi_edit.setText(str(fake_multi))
            window._show_direct_workflow()

            page = window.direct_page
            page.set_source_file(str(source))
            page.iso_edit.setText("0.05")
            self.assertEqual(page.start_button.text(), "打开 Multiwfn 并继续")

            fake_process = mock.Mock()
            fake_process.poll.return_value = None
            with mock.patch(
                "direct_workflow_qt6.subprocess.Popen", return_value=fake_process
            ) as popen:
                page.start_workflow()

            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(fake_multi.resolve()), str(source.resolve())])
            self.assertEqual(kwargs["cwd"], str(source.parent.resolve()))
            self.assertEqual(kwargs["env"]["Multiwfnpath"], str(fake_multi.parent.resolve()))
            if os.name == "nt":
                self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NEW_CONSOLE)

            page.process_timer.stop()
            page.multiwfn_process = None
        finally:
            window.close()

    def test_direct_workflow_completion_actions_delete_only_new_intermediates(self) -> None:
        window = MainWindow()
        try:
            page = window.direct_page
            source = Path(self.temp_dir.name) / "source.wfn"
            created_cube = Path(self.temp_dir.name) / "new.cub"
            created_dat = Path(self.temp_dir.name) / "new.dat"
            render = Path(self.temp_dir.name) / "render.png"
            source.write_text("input", encoding="utf-8")
            created_cube.write_text("cube", encoding="utf-8")
            created_dat.write_text("dat", encoding="utf-8")
            render.write_text("image", encoding="utf-8")
            page.set_source_file(str(source))
            page.generated_intermediates.update({created_cube, created_dat})

            finished_process = mock.Mock()
            finished_process.poll.return_value = 0
            page.vmd_process = finished_process
            page._poll_processes()
            self.assertFalse(page.finish_keep_button.isHidden())
            self.assertFalse(page.finish_delete_button.isHidden())
            self.assertEqual(page.start_button.text(), "重新打开 VMD")

            with mock.patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._finish_workflow(delete_intermediates=True)
            self.assertFalse(created_cube.exists())
            self.assertFalse(created_dat.exists())
            self.assertTrue(source.exists())
            self.assertTrue(render.exists())
            self.assertIsNone(page.source_path)
        finally:
            window.close()

    def test_style_parameter_dialog_exposes_visual_controls_and_manual_edit(self) -> None:
        style = core.STYLE_BY_ID["soft_glossy_449"]
        dialog = StyleParameterDialog(style, None, "套装风格：Soft Artistic Glossy")
        try:
            dialog.show()
            self.app.processEvents()
            self.assertEqual(dialog.material_combo.currentText(), "Glossy")
            self.assertEqual(len(dialog.material_rows), 8)
            self.assertFalse(dialog.material_rows["specular"][0].isChecked())
            self.assertTrue(dialog.material_rows["opacity"][0].isChecked())
            self.assertTrue(dialog.edit_button.isVisible())
            self.assertTrue(dialog.summary_card.isVisible())
            self.assertFalse(dialog.material_combo.isVisible())
            self.assertFalse(dialog.save_card.isVisible())

            dialog.edit_button.click()
            self.app.processEvents()
            self.assertFalse(dialog.summary_card.isVisible())
            self.assertTrue(dialog.material_combo.isVisible())
            self.assertTrue(dialog.save_card.isVisible())
            dialog.name_edit.setText("手动参数测试")
            dialog._save()
            self.assertIsNotNone(dialog.saved_style)
            self.assertTrue(dialog.saved_style["is_custom"])
            self.assertNotEqual(dialog.saved_style["id"], style["id"])
        finally:
            dialog.close()

    def test_direct_workflow_no_longer_shows_step_option_strip(self) -> None:
        window = MainWindow()
        try:
            self.assertEqual(window.direct_page.findChildren(QLabel, "workflowStep"), [])
        finally:
            window.close()

    def test_ui_copy_avoids_internal_badges_and_duplicate_status(self) -> None:
        window = MainWindow()
        style = core.STYLE_BY_ID["soft_glossy_449"]
        dialog = StyleParameterDialog(style, None, "套装风格：Soft Artistic Glossy")
        try:
            window_text = "\n".join(
                label.text() for label in window.findChildren(QLabel)
            )
            dialog_text = "\n".join(
                label.text() for label in dialog.findChildren(QLabel)
            )
            combined = window_text + "\n" + dialog_text
            for unwanted in (
                "只读原件",
                "独立任务空间",
                "当前为参数查看模式",
                "开始后会自动切换",
                "独立任务目录",
                "不会擅自",
            ):
                self.assertNotIn(unwanted, combined)

            self.assertFalse(hasattr(window, "detail_label"))
            self.assertNotIn("软件已启动", window.log_view.toPlainText())
            self.assertNotIn("· 内置", window.batch_page.preset_combo.currentText())
            self.assertNotIn("· 自定义", window.batch_page.preset_combo.currentText())
            self.assertEqual(window.count_label.text(), f"{len(window.bundle_styles)} 个风格")
            self.assertTrue(
                all(combo.itemText(0) == "不修改" for combo in dialog.state_combos.values())
            )
            self.assertEqual(
                dialog.findChildren(QLabel, "parameterBadge"),
                [],
            )
        finally:
            dialog.close()
            window.close()


if __name__ == "__main__":
    unittest.main()
