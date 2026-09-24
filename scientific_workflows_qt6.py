"""Qt page shared by the additional data-driven automatic workflows."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import scientific_workflows as science
import vmd_style_tool as core


class _ScientificWorker(QObject):
    event = Signal(object)
    finished = Signal(object, object)

    def __init__(
        self,
        workflow_id: str,
        method: str,
        inputs: dict,
        options: dict,
        output_root: Path,
        multiwfn_exe: Path,
        vmd_exe: Path,
    ) -> None:
        super().__init__()
        self.args = (
            workflow_id,
            method,
            inputs,
            options,
            output_root,
            multiwfn_exe,
            vmd_exe,
        )
        self.runner: science.ScientificWorkflowRunner | None = None
        self._cancel_pending = False

    @Slot()
    def run(self) -> None:
        try:
            self.runner = science.ScientificWorkflowRunner(
                *self.args, event_callback=self.event.emit
            )
            if self._cancel_pending:
                self.runner.cancel()
            result = self.runner.run()
        except Exception as exc:
            self.finished.emit(None, exc)
            return
        self.finished.emit(result, None)

    def cancel(self) -> None:
        self._cancel_pending = True
        if self.runner is not None:
            self.runner.cancel()


class ScientificWorkflowPage(QWidget):
    backRequested = Signal()
    settingsChanged = Signal(object)

    def __init__(
        self,
        storage_dir: Path,
        multiwfn_path_getter: Callable[[], str],
        vmd_path_getter: Callable[[], str],
        style_dialog_factory,
    ) -> None:
        super().__init__()
        self.storage_dir = Path(storage_dir)
        self.multiwfn_path_getter = multiwfn_path_getter
        self.vmd_path_getter = vmd_path_getter
        self.style_dialog_factory = style_dialog_factory
        self.spec = science.workflow_specs()[0]
        self.style_snapshot: dict = {}
        self.thread: QThread | None = None
        self.worker: _ScientificWorker | None = None
        self.last_run_dir = ""
        self.role_rows: dict[str, tuple[QLabel, QLineEdit, QPushButton]] = {}
        self._build_ui()
        self.configure(self.spec.id)

    @staticmethod
    def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("batchCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 15, 16, 16)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("batchCardTitle")
        layout.addWidget(heading)
        if hint:
            helper = QLabel(hint)
            helper.setObjectName("batchHint")
            helper.setWordWrap(True)
            layout.addWidget(helper)
        return frame, layout

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        toolbar = QFrame()
        toolbar.setObjectName("batchToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 8, 12, 8)
        back = QPushButton("返回全部流程")
        back.clicked.connect(self.backRequested.emit)
        toolbar_layout.addWidget(back)
        self.toolbar_title = QLabel()
        self.toolbar_title.setObjectName("batchToolbarLabel")
        toolbar_layout.addWidget(self.toolbar_title)
        toolbar_layout.addStretch(1)
        self.ready_badge = QLabel("等待配置")
        self.ready_badge.setObjectName("batchPresetInline")
        toolbar_layout.addWidget(self.ready_badge)
        root.addWidget(toolbar)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 8, 8)
        layout.setSpacing(12)

        method_card, method_layout = self._card(
            "1 · 选择分析方法",
            "选择适合当前分析目标的方法。",
        )
        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("分析方法"))
        self.method_combo = QComboBox()
        self.method_combo.currentIndexChanged.connect(self._sync_method_options)
        method_row.addWidget(self.method_combo, 1)
        method_layout.addLayout(method_row)
        self.method_note = QLabel()
        self.method_note.setObjectName("detailLabel")
        self.method_note.setWordWrap(True)
        method_layout.addWidget(self.method_note)
        self.weak_scatter_check = QCheckBox(
            "同时生成填色散点图"
        )
        self.weak_scatter_check.setChecked(False)
        self.weak_scatter_check.setToolTip(
            "RDG/NCI 绘制 RDG，IRI 绘制 IRI，IGMH 绘制片段间 δg；需要 Gnuplot。"
        )
        self.weak_scatter_check.toggled.connect(self._sync_weak_scatter_path)
        method_layout.addWidget(self.weak_scatter_check)

        self.gnuplot_row_widget = QWidget()
        gnuplot_row = QHBoxLayout(self.gnuplot_row_widget)
        gnuplot_row.setContentsMargins(0, 0, 0, 0)
        gnuplot_row.setSpacing(8)
        gnuplot_row.addWidget(QLabel("Gnuplot 程序"))
        self.gnuplot_edit = QLineEdit()
        self.gnuplot_edit.setPlaceholderText("选择 Gnuplot 安装目录 bin 文件夹中的 gnuplot.exe")
        gnuplot_row.addWidget(self.gnuplot_edit, 1)
        browse_gnuplot = QPushButton("浏览")
        browse_gnuplot.clicked.connect(self._browse_gnuplot)
        gnuplot_row.addWidget(browse_gnuplot)
        method_layout.addWidget(self.gnuplot_row_widget)

        self.igmh_prescreen_check = QCheckBox(
            "仅计算片段表面重叠区域（推荐，可显著减少 IGMH 耗时）"
        )
        self.igmh_prescreen_check.setChecked(True)
        self.igmh_prescreen_check.setToolTip(
            "只计算两个或多个片段表面的重叠区域；适用于绘制片段间 δg 等值面。"
        )
        method_layout.addWidget(self.igmh_prescreen_check)
        self.rdg_interfragment_check = QCheckBox(
            "仅保留两个片段之间的 RDG 等值面（去除分子内干扰）"
        )
        self.rdg_interfragment_check.setChecked(False)
        self.rdg_interfragment_check.setToolTip(
            "按 Multiwfn 手册 4.13.4.2，仅保留两个片段缩放范德华区域的重叠部分。"
        )
        self.rdg_interfragment_check.toggled.connect(self._sync_method_options)
        method_layout.addWidget(self.rdg_interfragment_check)
        layout.addWidget(method_card)

        input_card, input_layout = self._card(
            "2 · 添加计算文件",
            "程序只读取原文件；计算产生的 Cube、日志和图片会写入新的任务目录。",
        )
        self.input_form = QFormLayout()
        self.input_form.setHorizontalSpacing(12)
        self.input_form.setVerticalSpacing(9)
        input_layout.addLayout(self.input_form)

        self.reference_files_panel = QWidget()
        reference_layout = QVBoxLayout(self.reference_files_panel)
        reference_layout.setContentsMargins(0, 4, 0, 0)
        reference_layout.setSpacing(8)
        reference_title = QLabel("减去的参考体系")
        reference_title.setObjectName("batchCardTitle")
        reference_layout.addWidget(reference_title)
        reference_hint = QLabel(
            "可一次添加一个或多个片段/参考态波函数。软件按“目标体系 − 参考体系 1 − 参考体系 2 …”计算。"
        )
        reference_hint.setObjectName("batchHint")
        reference_hint.setWordWrap(True)
        reference_layout.addWidget(reference_hint)
        self.reference_files_list = QListWidget()
        self.reference_files_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self.reference_files_list.setMinimumHeight(105)
        reference_layout.addWidget(self.reference_files_list)
        reference_buttons = QHBoxLayout()
        add_references = QPushButton("添加参考文件")
        add_references.clicked.connect(self._add_reference_files)
        remove_references = QPushButton("移除选中")
        remove_references.clicked.connect(self._remove_reference_files)
        reference_buttons.addWidget(add_references)
        reference_buttons.addWidget(remove_references)
        reference_buttons.addStretch(1)
        reference_layout.addLayout(reference_buttons)
        coordinate_hint = QLabel(
            "重要：所有参考体系必须保留目标体系中的原始坐标和取向，并建议使用相同理论水平与基组；"
            "不要让量化程序把片段重新转到标准取向。"
        )
        coordinate_hint.setObjectName("detailLabel")
        coordinate_hint.setWordWrap(True)
        reference_layout.addWidget(coordinate_hint)
        input_layout.addWidget(self.reference_files_panel)
        layout.addWidget(input_card)

        settings_card, settings_layout = self._card(
            "3 · 计算与绘图设置",
            "网格质量 2 是手册中的中等质量设置，适合作为日常默认值；高质量会显著增加时间和内存占用。",
        )
        settings_form = QFormLayout()
        self.grid_combo = QComboBox()
        self.grid_combo.addItem("快速预览", 1)
        self.grid_combo.addItem("中等质量（推荐）", 2)
        self.grid_combo.addItem("高质量", 3)
        self.grid_combo.setCurrentIndex(1)
        settings_form.addRow("空间网格", self.grid_combo)

        self.state_spin = QSpinBox()
        self.state_spin.setRange(1, 9999)
        self.state_spin.setValue(1)
        settings_form.addRow("激发态序号", self.state_spin)
        self.state_label = settings_form.labelForField(self.state_spin)

        self.nto_pairs_spin = QSpinBox()
        self.nto_pairs_spin.setRange(1, 10)
        self.nto_pairs_spin.setValue(1)
        settings_form.addRow("主导 NTO 对数", self.nto_pairs_spin)
        self.nto_pairs_label = settings_form.labelForField(self.nto_pairs_spin)

        self.fragments_edit = QLineEdit()
        self.fragments_edit.setPlaceholderText("例如：1-12;13-25")
        settings_form.addRow("IGMH 片段", self.fragments_edit)
        self.fragments_label = settings_form.labelForField(self.fragments_edit)

        self.rdg_overlap_scale = QDoubleSpinBox()
        self.rdg_overlap_scale.setDecimals(2)
        self.rdg_overlap_scale.setRange(0.1, 10.0)
        self.rdg_overlap_scale.setSingleStep(0.1)
        self.rdg_overlap_scale.setValue(1.8)
        self.rdg_overlap_scale.setToolTip(
            "范德华半径的缩放倍率；Multiwfn 手册示例使用 1.8，可按体系调整。"
        )
        settings_form.addRow("片段重叠范围", self.rdg_overlap_scale)
        self.rdg_overlap_scale_label = settings_form.labelForField(
            self.rdg_overlap_scale
        )

        self.iso_spin = QDoubleSpinBox()
        self.iso_spin.setDecimals(5)
        self.iso_spin.setRange(0.00001, 1.0)
        self.iso_spin.setSingleStep(0.0005)
        self.iso_spin.setValue(0.05)
        self.iso_spin.setSuffix(" a.u.")
        settings_form.addRow("正负等值面", self.iso_spin)
        self.iso_label = settings_form.labelForField(self.iso_spin)
        settings_layout.addLayout(settings_form)

        self.style_row_widget = QWidget()
        style_row = QHBoxLayout(self.style_row_widget)
        style_row.setContentsMargins(0, 0, 0, 0)
        self.style_field_label = QLabel("绘图方案")
        style_row.addWidget(self.style_field_label)
        self.style_label = QLabel("尚未选择")
        self.style_label.setObjectName("detailLabel")
        self.style_label.setWordWrap(True)
        style_row.addWidget(self.style_label, 1)
        self.choose_style_button = QPushButton("选择绘图方案")
        self.choose_style_button.clicked.connect(self._choose_style)
        style_row.addWidget(self.choose_style_button)
        settings_layout.addWidget(self.style_row_widget)

        self.weak_display_label = QLabel()
        self.weak_display_label.setObjectName("detailLabel")
        self.weak_display_label.setWordWrap(True)
        settings_layout.addWidget(self.weak_display_label)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.storage_dir / "automatic_runs"))
        browse_output = QPushButton("选择目录")
        browse_output.clicked.connect(self._browse_output)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse_output)
        settings_layout.addWidget(QLabel("结果保存位置"))
        settings_layout.addLayout(output_row)
        self.keep_cubes = QCheckBox("保留 Cube 文件")
        self.keep_cubes.setChecked(True)
        settings_layout.addWidget(self.keep_cubes)
        layout.addWidget(settings_card)

        run_card, run_layout = self._card(
            "4 · 运行与结果",
            "核心 PNG 和 NTO 波函数放在任务目录最外层；Cube 与日志分别归档。",
        )
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("等待运行")
        run_layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.start_button = QPushButton("开始全自动流程")
        self.start_button.setObjectName("primaryBtn")
        self.start_button.clicked.connect(self._start)
        self.cancel_button = QPushButton("停止")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.open_button = QPushButton("打开结果目录")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_result)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        run_layout.addLayout(buttons)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setMinimumHeight(145)
        run_layout.addWidget(self.log)
        layout.addWidget(run_card)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("batchPageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def configure(self, workflow_id: str) -> None:
        self.spec = science.workflow_spec(workflow_id)
        # This page instance is shared by all data-driven workflows. Reset all
        # result-only state when entering another workflow so a completed
        # task's log and result directory never appear under the next one.
        self.log.clear()
        self.last_run_dir = ""
        self.open_button.setEnabled(False)
        self.reference_files_list.clear()
        if self.spec.id == science.WORKFLOW_WEAK:
            self.weak_scatter_check.setChecked(False)
            self.igmh_prescreen_check.setChecked(True)
            self.rdg_interfragment_check.setChecked(False)
        self.toolbar_title.setText(self.spec.name)
        self.method_combo.blockSignals(True)
        self.method_combo.clear()
        for value, label in self.spec.methods:
            self.method_combo.addItem(label, value)
        self.method_combo.blockSignals(False)
        while self.input_form.rowCount():
            self.input_form.removeRow(0)
        self.role_rows.clear()
        for role, label, _extensions in self.spec.input_roles:
            editor = QLineEdit()
            editor.setPlaceholderText(f"选择{label}")
            button = QPushButton("浏览")
            button.clicked.connect(lambda _checked=False, key=role: self._browse_input(key))
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            row_layout.addWidget(editor, 1)
            row_layout.addWidget(button)
            label_widget = QLabel(label)
            self.input_form.addRow(label_widget, row)
            self.role_rows[role] = (label_widget, editor, button)
        if self.spec.id == science.WORKFLOW_WEAK:
            self.style_snapshot = {}
        elif str(self.style_snapshot.get("style", {}).get("surface_mode") or "") != self.spec.surface_mode:
            self.style_snapshot = self._default_style_snapshot()
        self.style_row_widget.setVisible(True)
        self.style_field_label.setText(
            "骨架样式" if self.spec.id == science.WORKFLOW_WEAK else "绘图方案"
        )
        self.choose_style_button.setText(
            "选择骨架" if self.spec.id == science.WORKFLOW_WEAK else "选择绘图方案"
        )
        self.weak_display_label.setVisible(self.spec.id == science.WORKFLOW_WEAK)
        self._sync_style_label()
        self._sync_method_options()
        self.progress.setValue(0)
        self.progress.setFormat("等待运行")
        self.ready_badge.setText("等待配置")

    def _default_style_snapshot(self) -> dict:
        if self.spec.id == science.WORKFLOW_WEAK:
            return {}
        styles = [
            copy.deepcopy(item)
            for item in core.get_all_bundle_styles()
            if str(item.get("surface_mode") or "signed") == self.spec.surface_mode
        ]
        if not styles:
            return {}
        style = styles[0]
        return {
            "style": style,
            "rep0_commands": list(style.get("rep0_commands") or []),
            "selection_text": f"套装风格：{style.get('name')}",
            "mode": "bundle",
            "bundle_id": str(style.get("id") or ""),
        }

    def _sync_method_options(self) -> None:
        method = str(self.method_combo.currentData() or "")
        is_weak = self.spec.id == science.WORKFLOW_WEAK
        self.weak_scatter_check.setVisible(is_weak)
        self._sync_weak_scatter_path()
        self.igmh_prescreen_check.setVisible(is_weak and method == "igmh")
        self.rdg_interfragment_check.setVisible(is_weak and method == "rdg")
        self.state_spin.setVisible(self.spec.id == science.WORKFLOW_EXCITED)
        self.state_label.setVisible(self.spec.id == science.WORKFLOW_EXCITED)
        self.nto_pairs_spin.setVisible(
            self.spec.id == science.WORKFLOW_EXCITED and method == "nto"
        )
        self.nto_pairs_label.setVisible(
            self.spec.id == science.WORKFLOW_EXCITED and method == "nto"
        )
        show_fragments = is_weak and (
            method == "igmh"
            or (method == "rdg" and self.rdg_interfragment_check.isChecked())
        )
        self.fragments_edit.setVisible(show_fragments)
        self.fragments_label.setVisible(show_fragments)
        if show_fragments:
            self.fragments_label.setText(
                "IGMH 片段" if method == "igmh" else "两个片段"
            )
        show_rdg_overlap = (
            is_weak
            and method == "rdg"
            and self.rdg_interfragment_check.isChecked()
        )
        self.rdg_overlap_scale.setVisible(show_rdg_overlap)
        self.rdg_overlap_scale_label.setVisible(show_rdg_overlap)
        show_references = (
            self.spec.id == science.WORKFLOW_DEFORMATION
            and method == "fragment_difference"
        )
        self.reference_files_panel.setVisible(show_references)
        if "wavefunction" in self.role_rows:
            self.role_rows["wavefunction"][0].setText(
                "目标体系波函数" if show_references else (
                    "分子波函数文件"
                    if self.spec.id == science.WORKFLOW_DEFORMATION
                    else self.spec.input_roles[0][1]
                )
            )
        show_iso = self.spec.id == science.WORKFLOW_DEFORMATION
        self.iso_spin.setVisible(show_iso)
        self.iso_label.setVisible(show_iso)
        if show_iso:
            self.iso_spin.setValue(0.0012 if show_references else 0.05)
        notes = {
            "iri": (
                "适合在一张图中同时观察整个体系的化学键和各种强弱相互作用；"
                "生成 IRI 与 sign(λ₂)ρ 网格，随后可在 VMD 中自由调整。"
            ),
            "rdg": (
                "经典 NCI/RDG 分析，对网格质量较敏感；生成 RDG 与 sign(λ₂)ρ 网格，"
                "随后可在 VMD 中自由调整。若只研究两个片段之间的作用，可按手册方法去除分子内等值面。"
            ),
            "igmh": (
                "适合划分片段后专门分析片段间相互作用。"
                "请用分号分隔不同片段，例如：1-12;13-25。"
            ),
            "hole_electron": "输出空穴、电子和电荷密度差；激发态信息来自对应 Gaussian/ORCA 输出。",
            "nto": "先导出 NTO 波函数，再自动选择具有最大 NTO 本征值的空穴/电子对生成 Cube。",
            "spin": "适用于包含有效开壳层信息的波函数；生成数据后可在 VMD 中自由调整。",
            "deformation": (
                "按 Multiwfn 手册以同一几何下的分子密度减去球对称自由原子密度；"
                "程序会使用 Multiwfn 随附的 atomwfn 数据，不会额外调用 Gaussian。"
            ),
            "fragment_difference": (
                "按 Multiwfn 手册 3.7.1 与教程 4.5.5，在目标体系网格上依次减去所有参考体系电子密度。"
                "适用于复合物 − 各孤立片段，也可用于同一几何下两个状态的密度差。"
            ),
        }
        self.method_note.setText(notes.get(method, self.spec.description))
        if self.spec.id == science.WORKFLOW_WEAK:
            profile = science.weak_interaction_display_profile(method)
            self.weak_display_label.setText(
                "默认显示参数\n"
                f"{profile['surface_field']} 等值面 = {profile['iso_value']:g}；"
                f"以 {profile['color_field']} 着色；BGR 范围 "
                f"{profile['color_min']:g} ～ {profile['color_max']:g}。"
                "VMD 打开后可自由修改。"
            )

    def _sync_weak_scatter_path(self) -> None:
        self.gnuplot_row_widget.setVisible(
            self.spec.id == science.WORKFLOW_WEAK
            and self.weak_scatter_check.isChecked()
        )

    def _browse_gnuplot(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Gnuplot 程序",
            self.gnuplot_edit.text().strip(),
            "Gnuplot (gnuplot.exe);;可执行程序 (*.exe);;所有文件 (*)",
        )
        if chosen:
            self.gnuplot_edit.setText(chosen)

    def _browse_input(self, role: str) -> None:
        role_info = next((item for item in self.spec.input_roles if item[0] == role), None)
        if role_info is None:
            return
        _role, label, extensions = role_info
        patterns = " ".join(f"*{ext}" for ext in extensions)
        chosen, _ = QFileDialog.getOpenFileName(self, f"选择{label}", "", f"支持的文件 ({patterns});;所有文件 (*)")
        if chosen:
            self.role_rows[role][1].setText(chosen)

    def _add_reference_files(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in science.WAVEFUNCTION_EXTENSIONS)
        chosen, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要减去的参考体系波函数",
            "",
            f"支持的文件 ({patterns});;所有文件 (*)",
        )
        existing = {
            str(self.reference_files_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.reference_files_list.count())
        }
        for raw_path in chosen:
            path = str(Path(raw_path).expanduser().resolve())
            if path in existing:
                continue
            item = QListWidgetItem(f"−  {Path(path).name}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.reference_files_list.addItem(item)
            existing.add(path)

    def _remove_reference_files(self) -> None:
        for item in list(self.reference_files_list.selectedItems()):
            self.reference_files_list.takeItem(self.reference_files_list.row(item))

    def _reference_file_paths(self) -> list[str]:
        return [
            str(self.reference_files_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.reference_files_list.count())
        ]

    def _browse_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择结果保存目录", self.output_edit.text().strip())
        if chosen:
            self.output_edit.setText(chosen)

    def _choose_style(self) -> None:
        dialog = self.style_dialog_factory(
            self.style_snapshot,
            self,
            surface_mode=self.spec.surface_mode,
            skeleton_only=self.spec.id == science.WORKFLOW_WEAK,
        )
        if dialog.exec():
            self.style_snapshot = dialog.selection()
            self._sync_style_label()

    def _sync_style_label(self) -> None:
        if self.spec.id == science.WORKFLOW_WEAK and not self.style_snapshot:
            self.style_label.setText("Multiwfn 推荐骨架（随分析方法）")
            return
        self.style_label.setText(
            str(self.style_snapshot.get("selection_text") or "尚未选择兼容的绘图方案")
        )

    def _start(self) -> None:
        if self.is_running():
            return
        if (
            self.spec.id == science.WORKFLOW_WEAK
            and self.weak_scatter_check.isChecked()
            and not Path(self.gnuplot_edit.text().strip()).expanduser().is_file()
        ):
            QMessageBox.warning(
                self,
                "需要 Gnuplot",
                "请先选择 Gnuplot 安装目录 bin 文件夹中的 gnuplot.exe。",
            )
            return
        if (
            self.spec.id == science.WORKFLOW_WEAK
            and str(self.method_combo.currentData() or "") == "rdg"
            and self.rdg_interfragment_check.isChecked()
            and len(
                [
                    part
                    for part in self.fragments_edit.text().split(";")
                    if part.strip()
                ]
            )
            != 2
        ):
            QMessageBox.warning(
                self,
                "需要两个片段",
                "请用分号填写恰好两个片段，例如：1-12;13-25。",
            )
            return
        inputs = {role: editor.text().strip() for role, (_label, editor, _button) in self.role_rows.items()}
        output = self.output_edit.text().strip()
        options = {
            "grid_quality": int(self.grid_combo.currentData() or 2),
            "excited_state": self.state_spin.value(),
            "nto_pairs": self.nto_pairs_spin.value(),
            "fragments": self.fragments_edit.text().strip(),
            "draw_scatter": self.weak_scatter_check.isChecked(),
            "gnuplot_path": self.gnuplot_edit.text().strip(),
            "igmh_prescreen": self.igmh_prescreen_check.isChecked(),
            "rdg_interfragment_only": self.rdg_interfragment_check.isChecked(),
            "rdg_overlap_scale": self.rdg_overlap_scale.value(),
            "reference_files": self._reference_file_paths(),
            "iso_value": self.iso_spin.value(),
            "keep_cubes": self.keep_cubes.isChecked(),
            "style_snapshot": copy.deepcopy(self.style_snapshot),
            "width": 1400,
            "height": 1050,
        }
        self.log.clear()
        self.log.appendPlainText(f"开始：{self.spec.name} / {self.method_combo.currentText()}")
        self.progress.setValue(1)
        self.progress.setFormat("正在启动")
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.open_button.setEnabled(False)
        self.ready_badge.setText("运行中")
        self.thread = QThread(self)
        self.worker = _ScientificWorker(
            self.spec.id,
            str(self.method_combo.currentData() or ""),
            inputs,
            options,
            Path(output),
            Path(self.multiwfn_path_getter()),
            Path(self.vmd_path_getter()),
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.event.connect(self._on_event)
        self.worker.finished.connect(self._on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._cleanup_thread)
        self.settingsChanged.emit(
            {
                "scientific_workflow_settings": {
                    "output_dir": output,
                    "grid_quality": options["grid_quality"],
                    "keep_cubes": options["keep_cubes"],
                    "gnuplot_path": options["gnuplot_path"],
                }
            }
        )
        self.thread.start()

    @Slot(object)
    def _on_event(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        progress = int(round(float(event.get("progress") or 0)))
        message = str(event.get("message") or "处理中")
        self.progress.setValue(progress)
        self.progress.setFormat(f"{progress}% · {message}")
        if not self.log.toPlainText().endswith(message):
            self.log.appendPlainText(message)

    @Slot(object, object)
    def _on_finished(self, result: object, error: object) -> None:
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if error is not None:
            self.progress.setValue(100)
            self.progress.setFormat("运行失败")
            self.ready_badge.setText("失败")
            self.log.appendPlainText(f"失败：{error}")
            return
        payload = result if isinstance(result, dict) else {}
        self.last_run_dir = str(payload.get("run_dir") or "")
        self.progress.setValue(100)
        self.progress.setFormat("100% · 已完成")
        self.ready_badge.setText("已完成")
        self.log.appendPlainText(f"结果：{self.last_run_dir}")
        self.open_button.setEnabled(bool(self.last_run_dir))

    def _open_result(self) -> None:
        path = Path(self.last_run_dir)
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.isRunning()

    def cancel(self) -> None:
        if self.worker is not None:
            self.cancel_button.setEnabled(False)
            self.progress.setFormat("正在停止")
            self.worker.cancel()

    def _cleanup_thread(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None

    def cleanup(self) -> None:
        self.cancel()

    def load_settings(self, config: dict) -> None:
        saved = config.get("scientific_workflow_settings")
        if not isinstance(saved, dict):
            return
        self.output_edit.setText(str(saved.get("output_dir") or self.output_edit.text()))
        index = self.grid_combo.findData(int(saved.get("grid_quality") or 2))
        self.grid_combo.setCurrentIndex(max(0, index))
        self.keep_cubes.setChecked(bool(saved.get("keep_cubes", True)))
        self.gnuplot_edit.setText(str(saved.get("gnuplot_path") or ""))
