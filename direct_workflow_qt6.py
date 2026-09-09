from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable

import vmd_style_tool as core
import qt_feedback
from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDoubleValidator
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


CUBE_SUFFIXES = {".cub", ".cube"}
INTERMEDIATE_SUFFIXES = CUBE_SUFFIXES | {".dat"}


def is_cube_file(path: Path) -> bool:
    return path.suffix.lower() in CUBE_SUFFIXES


def file_snapshot(
    directories: list[Path], suffixes: set[str]
) -> dict[Path, tuple[int, int]]:
    snapshot: dict[Path, tuple[int, int]] = {}
    seen: set[Path] = set()
    for directory in directories:
        try:
            resolved_dir = directory.resolve()
        except OSError:
            continue
        if resolved_dir in seen or not resolved_dir.is_dir():
            continue
        seen.add(resolved_dir)
        try:
            candidates = list(resolved_dir.iterdir())
        except OSError:
            continue
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in suffixes:
                continue
            try:
                stat = candidate.stat()
                snapshot[candidate.resolve()] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue
    return snapshot


def cube_snapshot(directories: list[Path]) -> dict[Path, tuple[int, int]]:
    return file_snapshot(directories, CUBE_SUFFIXES)


def _build_multi_cube_vmd_tcl(
    style: dict, rep0_commands: list[str] | None, cube_count: int
) -> str:
    """Extend the existing direct-drawing script to load extra Cube molecules."""

    script = core.build_vmd_tcl(style, rep0_commands=rep0_commands)
    if cube_count <= 1:
        return script

    lines = script.rstrip("\n").splitlines()
    load_line = "mol new $AUTO_CUBE_FILE type cube waitfor all"
    surface_mode = str(style.get("surface_mode") or "signed")
    end_line = (
        "mol modmaterial 1 top $mater"
        if surface_mode == "volume_mapped"
        else "mol modmaterial 2 top $mater"
    )
    start = lines.index(load_line)
    end = next(
        index for index in range(start, len(lines)) if lines[index] == end_line
    )
    molecule_block = lines[start : end + 1]
    extra_lines = [
        "",
        "# Load the additional Cube files selected in the direct workflow.",
        "for {set AUTO_CUBE_INDEX 2} {$AUTO_CUBE_INDEX <= $::env(CUBE_FILE_COUNT)} {incr AUTO_CUBE_INDEX} {",
        '    set AUTO_CUBE_ENV_NAME "CUBE_FILE_$AUTO_CUBE_INDEX"',
        "    set AUTO_CUBE_FILE [file normalize [set ::env($AUTO_CUBE_ENV_NAME)]]",
    ]
    if surface_mode == "volume_mapped":
        extra_lines.extend(
            [
                '    set AUTO_COLOR_ENV_NAME "COLOR_CUBE_FILE_$AUTO_CUBE_INDEX"',
                "    set AUTO_COLOR_CUBE_FILE [file normalize [set ::env($AUTO_COLOR_ENV_NAME)]]",
            ]
        )
    extra_lines.extend(f"    {line}" for line in molecule_block)
    extra_lines.append("}")
    lines[end + 1 : end + 1] = extra_lines
    return "\n".join(lines) + "\n"


class FileDropZone(QFrame):
    fileSelected = Signal(str)
    browseRequested = Signal()
    invalidDrop = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("directDropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(155)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(7)
        layout.setAlignment(Qt.AlignCenter)
        icon = QLabel("＋")
        icon.setObjectName("directDropIcon")
        icon.setAlignment(Qt.AlignCenter)
        title = QLabel("将一个本地文件拖到这里")
        title.setObjectName("directDropTitle")
        title.setAlignment(Qt.AlignCenter)
        subtitle = QLabel("或点击此区域选择文件 · Cube 可直接绘图\n其他文件将交给 Multiwfn")
        subtitle.setObjectName("helperText")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(subtitle)

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(url.isLocalFile() for url in urls):
            event.acceptProposedAction()
            self._set_drag_active(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        self._set_drag_active(False)
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        files = [path for path in paths if path.is_file()]
        if len(files) != 1:
            self.invalidDrop.emit("直接绘图一次只接受一个文件，请只拖入一个文件。")
            event.ignore()
            return
        self.fileSelected.emit(str(files[0]))
        event.acceptProposedAction()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self.browseRequested.emit()
        super().mousePressEvent(event)


class DirectWorkflowPage(QWidget):
    backRequested = Signal()

    def __init__(
        self,
        multiwfn_path_getter: Callable[[], str],
        vmd_path_getter: Callable[[], str],
    ) -> None:
        super().__init__()
        self.multiwfn_path_getter = multiwfn_path_getter
        self.vmd_path_getter = vmd_path_getter
        self.style_data: dict = {}
        self.rep0_commands: list[str] | None = None
        self.source_path: Path | None = None
        self.cube_path: Path | None = None
        self.cube_paths: list[Path] = []
        self.multiwfn_process: subprocess.Popen[bytes] | None = None
        self.vmd_process: subprocess.Popen[bytes] | None = None
        self.temp_tcl_path: Path | None = None
        self.before_cubes: dict[Path, tuple[int, int]] = {}
        self.before_intermediates: dict[Path, tuple[int, int]] = {}
        self.generated_intermediates: set[Path] = set()
        self.scan_directories: list[Path] = []
        self.cancel_requested = False

        self.process_timer = QTimer(self)
        self.process_timer.setInterval(400)
        self.process_timer.timeout.connect(self._poll_processes)

        self._build_ui()

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("workflowCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("paneTitle")
        layout.addWidget(label)
        return frame, layout

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setObjectName("directWorkflowScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(2, 2, 8, 8)
        body_layout.setSpacing(12)
        scroll.setWidget(body)
        root_layout.addWidget(scroll, 1)

        style_card, style_layout = self._card("当前绘图风格")
        style_row = QHBoxLayout()
        style_row.setSpacing(14)
        self.style_name_label = QLabel("尚未选择风格")
        self.style_name_label.setObjectName("workflowStyleName")
        self.style_name_label.setWordWrap(True)
        self.style_meta_label = QLabel("")
        self.style_meta_label.setObjectName("helperText")
        self.style_meta_label.setWordWrap(True)
        style_text = QVBoxLayout()
        style_text.setSpacing(4)
        style_text.addWidget(self.style_name_label)
        style_text.addWidget(self.style_meta_label)
        style_row.addLayout(style_text, 1)
        self.change_style_button = QPushButton("更换风格")
        self.change_style_button.clicked.connect(self._request_back)
        style_row.addWidget(self.change_style_button)
        style_layout.addLayout(style_row)
        body_layout.addWidget(style_card)

        source_card, source_layout = self._card("添加输入文件")
        self.source_card = source_card
        self.drop_zone = FileDropZone()
        self.drop_zone.fileSelected.connect(self.set_source_file)
        self.drop_zone.browseRequested.connect(self._browse_source)
        self.drop_zone.invalidDrop.connect(self._show_invalid_drop)
        source_layout.addWidget(self.drop_zone)

        self.file_info_frame = QFrame()
        self.file_info_frame.setObjectName("selectedFileCard")
        file_info_layout = QHBoxLayout(self.file_info_frame)
        file_info_layout.setContentsMargins(12, 10, 12, 10)
        self.file_info_label = QLabel("")
        self.file_info_label.setWordWrap(True)
        file_info_layout.addWidget(self.file_info_label, 1)
        replace_file_button = QPushButton("更换文件")
        replace_file_button.clicked.connect(self._browse_source)
        file_info_layout.addWidget(replace_file_button)
        self.file_info_frame.hide()
        source_layout.addWidget(self.file_info_frame)
        body_layout.addWidget(source_card)

        settings_card, settings_layout = self._card("运行设置")
        self.settings_card = settings_card
        output_label = QLabel("结果与 VMD 图片保存目录")
        output_label.setObjectName("fieldLabel")
        settings_layout.addWidget(output_label)
        output_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("添加文件后默认使用该文件所在目录")
        output_row.addWidget(self.output_dir_edit, 1)
        output_button = QPushButton("选择目录")
        output_button.clicked.connect(self._browse_output_dir)
        output_row.addWidget(output_button)
        settings_layout.addLayout(output_row)
        iso_label = QLabel("等值面数值")
        iso_label.setObjectName("fieldLabel")
        settings_layout.addWidget(iso_label)
        self.iso_edit = QLineEdit()
        validator = QDoubleValidator(0.000000000001, 1.0e12, 12, self.iso_edit)
        validator.setNotation(QDoubleValidator.ScientificNotation)
        self.iso_edit.setValidator(validator)
        self.iso_edit.setPlaceholderText("例如 0.05")
        settings_layout.addWidget(self.iso_edit)
        self.iso_hint = QLabel("请按分析需要填写正数，负等值面会自动使用相反数。")
        self.iso_hint.setObjectName("helperText")
        settings_layout.addWidget(self.iso_hint)
        body_layout.addWidget(settings_card)

        status_card, status_layout = self._card("工作流状态")
        self.status_label = QLabel("请选择或拖入一个文件。")
        self.status_label.setObjectName("workflowStatus")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        self.session_log = QPlainTextEdit()
        self.session_log.setReadOnly(True)
        self.session_log.setMaximumBlockCount(500)
        self.session_log.setFixedHeight(110)
        self.session_log.setPlaceholderText("运行进度会显示在这里")
        self.session_log.hide()
        status_layout.addWidget(self.session_log)
        body_layout.addWidget(status_card)
        body_layout.addStretch(1)

        footer = QFrame()
        footer.setObjectName("workflowFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 10, 12, 10)
        footer_layout.setSpacing(8)
        self.back_button = QPushButton("返回绘图方案")
        self.back_button.clicked.connect(self._request_back)
        footer_layout.addWidget(self.back_button)
        self.open_dir_button = QPushButton("打开结果目录")
        self.open_dir_button.clicked.connect(self._open_output_dir)
        self.open_dir_button.setEnabled(False)
        footer_layout.addWidget(self.open_dir_button)
        self.manual_cube_button = QPushButton("手动选择 Cube（可多选）")
        self.manual_cube_button.clicked.connect(self._manual_select_cube)
        self.manual_cube_button.hide()
        footer_layout.addWidget(self.manual_cube_button)
        footer_layout.addStretch(1)
        self.stop_button = QPushButton("停止工作流")
        self.stop_button.setObjectName("dangerBtn")
        self.stop_button.clicked.connect(self.stop_workflow)
        self.stop_button.hide()
        footer_layout.addWidget(self.stop_button)
        self.finish_delete_button = QPushButton("完成并删除中间文件")
        self.finish_delete_button.setObjectName("dangerBtn")
        self.finish_delete_button.clicked.connect(
            lambda: self._finish_workflow(delete_intermediates=True)
        )
        self.finish_delete_button.hide()
        footer_layout.addWidget(self.finish_delete_button)
        self.finish_keep_button = QPushButton("完成")
        self.finish_keep_button.clicked.connect(
            lambda: self._finish_workflow(delete_intermediates=False)
        )
        self.finish_keep_button.hide()
        footer_layout.addWidget(self.finish_keep_button)
        self.start_button = QPushButton("开始直接绘图")
        self.start_button.setObjectName("primaryBtn")
        self.start_button.clicked.connect(self.start_workflow)
        self.start_button.setEnabled(False)
        footer_layout.addWidget(self.start_button)
        root_layout.addWidget(footer)

    def configure_style(
        self, style: dict, rep0_commands: list[str] | None, selection_text: str
    ) -> None:
        self.style_data = dict(style)
        self.rep0_commands = list(rep0_commands) if rep0_commands else None
        self.style_name_label.setText(str(style.get("name") or "未命名风格"))
        material = str(style.get("material") or "Glossy")
        surface_mode = str(style.get("surface_mode") or "signed")
        if surface_mode == "volume_mapped":
            method = str(style.get("color_scale_method") or "BWR")
            low = float(style.get("color_scale_min", -0.03))
            high = float(style.get("color_scale_max", 0.03))
            self.style_meta_label.setText(
                f"{selection_text} · 材质 {material} · 电子密度等值面映射 ESP · {method} {low:g}～{high:g} a.u."
            )
            self.iso_hint.setText(
                "填写电子密度等值面值；ESP 风格需要一对空间坐标对齐的电子密度 Cube 与 ESP Cube。"
            )
        else:
            pos = str(style.get("pos_color_expr") or f"ColorID {style.get('pos_color', 1)}")
            neg = str(style.get("neg_color_expr") or f"ColorID {style.get('neg_color', 0)}")
            self.style_meta_label.setText(
                f"{selection_text} · 材质 {material} · 正等值面 {pos} · 负等值面 {neg}"
            )
            self.iso_hint.setText("请按分析需要填写正数，负等值面会自动使用相反数。")
        self.iso_edit.setText(format(float(style.get("default_iso_value", 0.05)), ".12g"))

    def set_source_file(self, raw_path: str) -> None:
        if self.is_running():
            self._set_status("当前任务运行中，停止或完成后可以更换文件。")
            return
        path = Path(raw_path).expanduser()
        if not path.is_file():
            self._set_status("未能添加文件：请选择一个存在的本地文件。")
            return
        try:
            path = path.resolve()
        except OSError:
            pass
        if self.source_path != path:
            self.before_cubes.clear()
            self.before_intermediates.clear()
            self.generated_intermediates.clear()
            self.scan_directories.clear()
        self.source_path = path
        self.cube_path = path if is_cube_file(path) else None
        self.cube_paths = [path] if is_cube_file(path) else []
        self.output_dir_edit.setText(str(path.parent))
        self.open_dir_button.setEnabled(True)
        kind = "Cube 格点文件" if is_cube_file(path) else "由 Multiwfn 打开的输入文件"
        route = "跳过 Multiwfn，直接进入 VMD" if is_cube_file(path) else "打开 Multiwfn，生成 Cube 后进入 VMD"
        self.file_info_label.setText(
            f"{path.name}\n类型：{kind}\n处理方式：{route}"
        )
        self.file_info_frame.show()
        self.manual_cube_button.hide()
        self._set_finish_actions_visible(False)
        self.start_button.setEnabled(True)
        self.start_button.setText("在 VMD 中直接绘图" if is_cube_file(path) else "打开 Multiwfn 并继续")
        self._set_status(f"已添加 {path.name}。请填写等值面数值后开始。")
        self._append_log(f"已添加文件：{path}")

    def _browse_source(self) -> None:
        current = str(self.source_path.parent) if self.source_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择绘图输入文件",
            current,
            "Multiwfn / Cube (*.cub *.cube *.fch *.fchk *.wfn *.wfx *.mwfn *.molden *.input *.out *.log);;所有文件 (*)",
        )
        if path:
            self.set_source_file(path)

    def _show_invalid_drop(self, message: str) -> None:
        self._set_status(message)

    def _browse_output_dir(self) -> None:
        current = self.output_dir_edit.text().strip()
        if not current and self.source_path:
            current = str(self.source_path.parent)
        path = QFileDialog.getExistingDirectory(self, "选择结果和图片保存目录", current)
        if path:
            self.output_dir_edit.setText(path)
            self.open_dir_button.setEnabled(True)
            self._append_log(f"结果目录已改为：{path}")

    def _validated_output_dir(self) -> Path:
        raw = self.output_dir_edit.text().strip()
        if not raw and self.source_path:
            raw = str(self.source_path.parent)
            self.output_dir_edit.setText(raw)
        if not raw:
            raise ValueError("请选择结果和图片保存目录。")
        path = Path(raw).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def _validated_iso(self) -> float:
        raw = self.iso_edit.text().strip().replace(",", ".")
        if not raw:
            raise ValueError("请输入等值面数值。")
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("等值面数值必须是有效数字。") from exc
        if not value > 0 or not value < float("inf"):
            raise ValueError("等值面数值必须是大于零的有限数字。")
        return value

    def start_workflow(self) -> None:
        if self.is_running():
            return
        if not self.style_data:
            self._set_status("请先返回绘图方案选择绘图风格。")
            return
        if self.source_path is None or not self.source_path.is_file():
            self._set_status("请先选择或拖入一个文件。")
            return
        try:
            output_dir = self._validated_output_dir()
            iso_value = self._validated_iso()
        except (OSError, ValueError) as exc:
            qt_feedback.show_error(self, "运行设置不完整", exc, stage="检查直接绘图设置")
            return

        selected_cubes = [path for path in self.cube_paths if path.is_file()]
        if selected_cubes:
            self._launch_vmd(selected_cubes, iso_value, output_dir)
        elif self.cube_path is not None and self.cube_path.is_file():
            self._launch_vmd([self.cube_path], iso_value, output_dir)
        elif is_cube_file(self.source_path):
            self.cube_path = self.source_path
            self.cube_paths = [self.source_path]
            self._launch_vmd([self.source_path], iso_value, output_dir)
        else:
            self._launch_multiwfn(output_dir)

    def _launch_multiwfn(self, output_dir: Path) -> None:
        assert self.source_path is not None
        multi_raw = self.multiwfn_path_getter().strip()
        multi = Path(multi_raw).expanduser() if multi_raw else Path()
        if not multi_raw or not multi.is_file():
            qt_feedback.show_error(
                self,
                "无法启动 Multiwfn",
                FileNotFoundError(multi_raw or "Multiwfn.exe"),
                stage="启动 Multiwfn",
                program="Multiwfn",
                file_path=multi_raw or None,
            )
            return
        multi = multi.resolve()
        self.scan_directories = [output_dir, self.source_path.parent]
        self.before_cubes = cube_snapshot(self.scan_directories)
        self.before_intermediates = file_snapshot(
            self.scan_directories, INTERMEDIATE_SUFFIXES
        )
        env = os.environ.copy()
        env["Multiwfnpath"] = str(multi.parent)
        creation_flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        self.cancel_requested = False
        try:
            self.multiwfn_process = subprocess.Popen(
                [str(multi), str(self.source_path)],
                cwd=str(output_dir),
                env=env,
                creationflags=creation_flags,
            )
        except OSError as exc:
            self.multiwfn_process = None
            qt_feedback.show_error(
                self, "无法启动 Multiwfn", exc, stage="启动 Multiwfn", program="Multiwfn", file_path=multi
            )
            return
        self.process_timer.start()
        self.start_button.setEnabled(False)
        self.start_button.setText("等待 Multiwfn 完成…")
        self.stop_button.show()
        self._set_inputs_locked(True)
        self.manual_cube_button.hide()
        if str(self.style_data.get("surface_mode") or "signed") == "volume_mapped":
            self._set_status(
                "Multiwfn 已打开。请生成空间坐标对齐的电子密度 Cube 与 ESP Cube，然后正常输入 q 退出。"
            )
        else:
            self._set_status(
                "Multiwfn 已打开。请在其窗口中生成一个 Cube 文件，然后正常输入 q 退出。"
            )
        self._append_log(f"已启动 Multiwfn：{self.source_path.name}")

    def _changed_cubes(self) -> list[Path]:
        after = cube_snapshot(self.scan_directories)
        changed = [
            path
            for path, signature in after.items()
            if path not in self.before_cubes or self.before_cubes[path] != signature
        ]
        changed.sort(
            key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
            reverse=True,
        )
        return changed

    def _select_detected_cubes(self, candidates: list[Path]) -> list[Path] | None:
        if len(candidates) <= 1:
            return list(candidates)

        dialog = QDialog(self)
        dialog.setWindowTitle("选择要载入的 Cube")
        dialog.setMinimumWidth(680)
        layout = QVBoxLayout(dialog)
        instruction = QLabel(
            f"本次检测到 {len(candidates)} 个 Cube。请勾选一个或多个，所选文件将同时载入 VMD："
        )
        instruction.setWordWrap(True)
        layout.addWidget(instruction)

        cube_list = QListWidget(dialog)
        items: list[QListWidgetItem] = []
        for index, path in enumerate(candidates):
            item = QListWidgetItem(f"{path.name}    {path.parent}")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if index == 0 else Qt.CheckState.Unchecked
            )
            cube_list.addItem(item)
            items.append(item)
        layout.addWidget(cube_list)

        selection_row = QHBoxLayout()
        select_all = QPushButton("全选")
        clear_all = QPushButton("清除")
        selection_row.addWidget(select_all)
        selection_row.addWidget(clear_all)
        selection_row.addStretch(1)
        layout.addLayout(selection_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText("载入所选")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(buttons)

        def set_all(state: Qt.CheckState) -> None:
            for item in items:
                item.setCheckState(state)

        def refresh_confirm() -> None:
            confirm.setEnabled(
                any(item.checkState() == Qt.CheckState.Checked for item in items)
            )

        select_all.clicked.connect(lambda: set_all(Qt.CheckState.Checked))
        clear_all.clicked.connect(lambda: set_all(Qt.CheckState.Unchecked))
        cube_list.itemChanged.connect(lambda _item: refresh_confirm())
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        refresh_confirm()

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return [
            Path(str(item.data(Qt.ItemDataRole.UserRole)))
            for item in items
            if item.checkState() == Qt.CheckState.Checked
        ]

    def _handle_multiwfn_finished(self, exit_code: int) -> None:
        self.stop_button.hide()
        self.back_button.setEnabled(True)
        after_intermediates = file_snapshot(
            self.scan_directories, INTERMEDIATE_SUFFIXES
        )
        self.generated_intermediates.update(
            path for path in after_intermediates if path not in self.before_intermediates
        )
        if self.cancel_requested:
            self._set_inputs_locked(False)
            self.start_button.setEnabled(True)
            self.start_button.setText("打开 Multiwfn 并继续")
            self._set_status("本次 Multiwfn 工作流已停止，已有结果文件没有被删除。")
            self._append_log("Multiwfn 工作流已由用户停止。")
            return

        changed = self._changed_cubes()
        if not changed:
            self._set_inputs_locked(False)
            self.start_button.setEnabled(True)
            self.start_button.setText("重新打开 Multiwfn")
            self.manual_cube_button.show()
            self._set_status(
                "Multiwfn 已结束，但没有检测到新的 Cube。可以重新运行或手动选择 Cube。"
            )
            self._append_log(
                f"未检测到本次生成的 Cube 文件。Multiwfn 退出码：{exit_code}"
            )
            return

        selected = self._select_detected_cubes(changed)
        if not selected:
            self._set_inputs_locked(False)
            self.start_button.setEnabled(True)
            self.start_button.setText("重新打开 Multiwfn")
            self.manual_cube_button.show()
            self._set_status("尚未选择要绘制的 Cube。可以手动选择，或重新打开 Multiwfn。")
            return

        self.cube_paths = list(selected)
        self.cube_path = selected[0]
        self._append_log(
            "已选择 Cube：" + "、".join(path.name for path in selected)
        )
        try:
            output_dir = self._validated_output_dir()
            iso_value = self._validated_iso()
        except (OSError, ValueError) as exc:
            self._set_inputs_locked(False)
            self.start_button.setEnabled(True)
            self.start_button.setText("在 VMD 中绘图")
            qt_feedback.show_error(self, "无法继续到 VMD", exc, stage="检查 VMD 绘图设置")
            return
        self._launch_vmd(selected, iso_value, output_dir)

    def _manual_select_cube(self) -> None:
        current = self.output_dir_edit.text().strip()
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要绘制的 Cube（可多选）",
            current,
            "Cube (*.cub *.cube);;所有文件 (*)",
        )
        if not paths:
            return
        cubes = [Path(path).resolve() for path in paths]
        self.cube_paths = cubes
        self.cube_path = cubes[0]
        try:
            output_dir = self._validated_output_dir()
            iso_value = self._validated_iso()
        except (OSError, ValueError) as exc:
            qt_feedback.show_error(self, "无法继续到 VMD", exc, stage="检查 VMD 绘图设置")
            return
        self.manual_cube_button.hide()
        self._launch_vmd(cubes, iso_value, output_dir)

    @staticmethod
    def _iso_text(value: float) -> str:
        return format(value, ".12g")

    def _launch_vmd(
        self, cubes: list[Path], iso_value: float, output_dir: Path
    ) -> None:
        vmd_raw = self.vmd_path_getter().strip()
        vmd = Path(vmd_raw).expanduser() if vmd_raw else Path()
        if not vmd_raw or not vmd.is_file():
            qt_feedback.show_error(
                self,
                "无法启动 VMD",
                FileNotFoundError(vmd_raw or "vmd.exe"),
                stage="启动 VMD",
                program="VMD",
                file_path=vmd_raw or None,
            )
            self._set_inputs_locked(False)
            self.start_button.setEnabled(True)
            self.start_button.setText("在 VMD 中绘图")
            self.manual_cube_button.show()
            return
        vmd = vmd.resolve()
        selected_cubes = list(dict.fromkeys(cube.resolve() for cube in cubes))
        if not selected_cubes:
            return
        surface_mode = str(self.style_data.get("surface_mode") or "signed")
        cube_sets: list[tuple[Path, Path | None]] = []
        if surface_mode == "volume_mapped":
            seen_pairs: set[tuple[Path, Path]] = set()
            for selected_cube in selected_cubes:
                pair = core.find_esp_cube_pair(selected_cube)
                if pair is None:
                    pair = core.find_esp_cube_pair(selected_cube, selected_cubes)
                if pair is not None and pair in seen_pairs:
                    continue
                if pair is None:
                    selected_role = core.cube_semantic_role(selected_cube)
                    requested_role = "电子密度" if selected_role == "esp" else "ESP"
                    companion_raw, _ = QFileDialog.getOpenFileName(
                        self,
                        f"为 {selected_cube.name} 选择配套的{requested_role} Cube",
                        str(selected_cube.parent),
                        "Cube (*.cub *.cube);;所有文件 (*)",
                    )
                    if not companion_raw:
                        self._set_inputs_locked(False)
                        self.start_button.setEnabled(True)
                        self.start_button.setText("在 VMD 中绘图")
                        self.manual_cube_button.show()
                        self._set_status(
                            "ESP 等值面需要电子密度 Cube 和 ESP Cube；尚未选择完整文件对。"
                        )
                        return
                    companion = Path(companion_raw).resolve()
                    pair = core.find_esp_cube_pair(
                        selected_cube, [selected_cube, companion]
                    )
                    if pair is None:
                        if selected_role == "esp":
                            pair = (companion, selected_cube)
                        else:
                            pair = (selected_cube, companion)
                surface_cube, color_cube = pair
                pair_key = (surface_cube.resolve(), color_cube.resolve())
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                cube_sets.append(pair_key)
        else:
            cube_sets = [(cube, None) for cube in selected_cubes]

        for surface_cube, color_cube in cube_sets:
            if color_cube is None:
                continue
            try:
                grids_match = core.cube_grids_compatible(surface_cube, color_cube)
            except ValueError as exc:
                qt_feedback.show_error(
                    self,
                    "Cube 文件无效",
                    exc,
                    stage="检查 Cube 空间网格",
                    file_path=surface_cube,
                )
                self._set_inputs_locked(False)
                self.start_button.setEnabled(True)
                self.start_button.setText("在 VMD 中绘图")
                return
            if not grids_match:
                QMessageBox.critical(
                    self,
                    "Cube 网格不一致",
                    "电子密度 Cube 与 ESP Cube 的空间范围或网格方向不相容，不能进行可靠的表面映射。两份文件允许使用不同网格密度。",
                )
                self.start_button.setEnabled(True)
                self.start_button.setText("在 VMD 中绘图")
                self._set_inputs_locked(False)
                return

        surface_cube, color_cube = cube_sets[0]
        tcl_path = Path(tempfile.gettempdir()) / f"autocube_direct_{uuid.uuid4().hex}.tcl"
        try:
            core.write_text_atomic(
                tcl_path,
                _build_multi_cube_vmd_tcl(
                    self.style_data, self.rep0_commands, len(cube_sets)
                ),
            )
            env = os.environ.copy()
            env["CUBE_FILE_COUNT"] = str(len(cube_sets))
            for index, (surface, color) in enumerate(cube_sets, start=1):
                suffix = "" if index == 1 else f"_{index}"
                env[f"CUBE_FILE{suffix}"] = str(surface)
                if color is not None:
                    env[f"COLOR_CUBE_FILE{suffix}"] = str(color)
            env["ISO_NORM"] = self._iso_text(iso_value)
            env["A_DIR"] = str(output_dir.resolve())
            self.vmd_process = subprocess.Popen(
                [str(vmd), "-e", str(tcl_path)],
                cwd=str(output_dir),
                env=env,
            )
        except OSError as exc:
            try:
                tcl_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.vmd_process = None
            qt_feedback.show_error(
                self, "无法启动 VMD", exc, stage="启动 VMD", program="VMD", file_path=vmd
            )
            self._set_inputs_locked(False)
            self.start_button.setEnabled(True)
            self.start_button.setText("在 VMD 中绘图")
            self.manual_cube_button.show()
            return

        self.temp_tcl_path = tcl_path
        self.cube_path = surface_cube
        self.cube_paths = selected_cubes
        self.process_timer.start()
        self._set_finish_actions_visible(False)
        self.start_button.setEnabled(False)
        self.start_button.setText("VMD 正在运行…")
        self.stop_button.show()
        self._set_inputs_locked(True)
        self.open_dir_button.setEnabled(True)
        self._set_status(
            f"VMD 已启动。使用 Render 保存图片时，输出将默认进入：{output_dir}"
        )
        if len(cube_sets) > 1:
            self._append_log(
                f"已在同一个 VMD 窗口载入 {len(cube_sets)} 组 Cube："
                + "、".join(surface.name for surface, _color in cube_sets)
            )
        elif color_cube is not None:
            self._append_log(
                f"已用 {self.style_name_label.text()} 打开电子密度 {surface_cube.name}，映射 {color_cube.name}，等值面 {self._iso_text(iso_value)}。"
            )
        else:
            self._append_log(
                f"已用 {self.style_name_label.text()} 打开 {surface_cube.name}，等值面 ±{self._iso_text(iso_value)}。"
            )

    def _poll_processes(self) -> None:
        if self.multiwfn_process is not None:
            exit_code = self.multiwfn_process.poll()
            if exit_code is not None:
                self.multiwfn_process = None
                self._handle_multiwfn_finished(exit_code)

        if self.vmd_process is not None:
            exit_code = self.vmd_process.poll()
            if exit_code is not None:
                self.vmd_process = None
                self._cleanup_temp_tcl()
                self.stop_button.hide()
                self._set_inputs_locked(False)
                self.start_button.setEnabled(True)
                self.start_button.setText("重新打开 VMD")
                self._set_finish_actions_visible(True)
                if self.cancel_requested:
                    self._set_status("VMD 已停止，Cube 和已有渲染结果均已保留。")
                    self._append_log("VMD 已由用户停止。")
                elif exit_code == 0:
                    self._set_status("VMD 已关闭。渲染图片保存在所选结果目录中。")
                    self._append_log("VMD 工作流已完成。")
                else:
                    self._set_status("VMD 未正常结束，已有结果文件已保留。")
                    self._append_log(f"VMD 未正常结束。退出码：{exit_code}")
                self.cancel_requested = False

        if not self.is_running():
            self.process_timer.stop()

    def stop_workflow(self) -> None:
        process = self.multiwfn_process or self.vmd_process
        if process is None:
            return
        answer = QMessageBox.question(
            self,
            "停止工作流",
            "确定停止当前程序吗？已经产生的 Cube 和图片不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.cancel_requested = True
        try:
            process.terminate()
        except OSError:
            pass

    def _cleanup_temp_tcl(self) -> None:
        if self.temp_tcl_path is None:
            return
        try:
            self.temp_tcl_path.unlink(missing_ok=True)
        except OSError:
            pass
        self.temp_tcl_path = None

    def _open_output_dir(self) -> None:
        try:
            output_dir = self._validated_output_dir()
        except (OSError, ValueError) as exc:
            qt_feedback.show_error(self, "目录不可用", exc, stage="打开结果目录")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_dir)))

    def _set_finish_actions_visible(self, visible: bool) -> None:
        self.finish_keep_button.setVisible(visible)
        self.finish_delete_button.setVisible(visible)

    def _deletable_intermediates(self) -> list[Path]:
        source = None
        if self.source_path is not None:
            try:
                source = self.source_path.resolve()
            except OSError:
                source = self.source_path
        candidates: list[Path] = []
        for path in sorted(self.generated_intermediates, key=lambda item: str(item).lower()):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if source is not None and resolved == source:
                continue
            if resolved.suffix.lower() not in INTERMEDIATE_SUFFIXES:
                continue
            if resolved.is_file():
                candidates.append(resolved)
        return candidates

    def _finish_workflow(self, delete_intermediates: bool) -> None:
        if self.is_running():
            self._set_status("请先关闭或停止当前程序，再完成工作流。")
            return

        deleted: list[Path] = []
        failed: list[tuple[Path, OSError]] = []
        if delete_intermediates:
            candidates = self._deletable_intermediates()
            if candidates:
                preview = "\n".join(f"• {path.name}" for path in candidates[:8])
                if len(candidates) > 8:
                    preview += f"\n• 另有 {len(candidates) - 8} 个文件"
                answer = QMessageBox.question(
                    self,
                    "删除本次中间文件",
                    "将仅删除本轮新生成的 Cube/DAT 中间文件；输入文件和渲染图片会保留。\n\n"
                    f"待删除：\n{preview}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                for path in candidates:
                    try:
                        path.unlink()
                        deleted.append(path)
                    except OSError as exc:
                        failed.append((path, exc))

        if failed:
            for path, error in failed:
                self._append_log(f"中间文件未能删除：{path}（{error}）")
            qt_feedback.show_error(
                self,
                "部分中间文件未能删除",
                failed[0][1],
                stage=f"清理中间文件（已删除 {len(deleted)} 个，失败 {len(failed)} 个）",
                file_path=failed[0][0],
            )
        elif delete_intermediates:
            self._append_log(f"工作流已完成，已删除 {len(deleted)} 个本次中间文件。")
        else:
            self._append_log("工作流已完成，所有中间文件均已保留。")

        self._reset_session()
        self.backRequested.emit()

    def _reset_session(self) -> None:
        self.source_path = None
        self.cube_path = None
        self.cube_paths.clear()
        self.before_cubes.clear()
        self.before_intermediates.clear()
        self.generated_intermediates.clear()
        self.scan_directories.clear()
        self.cancel_requested = False
        self.file_info_frame.hide()
        self.file_info_label.clear()
        self.output_dir_edit.clear()
        self.iso_edit.clear()
        self.manual_cube_button.hide()
        self.stop_button.hide()
        self._set_finish_actions_visible(False)
        self.open_dir_button.setEnabled(False)
        self.back_button.setEnabled(True)
        self._set_inputs_locked(False)
        self.start_button.setEnabled(False)
        self.start_button.setText("开始直接绘图")
        self._set_status("请选择或拖入一个文件。")
        self.session_log.clear()
        self.session_log.hide()

    def _request_back(self) -> None:
        if self.is_running():
            self._set_status("请先停止当前工作流，再返回绘图方案。")
            return
        self.backRequested.emit()

    def _set_inputs_locked(self, locked: bool) -> None:
        """运行期间只锁定会改变本轮结果的输入，状态和停止操作仍可用。"""
        hint = "当前任务运行中，停止或完成后可以修改。" if locked else ""
        for widget in (self.change_style_button, self.source_card, self.settings_card):
            widget.setEnabled(not locked)
            widget.setToolTip(hint)
        self.back_button.setEnabled(not locked)
        self.back_button.setToolTip(hint)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _append_log(self, text: str) -> None:
        self.session_log.show()
        self.session_log.appendPlainText(text)

    def is_running(self) -> bool:
        return self.multiwfn_process is not None or self.vmd_process is not None

    def cleanup(self) -> None:
        if not self.is_running():
            self._cleanup_temp_tcl()
