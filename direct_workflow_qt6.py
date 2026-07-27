from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable

import vmd_style_tool as core
from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDoubleValidator
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
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
        subtitle = QLabel("或点击此区域选择文件 · Cube 可直接绘图，其他文件将交给 Multiwfn")
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
        change_style = QPushButton("更换风格")
        change_style.clicked.connect(self._request_back)
        style_row.addWidget(change_style)
        style_layout.addLayout(style_row)
        body_layout.addWidget(style_card)

        source_card, source_layout = self._card("添加输入文件")
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
        iso_hint = QLabel("请按分析需要填写正数，负等值面会自动使用相反数。")
        iso_hint.setObjectName("helperText")
        settings_layout.addWidget(iso_hint)
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
        self.manual_cube_button = QPushButton("手动选择 Cube 并继续")
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
        pos = str(style.get("pos_color_expr") or f"ColorID {style.get('pos_color', 1)}")
        neg = str(style.get("neg_color_expr") or f"ColorID {style.get('neg_color', 0)}")
        self.style_meta_label.setText(
            f"{selection_text} · 材质 {material} · 正等值面 {pos} · 负等值面 {neg}"
        )
        self._append_log(f"已选择绘图风格：{self.style_name_label.text()}")

    def set_source_file(self, raw_path: str) -> None:
        if self.is_running():
            QMessageBox.information(self, "工作流运行中", "请先停止当前工作流，再更换文件。")
            return
        path = Path(raw_path).expanduser()
        if not path.is_file():
            QMessageBox.warning(self, "文件不可用", "请选择一个存在的本地文件。")
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
        QMessageBox.information(self, "无法添加文件", message)

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
            QMessageBox.warning(self, "尚未选择风格", "请返回绘图方案选择绘图风格。")
            return
        if self.source_path is None or not self.source_path.is_file():
            QMessageBox.warning(self, "尚未添加文件", "请先选择或拖入一个文件。")
            return
        try:
            output_dir = self._validated_output_dir()
            iso_value = self._validated_iso()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "运行设置不完整", str(exc))
            return

        if self.cube_path is not None and self.cube_path.is_file():
            self._launch_vmd(self.cube_path, iso_value, output_dir)
        elif is_cube_file(self.source_path):
            self.cube_path = self.source_path
            self._launch_vmd(self.source_path, iso_value, output_dir)
        else:
            self._launch_multiwfn(output_dir)

    def _launch_multiwfn(self, output_dir: Path) -> None:
        assert self.source_path is not None
        multi_raw = self.multiwfn_path_getter().strip()
        multi = Path(multi_raw).expanduser() if multi_raw else Path()
        if not multi_raw or not multi.is_file():
            QMessageBox.critical(self, "无法启动 Multiwfn", "请先在左侧设置有效的 Multiwfn.exe 路径。")
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
            QMessageBox.critical(self, "无法启动 Multiwfn", str(exc))
            return
        self.process_timer.start()
        self.start_button.setEnabled(False)
        self.start_button.setText("等待 Multiwfn 完成…")
        self.stop_button.show()
        self.back_button.setEnabled(False)
        self.manual_cube_button.hide()
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
            self.start_button.setEnabled(True)
            self.start_button.setText("打开 Multiwfn 并继续")
            self._set_status("本次 Multiwfn 工作流已停止，已有结果文件没有被删除。")
            self._append_log("Multiwfn 工作流已由用户停止。")
            return

        changed = self._changed_cubes()
        if not changed:
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

        selected = changed[0]
        if len(changed) > 1:
            choices = [f"{path.name}（{path.parent}）" for path in changed]
            choice, accepted = QInputDialog.getItem(
                self,
                "选择要绘制的 Cube",
                f"本次检测到 {len(changed)} 个 Cube，请选择一个：",
                choices,
                0,
                False,
            )
            if not accepted:
                self.start_button.setEnabled(True)
                self.start_button.setText("重新打开 Multiwfn")
                self.manual_cube_button.show()
                self._set_status("尚未选择要绘制的 Cube。可以手动选择，或重新打开 Multiwfn。")
                return
            selected = changed[choices.index(choice)]

        self.cube_path = selected
        self._append_log(f"已检测到 Cube：{selected}")
        try:
            output_dir = self._validated_output_dir()
            iso_value = self._validated_iso()
        except (OSError, ValueError) as exc:
            self.start_button.setEnabled(True)
            self.start_button.setText("在 VMD 中绘图")
            QMessageBox.warning(self, "无法继续到 VMD", str(exc))
            return
        self._launch_vmd(selected, iso_value, output_dir)

    def _manual_select_cube(self) -> None:
        current = self.output_dir_edit.text().strip()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要绘制的 Cube", current, "Cube (*.cub *.cube);;所有文件 (*)"
        )
        if not path:
            return
        cube = Path(path).resolve()
        self.cube_path = cube
        try:
            output_dir = self._validated_output_dir()
            iso_value = self._validated_iso()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法继续到 VMD", str(exc))
            return
        self.manual_cube_button.hide()
        self._launch_vmd(cube, iso_value, output_dir)

    @staticmethod
    def _iso_text(value: float) -> str:
        return format(value, ".12g")

    def _launch_vmd(self, cube: Path, iso_value: float, output_dir: Path) -> None:
        vmd_raw = self.vmd_path_getter().strip()
        vmd = Path(vmd_raw).expanduser() if vmd_raw else Path()
        if not vmd_raw or not vmd.is_file():
            QMessageBox.critical(self, "无法启动 VMD", "请先在左侧设置有效的 vmd.exe 路径。")
            self.start_button.setEnabled(True)
            self.start_button.setText("在 VMD 中绘图")
            self.manual_cube_button.show()
            return
        vmd = vmd.resolve()
        tcl_path = Path(tempfile.gettempdir()) / f"autocube_direct_{uuid.uuid4().hex}.tcl"
        try:
            core.write_text_atomic(
                tcl_path,
                core.build_vmd_tcl(self.style_data, rep0_commands=self.rep0_commands),
            )
            env = os.environ.copy()
            env["CUBE_FILE"] = str(cube.resolve())
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
            QMessageBox.critical(self, "无法启动 VMD", str(exc))
            self.start_button.setEnabled(True)
            self.start_button.setText("在 VMD 中绘图")
            self.manual_cube_button.show()
            return

        self.temp_tcl_path = tcl_path
        self.cube_path = cube
        self.process_timer.start()
        self._set_finish_actions_visible(False)
        self.start_button.setEnabled(False)
        self.start_button.setText("VMD 正在运行…")
        self.stop_button.show()
        self.back_button.setEnabled(False)
        self.open_dir_button.setEnabled(True)
        self._set_status(
            f"VMD 已启动。使用 Render 保存图片时，输出将默认进入：{output_dir}"
        )
        self._append_log(
            f"已用 {self.style_name_label.text()} 打开 {cube.name}，等值面 ±{self._iso_text(iso_value)}。"
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
                self.back_button.setEnabled(True)
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
            QMessageBox.warning(self, "目录不可用", str(exc))
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
            QMessageBox.information(self, "工作流运行中", "请先关闭或停止当前程序，再完成工作流。")
            return

        deleted: list[Path] = []
        failed: list[tuple[Path, str]] = []
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
                        failed.append((path, str(exc)))

        if failed:
            details = "\n".join(f"{path.name}：{reason}" for path, reason in failed[:6])
            QMessageBox.warning(
                self,
                "部分中间文件未能删除",
                f"已删除 {len(deleted)} 个文件，另有 {len(failed)} 个文件删除失败：\n{details}",
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
        self.start_button.setEnabled(False)
        self.start_button.setText("开始直接绘图")
        self._set_status("请选择或拖入一个文件。")
        self.session_log.clear()

    def _request_back(self) -> None:
        if self.is_running():
            QMessageBox.information(self, "工作流运行中", "请先停止当前工作流，再返回绘图方案。")
            return
        self.backRequested.emit()

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _append_log(self, text: str) -> None:
        self.session_log.appendPlainText(text)

    def is_running(self) -> bool:
        return self.multiwfn_process is not None or self.vmd_process is not None

    def cleanup(self) -> None:
        if not self.is_running():
            self._cleanup_temp_tcl()
