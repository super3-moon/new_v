from __future__ import annotations

import colorsys
import re
import shutil
import sys
import tempfile
import zlib
from pathlib import Path

import vmd_style_tool as core
from automatic_workflows_qt6 import AutomaticWorkflowsPage
from direct_workflow_qt6 import DirectWorkflowPage
from multiwfn_batch_qt6 import MultiwfnBatchPage
from style_parameter_dialog_qt6 import StyleParameterDialog
from PySide6.QtCore import (
    QCoreApplication,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QThread,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

CARD_W = 256
CARD_IMG_W = 232
CARD_IMG_H = 144
CARD_GAP = 12
SPLIT_CARD_W = 204
SPLIT_CARD_H = 232
SPLIT_CARD_IMG_W = 180
SPLIT_CARD_IMG_H = 112
SPLIT_CARD_GAP = 10
WINDOW_MIN_W = 960
WINDOW_MIN_H = 620
WINDOW_MAX_W = 1360
WINDOW_MAX_H = 800
WINDOW_WIDTH_RATIO = 0.86
WINDOW_HEIGHT_RATIO = 0.88


def preferred_window_size(available_width: int, available_height: int) -> tuple[int, int]:
    width = max(
        WINDOW_MIN_W,
        min(WINDOW_MAX_W, int(max(0, available_width) * WINDOW_WIDTH_RATIO)),
    )
    height = max(
        WINDOW_MIN_H,
        min(WINDOW_MAX_H, int(max(0, available_height) * WINDOW_HEIGHT_RATIO)),
    )
    return width, height


class WheelNavigationGuard(QObject):
    """Keep wheel gestures for page navigation outside an open drop-down.

    Qt changes a combo box, spin box, or slider as soon as the pointer happens to
    be above it. That is especially easy to trigger in this application's long
    configuration pages. This application-wide filter forwards such gestures
    to the nearest useful scroll area instead. A combo box's open popup remains
    untouched so its options can still be scrolled normally.
    """

    _PROTECTED_WIDGETS = (QComboBox, QAbstractSpinBox, QSlider)

    @classmethod
    def _protected_widget(cls, watched: QObject) -> QWidget | None:
        widget = watched if isinstance(watched, QWidget) else None
        while widget is not None:
            if isinstance(widget, cls._PROTECTED_WIDGETS):
                return widget
            widget = widget.parentWidget()
        return None

    @staticmethod
    def _can_scroll(scroll_area: QAbstractScrollArea, event: QWheelEvent) -> bool:
        delta = event.pixelDelta()
        if delta.isNull():
            delta = event.angleDelta()

        vertical = scroll_area.verticalScrollBar()
        if delta.y() > 0 and vertical.value() > vertical.minimum():
            return True
        if delta.y() < 0 and vertical.value() < vertical.maximum():
            return True

        horizontal = scroll_area.horizontalScrollBar()
        if delta.x() > 0 and horizontal.value() > horizontal.minimum():
            return True
        if delta.x() < 0 and horizontal.value() < horizontal.maximum():
            return True
        return False

    @classmethod
    def _scroll_area_for(
        cls, control: QWidget, event: QWheelEvent
    ) -> QAbstractScrollArea | None:
        candidates: list[QAbstractScrollArea] = []
        parent = control.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                candidates.append(parent)
                if cls._can_scroll(parent, event):
                    return parent
            parent = parent.parentWidget()
        return candidates[0] if candidates else None

    @staticmethod
    def _forward_wheel(
        scroll_area: QAbstractScrollArea, event: QWheelEvent
    ) -> None:
        viewport = scroll_area.viewport()
        local_position = QPointF(
            viewport.mapFromGlobal(event.globalPosition().toPoint())
        )
        forwarded = QWheelEvent(
            local_position,
            event.globalPosition(),
            event.pixelDelta(),
            event.angleDelta(),
            event.buttons(),
            event.modifiers(),
            event.phase(),
            event.inverted(),
            event.source(),
            event.pointingDevice(),
        )
        QCoreApplication.sendEvent(viewport, forwarded)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel or not isinstance(event, QWheelEvent):
            return super().eventFilter(watched, event)

        control = self._protected_widget(watched)
        if control is None or not control.isEnabled():
            return super().eventFilter(watched, event)

        if isinstance(control, QComboBox) and control.view().isVisible():
            return super().eventFilter(watched, event)

        scroll_area = self._scroll_area_for(control, event)
        if scroll_area is not None:
            self._forward_wheel(scroll_area, event)
        return True


def install_wheel_navigation_guard(
    app: QApplication | None = None,
) -> WheelNavigationGuard:
    """Install one application-wide wheel guard and retain its Python lifetime."""

    qt_app = app or QApplication.instance()
    if not isinstance(qt_app, QApplication):
        raise RuntimeError("QApplication must exist before installing the wheel guard")

    guard = getattr(qt_app, "_wheel_navigation_guard", None)
    if not isinstance(guard, WheelNavigationGuard):
        guard = WheelNavigationGuard(qt_app)
        qt_app.installEventFilter(guard)
        setattr(qt_app, "_wheel_navigation_guard", guard)
    return guard


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def prepare_runtime_files() -> None:
    base = app_dir()
    bundle_root = Path(getattr(sys, "_MEIPASS", base)).resolve()

    src_style = bundle_root / "vmd_cube_styles"
    dst_style = base / "vmd_cube_styles"
    dst_style.mkdir(parents=True, exist_ok=True)
    if src_style.exists():
        for item in src_style.iterdir():
            dst = dst_style / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            elif not dst.exists():
                shutil.copy2(item, dst)

    src_custom = bundle_root / "vmd_custom_styles.default.json"
    dst_custom = base / "vmd_custom_styles.json"
    if src_custom.exists() and not dst_custom.exists():
        shutil.copy2(src_custom, dst_custom)

    core.ROOT = base
    core.STYLE_DIR = dst_style
    core.CONFIG_FILE = base / "vmd_style_tool_config.json"
    core.CUSTOM_STYLES_FILE = dst_custom


def clean_name_for_file(name: str) -> str:
    t = (name or "").strip()
    t = re.sub(r"[<>:\"/\\|?*]+", "_", t)
    t = re.sub(r"\s+", "_", t)
    t = re.sub(r"_+", "_", t).strip("_")
    return t or "Style"


def rgb_to_css(rgb: tuple[float, float, float]) -> str:
    vals = [max(0, min(255, int(round(v * 255)))) for v in rgb]
    return f"rgb({vals[0]}, {vals[1]}, {vals[2]})"


def rgb_to_text(rgb: tuple[float, float, float]) -> str:
    return f"{rgb[0]:.3f}, {rgb[1]:.3f}, {rgb[2]:.3f}"


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def png_data_stream_is_valid(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    idat_parts: list[bytes] = []
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return False
        if chunk_type == b"IDAT":
            idat_parts.append(data[offset + 8 : offset + 8 + length])
        offset = chunk_end
        if chunk_type == b"IEND":
            break
    if not idat_parts:
        return False
    try:
        zlib.decompress(b"".join(idat_parts))
    except zlib.error:
        return False
    return True


class AiRecognitionWorker(QObject):
    finished = Signal(object, object)

    def __init__(
        self,
        image_path: Path,
        api_key: str,
        model: str,
        provider: str,
        image_context: str,
    ) -> None:
        super().__init__()
        self.image_path = image_path
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.image_context = image_context

    @Slot()
    def run(self) -> None:
        try:
            result = core.recognize_ai_style_from_image(
                self.image_path,
                api_key=self.api_key,
                model=self.model,
                provider=self.provider,
                image_context=self.image_context,
            )
        except Exception as exc:
            self.finished.emit(None, str(exc))
            return
        self.finished.emit(result, None)


class CropImageLabel(QLabel):
    cropChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._source_pixmap = QPixmap()
        self._selection = QRect()
        self._drag_start: QPoint | None = None
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    def load_image(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            raise ValueError("无法读取该图片。")
        self._source_pixmap = pixmap
        self.setMinimumHeight(240)
        self._selection = QRect()
        self._drag_start = None
        self.update()
        self.cropChanged.emit()

    def reset_crop(self) -> None:
        self._selection = QRect()
        self._drag_start = None
        self.update()
        self.cropChanged.emit()

    def has_crop(self) -> bool:
        return not self._selection.normalized().isNull()

    def _image_rect(self) -> QRect:
        if self._source_pixmap.isNull():
            return QRect()
        scaled = self._source_pixmap.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRect(
            (self.width() - scaled.width()) // 2,
            (self.height() - scaled.height()) // 2,
            scaled.width(),
            scaled.height(),
        )

    def _clamp_point(self, point: QPoint) -> QPoint:
        rect = self._image_rect()
        return QPoint(
            max(rect.left(), min(point.x(), rect.right())),
            max(rect.top(), min(point.y(), rect.bottom())),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor("#eef3f8"))
        if self._source_pixmap.isNull():
            painter.setPen(QColor("#718399"))
            painter.drawText(
                self.rect().adjusted(20, 20, -20, -20),
                Qt.AlignCenter | Qt.TextWordWrap,
                "选择图片后，可在这里拖动框选需要识别的区域",
            )
            painter.end()
            return

        rect = self._image_rect()
        painter.drawPixmap(rect, self._source_pixmap)
        selection = self._selection.normalized()
        if not selection.isNull():
            painter.fillRect(selection, QColor(31, 111, 235, 45))
            pen = QPen(QColor("#1f6feb"))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(selection.adjusted(1, 1, -1, -1))
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self._source_pixmap.isNull():
            return
        rect = self._image_rect()
        pos = event.position().toPoint()
        if not rect.contains(pos):
            return
        self._drag_start = self._clamp_point(pos)
        self._selection = QRect(self._drag_start, self._drag_start)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start is None:
            return
        self._selection = QRect(self._drag_start, self._clamp_point(event.position().toPoint()))
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self._drag_start is None:
            return
        self._selection = QRect(self._drag_start, self._clamp_point(event.position().toPoint()))
        self._drag_start = None
        if self._selection.normalized().width() < 8 or self._selection.normalized().height() < 8:
            self._selection = QRect()
        self.update()
        self.cropChanged.emit()

    def save_effective_image(self, target: Path) -> Path:
        if self._source_pixmap.isNull():
            raise ValueError("请先选择图片。")
        source_rect = self._source_pixmap.rect()
        selection = self._selection.normalized()
        if selection.isNull():
            cropped = self._source_pixmap
        else:
            display = self._image_rect()
            sx = self._source_pixmap.width() / max(1, display.width())
            sy = self._source_pixmap.height() / max(1, display.height())
            crop_rect = QRect(
                int((selection.left() - display.left()) * sx),
                int((selection.top() - display.top()) * sy),
                int(selection.width() * sx),
                int(selection.height() * sy),
            ).intersected(source_rect)
            cropped = self._source_pixmap.copy(crop_rect)
        if cropped.isNull() or not cropped.save(str(target), "PNG"):
            raise ValueError("保存裁剪图片失败。")
        return target


class StyleCard(QFrame):
    clicked = Signal(str)
    _preview_cache: dict[tuple[str, int, int], QPixmap | None] = {}

    def __init__(
        self,
        style: dict,
        subtitle: str,
        *,
        card_width: int = CARD_W,
        card_height: int | None = None,
        image_width: int = CARD_IMG_W,
        image_height: int = CARD_IMG_H,
    ) -> None:
        super().__init__()
        self.style_id = str(style.get("id", ""))
        self.card_width = int(card_width)
        self.card_height = card_height
        self.image_width = int(image_width)
        self.image_height = int(image_height)
        self.setObjectName("styleCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("selected", False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(self.card_width)
        if self.card_height is not None:
            self.setFixedHeight(int(self.card_height))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(7)

        img = QLabel()
        img.setObjectName("cardImage")
        img.setFixedSize(self.image_width, self.image_height)
        img.setAlignment(Qt.AlignCenter)
        self._set_pixmap(img, str(style.get("image", "")))
        lay.addWidget(img, alignment=Qt.AlignHCenter)

        name = str(style.get("name", self.style_id))
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        t = QLabel(name)
        t.setObjectName("cardTitle")
        t.setWordWrap(True)
        t.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title_row.addWidget(t, 1)
        if style.get("is_custom"):
            custom_badge = QLabel("自定义")
            custom_badge.setObjectName("customBadge")
            title_row.addWidget(custom_badge, 0, Qt.AlignTop)
        lay.addLayout(title_row)

        s = QLabel(subtitle)
        s.setObjectName("cardSubtitle")
        s.setWordWrap(True)
        s.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lay.addWidget(s)
        if self.card_height is not None:
            lay.addStretch(1)
        self.setToolTip(f"{name}\n{subtitle}".strip())

    def _set_pixmap(self, label: QLabel, image_name: str) -> None:
        p = core.STYLE_DIR / image_name
        try:
            stat = p.stat()
            cache_key = (str(p.resolve()), stat.st_mtime_ns, stat.st_size)
        except OSError:
            cache_key = (str(p.resolve()), 0, 0)
        if cache_key not in self._preview_cache:
            if p.suffix.lower() == ".png" and not png_data_stream_is_valid(p):
                loaded = QPixmap()
            else:
                loaded = QPixmap(str(p))
            self._preview_cache[cache_key] = None if loaded.isNull() else loaded
        pix = self._preview_cache[cache_key]
        if pix is None:
            blank = QPixmap(self.image_width, self.image_height)
            blank.fill(Qt.transparent)
            painter = QPainter(blank)
            gradient = QLinearGradient(0, 0, self.image_width, self.image_height)
            gradient.setColorAt(0.0, QColor("#dbeafe"))
            gradient.setColorAt(1.0, QColor("#e9d5ff"))
            painter.fillRect(blank.rect(), gradient)
            painter.setPen(QColor("#31537a"))
            font = painter.font()
            font.setBold(True)
            font.setPointSize(16)
            painter.setFont(font)
            painter.drawText(blank.rect(), Qt.AlignCenter, "VMD\n预览待补充")
            painter.end()
            label.setPixmap(blank)
            return
        canvas = QPixmap(self.image_width, self.image_height)
        canvas.fill(QColor("#f7f9fc"))
        scaled = pix.scaled(
            self.image_width - 8,
            self.image_height - 8,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(
            (self.image_width - scaled.width()) // 2,
            (self.image_height - scaled.height()) // 2,
            scaled,
        )
        painter.end()
        label.setPixmap(canvas)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", bool(selected))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self.style_id:
            self.clicked.emit(self.style_id)
        super().mousePressEvent(event)


class CardGrid(QScrollArea):
    stylePicked = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        card_width: int = CARD_W,
        card_height: int | None = None,
        image_width: int = CARD_IMG_W,
        image_height: int = CARD_IMG_H,
        gap: int = CARD_GAP,
        max_columns: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.card_width = int(card_width)
        self.card_height = card_height
        self.image_width = int(image_width)
        self.image_height = int(image_height)
        self.gap = int(gap)
        self.max_columns = max_columns
        self.setObjectName("cardGrid")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._body = QWidget()
        self._grid = QGridLayout(self._body)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setHorizontalSpacing(self.gap)
        self._grid.setVerticalSpacing(self.gap)
        self.setWidget(self._body)

        self.cards: list[StyleCard] = []
        self.selected_id = ""
        self.placeholder = QLabel("暂无风格")
        self.placeholder.setObjectName("emptyLabel")
        self.placeholder.setAlignment(Qt.AlignCenter)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._relayout()

    def load_styles(self, styles: list[dict], selected_id: str, subtitle_fn) -> str:
        self.selected_id = selected_id or ""
        self.cards.clear()
        self._clear(delete_widgets=True)
        if not styles:
            self._grid.addWidget(self.placeholder, 0, 0)
            return self.selected_id

        for st in styles:
            sid = str(st.get("id", ""))
            if not sid:
                continue
            sub = subtitle_fn(st) if callable(subtitle_fn) else str(st.get("notes", ""))
            card = StyleCard(
                st,
                sub,
                card_width=self.card_width,
                card_height=self.card_height,
                image_width=self.image_width,
                image_height=self.image_height,
            )
            card.clicked.connect(self._on_pick)
            self.cards.append(card)

        ids = [c.style_id for c in self.cards]
        if self.selected_id not in ids and ids:
            self.selected_id = ids[0]
        self._relayout()
        self._sync()
        return self.selected_id

    def _clear(self, delete_widgets: bool) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item and item.widget():
                if delete_widgets:
                    item.widget().deleteLater()

    def _cols(self) -> int:
        w = max(260, self.viewport().width() - 8)
        columns = max(1, w // (self.card_width + self.gap))
        if self.max_columns is not None:
            columns = min(columns, self.max_columns)
        return columns

    def _relayout(self) -> None:
        self._clear(delete_widgets=False)
        if not self.cards:
            self._grid.addWidget(self.placeholder, 0, 0)
            return
        cols = self._cols()
        for i, card in enumerate(self.cards):
            r = i // cols
            c = i % cols
            self._grid.addWidget(card, r, c, alignment=Qt.AlignTop)
            card.show()
        self._grid.setRowStretch((len(self.cards) // cols) + 1, 1)
        self._body.updateGeometry()
        self._body.update()
        self.viewport().update()

    def _sync(self) -> None:
        for c in self.cards:
            c.set_selected(c.style_id == self.selected_id)

    def _on_pick(self, style_id: str) -> None:
        self.selected_id = style_id
        self._sync()
        self.stylePicked.emit(style_id)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        install_wheel_navigation_guard()
        self.setWindowTitle("VMD + Multiwfn 绘图工作台")
        self.setMinimumSize(WINDOW_MIN_W, WINDOW_MIN_H)
        screen = QApplication.primaryScreen()
        if screen is None:
            initial_width, initial_height = 1280, 720
        else:
            available = screen.availableGeometry()
            initial_width, initial_height = preferred_window_size(
                available.width(), available.height()
            )
        self.resize(initial_width, initial_height)

        self.bundle_styles: list[dict] = []
        self.skeleton_styles: list[dict] = []
        self.bundle_map: dict[str, dict] = {}
        self.skeleton_map: dict[str, dict] = {}
        self.selected_bundle_id = ""
        self.selected_iso_id = ""
        self.selected_skeleton_id = ""
        self.mode = "bundle"
        self.dark_mode = False
        self.ai_effective_image_path = ""
        self.ai_current_guess: dict | None = None
        self.ai_temp_files: set[Path] = set()
        self.ai_thread: QThread | None = None
        self.ai_worker: AiRecognitionWorker | None = None
        self.ai_pending: dict = {}
        self._page_animation: QPropertyAnimation | None = None

        self._build_ui()
        self._load_initial()

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("sectionCard")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("sectionCaption")
        lay.addWidget(label)
        return frame, lay

    @staticmethod
    def _apply_shadow(
        widget: QWidget, *, blur: int = 24, offset_y: int = 4, alpha: int = 26
    ) -> None:
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(blur)
        shadow.setOffset(0, offset_y)
        shadow.setColor(QColor(20, 35, 55, alpha))
        widget.setGraphicsEffect(shadow)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)

        main_split = QSplitter(Qt.Horizontal)
        main_split.setChildrenCollapsible(False)
        main_split.setHandleWidth(8)
        self.main_split = main_split
        outer.addWidget(main_split, 1)

        left_panel = QFrame()
        left_panel.setObjectName("leftPanel")
        left_panel.setMinimumWidth(300)
        left_panel.setMaximumWidth(340)
        left_panel_layout = QVBoxLayout(left_panel)
        left_panel_layout.setContentsMargins(10, 10, 10, 10)
        left_panel_layout.setSpacing(0)

        left_scroll = QScrollArea()
        left_scroll.setObjectName("leftScroll")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        left_body = QWidget()
        left_col = QVBoxLayout(left_body)
        left_col.setContentsMargins(2, 2, 2, 2)
        left_col.setSpacing(10)
        left_scroll.setWidget(left_body)
        left_panel_layout.addWidget(left_scroll)
        self._apply_shadow(left_panel, blur=28, offset_y=5, alpha=24)
        main_split.addWidget(left_panel)

        right_panel = QFrame()
        right_panel.setObjectName("rightPanel")
        self.right_panel = right_panel
        right_col = QVBoxLayout(right_panel)
        right_col.setContentsMargins(12, 12, 12, 12)
        right_col.setSpacing(10)
        self._apply_shadow(right_panel, blur=28, offset_y=5, alpha=24)
        main_split.addWidget(right_panel)
        main_split.setStretchFactor(1, 1)
        main_split.setSizes([320, 780])

        brand = QFrame()
        brand.setObjectName("brandCard")
        brand_l = QHBoxLayout(brand)
        brand_l.setContentsMargins(12, 11, 10, 11)
        brand_l.setSpacing(10)
        brand_mark = QLabel("VM")
        brand_mark.setObjectName("brandMark")
        brand_mark.setAlignment(Qt.AlignCenter)
        brand_mark.setFixedSize(42, 42)
        brand_l.addWidget(brand_mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_title = QLabel("Molecular Studio")
        brand_title.setObjectName("brandTitle")
        brand_subtitle = QLabel("VMD · Multiwfn 工作台")
        brand_subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(brand_title)
        brand_text.addWidget(brand_subtitle)
        brand_l.addLayout(brand_text, 1)
        self.theme_btn = QPushButton("深色")
        self.theme_btn.setObjectName("themeButton")
        self.theme_btn.setCheckable(True)
        self.theme_btn.clicked.connect(self._toggle_theme)
        brand_l.addWidget(self.theme_btn)
        left_col.addWidget(brand)

        workspace_sec, workspace_l = self._section("工作区")
        workspace_grid = QGridLayout()
        workspace_grid.setHorizontalSpacing(6)
        workspace_grid.setVerticalSpacing(6)
        self.nav_style_btn = QPushButton("绘图方案")
        self.nav_style_btn.setObjectName("navButton")
        self.nav_style_btn.setCheckable(True)
        self.nav_style_btn.clicked.connect(self._show_style_selection)
        self.nav_custom_btn = QPushButton("自定义")
        self.nav_custom_btn.setObjectName("navButton")
        self.nav_custom_btn.setCheckable(True)
        self.nav_custom_btn.clicked.connect(self._show_custom_import)
        self.nav_automation_btn = QPushButton("全自动流程")
        self.nav_automation_btn.setObjectName("navButton")
        self.nav_automation_btn.setCheckable(True)
        self.nav_automation_btn.clicked.connect(self._show_automation_page)
        self.nav_batch_btn = QPushButton("批量 Multiwfn")
        self.nav_batch_btn.setObjectName("navButton")
        self.nav_batch_btn.setCheckable(True)
        self.nav_batch_btn.clicked.connect(self._show_batch_page)
        workspace_grid.addWidget(self.nav_style_btn, 0, 0)
        workspace_grid.addWidget(self.nav_automation_btn, 0, 1)
        workspace_grid.addWidget(self.nav_custom_btn, 1, 0)
        workspace_grid.addWidget(self.nav_batch_btn, 1, 1)
        workspace_grid.setColumnStretch(0, 1)
        workspace_grid.setColumnStretch(1, 1)
        workspace_l.addLayout(workspace_grid)
        left_col.addWidget(workspace_sec)

        self.style_mode_section, mode_l = self._section("方案组合方式")
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.mode_bundle_btn = QPushButton("套装模式")
        self.mode_bundle_btn.setObjectName("modeButton")
        self.mode_bundle_btn.setCheckable(True)
        self.mode_split_btn = QPushButton("拆分模式")
        self.mode_split_btn.setObjectName("modeButton")
        self.mode_split_btn.setCheckable(True)
        self.mode_bundle_btn.clicked.connect(lambda: self._set_mode("bundle"))
        self.mode_split_btn.clicked.connect(lambda: self._set_mode("split"))
        mode_row.addWidget(self.mode_bundle_btn)
        mode_row.addWidget(self.mode_split_btn)
        mode_l.addLayout(mode_row)
        left_col.addWidget(self.style_mode_section)

        self.path_section, path_l = self._section("程序路径")
        path_grid = QGridLayout()
        path_grid.setHorizontalSpacing(6)
        path_grid.setVerticalSpacing(6)

        path_grid.addWidget(QLabel("Multiwfn"), 0, 0)
        self.multi_edit = QLineEdit()
        self.multi_edit.setPlaceholderText(r"E:\...\Multiwfn.exe")
        path_grid.addWidget(self.multi_edit, 0, 1)
        btn_multi = QPushButton("浏览")
        btn_multi.clicked.connect(self._pick_multi)
        path_grid.addWidget(btn_multi, 0, 2)

        path_grid.addWidget(QLabel("VMD"), 1, 0)
        self.vmd_edit = QLineEdit()
        self.vmd_edit.setPlaceholderText(r"E:\...\vmd.exe")
        path_grid.addWidget(self.vmd_edit, 1, 1)
        btn_vmd = QPushButton("浏览")
        btn_vmd.clicked.connect(self._pick_vmd)
        path_grid.addWidget(btn_vmd, 1, 2)
        path_l.addLayout(path_grid)
        btn_scan_paths = QPushButton("自动扫描程序路径")
        btn_scan_paths.clicked.connect(self._scan_paths)
        path_l.addWidget(btn_scan_paths)
        left_col.addWidget(self.path_section)

        # Script export uses a native save dialog.  Keep only its remembered
        # name and directory instead of constructing a permanently hidden form.
        self.out_edit = QLineEdit(left_body)
        self.out_edit.hide()
        self.out_dir_edit = QLineEdit(left_body)
        self.out_dir_edit.hide()

        self.log_section, log_l = self._section("活动记录")
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setFixedHeight(96)
        self.log_view.setPlaceholderText("运行状态与提示会显示在这里")
        log_l.addWidget(self.log_view)
        self.log_section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        left_col.addWidget(self.log_section)
        left_col.addStretch(1)

        self.page_header = QFrame()
        self.page_header.setObjectName("pageHeader")
        head_row = QHBoxLayout(self.page_header)
        head_row.setContentsMargins(2, 0, 2, 0)
        head_row.setSpacing(10)
        heading = QVBoxLayout()
        heading.setSpacing(1)
        self.main_title = QLabel("风格选择")
        self.main_title.setObjectName("sectionTitle")
        self.main_subtitle = QLabel("选择视觉方案后直接绘图，或导出可重复使用的工作流脚本")
        self.main_subtitle.setObjectName("pageSubtitle")
        heading.addWidget(self.main_title)
        heading.addWidget(self.main_subtitle)
        head_row.addLayout(heading)
        head_row.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("countPill")
        head_row.addWidget(self.count_label)
        self.delete_custom_btn = QPushButton("删除自定义风格")
        self.delete_custom_btn.setObjectName("dangerBtn")
        self.delete_custom_btn.clicked.connect(self._delete_selected_custom_style)
        self.delete_custom_btn.hide()
        head_row.addWidget(self.delete_custom_btn)
        right_col.addWidget(self.page_header)

        self.filter_bar = QFrame()
        self.filter_bar.setObjectName("filterBar")
        filter_row = QHBoxLayout(self.filter_bar)
        filter_row.setContentsMargins(10, 8, 10, 8)
        filter_row.setSpacing(8)
        self.style_search_edit = QLineEdit()
        self.style_search_edit.setObjectName("styleSearch")
        self.style_search_edit.setPlaceholderText("搜索名称、说明或来源…")
        self.style_search_edit.setClearButtonEnabled(True)
        self.style_search_edit.textChanged.connect(self._refresh_lists)
        filter_row.addWidget(self.style_search_edit, 1)
        self.material_filter_combo = QComboBox()
        self.material_filter_combo.setObjectName("materialFilter")
        self.material_filter_combo.setMinimumWidth(150)
        self.material_filter_combo.currentIndexChanged.connect(self._refresh_lists)
        filter_row.addWidget(self.material_filter_combo)
        self.style_sort_combo = QComboBox()
        self.style_sort_combo.setObjectName("styleSort")
        self.style_sort_combo.addItem("默认排序", "default")
        self.style_sort_combo.addItem("按名称", "name")
        self.style_sort_combo.addItem("按材质", "material")
        self.style_sort_combo.addItem("自定义优先", "custom")
        self.style_sort_combo.currentIndexChanged.connect(self._refresh_lists)
        filter_row.addWidget(self.style_sort_combo)
        right_col.addWidget(self.filter_bar)

        self.stack = QStackedWidget()
        right_col.addWidget(self.stack, 1)

        bundle_page = QWidget()
        bundle_l = QVBoxLayout(bundle_page)
        bundle_l.setContentsMargins(0, 0, 0, 0)
        self.bundle_grid = CardGrid()
        self.bundle_grid.stylePicked.connect(self._on_bundle_picked)
        bundle_l.addWidget(self.bundle_grid)
        self.stack.addWidget(bundle_page)

        split_page = QWidget()
        split_l = QHBoxLayout(split_page)
        split_l.setContentsMargins(0, 0, 0, 0)
        split_l.setSpacing(10)

        split_inner = QSplitter(Qt.Horizontal)
        split_inner.setChildrenCollapsible(False)
        split_inner.setHandleWidth(6)
        self.split_inner = split_inner

        sk_wrap = QFrame()
        sk_wrap.setObjectName("stylePane")
        sk_wrap.setMinimumWidth(260)
        sk_l = QVBoxLayout(sk_wrap)
        sk_l.setContentsMargins(10, 10, 10, 10)
        sk_l.setSpacing(8)
        sk_title = QLabel("骨架样式")
        sk_title.setObjectName("paneTitle")
        sk_l.addWidget(sk_title)
        self.skeleton_grid = CardGrid(
            card_width=SPLIT_CARD_W,
            card_height=SPLIT_CARD_H,
            image_width=SPLIT_CARD_IMG_W,
            image_height=SPLIT_CARD_IMG_H,
            gap=SPLIT_CARD_GAP,
            max_columns=2,
        )
        self.skeleton_grid.stylePicked.connect(self._on_skeleton_picked)
        sk_l.addWidget(self.skeleton_grid, 1)
        split_inner.addWidget(sk_wrap)

        iso_wrap = QFrame()
        iso_wrap.setObjectName("stylePane")
        iso_wrap.setMinimumWidth(260)
        iso_l = QVBoxLayout(iso_wrap)
        iso_l.setContentsMargins(10, 10, 10, 10)
        iso_l.setSpacing(8)
        iso_title = QLabel("等值面样式")
        iso_title.setObjectName("paneTitle")
        iso_l.addWidget(iso_title)
        self.iso_grid = CardGrid(
            card_width=SPLIT_CARD_W,
            card_height=SPLIT_CARD_H,
            image_width=SPLIT_CARD_IMG_W,
            image_height=SPLIT_CARD_IMG_H,
            gap=SPLIT_CARD_GAP,
            max_columns=2,
        )
        self.iso_grid.stylePicked.connect(self._on_iso_picked)
        iso_l.addWidget(self.iso_grid, 1)
        split_inner.addWidget(iso_wrap)

        split_inner.setStretchFactor(0, 1)
        split_inner.setStretchFactor(1, 1)
        split_inner.setSizes([520, 520])
        split_l.addWidget(split_inner, 1)
        self.stack.addWidget(split_page)

        self.custom_page_index = self.stack.addWidget(self._build_custom_import_page())
        self.batch_page = MultiwfnBatchPage(
            core.ROOT, lambda: self.multi_edit.text().strip()
        )
        self.batch_page.settingsChanged.connect(self._save_batch_settings)
        self.batch_page_index = self.stack.addWidget(self.batch_page)
        self.automation_page = AutomaticWorkflowsPage(
            core.ROOT,
            lambda: self.multi_edit.text().strip(),
            lambda: self.vmd_edit.text().strip(),
        )
        self.automation_page.settingsChanged.connect(self._save_batch_settings)
        self.automation_page_index = self.stack.addWidget(self.automation_page)
        self.direct_page = DirectWorkflowPage(
            lambda: self.multi_edit.text().strip(),
            lambda: self.vmd_edit.text().strip(),
        )
        self.direct_page.backRequested.connect(self._show_style_selection)
        self.direct_page_index = self.stack.addWidget(self.direct_page)

        self.style_action_bar = QFrame()
        self.style_action_bar.setObjectName("styleActionBar")
        style_action_layout = QHBoxLayout(self.style_action_bar)
        style_action_layout.setContentsMargins(12, 9, 12, 9)
        style_action_layout.setSpacing(8)
        self.action_selection_label = QLabel("请选择一个绘图风格")
        self.action_selection_label.setObjectName("actionSelectionLabel")
        self.action_selection_label.setWordWrap(True)
        style_action_layout.addWidget(self.action_selection_label, 1)
        self.btn_style_parameters = QPushButton("查看风格参数")
        self.btn_style_parameters.clicked.connect(self._show_selected_style_parameters)
        style_action_layout.addWidget(self.btn_style_parameters)
        self.btn_export_script = QPushButton("导出脚本")
        self.btn_export_script.clicked.connect(self._export_script_dialog)
        style_action_layout.addWidget(self.btn_export_script)
        self.btn_direct_draw = QPushButton("直接绘图")
        self.btn_direct_draw.setObjectName("primaryBtn")
        self.btn_direct_draw.clicked.connect(self._show_direct_workflow)
        style_action_layout.addWidget(self.btn_direct_draw)
        right_col.addWidget(self.style_action_bar)
        self.btn_generate = self.btn_export_script

        self.search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.search_shortcut.activated.connect(self.style_search_edit.setFocus)
        self.generate_shortcut = QShortcut(QKeySequence("Ctrl+G"), self)
        self.generate_shortcut.activated.connect(self.btn_generate.click)
        self.direct_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.direct_shortcut.activated.connect(self.btn_direct_draw.click)
        self.theme_shortcut = QShortcut(QKeySequence("Ctrl+T"), self)
        self.theme_shortcut.activated.connect(self.theme_btn.click)
        self.style_search_edit.setToolTip("支持多关键词搜索（Ctrl+F）")
        self.btn_generate.setToolTip("导出脚本（Ctrl+G）")
        self.btn_direct_draw.setToolTip("使用当前风格直接绘图（Ctrl+Enter）")
        self.theme_btn.setToolTip("切换深浅主题（Ctrl+T）")

        self._apply_styles()

    def _build_custom_import_page(self) -> QWidget:
        page = QWidget()
        page_l = QVBoxLayout(page)
        page_l.setContentsMargins(0, 0, 0, 0)
        page_l.setSpacing(10)

        switch_row = QHBoxLayout()
        switch_row.setSpacing(8)
        self.import_state_btn = QPushButton("VMD 状态文件")
        self.import_state_btn.setObjectName("modeButton")
        self.import_state_btn.setCheckable(True)
        self.import_ai_btn = QPushButton("AI 图片识别")
        self.import_ai_btn.setObjectName("modeButton")
        self.import_ai_btn.setCheckable(True)
        self.import_state_btn.clicked.connect(lambda: self._set_import_page(0))
        self.import_ai_btn.clicked.connect(lambda: self._set_import_page(1))
        switch_row.addWidget(self.import_state_btn)
        switch_row.addWidget(self.import_ai_btn)
        switch_row.addStretch(1)
        page_l.addLayout(switch_row)

        self.import_stack = QStackedWidget()
        page_l.addWidget(self.import_stack, 1)

        state_page = QWidget()
        state_l = QVBoxLayout(state_page)
        state_l.setContentsMargins(0, 0, 0, 0)
        state_l.setSpacing(10)
        state_intro = QFrame()
        state_intro.setObjectName("importIntro")
        state_intro_l = QVBoxLayout(state_intro)
        state_intro_l.setContentsMargins(18, 15, 18, 15)
        state_intro_l.setSpacing(5)
        state_kicker = QLabel("导入已有方案")
        state_kicker.setObjectName("kickerLabel")
        state_intro_l.addWidget(state_kicker)
        state_title = QLabel("把成熟方案保存为自定义风格")
        state_title.setObjectName("importTitle")
        state_intro_l.addWidget(state_title)
        state_desc = QLabel(
            "读取 VMD Save State 中稳定、可复用的视觉设置，并配上封面和说明。"
        )
        state_desc.setObjectName("mutedLabel")
        state_desc.setWordWrap(True)
        state_intro_l.addWidget(state_desc)
        state_l.addWidget(state_intro)

        state_wrap = QFrame()
        state_wrap.setObjectName("stylePane")
        state_grid = QGridLayout(state_wrap)
        state_grid.setContentsMargins(20, 20, 20, 20)
        state_grid.setHorizontalSpacing(10)
        state_grid.setVerticalSpacing(12)

        form_title = QLabel("导入 VMD Save State")
        form_title.setObjectName("paneTitle")
        form_hint = QLabel("导入材质、颜色、灯光和视角等可复用的视觉设置。")
        form_hint.setObjectName("mutedLabel")
        form_hint.setWordWrap(True)
        state_grid.addWidget(form_title, 0, 0, 1, 3)
        state_grid.addWidget(form_hint, 1, 0, 1, 3)

        state_grid.addWidget(QLabel("名称"), 2, 0)
        self.custom_name_edit = QLineEdit()
        self.custom_name_edit.setPlaceholderText("例如：柔和高光等值面")
        state_grid.addWidget(self.custom_name_edit, 2, 1, 1, 2)

        state_grid.addWidget(QLabel("简介"), 3, 0)
        self.custom_desc_edit = QLineEdit()
        self.custom_desc_edit.setPlaceholderText("简要说明适用场景和视觉特点")
        state_grid.addWidget(self.custom_desc_edit, 3, 1, 1, 2)

        state_grid.addWidget(QLabel("状态文件"), 4, 0)
        self.state_file_edit = QLineEdit()
        self.state_file_edit.setPlaceholderText("选择 .vmd / Save State 文件")
        state_grid.addWidget(self.state_file_edit, 4, 1)
        btn_state = QPushButton("选择")
        btn_state.clicked.connect(self._pick_state_file)
        state_grid.addWidget(btn_state, 4, 2)

        state_grid.addWidget(QLabel("封面图"), 5, 0)
        self.cover_file_edit = QLineEdit()
        self.cover_file_edit.setPlaceholderText("可选，建议使用横向预览图")
        state_grid.addWidget(self.cover_file_edit, 5, 1)
        btn_cover = QPushButton("选择")
        btn_cover.clicked.connect(self._pick_cover_file)
        state_grid.addWidget(btn_cover, 5, 2)

        btn_import = QPushButton("导入自定义风格")
        btn_import.setObjectName("generateBtn")
        btn_import.clicked.connect(self._import_custom_style)
        state_grid.addWidget(btn_import, 6, 1, 1, 2)
        state_grid.setColumnStretch(1, 1)
        state_l.addWidget(state_wrap)
        state_l.addStretch(1)
        self.import_stack.addWidget(state_page)

        ai_page = QWidget()
        ai_outer = QVBoxLayout(ai_page)
        ai_outer.setContentsMargins(0, 0, 0, 0)
        ai_outer.setSpacing(0)
        ai_scroll = QScrollArea()
        ai_scroll.setObjectName("aiImportScroll")
        ai_scroll.setWidgetResizable(True)
        ai_scroll.setFrameShape(QFrame.NoFrame)
        ai_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ai_body = QWidget()
        ai_l = QVBoxLayout(ai_body)
        ai_l.setContentsMargins(0, 0, 0, 0)
        ai_l.setSpacing(10)
        ai_scroll.setWidget(ai_body)
        ai_outer.addWidget(ai_scroll, 1)

        def form_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("formLabel")
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return label

        ai_wrap = QFrame()
        ai_wrap.setObjectName("stylePane")
        ai_form = QVBoxLayout(ai_wrap)
        ai_form.setContentsMargins(12, 12, 12, 12)
        ai_form.setSpacing(9)

        provider_row = QHBoxLayout()
        provider_row.setSpacing(8)
        provider_label = form_label("提供商")
        provider_label.setMinimumWidth(52)
        provider_row.addWidget(provider_label)
        self.ai_provider_combo = QComboBox()
        self.ai_provider_combo.addItem("OpenAI", "openai")
        self.ai_provider_combo.addItem("Gemini", "gemini")
        self.ai_provider_combo.currentIndexChanged.connect(self._on_ai_provider_changed)
        self.ai_provider_combo.setMinimumWidth(110)
        provider_row.addWidget(self.ai_provider_combo)

        model_label = form_label("模型")
        model_label.setMinimumWidth(40)
        provider_row.addWidget(model_label)
        self.ai_model_edit = QLineEdit()
        provider_row.addWidget(self.ai_model_edit, 1)
        ai_form.addLayout(provider_row)

        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        key_label = form_label("API Key")
        key_label.setMinimumWidth(52)
        key_row.addWidget(key_label)
        self.ai_key_edit = QLineEdit()
        self.ai_key_edit.setEchoMode(QLineEdit.Password)
        self.ai_key_edit.setPlaceholderText("OPENAI_API_KEY")
        key_row.addWidget(self.ai_key_edit, 1)
        self.ai_key_toggle = QPushButton("显示")
        self.ai_key_toggle.setCheckable(True)
        self.ai_key_toggle.setFixedWidth(58)
        self.ai_key_toggle.toggled.connect(self._toggle_ai_key_visibility)
        key_row.addWidget(self.ai_key_toggle)
        ai_form.addLayout(key_row)

        image_row = QHBoxLayout()
        image_row.setSpacing(8)
        image_label = form_label("图片")
        image_label.setMinimumWidth(52)
        image_row.addWidget(image_label)
        self.ai_image_edit = QLineEdit()
        self.ai_image_edit.setPlaceholderText("选择需要识别的参考图片")
        image_row.addWidget(self.ai_image_edit, 1)
        btn_ai_image = QPushButton("选择")
        btn_ai_image.clicked.connect(self._pick_ai_image)
        image_row.addWidget(btn_ai_image)
        self.btn_ai_recognize = QPushButton("识别风格")
        self.btn_ai_recognize.setObjectName("primaryBtn")
        self.btn_ai_recognize.clicked.connect(self._recognize_ai_style)
        image_row.addWidget(self.btn_ai_recognize)
        ai_form.addLayout(image_row)
        ai_l.addWidget(ai_wrap)

        preview_wrap = QFrame()
        preview_wrap.setObjectName("stylePane")
        preview_l = QVBoxLayout(preview_wrap)
        preview_l.setContentsMargins(12, 12, 12, 12)
        preview_l.setSpacing(8)
        preview_head = QHBoxLayout()
        preview_title = QLabel("识别区域")
        preview_title.setObjectName("paneTitle")
        preview_head.addWidget(preview_title)
        preview_head.addStretch(1)
        self.ai_crop_status = QLabel("未裁剪")
        self.ai_crop_status.setObjectName("countPill")
        preview_head.addWidget(self.ai_crop_status)
        btn_reset_crop = QPushButton("重置裁剪")
        btn_reset_crop.clicked.connect(self._reset_ai_crop)
        preview_head.addWidget(btn_reset_crop)
        preview_l.addLayout(preview_head)
        self.ai_crop_label = CropImageLabel()
        self.ai_crop_label.setObjectName("cropPreview")
        self.ai_crop_label.cropChanged.connect(self._update_ai_crop_status)
        preview_l.addWidget(self.ai_crop_label)
        ai_l.addWidget(preview_wrap)

        result_wrap = QFrame()
        result_wrap.setObjectName("stylePane")
        result_l = QVBoxLayout(result_wrap)
        result_l.setContentsMargins(12, 12, 12, 12)
        result_l.setSpacing(8)
        result_head = QHBoxLayout()
        result_title = QLabel("识别结果")
        result_title.setObjectName("paneTitle")
        result_head.addWidget(result_title)
        result_head.addStretch(1)
        result_l.addLayout(result_head)

        result_grid = QGridLayout()
        result_grid.setHorizontalSpacing(10)
        result_grid.setVerticalSpacing(10)
        self.ai_result_empty = QLabel("选择图片后点击“识别风格”。")
        self.ai_result_empty.setObjectName("emptyLabel")
        result_grid.addWidget(self.ai_result_empty, 0, 0, 1, 2)

        meta_box, meta_l = self._result_box("保存信息")
        self.ai_name_edit = QLineEdit()
        self.ai_desc_edit = QLineEdit()
        meta_l.addLayout(self._field_row("名称", self.ai_name_edit))
        meta_l.addLayout(self._field_row("简介", self.ai_desc_edit))

        color_box, color_l = self._result_box("颜色")
        self.ai_pos_swatch, self.ai_pos_text = self._add_color_row(color_l, "正相")
        self.ai_neg_swatch, self.ai_neg_text = self._add_color_row(color_l, "负相")
        self.ai_skeleton_swatch, self.ai_skeleton_text = self._add_color_row(color_l, "骨架")
        self.ai_bg_swatch, self.ai_bg_text = self._add_color_row(color_l, "背景")

        material_box, material_l = self._result_box("材质 / 透明度")
        self.ai_material_combo = QComboBox()
        self.ai_material_combo.addItems(core.VMD_MATERIALS)
        material_l.addLayout(self._field_row("材质", self.ai_material_combo))
        opacity_row, self.ai_opacity_spin = self._slider_spin_row("透明度", 0.05, 1.00, 0.05)
        specular_row, self.ai_specular_spin = self._slider_spin_row("高光", 0.00, 1.00, 0.05)
        shininess_row, self.ai_shininess_spin = self._slider_spin_row("锐度", 0.00, 1.00, 0.05)
        material_l.addLayout(opacity_row)
        material_l.addLayout(specular_row)
        material_l.addLayout(shininess_row)

        view_box, view_l = self._result_box("视角 / 骨架")
        self.ai_projection_combo = QComboBox()
        self.ai_projection_combo.addItems(["Orthographic", "Perspective"])
        self.ai_skeleton_combo = QComboBox()
        self.ai_skeleton_combo.addItems(["CPK", "Licorice", "Bonds", "Lines"])
        self.ai_skeleton_material_combo = QComboBox()
        self.ai_skeleton_material_combo.addItems(core.VMD_MATERIALS)
        self.ai_depthcue_check = QCheckBox("Depthcue 开启")
        view_l.addLayout(self._field_row("投影", self.ai_projection_combo))
        view_l.addLayout(self._field_row("骨架", self.ai_skeleton_combo))
        view_l.addLayout(self._field_row("骨架材质", self.ai_skeleton_material_combo))
        view_l.addWidget(self.ai_depthcue_check)

        default_box, default_l = self._result_box("识别不确定时")
        self.ai_default_material_check = QCheckBox("使用 Glossy 材质")
        self.ai_default_render_check = QCheckBox("透明度和高光使用推荐值")
        self.ai_default_view_check = QCheckBox("使用正交投影并关闭景深")
        self.ai_default_light_check = QCheckBox("使用默认灯光组合")
        self.ai_default_skeleton_check = QCheckBox("使用 CPK + Glossy 骨架")
        self.ai_default_isovalue_check = QCheckBox("绘图时再填写等值面数值")
        for chk in (
            self.ai_default_material_check,
            self.ai_default_render_check,
            self.ai_default_view_check,
            self.ai_default_light_check,
            self.ai_default_skeleton_check,
            self.ai_default_isovalue_check,
        ):
            chk.setChecked(True)
            chk.toggled.connect(self._apply_checked_defaults)
            default_l.addWidget(chk)

        light_box, light_l = self._result_box("光照 / 状态")
        light_row = QHBoxLayout()
        self.ai_light_checks: dict[str, QCheckBox] = {}
        for idx in range(4):
            chk = QCheckBox(str(idx))
            self.ai_light_checks[str(idx)] = chk
            light_row.addWidget(chk)
        light_row.addStretch(1)
        light_l.addLayout(self._labeled_layout("灯光", light_row))
        self.ai_conf_label = QLabel("识别可靠度：尚未识别")
        self.ai_uncertain_label = QLabel("建议检查：尚未识别")
        self.ai_method_label = QLabel("识别方式：尚未识别")
        for label in (self.ai_conf_label, self.ai_uncertain_label, self.ai_method_label):
            label.setObjectName("resultText")
            label.setWordWrap(True)
            light_l.addWidget(label)

        self.ai_result_boxes = [
            meta_box,
            color_box,
            material_box,
            view_box,
            default_box,
            light_box,
        ]
        result_grid.addWidget(meta_box, 1, 0, 1, 2)
        result_grid.addWidget(color_box, 2, 0)
        result_grid.addWidget(material_box, 2, 1)
        result_grid.addWidget(view_box, 3, 0)
        result_grid.addWidget(default_box, 3, 1)
        result_grid.addWidget(light_box, 4, 0, 1, 2)
        result_l.addLayout(result_grid)
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.btn_save_ai = QPushButton("保存为自定义风格")
        self.btn_save_ai.setObjectName("generateBtn")
        self.btn_save_ai.clicked.connect(self._save_ai_custom_style)
        save_row.addWidget(self.btn_save_ai)
        result_l.addLayout(save_row)
        ai_l.addWidget(result_wrap)
        ai_l.addStretch(1)

        self.import_stack.addWidget(ai_page)
        self._set_import_page(0)
        self._on_ai_provider_changed()
        self._set_result_panel_visible(False)
        return page

    def _result_box(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        box = QFrame()
        box.setObjectName("resultBox")
        box.setMinimumHeight(118)
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("resultTitle")
        lay.addWidget(label)
        return box, lay

    def _field_row(self, label_text: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(label_text)
        label.setObjectName("fieldLabel")
        label.setMinimumWidth(64)
        row.addWidget(label)
        row.addWidget(widget, 1)
        return row

    def _labeled_layout(self, label_text: str, inner: QHBoxLayout) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(label_text)
        label.setObjectName("fieldLabel")
        label.setMinimumWidth(64)
        row.addWidget(label)
        row.addLayout(inner, 1)
        return row

    def _number_spin(self, low: float, high: float, step: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        return spin

    def _slider_spin_row(
        self, label_text: str, low: float, high: float, step: float
    ) -> tuple[QHBoxLayout, QDoubleSpinBox]:
        row = QHBoxLayout()
        label = QLabel(label_text)
        label.setObjectName("fieldLabel")
        label.setMinimumWidth(64)
        slider = QSlider(Qt.Horizontal)
        slider.setObjectName("valueSlider")
        spin = self._number_spin(low, high, step)
        spin.setFixedWidth(82)
        steps = int(round((high - low) / step))
        slider.setRange(0, steps)

        def slider_to_value(position: int) -> float:
            return low + position * step

        def value_to_slider(value: float) -> int:
            return int(round((value - low) / step))

        def on_slider(position: int) -> None:
            spin.blockSignals(True)
            spin.setValue(slider_to_value(position))
            spin.blockSignals(False)

        def on_spin(value: float) -> None:
            slider.blockSignals(True)
            slider.setValue(value_to_slider(value))
            slider.blockSignals(False)

        slider.valueChanged.connect(on_slider)
        spin.valueChanged.connect(on_spin)
        row.addWidget(label)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        return row, spin

    def _add_color_row(self, layout: QVBoxLayout, label_text: str) -> tuple[QLabel, QLabel]:
        row = QHBoxLayout()
        label = QLabel(label_text)
        label.setObjectName("fieldLabel")
        label.setMinimumWidth(44)
        swatch = QLabel()
        swatch.setObjectName("colorSwatch")
        swatch.setFixedSize(28, 22)
        text = QLabel("-")
        text.setObjectName("resultText")
        row.addWidget(label)
        row.addWidget(swatch)
        row.addWidget(text, 1)
        layout.addLayout(row)
        return swatch, text

    def _set_swatch(self, swatch: QLabel, text_label: QLabel, rgb: tuple[float, float, float]) -> None:
        swatch.setStyleSheet(
            "border: 1px solid #b8c6d8; border-radius: 5px;"
            f"background: {rgb_to_css(rgb)};"
        )
        text_label.setText(rgb_to_text(rgb))

    def _set_combo_text(self, combo: QComboBox, text: str) -> None:
        index = combo.findText(str(text))
        if index >= 0:
            combo.setCurrentIndex(index)

    def _set_result_panel_visible(self, visible: bool) -> None:
        if hasattr(self, "ai_result_empty"):
            self.ai_result_empty.setVisible(not visible)
        for box in getattr(self, "ai_result_boxes", []):
            box.setVisible(visible)
        if hasattr(self, "btn_save_ai"):
            self.btn_save_ai.setVisible(visible)

    def _apply_checked_defaults(self) -> None:
        if not hasattr(self, "ai_default_material_check"):
            return
        if self.ai_default_material_check.isChecked():
            self._set_combo_text(self.ai_material_combo, "Glossy")
        if self.ai_default_render_check.isChecked():
            self.ai_opacity_spin.setValue(1.00)
            self.ai_specular_spin.setValue(0.45)
            self.ai_shininess_spin.setValue(0.70)
        if self.ai_default_view_check.isChecked():
            self._set_combo_text(self.ai_projection_combo, "Orthographic")
            self.ai_depthcue_check.setChecked(False)
        if self.ai_default_light_check.isChecked():
            for idx, chk in self.ai_light_checks.items():
                chk.setChecked(idx in {"0", "1", "2"})
        if self.ai_default_skeleton_check.isChecked():
            self._set_combo_text(self.ai_skeleton_combo, "CPK")
            self._set_combo_text(self.ai_skeleton_material_combo, "Glossy")

    def _apply_styles(self) -> None:
        style_sheet = (
            """
            QMainWindow, QWidget {
                background: #f3f5f7;
                color: #18212f;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 13px;
            }
            QLabel, QCheckBox { background: transparent; }
            QFrame#heroCard {
                background: #ffffff;
                border: 1px solid #d8e2ed;
                border-radius: 14px;
            }
            QLabel#appTitle { font-size: 22px; font-weight: 700; color: #102a43; }
            QLabel#appSubTitle { font-size: 13px; color: #5d7489; }
            QLabel#countPill {
                background: #eff5ff;
                color: #2359a5;
                border: 1px solid #d8e5f7;
                border-radius: 9px;
                padding: 4px 8px;
                font-weight: 600;
            }

            QFrame#leftPanel, QFrame#rightPanel {
                background: #ffffff;
                border: 1px solid #e2e7ec;
                border-radius: 16px;
            }
            QFrame#sectionCard {
                background: #f8fafc;
                border: 1px solid #edf0f3;
                border-radius: 12px;
            }
            QLabel#sectionCaption {
                font-size: 12px;
                font-weight: 700;
                color: #596579;
                padding-bottom: 2px;
            }
            QFrame#brandCard {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #172033, stop:0.55 #1e3150, stop:1 #1e4b57
                );
                border: 1px solid #273c57;
                border-radius: 14px;
            }
            QLabel#brandMark {
                color: #ffffff;
                background: rgba(255, 255, 255, 28);
                border: 1px solid rgba(255, 255, 255, 48);
                border-radius: 11px;
                font-size: 14px;
                font-weight: 800;
            }
            QLabel#brandTitle { color: #ffffff; font-size: 15px; font-weight: 800; }
            QLabel#brandSubtitle { color: #b8c8dc; font-size: 11px; }
            QPushButton#themeButton {
                min-height: 26px;
                padding: 3px 8px;
                color: #dce8f5;
                background: rgba(255, 255, 255, 18);
                border: 1px solid rgba(255, 255, 255, 38);
                border-radius: 8px;
                font-size: 11px;
                font-weight: 700;
            }
            QPushButton#themeButton:hover,
            QPushButton#themeButton:checked { background: rgba(255, 255, 255, 36); }
            QFrame#pageHeader { background: transparent; border: none; }
            QLabel#pageSubtitle { color: #788596; font-size: 12px; }
            QFrame#filterBar {
                background: #f8fafc;
                border: 1px solid #e7ebef;
                border-radius: 12px;
            }

            QLineEdit, QPlainTextEdit, QTableWidget, QSpinBox {
                border: 1px solid #d6dde5;
                border-radius: 9px;
                padding: 6px 8px;
                min-height: 30px;
                background: #ffffff;
                color: #1b2938;
                selection-background-color: #3977d5;
            }
            QLineEdit:focus, QPlainTextEdit:focus, QTableWidget:focus, QSpinBox:focus {
                border: 1px solid #6b9fe8;
                background: #fcfeff;
            }
            QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled,
            QSpinBox:disabled, QDoubleSpinBox:disabled {
                border-color: #e1e6eb;
                background: #f3f5f7;
                color: #758497;
            }
            QTableWidget {
                gridline-color: #e1e8f0;
                alternate-background-color: #f5f9fd;
            }
            QHeaderView::section {
                background: #f1f4f7;
                color: #455468;
                border: none;
                border-right: 1px solid #e1e6eb;
                border-bottom: 1px solid #d9e0e6;
                padding: 7px;
                font-weight: 700;
            }
            QProgressBar {
                border: 1px solid #c5d5e6;
                border-radius: 8px;
                background: #edf3f9;
                text-align: center;
                min-height: 28px;
            }
            QProgressBar::chunk {
                background: #14a579;
                border-radius: 7px;
            }
            QComboBox {
                border: 1px solid #d6dde5;
                border-radius: 9px;
                padding: 6px 8px;
                min-height: 30px;
                background: #ffffff;
                color: #1b2938;
                selection-background-color: #3977d5;
            }
            QComboBox:focus {
                border: 1px solid #5ea2ec;
                background: #fcfeff;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QDoubleSpinBox {
                border: 1px solid #d6dde5;
                border-radius: 9px;
                padding: 6px 8px;
                min-height: 30px;
                background: #ffffff;
                color: #17334b;
            }
            QDoubleSpinBox:focus {
                border: 1px solid #5ea2ec;
                background: #fcfeff;
            }
            QSlider#valueSlider::groove:horizontal {
                height: 6px;
                border-radius: 3px;
                background: #dbe7f5;
            }
            QSlider#valueSlider::sub-page:horizontal {
                border-radius: 3px;
                background: #2d76dc;
            }
            QSlider#valueSlider::handle:horizontal {
                width: 16px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
                background: #ffffff;
                border: 2px solid #1f6feb;
            }
            QCheckBox {
                spacing: 8px;
                color: #25445e;
                font-weight: 600;
                padding: 5px 6px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 5px;
                border: 2px solid #8fb3dc;
                background: #ffffff;
            }
            QCheckBox::indicator:hover {
                border: 2px solid #1f6feb;
                background: #eef5ff;
            }
            QCheckBox::indicator:checked {
                border: 2px solid #1f6feb;
                background: #1f6feb;
            }

            QPushButton {
                border: 1px solid #d3dbe4;
                border-radius: 9px;
                padding: 7px 10px;
                min-height: 30px;
                background: #ffffff;
                color: #344256;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #f5f8fb;
                border: 1px solid #b9c7d6;
            }
            QPushButton#modeButton {
                min-height: 32px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#modeButton:checked {
                border: 1px solid #7ca8e8;
                background: #edf4ff;
                color: #164f9e;
            }
            QPushButton#navButton {
                min-height: 32px;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#navButton:checked {
                border: 1px solid #2e6dc7;
                background: #316fca;
                color: #ffffff;
            }
            QPushButton#generateBtn {
                border: 1px solid #1f6feb;
                background: #1f6feb;
                color: #ffffff;
                font-weight: 700;
            }
            QPushButton#generateBtn:hover { background: #195fc8; }
            QPushButton#dangerBtn {
                border: 1px solid #f0b8b1;
                background: #fff5f4;
                color: #b42318;
                font-weight: 700;
            }
            QPushButton#dangerBtn:hover {
                border: 1px solid #e07568;
                background: #ffe7e4;
            }
            QPushButton#primaryBtn {
                border: 1px solid #1f6feb;
                background: #1f6feb;
                color: #ffffff;
                font-weight: 700;
            }
            QPushButton#primaryBtn:hover { background: #195fc8; }
            QPushButton#primaryBtn:disabled {
                border: 1px solid #a7bdd8;
                background: #bfd0e5;
                color: #f7fbff;
            }
            QPushButton:disabled,
            QPushButton#dangerBtn:disabled,
            QPushButton#generateBtn:disabled {
                border-color: #dce4eb;
                background: #eef2f6;
                color: #9aaaba;
            }

            QScrollArea {
                border: 1px solid #e3e8ed;
                border-radius: 12px;
                background: #f8fafc;
            }
            QScrollArea#cardGrid,
            QScrollArea#cardGrid > QWidget > QWidget {
                border: none;
                background: transparent;
            }
            QScrollArea#leftScroll {
                border: none;
                border-radius: 0px;
                background: transparent;
            }
            QScrollArea#aiImportScroll {
                border: none;
                border-radius: 0px;
                background: transparent;
            }
            QScrollArea#batchPageScroll,
            QScrollArea#batchPageScroll > QWidget > QWidget {
                border: none;
                border-radius: 0px;
                background: transparent;
            }
            QScrollArea#batchPageScroll QScrollBar:vertical {
                width: 12px;
                margin: 4px 2px;
                border: none;
                background: transparent;
            }
            QScrollArea#batchPageScroll QScrollBar::handle:vertical {
                min-height: 36px;
                border-radius: 5px;
                background: #b6c9dc;
            }
            QScrollArea#batchPageScroll QScrollBar::handle:vertical:hover {
                background: #7fa7cf;
            }
            QScrollArea#batchPageScroll QScrollBar::add-line:vertical,
            QScrollArea#batchPageScroll QScrollBar::sub-line:vertical {
                height: 0px;
                background: transparent;
            }
            QScrollBar:vertical {
                width: 8px;
                margin: 3px 2px;
                border: none;
                background: transparent;
            }
            QScrollBar::handle:vertical {
                min-height: 34px;
                border-radius: 4px;
                background: #d3dbe4;
            }
            QScrollBar::handle:vertical:hover { background: #aab8c6; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
                background: transparent;
            }
            QMenu {
                background: #ffffff;
                color: #273548;
                border: 1px solid #dce3ea;
                border-radius: 8px;
                padding: 6px;
            }
            QMenu::item { padding: 7px 22px 7px 12px; border-radius: 6px; }
            QMenu::item:selected { background: #edf4ff; color: #174f9b; }
            QMenu::separator { height: 1px; background: #e7ebef; margin: 5px 8px; }
            QSplitter::handle { background: #edf0f3; border-radius: 2px; }
            QStatusBar {
                background: transparent;
                color: #748295;
                border: none;
                padding-left: 6px;
            }
            QStatusBar::item { border: none; }
            QToolTip {
                background: #24364a;
                color: #ffffff;
                border: 1px solid #344c66;
                border-radius: 6px;
                padding: 5px 7px;
            }
            QFrame#stylePane {
                background: #ffffff;
                border: 1px solid #e4e9ee;
                border-radius: 14px;
            }
            QFrame#importIntro {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #172136, stop:0.55 #203b5f, stop:1 #285b65
                );
                border: 1px solid #2e4e68;
                border-radius: 16px;
            }
            QLabel#kickerLabel {
                color: #8ec5ff;
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 1.2px;
            }
            QLabel#importTitle {
                color: #ffffff;
                font-size: 22px;
                font-weight: 800;
            }
            QFrame#importIntro QLabel#mutedLabel { color: #c1cfdd; }
            QFrame#batchHero {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #f5f8fc, stop:0.55 #eef4fb, stop:1 #edf7f5
                );
                border: 1px solid #dce5ed;
                border-radius: 16px;
            }
            QLabel#batchHeroTitle {
                color: #1c3047;
                font-size: 19px;
                font-weight: 800;
                letter-spacing: 0.4px;
            }
            QLabel#batchHeroSubtitle { color: #65768a; font-size: 12px; }
            QLabel#batchBadge {
                color: #315d5a;
                background: rgba(255, 255, 255, 220);
                border: 1px solid #cee1df;
                border-radius: 12px;
                padding: 7px 12px;
                font-weight: 700;
            }
            QFrame#batchToolbar {
                background: #ffffff;
                border: 1px solid #e2e7ec;
                border-radius: 12px;
            }
            QLabel#batchToolbarLabel { color: #34536d; font-weight: 700; }
            QLabel#batchPresetInline {
                color: #647c91;
                background: #f4f7fa;
                border: 1px solid #dce5ed;
                border-radius: 9px;
                padding: 4px 8px;
                font-weight: 700;
            }
            QLabel#batchPresetInline[state="draft"] {
                color: #9a5a00;
                background: #fff7e8;
                border-color: #f2cf91;
            }
            QPushButton#batchAddFlowButton {
                min-width: 40px;
                max-width: 40px;
                padding: 5px 0;
                border-color: #1f6feb;
                background: #1f6feb;
                color: #ffffff;
                font-size: 20px;
                font-weight: 700;
            }
            QPushButton#batchAddFlowButton:hover {
                border-color: #195fc8;
                background: #195fc8;
            }
            QPushButton#batchFlowActionButton {
                padding-left: 11px;
                padding-right: 11px;
                background: #f7f9fc;
                border-color: #d8e1ea;
                color: #36516b;
            }
            QFrame#batchCard {
                background: #ffffff;
                border: 1px solid #e1e7ed;
                border-radius: 15px;
            }
            QLabel#batchCardTitle {
                color: #24384d;
                font-size: 15px;
                font-weight: 800;
            }
            QLabel#batchHint { color: #71869a; font-size: 12px; }
            QFrame#batchAdvancedPanel {
                background: #f7fafc;
                border: 1px solid #dce6ef;
                border-radius: 11px;
            }
            QFrame#commonOutputCard {
                background: #f8fbfe;
                border: 1px solid #dce7f0;
                border-radius: 11px;
            }
            QCheckBox#commonOutputCheck {
                color: #284963;
                font-weight: 700;
                spacing: 8px;
            }
            QPlainTextEdit#batchInput {
                background: #fbfdff;
                border: 1px solid #cddbe7;
                border-radius: 10px;
                padding: 9px;
                selection-background-color: #2e75cb;
            }
            QPlainTextEdit#batchInput:focus {
                border: 2px solid #4a86cf;
            }
            QLabel#recorderTitle { color: #1f3448; }
            QLabel#recorderHint { color: #647b90; font-size: 11px; }
            QFrame#recorderFileBar {
                background: #f3f7fb;
                border: 1px solid #dce6ef;
                border-radius: 10px;
            }
            QLabel#recorderFileName { color: #285273; font-weight: 650; }
            QLabel#recorderState {
                color: #49647c;
                background: #e8eef5;
                border-radius: 9px;
                padding: 5px 9px;
            }
            QLabel#batchPresetSummary {
                color: #214f76;
                background: #edf6ff;
                border: 1px solid #cfe2f5;
                border-radius: 11px;
                padding: 11px 12px;
                font-weight: 600;
            }
            QTabWidget#batchWorkspaceTabs::pane {
                border: none;
                background: transparent;
                top: -1px;
            }
            QTabWidget#batchWorkspaceTabs QTabBar::tab {
                min-width: 132px;
                min-height: 24px;
                padding: 9px 20px;
                margin-right: 6px;
                color: #587087;
                background: #eaf0f6;
                border: 1px solid #d6e0ea;
                border-bottom: 2px solid #d6e0ea;
                border-top-left-radius: 11px;
                border-top-right-radius: 11px;
                font-weight: 700;
            }
            QTabWidget#batchWorkspaceTabs QTabBar::tab:hover {
                color: #285b8d;
                background: #f2f7fd;
            }
            QTabWidget#batchWorkspaceTabs QTabBar::tab:selected {
                color: #124f8d;
                background: #ffffff;
                border-color: #b9d2ec;
                border-bottom: 3px solid #2d76dc;
            }
            QLabel#batchRunBadge {
                color: #49657d;
                background: #edf2f7;
                border: 1px solid #d4dee8;
                border-radius: 12px;
                padding: 7px 12px;
                font-weight: 800;
            }
            QLabel#batchRunBadge[state="running"] {
                color: #175ca5;
                background: #eaf3ff;
                border-color: #bad6f4;
            }
            QLabel#batchRunBadge[state="success"] {
                color: #08775c;
                background: #e7f8f2;
                border-color: #a9dfce;
            }
            QLabel#batchRunBadge[state="warning"] {
                color: #9b5c08;
                background: #fff6e6;
                border-color: #efd19b;
            }
            QLabel#batchRunBadge[state="failed"] {
                color: #b42318;
                background: #fff0ee;
                border-color: #efb8b2;
            }
            QStackedWidget#batchResultStack {
                border: none;
                background: transparent;
            }
            QFrame#batchEmptyState {
                background: #f8fbfe;
                border: 1px dashed #c7d8e8;
                border-radius: 14px;
            }
            QLabel#batchEmptyIcon {
                color: #4d8bd0;
                font-size: 36px;
                font-weight: 700;
            }
            QLabel#batchEmptyTitle {
                color: #244963;
                font-size: 18px;
                font-weight: 800;
            }
            QLabel#batchEmptyDescription { color: #6b8296; }
            QLabel#paneTitle { font-size: 14px; font-weight: 700; color: #25445e; }
            QLabel#formLabel { color: #25445e; font-weight: 600; }
            QLabel#fieldLabel { color: #436278; font-weight: 600; }
            QLabel#resultTitle { color: #173854; font-weight: 700; font-size: 14px; }
            QLabel#resultText { color: #25445e; }
            QFrame#resultBox {
                background: #ffffff;
                border: 1px solid #d8e3ef;
                border-radius: 10px;
            }
            QLabel#colorSwatch {
                border: 1px solid #b8c6d8;
                border-radius: 5px;
                background: #ffffff;
            }

            QFrame#styleCard {
                background: #ffffff;
                border: 1px solid #e1e6eb;
                border-radius: 14px;
            }
            QFrame#styleCard:hover {
                border: 1px solid #b5c4d3;
                background: #ffffff;
            }
            QFrame#styleCard[selected="true"] {
                border: 1px solid #3977d5;
                background: #f4f8ff;
            }
            QLabel#cardImage {
                border: none;
                border-radius: 10px;
                background: #f7f9fc;
            }
            QLabel#cardTitle { font-size: 14px; font-weight: 800; color: #24364a; }
            QLabel#cardSubtitle { font-size: 11px; color: #6d7c8e; }
            QLabel#customBadge {
                color: #235fae;
                background: #eaf2ff;
                border: 1px solid #cfe0f7;
                border-radius: 7px;
                padding: 2px 6px;
                font-size: 10px;
                font-weight: 700;
            }
            QLabel#emptyLabel { color: #5f788f; font-size: 13px; padding: 20px; }

            QLabel#sectionTitle { font-size: 20px; font-weight: 800; color: #1d2d40; }
            QLabel#mutedLabel { color: #6d7c8e; }
            QFrame#styleActionBar, QFrame#workflowFooter {
                background: #f8fafc;
                border: 1px solid #dfe7ef;
                border-radius: 12px;
            }
            QLabel#actionSelectionLabel {
                color: #40566d;
                font-weight: 700;
            }
            QFrame#workflowCard {
                background: #ffffff;
                border: 1px solid #dfe7ef;
                border-radius: 14px;
            }
            QLabel#workflowStyleName {
                color: #173854;
                font-size: 16px;
                font-weight: 800;
            }
            QFrame#directDropZone {
                background: #f7fbff;
                border: 2px dashed #9ebcdf;
                border-radius: 14px;
            }
            QFrame#directDropZone:hover,
            QFrame#directDropZone[dragActive="true"] {
                background: #edf6ff;
                border-color: #3977d5;
            }
            QLabel#directDropIcon {
                color: #3977d5;
                font-size: 28px;
                font-weight: 700;
            }
            QLabel#directDropTitle {
                color: #244963;
                font-size: 16px;
                font-weight: 800;
            }
            QFrame#selectedFileCard {
                background: #f2f8ff;
                border: 1px solid #cfe0f2;
                border-radius: 10px;
            }
            QLabel#workflowStatus {
                color: #28587c;
                background: #eef7f4;
                border: 1px solid #cce6dc;
                border-radius: 9px;
                padding: 9px 11px;
                font-weight: 700;
            }
            QFrame#parameterHeader {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #edf5ff, stop:1 #f2fbf8
                );
                border: 1px solid #cfe0ef;
                border-radius: 14px;
            }
            QFrame#parameterCard {
                background: #ffffff;
                border: 1px solid #dfe7ef;
                border-radius: 14px;
            }
            QFrame#parameterColorRow {
                background: #f7fafd;
                border: 1px solid #e1e8ef;
                border-radius: 10px;
            }
            QLabel#parameterSummaryLabel {
                min-width: 76px;
                padding: 10px 8px 10px 0;
                color: #51677c;
                font-weight: 700;
                border-bottom: 1px solid #edf1f5;
            }
            QLabel#parameterSummaryValue {
                padding: 10px 0;
                color: #213b52;
                border-bottom: 1px solid #edf1f5;
            }
            QLabel#dialogTitle {
                color: #173854;
                font-size: 18px;
                font-weight: 800;
            }
            QScrollArea#directWorkflowScroll,
            QScrollArea#directWorkflowScroll > QWidget > QWidget {
                background: transparent;
                border: none;
            }
            QLabel#cropPreview {
                border: 1px solid #d8e3ef;
                border-radius: 10px;
                background: #eef3f8;
            }
            QPlainTextEdit#logView {
                border: 1px solid #20354a;
                border-radius: 10px;
                background: #0f253a;
                color: #d5e5f8;
            }
            QPlainTextEdit#aiResult {
                border: 1px solid #cfd9e4;
                border-radius: 10px;
                background: #ffffff;
                color: #17334b;
                selection-background-color: #2d76dc;
            }
            QPlainTextEdit#batchLog {
                border: 1px solid #20354a;
                background: #0f253a;
                color: #d5e5f8;
            }
            """
        )
        if self.dark_mode:
            style_sheet += """
            QMainWindow, QWidget {
                background: #101722;
                color: #dbe7f5;
            }
            QLabel, QCheckBox { background: transparent; }
            QFrame#leftPanel, QFrame#rightPanel, QFrame#sectionCard,
            QFrame#stylePane, QFrame#resultBox, QFrame#batchCard,
            QFrame#batchToolbar, QFrame#workflowCard,
            QFrame#styleActionBar, QFrame#workflowFooter {
                background: #172233;
                border-color: #2d4058;
            }
            QFrame#filterBar {
                background: #141f2e;
                border-color: #293b50;
            }
            QLabel#pageSubtitle { color: #8fa2b6; }
            QFrame#brandCard {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #101b2d, stop:0.55 #18304e, stop:1 #17434a
                );
                border-color: #314d67;
            }
            QFrame#batchHero {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #17324a, stop:0.5 #242c4b, stop:1 #143d3b
                );
                border-color: #35506d;
            }
            QLabel#batchHeroTitle { color: #eef7ff; }
            QLabel#batchHeroSubtitle { color: #adc0d4; }
            QLabel#batchBadge {
                color: #a9eee2;
                background: rgba(14, 28, 43, 205);
                border-color: #386c69;
            }
            QLabel#batchToolbarLabel, QLabel#batchCardTitle { color: #e5eef9; }
            QLabel#batchPresetInline {
                color: #b4c5d7;
                background: #1b2a3d;
                border-color: #344a63;
            }
            QLabel#batchPresetInline[state="draft"] {
                color: #ffd695;
                background: #3b3020;
                border-color: #725b32;
            }
            QLabel#batchHint { color: #9fb2c6; }
            QPushButton#batchAddFlowButton {
                background: #3979cf;
                border-color: #4a8ee8;
                color: #ffffff;
            }
            QPushButton#batchAddFlowButton:hover {
                background: #4388df;
                border-color: #62a0ea;
            }
            QPushButton#batchFlowActionButton {
                background: #1d2d42;
                border-color: #344b66;
                color: #dce8f5;
            }
            QPushButton#batchFlowActionButton:hover {
                background: #29415e;
                border-color: #4a6a8d;
            }
            QFrame#batchAdvancedPanel {
                background: #112536;
                border-color: #29445a;
            }
            QFrame#commonOutputCard {
                background: #132638;
                border-color: #2b455b;
            }
            QCheckBox#commonOutputCheck { color: #dce9f5; }
            QPlainTextEdit#batchInput {
                color: #e4edf6;
                background: #102435;
                border-color: #2d4a60;
            }
            QPlainTextEdit#batchInput:focus { border-color: #5c99d3; }
            QLabel#recorderTitle { color: #edf4fb; }
            QLabel#recorderHint { color: #9fb2c6; }
            QFrame#recorderFileBar {
                background: #13293c;
                border-color: #29465d;
            }
            QLabel#recorderFileName { color: #c5ddf1; }
            QLabel#recorderState {
                color: #b7ccde;
                background: #203b50;
            }
            QLabel#batchPresetSummary {
                color: #cde6ff;
                background: #182f45;
                border-color: #31516f;
            }
            QTabWidget#batchWorkspaceTabs QTabBar::tab {
                color: #9eb1c5;
                background: #172334;
                border-color: #2c4058;
            }
            QTabWidget#batchWorkspaceTabs QTabBar::tab:hover {
                color: #cfe5fb;
                background: #21334a;
            }
            QTabWidget#batchWorkspaceTabs QTabBar::tab:selected {
                color: #eaf5ff;
                background: #202e41;
                border-color: #3b5a78;
                border-bottom-color: #62a0ea;
            }
            QScrollArea, QScrollArea > QWidget > QWidget {
                background: #101722;
            }
            QScrollArea#batchPageScroll,
            QScrollArea#batchPageScroll > QWidget > QWidget {
                background: transparent;
            }
            QScrollArea#batchPageScroll QScrollBar::handle:vertical {
                background: #405873;
            }
            QScrollArea#batchPageScroll QScrollBar::handle:vertical:hover {
                background: #5c7da1;
            }
            QScrollBar::handle:vertical { background: #40536a; }
            QScrollBar::handle:vertical:hover { background: #5a718b; }
            QFrame#batchEmptyState {
                background: #111c2a;
                border-color: #36506b;
            }
            QLabel#batchEmptyIcon { color: #78b2f5; }
            QLabel#batchEmptyTitle { color: #e5eef9; }
            QLabel#batchEmptyDescription { color: #9fb2c6; }
            QLabel#actionSelectionLabel, QLabel#workflowStyleName,
            QLabel#directDropTitle { color: #e5eef9; }
            QFrame#parameterHeader {
                background: #172b40;
                border-color: #31516f;
            }
            QFrame#parameterCard {
                background: #172233;
                border-color: #2d4058;
            }
            QFrame#parameterColorRow {
                background: #111c2a;
                border-color: #30465f;
            }
            QLabel#parameterSummaryLabel {
                color: #9fb4c9;
                border-bottom-color: #2a3b4f;
            }
            QLabel#parameterSummaryValue {
                color: #dfebf7;
                border-bottom-color: #2a3b4f;
            }
            QLabel#dialogTitle { color: #eef6ff; }
            QFrame#directDropZone {
                background: #111c2a;
                border-color: #405f7e;
            }
            QFrame#directDropZone:hover,
            QFrame#directDropZone[dragActive="true"] {
                background: #182f45;
                border-color: #62a0ea;
            }
            QFrame#selectedFileCard {
                background: #172b40;
                border-color: #31516f;
            }
            QLabel#workflowStatus {
                color: #c8eee3;
                background: #17352f;
                border-color: #32665a;
            }
            QLineEdit, QComboBox, QDoubleSpinBox, QPlainTextEdit,
            QTableWidget, QSpinBox {
                background: #0e1825;
                color: #e4edf8;
                border-color: #3a506b;
                selection-background-color: #3979cf;
            }
            QTableWidget { gridline-color: #2d4058; alternate-background-color: #132033; }
            QHeaderView::section {
                background: #223249;
                color: #e4edf8;
                border-color: #3b526f;
            }
            QProgressBar { background: #0e1825; color: #e4edf8; border-color: #3a506b; }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {
                border-color: #62a0ea;
            }
            QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled,
            QSpinBox:disabled, QDoubleSpinBox:disabled {
                background: #172231;
                color: #7890a8;
                border-color: #2e4157;
            }
            QPushButton {
                background: #223249;
                color: #e4edf8;
                border-color: #3b526f;
            }
            QPushButton:hover { background: #2b405d; }
            QPushButton#primaryBtn, QPushButton#generateBtn {
                background: #3979cf;
                border-color: #4a8ee8;
                color: white;
            }
            QPushButton:disabled,
            QPushButton#dangerBtn:disabled,
            QPushButton#generateBtn:disabled {
                background: #182433;
                color: #64778b;
                border-color: #293b50;
            }
            QPushButton#modeButton:checked {
                background: #203d5f;
                border-color: #4f7fb5;
                color: #e7f2ff;
            }
            QPushButton#navButton:checked {
                background: #316fca;
                border-color: #4a85d8;
                color: white;
            }
            QFrame#styleCard {
                background: #192638;
                border-color: #334a65;
            }
            QFrame#styleCard:hover { border-color: #62a0ea; }
            QFrame#styleCard[selected="true"] {
                background: #203a59;
                border-color: #78b2f5;
            }
            QLabel#cardTitle, QLabel#paneTitle, QLabel#sectionTitle,
            QLabel#sectionCaption, QLabel#formLabel { color: #e5eef9; }
            QLabel#cardSubtitle, QLabel#helperText,
            QLabel#mutedLabel { color: #a9bbcf; }
            QLabel#customBadge {
                color: #b9d9ff;
                background: #203b5d;
                border-color: #365f8e;
            }
            QLabel#cropPreview, QLabel#cardImage {
                background: #0d1622;
                border-color: #34485f;
            }
            QSplitter::handle { background: #26384e; }
            QStatusBar { color: #8fa2b6; }
            QMenu {
                background: #172334;
                color: #e4edf8;
                border-color: #354b64;
            }
            QMenu::item:selected { background: #243b58; color: #ffffff; }
            QMenu::separator { background: #31445b; }
            QToolTip {
                background: #23364c;
                color: white;
                border: 1px solid #4a6685;
            }
            """
        self.setStyleSheet(style_sheet)

    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        self.statusBar().showMessage(text, 5000)

    def _animate_stack_page(self) -> None:
        page = self.stack.currentWidget()
        if page is None:
            return
        if self._page_animation is not None:
            self._page_animation.stop()
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(240)
        animation.setStartValue(0.28)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(lambda p=page: p.setGraphicsEffect(None))
        self._page_animation = animation
        animation.start()

    def _save_batch_settings(self, payload: dict) -> None:
        core.save_config(dict(payload))

    def _toggle_theme(self, checked: bool | None = None) -> None:
        self.dark_mode = bool(self.theme_btn.isChecked() if checked is None else checked)
        self.theme_btn.setChecked(self.dark_mode)
        self.theme_btn.setText("浅色" if self.dark_mode else "深色")
        self._apply_styles()
        core.save_config({"theme": "dark" if self.dark_mode else "light"})

    def _refresh_material_filter(self) -> None:
        current = str(self.material_filter_combo.currentData() or "")
        materials = sorted(
            {str(style.get("material") or "未指定") for style in self.bundle_styles}
        )
        self.material_filter_combo.blockSignals(True)
        self.material_filter_combo.clear()
        self.material_filter_combo.addItem("全部材质", "")
        for material in materials:
            self.material_filter_combo.addItem(material, material)
        index = self.material_filter_combo.findData(current)
        self.material_filter_combo.setCurrentIndex(index if index >= 0 else 0)
        self.material_filter_combo.blockSignals(False)

    def _matches_style_search(self, style: dict, query: str) -> bool:
        if not query:
            return True
        haystack = " ".join(
            [
                str(style.get("name") or ""),
                str(style.get("notes") or ""),
                str(style.get("material") or ""),
                " ".join(str(x) for x in style.get("sources", [])),
            ]
        ).casefold()
        return all(part in haystack for part in query.casefold().split())

    def _style_stack_index(self) -> int:
        return 1 if self.mode == "split" else 0

    def _set_page_chrome(self, page: str, title: str, subtitle: str) -> None:
        self.main_title.setText(title)
        self.main_subtitle.setText(subtitle)
        self.page_header.setVisible(page not in {"batch", "automation"})
        showing_styles = page == "styles"
        self.filter_bar.setVisible(showing_styles)
        self.count_label.setVisible(showing_styles)
        self.style_action_bar.setVisible(showing_styles)
        self.style_mode_section.setVisible(showing_styles)
        self.generate_shortcut.setEnabled(showing_styles)
        self.direct_shortcut.setEnabled(showing_styles)
        self.nav_style_btn.setChecked(page in {"styles", "direct"})
        self.nav_custom_btn.setChecked(page == "custom")
        self.nav_automation_btn.setChecked(page == "automation")
        self.nav_batch_btn.setChecked(page == "batch")
        if not showing_styles:
            self.delete_custom_btn.hide()

    def _show_style_selection(self) -> None:
        self._set_page_chrome(
            "styles",
            "绘图方案",
            "选择整套风格，或分别组合骨架与等值面风格",
        )
        self.stack.setCurrentIndex(self._style_stack_index())
        self._animate_stack_page()
        self._update_detail()

    def _show_custom_import(self) -> None:
        self._set_page_chrome(
            "custom", "自定义风格", "从 Save State 或参考图片建立自己的视觉预设"
        )
        self.stack.setCurrentIndex(self.custom_page_index)
        self._animate_stack_page()
        self._sync_delete_custom_button()

    def _show_batch_page(self) -> None:
        self._set_page_chrome(
            "batch", "批量 Multiwfn", "记录或导入一次操作流程，再应用到整批计算文件"
        )
        self.stack.setCurrentIndex(self.batch_page_index)
        self._animate_stack_page()

    def _show_automation_page(self) -> None:
        self._set_page_chrome(
            "automation",
            "全自动流程",
            "选择完整流程，由软件依次完成计算、校验、绘图与结果整理",
        )
        self.stack.setCurrentIndex(self.automation_page_index)
        self._animate_stack_page()

    def _show_direct_workflow(self) -> None:
        try:
            style, rep0_commands, selection_text = self._current_style_selection()
        except ValueError as exc:
            QMessageBox.warning(self, "无法开始直接绘图", str(exc))
            return
        self.direct_page.configure_style(style, rep0_commands, selection_text)
        self._set_page_chrome(
            "direct",
            "直接绘图",
            "添加一个本地文件，由软件接管 Multiwfn、Cube 检测和 VMD 启动流程",
        )
        self.stack.setCurrentIndex(self.direct_page_index)
        self._animate_stack_page()

    def _set_import_page(self, index: int) -> None:
        index = 1 if index == 1 else 0
        self.import_state_btn.setChecked(index == 0)
        self.import_ai_btn.setChecked(index == 1)
        self.import_stack.setCurrentIndex(index)

    def _current_ai_provider(self) -> str:
        data = self.ai_provider_combo.currentData()
        return str(data or "openai")

    def _toggle_ai_key_visibility(self, visible: bool) -> None:
        self.ai_key_edit.setEchoMode(QLineEdit.Normal if visible else QLineEdit.Password)
        self.ai_key_toggle.setText("隐藏" if visible else "显示")

    def _on_ai_provider_changed(self) -> None:
        provider = self._current_ai_provider()
        current_model = self.ai_model_edit.text().strip()
        known_defaults = {"gpt-4.1-mini", "gemini-3.5-flash"}
        if provider == "gemini":
            self.ai_key_edit.setPlaceholderText("GEMINI_API_KEY")
            if not current_model or current_model in known_defaults:
                self.ai_model_edit.setText(
                    core.os.environ.get("GEMINI_VMD_STYLE_MODEL", "gemini-3.5-flash")
                )
        else:
            self.ai_key_edit.setPlaceholderText("OPENAI_API_KEY")
            if not current_model or current_model in known_defaults:
                self.ai_model_edit.setText(
                    core.os.environ.get("OPENAI_VMD_STYLE_MODEL", "gpt-4.1-mini")
                )

    def _set_mode(self, mode: str) -> None:
        self.mode = "split" if mode == "split" else "bundle"
        self.mode_bundle_btn.setChecked(self.mode == "bundle")
        self.mode_split_btn.setChecked(self.mode == "split")
        self._refresh_lists()
        self._sync_output_name()
        self._show_style_selection()

    def _subtitle_iso(self, st: dict) -> str:
        if st.get("surface_mode") == "volume_mapped":
            method = st.get("color_scale_method", "BWR")
            low = float(st.get("color_scale_min", -0.03))
            high = float(st.get("color_scale_max", 0.03))
            draw = {
                0: "Solid Surface",
                1: "Wireframe",
                2: "Points",
                3: "Shaded Points",
            }.get(int(st.get("surface_draw", 0)), "Solid Surface")
            return f"{st.get('material', 'Glossy')} | ESP {method} {low:g}～{high:g} | {draw}"
        pos = st.get("pos_color_expr", f"ColorID {st.get('pos_color', 1)}")
        neg = st.get("neg_color_expr", f"ColorID {st.get('neg_color', 0)}")
        return f"{st.get('material', 'Glossy')} | {pos}/{neg}"

    def _subtitle_skeleton(self, st: dict) -> str:
        return st.get("notes", "分子骨架样式")

    def _refresh_lists(self) -> None:
        query = self.style_search_edit.text().strip()
        material = str(self.material_filter_combo.currentData() or "")
        visible_bundle = [
            style
            for style in self.bundle_styles
            if (not material or str(style.get("material") or "") == material)
            and self._matches_style_search(style, query)
        ]
        visible_skeleton = [
            style
            for style in self.skeleton_styles
            if self._matches_style_search(style, query)
        ]
        sort_mode = str(self.style_sort_combo.currentData() or "default")
        if sort_mode == "name":
            visible_bundle.sort(key=lambda style: str(style.get("name") or "").casefold())
            visible_skeleton.sort(key=lambda style: str(style.get("name") or "").casefold())
        elif sort_mode == "material":
            visible_bundle.sort(
                key=lambda style: (
                    str(style.get("material") or "").casefold(),
                    str(style.get("name") or "").casefold(),
                )
            )
        elif sort_mode == "custom":
            visible_bundle.sort(
                key=lambda style: (
                    not bool(style.get("is_custom")),
                    str(style.get("name") or "").casefold(),
                )
            )
        self.selected_bundle_id = self.bundle_grid.load_styles(
            visible_bundle, self.selected_bundle_id, self._subtitle_iso
        )
        self.selected_iso_id = self.iso_grid.load_styles(
            visible_bundle, self.selected_iso_id, self._subtitle_iso
        )
        self.selected_skeleton_id = self.skeleton_grid.load_styles(
            visible_skeleton, self.selected_skeleton_id, self._subtitle_skeleton
        )
        if self.mode == "split":
            self.count_label.setText(
                f"骨架 {len(visible_skeleton)}/{len(self.skeleton_styles)} · "
                f"等值面 {len(visible_bundle)}/{len(self.bundle_styles)}"
            )
        elif len(visible_bundle) == len(self.bundle_styles):
            self.count_label.setText(f"{len(visible_bundle)} 个风格")
        else:
            self.count_label.setText(
                f"显示 {len(visible_bundle)}/{len(self.bundle_styles)} 个风格"
            )
        self._sync_output_name()
        self._update_detail()

    def _refresh_styles(self) -> None:
        self.bundle_styles = core.get_all_bundle_styles()
        self.skeleton_styles = list(core.SKELETON_STYLES)
        self.bundle_map = {s["id"]: s for s in self.bundle_styles}
        self.skeleton_map = {s["id"]: s for s in self.skeleton_styles}
        self._refresh_material_filter()

        if self.selected_bundle_id not in self.bundle_map and self.bundle_styles:
            self.selected_bundle_id = (
                core.DEFAULT_STYLE_ID
                if core.DEFAULT_STYLE_ID in self.bundle_map
                else self.bundle_styles[0]["id"]
            )
        if self.selected_iso_id not in self.bundle_map and self.bundle_styles:
            self.selected_iso_id = (
                core.DEFAULT_STYLE_ID
                if core.DEFAULT_STYLE_ID in self.bundle_map
                else self.bundle_styles[0]["id"]
            )
        if self.selected_skeleton_id not in self.skeleton_map and self.skeleton_styles:
            self.selected_skeleton_id = self.skeleton_styles[0]["id"]
        self._refresh_lists()

    def _load_initial(self) -> None:
        conf = core.load_config()
        self.multi_edit.setText(conf.get("multiwfn_exe", ""))
        self.vmd_edit.setText(conf.get("vmd_exe", ""))
        self.out_dir_edit.setText(conf.get("output_dir", str(core.ROOT)))
        self.dark_mode = conf.get("theme", "light") == "dark"
        self.theme_btn.setChecked(self.dark_mode)
        self.theme_btn.setText("浅色" if self.dark_mode else "深色")
        self._apply_styles()
        self.selected_bundle_id = conf.get("last_style", "")
        self.selected_iso_id = conf.get("last_iso_style", "")
        self.selected_skeleton_id = conf.get("last_skeleton", "")
        self._refresh_styles()
        self._set_mode(conf.get("mode", "bundle"))
        self.batch_page.load_settings(conf)
        self.automation_page.load_settings(conf)
        if core.CUSTOM_STYLES_LOAD_ERROR:
            self._log(core.CUSTOM_STYLES_LOAD_ERROR)

    def _style_name_by_id(self, style_id: str, default: str = "Style") -> str:
        style = self.bundle_map.get(style_id)
        return style["name"] if style else default

    def _skeleton_name_by_id(self, skeleton_id: str, default: str = "Skeleton") -> str:
        style = self.skeleton_map.get(skeleton_id)
        return style["name"] if style else default

    def _current_style_selection(
        self,
    ) -> tuple[dict, list[str] | None, str]:
        if self.mode == "split":
            skeleton = self.skeleton_map.get(self.selected_skeleton_id)
            iso_style = self.bundle_map.get(self.selected_iso_id)
            if skeleton is None or iso_style is None:
                raise ValueError("拆分模式需要同时选择骨架样式和等值面样式。")
            style = core.compose_combo_style(skeleton, iso_style)
            rep0_commands = list(skeleton.get("rep0_commands", []))
            selection_text = f"骨架：{skeleton['name']} · 等值面：{iso_style['name']}"
            return style, rep0_commands, selection_text

        style = self.bundle_map.get(self.selected_bundle_id)
        if style is None:
            raise ValueError("请先选择一个绘图风格。")
        rep0_commands = style.get("rep0_commands") or None
        return style, rep0_commands, f"套装风格：{style['name']}"

    def _auto_output_name(self) -> str:
        if self.mode == "split":
            iso_name = clean_name_for_file(self._style_name_by_id(self.selected_iso_id, "Iso"))
            return f"{iso_name}.cmd"
        bundle_name = clean_name_for_file(self._style_name_by_id(self.selected_bundle_id, "Style"))
        return f"{bundle_name}.cmd"

    def _sync_output_name(self) -> None:
        self.out_edit.setText(self._auto_output_name())

    def _current_custom_style_for_delete(self) -> dict | None:
        if self.stack.currentIndex() not in {0, 1}:
            return None
        style_id = self.selected_iso_id if self.mode == "split" else self.selected_bundle_id
        style = self.bundle_map.get(style_id)
        if style and style.get("is_custom"):
            return style
        return None

    def _sync_delete_custom_button(self) -> None:
        style = self._current_custom_style_for_delete()
        self.delete_custom_btn.setVisible(style is not None)

    def _update_detail(self) -> None:
        if self.stack.currentIndex() == self.batch_page_index:
            self._sync_delete_custom_button()
            return
        if self.stack.currentIndex() == self.custom_page_index:
            self._sync_delete_custom_button()
            return
        if self.mode == "split":
            sk = self._skeleton_name_by_id(self.selected_skeleton_id, "Skeleton")
            iso = self._style_name_by_id(self.selected_iso_id, "Iso")
            self.action_selection_label.setText(f"骨架：{sk}\n等值面：{iso}")
        else:
            bundle = self._style_name_by_id(self.selected_bundle_id, "Style")
            self.action_selection_label.setText(f"当前选择：{bundle}")
        self._sync_delete_custom_button()

    def _delete_selected_custom_style(self) -> None:
        style = self._current_custom_style_for_delete()
        if not style:
            QMessageBox.information(self, "无法删除", "请选择一个自定义风格。")
            self._sync_delete_custom_button()
            return

        style_id = str(style.get("id") or "")
        style_name = str(style.get("name") or style_id)
        answer = QMessageBox.question(
            self,
            "删除自定义风格",
            f"确定删除自定义风格“{style_name}”吗？\n此操作不会删除内置风格。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        removed = core.delete_custom_style(style_id)
        if removed is None:
            QMessageBox.information(self, "删除失败", "没有找到这个自定义风格，列表将刷新。")
        else:
            self._log(f"已删除自定义风格：{style_name}")

        if self.selected_bundle_id == style_id:
            self.selected_bundle_id = ""
        if self.selected_iso_id == style_id:
            self.selected_iso_id = ""
        self._refresh_styles()
        self._show_style_selection()
        if removed is not None:
            QMessageBox.information(self, "删除成功", f"已删除：{style_name}")

    def _on_bundle_picked(self, style_id: str) -> None:
        self.selected_bundle_id = style_id
        if self.mode == "bundle":
            self._sync_output_name()
        self._update_detail()

    def _on_iso_picked(self, style_id: str) -> None:
        self.selected_iso_id = style_id
        if self.mode == "split":
            self._sync_output_name()
        self._update_detail()

    def _on_skeleton_picked(self, skeleton_id: str) -> None:
        self.selected_skeleton_id = skeleton_id
        if self.mode == "split":
            self._sync_output_name()
        self._update_detail()

    def _show_selected_style_parameters(self) -> None:
        try:
            style, rep0_commands, selection_text = self._current_style_selection()
        except ValueError as exc:
            QMessageBox.warning(self, "无法查看参数", str(exc))
            return
        dialog = StyleParameterDialog(style, rep0_commands, selection_text, self)
        dialog.exec()
        if dialog.saved_style is None:
            return
        try:
            core.upsert_custom_style(dialog.saved_style)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "无法保存风格", str(exc))
            return
        saved_id = str(dialog.saved_style["id"])
        saved_name = str(dialog.saved_style["name"])
        was_split_mode = self.mode == "split"
        self.selected_bundle_id = saved_id
        self.selected_iso_id = saved_id
        self._refresh_styles()
        if was_split_mode:
            self._set_mode("bundle")
        else:
            self._show_style_selection()
        self._log(f"已保存风格参数：{saved_name}")
        QMessageBox.information(self, "保存成功", f"已保存到自定义风格：{saved_name}")

    def _export_script_dialog(self) -> None:
        try:
            self._current_style_selection()
        except ValueError as exc:
            QMessageBox.warning(self, "无法导出脚本", str(exc))
            return
        suggested_dir = Path(self.out_dir_edit.text().strip() or core.ROOT).expanduser()
        suggested = suggested_dir / self._auto_output_name()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出当前风格脚本",
            str(suggested),
            "Windows 命令脚本 (*.cmd);;所有文件 (*)",
        )
        if not path:
            return
        selected = Path(path)
        self.out_dir_edit.setText(str(selected.parent))
        self.out_edit.setText(selected.name)
        self._generate_script()

    def _pick_multi(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Multiwfn.exe", "", "Executable (*.exe);;All Files (*)"
        )
        if path:
            self.multi_edit.setText(path)

    def _pick_vmd(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 vmd.exe", "", "Executable (*.exe);;All Files (*)"
        )
        if path:
            self.vmd_edit.setText(path)

    def _pick_state_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 VMD Save State 文件",
            "",
            "State/Tcl (*.vmd *.tcl *.txt);;All Files (*)",
        )
        if path:
            self.state_file_edit.setText(path)

    def _pick_cover_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择封面图",
            "",
            "Image (*.png *.jpg *.jpeg *.webp *.gif);;All Files (*)",
        )
        if path:
            self.cover_file_edit.setText(path)

    def _cleanup_ai_temp_files(self, keep: Path | None = None) -> None:
        keep_resolved = keep.resolve() if keep and keep.exists() else None
        remaining: set[Path] = set()
        for path in self.ai_temp_files:
            try:
                if keep_resolved is not None and path.resolve() == keep_resolved:
                    remaining.add(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                remaining.add(path)
        self.ai_temp_files = remaining

    def _pick_ai_image(self) -> None:
        if self.ai_thread is not None and self.ai_thread.isRunning():
            QMessageBox.information(self, "识别进行中", "请等待当前识别完成后再更换图片。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择论文图片",
            "",
            "Image (*.png *.jpg *.jpeg *.webp *.gif);;All Files (*)",
        )
        if not path:
            return
        self._cleanup_ai_temp_files()
        try:
            self.ai_crop_label.load_image(path)
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", str(exc))
            return
        self.ai_image_edit.setText(path)
        self.ai_current_guess = None
        self._set_result_panel_visible(False)
        self.ai_effective_image_path = ""
        self._update_ai_crop_status()

    def _reset_ai_crop(self) -> None:
        self._cleanup_ai_temp_files()
        self.ai_crop_label.reset_crop()
        self.ai_effective_image_path = ""

    def _update_ai_crop_status(self) -> None:
        if self.ai_crop_label.has_crop():
            self.ai_crop_status.setText("已裁剪")
        else:
            self.ai_crop_status.setText("未裁剪")

    def _current_ai_image_for_api(self) -> Path:
        image_path = Path(self.ai_image_edit.text().strip())
        if not image_path.exists():
            raise ValueError("请先选择有效的图片。")
        if not self.ai_crop_label.has_crop():
            return image_path
        tmp = tempfile.NamedTemporaryFile(
            prefix="autocube_ai_style_", suffix=".png", delete=False
        )
        tmp_path = Path(tmp.name)
        tmp.close()
        saved = self.ai_crop_label.save_effective_image(tmp_path)
        self.ai_temp_files.add(saved)
        return saved

    def _cluster_colors(self, pixels: list[tuple[int, int, int]]) -> list[dict]:
        clusters: list[dict] = []
        threshold = 58
        for r, g, b in pixels:
            best = None
            best_dist = None
            for cluster in clusters:
                cr, cg, cb = cluster["center"]
                dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = cluster
            if best is None or best_dist is None or best_dist > threshold**2:
                clusters.append(
                    {"sum": [r, g, b], "count": 1, "center": (float(r), float(g), float(b))}
                )
                continue
            best["count"] += 1
            best["sum"][0] += r
            best["sum"][1] += g
            best["sum"][2] += b
            count = best["count"]
            best["center"] = (
                best["sum"][0] / count,
                best["sum"][1] / count,
                best["sum"][2] / count,
            )

        for cluster in clusters:
            r, g, b = cluster["center"]
            h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            cluster["hsv"] = (h, s, v)
            cluster["rgb01"] = (clamp01(r / 255.0), clamp01(g / 255.0), clamp01(b / 255.0))
            cluster["score"] = cluster["count"] * (0.65 + s) * (0.55 + v)
        clusters.sort(key=lambda item: item["score"], reverse=True)
        return clusters

    def _measure_image_style(self, image_path: Path) -> dict:
        image = QImage(str(image_path))
        if image.isNull():
            raise ValueError("无法读取图片用于测色。")
        scaled = image.scaled(260, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        w, h = scaled.width(), scaled.height()
        edge = max(4, min(w, h) // 28)
        colored: list[tuple[int, int, int]] = []
        bg: list[tuple[int, int, int]] = []

        for y in range(h):
            for x in range(w):
                color = scaled.pixelColor(x, y)
                if color.alpha() < 150:
                    continue
                r, g, b = color.red(), color.green(), color.blue()
                hval, sval, vval = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                if sval < 0.08 and vval > 0.82:
                    bg.append((r, g, b))
                    continue
                if sval < 0.20 or vval < 0.32 or vval > 0.98:
                    continue
                near_edge = x < edge or y < edge or x >= w - edge or y >= h - edge
                if near_edge and sval > 0.35:
                    continue
                if y > h * 0.72 and vval < 0.55:
                    continue
                colored.append((r, g, b))

        clusters = self._cluster_colors(colored)
        surface_clusters = [
            c
            for c in clusters
            if c["count"] >= max(12, len(colored) * 0.015)
        ]

        defaults = {
            "positive_color_rgb": (0.48, 0.76, 0.42),
            "negative_color_rgb": (0.06, 0.72, 0.88),
            "skeleton_color_rgb": (0.70, 0.56, 0.36),
            "background_rgb": (1.0, 1.0, 1.0),
        }
        if surface_clusters:
            defaults["positive_color_rgb"] = surface_clusters[0]["rgb01"]
        if len(surface_clusters) > 1:
            first = surface_clusters[0]["rgb01"]
            for cluster in surface_clusters[1:]:
                rgb = cluster["rgb01"]
                dist = sum((rgb[i] - first[i]) ** 2 for i in range(3))
                if dist > 0.025:
                    defaults["negative_color_rgb"] = rgb
                    break

        skeleton_candidates = []
        for cluster in clusters:
            r, g, b = cluster["rgb01"]
            hue, sat, val = cluster["hsv"]
            warm = 0.04 <= hue <= 0.18 and r >= g >= b * 0.65
            tan = (
                r > 0.45
                and g > 0.30
                and b < 0.45
                and r >= g * 0.85
                and r > b * 1.15
                and g > b * 1.05
                and sat > 0.18
            )
            if warm or tan:
                skeleton_candidates.append(cluster)
        if skeleton_candidates:
            defaults["skeleton_color_rgb"] = skeleton_candidates[0]["rgb01"]

        if bg:
            sample = bg[:: max(1, len(bg) // 1200)]
            defaults["background_rgb"] = tuple(
                clamp01(sum(px[i] for px in sample) / len(sample) / 255.0)
                for i in range(3)
            )
        return defaults

    def _base_guess_from_measurement(self, measured: dict, style_name: str) -> dict:
        return {
            "style_name": style_name or "图片风格",
            "style_summary": "由图片测色得到的基础 VMD 风格。",
            "material": "Glossy",
            "positive_color_rgb": measured["positive_color_rgb"],
            "negative_color_rgb": measured["negative_color_rgb"],
            "background_rgb": measured["background_rgb"],
            "skeleton_color_rgb": measured["skeleton_color_rgb"],
            "projection": "Orthographic",
            "depthcue": False,
            "ambient": 0.10,
            "diffuse": 0.82,
            "specular": 0.45,
            "shininess": 0.70,
            "mirror": 0.0,
            "opacity": 1.0,
            "outline": 0.0,
            "outline_width": 0.0,
            "lights": {"0": True, "1": True, "2": True, "3": False},
            "skeleton_style": "CPK",
            "skeleton_material": "Glossy",
            "confidence": 0.62,
            "uncertain_fields": ["isovalue", "material"],
        }

    def _merge_ai_with_measurement(self, ai_guess: dict, measured: dict, style_name: str) -> dict:
        guess = self._base_guess_from_measurement(measured, style_name)
        try:
            normalized = core.normalize_ai_style_guess(ai_guess)
        except Exception:
            normalized = {}

        material = normalized.get("material", "")
        if material in core.VMD_MATERIALS:
            guess["material"] = material
        material_presets = {
            "Glass1": {
                "ambient": 0.30,
                "diffuse": 0.60,
                "specular": 0.50,
                "shininess": 1.00,
                "opacity": 0.60,
            },
            "Glass2": {
                "ambient": 0.20,
                "diffuse": 0.55,
                "specular": 0.62,
                "shininess": 0.90,
                "opacity": 0.50,
            },
            "Glass3": {
                "ambient": 0.15,
                "diffuse": 0.50,
                "specular": 0.70,
                "shininess": 0.85,
                "opacity": 0.42,
            },
            "Transparent": {
                "ambient": 0.10,
                "diffuse": 0.55,
                "specular": 0.45,
                "shininess": 0.75,
                "opacity": 0.55,
            },
            "Translucent": {
                "ambient": 0.15,
                "diffuse": 0.65,
                "specular": 0.35,
                "shininess": 0.65,
                "opacity": 0.65,
            },
            "EdgyGlass": {
                "diffuse": 0.80,
                "specular": 0.25,
                "shininess": 0.80,
                "opacity": 0.73,
                "outline": 0.59,
                "outline_width": 0.34,
            },
        }
        if material in material_presets:
            guess.update(material_presets[material])

        for key in ("style_summary", "skeleton_style", "skeleton_material"):
            if normalized.get(key):
                guess[key] = normalized[key]
        guess["confidence"] = min(0.95, max(0.70, float(normalized.get("confidence", 0.72))))
        uncertain = {str(field).strip() for field in normalized.get("uncertain_fields", [])}
        if "render_material" in uncertain:
            uncertain.add("material")
        if "light" in uncertain:
            uncertain.add("lights")
        if "camera" in uncertain:
            uncertain.add("projection")
        uncertain.update({"isovalue", "lights"})
        guess["uncertain_fields"] = sorted(uncertain)
        return core.normalize_ai_style_guess(guess)

    def _display_ai_result(self, guess: dict, source: str) -> None:
        guess = core.normalize_ai_style_guess(guess)
        self.ai_current_guess = guess
        self._set_result_panel_visible(True)
        self._set_swatch(self.ai_pos_swatch, self.ai_pos_text, guess["positive_color_rgb"])
        self._set_swatch(self.ai_neg_swatch, self.ai_neg_text, guess["negative_color_rgb"])
        self._set_swatch(self.ai_skeleton_swatch, self.ai_skeleton_text, guess["skeleton_color_rgb"])
        self._set_swatch(self.ai_bg_swatch, self.ai_bg_text, guess["background_rgb"])
        self._set_combo_text(self.ai_material_combo, guess["material"])
        self._set_combo_text(self.ai_projection_combo, guess["projection"])
        self._set_combo_text(self.ai_skeleton_combo, guess["skeleton_style"])
        self._set_combo_text(self.ai_skeleton_material_combo, guess["skeleton_material"])
        self.ai_opacity_spin.setValue(float(guess["opacity"]))
        self.ai_specular_spin.setValue(float(guess["specular"]))
        self.ai_shininess_spin.setValue(float(guess["shininess"]))
        self.ai_depthcue_check.setChecked(bool(guess["depthcue"]))
        for idx, chk in self.ai_light_checks.items():
            chk.setChecked(bool(guess["lights"].get(idx, False)))
        uncertain_fields = set(guess.get("uncertain_fields", []))
        self.ai_default_material_check.setChecked("material" in uncertain_fields)
        self.ai_default_render_check.setChecked(
            bool({"opacity", "specular", "shininess", "material"} & uncertain_fields)
        )
        self.ai_default_view_check.setChecked(
            bool({"projection", "depthcue", "view"} & uncertain_fields)
        )
        self.ai_default_light_check.setChecked("lights" in uncertain_fields)
        self.ai_default_skeleton_check.setChecked(
            bool({"skeleton_style", "skeleton_material", "skeleton"} & uncertain_fields)
        )
        self.ai_default_isovalue_check.setChecked(True)
        self._apply_checked_defaults()
        confidence = float(guess["confidence"])
        reliability = "较高" if confidence >= 0.85 else "一般" if confidence >= 0.70 else "较低"
        field_labels = {
            "ambient": "环境光",
            "background": "背景颜色",
            "depthcue": "景深",
            "diffuse": "漫反射",
            "isovalue": "等值面数值",
            "lights": "灯光",
            "material": "材质",
            "mirror": "镜面反射",
            "opacity": "透明度",
            "outline": "轮廓",
            "outline_width": "轮廓宽度",
            "positive_negative_assignment": "正负等值面颜色",
            "projection": "投影方式",
            "shininess": "高光锐度",
            "skeleton": "骨架样式",
            "skeleton_material": "骨架材质",
            "skeleton_style": "骨架显示方式",
            "specular": "高光",
            "view": "视角",
        }
        uncertain_labels = sorted(
            {
                field_labels[field]
                for field in guess["uncertain_fields"]
                if field in field_labels
            }
        )
        source_labels = {
            "本地测色": "图片测色",
            "测色 + AI": "图片测色和 AI 分析",
        }
        self.ai_conf_label.setText(f"识别可靠度：{reliability}")
        self.ai_uncertain_label.setText(
            "建议检查：" + ("、".join(uncertain_labels) if uncertain_labels else "无")
        )
        self.ai_method_label.setText(
            f"识别方式：{source_labels.get(source, source)}"
        )

    def _guess_from_result_panel(self) -> dict:
        if not self.ai_current_guess:
            raise ValueError("请先识别图片风格。")
        self._apply_checked_defaults()
        guess = dict(self.ai_current_guess)
        guess["material"] = self.ai_material_combo.currentText()
        guess["projection"] = self.ai_projection_combo.currentText()
        guess["skeleton_style"] = self.ai_skeleton_combo.currentText()
        guess["skeleton_material"] = self.ai_skeleton_material_combo.currentText()
        guess["opacity"] = self.ai_opacity_spin.value()
        guess["specular"] = self.ai_specular_spin.value()
        guess["shininess"] = self.ai_shininess_spin.value()
        guess["depthcue"] = self.ai_depthcue_check.isChecked()
        guess["lights"] = {
            idx: chk.isChecked() for idx, chk in self.ai_light_checks.items()
        }
        return core.normalize_ai_style_guess(guess)

    def _recognize_ai_style(self) -> None:
        if self.ai_thread is not None and self.ai_thread.isRunning():
            return
        try:
            image_for_api = self._current_ai_image_for_api()
        except Exception as exc:
            QMessageBox.critical(self, "识别失败", str(exc))
            return

        api_key = self.ai_key_edit.text().strip()
        model = self.ai_model_edit.text().strip()
        provider = self._current_ai_provider()
        style_name = self.ai_name_edit.text().strip() or Path(self.ai_image_edit.text()).stem
        try:
            measured = self._measure_image_style(image_for_api)
        except Exception as exc:
            QMessageBox.critical(self, "识别失败", str(exc))
            return

        self._log("正在识别图片风格……")
        self.btn_ai_recognize.setEnabled(False)
        self.btn_ai_recognize.setText("识别中…")
        source_name = Path(self.ai_image_edit.text().strip()).name
        image_context = core.build_ai_image_context(
            measured,
            style_name=style_name,
            source_hint=source_name,
        )
        self.ai_pending = {
            "image": image_for_api,
            "measured": measured,
            "style_name": style_name,
            "provider": provider,
        }
        thread = QThread(self)
        worker = AiRecognitionWorker(
            image_for_api, api_key, model, provider, image_context
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_ai_recognition_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_ai_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self.ai_thread = thread
        self.ai_worker = worker
        thread.start()

    @Slot(object, object)
    def _on_ai_recognition_finished(self, api_guess, error) -> None:
        pending = dict(self.ai_pending)
        measured = pending.get("measured") or {}
        style_name = str(pending.get("style_name") or "图片风格")
        image_for_api = Path(pending.get("image") or self.ai_image_edit.text().strip())
        if error:
            self._log("AI 分析不可用，已根据图片颜色生成结果。")
            guess = self._base_guess_from_measurement(measured, style_name)
            source = "本地测色"
        else:
            guess = self._merge_ai_with_measurement(api_guess, measured, style_name)
            source = "测色 + AI"

        self.ai_effective_image_path = str(image_for_api)
        self._display_ai_result(guess, source)
        if not self.ai_name_edit.text().strip():
            self.ai_name_edit.setText(guess.get("style_name", "图片风格"))
        if not self.ai_desc_edit.text().strip() and guess.get("style_summary"):
            self.ai_desc_edit.setText(guess.get("style_summary", ""))
        self._log("图片风格识别完成。")
        self.btn_ai_recognize.setEnabled(True)
        self.btn_ai_recognize.setText("识别风格")

    @Slot()
    def _on_ai_thread_finished(self) -> None:
        self.ai_worker = None
        self.ai_thread = None
        self.ai_pending = {}

    def _save_ai_custom_style(self) -> None:
        try:
            guess = self._guess_from_result_panel()
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        image_path = Path(self.ai_image_edit.text().strip())
        if not image_path.exists():
            QMessageBox.critical(self, "保存失败", "请先选择有效的图片。")
            return

        try:
            cover_path = (
                Path(self.ai_effective_image_path)
                if self.ai_effective_image_path
                else self._current_ai_image_for_api()
            )
            name = self.ai_name_edit.text().strip() or image_path.stem
            desc = self.ai_desc_edit.text().strip()
            provider = self._current_ai_provider()
            style = core.build_custom_style_from_ai_guess(
                guess, name, desc, provider=provider
            )
            ext = cover_path.suffix.lower() if cover_path.suffix else ".png"
            if ext == ".jpeg":
                ext = ".jpg"
            if ext not in {".png", ".jpg", ".webp", ".gif"}:
                ext = ".png"
            image_file = f"{style['id']}_cover{ext}"
            core.write_bytes_atomic(core.STYLE_DIR / image_file, cover_path.read_bytes())
            style["image"] = image_file
            core.upsert_custom_style(style)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        self.selected_bundle_id = style["id"]
        self.selected_iso_id = style["id"]
        self._refresh_styles()
        self._show_style_selection()
        self._log(f"已保存 AI 图片风格：{style['name']}")
        QMessageBox.information(self, "保存成功", f"已保存：{style['name']}")

    def _scan_paths(self) -> None:
        found = core.find_path_candidates()
        if not self.multi_edit.text().strip() and found["multiwfn"]:
            self.multi_edit.setText(found["multiwfn"][0])
        if not self.vmd_edit.text().strip() and found["vmd"]:
            self.vmd_edit.setText(found["vmd"][0])
        multi_status = "已找到" if Path(self.multi_edit.text().strip()).is_file() else "未找到"
        vmd_status = "已找到" if Path(self.vmd_edit.text().strip()).is_file() else "未找到"
        self._log(f"程序扫描完成。Multiwfn：{multi_status}；VMD：{vmd_status}。")

    def _read_state_text(self, path: Path) -> str:
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
            try:
                return path.read_text(encoding=enc)
            except Exception:
                continue
        raise ValueError("无法用 UTF-8/GBK 解码该文件。")

    def _import_custom_style(self) -> None:
        state_path = Path(self.state_file_edit.text().strip())
        if not state_path.exists():
            QMessageBox.critical(self, "导入失败", "请先选择有效的 Save State 文件。")
            return

        try:
            state_text = self._read_state_text(state_path)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", f"读取文件失败：{exc}")
            return

        custom_name = self.custom_name_edit.text().strip() or state_path.stem
        custom_desc = self.custom_desc_edit.text().strip()
        try:
            style = core.parse_save_state_to_custom_style(state_text, custom_name, custom_desc)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", f"解析 save state 失败：{exc}")
            return

        cover_text = self.cover_file_edit.text().strip()
        if cover_text:
            cover_path = Path(cover_text)
            if cover_path.exists():
                ext = cover_path.suffix.lower() if cover_path.suffix else ".png"
                if ext == ".jpeg":
                    ext = ".jpg"
                if ext not in {".png", ".jpg", ".webp", ".gif"}:
                    QMessageBox.critical(self, "导入失败", "封面图格式不支持。")
                    return
                image_file = f"{style['id']}_cover{ext}"
                core.write_bytes_atomic(core.STYLE_DIR / image_file, cover_path.read_bytes())
                style["image"] = image_file

        core.upsert_custom_style(style)

        self.selected_bundle_id = style["id"]
        self.selected_iso_id = style["id"]
        self._refresh_styles()
        self._show_style_selection()
        self._log(f"已导入自定义风格：{style['name']}")
        QMessageBox.information(self, "导入成功", f"已导入：{style['name']}")

    def _generate_script(self) -> None:
        multi = self.multi_edit.text().strip()
        vmd = self.vmd_edit.text().strip()
        out_name = self.out_edit.text().strip()
        if not out_name:
            out_name = self._auto_output_name()
            self.out_edit.setText(out_name)

        if not multi or not Path(multi).exists():
            QMessageBox.critical(self, "生成失败", "Multiwfn.exe 路径无效。")
            return
        if not vmd or not Path(vmd).exists():
            QMessageBox.critical(self, "生成失败", "vmd.exe 路径无效。")
            return

        try:
            style, rep0_commands, _ = self._current_style_selection()
        except ValueError as exc:
            QMessageBox.critical(self, "生成失败", str(exc))
            return

        selected_skeleton = self.selected_skeleton_id
        if self.mode == "split":
            selected_bundle = self.selected_iso_id
            selected_iso = self.selected_iso_id
            default_id = f"{selected_skeleton}_{selected_iso}"
        else:
            selected_bundle = self.selected_bundle_id
            selected_iso = self.selected_bundle_id
            default_id = self.selected_bundle_id

        safe_out = core._sanitize_output_name(out_name, default_id)
        output_dir = Path(self.out_dir_edit.text().strip() or core.ROOT).expanduser()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            output_dir = output_dir.resolve()
        except OSError as exc:
            QMessageBox.critical(self, "生成失败", f"保存目录不可用：{exc}")
            return
        out_path = output_dir / safe_out
        script = core.build_cmd_script(style, multi, vmd, rep0_commands=rep0_commands)
        core.write_text_atomic(out_path, script)

        core.save_config(
            {
                "multiwfn_exe": multi,
                "vmd_exe": vmd,
                "output_name": safe_out,
                "output_dir": str(output_dir),
                "mode": self.mode,
                "theme": "dark" if self.dark_mode else "light",
                "last_style": selected_bundle,
                "last_skeleton": selected_skeleton,
                "last_iso_style": selected_iso,
            }
        )
        self.out_edit.setText(safe_out)
        self._log(f"已生成脚本：{out_path}")
        QMessageBox.information(self, "生成成功", f"脚本已生成：\n{out_path}")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if hasattr(self, "direct_page") and self.direct_page.is_running():
            QMessageBox.information(self, "直接绘图进行中", "请先停止直接绘图工作流，再关闭软件。")
            event.ignore()
            return
        if hasattr(self, "batch_page") and self.batch_page.is_running():
            QMessageBox.information(self, "批处理进行中", "请先停止批处理任务，再关闭软件。")
            event.ignore()
            return
        if hasattr(self, "automation_page") and self.automation_page.is_running():
            QMessageBox.information(
                self,
                "全自动流程进行中",
                "请先停止当前全自动流程，再关闭软件。",
            )
            event.ignore()
            return
        if self.ai_thread is not None and self.ai_thread.isRunning():
            QMessageBox.information(self, "识别进行中", "AI 识别完成后即可关闭软件。")
            event.ignore()
            return
        if hasattr(self, "direct_page"):
            self.direct_page.cleanup()
        if hasattr(self, "automation_page"):
            self.automation_page.cleanup()
        self._cleanup_ai_temp_files()
        super().closeEvent(event)


def run_self_test() -> int:
    prepare_runtime_files()
    styles = core.get_all_bundle_styles()
    sk = core.SKELETON_STYLES
    print(f"SELFTEST_OK styles={len(styles)} skeletons={len(sk)} root={core.ROOT}")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return run_self_test()
    prepare_runtime_files()
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
