from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Callable

import multiwfn_batch as batch
from multiwfn_recorder_qt6 import MultiwfnRecorderDialog
from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


STATUS_TEXT = {
    batch.STATUS_PENDING: "等待",
    batch.STATUS_RUNNING: "运行中",
    batch.STATUS_SUCCESS: "成功",
    batch.STATUS_FAILED: "失败",
    batch.STATUS_CANCELLED: "已取消",
    batch.STATUS_TIMEOUT: "超时",
}

COMMON_OUTPUT_GROUPS = (
    {
        "id": "cube",
        "title": "Cube 格点文件",
        "detail": "电子密度、ESP、ELF、轨道等 *.cub",
        "patterns": ("*.cub",),
    },
    {
        "id": "text",
        "title": "文本结果",
        "detail": "键级矩阵、分析报告等 *.txt",
        "patterns": ("*.txt",),
    },
    {
        "id": "data",
        "title": "数据表",
        "detail": "数值数据 *.dat / *.csv",
        "patterns": ("*.dat", "*.csv"),
    },
    {
        "id": "structure",
        "title": "结构文件",
        "detail": "XYZ、PDB、MOL、MOL2、CIF",
        "patterns": ("*.xyz", "*.pdb", "*.mol", "*.mol2", "*.cif"),
    },
    {
        "id": "wavefunction",
        "title": "波函数 / 电荷文件",
        "detail": "WFN、WFX、MWFN、FCH、CHG、Molden",
        "patterns": (
            "*.wfn",
            "*.wfx",
            "*.mwfn",
            "*.fch",
            "*.fchk",
            "*.chg",
            "*.47",
            "*.molden",
            "*.molden.input",
        ),
    },
    {
        "id": "image",
        "title": "图片",
        "detail": "PNG、BMP、TGA、JPG",
        "patterns": ("*.png", "*.bmp", "*.tga", "*.jpg", "*.jpeg"),
    },
)


class SequenceEditor(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        button_row = QHBoxLayout()
        self.blank_button = QPushButton("在光标处加入空行")
        self.blank_button.clicked.connect(self._insert_blank_line)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self._clear)
        button_row.addWidget(self.blank_button)
        button_row.addWidget(clear_button)
        button_row.addStretch(1)
        self.stats_label = QLabel("0 次输入")
        self.stats_label.setObjectName("countPill")
        button_row.addWidget(self.stats_label)
        layout.addLayout(button_row)

        self.raw_edit = QPlainTextEdit()
        self.raw_edit.setObjectName("batchInput")
        self.raw_edit.setPlaceholderText(
            "每行代表向 Multiwfn 发送一次输入；空行代表按一次回车。\n"
            "可直接按 Ctrl+V 粘贴，也可从上方导入 TXT 或自动录制。"
        )
        self.raw_edit.textChanged.connect(self._update_stats)
        layout.addWidget(self.raw_edit, 1)

        note = QLabel("文件名和路径会按每个输入文件自动替换。")
        note.setObjectName("batchHint")
        note.setWordWrap(True)
        layout.addWidget(note)

    def _insert_blank_line(self) -> None:
        cursor = self.raw_edit.textCursor()
        cursor.insertText("\n")
        self.raw_edit.setTextCursor(cursor)
        self.raw_edit.setFocus()

    def _clear(self) -> None:
        if not self.raw_edit.toPlainText():
            return
        answer = QMessageBox.question(
            self,
            "清空操作流程",
            "确定清空当前全部 Multiwfn 输入吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.raw_edit.clear()

    def _update_stats(self) -> None:
        text = self.raw_edit.toPlainText()
        if not text:
            self.stats_label.setText("0 次输入")
            return
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        blank_count = sum(not line for line in lines)
        suffix = f" · {blank_count} 个空行" if blank_count else ""
        self.stats_label.setText(f"{len(lines)} 次输入{suffix}")

    def set_text(self, text: str) -> None:
        self.raw_edit.setPlainText(str(text or ""))

    def text(self) -> str:
        return self.raw_edit.toPlainText()


class BatchFileTable(QTableWidget):
    pathsDropped = Signal(object)

    def __init__(self, rows: int, columns: int) -> None:
        super().__init__(rows, columns)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    @staticmethod
    def _local_paths(event) -> list[Path]:
        if not event.mimeData().hasUrls():
            return []
        return [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._local_paths(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._local_paths(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        paths = self._local_paths(event)
        if not paths:
            super().dropEvent(event)
            return
        self.pathsDropped.emit(paths)
        event.acceptProposedAction()


class BatchExecutionWorker(QObject):
    event = Signal(object)
    finished = Signal(object, object)

    def __init__(
        self,
        files: list[Path],
        preset: batch.BatchPreset,
        variables: dict[str, str],
        output_root: Path,
        multiwfn_exe: Path,
        prefix: str,
    ) -> None:
        super().__init__()
        self.files = files
        self.preset = preset
        self.variables = variables
        self.output_root = output_root
        self.multiwfn_exe = multiwfn_exe
        self.prefix = prefix
        self.runner: batch.MultiwfnBatchRunner | None = None
        self.cancel_requested = False

    @Slot()
    def run(self) -> None:
        try:
            plan = batch.create_batch_plan(
                self.files,
                self.preset,
                self.output_root,
                self.variables,
                prefix=self.prefix,
            )
            self.runner = batch.MultiwfnBatchRunner(
                plan, self.multiwfn_exe, event_callback=self.event.emit
            )
            if self.cancel_requested:
                self.runner.cancel()
            result = self.runner.run()
        except Exception as exc:
            self.finished.emit(None, str(exc))
            return
        self.finished.emit(result, None)

    def cancel(self) -> None:
        self.cancel_requested = True
        if self.runner is not None:
            self.runner.cancel()


class MultiwfnBatchPage(QWidget):
    settingsChanged = Signal(object)
    DRAFT_PRESET_ID = "__unsaved_draft__"

    def __init__(
        self,
        storage_dir: Path,
        multiwfn_path_getter: Callable[[], str],
    ) -> None:
        super().__init__()
        self.storage_dir = Path(storage_dir)
        self.presets_file = self.storage_dir / "multiwfn_batch_presets.json"
        self.multiwfn_path_getter = multiwfn_path_getter
        self.files: list[Path] = []
        self.file_enabled: dict[str, bool] = {}
        self.presets: list[batch.BatchPreset] = []
        self.thread: QThread | None = None
        self.worker: BatchExecutionWorker | None = None
        self.last_run_dir = ""
        self._syncing_common_outputs = False
        self._loading_editor = False
        self._editor_dirty = False
        self._editor_preset_id = ""
        self._draft_kind = ""
        self._draft_return_preset_id = ""
        self._loaded_summary_text = ""
        self._active_run_mode = ""
        self._tab_animation: QPropertyAnimation | None = None
        self._progress_animation: QPropertyAnimation | None = None
        self._build_ui()
        self._connect_editor_change_tracking()
        self._reload_presets()

    def _pane(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("batchCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 15, 16, 16)
        layout.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("batchCardTitle")
        layout.addWidget(label)
        self._apply_shadow(frame)
        return frame, layout

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("batchHint")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _apply_shadow(
        widget: QWidget, *, blur: int = 20, offset_y: int = 3, alpha: int = 20
    ) -> None:
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(blur)
        shadow.setOffset(0, offset_y)
        shadow.setColor(QColor(20, 35, 55, alpha))
        widget.setGraphicsEffect(shadow)

    @staticmethod
    def _scrollable_page(content: QWidget, minimum_height: int) -> QScrollArea:
        content.setMinimumHeight(minimum_height)
        scroll = QScrollArea()
        scroll.setObjectName("batchPageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(content)
        return scroll

    def _animate_current_tab(self, index: int) -> None:
        if not hasattr(self, "workspace_tabs"):
            return
        page = self.workspace_tabs.widget(index)
        if page is None:
            return
        if self._tab_animation is not None:
            self._tab_animation.stop()
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(230)
        animation.setStartValue(0.25)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(lambda p=page: p.setGraphicsEffect(None))
        self._tab_animation = animation
        animation.start()

    def _set_progress_animated(self, value: int) -> None:
        if self._progress_animation is not None:
            self._progress_animation.stop()
        animation = QPropertyAnimation(self.progress, b"value", self)
        animation.setDuration(280)
        animation.setStartValue(self.progress.value())
        animation.setEndValue(int(value))
        animation.setEasingCurve(QEasingCurve.OutCubic)
        self._progress_animation = animation
        animation.start()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        hero = QFrame()
        hero.setObjectName("batchHero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(20, 14, 20, 14)
        hero_layout.setSpacing(14)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(3)
        hero_title = QLabel("批量 Multiwfn")
        hero_title.setObjectName("batchHeroTitle")
        hero_subtitle = QLabel("记录或导入一次操作流程，再应用到整批计算文件")
        hero_subtitle.setObjectName("batchHeroSubtitle")
        hero_subtitle.setWordWrap(True)
        hero_text.addWidget(hero_title)
        hero_text.addWidget(hero_subtitle)
        hero_layout.addLayout(hero_text, 1)
        self.hero_badge = QLabel("版本 2026.7.11")
        self.hero_badge.setObjectName("batchBadge")
        hero_layout.addWidget(self.hero_badge, 0, Qt.AlignVCenter)
        self._apply_shadow(hero, blur=24, offset_y=4, alpha=24)
        root.addWidget(hero)

        preset_toolbar = QFrame()
        preset_toolbar.setObjectName("batchToolbar")
        preset_toolbar_layout = QHBoxLayout(preset_toolbar)
        preset_toolbar_layout.setContentsMargins(14, 9, 14, 9)
        preset_toolbar_layout.setSpacing(8)
        preset_label = QLabel("流程名称")
        preset_label.setObjectName("batchToolbarLabel")
        preset_toolbar_layout.addWidget(preset_label)
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(150)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selection_changed)
        preset_toolbar_layout.addWidget(self.preset_combo, 1)

        self.new_preset_button = QPushButton("+")
        self.new_preset_button.setObjectName("batchAddFlowButton")
        self.new_preset_button.setFixedWidth(42)
        self.new_preset_button.setToolTip("新建空白流程")
        self.new_preset_button.setAccessibleName("新建空白流程")
        self.new_preset_button.clicked.connect(self._new_blank_preset)
        preset_toolbar_layout.addWidget(self.new_preset_button)

        self.copy_preset_button = QPushButton("复制")
        self.copy_preset_button.setObjectName("batchFlowActionButton")
        self.copy_preset_button.setToolTip("复制当前流程并另存为新流程")
        self.copy_preset_button.clicked.connect(self._new_preset)
        preset_toolbar_layout.addWidget(self.copy_preset_button)

        self.import_preset_button = QPushButton("导入")
        self.import_preset_button.setObjectName("batchFlowActionButton")
        self.import_preset_button.setToolTip("从 JSON 文件导入流程")
        self.import_preset_button.clicked.connect(self._import_presets)
        preset_toolbar_layout.addWidget(self.import_preset_button)

        self.export_preset_button = QPushButton("导出流程")
        self.export_preset_button.setObjectName("batchFlowActionButton")
        self.export_preset_button.setToolTip(
            "将当前编辑内容导出为 JSON 文件，包括尚未保存的修改"
        )
        self.export_preset_button.clicked.connect(self._export_current_preset)
        preset_toolbar_layout.addWidget(self.export_preset_button)

        preset_toolbar_layout.addStretch(1)
        self.preset_summary_label = QLabel("选择一个批量流程")
        self.preset_summary_label.setObjectName("batchPresetInline")
        self.preset_summary_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.preset_summary_label.setMinimumWidth(96)
        self.preset_summary_label.setMaximumWidth(120)
        preset_toolbar_layout.addWidget(self.preset_summary_label)
        root.addWidget(preset_toolbar)

        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.setObjectName("batchWorkspaceTabs")
        self.workspace_tabs.setDocumentMode(True)
        self.workspace_tabs.currentChanged.connect(self._animate_current_tab)
        root.addWidget(self.workspace_tabs, 1)

        task_page = QWidget()
        task_page_layout = QVBoxLayout(task_page)
        task_page_layout.setContentsMargins(10, 14, 10, 10)
        task_page_layout.setSpacing(10)
        task_splitter = QSplitter(Qt.Horizontal)
        task_splitter.setChildrenCollapsible(False)
        task_splitter.setHandleWidth(10)
        self.task_splitter = task_splitter
        task_page_layout.addWidget(task_splitter, 1)

        input_frame, input_layout = self._pane("待处理文件")
        input_layout.addWidget(
            self._hint(
                "添加单个文件、扫描文件夹，或将文件和文件夹直接拖入下方列表。"
            )
        )
        file_buttons = QHBoxLayout()
        file_buttons.setSpacing(8)
        add_files = QPushButton("添加文件")
        add_folder = QPushButton("扫描文件夹")
        remove_files = QPushButton("移除选中")
        clear_files = QPushButton("清空")
        add_files.clicked.connect(self._add_files)
        add_folder.clicked.connect(self._add_folder)
        remove_files.clicked.connect(self._remove_selected_files)
        clear_files.clicked.connect(self._clear_files)
        file_buttons.addWidget(add_files)
        file_buttons.addWidget(add_folder)
        file_buttons.addWidget(remove_files)
        file_buttons.addWidget(clear_files)
        input_layout.addLayout(file_buttons)

        scan_row = QHBoxLayout()
        self.recursive_check = QCheckBox("递归扫描子文件夹")
        self.recursive_check.setChecked(True)
        self.file_count_label = QLabel("0 个文件")
        self.file_count_label.setObjectName("countPill")
        scan_row.addWidget(self.recursive_check)
        scan_row.addStretch(1)
        scan_row.addWidget(self.file_count_label)
        input_layout.addLayout(scan_row)

        self.file_table = BatchFileTable(0, 3)
        self.file_table.pathsDropped.connect(self._handle_dropped_paths)
        self.file_table.itemChanged.connect(self._on_file_item_changed)
        self.file_table.setHorizontalHeaderLabels(["启用", "文件", "格式"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.file_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.verticalHeader().setDefaultSectionSize(36)
        self.file_table.setMinimumHeight(300)
        input_layout.addWidget(self.file_table, 1)

        output_row = QHBoxLayout()
        output_label = QLabel("批处理结果目录")
        output_label.setObjectName("formLabel")
        output_row.addWidget(output_label)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("批处理运行记录和结果保存位置")
        output_row.addWidget(self.output_dir_edit, 1)
        pick_output = QPushButton("选择")
        pick_output.clicked.connect(self._pick_output_dir)
        output_row.addWidget(pick_output)
        input_layout.addLayout(output_row)
        task_splitter.addWidget(input_frame)

        run_frame, run_layout = self._pane("开始运行")
        run_layout.addWidget(self._hint("建议先用第一个文件试运行，确认菜单路径与结果文件正确后再处理全部文件。"))
        self.task_preset_summary = QLabel("尚未选择批量流程")
        self.task_preset_summary.setObjectName("batchPresetSummary")
        self.task_preset_summary.setWordWrap(True)
        run_layout.addWidget(self.task_preset_summary)
        run_layout.addStretch(1)
        preview_button = QPushButton("预检并预览首个任务")
        preview_button.setMinimumHeight(40)
        preview_button.clicked.connect(self._preview_run)
        self.trial_button = QPushButton("首文件试运行")
        self.trial_button.setMinimumHeight(40)
        self.trial_button.clicked.connect(lambda: self._start_run(True))
        self.start_button = QPushButton("开始批处理")
        self.start_button.setObjectName("generateBtn")
        self.start_button.setMinimumHeight(44)
        self.start_button.clicked.connect(lambda: self._start_run(False))
        run_layout.addWidget(preview_button)
        run_layout.addWidget(self.trial_button)
        run_layout.addWidget(self.start_button)
        task_splitter.addWidget(run_frame)
        task_splitter.setSizes([760, 360])
        self.task_scroll = self._scrollable_page(task_page, 560)
        self.workspace_tabs.addTab(self.task_scroll, "① 选择文件")

        template_page = QWidget()
        template_page_layout = QVBoxLayout(template_page)
        template_page_layout.setContentsMargins(10, 14, 10, 12)
        template_page_layout.setSpacing(10)

        source_frame, source_layout = self._pane("1 · 获取一次完整的操作流程")
        source_layout.addWidget(
            self._hint(
                "可从剪贴板、TXT 文件或一次真实操作中获取流程。每行代表一次输入，空行代表按 Enter。"
            )
        )
        source_buttons = QHBoxLayout()
        source_buttons.setSpacing(9)
        self.paste_commands_button = QPushButton("从剪贴板载入")
        self.paste_commands_button.setMinimumHeight(42)
        self.paste_commands_button.clicked.connect(self._paste_command_sequence)
        self.import_commands_button = QPushButton("导入命令 TXT")
        self.import_commands_button.setMinimumHeight(42)
        self.import_commands_button.clicked.connect(self._import_command_text)
        self.record_commands_button = QPushButton("操作一次并自动记录")
        self.record_commands_button.setObjectName("primaryBtn")
        self.record_commands_button.setMinimumHeight(42)
        self.record_commands_button.clicked.connect(self._record_command_sequence)
        source_buttons.addWidget(self.paste_commands_button)
        source_buttons.addWidget(self.import_commands_button)
        source_buttons.addWidget(self.record_commands_button)
        source_buttons.addStretch(1)
        source_layout.addLayout(source_buttons)
        template_page_layout.addWidget(source_frame)

        sequence_frame, sequence_layout = self._pane("2 · 核对 Multiwfn 命令序列")
        sequence_layout.addWidget(
            self._hint(
                "可在文本中直接修改或 Ctrl+V 粘贴。请保留必要的空行，并让序列最终正常返回菜单或退出程序。"
            )
        )
        self.sequence_editor = SequenceEditor()
        self.sequence_editor.setMinimumHeight(300)
        sequence_layout.addWidget(self.sequence_editor, 1)
        template_page_layout.addWidget(sequence_frame)

        output_frame, output_layout = self._pane("3 · 指定批量完成后要保留的结果")
        output_layout.addWidget(
            self._hint(
                "勾选需要汇总的文件类型即可。软件会保留原文件名并自动加上输入文件名前缀，"
                "避免批量任务之间互相覆盖。"
            )
        )

        common_grid = QGridLayout()
        common_grid.setHorizontalSpacing(9)
        common_grid.setVerticalSpacing(9)
        self.common_output_checks: dict[str, QCheckBox] = {}

        log_card = QFrame()
        log_card.setObjectName("commonOutputCard")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(12, 9, 12, 9)
        log_layout.setSpacing(2)
        log_check = QCheckBox("完整运行记录")
        log_check.setObjectName("commonOutputCheck")
        log_check.setChecked(True)
        log_check.setEnabled(False)
        log_detail = QLabel("每个文件自动保存一份 Multiwfn 控制台日志")
        log_detail.setObjectName("batchHint")
        log_detail.setWordWrap(True)
        log_layout.addWidget(log_check)
        log_layout.addWidget(log_detail)
        common_grid.addWidget(log_card, 0, 0)

        for index, group in enumerate(COMMON_OUTPUT_GROUPS, 1):
            card = QFrame()
            card.setObjectName("commonOutputCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 9, 12, 9)
            card_layout.setSpacing(2)
            check = QCheckBox(str(group["title"]))
            check.setObjectName("commonOutputCheck")
            check.toggled.connect(
                lambda checked, group_id=str(group["id"]): self._on_common_output_toggled(
                    group_id, checked
                )
            )
            detail = QLabel(str(group["detail"]))
            detail.setObjectName("batchHint")
            detail.setWordWrap(True)
            card_layout.addWidget(check)
            card_layout.addWidget(detail)
            common_grid.addWidget(card, index // 2, index % 2)
            self.common_output_checks[str(group["id"])] = check
        common_grid.setColumnStretch(0, 1)
        common_grid.setColumnStretch(1, 1)
        output_layout.addLayout(common_grid)

        self.manual_output_toggle = QCheckBox("补充手动匹配规则（高级）")
        self.manual_output_toggle.toggled.connect(
            lambda visible: self.manual_output_container.setVisible(bool(visible))
        )
        output_layout.addWidget(self.manual_output_toggle)
        self.manual_output_container = QFrame()
        self.manual_output_container.setObjectName("batchAdvancedPanel")
        manual_layout = QVBoxLayout(self.manual_output_container)
        manual_layout.setContentsMargins(10, 9, 10, 10)
        manual_layout.setSpacing(7)
        manual_layout.addWidget(
            self._hint(
                "仅在常用类型无法覆盖时使用。可填写确切文件名或通配符；“必须生成”用于严格检查。"
            )
        )
        self.output_rules_table = QTableWidget(0, 3)
        self.output_rules_table.setHorizontalHeaderLabels(
            ["文件名 / 匹配模式", "自定义保存名（可留空）", "必须生成"]
        )
        self.output_rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.output_rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.output_rules_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.output_rules_table.verticalHeader().setVisible(False)
        self.output_rules_table.verticalHeader().setDefaultSectionSize(36)
        self.output_rules_table.setMinimumHeight(150)
        self.output_rules_table.setAlternatingRowColors(True)
        self.output_rules_table.itemChanged.connect(
            lambda _item: self._sync_common_output_checks_from_table()
        )
        manual_layout.addWidget(self.output_rules_table)
        output_buttons = QHBoxLayout()
        add_output = QPushButton("添加手动规则")
        add_output.clicked.connect(lambda: self._add_output_rule_row(begin_edit=True))
        remove_output = QPushButton("删除选中")
        remove_output.clicked.connect(self._remove_output_rule_rows)
        output_buttons.addWidget(add_output)
        output_buttons.addWidget(remove_output)
        output_buttons.addStretch(1)
        manual_layout.addLayout(output_buttons)
        self.manual_output_container.setVisible(False)
        output_layout.addWidget(self.manual_output_container)
        template_page_layout.addWidget(output_frame)

        preset_frame, preset_layout = self._pane("4 · 流程信息与保存（按需）")
        basic_grid = QGridLayout()
        basic_grid.setHorizontalSpacing(10)
        basic_grid.setVerticalSpacing(9)
        basic_grid.addWidget(QLabel("流程名称"), 0, 0)
        self.preset_name_edit = QLineEdit()
        self.preset_name_edit.setPlaceholderText("例如：导出 XYZ、生成 ELF Cube、表面静电势")
        basic_grid.addWidget(self.preset_name_edit, 0, 1)
        basic_grid.addWidget(QLabel("支持格式"), 0, 2)
        self.extensions_edit = QLineEdit()
        self.extensions_edit.setPlaceholderText(".fch, .fchk, .wfn, .wfx")
        basic_grid.addWidget(self.extensions_edit, 0, 3)
        basic_grid.addWidget(QLabel("用途说明"), 1, 0)
        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("这条流程完成什么操作、会得到哪些结果")
        basic_grid.addWidget(self.description_edit, 1, 1, 1, 3)
        basic_grid.setColumnStretch(1, 2)
        basic_grid.setColumnStretch(3, 2)
        preset_layout.addLayout(basic_grid)

        self.advanced_toggle = QCheckBox("显示高级设置（通常无需修改）")
        self.advanced_toggle.toggled.connect(self._toggle_advanced_settings)
        preset_layout.addWidget(self.advanced_toggle)
        self.advanced_container = QFrame()
        self.advanced_container.setObjectName("batchAdvancedPanel")
        advanced_grid = QGridLayout(self.advanced_container)
        advanced_grid.setContentsMargins(12, 10, 12, 12)
        advanced_grid.setHorizontalSpacing(10)
        advanced_grid.setVerticalSpacing(8)
        advanced_grid.addWidget(QLabel("适配版本"), 0, 0)
        self.version_edit = QLineEdit()
        self.version_edit.setPlaceholderText(batch.CURRENT_MULTIWFN_VERSION)
        advanced_grid.addWidget(self.version_edit, 0, 1)
        advanced_grid.addWidget(QLabel("单文件超时（秒）"), 0, 2)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 86400)
        self.timeout_spin.setValue(600)
        advanced_grid.addWidget(self.timeout_spin, 0, 3)
        advanced_grid.addWidget(QLabel("命令行参数"), 1, 0)
        self.arguments_edit = QPlainTextEdit()
        self.arguments_edit.setPlaceholderText("每行一个参数；现有流程的设置通常无需改动")
        self.arguments_edit.setMaximumHeight(92)
        advanced_grid.addWidget(self.arguments_edit, 1, 1, 1, 3)
        advanced_grid.addWidget(QLabel("可复用参数"), 2, 0, Qt.AlignTop)
        variables_box = QWidget()
        variables_layout = QVBoxLayout(variables_box)
        variables_layout.setContentsMargins(0, 0, 0, 0)
        variables_layout.setSpacing(6)
        self.variables_table = QTableWidget(0, 2)
        self.variables_table.setHorizontalHeaderLabels(["变量", "默认值"])
        self.variables_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.variables_table.verticalHeader().setVisible(False)
        self.variables_table.setMaximumHeight(150)
        variables_layout.addWidget(self.variables_table)
        variable_buttons = QHBoxLayout()
        self.add_variable_button = QPushButton("添加参数")
        remove_variable = QPushButton("删除参数")
        self.add_variable_button.clicked.connect(
            lambda: self._add_variable_row(begin_edit=True)
        )
        remove_variable.clicked.connect(self._remove_variable_rows)
        variable_buttons.addWidget(self.add_variable_button)
        variable_buttons.addWidget(remove_variable)
        variable_buttons.addStretch(1)
        variables_layout.addLayout(variable_buttons)
        advanced_grid.addWidget(variables_box, 2, 1, 1, 3)
        self.advanced_container.setVisible(False)
        preset_layout.addWidget(self.advanced_container)

        preset_actions = QHBoxLayout()
        preset_actions.addStretch(1)
        self.save_as_button = QPushButton("保存为新流程")
        self.save_as_button.setObjectName("primaryBtn")
        self.save_as_button.clicked.connect(self._save_as_preset)
        self.update_button = QPushButton("保存当前修改")
        self.update_button.clicked.connect(self._update_current_preset)
        self.delete_button = QPushButton("删除当前流程")
        self.delete_button.setObjectName("dangerBtn")
        self.delete_button.clicked.connect(self._delete_current_preset)
        reset_button = QPushButton("放弃未保存修改")
        reset_button.clicked.connect(self._discard_editor_changes)
        preset_actions.addWidget(self.save_as_button)
        preset_actions.addWidget(self.update_button)
        preset_actions.addWidget(self.delete_button)
        preset_actions.addWidget(reset_button)
        preset_layout.addLayout(preset_actions)
        template_page_layout.addWidget(preset_frame)

        self.template_scroll = self._scrollable_page(template_page, 1110)
        self.workspace_tabs.addTab(self.template_scroll, "② 操作流程")

        result_page = QWidget()
        result_page_layout = QVBoxLayout(result_page)
        result_page_layout.setContentsMargins(10, 14, 10, 10)
        result_page_layout.setSpacing(10)
        queue_frame, queue_layout = self._pane("运行队列与结果")
        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self.run_state_badge = QLabel("待运行")
        self.run_state_badge.setObjectName("batchRunBadge")
        run_row.addWidget(self.run_state_badge)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("dangerBtn")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.cancel)
        self.open_results_button = QPushButton("打开结果目录")
        self.open_results_button.setEnabled(False)
        self.open_results_button.clicked.connect(self._open_results)
        run_row.addWidget(self.stop_button)
        run_row.addWidget(self.open_results_button)
        self.continue_batch_button = QPushButton("试运行通过，开始全部文件")
        self.continue_batch_button.setObjectName("generateBtn")
        self.continue_batch_button.setVisible(False)
        self.continue_batch_button.clicked.connect(self._continue_full_batch)
        run_row.addWidget(self.continue_batch_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        run_row.addWidget(self.progress, 1)
        queue_layout.addLayout(run_row)

        self.result_stack = QStackedWidget()
        self.result_stack.setObjectName("batchResultStack")

        empty_page = QFrame()
        empty_page.setObjectName("batchEmptyState")
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(24, 24, 24, 24)
        empty_layout.setSpacing(10)
        empty_layout.addStretch(1)
        empty_icon = QLabel("◎")
        empty_icon.setObjectName("batchEmptyIcon")
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_title = QLabel("还没有运行任务")
        empty_title.setObjectName("batchEmptyTitle")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_description = QLabel(
            "先在“选择文件”中添加输入并选择批量流程，然后试运行首个文件或直接开始批处理。"
        )
        empty_description.setObjectName("batchEmptyDescription")
        empty_description.setAlignment(Qt.AlignCenter)
        empty_description.setWordWrap(True)
        empty_layout.addWidget(empty_icon)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_description)
        empty_actions = QHBoxLayout()
        empty_actions.addStretch(1)
        self.empty_back_button = QPushButton("返回选择文件")
        self.empty_back_button.clicked.connect(lambda: self.workspace_tabs.setCurrentIndex(0))
        self.empty_trial_button = QPushButton("首文件试运行")
        self.empty_trial_button.clicked.connect(lambda: self._start_run(True))
        self.empty_start_button = QPushButton("开始批处理")
        self.empty_start_button.setObjectName("generateBtn")
        self.empty_start_button.clicked.connect(lambda: self._start_run(False))
        empty_actions.addWidget(self.empty_back_button)
        empty_actions.addWidget(self.empty_trial_button)
        empty_actions.addWidget(self.empty_start_button)
        empty_actions.addStretch(1)
        empty_layout.addLayout(empty_actions)
        empty_layout.addStretch(1)
        self.result_stack.addWidget(empty_page)

        result_content = QWidget()
        result_content_layout = QVBoxLayout(result_content)
        result_content_layout.setContentsMargins(0, 0, 0, 0)
        result_splitter = QSplitter(Qt.Horizontal)
        result_splitter.setChildrenCollapsible(False)
        result_splitter.setHandleWidth(10)
        self.result_splitter = result_splitter
        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(["#", "输入文件", "状态", "耗时", "输出/说明"])
        self.queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.queue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.verticalHeader().setDefaultSectionSize(38)
        result_splitter.addWidget(self.queue_table)
        self.batch_log = QPlainTextEdit()
        self.batch_log.setObjectName("batchLog")
        self.batch_log.setReadOnly(True)
        self.batch_log.setMaximumBlockCount(5000)
        result_splitter.addWidget(self.batch_log)
        result_splitter.setSizes([760, 440])
        result_splitter.setMinimumHeight(360)
        result_content_layout.addWidget(result_splitter, 1)
        self.result_stack.addWidget(result_content)
        self.result_stack.setCurrentIndex(0)
        queue_layout.addWidget(self.result_stack, 1)
        self.run_summary_label = QLabel("尚未运行")
        self.run_summary_label.setObjectName("detailLabel")
        self.run_summary_label.setWordWrap(True)
        queue_layout.addWidget(self.run_summary_label)
        result_page_layout.addWidget(queue_frame, 1)
        self.results_scroll = self._scrollable_page(result_page, 500)
        self.workspace_tabs.addTab(self.results_scroll, "③ 运行结果")
        self.workspace_tabs.setCurrentIndex(0)
        self._update_responsive_layout()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_responsive_layout()

    def _update_responsive_layout(self) -> None:
        if not all(
            hasattr(self, name)
            for name in (
                "task_splitter",
                "result_splitter",
                "hero_badge",
                "preset_summary_label",
            )
        ):
            return
        width = max(0, self.width())
        compact = width < 860
        task_orientation = Qt.Vertical if compact else Qt.Horizontal
        if self.task_splitter.orientation() != task_orientation:
            self.task_splitter.setOrientation(task_orientation)
            self.task_splitter.setSizes([520, 260] if compact else [760, 360])

        result_orientation = Qt.Vertical if width < 900 else Qt.Horizontal
        if self.result_splitter.orientation() != result_orientation:
            self.result_splitter.setOrientation(result_orientation)
            self.result_splitter.setSizes(
                [330, 230] if result_orientation == Qt.Vertical else [760, 440]
            )

        self.hero_badge.setVisible(width >= 650)
        self.preset_summary_label.setVisible(width >= 930)

    def load_settings(self, config: dict) -> None:
        output_dir = str(
            config.get("batch_output_dir")
            or (Path(config.get("output_dir") or self.storage_dir) / "batch_runs")
        )
        self.output_dir_edit.setText(output_dir)
        wanted = str(config.get("batch_last_preset") or "")
        index = self.preset_combo.findData(wanted)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.isRunning()

    def _connect_editor_change_tracking(self) -> None:
        for editor in (
            self.preset_name_edit,
            self.description_edit,
            self.extensions_edit,
            self.version_edit,
        ):
            editor.textChanged.connect(self._mark_editor_dirty)
        self.arguments_edit.textChanged.connect(self._mark_editor_dirty)
        self.sequence_editor.raw_edit.textChanged.connect(self._mark_editor_dirty)
        self.timeout_spin.valueChanged.connect(self._mark_editor_dirty)
        self.variables_table.itemChanged.connect(self._mark_editor_dirty)
        self.output_rules_table.itemChanged.connect(self._mark_editor_dirty)

    def _mark_editor_dirty(self, *_args) -> None:
        if self._loading_editor:
            return
        self._editor_dirty = True
        self.continue_batch_button.setVisible(False)
        self._update_editor_state_chrome()

    def _preset_by_id(self, preset_id: str) -> batch.BatchPreset | None:
        return next((item for item in self.presets if item.id == preset_id), None)

    def _editor_base_preset(self) -> batch.BatchPreset | None:
        return self._preset_by_id(self._editor_preset_id)

    def _set_combo_to_id(self, preset_id: str) -> None:
        index = self.preset_combo.findData(preset_id)
        if index < 0:
            return
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(index)
        self.preset_combo.blockSignals(False)

    def _remove_draft_combo_item(self) -> None:
        index = self.preset_combo.findData(self.DRAFT_PRESET_ID)
        if index < 0:
            return
        self.preset_combo.blockSignals(True)
        self.preset_combo.removeItem(index)
        self.preset_combo.blockSignals(False)

    def _show_draft_in_selector(self) -> None:
        name = self.preset_name_edit.text().strip() or "未命名流程"
        index = self.preset_combo.findData(self.DRAFT_PRESET_ID)
        self.preset_combo.blockSignals(True)
        if index < 0:
            self.preset_combo.addItem(name, self.DRAFT_PRESET_ID)
            index = self.preset_combo.count() - 1
        else:
            self.preset_combo.setItemText(index, name)
        self.preset_combo.setCurrentIndex(index)
        self.preset_combo.blockSignals(False)

    def _set_preset_badge(self, text: str, state: str, tooltip: str) -> None:
        self.preset_summary_label.show()
        self.preset_summary_label.setText(text)
        self.preset_summary_label.setToolTip(tooltip)
        self.preset_summary_label.setProperty("state", state)
        self.preset_summary_label.style().unpolish(self.preset_summary_label)
        self.preset_summary_label.style().polish(self.preset_summary_label)

    def _update_editor_state_chrome(self) -> None:
        name = self.preset_name_edit.text().strip() or "未命名流程"
        if self._draft_kind:
            self._show_draft_in_selector()
            self._set_preset_badge(
                "未保存草稿",
                "draft",
                "这是尚未保存的新流程。运行会采用当前内容；保存后才会加入流程列表。",
            )
            self.task_preset_summary.setText(
                f"{name}\n当前将使用未保存草稿运行；保存后才会加入流程列表。"
            )
            return
        if self._editor_dirty:
            self._set_preset_badge(
                "有未保存修改",
                "draft",
                "运行会采用当前编辑内容。切换流程前会询问是否保存或放弃修改。",
            )
            self.task_preset_summary.setText(
                f"{name}\n当前将使用未保存修改运行。"
            )
            return
        if self._loaded_summary_text:
            self.task_preset_summary.setText(self._loaded_summary_text)

    def _restore_editor_selection(self) -> None:
        target_id = (
            self.DRAFT_PRESET_ID if self._draft_kind else self._editor_preset_id
        )
        self._set_combo_to_id(target_id)

    def _resolve_unsaved_changes(self) -> bool:
        if not self._editor_dirty:
            return True
        answer = QMessageBox.warning(
            self,
            "当前流程尚未保存",
            "当前编辑内容尚未保存。保存后继续，还是放弃这些修改？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Discard:
            self._editor_dirty = False
            return True
        base = self._editor_base_preset()
        if self._draft_kind or base is None or base.builtin:
            return self._save_as_preset()
        return self._update_current_preset()

    def _on_preset_selection_changed(self, index: int) -> None:
        target_id = str(self.preset_combo.itemData(index) or "")
        if not target_id or target_id == self.DRAFT_PRESET_ID:
            return
        if target_id == self._editor_preset_id and not self._draft_kind:
            return
        if not self._resolve_unsaved_changes():
            self._restore_editor_selection()
            return
        self._remove_draft_combo_item()
        self._set_combo_to_id(target_id)
        self._load_selected_preset()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.stop_button.setEnabled(False)
            self._set_run_state("正在停止", "warning")
            self._append_log("正在停止当前任务……")

    def _set_run_state(self, text: str, state: str) -> None:
        self.run_state_badge.setText(text)
        self.run_state_badge.setProperty("state", state)
        self.run_state_badge.style().unpolish(self.run_state_badge)
        self.run_state_badge.style().polish(self.run_state_badge)

    def _reload_presets(self, select_id: str = "") -> None:
        try:
            user_presets = batch.load_user_presets(self.presets_file)
        except batch.BatchValidationError as exc:
            user_presets = []
            self._append_log(str(exc))
        self.presets = batch.builtin_presets() + user_presets
        current = select_id or str(self.preset_combo.currentData() or "")
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in self.presets:
            self.preset_combo.addItem(preset.name, preset.id)
        index = self.preset_combo.findData(current)
        self.preset_combo.setCurrentIndex(index if index >= 0 else 0)
        self.preset_combo.blockSignals(False)
        self._load_selected_preset()

    def _selected_preset(self) -> batch.BatchPreset | None:
        preset_id = str(self.preset_combo.currentData() or "")
        return next((preset for preset in self.presets if preset.id == preset_id), None)

    def _load_selected_preset(self, _index: int | None = None) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        self._loading_editor = True
        try:
            self.preset_name_edit.setText(preset.name)
            self.description_edit.setText(preset.description)
            self.version_edit.setText(preset.multiwfn_version)
            self.timeout_spin.setValue(preset.timeout_seconds)
            self.extensions_edit.setText(", ".join(preset.input_extensions))
            self.arguments_edit.setPlainText("\n".join(preset.arguments))
            self.sequence_editor.set_text(preset.stdin_template)
            self._load_variables(preset.variables)
            self._load_output_rules(preset.output_rules)
        finally:
            self._loading_editor = False
        self._editor_preset_id = preset.id
        self._draft_kind = ""
        self._draft_return_preset_id = ""
        self._editor_dirty = False
        self._remove_draft_combo_item()
        self.update_button.setEnabled(not preset.builtin)
        self.delete_button.setEnabled(not preset.builtin)
        summary = (
            f"{preset.description or '未填写说明'}\n"
            f"支持 {len(preset.input_extensions)} 种输入格式"
        )
        origin = "内置" if preset.builtin else "自定义"
        version = preset.multiwfn_version or "未标注版本"
        self._set_preset_badge(
            f"{origin}流程",
            "builtin" if preset.builtin else "custom",
            f"{summary}\n适配版本：{version}",
        )
        self.preset_summary_label.hide()
        self._loaded_summary_text = f"{preset.name}\n{summary}"
        self.task_preset_summary.setText(self._loaded_summary_text)
        self.continue_batch_button.setVisible(False)

    def _new_preset(self) -> None:
        fallback_id = (
            self._editor_preset_id
            or self._draft_return_preset_id
            or str(self.preset_combo.currentData() or "")
        )
        if not self._resolve_unsaved_changes():
            return
        current = self._selected_preset() or self._preset_by_id(fallback_id)
        if current is None:
            return
        self._set_combo_to_id(current.id)
        self._load_selected_preset()
        self._loading_editor = True
        try:
            self.preset_name_edit.setText(current.name + " 副本")
        finally:
            self._loading_editor = False
        self._editor_preset_id = ""
        self._draft_kind = "copy"
        self._draft_return_preset_id = current.id
        self._editor_dirty = True
        self.update_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self._update_editor_state_chrome()
        self.workspace_tabs.setCurrentIndex(1)
        self.preset_name_edit.setFocus()
        self.preset_name_edit.selectAll()

    def _new_blank_preset(self) -> None:
        fallback_id = (
            self._editor_preset_id
            or self._draft_return_preset_id
            or str(self.preset_combo.currentData() or "")
        )
        if not self._resolve_unsaved_changes():
            return
        selected = self._selected_preset()
        return_id = selected.id if selected is not None else fallback_id
        common_extensions = [
            ".fch",
            ".fchk",
            ".wfn",
            ".wfx",
            ".mwfn",
            ".molden",
            ".molden.input",
            ".cub",
            ".pdb",
            ".xyz",
        ]
        if self.files:
            first_suffix = "".join(self.files[0].suffixes).lower()
            if first_suffix and first_suffix not in common_extensions:
                common_extensions.insert(0, first_suffix)
        self._loading_editor = True
        try:
            self.preset_name_edit.setText("新建 Multiwfn 批量流程")
            self.description_edit.setText("")
            self.extensions_edit.setText(", ".join(common_extensions))
            self.version_edit.setText(batch.CURRENT_MULTIWFN_VERSION)
            self.timeout_spin.setValue(600)
            self.arguments_edit.setPlainText("-isilent\n1")
            self.sequence_editor.set_text("")
            self._load_variables({})
            self._load_output_rules([])
        finally:
            self._loading_editor = False
        self._editor_preset_id = ""
        self._draft_kind = "new"
        self._draft_return_preset_id = return_id
        self._editor_dirty = True
        self.update_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self._update_editor_state_chrome()
        self.workspace_tabs.setCurrentIndex(1)
        self.template_scroll.verticalScrollBar().setValue(0)
        self.preset_name_edit.setFocus()
        self.preset_name_edit.selectAll()

    def _discard_editor_changes(self) -> None:
        target_id = (
            self._draft_return_preset_id
            if self._draft_kind
            else self._editor_preset_id
        )
        if not target_id and self.presets:
            target_id = self.presets[0].id
        self._editor_dirty = False
        self._draft_kind = ""
        self._remove_draft_combo_item()
        self._set_combo_to_id(target_id)
        self._load_selected_preset()

    def _toggle_advanced_settings(self, visible: bool) -> None:
        self.advanced_container.setVisible(bool(visible))

    def _replace_sequence_allowed(self, action: str) -> bool:
        if not self.sequence_editor.text().strip():
            return True
        answer = QMessageBox.question(
            self,
            action,
            "当前操作流程已有内容。是否用新内容替换？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _paste_command_sequence(self) -> None:
        text = QApplication.clipboard().text()
        if not text:
            QMessageBox.information(self, "剪贴板为空", "剪贴板中没有可载入的命令文本。")
            return
        if not self._replace_sequence_allowed("载入剪贴板命令"):
            return
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self.sequence_editor.set_text(normalized)
        self._append_log("已从剪贴板载入 Multiwfn 命令序列。")

    def _import_command_text(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入 Multiwfn 命令序列",
            "",
            "命令文本 (*.txt *.in *.inp);;All Files (*)",
        )
        if not path:
            return
        try:
            text = batch.read_command_text_file(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "命令文件无法导入", str(exc))
            return
        if not self._replace_sequence_allowed("导入命令 TXT"):
            return
        self.sequence_editor.set_text(text)
        self._append_log(f"已导入命令序列：{path}")

    def _record_command_sequence(self) -> None:
        if not self._replace_sequence_allowed("开始录制新的操作流程"):
            return
        multiwfn_exe = Path(self.multiwfn_path_getter()).expanduser()
        if not multiwfn_exe.is_file():
            QMessageBox.warning(
                self,
                "Multiwfn 路径无效",
                "请先在设置中选择 Multiwfn_2026.7.11_bin_Win64\\Multiwfn.exe。",
            )
            return
        enabled = self._enabled_files()
        if enabled:
            input_file = enabled[0]
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择一个文件完成示范操作", "", "All Files (*)"
            )
            if not path:
                return
            input_file = Path(path).expanduser().resolve()

        dialog = MultiwfnRecorderDialog(multiwfn_exe, input_file, self)
        if dialog.exec() != QDialog.Accepted:
            return
        self.sequence_editor.set_text(dialog.sequence_text)
        self._append_detected_outputs(input_file, dialog.generated_files)
        if not self.extensions_edit.text().strip():
            suffix = "".join(input_file.suffixes) or input_file.suffix
            self.extensions_edit.setText(suffix)
        details = f"，识别到 {len(dialog.generated_files)} 个结果文件" if dialog.generated_files else ""
        self._append_log(f"已采用 {len(dialog.recorded_commands)} 步操作记录{details}。")

    def _append_detected_outputs(
        self, input_file: Path, generated_files: list[Path]
    ) -> None:
        existing = {
            (self.output_rules_table.item(row, 0).text().strip().casefold())
            for row in range(self.output_rules_table.rowCount())
            if self.output_rules_table.item(row, 0)
        }
        for path in generated_files:
            name = path.name
            if not name or name.casefold() in existing:
                continue
            if name.casefold().startswith(input_file.stem.casefold()):
                rename = "${stem}" + name[len(input_file.stem) :]
            else:
                rename = "${stem}_" + name
            self._add_output_rule_row(name, rename, True)
            existing.add(name.casefold())
        self._sync_common_output_checks_from_table()

    def _load_output_rules(self, rules: list[batch.OutputRule]) -> None:
        self.output_rules_table.setRowCount(0)
        for rule in rules:
            self._add_output_rule_row(rule.pattern, rule.rename, rule.required)
        self._sync_common_output_checks_from_table()

    @staticmethod
    def _common_output_group(group_id: str) -> dict | None:
        return next(
            (
                group
                for group in COMMON_OUTPUT_GROUPS
                if str(group["id"]) == str(group_id)
            ),
            None,
        )

    @staticmethod
    def _pattern_belongs_to_group(pattern: str, group: dict) -> bool:
        normalized = str(pattern or "").strip().casefold()
        for wildcard in group["patterns"]:
            extension = str(wildcard).removeprefix("*").casefold()
            if normalized.endswith(extension):
                return True
        return False

    def _sync_common_output_checks_from_table(self) -> None:
        if not hasattr(self, "common_output_checks"):
            return
        self._syncing_common_outputs = True
        try:
            patterns = [
                self.output_rules_table.item(row, 0).text().strip()
                for row in range(self.output_rules_table.rowCount())
                if self.output_rules_table.item(row, 0)
            ]
            for group in COMMON_OUTPUT_GROUPS:
                check = self.common_output_checks[str(group["id"])]
                check.blockSignals(True)
                check.setChecked(
                    any(
                        self._pattern_belongs_to_group(pattern, group)
                        for pattern in patterns
                    )
                )
                check.blockSignals(False)
        finally:
            self._syncing_common_outputs = False

    def _on_common_output_toggled(self, group_id: str, checked: bool) -> None:
        if self._syncing_common_outputs:
            return
        group = self._common_output_group(group_id)
        if group is None:
            return
        matching_rows = [
            row
            for row in range(self.output_rules_table.rowCount())
            if self.output_rules_table.item(row, 0)
            and self._pattern_belongs_to_group(
                self.output_rules_table.item(row, 0).text(), group
            )
        ]
        if checked:
            if not matching_rows:
                for pattern in group["patterns"]:
                    self._add_output_rule_row(str(pattern), "", False)
        else:
            for row in reversed(matching_rows):
                self.output_rules_table.removeRow(row)
        self._sync_common_output_checks_from_table()
        self._mark_editor_dirty()

    def _add_output_rule_row(
        self,
        pattern: str = "",
        rename: str = "",
        required: bool = True,
        *,
        begin_edit: bool = False,
    ) -> None:
        row = self.output_rules_table.rowCount()
        self.output_rules_table.insertRow(row)
        pattern_item = QTableWidgetItem(str(pattern))
        rename_item = QTableWidgetItem(str(rename))
        required_item = QTableWidgetItem("")
        required_item.setFlags(
            Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
        )
        required_item.setCheckState(Qt.Checked if required else Qt.Unchecked)
        self.output_rules_table.setItem(row, 0, pattern_item)
        self.output_rules_table.setItem(row, 1, rename_item)
        self.output_rules_table.setItem(row, 2, required_item)
        if begin_edit:
            self.output_rules_table.setCurrentItem(pattern_item)
            self.output_rules_table.scrollToItem(pattern_item)
            self.output_rules_table.editItem(pattern_item)
        self._sync_common_output_checks_from_table()

    def _remove_output_rule_rows(self) -> None:
        rows = sorted(
            {item.row() for item in self.output_rules_table.selectedItems()}, reverse=True
        )
        for row in rows:
            self.output_rules_table.removeRow(row)
        self._sync_common_output_checks_from_table()
        if rows:
            self._mark_editor_dirty()

    def _load_variables(self, variables: dict[str, str]) -> None:
        self.variables_table.setRowCount(0)
        for name, value in variables.items():
            self._add_variable_row(name, value)

    def _add_variable_row(
        self, name: str = "", value: str = "", *, begin_edit: bool = False
    ) -> None:
        row = self.variables_table.rowCount()
        self.variables_table.insertRow(row)
        name_item = QTableWidgetItem(str(name))
        self.variables_table.setItem(row, 0, name_item)
        self.variables_table.setItem(row, 1, QTableWidgetItem(str(value)))
        if begin_edit:
            self.variables_table.setCurrentItem(name_item)
            self.variables_table.scrollToItem(name_item)
            self.variables_table.editItem(name_item)

    def _remove_variable_rows(self) -> None:
        rows = sorted(
            {item.row() for item in self.variables_table.selectedItems()}, reverse=True
        )
        for row in rows:
            self.variables_table.removeRow(row)
        if rows:
            self._mark_editor_dirty()

    def _editor_variables(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in range(self.variables_table.rowCount()):
            name_item = self.variables_table.item(row, 0)
            value_item = self.variables_table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            if name:
                result[name] = value_item.text() if value_item else ""
        return result

    def _parse_output_rules(self) -> list[batch.OutputRule]:
        result: list[batch.OutputRule] = []
        for row in range(self.output_rules_table.rowCount()):
            pattern_item = self.output_rules_table.item(row, 0)
            rename_item = self.output_rules_table.item(row, 1)
            required_item = self.output_rules_table.item(row, 2)
            pattern = pattern_item.text().strip() if pattern_item else ""
            if not pattern:
                continue
            rename = rename_item.text().strip() if rename_item else ""
            required = required_item is None or required_item.checkState() == Qt.Checked
            result.append(batch.OutputRule(pattern, rename, required))
        return result

    def _preset_from_editor(self, *, preset_id: str = "temporary") -> batch.BatchPreset:
        extensions = [
            item.strip()
            for item in self.extensions_edit.text().replace(";", ",").split(",")
            if item.strip()
        ]
        arguments = [
            line.strip()
            for line in self.arguments_edit.toPlainText().splitlines()
            if line.strip()
        ]
        preset = batch.BatchPreset(
            id=preset_id,
            name=self.preset_name_edit.text().strip(),
            description=self.description_edit.text().strip(),
            input_extensions=extensions,
            arguments=arguments,
            stdin_template=self.sequence_editor.text(),
            output_rules=self._parse_output_rules(),
            variables=self._editor_variables(),
            timeout_seconds=self.timeout_spin.value(),
            multiwfn_version=self.version_edit.text().strip(),
            builtin=False,
        )
        preset.validate()
        return preset

    def _save_as_preset(self) -> bool:
        try:
            preset = self._preset_from_editor(
                preset_id=f"user_{uuid.uuid4().hex[:10]}"
            )
            users = [item for item in self.presets if not item.builtin]
            users.append(preset)
            batch.save_user_presets(self.presets_file, users)
        except Exception as exc:
            QMessageBox.critical(self, "流程保存失败", str(exc))
            return False
        self._reload_presets(preset.id)
        self._append_log(f"已保存自定义流程：{preset.name}")
        return True

    def _update_current_preset(self) -> bool:
        current = self._editor_base_preset()
        if current is None or current.builtin:
            QMessageBox.information(self, "无法更新", "内置流程请使用“保存为新流程”。")
            return False
        try:
            replacement = self._preset_from_editor(preset_id=current.id)
            users = [
                replacement if item.id == current.id else item
                for item in self.presets
                if not item.builtin
            ]
            batch.save_user_presets(self.presets_file, users)
        except Exception as exc:
            QMessageBox.critical(self, "流程更新失败", str(exc))
            return False
        self._reload_presets(replacement.id)
        self._append_log(f"已更新流程：{replacement.name}")
        return True

    def _delete_current_preset(self) -> None:
        current = self._editor_base_preset()
        if current is None or current.builtin:
            return
        answer = QMessageBox.question(
            self,
            "删除流程",
            f"确定删除“{current.name}”吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        users = [
            item for item in self.presets if not item.builtin and item.id != current.id
        ]
        batch.save_user_presets(self.presets_file, users)
        self._reload_presets()
        self._append_log(f"已删除流程：{current.name}")

    def _import_presets(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 Multiwfn 批量流程", "", "JSON (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            imported = batch.load_preset_file(Path(path))
            users = [item for item in self.presets if not item.builtin]
            known = {item.id for item in users} | {item.id for item in batch.builtin_presets()}
            for preset in imported:
                if preset.id in known:
                    preset.id = f"user_{uuid.uuid4().hex[:10]}"
                preset.builtin = False
                users.append(preset)
                known.add(preset.id)
            batch.save_user_presets(self.presets_file, users)
        except Exception as exc:
            QMessageBox.critical(self, "流程导入失败", str(exc))
            return
        self._reload_presets(imported[-1].id if imported else "")
        self._append_log(f"已导入 {len(imported)} 个批量流程。")

    def _export_current_preset(self) -> None:
        base = self._editor_base_preset()
        try:
            preset = self._preset_from_editor(
                preset_id=base.id if base is not None else "multiwfn_flow"
            )
        except Exception as exc:
            QMessageBox.critical(self, "流程无效", str(exc))
            return
        default_name = f"{preset.id}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出批量流程", default_name, "JSON (*.json)"
        )
        if not path:
            return
        try:
            batch.save_preset_file(Path(path), [preset])
        except Exception as exc:
            QMessageBox.critical(self, "流程导出失败", str(exc))
            return
        self._append_log(f"流程已导出：{path}")

    def _current_extensions(self) -> list[str]:
        try:
            values = [
                item.strip()
                for item in self.extensions_edit.text().replace(";", ",").split(",")
                if item.strip()
            ]
            return batch.normalize_extensions(values)
        except Exception:
            preset = self._editor_base_preset() or self._selected_preset()
            return preset.input_extensions if preset else [".fch", ".fchk"]

    def _add_files(self) -> None:
        extensions = self._current_extensions()
        filter_text = "支持的输入 (" + " ".join(f"*{ext}" for ext in extensions) + ")"
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添加 Multiwfn 输入文件", "", filter_text + ";;All Files (*)"
        )
        self._append_files(Path(path) for path in paths)

    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "扫描输入文件夹", "")
        if not path:
            return
        files = batch.scan_input_files(
            [Path(path)], self._current_extensions(), recursive=self.recursive_check.isChecked()
        )
        self._append_files(files)
        self._append_log(f"从文件夹扫描到 {len(files)} 个匹配文件。")

    @staticmethod
    def _file_key(path: Path) -> str:
        return os.path.normcase(str(Path(path).expanduser().resolve()))

    def _capture_file_enabled_states(self) -> None:
        for row, path in enumerate(self.files):
            item = self.file_table.item(row, 0)
            if item is not None:
                self.file_enabled[self._file_key(path)] = (
                    item.checkState() == Qt.Checked
                )

    def _on_file_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        row = item.row()
        if 0 <= row < len(self.files):
            self.file_enabled[self._file_key(self.files[row])] = (
                item.checkState() == Qt.Checked
            )

    def _handle_dropped_paths(self, paths: list[Path]) -> None:
        files: list[Path] = []
        folders: list[Path] = []
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if path.is_file():
                files.append(path)
            elif path.is_dir():
                folders.append(path)
        if folders:
            files.extend(
                batch.scan_input_files(
                    folders,
                    self._current_extensions(),
                    recursive=self.recursive_check.isChecked(),
                )
            )
        before = len(self.files)
        self._append_files(files)
        added = len(self.files) - before
        if added:
            self._append_log(f"已通过拖放添加 {added} 个输入文件。")
        elif paths:
            self._append_log("拖入的内容中没有当前流程支持的新文件。")

    def _append_files(self, paths) -> None:
        self._capture_file_enabled_states()
        known = {self._file_key(path) for path in self.files}
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            key = self._file_key(path)
            if path.is_file() and key not in known:
                self.files.append(path)
                self.file_enabled[key] = True
                known.add(key)
        self._refresh_file_table()

    def _refresh_file_table(self) -> None:
        self.file_table.blockSignals(True)
        self.file_table.setRowCount(0)
        for path in self.files:
            row = self.file_table.rowCount()
            self.file_table.insertRow(row)
            enabled = QTableWidgetItem("")
            enabled.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            enabled.setCheckState(
                Qt.Checked
                if self.file_enabled.get(self._file_key(path), True)
                else Qt.Unchecked
            )
            name = QTableWidgetItem(str(path))
            name.setToolTip(str(path))
            extension = QTableWidgetItem("".join(path.suffixes) or path.suffix)
            self.file_table.setItem(row, 0, enabled)
            self.file_table.setItem(row, 1, name)
            self.file_table.setItem(row, 2, extension)
        self.file_table.blockSignals(False)
        self.file_count_label.setText(f"{len(self.files)} 个文件")
        if hasattr(self, "continue_batch_button"):
            self.continue_batch_button.setVisible(False)

    def _remove_selected_files(self) -> None:
        self._capture_file_enabled_states()
        rows = sorted({item.row() for item in self.file_table.selectedItems()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self.files):
                removed = self.files.pop(row)
                self.file_enabled.pop(self._file_key(removed), None)
        self._refresh_file_table()

    def _clear_files(self) -> None:
        self.files.clear()
        self.file_enabled.clear()
        self._refresh_file_table()

    def _enabled_files(self) -> list[Path]:
        self._capture_file_enabled_states()
        result = []
        for row, path in enumerate(self.files):
            item = self.file_table.item(row, 0)
            if item is None or item.checkState() == Qt.Checked:
                result.append(path)
        return result

    def _pick_output_dir(self) -> None:
        current = self.output_dir_edit.text().strip() or str(self.storage_dir)
        path = QFileDialog.getExistingDirectory(self, "选择批处理结果目录", current)
        if path:
            self.output_dir_edit.setText(path)
            self.settingsChanged.emit({"batch_output_dir": path})

    def _validated_run_inputs(self) -> tuple[batch.BatchPreset, list[Path], Path, Path]:
        preset = self._preset_from_editor(
            preset_id=self._editor_preset_id or "unsaved_draft"
        )
        files = self._enabled_files()
        if not files:
            raise batch.BatchValidationError("请先添加并启用至少一个输入文件。")
        output_root = Path(self.output_dir_edit.text().strip()).expanduser()
        if not str(output_root):
            raise batch.BatchValidationError("请选择结果目录。")
        output_root.mkdir(parents=True, exist_ok=True)
        output_root = output_root.resolve()
        exe = Path(self.multiwfn_path_getter().strip()).expanduser()
        if not exe.is_file():
            raise batch.BatchValidationError("请先在左侧设置有效的 Multiwfn.exe 路径。")
        return preset, files, output_root, exe.resolve()

    def _preview_run(self) -> None:
        try:
            preset, files, output_root, exe = self._validated_run_inputs()
            plan = batch.create_batch_plan(files, preset, output_root, preset.variables, prefix="preview")
            preview = batch.render_job_preview(plan, plan.jobs[0], exe)
        except Exception as exc:
            QMessageBox.critical(self, "预检失败", str(exc))
            return
        command = subprocess.list2cmdline(preview["command"])
        text = (
            f"流程：{preset.name}\n"
            f"文件：{len(files)} 个\n"
            f"结果根目录：{output_root}\n\n"
            f"首个任务命令：\n{command}\n\n"
            f"首个任务输入序列：\n{preview['stdin']}"
        )
        dialog = QMessageBox(self)
        dialog.setWindowTitle("批处理预检通过")
        dialog.setIcon(QMessageBox.Information)
        dialog.setText("批量流程和输入文件检查通过。")
        dialog.setDetailedText(text)
        dialog.exec()

    def _populate_queue(self, files: list[Path]) -> None:
        self.queue_table.setRowCount(0)
        for index, path in enumerate(files, 1):
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            self.queue_table.setItem(row, 0, QTableWidgetItem(str(index)))
            file_item = QTableWidgetItem(path.name)
            file_item.setToolTip(str(path))
            self.queue_table.setItem(row, 1, file_item)
            self.queue_table.setItem(row, 2, QTableWidgetItem(STATUS_TEXT[batch.STATUS_PENDING]))
            self.queue_table.setItem(row, 3, QTableWidgetItem("-"))
            self.queue_table.setItem(row, 4, QTableWidgetItem("等待运行"))
        self.result_stack.setCurrentIndex(1)

    def _start_run(self, trial: bool) -> None:
        if self.is_running():
            return
        try:
            preset, files, output_root, exe = self._validated_run_inputs()
        except Exception as exc:
            QMessageBox.critical(self, "无法开始", str(exc))
            return
        if trial:
            files = files[:1]
        self._active_run_mode = "trial" if trial else "batch"
        self.continue_batch_button.setVisible(False)
        self._populate_queue(files)
        self.batch_log.clear()
        self.progress.setRange(0, len(files))
        self.progress.setValue(0)
        self.run_summary_label.setText("正在准备试运行……" if trial else "正在准备批处理……")
        self._set_run_state("准备试运行" if trial else "准备批处理", "running")
        self.start_button.setEnabled(False)
        self.trial_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.open_results_button.setEnabled(False)
        self.workspace_tabs.setCurrentIndex(2)
        self.settingsChanged.emit(
            {
                "batch_output_dir": str(output_root),
                "batch_last_preset": (
                    self._editor_preset_id or self._draft_return_preset_id
                ),
            }
        )

        self.thread = QThread(self)
        self.worker = BatchExecutionWorker(
            files,
            preset,
            preset.variables,
            output_root,
            exe,
            "trial" if trial else "batch",
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.event.connect(self._on_worker_event)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.finished.connect(lambda _result, _error: self.thread.quit() if self.thread else None)
        self.thread.finished.connect(self._cleanup_thread)
        self.thread.start()

    @Slot(object)
    def _on_worker_event(self, event: dict) -> None:
        kind = str(event.get("kind") or "")
        if kind == "batch_started":
            self.last_run_dir = str(event.get("run_dir") or "")
            self.run_summary_label.setText(f"运行目录：{self.last_run_dir}")
            self._set_run_state("运行中", "running")
            self._append_log(
                f"开始执行 {event.get('total')} 个任务：{event.get('preset')}"
            )
        elif kind == "output":
            text = str(event.get("text") or "")
            if text:
                self.batch_log.appendPlainText(text)
        elif kind == "warning":
            self._append_log("警告：" + str(event.get("message") or ""))
        elif kind == "job_status":
            index = int(event.get("index") or 0)
            row = index - 1
            if 0 <= row < self.queue_table.rowCount():
                status = str(event.get("status") or "")
                self.queue_table.setItem(row, 2, QTableWidgetItem(STATUS_TEXT.get(status, status)))
                duration = event.get("duration")
                if duration is not None:
                    self.queue_table.setItem(row, 3, QTableWidgetItem(f"{float(duration):.2f}s"))
                message = str(event.get("message") or "")
                self.queue_table.setItem(row, 4, QTableWidgetItem(message))
                if message:
                    self._append_log(message)
        elif kind == "progress":
            self._set_progress_animated(int(event.get("completed") or 0))

    @Slot(object, object)
    def _on_worker_finished(self, result: dict | None, error: str | None) -> None:
        completed_mode = self._active_run_mode
        self._active_run_mode = ""
        self.start_button.setEnabled(True)
        self.trial_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if error:
            self.continue_batch_button.setVisible(False)
            self.run_summary_label.setText(f"运行失败：{error}")
            self._set_run_state("运行失败", "failed")
            self._append_log(f"运行失败：{error}")
            QMessageBox.critical(self, "批处理失败", error)
            return
        assert result is not None
        self.last_run_dir = str(result.get("run_dir") or self.last_run_dir)
        self.open_results_button.setEnabled(bool(self.last_run_dir))
        summary = (
            f"完成：成功 {result.get('success', 0)}，失败 {result.get('failed', 0)}，"
            f"取消 {result.get('cancelled', 0)}；汇总：{result.get('summary', '')}"
        )
        self.run_summary_label.setText(summary)
        failed_count = int(result.get("failed") or 0)
        cancelled_count = int(result.get("cancelled") or 0)
        if failed_count > 0:
            self._set_run_state("部分任务失败", "failed")
        elif cancelled_count > 0:
            self._set_run_state("已取消", "warning")
        else:
            self._set_run_state("全部完成", "success")
        self._append_log(summary)
        if (
            completed_mode == "trial"
            and failed_count == 0
            and cancelled_count == 0
            and int(result.get("success") or 0) > 0
        ):
            total = len(self._enabled_files())
            if total > 1:
                self.continue_batch_button.setText(
                    f"试运行通过，开始全部 {total} 个文件"
                )
                self.continue_batch_button.setToolTip(
                    "使用当前流程重新执行完整批次；首个文件会纳入同一批结果中。"
                )
                self.continue_batch_button.setVisible(True)
        else:
            self.continue_batch_button.setVisible(False)
        if int(result.get("failed") or 0) > 0:
            QMessageBox.warning(self, "批处理完成", summary)
        else:
            QMessageBox.information(self, "批处理完成", summary)

    def _continue_full_batch(self) -> None:
        self.continue_batch_button.setVisible(False)
        self._start_run(False)

    @Slot()
    def _cleanup_thread(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None

    def _open_results(self) -> None:
        if self.last_run_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_run_dir))

    def _append_log(self, text: str) -> None:
        self.batch_log.appendPlainText(str(text))
