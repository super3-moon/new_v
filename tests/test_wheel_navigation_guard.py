from __future__ import annotations

import os
import unittest
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QScrollArea,
    QSlider,
    QSpinBox,
    QWidget,
)

from vmd_style_tool_qt6 import (
    WheelNavigationGuard,
    install_wheel_navigation_guard,
)


class WheelNavigationGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.guard = install_wheel_navigation_guard(cls.app)

    @staticmethod
    def _wheel_event(widget: QWidget, angle_y: int) -> QWheelEvent:
        local = QPointF(widget.rect().center())
        global_position = QPointF(widget.mapToGlobal(widget.rect().center()))
        return QWheelEvent(
            local,
            global_position,
            QPoint(),
            QPoint(0, angle_y),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )

    def _scroll_page(self, control: QWidget) -> tuple[QScrollArea, QWidget]:
        page = QScrollArea()
        page.resize(280, 180)
        body = QWidget()
        body.setMinimumSize(250, 800)
        control.setParent(body)
        control.setGeometry(30, 280, 160, 32)
        page.setWidget(body)
        page.show()
        self.app.processEvents()
        page.verticalScrollBar().setValue(100)
        return page, body

    def test_guard_is_installed_once_for_the_whole_application(self) -> None:
        again = install_wheel_navigation_guard(self.app)
        self.assertIs(again, self.guard)
        self.assertIsInstance(again, WheelNavigationGuard)

    def test_closed_combo_keeps_value_and_scrolls_outer_page(self) -> None:
        combo = QComboBox()
        combo.addItems(["A", "B", "C"])
        combo.setCurrentIndex(1)
        page, _body = self._scroll_page(combo)
        try:
            before_scroll = page.verticalScrollBar().value()
            QApplication.sendEvent(combo, self._wheel_event(combo, -120))
            self.app.processEvents()

            self.assertEqual(combo.currentIndex(), 1)
            self.assertGreater(page.verticalScrollBar().value(), before_scroll)
        finally:
            page.close()

    def test_spin_editor_and_slider_are_protected_too(self) -> None:
        controls: list[tuple[QWidget, QWidget, Callable[[], int], int]] = []

        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(50)
        controls.append((spin, spin.lineEdit(), spin.value, 50))

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(50)
        controls.append((slider, slider, slider.value, 50))

        for root_control, watched, value_getter, expected in controls:
            with self.subTest(control=type(watched.parentWidget() or watched).__name__):
                page, _body = self._scroll_page(root_control)
                try:
                    before_scroll = page.verticalScrollBar().value()
                    QApplication.sendEvent(
                        watched, self._wheel_event(watched, -120)
                    )
                    self.app.processEvents()
                    self.assertEqual(value_getter(), expected)
                    self.assertGreater(
                        page.verticalScrollBar().value(), before_scroll
                    )
                finally:
                    page.close()

    def test_open_combo_popup_is_left_to_qt_for_option_scrolling(self) -> None:
        combo = QComboBox()
        combo.addItems([str(index) for index in range(20)])
        combo.show()
        combo.showPopup()
        self.app.processEvents()
        try:
            self.assertTrue(combo.view().isVisible())
            handled = self.guard.eventFilter(
                combo, self._wheel_event(combo, -120)
            )
            self.assertFalse(handled)
        finally:
            combo.hidePopup()
            combo.close()

    def test_control_outside_scroll_page_never_changes_by_hover_wheel(self) -> None:
        combo = QComboBox()
        combo.addItems(["A", "B", "C"])
        combo.setCurrentIndex(1)
        QApplication.sendEvent(combo, self._wheel_event(combo, -120))
        self.app.processEvents()
        self.assertEqual(combo.currentIndex(), 1)


if __name__ == "__main__":
    unittest.main()
