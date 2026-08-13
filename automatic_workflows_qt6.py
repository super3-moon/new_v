from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Iterable

import automatic_workflows as automation
import vmd_style_tool as core
from orbital_diagram_qt6 import OrbitalDiagramPage
from style_parameter_dialog_qt6 import StyleParameterDialog
from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QDoubleValidator, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


SUPPORTED_INPUT_EXTENSIONS = {
    ".fch",
    ".fchk",
    ".wfn",
    ".wfx",
    ".mwfn",
    ".molden",
    ".molden.input",
}
DEFAULT_WORKFLOW_ID = "surface_esp"
DEFAULT_STYLE_ID = "esp_e3_bwr_edgyglass_443"


def _is_supported_input(path: Path) -> bool:
    name = path.name.casefold()
    return path.is_file() and any(name.endswith(ext) for ext in SUPPORTED_INPUT_EXTENSIONS)


def _style_preview(style: dict, width: int, height: int) -> QPixmap:
    image_name = str(style.get("image") or "")
    path = core.STYLE_DIR / image_name
    pixmap = QPixmap(str(path)) if path.is_file() else QPixmap()
    canvas = QPixmap(width, height)
    canvas.fill(QColor("#f5f8fc"))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    if pixmap.isNull():
        painter.setPen(QColor("#56708f"))
        painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, "VMD\n预览")
    else:
        scaled = pixmap.scaled(
            max(1, width - 10),
            max(1, height - 10),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(
            (width - scaled.width()) // 2,
            (height - scaled.height()) // 2,
            scaled,
        )
    painter.end()
    return canvas


def _style_summary(style: dict) -> str:
    material = str(style.get("material") or "Glossy")
    if str(style.get("surface_mode") or "signed") == "signed":
        positive = int(style.get("pos_color", 1))
        negative = int(style.get("neg_color", 0))
        return f"{material} · 正相位 ColorID {positive} · 负相位 ColorID {negative}"
    method = str(style.get("color_scale_method") or "BWR")
    low = float(style.get("color_scale_min", -0.03))
    high = float(style.get("color_scale_max", 0.03))
    return f"{material} · {method} {low:g}～{high:g} a.u."


def _snapshot_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_value(result: object, name: str, default=None):
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


class WorkflowDropTable(QTableWidget):
    pathsDropped = Signal(object)

    def __init__(self) -> None:
        super().__init__(0, 3)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(36)
        self.setHorizontalHeaderLabels(["启用", "输入文件", "格式"])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.setMinimumHeight(230)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.pathsDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class _SelectableStyleCard(QFrame):
    picked = Signal(str)

    def __init__(self, style: dict, *, compact: bool = False) -> None:
        super().__init__()
        self.style_id = str(style.get("id") or "")
        self.setObjectName("styleCard")
        self.setProperty("selected", False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(11)
        preview = QLabel()
        preview.setObjectName("cardImage")
        image_w, image_h = ((118, 72) if compact else (148, 92))
        preview.setFixedSize(image_w, image_h)
        preview.setPixmap(_style_preview(style, image_w, image_h))
        layout.addWidget(preview)

        text = QVBoxLayout()
        text.setSpacing(4)
        title = QLabel(str(style.get("name") or self.style_id))
        title.setObjectName("cardTitle")
        title.setWordWrap(True)
        subtitle = QLabel(_style_summary(style) if style.get("surface_mode") == "volume_mapped" else str(style.get("notes") or "骨架样式"))
        subtitle.setObjectName("cardSubtitle")
        subtitle.setWordWrap(True)
        text.addWidget(title)
        text.addWidget(subtitle)
        text.addStretch(1)
        layout.addLayout(text, 1)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", bool(selected))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self.style_id:
            self.picked.emit(self.style_id)
        super().mousePressEvent(event)


class _StyleChoiceList(QScrollArea):
    selectionChanged = Signal(str)

    def __init__(self, styles: list[dict], selected_id: str = "", *, compact: bool = False) -> None:
        super().__init__()
        self.styles = {str(style.get("id") or ""): style for style in styles}
        self.cards: dict[str, _SelectableStyleCard] = {}
        self.selected_id = selected_id if selected_id in self.styles else ""
        if not self.selected_id and styles:
            self.selected_id = str(styles[0].get("id") or "")
        self.setObjectName("cardGrid")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 8, 4)
        layout.setSpacing(9)
        for style in styles:
            card = _SelectableStyleCard(style, compact=compact)
            card.picked.connect(self._pick)
            self.cards[card.style_id] = card
            layout.addWidget(card)
        if not styles:
            empty = QLabel("没有兼容的绘图方案")
            empty.setObjectName("emptyLabel")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
        layout.addStretch(1)
        self.setWidget(body)
        self._sync()

    def _pick(self, style_id: str) -> None:
        self.selected_id = style_id
        self._sync()
        self.selectionChanged.emit(style_id)

    def _sync(self) -> None:
        for style_id, card in self.cards.items():
            card.set_selected(style_id == self.selected_id)


class AutomationStyleDialog(QDialog):
    """Select a workflow-compatible drawing scheme without exposing Tcl or JSON."""

    def __init__(
        self,
        current: dict | None = None,
        parent: QWidget | None = None,
        *,
        surface_mode: str = "volume_mapped",
    ) -> None:
        super().__init__(parent)
        current = current or {}
        self.surface_mode = "signed" if surface_mode == "signed" else "volume_mapped"
        self.setWindowTitle("选择绘图方案")
        self.setModal(True)
        self.resize(980, 700)
        self.setMinimumSize(760, 560)

        self.styles = [
            copy.deepcopy(style)
            for style in core.get_all_bundle_styles()
            if str(style.get("surface_mode") or "signed") == self.surface_mode
        ]
        self.style_map = {str(style.get("id") or ""): style for style in self.styles}
        self.skeletons = [copy.deepcopy(style) for style in core.SKELETON_STYLES]
        self.skeleton_map = {str(style.get("id") or ""): style for style in self.skeletons}
        self.mode = str(current.get("mode") or "bundle")
        default_style_id = DEFAULT_STYLE_ID if self.surface_mode == "volume_mapped" else core.DEFAULT_STYLE_ID
        self.bundle_id = str(current.get("bundle_id") or default_style_id)
        self.iso_id = str(current.get("bundle_id") or current.get("iso_id") or default_style_id)
        self.skeleton_id = str(current.get("skeleton_id") or "")

        if self.bundle_id not in self.style_map and self.styles:
            self.bundle_id = str(self.styles[0].get("id") or "")
        if self.iso_id not in self.style_map and self.styles:
            self.iso_id = str(self.styles[0].get("id") or "")
        if self.skeleton_id not in self.skeleton_map and self.skeletons:
            self.skeleton_id = str(self.skeletons[0].get("id") or "")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        heading = QLabel(
            "选择用于分子轨道的绘图方案"
            if self.surface_mode == "signed"
            else "选择用于表面静电势图的绘图方案"
        )
        heading.setObjectName("batchHeroTitle")
        root.addWidget(heading)
        hint = QLabel(
            "这里只显示具有正、负相位配色的轨道等值面方案。它是进入 VMD 时的初始方案；确认前仍可在 VMD 中自由调整全部显示参数。"
            if self.surface_mode == "signed"
            else "这里只显示能够把 ESP 数据映射到电子密度表面的方案。科学等值面仍由自动化流程统一控制。"
        )
        hint.setObjectName("batchHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, 1)

        self.bundle_list = _StyleChoiceList(self.styles, self.bundle_id)
        self.bundle_list.selectionChanged.connect(self._on_bundle_changed)
        self.tabs.addTab(self.bundle_list, "套装模式")

        split_page = QWidget()
        split_layout = QHBoxLayout(split_page)
        split_layout.setContentsMargins(8, 10, 8, 8)
        split_layout.setSpacing(12)
        skeleton_card = QFrame()
        skeleton_card.setObjectName("batchCard")
        skeleton_layout = QVBoxLayout(skeleton_card)
        skeleton_title = QLabel("骨架样式")
        skeleton_title.setObjectName("paneTitle")
        skeleton_layout.addWidget(skeleton_title)
        self.skeleton_list = _StyleChoiceList(self.skeletons, self.skeleton_id, compact=True)
        self.skeleton_list.selectionChanged.connect(self._on_skeleton_changed)
        skeleton_layout.addWidget(self.skeleton_list, 1)
        split_layout.addWidget(skeleton_card, 1)

        iso_card = QFrame()
        iso_card.setObjectName("batchCard")
        iso_layout = QVBoxLayout(iso_card)
        iso_title = QLabel("等值面样式")
        iso_title.setObjectName("paneTitle")
        iso_layout.addWidget(iso_title)
        self.iso_list = _StyleChoiceList(self.styles, self.iso_id, compact=True)
        self.iso_list.selectionChanged.connect(self._on_iso_changed)
        iso_layout.addWidget(self.iso_list, 1)
        split_layout.addWidget(iso_card, 1)
        self.tabs.addTab(split_page, "拆分模式")
        self.tabs.setCurrentIndex(1 if self.mode == "split" else 0)
        self.tabs.currentChanged.connect(self._on_mode_changed)

        footer = QHBoxLayout()
        self.selection_label = QLabel()
        self.selection_label.setObjectName("detailLabel")
        self.selection_label.setWordWrap(True)
        footer.addWidget(self.selection_label, 1)
        parameters = QPushButton("查看方案参数")
        parameters.clicked.connect(self._show_parameters)
        footer.addWidget(parameters)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        apply_button = QPushButton("应用到此自动化流程")
        apply_button.setObjectName("primaryBtn")
        apply_button.clicked.connect(self._accept_selection)
        footer.addWidget(apply_button)
        root.addLayout(footer)
        self._sync_summary()

    def _on_mode_changed(self, index: int) -> None:
        self.mode = "split" if index == 1 else "bundle"
        self._sync_summary()

    def _on_bundle_changed(self, style_id: str) -> None:
        self.bundle_id = style_id
        self._sync_summary()

    def _on_iso_changed(self, style_id: str) -> None:
        self.iso_id = style_id
        self._sync_summary()

    def _on_skeleton_changed(self, style_id: str) -> None:
        self.skeleton_id = style_id
        self._sync_summary()

    def _selection_parts(self) -> tuple[dict, list[str] | None, str]:
        if self.mode == "split":
            skeleton = self.skeleton_map.get(self.skeleton_id)
            iso_style = self.style_map.get(self.iso_id)
            if skeleton is None or iso_style is None:
                raise ValueError("拆分模式需要同时选择骨架和等值面方案。")
            style = core.compose_combo_style(skeleton, iso_style)
            style["surface_mode"] = self.surface_mode
            rep0 = list(skeleton.get("rep0_commands") or [])
            text = f"骨架：{skeleton.get('name')} · 等值面：{iso_style.get('name')}"
            return style, rep0, text
        style = self.style_map.get(self.bundle_id)
        if style is None:
            raise ValueError("请选择一个兼容的绘图方案。")
        rep0 = list(style.get("rep0_commands") or []) or None
        return copy.deepcopy(style), rep0, f"套装风格：{style.get('name')}"

    def _sync_summary(self) -> None:
        try:
            style, _rep0, selection_text = self._selection_parts()
        except ValueError as exc:
            self.selection_label.setText(str(exc))
            return
        self.selection_label.setText(f"{selection_text}\n{_style_summary(style)}")

    def _show_parameters(self) -> None:
        try:
            style, rep0, selection_text = self._selection_parts()
        except ValueError as exc:
            QMessageBox.warning(self, "尚未选择方案", str(exc))
            return
        StyleParameterDialog(style, rep0, selection_text, self).exec()

    def _accept_selection(self) -> None:
        try:
            self._selection_parts()
        except ValueError as exc:
            QMessageBox.warning(self, "无法应用", str(exc))
            return
        self.accept()

    def selection(self) -> dict:
        style, rep0, selection_text = self._selection_parts()
        payload = {
            "style": copy.deepcopy(style),
            "rep0_commands": list(rep0 or []),
            "selection_text": selection_text,
            "mode": self.mode,
            "bundle_id": self.bundle_id if self.mode == "bundle" else self.iso_id,
            "iso_id": self.iso_id,
            "skeleton_id": self.skeleton_id if self.mode == "split" else "",
        }
        payload["hash"] = _snapshot_hash(payload)
        return payload


class AutomaticExecutionWorker(QObject):
    event = Signal(object)
    finished = Signal(object, object)

    def __init__(
        self,
        files: list[Path],
        output_root: Path,
        settings: dict,
        multiwfn_exe: Path,
        vmd_exe: Path,
        run_mode: str,
        resume_manifest: Path | None = None,
    ) -> None:
        super().__init__()
        self.files = list(files)
        self.output_root = output_root
        self.settings = copy.deepcopy(settings)
        self.multiwfn_exe = multiwfn_exe
        self.vmd_exe = vmd_exe
        self.run_mode = run_mode
        self.resume_manifest = resume_manifest
        self.runner = None
        self._cancel_pending = False

    @Slot()
    def run(self) -> None:
        result = None
        error = None
        try:
            if self.resume_manifest is not None:
                plan = automation.resume_automation_plan(
                    self.resume_manifest,
                    self.files,
                    self.settings,
                )
            else:
                plan = automation.create_automation_plan(
                    self.files,
                    DEFAULT_WORKFLOW_ID,
                    self.output_root,
                    self.settings,
                    prefix="trial" if self.run_mode == "trial" else "automation",
                )
            self.runner = automation.AutomaticWorkflowRunner(
                plan,
                self.multiwfn_exe,
                self.vmd_exe,
                event_callback=self.event.emit,
            )
            if self._cancel_pending:
                self.runner.cancel()
            result = self.runner.run()
        except Exception as exc:  # UI boundary: surface the error without killing Qt.
            error = exc
        self.finished.emit(result, error)

    def cancel(self) -> None:
        self._cancel_pending = True
        if self.runner is not None:
            try:
                self.runner.cancel()
            except Exception:
                pass


class AutomaticRetryWorker(QObject):
    event = Signal(object)
    finished = Signal(object, object)

    def __init__(self, manifest_path: Path, job_id: str, vmd_exe: Path) -> None:
        super().__init__()
        self.manifest_path = manifest_path
        self.job_id = job_id
        self.vmd_exe = vmd_exe
        self.runner = None
        self._cancel_pending = False

    def _runner_ready(self, runner) -> None:
        self.runner = runner
        if self._cancel_pending:
            runner.cancel()

    @Slot()
    def run(self) -> None:
        try:
            result = automation.retry_drawing_from_manifest(
                self.manifest_path,
                self.job_id,
                self.vmd_exe,
                event_callback=self.event.emit,
                runner_ready=self._runner_ready,
            )
        except Exception as exc:
            self.finished.emit(None, exc)
            return
        self.finished.emit(result, None)

    def cancel(self) -> None:
        self._cancel_pending = True
        if self.runner is not None:
            self.runner.cancel()


class AutomaticWorkflowsPage(QWidget):
    settingsChanged = Signal(object)

    def __init__(
        self,
        storage_dir: Path,
        multiwfn_path_getter: Callable[[], str],
        vmd_path_getter: Callable[[], str],
    ) -> None:
        super().__init__()
        self.storage_dir = Path(storage_dir)
        self.multiwfn_path_getter = multiwfn_path_getter
        self.vmd_path_getter = vmd_path_getter
        self.files: list[Path] = []
        self.file_enabled: dict[str, bool] = {}
        self.style_snapshot: dict = {}
        self.thread: QThread | None = None
        self.worker: AutomaticExecutionWorker | AutomaticRetryWorker | None = None
        self.last_run_dir = ""
        self.last_result: dict = {}
        self._run_files: list[Path] = []
        self._run_mode = ""
        self._cancel_requested = False
        self._trial_input = ""
        self._trial_signature = ""
        self._build_ui()
        self._select_default_style()

    @staticmethod
    def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("batchCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 15, 16, 16)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("batchCardTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        if hint:
            helper = QLabel(hint)
            helper.setObjectName("batchHint")
            helper.setWordWrap(True)
            layout.addWidget(helper)
        return frame, layout

    @staticmethod
    def _scroll_page(body: QWidget, minimum_height: int = 560) -> QScrollArea:
        body.setMinimumHeight(minimum_height)
        scroll = QScrollArea()
        scroll.setObjectName("batchPageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(body)
        return scroll

    def _hero(self) -> QFrame:
        hero = QFrame()
        hero.setObjectName("batchHero")
        layout = QHBoxLayout(hero)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(14)
        text = QVBoxLayout()
        text.setSpacing(3)
        title = QLabel("全自动流程")
        title.setObjectName("batchHeroTitle")
        subtitle = QLabel("选择一个完整流程，由软件依次接管计算、校验、绘图与结果整理")
        subtitle.setObjectName("batchHeroSubtitle")
        subtitle.setWordWrap(True)
        text.addWidget(title)
        text.addWidget(subtitle)
        layout.addLayout(text, 1)
        self.workflow_count_badge = QLabel(
            f"{len(automation.workflow_definitions())} 个流程"
        )
        self.workflow_count_badge.setObjectName("batchBadge")
        layout.addWidget(self.workflow_count_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        return hero

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addWidget(self._hero())
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("batchResultStack")
        root.addWidget(self.page_stack, 1)
        self.page_stack.addWidget(self._build_catalog_page())
        self.page_stack.addWidget(self._build_configuration_page())
        self.page_stack.addWidget(self._build_results_page())
        self.orbital_page = OrbitalDiagramPage(
            self.storage_dir,
            self.multiwfn_path_getter,
            self.vmd_path_getter,
            style_dialog_factory=AutomationStyleDialog,
        )
        self.orbital_page.settingsChanged.connect(self.settingsChanged.emit)
        self.orbital_page.backRequested.connect(
            lambda: self.page_stack.setCurrentIndex(0)
        )
        self.orbital_page_index = self.page_stack.addWidget(self.orbital_page)
        self.page_stack.setCurrentIndex(0)

    def _build_catalog_page(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 8, 8)
        layout.setSpacing(12)
        intro = QLabel("可用流程")
        intro.setObjectName("paneTitle")
        layout.addWidget(intro)

        flow_card = QFrame()
        flow_card.setObjectName("batchCard")
        flow_layout = QHBoxLayout(flow_card)
        flow_layout.setContentsMargins(20, 18, 20, 18)
        flow_layout.setSpacing(18)
        icon = QLabel("ESP")
        icon.setObjectName("batchEmptyIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(74, 74)
        flow_layout.addWidget(icon)
        flow_text = QVBoxLayout()
        flow_text.setSpacing(5)
        title = QLabel("表面静电势图")
        title.setObjectName("batchCardTitle")
        description = QLabel("自动生成电子密度与 ESP Cube，检查空间网格，套用绘图方案并由 VMD 完成渲染。")
        description.setObjectName("batchHint")
        description.setWordWrap(True)
        tags = QLabel("Multiwfn + VMD · 支持批量 · 自动整理结果")
        tags.setObjectName("detailLabel")
        tags.setWordWrap(True)
        flow_text.addWidget(title)
        flow_text.addWidget(description)
        flow_text.addWidget(tags)
        flow_layout.addLayout(flow_text, 1)
        start = QPushButton("开始配置")
        start.setObjectName("primaryBtn")
        start.setMinimumHeight(42)
        start.clicked.connect(lambda: self.page_stack.setCurrentIndex(1))
        flow_layout.addWidget(start, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(flow_card)

        orbital_card = QFrame()
        orbital_card.setObjectName("batchCard")
        orbital_layout = QHBoxLayout(orbital_card)
        orbital_layout.setContentsMargins(20, 18, 20, 18)
        orbital_layout.setSpacing(18)
        orbital_icon = QLabel("MO")
        orbital_icon.setObjectName("batchEmptyIcon")
        orbital_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        orbital_icon.setFixedSize(74, 74)
        orbital_layout.addWidget(orbital_icon)
        orbital_text = QVBoxLayout()
        orbital_text.setSpacing(5)
        orbital_title = QLabel("分子轨道能级图")
        orbital_title.setObjectName("batchCardTitle")
        orbital_description = QLabel(
            "配对 Gaussian/ORCA 输出与 FCH/Molden，选择需要的轨道，在 VMD 中自由调整一次最终场景，随后统一用 Tachyon 渲染并自动排版。"
        )
        orbital_description.setObjectName("batchHint")
        orbital_description.setWordWrap(True)
        orbital_tags = QLabel("Gaussian / ORCA · Multiwfn + VMD · 统一视角 · 自动能级排版")
        orbital_tags.setObjectName("detailLabel")
        orbital_tags.setWordWrap(True)
        orbital_text.addWidget(orbital_title)
        orbital_text.addWidget(orbital_description)
        orbital_text.addWidget(orbital_tags)
        orbital_layout.addLayout(orbital_text, 1)
        self.orbital_start_button = QPushButton("开始配置")
        self.orbital_start_button.setObjectName("primaryBtn")
        self.orbital_start_button.setMinimumHeight(42)
        self.orbital_start_button.clicked.connect(self._show_orbital_page)
        orbital_layout.addWidget(
            self.orbital_start_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(orbital_card)
        layout.addStretch(1)
        return self._scroll_page(body, 420)

    def _show_orbital_page(self) -> None:
        if hasattr(self, "orbital_page_index"):
            self.page_stack.setCurrentIndex(self.orbital_page_index)

    def _build_configuration_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        toolbar = QFrame()
        toolbar.setObjectName("batchToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 8, 12, 8)
        back = QPushButton("返回全部流程")
        back.clicked.connect(lambda: self.page_stack.setCurrentIndex(0))
        toolbar_layout.addWidget(back)
        name = QLabel("表面静电势图")
        name.setObjectName("batchToolbarLabel")
        toolbar_layout.addWidget(name)
        toolbar_layout.addStretch(1)
        self.config_ready_label = QLabel("尚未添加文件")
        self.config_ready_label.setObjectName("batchPresetInline")
        toolbar_layout.addWidget(self.config_ready_label)
        page_layout.addWidget(toolbar)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 8, 8)
        body_layout.setSpacing(12)

        files_card, files_layout = self._card(
            "1 · 添加需要处理的文件",
            "可添加文件、扫描文件夹，或直接把文件和文件夹拖入列表。这里只接受包含波函数信息的文件。",
        )
        file_actions = QHBoxLayout()
        add_files = QPushButton("添加文件")
        add_files.clicked.connect(self._add_files)
        add_folder = QPushButton("扫描文件夹")
        add_folder.clicked.connect(self._add_folder)
        remove = QPushButton("移除选中")
        remove.clicked.connect(self._remove_selected_files)
        clear = QPushButton("清空")
        clear.clicked.connect(self._clear_files)
        self.recursive_check = QCheckBox("包含子文件夹")
        self.recursive_check.setChecked(True)
        file_actions.addWidget(add_files)
        file_actions.addWidget(add_folder)
        file_actions.addWidget(remove)
        file_actions.addWidget(clear)
        file_actions.addStretch(1)
        file_actions.addWidget(self.recursive_check)
        files_layout.addLayout(file_actions)
        self.file_table = WorkflowDropTable()
        self.file_table.pathsDropped.connect(self._handle_dropped_paths)
        self.file_table.itemChanged.connect(self._on_file_item_changed)
        files_layout.addWidget(self.file_table)
        self.file_count_label = QLabel("0 个文件")
        self.file_count_label.setObjectName("countPill")
        files_layout.addWidget(self.file_count_label, 0, Qt.AlignmentFlag.AlignRight)
        body_layout.addWidget(files_card)

        style_card, style_layout = self._card(
            "2 · 选择绘图方案",
            "绘图方案只控制骨架、材质、色带、光照等视觉设置，不会改变电子密度等值面。",
        )
        style_row = QHBoxLayout()
        style_row.setSpacing(14)
        self.style_preview = QLabel()
        self.style_preview.setObjectName("cardImage")
        self.style_preview.setFixedSize(210, 126)
        self.style_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        style_row.addWidget(self.style_preview)
        style_text = QVBoxLayout()
        style_text.setSpacing(4)
        self.style_name_label = QLabel("尚未选择兼容方案")
        self.style_name_label.setObjectName("workflowStyleName")
        self.style_name_label.setWordWrap(True)
        self.style_selection_label = QLabel("")
        self.style_selection_label.setObjectName("detailLabel")
        self.style_selection_label.setWordWrap(True)
        self.style_meta_label = QLabel("")
        self.style_meta_label.setObjectName("batchHint")
        self.style_meta_label.setWordWrap(True)
        style_text.addWidget(self.style_name_label)
        style_text.addWidget(self.style_selection_label)
        style_text.addWidget(self.style_meta_label)
        style_text.addStretch(1)
        style_row.addLayout(style_text, 1)
        style_actions = QVBoxLayout()
        choose_style = QPushButton("选择绘图方案")
        choose_style.setObjectName("primaryBtn")
        choose_style.clicked.connect(self._choose_style)
        self.view_style_button = QPushButton("查看方案参数")
        self.view_style_button.clicked.connect(self._view_style_parameters)
        style_actions.addWidget(choose_style)
        style_actions.addWidget(self.view_style_button)
        style_actions.addStretch(1)
        style_row.addLayout(style_actions)
        style_layout.addLayout(style_row)
        body_layout.addWidget(style_card)

        settings_card, settings_layout = self._card(
            "3 · 计算与输出设置",
            "电子密度等值面会同时用于 Multiwfn 计算和 VMD 绘图，避免两端参数不一致。",
        )
        settings_grid = QGridLayout()
        settings_grid.setHorizontalSpacing(10)
        settings_grid.setVerticalSpacing(10)
        settings_grid.addWidget(QLabel("电子密度等值面（a.u.）"), 0, 0)
        self.rho_iso_edit = QLineEdit("0.001")
        validator = QDoubleValidator(0.000000000001, 1.0, 12, self.rho_iso_edit)
        validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
        self.rho_iso_edit.setValidator(validator)
        self.rho_iso_edit.textChanged.connect(self._configuration_changed)
        settings_grid.addWidget(self.rho_iso_edit, 0, 1)

        settings_grid.addWidget(QLabel("运行方式"), 0, 2)
        self.render_mode_combo = QComboBox()
        self.render_mode_combo.addItem("自动渲染图片并关闭 VMD", "automatic")
        self.render_mode_combo.addItem("生成场景后在 VMD 中查看", "interactive")
        self.render_mode_combo.addItem("仅生成 Cube，不启动 VMD", "cubes_only")
        self.render_mode_combo.currentIndexChanged.connect(self._on_render_mode_changed)
        settings_grid.addWidget(self.render_mode_combo, 0, 3)

        settings_grid.addWidget(QLabel("结果文件位置"), 1, 0)
        self.output_location_combo = QComboBox()
        self.output_location_combo.addItem("集中保存到结果目录", "result_root")
        self.output_location_combo.addItem("保存到各输入文件所在目录", "input_directory")
        self.output_location_combo.currentIndexChanged.connect(self._on_output_location_changed)
        settings_grid.addWidget(self.output_location_combo, 1, 1)

        settings_grid.addWidget(QLabel("图片尺寸"), 1, 2)
        self.image_size_combo = QComboBox()
        self.image_size_combo.addItem("标准 · 1600 × 1200", (1600, 1200))
        self.image_size_combo.addItem("高清 · 2400 × 1800", (2400, 1800))
        self.image_size_combo.addItem("自定义尺寸", None)
        self.image_size_combo.currentIndexChanged.connect(self._on_image_size_changed)
        settings_grid.addWidget(self.image_size_combo, 1, 3)

        settings_grid.addWidget(QLabel("结果目录"), 2, 0)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("自动化结果、运行记录和汇总文件的保存位置")
        self.output_dir_edit.textChanged.connect(self._configuration_changed)
        settings_grid.addWidget(self.output_dir_edit, 2, 1, 1, 2)
        choose_output = QPushButton("选择目录")
        choose_output.clicked.connect(self._pick_output_dir)
        settings_grid.addWidget(choose_output, 2, 3)
        settings_grid.setColumnStretch(1, 1)
        settings_grid.setColumnStretch(3, 1)
        settings_layout.addLayout(settings_grid)

        calculation_profile = QLabel(
            "计算精度：电子密度使用 High 网格，ESP 使用 Low 网格；两端共用上方同一个等值面数值。"
        )
        calculation_profile.setObjectName("batchHint")
        calculation_profile.setWordWrap(True)
        settings_layout.addWidget(calculation_profile)

        options = QHBoxLayout()
        self.keep_cubes_check = QCheckBox("完成后保留电子密度与 ESP Cube")
        self.keep_cubes_check.setChecked(True)
        self.keep_cubes_check.toggled.connect(self._configuration_changed)
        options.addWidget(self.keep_cubes_check)
        options.addStretch(1)
        self.advanced_toggle = QCheckBox("显示高级设置")
        options.addWidget(self.advanced_toggle)
        settings_layout.addLayout(options)

        self.advanced_panel = QFrame()
        self.advanced_panel.setObjectName("batchAdvancedPanel")
        advanced_layout = QHBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(12, 10, 12, 10)
        advanced_layout.addWidget(QLabel("VMD 单文件超时（秒）"))
        self.vmd_timeout_spin = QSpinBox()
        self.vmd_timeout_spin.setRange(30, 86400)
        self.vmd_timeout_spin.setValue(600)
        self.vmd_timeout_spin.valueChanged.connect(self._configuration_changed)
        advanced_layout.addWidget(self.vmd_timeout_spin)
        advanced_layout.addWidget(QLabel("自定义宽 × 高"))
        self.custom_width_spin = QSpinBox()
        self.custom_width_spin.setRange(320, 7680)
        self.custom_width_spin.setValue(1600)
        self.custom_width_spin.valueChanged.connect(self._configuration_changed)
        advanced_layout.addWidget(self.custom_width_spin)
        self.custom_height_spin = QSpinBox()
        self.custom_height_spin.setRange(240, 4320)
        self.custom_height_spin.setValue(1200)
        self.custom_height_spin.valueChanged.connect(self._configuration_changed)
        advanced_layout.addWidget(self.custom_height_spin)
        advanced_layout.addStretch(1)
        advanced_hint = QLabel("失败或取消时始终保留 Cube 和日志，便于仅重试绘图。")
        advanced_hint.setObjectName("batchHint")
        advanced_hint.setWordWrap(True)
        advanced_layout.addWidget(advanced_hint)
        self.advanced_panel.hide()
        self.advanced_toggle.toggled.connect(self.advanced_panel.setVisible)
        settings_layout.addWidget(self.advanced_panel)
        self._on_image_size_changed()
        body_layout.addWidget(settings_card)

        summary_card, summary_layout = self._card("4 · 运行前确认")
        self.workflow_summary_label = QLabel()
        self.workflow_summary_label.setObjectName("batchPresetSummary")
        self.workflow_summary_label.setWordWrap(True)
        summary_layout.addWidget(self.workflow_summary_label)
        body_layout.addWidget(summary_card)
        body_layout.addStretch(1)

        self.configuration_scroll = self._scroll_page(body, 1040)
        page_layout.addWidget(self.configuration_scroll, 1)
        footer = QFrame()
        footer.setObjectName("workflowFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 10, 12, 10)
        back_results = QPushButton("查看运行结果")
        back_results.clicked.connect(lambda: self.page_stack.setCurrentIndex(2))
        footer_layout.addWidget(back_results)
        footer_layout.addStretch(1)
        self.trial_button = QPushButton("首文件试运行")
        self.trial_button.clicked.connect(lambda: self._start_run(True))
        footer_layout.addWidget(self.trial_button)
        self.start_button = QPushButton("运行全部文件")
        self.start_button.setObjectName("primaryBtn")
        self.start_button.clicked.connect(lambda: self._start_run(False))
        footer_layout.addWidget(self.start_button)
        page_layout.addWidget(footer)
        return page

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        queue_card, queue_layout = self._card("运行队列与结果")
        run_row = QHBoxLayout()
        self.run_state_badge = QLabel("待运行")
        self.run_state_badge.setObjectName("batchRunBadge")
        run_row.addWidget(self.run_state_badge)
        self.cancel_button = QPushButton("停止")
        self.cancel_button.setObjectName("dangerBtn")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        run_row.addWidget(self.cancel_button)
        self.open_results_button = QPushButton("打开结果目录")
        self.open_results_button.setEnabled(False)
        self.open_results_button.clicked.connect(self._open_results)
        run_row.addWidget(self.open_results_button)
        self.open_image_button = QPushButton("打开选中图片")
        self.open_image_button.setEnabled(False)
        self.open_image_button.clicked.connect(self._open_selected_image)
        run_row.addWidget(self.open_image_button)
        self.retry_drawing_button = QPushButton("仅重试选中绘图")
        self.retry_drawing_button.setEnabled(False)
        self.retry_drawing_button.clicked.connect(self._retry_selected_drawing)
        run_row.addWidget(self.retry_drawing_button)
        self.continue_button = QPushButton("试运行通过，继续剩余文件")
        self.continue_button.setObjectName("primaryBtn")
        self.continue_button.hide()
        self.continue_button.clicked.connect(self._continue_after_trial)
        run_row.addWidget(self.continue_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        run_row.addWidget(self.progress, 1)
        queue_layout.addLayout(run_row)

        self.queue_table = QTableWidget(0, 6)
        self.queue_table.setHorizontalHeaderLabels(["#", "输入文件", "阶段", "状态", "耗时", "输出/说明"])
        self.queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.queue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.verticalHeader().setDefaultSectionSize(38)
        self.queue_table.setMinimumHeight(235)
        self.queue_table.itemSelectionChanged.connect(self._sync_selected_result)
        queue_layout.addWidget(self.queue_table, 2)

        selected_row = QHBoxLayout()
        selected_row.setSpacing(12)
        self.result_preview = QLabel("选择一项任务后可预览图片")
        self.result_preview.setObjectName("cardImage")
        self.result_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_preview.setFixedSize(210, 126)
        selected_row.addWidget(self.result_preview)
        self.selected_result_label = QLabel("任务完成后，这里会显示图片位置、失败阶段和可用操作。")
        self.selected_result_label.setObjectName("detailLabel")
        self.selected_result_label.setWordWrap(True)
        selected_row.addWidget(self.selected_result_label, 1)
        queue_layout.addLayout(selected_row)

        log_title = QLabel("运行记录")
        log_title.setObjectName("paneTitle")
        queue_layout.addWidget(log_title)
        self.run_log = QPlainTextEdit()
        self.run_log.setObjectName("batchLog")
        self.run_log.setReadOnly(True)
        self.run_log.setMaximumBlockCount(5000)
        self.run_log.setMinimumHeight(150)
        queue_layout.addWidget(self.run_log, 1)
        self.run_summary_label = QLabel("尚未运行")
        self.run_summary_label.setObjectName("detailLabel")
        self.run_summary_label.setWordWrap(True)
        queue_layout.addWidget(self.run_summary_label)
        self.results_scroll = self._scroll_page(queue_card, 620)
        layout.addWidget(self.results_scroll, 1)

        footer = QFrame()
        footer.setObjectName("workflowFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 10, 12, 10)
        back_config = QPushButton("返回流程设置")
        back_config.clicked.connect(lambda: self.page_stack.setCurrentIndex(1))
        footer_layout.addWidget(back_config)
        footer_layout.addStretch(1)
        layout.addWidget(footer)
        return page

    def _select_default_style(self) -> None:
        styles = [
            style
            for style in core.get_all_bundle_styles()
            if str(style.get("surface_mode") or "") == "volume_mapped"
        ]
        if not styles:
            self._sync_style_card()
            return
        style = next((item for item in styles if item.get("id") == DEFAULT_STYLE_ID), styles[0])
        rep0 = list(style.get("rep0_commands") or [])
        payload = {
            "style": copy.deepcopy(style),
            "rep0_commands": rep0,
            "selection_text": f"套装风格：{style.get('name')}",
            "mode": "bundle",
            "bundle_id": str(style.get("id") or ""),
            "iso_id": str(style.get("id") or ""),
            "skeleton_id": "",
        }
        payload["hash"] = _snapshot_hash(payload)
        self.style_snapshot = payload
        self._sync_style_card()

    def _choose_style(self) -> None:
        if self.is_running():
            QMessageBox.information(self, "任务运行中", "请先停止当前任务，再更换绘图方案。")
            return
        dialog = AutomationStyleDialog(self.style_snapshot, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.style_snapshot = dialog.selection()
        self._sync_style_card()
        self._configuration_changed()

    def _sync_style_card(self) -> None:
        style = self.style_snapshot.get("style") or {}
        if not style:
            self.style_preview.setPixmap(QPixmap())
            self.style_name_label.setText("尚未选择兼容方案")
            self.style_selection_label.clear()
            self.style_meta_label.clear()
            self.view_style_button.setEnabled(False)
        else:
            self.style_preview.setPixmap(_style_preview(style, 210, 126))
            self.style_name_label.setText(str(style.get("name") or "未命名方案"))
            self.style_selection_label.setText(str(self.style_snapshot.get("selection_text") or ""))
            self.style_meta_label.setText(_style_summary(style))
            self.view_style_button.setEnabled(True)
        self._sync_summary()

    def _view_style_parameters(self) -> None:
        style = self.style_snapshot.get("style") or {}
        if not style:
            QMessageBox.information(self, "尚未选择方案", "请先选择一个绘图方案。")
            return
        StyleParameterDialog(
            copy.deepcopy(style),
            list(self.style_snapshot.get("rep0_commands") or []),
            str(self.style_snapshot.get("selection_text") or "当前自动化绘图方案"),
            self,
        ).exec()

    def _paths_from_directory(self, directory: Path) -> list[Path]:
        iterator: Iterable[Path]
        iterator = directory.rglob("*") if self.recursive_check.isChecked() else directory.iterdir()
        return [path for path in iterator if _is_supported_input(path)]

    def _handle_dropped_paths(self, raw_paths: object) -> None:
        paths: list[Path] = []
        for raw in list(raw_paths or []):
            path = Path(str(raw)).expanduser()
            if path.is_dir():
                paths.extend(self._paths_from_directory(path))
            elif _is_supported_input(path):
                paths.append(path)
        self._append_files(paths)

    def _add_files(self) -> None:
        patterns = "波函数文件 (*.fch *.fchk *.wfn *.wfx *.mwfn *.molden *.molden.input);;所有文件 (*)"
        selected, _ = QFileDialog.getOpenFileNames(self, "添加输入文件", "", patterns)
        self._append_files(Path(path) for path in selected)

    def _add_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择包含计算文件的文件夹")
        if selected:
            self._append_files(self._paths_from_directory(Path(selected)))

    def _append_files(self, paths: Iterable[Path]) -> None:
        if self.is_running():
            QMessageBox.information(self, "任务运行中", "请先停止当前任务，再修改文件列表。")
            return
        existing = {os.path.normcase(str(path)) for path in self.files}
        ignored = 0
        for path in paths:
            try:
                resolved = path.expanduser().resolve()
            except OSError:
                ignored += 1
                continue
            key = os.path.normcase(str(resolved))
            if key in existing or not _is_supported_input(resolved):
                ignored += 1
                continue
            self.files.append(resolved)
            self.file_enabled[key] = True
            existing.add(key)
        self.files.sort(key=lambda path: str(path).casefold())
        self._refresh_file_table()
        self._invalidate_trial()
        if ignored:
            self._append_log(f"已忽略 {ignored} 个重复项或不支持的文件。")

    def _refresh_file_table(self) -> None:
        self.file_table.blockSignals(True)
        self.file_table.setRowCount(0)
        for path in self.files:
            row = self.file_table.rowCount()
            self.file_table.insertRow(row)
            key = os.path.normcase(str(path))
            enabled = QTableWidgetItem()
            enabled.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            enabled.setCheckState(Qt.CheckState.Checked if self.file_enabled.get(key, True) else Qt.CheckState.Unchecked)
            enabled.setData(Qt.ItemDataRole.UserRole, str(path))
            self.file_table.setItem(row, 0, enabled)
            file_item = QTableWidgetItem(path.name)
            file_item.setToolTip(str(path))
            self.file_table.setItem(row, 1, file_item)
            suffix = ".molden.input" if path.name.casefold().endswith(".molden.input") else path.suffix.casefold()
            self.file_table.setItem(row, 2, QTableWidgetItem(suffix))
        self.file_table.blockSignals(False)
        enabled_count = len(self._enabled_files())
        self.file_count_label.setText(f"{enabled_count}/{len(self.files)} 个文件")
        self._sync_summary()

    def _on_file_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        raw = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if raw:
            self.file_enabled[os.path.normcase(raw)] = item.checkState() == Qt.CheckState.Checked
        self.file_count_label.setText(f"{len(self._enabled_files())}/{len(self.files)} 个文件")
        self._invalidate_trial()
        self._sync_summary()

    def _remove_selected_files(self) -> None:
        if self.is_running():
            return
        selected_rows = {index.row() for index in self.file_table.selectionModel().selectedRows()}
        if not selected_rows:
            return
        removed = {str(self.file_table.item(row, 0).data(Qt.ItemDataRole.UserRole)) for row in selected_rows}
        removed_keys = {os.path.normcase(value) for value in removed}
        self.files = [path for path in self.files if os.path.normcase(str(path)) not in removed_keys]
        for key in removed_keys:
            self.file_enabled.pop(key, None)
        self._refresh_file_table()
        self._invalidate_trial()

    def _clear_files(self) -> None:
        if self.is_running():
            return
        self.files.clear()
        self.file_enabled.clear()
        self._refresh_file_table()
        self._invalidate_trial()

    def _enabled_files(self) -> list[Path]:
        return [path for path in self.files if self.file_enabled.get(os.path.normcase(str(path)), True)]

    def _pick_output_dir(self) -> None:
        current = self.output_dir_edit.text().strip() or str(self.storage_dir)
        selected = QFileDialog.getExistingDirectory(self, "选择自动化结果目录", current)
        if selected:
            self.output_dir_edit.setText(selected)

    def _on_output_location_changed(self) -> None:
        input_mode = self.output_location_combo.currentData() == "input_directory"
        self.output_dir_edit.setPlaceholderText(
            "运行记录与汇总文件的保存位置；图片将保存到各输入文件目录"
            if input_mode
            else "自动化结果、运行记录和汇总文件的保存位置"
        )
        self._configuration_changed()

    def _on_render_mode_changed(self, *_args) -> None:
        draws_image = self.render_mode_combo.currentData() != "cubes_only"
        self.image_size_combo.setEnabled(draws_image)
        self._configuration_changed()

    def _on_image_size_changed(self, *_args) -> None:
        custom = self.image_size_combo.currentData() is None
        if hasattr(self, "custom_width_spin"):
            self.custom_width_spin.setEnabled(custom)
            self.custom_height_spin.setEnabled(custom)
        if custom and hasattr(self, "advanced_toggle"):
            self.advanced_toggle.setChecked(True)
        self._configuration_changed()

    def _configuration_changed(self, *_args) -> None:
        self._invalidate_trial()
        self._sync_summary()

    def _sync_summary(self) -> None:
        if not hasattr(self, "workflow_summary_label"):
            return
        count = len(self._enabled_files())
        style = self.style_snapshot.get("style") or {}
        style_name = str(style.get("name") or "未选择")
        size = self.image_size_combo.currentData() if hasattr(self, "image_size_combo") else (1600, 1200)
        width, height = (
            size
            if isinstance(size, tuple)
            else (
                int(self.custom_width_spin.value()),
                int(self.custom_height_spin.value()),
            )
        )
        location = self.output_location_combo.currentText() if hasattr(self, "output_location_combo") else "集中保存到结果目录"
        iso = self.rho_iso_edit.text().strip() if hasattr(self, "rho_iso_edit") else "0.001"
        render_mode = self.render_mode_combo.currentData() if hasattr(self, "render_mode_combo") else "automatic"
        drawing_summary = (
            "仅生成 Cube，不启动 VMD"
            if render_mode == "cubes_only"
            else f"VMD 套用“{style_name}” · {width} × {height}"
        )
        self.workflow_summary_label.setText(
            f"{count} 个文件 · Multiwfn 生成电子密度/ESP Cube → 网格检查 → "
            f"{drawing_summary} · 等值面 {iso or '未填写'} a.u.\n"
            f"{location} · {'保留 Cube' if getattr(self, 'keep_cubes_check', None) and self.keep_cubes_check.isChecked() else '成功后清理 Cube'}"
        )
        self.config_ready_label.setText("可以运行" if count and style else ("尚未选择方案" if count else "尚未添加文件"))

    def _settings(self) -> dict:
        raw_iso = self.rho_iso_edit.text().strip().replace(",", ".")
        if not raw_iso:
            raise ValueError("请填写电子密度等值面。")
        try:
            iso = float(raw_iso)
        except ValueError as exc:
            raise ValueError("电子密度等值面必须是有效数字。") from exc
        if not 0 < iso < float("inf"):
            raise ValueError("电子密度等值面必须是大于零的有限数字。")
        style = self.style_snapshot.get("style") or {}
        if str(style.get("surface_mode") or "") != "volume_mapped":
            raise ValueError("请选择一个支持 ESP 体数据映射的绘图方案。")
        size = self.image_size_combo.currentData()
        width, height = (
            size
            if isinstance(size, tuple)
            else (
                int(self.custom_width_spin.value()),
                int(self.custom_height_spin.value()),
            )
        )
        return {
            "rho_iso": format(iso, ".12g"),
            "style_snapshot": copy.deepcopy(self.style_snapshot),
            "render_mode": str(self.render_mode_combo.currentData() or "automatic"),
            "width": int(width),
            "height": int(height),
            "output_location": str(self.output_location_combo.currentData() or "result_root"),
            "keep_cubes": self.keep_cubes_check.isChecked(),
            "vmd_timeout_seconds": int(self.vmd_timeout_spin.value()),
        }

    def _validated_inputs(self) -> tuple[list[Path], Path, Path, Path, dict]:
        files = self._enabled_files()
        if not files:
            raise ValueError("请先添加并启用至少一个输入文件。")
        multi_raw = self.multiwfn_path_getter().strip()
        multi = Path(multi_raw).expanduser() if multi_raw else Path()
        if not multi_raw or not multi.is_file():
            raise ValueError("请先在左侧设置有效的 Multiwfn.exe 路径。")
        vmd_raw = self.vmd_path_getter().strip()
        vmd = Path(vmd_raw).expanduser() if vmd_raw else Path()
        if self.render_mode_combo.currentData() != "cubes_only" and (
            not vmd_raw or not vmd.is_file()
        ):
            raise ValueError("请先在左侧设置有效的 vmd.exe 路径。")
        if self.render_mode_combo.currentData() == "cubes_only" and not vmd_raw:
            vmd = multi
        output_raw = self.output_dir_edit.text().strip()
        if not output_raw:
            raise ValueError("请选择自动化运行记录与结果目录。")
        output_root = Path(output_raw).expanduser()
        output_root.mkdir(parents=True, exist_ok=True)
        return files, output_root.resolve(), multi.resolve(), vmd.resolve(), self._settings()

    def _configuration_signature(self) -> str:
        try:
            settings = self._settings()
        except ValueError:
            return ""
        payload = {
            "files": [os.path.normcase(str(path)) for path in self._enabled_files()],
            "settings": settings,
            "output": self.output_dir_edit.text().strip(),
        }
        return _snapshot_hash(payload)

    def _invalidate_trial(self) -> None:
        if self.is_running():
            return
        self._trial_input = ""
        self._trial_signature = ""
        if hasattr(self, "continue_button"):
            self.continue_button.hide()

    def _populate_queue(self, files: list[Path]) -> None:
        self.last_result = {}
        self.open_image_button.setEnabled(False)
        self.retry_drawing_button.setEnabled(False)
        self.result_preview.setPixmap(QPixmap())
        self.result_preview.setText("选择一项任务后可预览图片")
        self.selected_result_label.setText("任务完成后，这里会显示图片位置、失败阶段和可用操作。")
        self.queue_table.setRowCount(0)
        for index, path in enumerate(files, 1):
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            self.queue_table.setItem(row, 0, QTableWidgetItem(str(index)))
            file_item = QTableWidgetItem(path.name)
            file_item.setData(Qt.ItemDataRole.UserRole, str(path))
            file_item.setToolTip(str(path))
            self.queue_table.setItem(row, 1, file_item)
            self.queue_table.setItem(row, 2, QTableWidgetItem("等待"))
            self.queue_table.setItem(row, 3, QTableWidgetItem("待运行"))
            self.queue_table.setItem(row, 4, QTableWidgetItem("-"))
            self.queue_table.setItem(row, 5, QTableWidgetItem("等待运行"))

    def _start_run(
        self,
        trial: bool,
        files_override: list[Path] | None = None,
        resume_manifest: Path | None = None,
    ) -> None:
        if self.is_running():
            return
        try:
            files, output_root, multi, vmd, settings = self._validated_inputs()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法开始", str(exc))
            return
        if files_override is not None:
            files = list(files_override)
        elif trial:
            files = files[:1]
        if not files:
            QMessageBox.information(self, "无需继续", "没有尚未运行的文件。")
            return

        worker_files = list(files)
        display_files = self._enabled_files() if resume_manifest is not None else worker_files
        self._run_files = display_files
        self._run_mode = "trial" if trial else "batch"
        self._cancel_requested = False
        self._populate_queue(display_files)
        if resume_manifest is not None and self._trial_input:
            for row in range(self.queue_table.rowCount()):
                item = self.queue_table.item(row, 1)
                if item and os.path.normcase(str(item.data(Qt.ItemDataRole.UserRole) or "")) == self._trial_input:
                    self.queue_table.setItem(row, 2, QTableWidgetItem("首文件试运行"))
                    self.queue_table.setItem(row, 3, QTableWidgetItem("完成"))
                    self.queue_table.setItem(row, 5, QTableWidgetItem("已通过，不会重复计算"))
                    break
        self.run_log.clear()
        self.progress.setRange(0, len(display_files))
        self.progress.setValue(0)
        self._set_run_state("准备试运行" if trial else "准备运行", "running")
        self.run_summary_label.setText("正在建立完整自动化任务……")
        self.continue_button.hide()
        self.cancel_button.setEnabled(True)
        self.trial_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.open_results_button.setEnabled(False)
        self.page_stack.setCurrentIndex(2)
        self.settingsChanged.emit(
            {
                "automatic_output_dir": str(output_root),
                "automatic_workflow_settings": copy.deepcopy(settings),
            }
        )

        self.thread = QThread(self)
        self.worker = AutomaticExecutionWorker(
            worker_files,
            output_root,
            settings,
            multi,
            vmd,
            self._run_mode,
            resume_manifest=resume_manifest,
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.event.connect(self._on_worker_event)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.finished.connect(lambda _result, _error: self.thread.quit() if self.thread else None)
        self.thread.finished.connect(self._cleanup_thread)
        self.thread.start()

    def _row_for_event(self, event: dict) -> int:
        raw_path = str(event.get("input_file") or event.get("file") or "")
        if raw_path:
            target = os.path.normcase(str(Path(raw_path)))
            for row in range(self.queue_table.rowCount()):
                item = self.queue_table.item(row, 1)
                if item and os.path.normcase(str(item.data(Qt.ItemDataRole.UserRole) or "")) == target:
                    return row
        try:
            index = int(event.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        if 1 <= index <= self.queue_table.rowCount():
            return index - 1
        if 0 <= index < self.queue_table.rowCount():
            return index
        return -1

    @Slot(object)
    def _on_worker_event(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        kind = str(event.get("kind") or event.get("type") or "")
        if kind == "run_started":
            self.last_run_dir = str(event.get("run_dir") or "")
            self.open_results_button.setEnabled(bool(self.last_run_dir))
            self._set_run_state("运行中", "running")
            self._append_log(f"开始执行 {event.get('total', len(self._run_files))} 个自动化任务。")
            return
        if kind == "output":
            source = str(event.get("source") or "程序")
            text = str(event.get("text") or "")
            if text:
                self._append_log(f"[{source}] {text}")
            return
        if kind == "progress":
            current = int(event.get("current") or event.get("completed") or 0)
            total = int(event.get("total") or len(self._run_files) or 1)
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(max(0, min(current, total)))
            return
        if kind == "job_stage":
            row = self._row_for_event(event)
            if row < 0:
                return
            stage = str(event.get("stage") or "")
            stage_text = {
                "multiwfn": "Multiwfn 计算",
                "cube_validation": "Cube 校验",
                "vmd_render": "VMD 渲染",
                "collect": "整理结果",
            }.get(stage, stage or "运行中")
            status = str(event.get("status") or "running")
            status_text = {
                "pending": "等待",
                "running": "进行中",
                "success": "完成",
                "failed": "失败",
                "cancelled": "已取消",
            }.get(status, status)
            self.queue_table.setItem(row, 2, QTableWidgetItem(stage_text))
            self.queue_table.setItem(row, 3, QTableWidgetItem(status_text))
            message = str(event.get("message") or event.get("output") or "")
            if message:
                self.queue_table.setItem(row, 5, QTableWidgetItem(message))
            elapsed = event.get("elapsed_seconds")
            if elapsed is not None:
                self.queue_table.setItem(row, 4, QTableWidgetItem(f"{float(elapsed):.1f} 秒"))
            return
        if kind == "run_finished":
            run_dir = str(event.get("run_dir") or "")
            if run_dir:
                self.last_run_dir = run_dir
                self.open_results_button.setEnabled(True)

    @Slot(object, object)
    def _on_worker_finished(self, result: object, error: object) -> None:
        self.cancel_button.setEnabled(False)
        self.trial_button.setEnabled(True)
        self.start_button.setEnabled(True)
        if error is not None:
            self._set_run_state("运行失败", "failed")
            self.run_summary_label.setText(str(error))
            self._append_log(f"[错误] {error}")
            return

        self.last_result = dict(result) if isinstance(result, dict) else {}

        run_dir = str(_result_value(result, "run_dir", "") or "")
        if run_dir:
            self.last_run_dir = run_dir
            self.open_results_button.setEnabled(True)
        success = int(_result_value(result, "success", 0) or 0)
        failed = int(_result_value(result, "failed", 0) or 0)
        cancelled = int(_result_value(result, "cancelled", 0) or 0)
        total = int(_result_value(result, "total", len(self._run_files)) or len(self._run_files))
        status = str(_result_value(result, "status", "") or "")
        completed_ok = failed == 0 and cancelled == 0 and success >= total and status not in {"failed", "cancelled"}
        if self._cancel_requested or status == "cancelled":
            self._set_run_state("已停止", "warning")
        elif completed_ok:
            self._set_run_state("全部完成", "success")
        elif success:
            self._set_run_state("部分完成", "warning")
        else:
            self._set_run_state("运行失败", "failed")
        self.progress.setValue(min(total, success + failed + cancelled))
        self.run_summary_label.setText(
            f"共 {total} 个任务：成功 {success}，失败 {failed}，取消 {cancelled}。"
            + (f"\n结果目录：{self.last_run_dir}" if self.last_run_dir else "")
        )
        for job in list(self.last_result.get("jobs") or []):
            if not isinstance(job, dict):
                continue
            row = self._row_for_event(
                {
                    "input_file": job.get("input_path", ""),
                    "index": job.get("index", 0),
                }
            )
            if row < 0:
                continue
            file_item = self.queue_table.item(row, 1)
            if file_item is not None:
                file_item.setData(Qt.ItemDataRole.UserRole + 1, str(job.get("id") or ""))
            stage = str(job.get("stage") or "")
            self.queue_table.setItem(
                row,
                2,
                QTableWidgetItem(
                    {
                        "multiwfn": "Multiwfn 计算",
                        "cube_validation": "Cube 校验",
                        "vmd_render": "VMD 渲染",
                        "collect": "整理结果",
                    }.get(stage, stage or "完成")
                ),
            )
            status_value = str(job.get("status") or "")
            self.queue_table.setItem(
                row,
                3,
                QTableWidgetItem(
                    {
                        "success": "完成",
                        "failed": "失败",
                        "timeout": "超时",
                        "cancelled": "已取消",
                    }.get(status_value, status_value)
                ),
            )
            self.queue_table.setItem(
                row,
                4,
                QTableWidgetItem(f"{float(job.get('duration_seconds') or 0):.1f} 秒"),
            )
            explanation = str(job.get("error") or job.get("image_path") or "完成")
            self.queue_table.setItem(row, 5, QTableWidgetItem(explanation))
        if self.queue_table.rowCount() and self.queue_table.currentRow() < 0:
            self.queue_table.selectRow(0)
        self._sync_selected_result()

        if self._run_mode == "trial" and completed_ok and self._run_files:
            self._trial_input = os.path.normcase(str(self._run_files[0]))
            self._trial_signature = self._configuration_signature()
            remaining = [
                path
                for path in self._enabled_files()
                if os.path.normcase(str(path)) != self._trial_input
            ]
            if remaining:
                self.continue_button.setText(f"试运行通过，继续剩余 {len(remaining)} 个文件")
                self.continue_button.show()

    def _continue_after_trial(self) -> None:
        if not self._trial_input or self._trial_signature != self._configuration_signature():
            self.continue_button.hide()
            QMessageBox.information(self, "需要重新试运行", "文件或流程设置已经变化，请重新执行首文件试运行。")
            return
        remaining = [
            path
            for path in self._enabled_files()
            if os.path.normcase(str(path)) != self._trial_input
        ]
        manifest = Path(str(self.last_result.get("manifest") or ""))
        if not manifest.is_file():
            self.continue_button.hide()
            QMessageBox.information(self, "无法继续", "首文件试运行记录不存在，请重新试运行。")
            return
        self._start_run(False, remaining, resume_manifest=manifest)

    def _append_log(self, text: str) -> None:
        if hasattr(self, "run_log"):
            self.run_log.appendPlainText(text.rstrip())

    def _set_run_state(self, text: str, state: str) -> None:
        self.run_state_badge.setText(text)
        self.run_state_badge.setProperty("state", state)
        self.run_state_badge.style().unpolish(self.run_state_badge)
        self.run_state_badge.style().polish(self.run_state_badge)

    def _selected_job_result(self) -> dict | None:
        row = self.queue_table.currentRow()
        if row < 0:
            return None
        file_item = self.queue_table.item(row, 1)
        if file_item is None:
            return None
        job_id = str(file_item.data(Qt.ItemDataRole.UserRole + 1) or "")
        input_path = os.path.normcase(str(file_item.data(Qt.ItemDataRole.UserRole) or ""))
        for job in list(self.last_result.get("jobs") or []):
            if not isinstance(job, dict):
                continue
            if job_id and str(job.get("id") or "") == job_id:
                return job
            if input_path and os.path.normcase(str(job.get("input_path") or "")) == input_path:
                return job
        return None

    def _sync_selected_result(self) -> None:
        job = self._selected_job_result()
        if job is None:
            self.open_image_button.setEnabled(False)
            self.retry_drawing_button.setEnabled(False)
            return
        image_path = Path(str(job.get("image_path") or ""))
        if image_path.is_file():
            pixmap = QPixmap(str(image_path))
            if not pixmap.isNull():
                self.result_preview.setText("")
                self.result_preview.setPixmap(
                    pixmap.scaled(
                        self.result_preview.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            self.open_image_button.setEnabled(True)
        else:
            self.result_preview.setPixmap(QPixmap())
            self.result_preview.setText("尚未生成图片")
            self.open_image_button.setEnabled(False)
        error = str(job.get("error") or "")
        failed_stage = str(job.get("failed_stage") or "")
        outputs = list(job.get("outputs") or [])
        detail = (
            f"状态：{job.get('status', '')}"
            + (f" · 失败阶段：{failed_stage}" if failed_stage else "")
            + (f"\n{error}" if error else "")
            + (f"\n图片：{image_path}" if image_path.is_file() else "")
            + (f"\n已归档 {len(outputs)} 个结果文件" if outputs else "")
        )
        self.selected_result_label.setText(detail)
        self.retry_drawing_button.setEnabled(
            bool(job.get("can_retry_drawing")) and not self.is_running()
        )

    def _open_selected_image(self) -> None:
        job = self._selected_job_result()
        image_path = Path(str((job or {}).get("image_path") or ""))
        if image_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(image_path.resolve())))

    def _retry_selected_drawing(self) -> None:
        if self.is_running():
            return
        job = self._selected_job_result()
        if not job or not job.get("can_retry_drawing"):
            QMessageBox.information(self, "无法重试", "选中的任务没有可复用的 Cube 绘图结果。")
            return
        manifest = Path(str(self.last_result.get("manifest") or ""))
        vmd_raw = self.vmd_path_getter().strip()
        vmd = Path(vmd_raw).expanduser() if vmd_raw else Path()
        if not manifest.is_file() or not vmd_raw or not vmd.is_file():
            QMessageBox.warning(self, "无法重试", "运行记录或 vmd.exe 路径不可用。")
            return
        self._run_mode = "retry"
        self._cancel_requested = False
        self.cancel_button.setEnabled(True)
        self.trial_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.retry_drawing_button.setEnabled(False)
        self._set_run_state("正在重试绘图", "running")
        self._append_log("仅重试 VMD 绘图，不重新运行 Multiwfn。")
        self.thread = QThread(self)
        self.worker = AutomaticRetryWorker(
            manifest, str(job.get("id") or ""), vmd.resolve()
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.event.connect(self._on_worker_event)
        self.worker.finished.connect(self._on_retry_finished)
        self.worker.finished.connect(
            lambda _result, _error: self.thread.quit() if self.thread else None
        )
        self.thread.finished.connect(self._cleanup_thread)
        self.thread.start()

    @Slot(object, object)
    def _on_retry_finished(self, result: object, error: object) -> None:
        self.cancel_button.setEnabled(False)
        self.trial_button.setEnabled(True)
        self.start_button.setEnabled(True)
        if error is not None or not isinstance(result, dict):
            message = str(error or "重试绘图失败。")
            self._set_run_state("重试失败", "failed")
            self._append_log(f"[错误] {message}")
            self._sync_selected_result()
            return
        jobs = list(self.last_result.get("jobs") or [])
        for index, job in enumerate(jobs):
            if isinstance(job, dict) and str(job.get("id") or "") == str(result.get("id") or ""):
                jobs[index] = dict(result)
                break
        self.last_result["jobs"] = jobs
        row = self._row_for_event(
            {"input_file": result.get("input_path", ""), "index": result.get("index", 0)}
        )
        if row >= 0:
            self.queue_table.setItem(row, 2, QTableWidgetItem("整理结果"))
            self.queue_table.setItem(
                row,
                3,
                QTableWidgetItem("完成" if result.get("status") == "success" else "失败"),
            )
            self.queue_table.setItem(
                row,
                4,
                QTableWidgetItem(f"{float(result.get('duration_seconds') or 0):.1f} 秒"),
            )
            self.queue_table.setItem(
                row,
                5,
                QTableWidgetItem(str(result.get("error") or result.get("image_path") or "完成")),
            )
            self.queue_table.selectRow(row)
        if result.get("status") == "success":
            self._set_run_state("绘图重试成功", "success")
            self._append_log(f"绘图重试成功：{result.get('image_path', '')}")
        else:
            self._set_run_state("重试失败", "failed")
            self._append_log(f"绘图重试失败：{result.get('error', '')}")
        self._sync_selected_result()

    def _open_results(self) -> None:
        path = Path(self.last_run_dir) if self.last_run_dir else Path(self.output_dir_edit.text().strip())
        if not str(path) or not path.exists():
            QMessageBox.information(self, "结果目录不可用", "尚未生成可打开的结果目录。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def is_running(self) -> bool:
        esp_running = self.thread is not None and self.thread.isRunning()
        orbital_running = (
            hasattr(self, "orbital_page") and self.orbital_page.is_running()
        )
        return bool(esp_running or orbital_running)

    def cancel(self) -> None:
        if self.thread is not None and self.thread.isRunning():
            self._cancel_requested = True
            self.cancel_button.setEnabled(False)
            self._set_run_state("正在停止", "warning")
            self._append_log("正在停止当前 Multiwfn 或 VMD 进程……")
            if self.worker is not None:
                self.worker.cancel()
        if hasattr(self, "orbital_page") and self.orbital_page.is_running():
            self.orbital_page.cancel()

    def _cleanup_thread(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        if self.thread is not None:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None

    def cleanup(self) -> None:
        self.cancel()
        if hasattr(self, "orbital_page"):
            self.orbital_page.cleanup()

    def load_settings(self, config: dict) -> None:
        output = str(
            config.get("automatic_output_dir")
            or (Path(config.get("output_dir") or self.storage_dir) / "automatic_runs")
        )
        self.output_dir_edit.setText(output)
        if hasattr(self, "orbital_page"):
            self.orbital_page.load_settings(config)
        saved = config.get("automatic_workflow_settings")
        if not isinstance(saved, dict):
            self._sync_summary()
            return
        snapshot = saved.get("style_snapshot")
        if (
            isinstance(snapshot, dict)
            and isinstance(snapshot.get("style"), dict)
            and snapshot["style"].get("surface_mode") == "volume_mapped"
        ):
            self.style_snapshot = copy.deepcopy(snapshot)
            self._sync_style_card()
        self.rho_iso_edit.setText(str(saved.get("rho_iso") or "0.001"))
        render_index = self.render_mode_combo.findData(
            str(saved.get("render_mode") or "automatic")
        )
        self.render_mode_combo.setCurrentIndex(max(0, render_index))
        output_index = self.output_location_combo.findData(
            str(saved.get("output_location") or "result_root")
        )
        self.output_location_combo.setCurrentIndex(max(0, output_index))
        width = int(saved.get("width") or 1600)
        height = int(saved.get("height") or 1200)
        size_index = self.image_size_combo.findData((width, height))
        if size_index < 0:
            size_index = self.image_size_combo.count() - 1
            self.custom_width_spin.setValue(max(320, min(7680, width)))
            self.custom_height_spin.setValue(max(240, min(4320, height)))
        self.image_size_combo.setCurrentIndex(size_index)
        self.keep_cubes_check.setChecked(bool(saved.get("keep_cubes", True)))
        self.vmd_timeout_spin.setValue(
            max(30, min(86400, int(saved.get("vmd_timeout_seconds") or 600)))
        )
        self._on_render_mode_changed()
        self._on_image_size_changed()
        self._sync_summary()


# The singular alias keeps the integration point readable in MainWindow while the
# plural class name mirrors the user-facing module title.
AutomationWorkflowPage = AutomaticWorkflowsPage
