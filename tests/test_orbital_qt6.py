from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

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
                page._add_paths(
                    [
                        self.gaussian_out,
                        self.gaussian_fchk,
                    ]
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
                page._add_paths(
                    [
                        self.gaussian_out,
                        self.gaussian_fchk,
                    ]
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
                    top_left = combo.mapTo(scroll.viewport(), QPoint(0, 0))
                    self.assertGreaterEqual(top_left.x(), 0)
                    self.assertLessEqual(
                        top_left.x() + combo.width(), scroll.viewport().width()
                    )
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
                page._add_paths([first_out, first_fchk, second_out, second_fchk])
                self.assertTrue(page.unpaired_issue)
                selection = page.input_table.selectionModel()
                flags = (
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows
                )
                selection.select(page.input_table.model().index(0, 0), flags)
                selection.select(page.input_table.model().index(1, 0), flags)
                page._manual_pair_selected()
                self.app.processEvents()
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
                first._add_paths([self.gaussian_out, self.gaussian_fchk])
                first.orbital_table.item(0, 1).setCheckState(Qt.CheckState.Unchecked)
                saved = first._settings()
            finally:
                first.close()
            second = OrbitalDiagramPage(root, lambda: "", lambda: "")
            try:
                second.load_settings({"orbital_diagram_settings": saved})
                second._add_paths([self.gaussian_out, self.gaussian_fchk])
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
                self.assertEqual(page.queue_table.item(0, 5).text(), "TARGET.png")
                self.assertEqual(page.queue_table.rowCount(), 2)
                self.assertEqual(page.queue_table.item(1, 3).text(), "失败")
                self.assertIn("仍有 1 个失败任务", page.run_state_label.text())
            finally:
                page.close()


if __name__ == "__main__":
    unittest.main()
