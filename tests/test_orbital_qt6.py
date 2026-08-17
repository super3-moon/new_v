from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QTableWidgetItem

from orbital_diagram_qt6 import OrbitalDiagramPage
import orbital_data
from tests.orbital_test_fixture import write_gaussian_pair


class OrbitalDiagramQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.fixture_directory = tempfile.TemporaryDirectory()
        cls.gaussian_out, cls.gaussian_fchk = write_gaussian_pair(
            Path(cls.fixture_directory.name)
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture_directory.cleanup()

    def _wait_until(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("等待界面异步操作完成超时")

    def _add_and_wait(self, page: OrbitalDiagramPage, paths) -> None:
        page._add_paths(paths)
        self._wait_until(lambda: not page.is_input_processing())

    def test_friendly_selector_pairing_and_per_orbital_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(
                Path(temporary),
                lambda: str(Path("Multiwfn_2026.7.11_bin_Win64/Multiwfn.exe").resolve()),
                lambda: str(Path("vmd19.3/vmd.exe").resolve()),
            )
            try:
                page.resize(1080, 680)
                page.show()
                self._add_and_wait(
                    page,
                    [
                        self.gaussian_out,
                        self.gaussian_fchk,
                    ],
                )
                self.app.processEvents()
                self.assertEqual(len(page.pairs), 1)
                self.assertEqual(page.pair_validity, [True])
                self.assertEqual(page.orbital_table.rowCount(), 6)
                self.assertEqual(
                    page._selection_spec()["expression"], "HOMO-1..LUMO+3"
                )
                page.orbital_table.item(0, 1).setCheckState(Qt.CheckState.Unchecked)
                settings = page._settings()
                self.assertEqual(len(settings["orbital_selections"][0]["orbitals"]), 5)
                self.assertEqual(settings["renderer"], "tachyon")
                self.assertFalse(settings["recording_required"])
                self.assertIsInstance(page.findChildren(QScrollArea)[0], QScrollArea)
                text = "\n".join(label.text() for label in page.findChildren(QLabel))
                self.assertIn("保存全部参数并确认", text)
            finally:
                page.close()

    def test_manual_expression_overrides_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            try:
                self._add_and_wait(
                    page,
                    [
                        self.gaussian_out,
                        self.gaussian_fchk,
                    ],
                )
                page.manual_expression_edit.setText("HOMO,LUMO+1")
                self.app.processEvents()
                self.assertEqual(page.orbital_table.rowCount(), 2)
                self.assertEqual(page._settings()["selection_mode"], "custom")
                self.assertEqual(page._settings()["selection_text"], "HOMO,LUMO+1")
            finally:
                page.close()

    def test_custom_range_uses_two_editable_offset_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            try:
                page.start_offset_combo.setEditText("-2")
                page.end_offset_combo.setEditText("+4")
                self.app.processEvents()

                spec = page._selection_spec()
                self.assertTrue(page.start_offset_combo.isEditable())
                self.assertTrue(page.end_offset_combo.isEditable())
                self.assertIn(
                    "-1",
                    [
                        page.start_offset_combo.itemText(index)
                        for index in range(page.start_offset_combo.count())
                    ],
                )
                self.assertIn(
                    "+3",
                    [
                        page.end_offset_combo.itemText(index)
                        for index in range(page.end_offset_combo.count())
                    ],
                )
                self.assertEqual(spec["expression"], "HOMO-2..LUMO+4")
                self.assertEqual(spec["start_anchor"], "HOMO")
                self.assertEqual(spec["start_offset"], -2)
                self.assertEqual(spec["end_anchor"], "LUMO")
                self.assertEqual(spec["end_offset"], 4)
                self.assertFalse(hasattr(page, "preset_combo"))
                self.assertFalse(hasattr(page, "start_anchor_combo"))
                self.assertFalse(hasattr(page, "end_anchor_combo"))
                visible_labels = {
                    label.text() for label in page.findChildren(QLabel)
                }
                self.assertIn("轨道范围", visible_labels)
                self.assertNotIn("快捷选择", visible_labels)
                self.assertNotIn("自定义范围", visible_labels)

                page.resize(620, 620)
                page.show()
                self.app.processEvents()
                scroll = page.page_stack.currentWidget().findChild(QScrollArea)
                self.assertIsNotNone(scroll)
                assert scroll is not None
                for combo in (page.start_offset_combo, page.end_offset_combo):
                    self.assertTrue(combo.property("explicitDropIndicator"))
                    indicator = combo.drop_indicator_rect()
                    self.assertGreater(indicator.width(), 0)
                    self.assertTrue(combo.rect().contains(indicator.center()))
                    top_left = combo.mapTo(scroll.viewport(), QPoint(0, 0))
                    self.assertGreaterEqual(top_left.x(), 0)
                    self.assertLessEqual(
                        top_left.x() + combo.width(), scroll.viewport().width()
                    )
            finally:
                page.close()

    def test_file_parsing_is_async_and_busy_state_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            page.show()
            original = orbital_data.parse_input_pair

            def delayed_parse(*args, **kwargs):
                time.sleep(0.18)
                return original(*args, **kwargs)

            try:
                with mock.patch.object(
                    orbital_data, "parse_input_pair", side_effect=delayed_parse
                ):
                    started = time.monotonic()
                    page._add_paths([self.gaussian_out, self.gaussian_fchk])
                    elapsed = time.monotonic() - started
                    self.assertLess(elapsed, 0.08)
                    self.assertTrue(page.is_input_processing())
                    self.assertFalse(page.input_progress.isHidden())
                    self.assertEqual(page.input_progress.maximum(), 0)
                    self.assertFalse(page.add_input_button.isEnabled())

                    # The GUI event queue is still serviced while the worker
                    # is deliberately held in a slow parser call.
                    page.ready_label.setText("界面仍可响应")
                    self.app.processEvents()
                    self.assertEqual(page.ready_label.text(), "界面仍可响应")
                    self._wait_until(lambda: not page.is_input_processing())

                self.assertEqual(page.pair_validity, [True])
                self.assertTrue(page.add_input_button.isEnabled())
                self.assertTrue(page.input_progress.isHidden())
                self.assertIn("读取完成", page.input_progress_label.text())
            finally:
                self._wait_until(lambda: not page.is_input_processing())
                page.close()

    def test_stale_input_worker_result_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            try:
                page._input_generation = 9
                page._on_input_worker_finished(
                    8,
                    {
                        "input_files": [self.gaussian_out],
                        "pairs": ["stale"],
                        "datasets": ["stale"],
                        "pair_validity": [True],
                    },
                    None,
                )
                self.assertEqual(page.input_files, [])
                self.assertEqual(page.pairs, [])
                self.assertEqual(page.ready_label.text(), "尚未添加输入")
            finally:
                page.close()

    def test_cleanup_cancels_and_joins_input_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            original = orbital_data.parse_input_pair

            def delayed_parse(*args, **kwargs):
                time.sleep(0.05)
                return original(*args, **kwargs)

            with mock.patch.object(
                orbital_data, "parse_input_pair", side_effect=delayed_parse
            ):
                page._add_paths([self.gaussian_out, self.gaussian_fchk])
                self.assertTrue(page.is_running())
                page.cleanup()
            self.assertIsNone(page.input_thread)
            self.assertIsNone(page.input_worker)
            self.assertFalse(page.is_running())
            page.close()

    def test_console_output_is_hidden_and_stages_are_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = orbital_data.InputPair(
                self.gaussian_out,
                self.gaussian_fchk,
                orbital_data.CalculationProgram.GAUSSIAN,
            )
            page = OrbitalDiagramPage(root, lambda: "", lambda: "")
            try:
                page._active_pairs = [pair]
                page._populate_queue([pair], ["job-1"])
                before_stage = page.queue_table.item(0, 2).text()
                page._on_worker_event(
                    {
                        "kind": "output",
                        "stage": "output",
                        "job_id": "job-1",
                        "text": "[VMD Info] internal console line",
                    }
                )
                self.assertEqual(page.queue_table.item(0, 2).text(), before_stage)
                self.assertNotIn("internal console", page.run_log.toPlainText())

                page._on_worker_event(
                    {
                        "kind": "job_started",
                        "stage": "unknown_internal_stage",
                        "status": "unknown_internal_status",
                        "job_id": "job-1",
                        "message": "任务已开始",
                    }
                )
                self.assertEqual(page.queue_table.item(0, 2).text(), "处理中")
                self.assertEqual(page.queue_table.item(0, 3).text(), "状态未知")

                page._on_worker_finished(
                    {
                        "run_dir": str(root),
                        "jobs": [
                            {
                                "id": "job-1",
                                "status": "success",
                                "stage": "output",
                                "pair": {
                                    "wavefunction_path": str(self.gaussian_fchk)
                                },
                            }
                        ],
                    },
                    None,
                )
                self.assertEqual(page.queue_table.item(0, 2).text(), "完成")

                visible_text = "\n".join(
                    label.text() for label in page.findChildren(QLabel)
                )
                headers = "\n".join(
                    page.orbital_table.horizontalHeaderItem(column).text()
                    for column in range(page.orbital_table.columnCount())
                )
                for internal_word in ("signed", "ColorID", "Multiwfn 号", "来源号"):
                    self.assertNotIn(internal_word, visible_text + headers)
            finally:
                page.close()

    def test_stage_progress_elapsed_and_result_summary_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pair = orbital_data.InputPair(
                self.gaussian_out,
                self.gaussian_fchk,
                orbital_data.CalculationProgram.GAUSSIAN,
            )
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            try:
                page._active_pairs = [pair]
                page._populate_queue([pair], ["job-1"])
                page._run_started_monotonic = time.monotonic() - 4.0
                page._on_worker_event(
                    {
                        "kind": "pair_stage",
                        "stage": "generating_reference_cube",
                        "status": "running",
                        "job_id": "job-1",
                        "wavefunction_path": str(self.gaussian_fchk),
                        "message": "正在准备参考轨道",
                    }
                )
                page._on_worker_event(
                    {
                        "kind": "progress",
                        "stage": "generating_reference_cube",
                        "status": "running",
                        "job_id": "job-1",
                        "wavefunction_path": str(self.gaussian_fchk),
                        "percent": 11,
                        "ceiling_percent": 23,
                        "message": "正在准备参考轨道",
                    }
                )
                page._tick_runtime()

                self.assertGreaterEqual(page.progress.value(), 11)
                self.assertLessEqual(page.progress.value(), 23)
                self.assertIn("准备参考轨道", page.progress.format())
                self.assertEqual(page.queue_table.item(0, 2).text(), "准备参考轨道")
                self.assertEqual(page.queue_table.item(0, 3).text(), "进行中")
                self.assertIn("正在准备参考轨道", page.queue_table.item(0, 5).text())
                self.assertNotEqual(page.queue_table.item(0, 4).text(), "-")
                self.assertIn("正在准备参考轨道", page.run_log.toPlainText())
                self.assertIn("当前进度", page.run_state_label.text())
            finally:
                page.close()

    def test_energy_anomaly_report_uses_selected_orbitals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            try:
                settings = {
                    "orbital_selections": [
                        {
                            "label": "测试任务",
                            "wavefunction_path": str(self.gaussian_fchk),
                            "orbitals": [
                                {"label": "HOMO-9", "energy_ev": -60.0},
                                {"label": "HOMO-2", "energy_ev": -8.2},
                                {"label": "HOMO-1", "energy_ev": -7.9},
                                {"label": "HOMO", "energy_ev": -7.5},
                            ],
                        }
                    ]
                }
                reports = page._energy_anomaly_reports(settings)
                self.assertEqual(len(reports), 1)
                self.assertIn("测试任务", reports[0])
                self.assertIn("HOMO-9", reports[0])
                self.assertIn("相差", reports[0])
            finally:
                page.close()

    def test_legacy_structured_range_loads_into_compact_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            try:
                page.load_settings(
                    {
                        "orbital_diagram_settings": {
                            "selection": {
                                "mode": "custom",
                                "expression": "HOMO-3..LUMO+5",
                                "start_anchor": "HOMO",
                                "start_offset": -3,
                                "end_anchor": "LUMO",
                                "end_offset": 5,
                                "spin_mode": "auto",
                            }
                        }
                    }
                )
                self.assertFalse(hasattr(page, "preset_combo"))
                self.assertEqual(page.start_offset_combo.currentText(), "-3")
                self.assertEqual(page.end_offset_combo.currentText(), "+5")
                self.assertEqual(page.manual_expression_edit.text(), "")
                self.assertEqual(
                    page._selection_spec()["expression"], "HOMO-3..LUMO+5"
                )
            finally:
                page.close()

    def test_legacy_shortcuts_migrate_without_restoring_redundant_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = OrbitalDiagramPage(Path(temporary), lambda: "", lambda: "")
            try:
                for mode, expected in (
                    ("homo", "HOMO"),
                    ("lumo", "LUMO"),
                    ("homo_lumo", "HOMO,LUMO"),
                ):
                    page.load_settings(
                        {
                            "orbital_diagram_settings": {
                                "selection": {"mode": mode, "spin_mode": "auto"}
                            }
                        }
                    )
                    self.assertEqual(page.manual_expression_edit.text(), expected)
                    self.assertEqual(page._selection_spec()["expression"], expected)

                page.load_settings(
                    {
                        "orbital_diagram_settings": {
                            "selection": {
                                "mode": "homo_minus_1_to_lumo_plus_3",
                                "spin_mode": "auto",
                            }
                        }
                    }
                )
                self.assertEqual(page.manual_expression_edit.text(), "")
                self.assertEqual(page.start_offset_combo.currentText(), "-1")
                self.assertEqual(page.end_offset_combo.currentText(), "+3")
                self.assertEqual(
                    page._selection_spec()["expression"], "HOMO-1..LUMO+3"
                )
            finally:
                page.close()

    def test_manual_pairing_handles_different_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_out, first_fchk = write_gaussian_pair(
                root, output_name="alpha.out", wavefunction_name="gamma.fchk"
            )
            second_out, second_fchk = write_gaussian_pair(
                root, output_name="beta.out", wavefunction_name="delta.fchk"
            )
            page = OrbitalDiagramPage(root, lambda: "", lambda: "")
            try:
                self._add_and_wait(
                    page, [first_out, first_fchk, second_out, second_fchk]
                )
                self.assertTrue(page.unpaired_issue)
                selection = page.input_table.selectionModel()
                flags = (
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows
                )
                selection.select(page.input_table.model().index(0, 0), flags)
                selection.select(page.input_table.model().index(1, 0), flags)
                page._manual_pair_selected()
                self._wait_until(lambda: not page.is_input_processing())
                self.assertEqual(len(page.manual_pairs), 1)
                self.assertEqual(len(page.pairs), 2)
                self.assertFalse(page.unpaired_issue)
                self.assertEqual(page.pair_validity, [True, True])
            finally:
                page.close()

    def test_saved_per_orbital_checks_are_restored_after_files_are_added(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = OrbitalDiagramPage(root, lambda: "", lambda: "")
            try:
                self._add_and_wait(first, [self.gaussian_out, self.gaussian_fchk])
                first.orbital_table.item(0, 1).setCheckState(Qt.CheckState.Unchecked)
                saved = first._settings()
            finally:
                first.close()
            second = OrbitalDiagramPage(root, lambda: "", lambda: "")
            try:
                second.load_settings({"orbital_diagram_settings": saved})
                self._add_and_wait(second, [self.gaussian_out, self.gaussian_fchk])
                self.assertEqual(
                    second.orbital_table.item(0, 1).checkState(),
                    Qt.CheckState.Unchecked,
                )
                self.assertEqual(len(second._settings()["orbital_selections"][0]["orbitals"]), 5)
            finally:
                second.close()

    def test_retry_result_ignores_other_manifest_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = orbital_data.InputPair(
                self.gaussian_out,
                self.gaussian_fchk,
                orbital_data.CalculationProgram.GAUSSIAN,
            )
            page = OrbitalDiagramPage(root, lambda: "", lambda: "")
            try:
                page._active_pairs = [target]
                page._active_job_ids = {"target"}
                page._populate_queue([target], ["target"])
                extra_row = page.queue_table.rowCount()
                page.queue_table.insertRow(extra_row)
                other_item = QTableWidgetItem("另一项失败任务")
                other_item.setData(
                    Qt.ItemDataRole.UserRole + 1, str(root / "other.fchk")
                )
                other_item.setData(Qt.ItemDataRole.UserRole + 2, "other")
                page.queue_table.setItem(extra_row, 0, other_item)
                page.queue_table.setItem(extra_row, 3, QTableWidgetItem("失败"))
                page._on_worker_finished(
                    {
                        "run_dir": str(root),
                        "jobs": [
                            {
                                "id": "other",
                                "status": "success",
                                "pair": {"wavefunction_path": str(root / "other.fchk")},
                                "diagram_path": "FIRST.png",
                            },
                            {
                                "id": "target",
                                "status": "success",
                                "pair": {"wavefunction_path": str(self.gaussian_fchk)},
                                "diagram_path": "TARGET.png",
                            },
                        ],
                    },
                    None,
                )
                self.assertEqual(page.queue_table.item(0, 3).text(), "完成")
                self.assertEqual(page.queue_table.item(0, 5).text(), "能级图已生成")
                self.assertEqual(
                    page.queue_table.item(0, 5).data(Qt.ItemDataRole.UserRole),
                    "TARGET.png",
                )
                self.assertEqual(page.queue_table.rowCount(), 2)
                self.assertEqual(page.queue_table.item(1, 3).text(), "失败")
                self.assertIn("仍有 1 个失败任务", page.run_state_label.text())
            finally:
                page.close()


if __name__ == "__main__":
    unittest.main()
