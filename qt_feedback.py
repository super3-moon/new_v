"""Small non-blocking notifications and consistent expandable error dialogs."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

import user_feedback


def show_toast(parent: QWidget, text: str, *, timeout_ms: int = 4000) -> QLabel:
    host = parent.window()
    toast = QLabel(str(text), host)
    toast.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    toast.setWordWrap(True)
    toast.setMaximumWidth(520)
    toast.setStyleSheet(
        "QLabel { background: #17324f; color: white; border: 1px solid #31577b; "
        "border-radius: 10px; padding: 10px 14px; font-weight: 600; }"
    )
    toast.adjustSize()
    width = min(max(220, toast.sizeHint().width()), max(220, host.width() - 40))
    toast.resize(width, toast.sizeHint().height())
    toast.move(max(20, host.width() - width - 24), max(20, host.height() - toast.height() - 24))
    toast.show()
    toast.raise_()
    QTimer.singleShot(max(1000, int(timeout_ms)), toast.close)
    return toast


def show_error(
    parent: QWidget,
    title: str,
    error: object,
    *,
    stage: str = "",
    program: str = "",
    file_path: object = None,
    log_path: object = None,
) -> None:
    info = user_feedback.describe_error(
        error,
        title=title,
        stage=stage,
        program=program,
        file_path=file_path,
        log_path=log_path,
    )
    box = QMessageBox(parent)
    box.setWindowTitle(info.title)
    box.setIcon(
        QMessageBox.Icon.Warning
        if info.severity == "warning"
        else QMessageBox.Icon.Critical
    )
    box.setText(info.reason)
    box.setInformativeText(info.action)
    box.setDetailedText(info.details)
    box.exec()


__all__ = ["show_error", "show_toast"]
