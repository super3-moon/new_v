from __future__ import annotations

import codecs
import locale
import os
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QCloseEvent, QFont, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


class MultiwfnRecorderDialog(QDialog):
    """Run one interactive Multiwfn session and retain every submitted command."""

    def __init__(
        self,
        multiwfn_exe: Path | str,
        input_file: Path | str,
        parent: QWidget | None = None,
        *,
        auto_start: bool = True,
    ) -> None:
        super().__init__(parent)
        self.multiwfn_exe = Path(multiwfn_exe).expanduser().resolve()
        self.input_file = Path(input_file).expanduser().resolve()
        self.recorded_commands: list[str] = []
        self.generated_files: list[Path] = []
        self._before_files = self._snapshot_files()
        self._accepting = False
        self._last_exit_code: int | None = None
        self._encoding = locale.getpreferredencoding(False) or "utf-8"
        self._decoder = codecs.getincrementaldecoder(self._encoding)(errors="replace")

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.started.connect(self._on_started)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

        self._build_ui()
        if auto_start:
            self.start()

    @property
    def sequence_text(self) -> str:
        if not self.recorded_commands:
            return ""
        return "\n".join(self.recorded_commands) + "\n"

    def _build_ui(self) -> None:
        self.setWindowTitle("操作一次并记录 Multiwfn 流程")
        self.resize(1040, 700)
        self.setMinimumSize(820, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("完成一次 Multiwfn 操作")
        title.setObjectName("recorderTitle")
        title.setStyleSheet("font-size: 18px; font-weight: 800;")
        root.addWidget(title)
        intro = QLabel(
            "根据 Multiwfn 输出的菜单，在底部输入框逐次发送选项。每次发送都会被自动记录；"
            "仅在输入框为空时按 Enter，或点击“空行（回车）”，才会记录空命令。"
            "请像正常使用 Multiwfn 一样完成全部操作并正常退出。"
        )
        intro.setObjectName("recorderHint")
        intro.setWordWrap(True)
        root.addWidget(intro)

        file_bar = QFrame()
        file_bar.setObjectName("recorderFileBar")
        file_layout = QHBoxLayout(file_bar)
        file_layout.setContentsMargins(12, 9, 12, 9)
        file_layout.addWidget(QLabel("示例文件"))
        file_name = QLabel(str(self.input_file))
        file_name.setObjectName("recorderFileName")
        file_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        file_layout.addWidget(file_name, 1)
        self.state_label = QLabel("准备启动")
        self.state_label.setObjectName("recorderState")
        file_layout.addWidget(self.state_label)
        root.addWidget(file_bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        terminal_frame = QFrame()
        terminal_layout = QVBoxLayout(terminal_frame)
        terminal_layout.setContentsMargins(0, 0, 6, 0)
        terminal_layout.addWidget(QLabel("Multiwfn 输出"))
        self.output_edit = QPlainTextEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setMaximumBlockCount(12000)
        terminal_font = QFont("Consolas")
        terminal_font.setStyleHint(QFont.Monospace)
        self.output_edit.setFont(terminal_font)
        self.output_edit.setStyleSheet(
            "QPlainTextEdit { background: #10283b; color: #d9e8f3; border: 1px solid #23455e; "
            "border-radius: 10px; padding: 9px; selection-background-color: #2e6f9e; }"
        )
        terminal_layout.addWidget(self.output_edit, 1)
        splitter.addWidget(terminal_frame)

        record_frame = QFrame()
        record_layout = QVBoxLayout(record_frame)
        record_layout.setContentsMargins(6, 0, 0, 0)
        record_header = QHBoxLayout()
        record_header.addWidget(QLabel("已记录的操作步骤"))
        self.count_label = QLabel("0 步")
        self.count_label.setStyleSheet("color: #60778e;")
        record_header.addStretch(1)
        record_header.addWidget(self.count_label)
        record_layout.addLayout(record_header)
        self.command_list = QListWidget()
        self.command_list.setAlternatingRowColors(True)
        record_layout.addWidget(self.command_list, 1)
        record_note = QLabel("如果中途输错，请使用底部“重新开始试录”，保证记录与真实菜单位置一致。")
        record_note.setObjectName("recorderHint")
        record_note.setWordWrap(True)
        record_layout.addWidget(record_note)
        splitter.addWidget(record_frame)
        splitter.setSizes([670, 330])
        root.addWidget(splitter, 1)

        quick_row = QHBoxLayout()
        quick_row.addWidget(QLabel("快捷发送"))
        blank_button = QPushButton("空行（回车）")
        zero_button = QPushButton("0 · 返回")
        quit_button = QPushButton("q · 返回 / 退出")
        blank_button.clicked.connect(lambda: self._submit_command(""))
        zero_button.clicked.connect(lambda: self._submit_command("0"))
        quit_button.clicked.connect(lambda: self._submit_command("q"))
        quick_row.addWidget(blank_button)
        quick_row.addWidget(zero_button)
        quick_row.addWidget(quit_button)
        quick_row.addStretch(1)
        root.addLayout(quick_row)

        input_row = QHBoxLayout()
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText("输入当前菜单选项、数值或文件名，然后按 Enter")
        self.command_edit.returnPressed.connect(self._submit_current_command)
        self.send_button = QPushButton("发送并记录")
        self.send_button.setObjectName("primaryBtn")
        self.send_button.clicked.connect(self._submit_current_command)
        input_row.addWidget(self.command_edit, 1)
        input_row.addWidget(self.send_button)
        root.addLayout(input_row)

        footer = QHBoxLayout()
        self.restart_button = QPushButton("重新开始试录")
        self.restart_button.clicked.connect(self._restart)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        self.use_button = QPushButton("采用这条操作记录")
        self.use_button.setObjectName("generateBtn")
        self.use_button.setEnabled(False)
        self.use_button.clicked.connect(self._accept_recording)
        footer.addWidget(self.restart_button)
        footer.addStretch(1)
        footer.addWidget(cancel_button)
        footer.addWidget(self.use_button)
        self.completion_hint = QLabel(
            "完成所有计算和文件导出后，请返回主菜单并输入 q；Multiwfn 正常退出后才能采用记录。"
        )
        self.completion_hint.setObjectName("recorderHint")
        self.completion_hint.setWordWrap(True)
        root.addWidget(self.completion_hint)
        root.addLayout(footer)

        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    def _snapshot_files(self) -> dict[Path, tuple[int, int]]:
        result: dict[Path, tuple[int, int]] = {}
        folder = self.input_file.parent
        if not folder.is_dir():
            return result
        for path in folder.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            result[path.resolve()] = (stat.st_size, stat.st_mtime_ns)
        return result

    def _detect_generated_files(self) -> None:
        after = self._snapshot_files()
        changed = []
        for path, signature in after.items():
            if path == self.input_file:
                continue
            if self._before_files.get(path) != signature:
                changed.append(path)
        self.generated_files = sorted(changed, key=lambda item: item.name.casefold())

    def start(self) -> None:
        if not self.multiwfn_exe.is_file() or not self.input_file.is_file():
            self.state_label.setText("路径无效")
            self.output_edit.appendPlainText("无法启动：Multiwfn 或示例文件路径无效。")
            return
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("Multiwfnpath", str(self.multiwfn_exe.parent))
        environment.insert("GFORTRAN_UNBUFFERED_PRECONNECTED", "1")
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(self.input_file.parent))
        self.process.setProgram(str(self.multiwfn_exe))
        self.process.setArguments([str(self.input_file)])
        self.state_label.setText("正在启动")
        self._last_exit_code = None
        self.use_button.setEnabled(False)
        self.command_edit.setEnabled(False)
        self.send_button.setEnabled(False)
        self.process.start()

    def _on_started(self) -> None:
        self.state_label.setText("录制中")
        self.command_edit.setEnabled(True)
        self.send_button.setEnabled(True)
        self.command_edit.setFocus()

    def _read_output(self) -> None:
        raw = bytes(self.process.readAllStandardOutput())
        if not raw:
            return
        text = self._decoder.decode(raw)
        self._append_output_text(text)

    def _append_output_text(self, text: str) -> None:
        if not text:
            return
        cursor = self.output_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self.output_edit.setTextCursor(cursor)
        self.output_edit.ensureCursorVisible()

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._read_output()
        self._append_output_text(self._decoder.decode(b"", final=True))
        self._last_exit_code = int(exit_code)
        self.command_edit.setEnabled(False)
        self.send_button.setEnabled(False)
        self._detect_generated_files()
        if exit_code == 0 and self.recorded_commands:
            self.state_label.setText("试录完成")
            self.completion_hint.setText(
                f"Multiwfn 已正常退出；记录了 {len(self.recorded_commands)} 步，"
                f"识别到 {len(self.generated_files)} 个生成文件。现在可以采用记录。"
            )
            self.use_button.setEnabled(True)
        else:
            self.state_label.setText("异常结束")
            self.completion_hint.setText(
                "这次记录没有正常结束，通常是操作流程尚未完成或菜单位置不正确。"
                "请检查左侧最后提示后重新试录；异常记录不会被采用。"
            )
            self.output_edit.appendPlainText(f"\n[诊断] Multiwfn 退出码：{exit_code}")
            self.use_button.setEnabled(False)

    def _on_error(self, _error: QProcess.ProcessError) -> None:
        if self.process.errorString():
            self.output_edit.appendPlainText("\n[启动提示] " + self.process.errorString())

    def _submit_current_command(self) -> None:
        value = self.command_edit.text()
        self.command_edit.clear()
        self._submit_command(value)

    def _submit_command(self, value: str) -> None:
        if self.process.state() != QProcess.Running:
            return
        command = str(value)
        self.recorded_commands.append(command)
        shown = command if command else "↵ 空行"
        self.command_list.addItem(f"{len(self.recorded_commands):02d}   {shown}")
        self.command_list.scrollToBottom()
        self.count_label.setText(f"{len(self.recorded_commands)} 步")
        self.process.write((command + "\n").encode(self._encoding, errors="replace"))

    def _clear_commands(self) -> None:
        self.recorded_commands.clear()
        self.command_list.clear()
        self.count_label.setText("0 步")
        self.use_button.setEnabled(False)

    def _stop_process(self) -> None:
        if self.process.state() == QProcess.NotRunning:
            return
        self.process.terminate()
        if not self.process.waitForFinished(1200):
            self.process.kill()
            self.process.waitForFinished(800)

    def _restart(self) -> None:
        self._stop_process()
        self._clear_commands()
        self.output_edit.clear()
        self.generated_files = []
        self._last_exit_code = None
        self._before_files = self._snapshot_files()
        self._decoder = codecs.getincrementaldecoder(self._encoding)(errors="replace")
        self.completion_hint.setText(
            "完成所有计算和文件导出后，请返回主菜单并输入 q；Multiwfn 正常退出后才能采用记录。"
        )
        self.start()

    def _accept_recording(self) -> None:
        if not self.recorded_commands:
            return
        if self.process.state() == QProcess.Running:
            QMessageBox.information(
                self,
                "操作尚未完成",
                "Multiwfn 仍在等待输入。请完成后续菜单操作，返回主菜单并输入 q 正常退出；"
                "否则批量回放会在输入流结束时报错。",
            )
            return
        if self._last_exit_code != 0:
            QMessageBox.warning(
                self,
                "记录不完整",
                "这次操作没有正常完成，不能用于批处理。请重新试录。",
            )
            return
        self._accepting = True
        self._detect_generated_files()
        self.accept()

    def reject(self) -> None:
        self._stop_process()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._accepting or self.process.state() == QProcess.NotRunning:
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "结束试录",
            "Multiwfn 仍在运行，确定结束本次试录吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._stop_process()
            event.accept()
        else:
            event.ignore()
