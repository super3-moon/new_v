from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import orbital_data
import vmd_style_tool as core
from PySide6.QtCore import QObject, QPoint, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QIntValidator,
    QPainter,
    QPalette,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
    QVBoxLayout,
    QWidget,
)


SUPPORTED_SUFFIXES = (".out", ".log", ".fch", ".fchk", ".molden", ".molden.input")


def _is_supported_file(path: Path) -> bool:
    name = path.name.casefold()
    return path.is_file() and any(name.endswith(suffix) for suffix in SUPPORTED_SUFFIXES)


def _same_path(left: object, right: object) -> bool:
    left_text = str(left or "")
    right_text = str(right or "")
    if not left_text or not right_text:
        return False
    if os.path.normcase(os.path.abspath(left_text)) == os.path.normcase(
        os.path.abspath(right_text)
    ):
        return True
    try:
        return os.path.samefile(left_text, right_text)
    except OSError:
        return False


def _style_hash(payload: dict[str, Any]) -> str:
    value = copy.deepcopy(payload)
    value.pop("hash", None)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    method = getattr(value, "to_dict", None)
    if callable(method):
        result = method()
        if isinstance(result, dict):
            return result
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


class OrbitalOffsetComboBox(QComboBox):
    """Editable offset selector with a drop indicator painted by this widget.

    The host application has theme rules for combo boxes.  Relying on the
    platform/theme arrow made the editable HOMO/LUMO selectors look like plain
    text fields on some Windows installations, so this small indicator is
    deliberately drawn after the normal combo box paint pass.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("explicitDropIndicator", True)

    def drop_indicator_rect(self):
        width = 18
        return self.rect().adjusted(self.width() - width, 0, -2, 0)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        rect = self.drop_indicator_rect()
        center = rect.center()
        color_role = (
            QPalette.ColorRole.Text
            if self.isEnabled()
            else QPalette.ColorRole.PlaceholderText
        )
        color = QColor(self.palette().color(color_role))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(
            QPolygon(
                [
                    QPoint(center.x() - 4, center.y() - 2),
                    QPoint(center.x() + 4, center.y() - 2),
                    QPoint(center.x(), center.y() + 3),
                ]
            )
        )


class _InputProcessingCancelled(Exception):
    pass


class OrbitalInputWorker(QObject):
    """Expand, pair and parse uploaded files away from the GUI thread."""

    progress = Signal(int, str, int, int)
    finished = Signal(int, object, object)

    def __init__(
        self,
        generation: int,
        source_paths: list[Path],
        manual_pairs: list[orbital_data.InputPair],
    ) -> None:
        super().__init__()
        self.generation = int(generation)
        self.source_paths = [Path(path) for path in source_paths]
        self.manual_pairs = list(manual_pairs)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def _check_cancelled(self) -> None:
        if self._cancel_requested:
            raise _InputProcessingCancelled()

    def _expand_paths(self) -> list[Path]:
        result: list[Path] = []
        for raw_index, raw in enumerate(self.source_paths, start=1):
            self._check_cancelled()
            path = Path(raw).expanduser()
            self.progress.emit(
                self.generation,
                f"正在查找文件：{path.name or path}",
                raw_index - 1,
                0,
            )
            if path.is_dir():
                try:
                    for item in path.rglob("*"):
                        self._check_cancelled()
                        if _is_supported_file(item):
                            result.append(item)
                except OSError:
                    continue
            elif _is_supported_file(path):
                result.append(path)

        unique: list[Path] = []
        seen: set[str] = set()
        for path in result:
            self._check_cancelled()
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                unique.append(resolved)
        return unique

    @staticmethod
    def _validation_text(
        pair: orbital_data.InputPair,
        dataset: orbital_data.OrbitalDataset | None,
        error: Exception | None,
    ) -> tuple[bool, str]:
        if error is not None:
            return False, f"读取失败：{error}"
        validation = dataset.pair_validation if dataset is not None else None
        valid = bool(validation and validation.is_valid)
        warnings = list(pair.warnings)
        if validation:
            warnings.extend(validation.warnings)
        if valid:
            text = "核验通过"
            if warnings:
                text += f"；请留意：{'；'.join(warnings)}"
            return True, text
        errors = list(validation.errors) if validation else ["没有获得核验结果"]
        return False, f"核验失败：{'；'.join(errors)}"

    def _process(self) -> dict[str, Any]:
        input_files = self._expand_paths()
        self._check_cancelled()
        available = {os.path.normcase(str(path)) for path in input_files}
        manual_pairs = [
            pair
            for pair in self.manual_pairs
            if os.path.normcase(str(pair.output_path)) in available
            and os.path.normcase(str(pair.wavefunction_path)) in available
        ]
        manual_paths = {
            os.path.normcase(str(path))
            for pair in manual_pairs
            for path in (pair.output_path, pair.wavefunction_path)
        }
        remaining = [
            path
            for path in input_files
            if os.path.normcase(str(path)) not in manual_paths
        ]
        pairs = list(manual_pairs)
        unpaired_issue = ""
        if remaining:
            try:
                pairs.extend(orbital_data.pair_input_files(remaining))
            except orbital_data.OrbitalDataError as exc:
                unpaired_issue = str(exc)
        self._check_cancelled()

        datasets: list[orbital_data.OrbitalDataset | None] = []
        validities: list[bool] = []
        validation_texts: list[str] = []
        total = len(pairs)
        for index, pair in enumerate(pairs, start=1):
            self._check_cancelled()
            self.progress.emit(
                self.generation,
                f"正在读取 {index}/{total}：{pair.label}",
                index - 1,
                total,
            )
            dataset: orbital_data.OrbitalDataset | None = None
            parse_error: Exception | None = None
            try:
                dataset = orbital_data.parse_input_pair(
                    pair.output_path,
                    pair.wavefunction_path,
                    strict=False,
                )
            except orbital_data.OrbitalDataError as exc:
                parse_error = exc
            valid, validation_text = self._validation_text(
                pair, dataset, parse_error
            )
            datasets.append(dataset)
            validities.append(valid)
            validation_texts.append(validation_text)
            self.progress.emit(
                self.generation,
                f"已读取 {index}/{total}：{pair.label}",
                index,
                total,
            )
        return {
            "input_files": input_files,
            "manual_pairs": manual_pairs,
            "pairs": pairs,
            "datasets": datasets,
            "pair_validity": validities,
            "validation_texts": validation_texts,
            "unpaired_issue": unpaired_issue,
        }

    @Slot()
    def run(self) -> None:
        result: object = None
        error: object = None
        try:
            result = self._process()
        except _InputProcessingCancelled:
            result = {"cancelled": True}
        except Exception as exc:
            error = exc
        self.finished.emit(self.generation, result, error)


class OrbitalInputTable(QTableWidget):
    pathsDropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 3, parent)
        self.setAcceptDrops(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalHeaderLabels(["类型", "文件", "位置"])
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.setMinimumHeight(142)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(url.isLocalFile() for url in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.pathsDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class _SignedStyleDialog(QDialog):
    """Small fallback selector used when the host does not supply its richer dialog."""

    def __init__(
        self,
        current: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择分子轨道绘图方案")
        self.setModal(True)
        self.resize(610, 300)
        self.styles = [
            copy.deepcopy(style)
            for style in core.get_all_bundle_styles()
            if str(style.get("surface_mode") or "signed") == "signed"
        ]
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)
        hint = QLabel(
            "这里只列出具有正、负相位配色的绘图方案。它是 VMD 打开时的初始外观。"
        )
        hint.setObjectName("batchHint")
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.combo = QComboBox()
        selected_id = str((current or {}).get("bundle_id") or "")
        for style in self.styles:
            style_id = str(style.get("id") or "")
            name = str(style.get("name") or style_id or "未命名方案")
            material = str(style.get("material") or "Glossy")
            self.combo.addItem(f"{name} · {material}", style_id)
        selected_index = self.combo.findData(selected_id)
        self.combo.setCurrentIndex(max(0, selected_index))
        root.addWidget(self.combo)
        self.summary = QLabel()
        self.summary.setObjectName("detailLabel")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        root.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.combo.currentIndexChanged.connect(self._sync_summary)
        self._sync_summary()

    def _current_style(self) -> dict[str, Any]:
        style_id = str(self.combo.currentData() or "")
        return next(
            (copy.deepcopy(style) for style in self.styles if str(style.get("id") or "") == style_id),
            {},
        )

    def _sync_summary(self) -> None:
        style = self._current_style()
        if not style:
            self.summary.setText("没有可用的正负相位绘图方案。")
            return
        self.summary.setText(
            "正负相位颜色已配置 · "
            f"材质：{style.get('material') or 'Glossy'}"
        )

    def selection(self) -> dict[str, Any]:
        style = self._current_style()
        payload: dict[str, Any] = {
            "style": style,
            "rep0_commands": list(style.get("rep0_commands") or []),
            "selection_text": f"套装风格：{style.get('name') or '未命名方案'}",
            "mode": "bundle",
            "bundle_id": str(style.get("id") or ""),
            "iso_id": str(style.get("id") or ""),
            "skeleton_id": "",
        }
        payload["hash"] = _style_hash(payload)
        return payload


class OrbitalDiagramWorker(QObject):
    event = Signal(object)
    finished = Signal(object, object)

    def __init__(
        self,
        pairs: list[orbital_data.InputPair],
        output_root: Path,
        settings: dict[str, Any],
        multiwfn_path: Path,
        vmd_path: Path,
        *,
        resume_manifest: Path | None = None,
        retry_stages: list[str] | None = None,
        job_ids: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.pairs = list(pairs)
        self.output_root = Path(output_root)
        self.settings = copy.deepcopy(settings)
        self.multiwfn_path = Path(multiwfn_path)
        self.vmd_path = Path(vmd_path)
        self.resume_manifest = Path(resume_manifest) if resume_manifest else None
        self.retry_stages = list(retry_stages or [])
        self.job_ids = list(job_ids or [])
        self.runner: object | None = None
        self._cancel_pending = False

    def _forward_event(self, event: object = None, *args: object, **kwargs: object) -> None:
        if event is None and kwargs:
            event = kwargs
        elif args or kwargs:
            payload: dict[str, Any] = {"event": event, "args": list(args)}
            payload.update(kwargs)
            event = payload
        self.event.emit(event)

    def _make_runner(self, runner_class: type, plan: object) -> object:
        try:
            signature = inspect.signature(runner_class)
            parameters = signature.parameters
        except (TypeError, ValueError):
            parameters = {}

        positional: list[object] = [plan]
        kwargs: dict[str, object] = {}
        path_values = {
            "multiwfn_path": self.multiwfn_path,
            "multiwfn_exe": self.multiwfn_path,
            "multiwfn": self.multiwfn_path,
            "vmd_path": self.vmd_path,
            "vmd_exe": self.vmd_path,
            "vmd": self.vmd_path,
        }
        callback_names = {
            "event_callback",
            "progress_callback",
            "callback",
            "log_callback",
            "on_event",
        }
        if parameters:
            for name, parameter in list(parameters.items())[1:]:
                if name in path_values:
                    kwargs[name] = path_values[name]
                elif name in callback_names:
                    kwargs[name] = self._forward_event
                elif (
                    parameter.default is inspect.Parameter.empty
                    and parameter.kind
                    in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ):
                    lowered = name.casefold()
                    if "multi" in lowered:
                        positional.append(self.multiwfn_path)
                    elif "vmd" in lowered:
                        positional.append(self.vmd_path)
            return runner_class(*positional, **kwargs)

        for attempt in (
            lambda: runner_class(
                plan,
                self.multiwfn_path,
                self.vmd_path,
                event_callback=self._forward_event,
            ),
            lambda: runner_class(plan, self.multiwfn_path, self.vmd_path),
            lambda: runner_class(plan),
        ):
            try:
                return attempt()
            except TypeError:
                continue
        raise TypeError("无法按受支持的构造方式创建 OrbitalDiagramRunner。")

    @Slot()
    def run(self) -> None:
        result: object = None
        error: object = None
        try:
            workflow = importlib.import_module("orbital_diagram_workflow")
            runner_class = getattr(workflow, "OrbitalDiagramRunner")
            if self.resume_manifest is not None:
                plan = workflow.resume_orbital_diagram_plan(
                    self.resume_manifest,
                    retry_stages=self.retry_stages,
                    job_ids=self.job_ids,
                )
            else:
                create_plan = getattr(workflow, "create_orbital_diagram_plan")
                plan = create_plan(
                    self.pairs,
                    self.output_root,
                    self.settings,
                    prefix="orbital_diagram",
                )
            self.runner = self._make_runner(runner_class, plan)
            if self._cancel_pending:
                cancel = getattr(self.runner, "cancel", None)
                if callable(cancel):
                    cancel()
            result = self.runner.run()
        except Exception as exc:  # the UI presents workflow errors verbatim
            error = exc
        self.finished.emit(result, error)

    def cancel(self) -> None:
        self._cancel_pending = True
        cancel = getattr(self.runner, "cancel", None)
        if callable(cancel):
            cancel()


class OrbitalDiagramPage(QWidget):
    """Qt page for the paired orbital-diagram automation workflow."""

    settingsChanged = Signal(object)
    backRequested = Signal()

    def __init__(
        self,
        storage_dir: Path,
        multiwfn_path_getter: Callable[[], str],
        vmd_path_getter: Callable[[], str],
        style_dialog_factory: Callable[..., object] | None = None,
    ) -> None:
        super().__init__()
        self.storage_dir = Path(storage_dir)
        self.multiwfn_path_getter = multiwfn_path_getter
        self.vmd_path_getter = vmd_path_getter
        self.style_dialog_factory = style_dialog_factory
        self.input_files: list[Path] = []
        self.manual_pairs: list[orbital_data.InputPair] = []
        self.unpaired_issue = ""
        self.pairs: list[orbital_data.InputPair] = []
        self.datasets: list[orbital_data.OrbitalDataset] = []
        self.pair_validity: list[bool] = []
        self.style_snapshot: dict[str, Any] = {}
        self.thread: QThread | None = None
        self.worker: OrbitalDiagramWorker | None = None
        self.last_result: object = None
        self.last_run_dir = ""
        self._active_pairs: list[orbital_data.InputPair] = []
        self._active_job_ids: set[str] = set()
        self._saved_orbital_selections: list[dict[str, Any]] = []
        self._restore_selection_pending = False
        self._loading_settings = False
        self.input_thread: QThread | None = None
        self.input_worker: OrbitalInputWorker | None = None
        self._input_generation = 0
        self._input_busy = False
        self._run_started_monotonic = 0.0
        self._row_started_monotonic: dict[str, float] = {}
        self._progress_ceiling = 0
        self._progress_stage_text = "等待开始"
        self._last_progress_message = ""
        self._last_progress_tick = 0.0
        self._build_ui()
        self._runtime_timer = QTimer(self)
        self._runtime_timer.setInterval(1000)
        self._runtime_timer.timeout.connect(self._tick_runtime)
        self._select_default_style()
        self._refresh_selection_preview()

    @staticmethod
    def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("batchCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 15)
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
        return card, layout

    @staticmethod
    def _scroll_page(body: QWidget, minimum_height: int) -> QScrollArea:
        body.setMinimumHeight(minimum_height)
        scroll = QScrollArea()
        scroll.setObjectName("batchPageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(body)
        return scroll

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
        title = QLabel("分子轨道能级图")
        title.setObjectName("batchToolbarLabel")
        toolbar_layout.addWidget(title)
        toolbar_layout.addStretch(1)
        self.ready_label = QLabel("尚未添加输入")
        self.ready_label.setObjectName("batchPresetInline")
        toolbar_layout.addWidget(self.ready_label)
        root.addWidget(toolbar)

        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("batchResultStack")
        self.page_stack.addWidget(self._build_configuration_page())
        self.page_stack.addWidget(self._build_results_page())
        root.addWidget(self.page_stack, 1)

    def _build_configuration_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 8, 8)
        body_layout.setSpacing(12)

        input_card, input_layout = self._card(
            "1 · 添加并核验配对文件",
            "拖入或添加 Gaussian out/log + fch/fchk，或 ORCA out + molden/molden.input。软件会按程序与文件名自动配对，再核验终止状态、体系和轨道数据。",
        )
        input_actions = QHBoxLayout()
        self.add_input_button = QPushButton("添加文件")
        self.add_input_button.clicked.connect(self._browse_files)
        self.add_folder_button = QPushButton("扫描文件夹")
        self.add_folder_button.clicked.connect(self._browse_folder)
        self.manual_pair_button = QPushButton("配对选中的两个文件")
        self.manual_pair_button.clicked.connect(self._manual_pair_selected)
        self.remove_input_button = QPushButton("移除选中")
        self.remove_input_button.clicked.connect(self._remove_selected_files)
        self.clear_input_button = QPushButton("清空")
        self.clear_input_button.clicked.connect(self._clear_files)
        input_actions.addWidget(self.add_input_button)
        input_actions.addWidget(self.add_folder_button)
        input_actions.addWidget(self.manual_pair_button)
        input_actions.addWidget(self.remove_input_button)
        input_actions.addWidget(self.clear_input_button)
        input_actions.addStretch(1)
        self.input_count_label = QLabel("0 个文件")
        self.input_count_label.setObjectName("countPill")
        input_actions.addWidget(self.input_count_label)
        input_layout.addLayout(input_actions)
        input_progress_row = QHBoxLayout()
        self.input_progress_label = QLabel("等待添加文件")
        self.input_progress_label.setObjectName("batchHint")
        self.input_progress_label.setWordWrap(True)
        input_progress_row.addWidget(self.input_progress_label, 1)
        self.input_progress = QProgressBar()
        self.input_progress.setObjectName("orbitalInputProgress")
        self.input_progress.setRange(0, 1)
        self.input_progress.setValue(0)
        self.input_progress.setTextVisible(True)
        self.input_progress.setMinimumWidth(210)
        self.input_progress.hide()
        input_progress_row.addWidget(self.input_progress)
        self.cancel_input_button = QPushButton("停止读取")
        self.cancel_input_button.clicked.connect(self._cancel_input_processing)
        self.cancel_input_button.hide()
        input_progress_row.addWidget(self.cancel_input_button)
        input_layout.addLayout(input_progress_row)
        self.input_table = OrbitalInputTable()
        self.input_table.pathsDropped.connect(self._add_paths)
        input_layout.addWidget(self.input_table)

        self.pair_table = QTableWidget(0, 4)
        self.pair_table.setHorizontalHeaderLabels(["任务", "程序", "配对文件", "核验结果"])
        self.pair_table.verticalHeader().setVisible(False)
        self.pair_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pair_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pair_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.pair_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.pair_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.pair_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.pair_table.setMinimumHeight(124)
        input_layout.addWidget(self.pair_table)
        self.pair_status_label = QLabel("请至少添加一组配套文件。")
        self.pair_status_label.setObjectName("batchHint")
        self.pair_status_label.setWordWrap(True)
        input_layout.addWidget(self.pair_status_label)
        body_layout.addWidget(input_card)

        select_card, select_layout = self._card(
            "2 · 选择要绘制的轨道",
            "直接输入 HOMO 与 LUMO 的相对偏移，或从下拉列表选常用值。解析后还可逐项取消不需要的轨道。",
        )
        quick_grid = QGridLayout()
        quick_grid.setHorizontalSpacing(10)
        quick_grid.setVerticalSpacing(9)
        quick_grid.addWidget(QLabel("轨道范围"), 0, 0)
        range_layout = QHBoxLayout()
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(8)
        start_anchor_label = QLabel("HOMO")
        start_anchor_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        start_anchor_label.setMinimumWidth(52)
        start_anchor_label.setToolTip("起点固定以 HOMO 为基准")
        range_layout.addWidget(start_anchor_label)
        self.start_offset_combo = self._make_offset_combo(-1, "HOMO")
        range_layout.addWidget(self.start_offset_combo, 1)
        separator_label = QLabel("至")
        separator_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        range_layout.addWidget(separator_label)
        end_anchor_label = QLabel("LUMO")
        end_anchor_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        end_anchor_label.setMinimumWidth(52)
        end_anchor_label.setToolTip("终点固定以 LUMO 为基准")
        range_layout.addWidget(end_anchor_label)
        self.end_offset_combo = self._make_offset_combo(3, "LUMO")
        range_layout.addWidget(self.end_offset_combo, 1)
        range_layout.addStretch(2)
        quick_grid.addLayout(range_layout, 0, 1, 1, 5)

        quick_grid.addWidget(QLabel("自旋"), 1, 0)
        self.spin_combo = QComboBox()
        self.spin_combo.addItem("自动（受限轨道 / 非限制两通道）", "auto")
        self.spin_combo.addItem("α + β", "both")
        self.spin_combo.addItem("仅 α", "alpha")
        self.spin_combo.addItem("仅 β", "beta")
        self.spin_combo.addItem("空间轨道", "spatial")
        self.spin_combo.currentIndexChanged.connect(self._selection_controls_changed)
        quick_grid.addWidget(self.spin_combo, 1, 1, 1, 5)
        quick_grid.addWidget(QLabel("手动表达式（可选）"), 2, 0)
        self.manual_expression_edit = QLineEdit()
        self.manual_expression_edit.setPlaceholderText(
            "如 HOMO-2..LUMO+3；或 alpha:HOMO-1..LUMO+2; beta:HOMO..LUMO"
        )
        self.manual_expression_edit.textChanged.connect(self._selection_controls_changed)
        quick_grid.addWidget(self.manual_expression_edit, 2, 1, 1, 5)
        quick_grid.setColumnStretch(1, 1)
        select_layout.addLayout(quick_grid)

        self.orbital_table = QTableWidget(0, 6)
        self.orbital_table.setHorizontalHeaderLabels(
            ["文件", "绘制", "自旋", "轨道", "占据数", "能量 / eV"]
        )
        self.orbital_table.verticalHeader().setVisible(False)
        self.orbital_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.orbital_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.orbital_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.orbital_table.setMinimumHeight(210)
        self.orbital_table.itemChanged.connect(self._orbital_check_changed)
        select_layout.addWidget(self.orbital_table)
        self.selection_status_label = QLabel("添加并核验文件后，这里会逐项显示实际绘制的轨道。")
        self.selection_status_label.setObjectName("batchHint")
        self.selection_status_label.setWordWrap(True)
        select_layout.addWidget(self.selection_status_label)
        body_layout.addWidget(select_card)

        style_card, style_layout = self._card(
            "3 · 初始绘图方案与 VMD 交互",
            "分子轨道使用带正、负相位配色的绘图方案。",
        )
        style_row = QHBoxLayout()
        style_text = QVBoxLayout()
        self.style_name_label = QLabel("正在载入绘图方案……")
        self.style_name_label.setObjectName("workflowStyleName")
        self.style_name_label.setWordWrap(True)
        self.style_detail_label = QLabel()
        self.style_detail_label.setObjectName("detailLabel")
        self.style_detail_label.setWordWrap(True)
        style_text.addWidget(self.style_name_label)
        style_text.addWidget(self.style_detail_label)
        style_row.addLayout(style_text, 1)
        choose_style = QPushButton("选择绘图方案")
        choose_style.setObjectName("primaryBtn")
        choose_style.clicked.connect(self._choose_style)
        style_row.addWidget(choose_style)
        style_layout.addLayout(style_row)
        interaction = QLabel(
            "VMD 交互步骤：打开后自由调整一切，最后点“保存全部参数并确认”。无需开始记录。确认后其余轨道会复用同一视角与全部显示参数。"
        )
        interaction.setObjectName("batchPresetSummary")
        interaction.setWordWrap(True)
        style_layout.addWidget(interaction)
        body_layout.addWidget(style_card)

        output_card, output_layout = self._card(
            "4 · 等值面、渲染与输出",
            "每个轨道使用正、负两个等值面；最终图片统一交给 Tachyon 渲染。",
        )
        output_grid = QGridLayout()
        output_grid.setHorizontalSpacing(10)
        output_grid.setVerticalSpacing(10)
        output_grid.addWidget(QLabel("轨道等值面（a.u.）"), 0, 0)
        self.iso_spin = QDoubleSpinBox()
        self.iso_spin.setDecimals(6)
        self.iso_spin.setRange(0.000001, 10.0)
        self.iso_spin.setSingleStep(0.01)
        self.iso_spin.setValue(0.05)
        self.iso_spin.valueChanged.connect(self._configuration_changed)
        output_grid.addWidget(self.iso_spin, 0, 1)
        output_grid.addWidget(QLabel("渲染器"), 0, 2)
        renderer = QLineEdit("Tachyon")
        renderer.setReadOnly(True)
        output_grid.addWidget(renderer, 0, 3)
        output_grid.addWidget(QLabel("图片尺寸"), 1, 0)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(320, 7680)
        self.width_spin.setValue(1600)
        self.width_spin.valueChanged.connect(self._configuration_changed)
        output_grid.addWidget(self.width_spin, 1, 1)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(240, 4320)
        self.height_spin.setValue(1200)
        self.height_spin.valueChanged.connect(self._configuration_changed)
        output_grid.addWidget(self.height_spin, 1, 2)
        output_grid.addWidget(QLabel("像素（宽 × 高）"), 1, 3)
        output_grid.addWidget(QLabel("结果目录"), 2, 0)
        self.output_dir_edit = QLineEdit(str(self.storage_dir / "orbital_diagram_runs"))
        self.output_dir_edit.textChanged.connect(self._configuration_changed)
        output_grid.addWidget(self.output_dir_edit, 2, 1, 1, 2)
        browse_output = QPushButton("选择目录")
        browse_output.clicked.connect(self._browse_output_dir)
        output_grid.addWidget(browse_output, 2, 3)
        output_grid.addWidget(QLabel("成果保存位置"), 3, 0)
        self.output_location_combo = QComboBox()
        self.output_location_combo.addItem("集中保存到结果目录", "result_root")
        self.output_location_combo.addItem("保存到波函数文件所在目录", "input_directory")
        self.output_location_combo.currentIndexChanged.connect(self._configuration_changed)
        output_grid.addWidget(self.output_location_combo, 3, 1)
        self.keep_cubes_check = QCheckBox("完成后保留轨道 Cube")
        self.keep_cubes_check.setChecked(True)
        self.keep_cubes_check.toggled.connect(self._configuration_changed)
        output_grid.addWidget(self.keep_cubes_check, 3, 2, 1, 2)
        self.diagram_title_check = QCheckBox("显示图标题")
        self.diagram_title_check.setChecked(False)
        self.diagram_title_check.toggled.connect(self._configuration_changed)
        output_grid.addWidget(self.diagram_title_check, 4, 0)
        self.diagram_title_edit = QLineEdit("Molecular orbital energy diagram")
        self.diagram_title_edit.setEnabled(False)
        self.diagram_title_edit.textChanged.connect(self._configuration_changed)
        self.diagram_title_check.toggled.connect(self.diagram_title_edit.setEnabled)
        output_grid.addWidget(self.diagram_title_edit, 4, 1, 1, 2)
        self.energy_unit_combo = QComboBox()
        self.energy_unit_combo.addItem("能量：eV", "eV")
        self.energy_unit_combo.addItem("能量：Hartree", "Hartree")
        self.energy_unit_combo.currentIndexChanged.connect(self._configuration_changed)
        output_grid.addWidget(self.energy_unit_combo, 4, 3)
        output_grid.setColumnStretch(1, 1)
        output_grid.setColumnStretch(3, 1)
        output_layout.addLayout(output_grid)
        body_layout.addWidget(output_card)
        body_layout.addStretch(1)

        layout.addWidget(self._scroll_page(body, 1240), 1)
        footer = QFrame()
        footer.setObjectName("workflowFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 10, 12, 10)
        results_button = QPushButton("查看运行结果")
        results_button.clicked.connect(lambda: self.page_stack.setCurrentIndex(1))
        footer_layout.addWidget(results_button)
        footer_layout.addStretch(1)
        self.start_button = QPushButton("开始生成轨道能级图")
        self.start_button.setObjectName("primaryBtn")
        self.start_button.clicked.connect(self._start_run)
        footer_layout.addWidget(self.start_button)
        layout.addWidget(footer)
        return page

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 8, 8)
        body_layout.setSpacing(12)
        state_card, state_layout = self._card("运行状态")
        state_row = QHBoxLayout()
        self.run_state_label = QLabel("尚未运行")
        self.run_state_label.setObjectName("batchPresetSummary")
        self.run_state_label.setWordWrap(True)
        state_row.addWidget(self.run_state_label, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0% · 等待开始")
        self.progress.setMinimumWidth(250)
        state_row.addWidget(self.progress)
        state_layout.addLayout(state_row)
        body_layout.addWidget(state_card)

        queue_card, queue_layout = self._card(
            "任务队列与结果预览",
            "选择队列中的任务可查看已生成图片；失败项可以单独重试。",
        )
        self.queue_table = QTableWidget(0, 6)
        self.queue_table.setHorizontalHeaderLabels(
            ["任务", "文件类型", "当前进度", "状态", "耗时", "结果 / 说明"]
        )
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        queue_header = self.queue_table.horizontalHeader()
        queue_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        queue_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        queue_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        queue_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        queue_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        queue_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.queue_table.setMinimumHeight(190)
        self.queue_table.itemSelectionChanged.connect(self._sync_result_preview)
        queue_layout.addWidget(self.queue_table)

        preview_row = QHBoxLayout()
        self.result_preview = QLabel("选择完成的任务后可预览图片")
        self.result_preview.setObjectName("cardImage")
        self.result_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_preview.setMinimumSize(360, 230)
        self.result_preview.setMaximumHeight(330)
        self.result_preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_row.addWidget(self.result_preview, 3)
        self.run_log = QPlainTextEdit()
        self.run_log.setReadOnly(True)
        self.run_log.setMaximumBlockCount(3000)
        self.run_log.setPlaceholderText("关键运行进度会显示在这里；完整日志保存在结果目录。")
        self.run_log.setMinimumHeight(230)
        preview_row.addWidget(self.run_log, 4)
        queue_layout.addLayout(preview_row)
        body_layout.addWidget(queue_card)
        body_layout.addStretch(1)

        layout.addWidget(self._scroll_page(body, 690), 1)
        footer = QFrame()
        footer.setObjectName("workflowFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 10, 12, 10)
        back = QPushButton("返回流程设置")
        back.clicked.connect(lambda: self.page_stack.setCurrentIndex(0))
        footer_layout.addWidget(back)
        self.open_results_button = QPushButton("打开结果目录")
        self.open_results_button.clicked.connect(self._open_results)
        self.open_results_button.setEnabled(False)
        footer_layout.addWidget(self.open_results_button)
        footer_layout.addStretch(1)
        self.retry_button = QPushButton("重试选中任务")
        self.retry_button.clicked.connect(self._retry_selected)
        self.retry_button.setEnabled(False)
        footer_layout.addWidget(self.retry_button)
        self.cancel_button = QPushButton("停止")
        self.cancel_button.setObjectName("dangerBtn")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        footer_layout.addWidget(self.cancel_button)
        layout.addWidget(footer)
        return page

    def _select_default_style(self) -> None:
        styles = [
            style
            for style in core.get_all_bundle_styles()
            if str(style.get("surface_mode") or "signed") == "signed"
        ]
        if not styles:
            self._sync_style_card()
            return
        default_id = str(getattr(core, "DEFAULT_STYLE_ID", "") or "")
        style = next((item for item in styles if str(item.get("id") or "") == default_id), styles[0])
        self.style_snapshot = {
            "style": copy.deepcopy(style),
            "rep0_commands": list(style.get("rep0_commands") or []),
            "selection_text": f"套装风格：{style.get('name') or '未命名方案'}",
            "mode": "bundle",
            "bundle_id": str(style.get("id") or ""),
            "iso_id": str(style.get("id") or ""),
            "skeleton_id": "",
        }
        self.style_snapshot["hash"] = _style_hash(self.style_snapshot)
        self._sync_style_card()

    def _sync_style_card(self) -> None:
        style = self.style_snapshot.get("style") or {}
        if not style:
            self.style_name_label.setText("没有可用的正负相位绘图方案")
            self.style_detail_label.setText("请检查绘图方案库。")
            return
        self.style_name_label.setText(str(style.get("name") or "未命名方案"))
        self.style_detail_label.setText(
            "正负相位颜色已配置 · "
            f"材质：{style.get('material') or 'Glossy'}"
        )

    def _choose_style(self) -> None:
        if self.is_running():
            QMessageBox.information(self, "任务运行中", "请先停止当前任务，再更换绘图方案。")
            return
        result: object
        if self.style_dialog_factory is None:
            result = _SignedStyleDialog(self.style_snapshot, self)
        else:
            factory = self.style_dialog_factory
            try:
                parameters = inspect.signature(factory).parameters
            except (TypeError, ValueError):
                parameters = {}
            kwargs: dict[str, object] = {}
            if "surface_mode" in parameters:
                kwargs["surface_mode"] = "signed"
            if "parent" in parameters:
                kwargs["parent"] = self
            try:
                result = factory(self.style_snapshot, **kwargs)
            except TypeError:
                result = factory(self.style_snapshot, self)

        if isinstance(result, dict):
            selection = copy.deepcopy(result)
        else:
            dialog = result
            execute = getattr(dialog, "exec", None)
            if not callable(execute) or execute() != QDialog.DialogCode.Accepted:
                return
            selection_method = getattr(dialog, "selection", None)
            selection = selection_method() if callable(selection_method) else {}
        if not isinstance(selection, dict):
            QMessageBox.warning(self, "方案不可用", "绘图方案选择器没有返回有效设置。")
            return
        style = selection.get("style")
        if not isinstance(style, dict) or str(style.get("surface_mode") or "signed") != "signed":
            QMessageBox.warning(self, "方案不兼容", "分子轨道只能使用正负相位绘图方案。")
            return
        selection = copy.deepcopy(selection)
        selection["hash"] = _style_hash(selection)
        self.style_snapshot = selection
        self._sync_style_card()
        self._configuration_changed()

    def _browse_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "添加轨道计算文件",
            "",
            "轨道计算文件 (*.out *.log *.fch *.fchk *.molden *.molden.input);;所有文件 (*)",
        )
        if files:
            self._add_paths([Path(item) for item in files])

    def _browse_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "扫描轨道计算文件夹", "")
        if directory:
            self._add_paths([Path(directory)])

    @Slot(object)
    def _add_paths(self, paths: object) -> None:
        if self.is_input_processing():
            QMessageBox.information(self, "正在读取文件", "请等待当前文件读取完成，或先停止读取。")
            return
        if self._workflow_is_running():
            QMessageBox.information(self, "任务运行中", "请先停止当前任务，再修改输入。")
            return
        raw_paths = paths if isinstance(paths, (list, tuple)) else [paths]
        sources = [*self.input_files, *[Path(item) for item in raw_paths if item]]
        self._begin_input_processing(sources, self.manual_pairs)

    def _remove_selected_files(self) -> None:
        if self.is_running() or self.is_input_processing():
            return
        rows = sorted({index.row() for index in self.input_table.selectedIndexes()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self.input_files):
                self.input_files.pop(row)
        remaining = {os.path.normcase(str(path)) for path in self.input_files}
        self.manual_pairs = [
            pair
            for pair in self.manual_pairs
            if os.path.normcase(str(pair.output_path)) in remaining
            and os.path.normcase(str(pair.wavefunction_path)) in remaining
        ]
        if self.input_files:
            self._begin_input_processing(self.input_files, self.manual_pairs)
        else:
            self._show_empty_inputs()

    def _clear_files(self) -> None:
        if self.is_running() or self.is_input_processing():
            return
        self.input_files.clear()
        self.manual_pairs.clear()
        self._show_empty_inputs()

    def _manual_pair_selected(self) -> None:
        if self.is_input_processing():
            return
        rows = sorted({index.row() for index in self.input_table.selectedIndexes()})
        if len(rows) != 2:
            QMessageBox.information(
                self,
                "请选择两个文件",
                "请同时选择一个 out/log 和一个 fch/fchk 或 Molden 文件。",
            )
            return
        paths = [self.input_files[row] for row in rows]
        wavefunctions = [
            path
            for path in paths
            if path.name.casefold().endswith(
                (".fch", ".fchk", ".molden", ".molden.input")
            )
        ]
        outputs = [path for path in paths if path.suffix.casefold() in {".out", ".log"}]
        if len(wavefunctions) != 1 or len(outputs) != 1:
            QMessageBox.warning(
                self,
                "无法配对",
                "选择内容必须恰好包含一个计算输出和一个波函数文件。",
            )
            return
        wavefunction, output = wavefunctions[0], outputs[0]
        expected = (
            orbital_data.CalculationProgram.GAUSSIAN
            if wavefunction.name.casefold().endswith((".fch", ".fchk"))
            else orbital_data.CalculationProgram.ORCA
        )
        selected_keys = {
            os.path.normcase(str(output.resolve())),
            os.path.normcase(str(wavefunction.resolve())),
        }
        self.manual_pairs = [
            pair
            for pair in self.manual_pairs
            if not selected_keys.intersection(
                {
                    os.path.normcase(str(pair.output_path)),
                    os.path.normcase(str(pair.wavefunction_path)),
                }
            )
        ]
        self.manual_pairs.append(
            orbital_data.InputPair(
                output,
                wavefunction,
                expected,
                pairing_reason="用户指定配对",
            )
        )
        self._begin_input_processing(self.input_files, self.manual_pairs)

    @staticmethod
    def _input_kind(path: Path) -> str:
        name = path.name.casefold()
        if name.endswith((".fch", ".fchk")):
            return "Gaussian 波函数"
        if name.endswith((".molden", ".molden.input")):
            return "ORCA 波函数"
        return "输出日志"

    def _render_input_table(self) -> None:
        self.input_table.setRowCount(0)
        for path in self.input_files:
            row = self.input_table.rowCount()
            self.input_table.insertRow(row)
            self.input_table.setItem(row, 0, QTableWidgetItem(self._input_kind(path)))
            item = QTableWidgetItem(path.name)
            item.setToolTip(str(path))
            self.input_table.setItem(row, 1, item)
            self.input_table.setItem(row, 2, QTableWidgetItem(str(path.parent)))
        self.input_count_label.setText(f"{len(self.input_files)} 个文件")

    def _show_empty_inputs(self) -> None:
        self._input_generation += 1
        self._render_input_table()
        self.pairs = []
        self.datasets = []
        self.pair_validity = []
        self.unpaired_issue = ""
        self.pair_table.setRowCount(0)
        self.pair_status_label.setText("请至少添加一组配套文件。")
        self.ready_label.setText("尚未添加输入")
        self.input_progress_label.setText("等待添加文件")
        self._refresh_selection_preview()
        self._configuration_changed()

    def is_input_processing(self) -> bool:
        return self._input_busy

    def _set_input_busy(self, busy: bool, message: str = "") -> None:
        self._input_busy = bool(busy)
        for widget in (
            self.add_input_button,
            self.add_folder_button,
            self.manual_pair_button,
            self.remove_input_button,
            self.clear_input_button,
        ):
            widget.setEnabled(not busy)
        self.input_table.setAcceptDrops(not busy)
        self.cancel_input_button.setVisible(busy)
        self.cancel_input_button.setEnabled(busy)
        self.input_progress.setVisible(busy)
        if busy:
            self.input_progress.setRange(0, 0)
            self.input_progress_label.setText(message or "正在读取并核验文件……")
            self.pair_status_label.setText("正在读取并核验文件，请稍候。")
            self.ready_label.setText("正在读取")
        if hasattr(self, "start_button"):
            self.start_button.setEnabled(not busy and not self._workflow_is_running())

    def _begin_input_processing(
        self,
        source_paths: Iterable[Path],
        manual_pairs: Iterable[orbital_data.InputPair],
    ) -> None:
        if self.input_thread is not None and self.input_thread.isRunning():
            return
        self._input_generation += 1
        generation = self._input_generation
        thread = QThread(self)
        worker = OrbitalInputWorker(
            generation,
            [Path(path) for path in source_paths],
            list(manual_pairs),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_input_progress)
        worker.finished.connect(self._on_input_worker_finished)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(
            lambda target=thread: self._on_input_thread_finished(target)
        )
        thread.finished.connect(thread.deleteLater)
        self.input_thread = thread
        self.input_worker = worker
        self._set_input_busy(True)
        thread.start()

    @Slot(int, str, int, int)
    def _on_input_progress(
        self, generation: int, message: str, current: int, total: int
    ) -> None:
        if generation != self._input_generation:
            return
        self.input_progress_label.setText(message)
        if total > 0:
            self.input_progress.setRange(0, total)
            self.input_progress.setValue(max(0, min(current, total)))
        else:
            self.input_progress.setRange(0, 0)

    @Slot(int, object, object)
    def _on_input_worker_finished(
        self, generation: int, result: object, error: object
    ) -> None:
        if generation != self._input_generation:
            return
        if error is not None:
            self._set_input_busy(False)
            self.input_progress_label.setText(f"文件读取失败：{error}")
            self.pair_status_label.setText("文件读取失败，请检查文件是否完整后重试。")
            self.ready_label.setText("读取失败")
            return
        payload = result if isinstance(result, dict) else {}
        if payload.get("cancelled"):
            self._set_input_busy(False)
            self.input_progress_label.setText("已停止读取")
            self.ready_label.setText(
                f"{sum(self.pair_validity)} 组输入就绪"
                if self.pairs
                else "尚未添加输入"
            )
            return
        self._apply_input_result(payload)
        self._set_input_busy(False)

    def _on_input_thread_finished(self, target: QThread) -> None:
        if target is not self.input_thread:
            return
        self.input_thread = None
        self.input_worker = None
        if self._input_busy:
            self._set_input_busy(False)
            self.input_progress_label.setText("已停止读取")

    def _cancel_input_processing(self) -> None:
        if not self.is_input_processing():
            return
        self._input_generation += 1
        if self.input_worker is not None:
            self.input_worker.cancel()
        self.cancel_input_button.setEnabled(False)
        self.input_progress_label.setText("正在停止读取……")

    def _apply_input_result(self, payload: dict[str, Any]) -> None:
        self.input_files = list(payload.get("input_files") or [])
        self.manual_pairs = list(payload.get("manual_pairs") or [])
        self.pairs = list(payload.get("pairs") or [])
        self.datasets = list(payload.get("datasets") or [])
        self.pair_validity = [bool(value) for value in payload.get("pair_validity") or []]
        self.unpaired_issue = str(payload.get("unpaired_issue") or "")
        validation_texts = [str(value) for value in payload.get("validation_texts") or []]
        self._render_input_table()
        self.pair_table.setRowCount(0)
        if not self.input_files:
            self._show_empty_inputs()
            return
        if not self.pairs:
            detail = self.unpaired_issue or "请同时添加计算输出和对应的波函数文件。"
            self.pair_status_label.setText(f"尚未形成完整配对：{detail}")
            self.ready_label.setText("等待完整配对")
            self.input_progress_label.setText(
                f"已读取 {len(self.input_files)} 个文件，等待补充配套文件"
            )
            self._refresh_selection_preview()
            self._configuration_changed()
            return

        messages: list[str] = []
        for pair, validation_text in zip(self.pairs, validation_texts):
            row = self.pair_table.rowCount()
            self.pair_table.insertRow(row)
            self.pair_table.setItem(row, 0, QTableWidgetItem(pair.label))
            program = "Gaussian" if pair.program.value == "gaussian" else "ORCA"
            self.pair_table.setItem(row, 1, QTableWidgetItem(program))
            file_item = QTableWidgetItem(
                f"{pair.output_path.name}\n+ {pair.wavefunction_path.name}"
            )
            file_item.setToolTip(f"{pair.output_path}\n{pair.wavefunction_path}")
            self.pair_table.setItem(row, 2, file_item)
            validation_item = QTableWidgetItem(validation_text)
            validation_item.setToolTip(validation_text)
            self.pair_table.setItem(row, 3, validation_item)
            messages.append(f"{pair.label}：{validation_text}")

        valid_count = sum(self.pair_validity)
        self.pair_status_label.setText("\n".join(messages))
        if self.unpaired_issue:
            self.pair_status_label.setText(
                self.pair_status_label.text()
                + f"\n仍有文件未配对：{self.unpaired_issue}。可选择两个文件后手动配对。"
            )
        self.ready_label.setText(
            f"{valid_count}/{len(self.pairs)} 组核验通过"
            if valid_count != len(self.pairs)
            else f"{valid_count} 组输入就绪"
        )
        self.input_progress_label.setText(
            f"读取完成：{len(self.input_files)} 个文件，{len(self.pairs)} 组任务"
        )
        self._refresh_selection_preview()
        self._configuration_changed()

    # Kept as a compatibility entry point for callers from older hosts.  The
    # operation is now asynchronous just like file picker and drag-and-drop.
    def _refresh_inputs(self) -> None:
        if self.input_files:
            self._begin_input_processing(self.input_files, self.manual_pairs)
        else:
            self._show_empty_inputs()

    def _pair_and_parse_inputs(self) -> None:
        self._refresh_inputs()

    @staticmethod
    def _frontier_text(anchor: str, offset: int) -> str:
        return anchor if offset == 0 else f"{anchor}{offset:+d}"

    def _make_offset_combo(self, value: int, anchor: str) -> QComboBox:
        """Create the compact, editable offset selector used after HOMO/LUMO."""

        combo = OrbitalOffsetComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        combo.setMinimumContentsLength(4)
        combo.setMinimumWidth(76)
        combo.setMaximumWidth(112)
        combo.setObjectName(f"orbital{anchor.title()}OffsetCombo")
        combo.setAccessibleName(f"{anchor} 偏移")
        for offset in range(-5, 6):
            combo.addItem(self._offset_text(offset), offset)
        editor = combo.lineEdit()
        if editor is not None:
            editor.setValidator(QIntValidator(-50, 50, editor))
            editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
            editor.setPlaceholderText("0")
            editor.setTextMargins(0, 0, 18, 0)
            editor.editingFinished.connect(
                lambda target=combo, default=value: self._normalize_offset_combo(
                    target, default
                )
            )
        combo.setToolTip(
            f"直接输入 {anchor} 的偏移整数（-50 至 50），或从列表选常用值"
        )
        combo.setCurrentText(self._offset_text(value))
        combo.currentTextChanged.connect(self._selection_controls_changed)
        return combo

    @staticmethod
    def _offset_text(value: int) -> str:
        return "0" if value == 0 else f"{value:+d}"

    @staticmethod
    def _offset_value(combo: QComboBox, fallback: int = 0) -> int:
        text = (
            combo.currentText()
            .strip()
            .replace("−", "-")
            .replace("＋", "+")
            .replace("－", "-")
        )
        try:
            value = int(text)
        except (TypeError, ValueError):
            value = fallback
        return max(-50, min(50, value))

    def _set_offset_combo(self, combo: QComboBox, value: int) -> None:
        combo.setCurrentText(self._offset_text(max(-50, min(50, int(value)))))

    def _normalize_offset_combo(self, combo: QComboBox, fallback: int = 0) -> None:
        self._set_offset_combo(combo, self._offset_value(combo, fallback))

    def _selection_spec(self) -> dict[str, Any]:
        manual = self.manual_expression_edit.text().strip()
        spin_mode = str(self.spin_combo.currentData() or "auto")
        if manual:
            return {
                "mode": "custom",
                "text": manual,
                "spin_mode": spin_mode,
                "expression": manual,
            }
        start_offset = self._offset_value(self.start_offset_combo, -1)
        end_offset = self._offset_value(self.end_offset_combo, 3)
        start = self._frontier_text("HOMO", start_offset)
        end = self._frontier_text("LUMO", end_offset)
        return {
            "mode": "custom",
            "text": f"{start}..{end}",
            "spin_mode": spin_mode,
            "expression": f"{start}..{end}",
            # Keep the structured legacy fields so settings written by this
            # version can still be read by older builds.
            "start_anchor": "HOMO",
            "start_offset": start_offset,
            "end_anchor": "LUMO",
            "end_offset": end_offset,
        }

    def _selection_controls_changed(self, _value: object = None) -> None:
        for widget in (
            self.start_offset_combo,
            self.end_offset_combo,
        ):
            widget.setEnabled(not bool(self.manual_expression_edit.text().strip()))
        self._refresh_selection_preview()
        self._configuration_changed()

    def _refresh_selection_preview(self) -> None:
        self.orbital_table.blockSignals(True)
        self.orbital_table.setRowCount(0)
        spec = self._selection_spec()
        errors: list[str] = []
        selected_count = 0
        restored_rows = 0
        saved_by_path: dict[str, set[tuple[str, int]]] = {}
        if self._restore_selection_pending:
            for selection in self._saved_orbital_selections:
                path = os.path.normcase(str(selection.get("wavefunction_path") or ""))
                if not path:
                    continue
                saved_by_path[path] = {
                    (str(item.get("spin") or "spatial"), int(item.get("global_index") or 0))
                    for item in list(selection.get("orbitals") or [])
                    if isinstance(item, dict)
                }
        for pair_index, (pair, dataset, valid) in enumerate(
            zip(self.pairs, self.datasets, self.pair_validity)
        ):
            if dataset is None:
                continue
            try:
                refs = orbital_data.resolve_orbital_selection(
                    dataset,
                    mode=str(spec["mode"]),
                    spin_mode=str(spec["spin_mode"]),
                    text=spec.get("text"),
                )
            except orbital_data.OrbitalDataError as exc:
                errors.append(f"{pair.label}：{exc}")
                continue
            for ref in refs:
                row = self.orbital_table.rowCount()
                self.orbital_table.insertRow(row)
                file_item = QTableWidgetItem(pair.label)
                file_item.setToolTip(str(pair.wavefunction_path))
                self.orbital_table.setItem(row, 0, file_item)
                payload = ref.to_dict()
                payload.update(
                    {
                        "pair_index": pair_index,
                        "pair_label": pair.label,
                        "output_path": str(pair.output_path),
                        "wavefunction_path": str(pair.wavefunction_path),
                        "pair_valid": valid,
                    }
                )
                check = QTableWidgetItem()
                check.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                saved = saved_by_path.get(os.path.normcase(str(pair.wavefunction_path)))
                checked = (
                    saved is None
                    or (str(ref.spin.value), int(ref.global_index)) in saved
                )
                check.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
                check.setData(Qt.ItemDataRole.UserRole, payload)
                self.orbital_table.setItem(row, 1, check)
                spin_text = {
                    "spatial": "空间",
                    "alpha": "α",
                    "beta": "β",
                }.get(ref.spin.value, ref.spin.value)
                self.orbital_table.setItem(row, 2, QTableWidgetItem(spin_text))
                self.orbital_table.setItem(row, 3, QTableWidgetItem(ref.label))
                self.orbital_table.setItem(row, 4, QTableWidgetItem(f"{ref.occupation:.6g}"))
                self.orbital_table.setItem(row, 5, QTableWidgetItem(f"{ref.energy_ev:.6f}"))
                selected_count += int(checked)
                restored_rows += int(saved is not None)
        self.orbital_table.blockSignals(False)
        if restored_rows:
            self._restore_selection_pending = False
        if errors:
            self.selection_status_label.setText("\n".join(errors))
        elif selected_count:
            self.selection_status_label.setText(
                f"已选择 {selected_count} 个轨道；可以取消不需要绘制的项目。"
            )
        else:
            self.selection_status_label.setText("添加并核验文件后，这里会逐项显示实际绘制的轨道。")

    def _orbital_check_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 1:
            selected = sum(
                1
                for row in range(self.orbital_table.rowCount())
                if self.orbital_table.item(row, 1) is not None
                and self.orbital_table.item(row, 1).checkState() == Qt.CheckState.Checked
            )
            self.selection_status_label.setText(
                f"已选择 {selected} 个轨道；可以取消不需要绘制的项目。"
            )
            self._configuration_changed()

    def _selected_orbitals(self) -> list[dict[str, Any]]:
        grouped: dict[int, dict[str, Any]] = {}
        for row in range(self.orbital_table.rowCount()):
            check = self.orbital_table.item(row, 1)
            if check is None:
                continue
            if check.checkState() != Qt.CheckState.Checked:
                continue
            payload = check.data(Qt.ItemDataRole.UserRole)
            if not isinstance(payload, dict):
                continue
            pair_index = int(payload.get("pair_index") or 0)
            entry = grouped.setdefault(
                pair_index,
                {
                    "pair_index": pair_index,
                    "label": str(payload.get("pair_label") or ""),
                    "output_path": str(payload.get("output_path") or ""),
                    "wavefunction_path": str(payload.get("wavefunction_path") or ""),
                    "orbitals": [],
                },
            )
            orbital_payload = {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "pair_index",
                    "pair_label",
                    "output_path",
                    "wavefunction_path",
                    "pair_valid",
                }
            }
            entry["orbitals"].append(orbital_payload)
        return [grouped[index] for index in sorted(grouped)]

    def _settings(self) -> dict[str, Any]:
        spec = self._selection_spec()
        selections = self._selected_orbitals()
        iso = float(self.iso_spin.value())
        return {
            "selection": copy.deepcopy(spec),
            "selection_mode": str(spec.get("mode") or ""),
            "selection_expression": str(spec.get("expression") or ""),
            "selection_text": str(spec.get("text") or ""),
            "start_offset": int(
                spec.get("start_offset", self._offset_value(self.start_offset_combo, -1))
            ),
            "end_offset": int(
                spec.get("end_offset", self._offset_value(self.end_offset_combo, 3))
            ),
            "spin_mode": str(spec.get("spin_mode") or "auto"),
            "orbital_selections": copy.deepcopy(selections),
            "selected_orbitals": copy.deepcopy(selections),
            "style_snapshot": copy.deepcopy(self.style_snapshot),
            "iso_value": iso,
            "orbital_iso": iso,
            "isovalue": iso,
            "render_mode": "interactive",
            "vmd_interactive": True,
            "interactive_instruction": "打开后自由调整一切，最后点保存全部参数并确认",
            "recording_required": False,
            "renderer": "tachyon",
            "render_engine": "Tachyon",
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "diagram_width": 1800,
            "energy_unit": str(self.energy_unit_combo.currentData() or "eV"),
            "energy_decimals": 2,
            "title": self.diagram_title_edit.text().strip()
            or "Molecular orbital energy diagram",
            "show_diagram_title": self.diagram_title_check.isChecked(),
            "output_location": str(
                self.output_location_combo.currentData() or "result_root"
            ),
            "keep_cubes": self.keep_cubes_check.isChecked(),
            "strict_pair_validation": True,
        }

    def _configuration_changed(self, _value: object = None) -> None:
        if self._loading_settings:
            return
        self.settingsChanged.emit(
            {
                "orbital_diagram_output_dir": self.output_dir_edit.text().strip(),
                "orbital_output_dir": self.output_dir_edit.text().strip(),
                "orbital_diagram_settings": self._settings(),
            }
        )

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "选择轨道图结果目录",
            self.output_dir_edit.text().strip() or str(self.storage_dir),
        )
        if path:
            self.output_dir_edit.setText(path)

    def _validated_inputs(
        self,
        pair_override: list[orbital_data.InputPair] | None = None,
    ) -> tuple[list[orbital_data.InputPair], Path, Path, Path, dict[str, Any]]:
        if not self.pairs:
            raise ValueError("请先添加并成功配对输出文件与波函数文件。")
        if self.unpaired_issue:
            raise ValueError("仍有输入文件未完成配对，请手动配对或移除多余文件。")
        if not all(self.pair_validity):
            raise ValueError("存在未通过内容核验的配对，不能开始运行。")
        selected = self._selected_orbitals()
        if not selected:
            raise ValueError("请至少勾选一个要绘制的轨道。")
        selected_pair_indices = {int(item.get("pair_index") or 0) for item in selected}
        if pair_override is not None:
            override_paths = {
                os.path.normcase(str(pair.wavefunction_path)) for pair in pair_override
            }
            pairs = [
                pair
                for index, pair in enumerate(self.pairs)
                if index in selected_pair_indices
                and os.path.normcase(str(pair.wavefunction_path)) in override_paths
            ]
        else:
            pairs = [
                pair for index, pair in enumerate(self.pairs) if index in selected_pair_indices
            ]
        if not pairs:
            raise ValueError("当前任务没有勾选任何轨道。")
        output_text = self.output_dir_edit.text().strip()
        if not output_text:
            raise ValueError("请选择结果目录。")
        output_root = Path(output_text).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        multi, vmd = self._validated_program_paths()
        settings = self._settings()
        if pair_override is not None:
            allowed = {os.path.normcase(str(pair.wavefunction_path)) for pair in pair_override}
            filtered = [
                item
                for item in settings["orbital_selections"]
                if os.path.normcase(str(item.get("wavefunction_path") or "")) in allowed
            ]
            settings["orbital_selections"] = copy.deepcopy(filtered)
            settings["selected_orbitals"] = copy.deepcopy(filtered)
        return pairs, output_root, multi, vmd, settings

    def _validated_program_paths(self) -> tuple[Path, Path]:
        multi_text = str(self.multiwfn_path_getter() or "").strip().strip('"')
        vmd_text = str(self.vmd_path_getter() or "").strip().strip('"')
        if not multi_text:
            raise ValueError("请先设置 Multiwfn 程序路径。")
        if not vmd_text:
            raise ValueError("请先设置 VMD 程序路径。")
        multi = Path(multi_text).expanduser()
        vmd = Path(vmd_text).expanduser()
        if not multi.exists():
            raise ValueError(f"Multiwfn 路径不存在：{multi}")
        if not vmd.exists():
            raise ValueError(f"VMD 路径不存在：{vmd}")
        return multi, vmd

    def _populate_queue(
        self,
        pairs: list[orbital_data.InputPair],
        job_ids: list[str] | None = None,
    ) -> None:
        self.queue_table.setRowCount(0)
        for index, pair in enumerate(pairs):
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            item = QTableWidgetItem(pair.label)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setData(Qt.ItemDataRole.UserRole + 1, str(pair.wavefunction_path))
            if job_ids and index < len(job_ids):
                item.setData(Qt.ItemDataRole.UserRole + 2, str(job_ids[index]))
            item.setToolTip(str(pair.wavefunction_path))
            self.queue_table.setItem(row, 0, item)
            self.queue_table.setItem(
                row,
                1,
                QTableWidgetItem("Gaussian" if pair.program.value == "gaussian" else "ORCA"),
            )
            self.queue_table.setItem(row, 2, QTableWidgetItem("等待"))
            self.queue_table.setItem(row, 3, QTableWidgetItem("待运行"))
            self.queue_table.setItem(row, 4, QTableWidgetItem("-"))
            self.queue_table.setItem(row, 5, QTableWidgetItem("等待开始"))

    def _energy_anomaly_reports(self, settings: dict[str, Any]) -> list[str]:
        try:
            workflow = importlib.import_module("orbital_diagram_workflow")
            detector = getattr(workflow, "detect_energy_spacing_anomaly")
        except (ImportError, AttributeError):
            return []
        reports: list[str] = []
        for selection in list(settings.get("orbital_selections") or []):
            if not isinstance(selection, dict):
                continue
            orbitals = [
                item
                for item in list(selection.get("orbitals") or [])
                if isinstance(item, dict)
            ]
            anomaly = detector(orbitals)
            if not isinstance(anomaly, dict):
                continue
            labels = "、".join(str(value) for value in anomaly.get("isolated_labels") or [])
            neighbor = "、".join(str(value) for value in anomaly.get("neighbor_labels") or [])
            try:
                gap = float(anomaly.get("gap_ev") or 0.0)
                isolated_energy = float(anomaly.get("isolated_energy_ev") or 0.0)
                neighbor_energy = float(anomaly.get("neighbor_energy_ev") or 0.0)
            except (TypeError, ValueError):
                continue
            task = str(selection.get("label") or Path(str(selection.get("wavefunction_path") or "任务")).stem)
            reports.append(
                f"{task}：{labels or '某个轨道'}（{isolated_energy:.2f} eV）与相邻的"
                f"{neighbor or '轨道'}（{neighbor_energy:.2f} eV）相差 {gap:.2f} eV"
            )
        return reports

    def _confirm_energy_spacing(self, settings: dict[str, Any]) -> bool:
        reports = self._energy_anomaly_reports(settings)
        if not reports:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("轨道能量范围异常")
        box.setText("检测到一个轨道与其余所选轨道的能量差距异常大。")
        box.setInformativeText(
            "\n".join(reports[:3])
            + "\n\n这通常意味着选中了深层轨道、轨道编号不符合预期，或输入文件中的轨道顺序需要检查。"
            "继续仍可生成图片，但能级间距会被压缩显示。"
        )
        inspect_button = box.addButton("返回检查", QMessageBox.ButtonRole.RejectRole)
        continue_button = box.addButton("仍然继续", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(inspect_button)
        box.exec()
        return box.clickedButton() is continue_button

    def _start_run(self) -> None:
        self._start_worker(None)

    def _start_worker(
        self,
        pair_override: list[orbital_data.InputPair] | None,
        *,
        resume_manifest: Path | None = None,
        retry_stages: list[str] | None = None,
        job_ids: list[str] | None = None,
    ) -> None:
        if self.is_running():
            return
        try:
            if resume_manifest is not None:
                manifest = Path(resume_manifest).expanduser().resolve()
                if not manifest.is_file():
                    raise ValueError("没有找到可用于断点重试的运行记录。")
                pairs = list(pair_override or [])
                if not pairs or not job_ids:
                    raise ValueError("没有找到要重试的具体任务。")
                multi, vmd = self._validated_program_paths()
                output_root = manifest.parent
                settings = {}
            else:
                pairs, output_root, multi, vmd, settings = self._validated_inputs(pair_override)
                if not self._confirm_energy_spacing(settings):
                    return
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法开始", str(exc))
            return
        self._active_pairs = list(pairs)
        self._active_job_ids = {str(item) for item in list(job_ids or []) if str(item)}
        if resume_manifest is None:
            self._populate_queue(pairs, job_ids)
        else:
            for index, pair in enumerate(pairs):
                matching_row = next(
                    (
                        row
                        for row in range(self.queue_table.rowCount())
                        if _same_path(
                            self.queue_table.item(row, 0).data(
                                Qt.ItemDataRole.UserRole + 1
                            )
                            if self.queue_table.item(row, 0)
                            else "",
                            pair.wavefunction_path,
                        )
                    ),
                    -1,
                )
                if matching_row < 0:
                    self._populate_queue(pairs, job_ids)
                    break
                item = self.queue_table.item(matching_row, 0)
                if item is not None and job_ids and index < len(job_ids):
                    item.setData(Qt.ItemDataRole.UserRole + 2, str(job_ids[index]))
                self.queue_table.setItem(
                    matching_row, 2, QTableWidgetItem("准备断点重试")
                )
                self.queue_table.setItem(
                    matching_row, 3, QTableWidgetItem("待运行")
                )
                self.queue_table.selectRow(matching_row)
        self.run_log.clear()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0% · 正在启动")
        self._progress_ceiling = 2
        self._progress_stage_text = "正在启动"
        self._run_started_monotonic = time.monotonic()
        self._last_progress_tick = self._run_started_monotonic
        self._row_started_monotonic.clear()
        self._last_progress_message = ""
        self._runtime_timer.start()
        self.run_state_label.setText(
            "正在准备参考轨道。VMD 打开后自由调整一切，最后点“保存全部参数并确认”。"
        )
        self.cancel_button.setEnabled(True)
        self.retry_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.open_results_button.setEnabled(False)
        self.page_stack.setCurrentIndex(1)
        if self.queue_table.rowCount() and not self.queue_table.selectedItems():
            self.queue_table.selectRow(0)
        self._configuration_changed()

        self.thread = QThread(self)
        self.worker = OrbitalDiagramWorker(
            pairs,
            output_root,
            settings,
            multi,
            vmd,
            resume_manifest=resume_manifest,
            retry_stages=retry_stages,
            job_ids=job_ids,
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.event.connect(self._on_worker_event)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.finished.connect(lambda _result, _error: self.thread.quit() if self.thread else None)
        self.thread.finished.connect(self._cleanup_thread)
        self.thread.start()

    def _tick_runtime(self) -> None:
        now = time.monotonic()
        for row in range(self.queue_table.rowCount()):
            status_item = self.queue_table.item(row, 3)
            if status_item is None or status_item.text() != "进行中":
                continue
            task_item = self.queue_table.item(row, 0)
            key = str(
                task_item.data(Qt.ItemDataRole.UserRole + 1) or row
                if task_item is not None
                else row
            )
            started = self._row_started_monotonic.setdefault(
                key, self._run_started_monotonic or now
            )
            self.queue_table.setItem(
                row,
                4,
                QTableWidgetItem(f"{max(0.0, now - started):.0f} 秒"),
            )
        if self.is_running() and self.progress.value() < self._progress_ceiling:
            elapsed = now - self._last_progress_tick
            steps = int(elapsed // 4.0)
            if steps > 0:
                self.progress.setValue(
                    min(self._progress_ceiling, self.progress.value() + steps)
                )
                self.progress.setFormat(f"%p% · {self._progress_stage_text}")
                self._last_progress_tick += steps * 4.0

    def _append_log(self, text: str) -> None:
        clean = str(text).rstrip()
        if clean:
            self.run_log.appendPlainText(clean)

    def _event_row(self, event: dict[str, Any]) -> int:
        event_job_id = str(event.get("job_id") or event.get("id") or "")
        if (
            self._active_job_ids
            and event_job_id
            and event_job_id not in self._active_job_ids
        ):
            return -1
        nested_pair = event.get("pair") if isinstance(event.get("pair"), dict) else {}
        raw_path = str(
            event.get("wavefunction_path")
            or event.get("input_path")
            or event.get("file")
            or nested_pair.get("wavefunction_path")
            or ""
        )
        if raw_path:
            for row in range(self.queue_table.rowCount()):
                item = self.queue_table.item(row, 0)
                if item and _same_path(
                    item.data(Qt.ItemDataRole.UserRole + 1), raw_path
                ):
                    return row
            return -1
        if event_job_id:
            for row in range(self.queue_table.rowCount()):
                item = self.queue_table.item(row, 0)
                if item and str(
                    item.data(Qt.ItemDataRole.UserRole + 2) or ""
                ) == event_job_id:
                    return row
            return -1
        if self._active_job_ids:
            return -1
        try:
            index = int(event.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        if 1 <= index <= self.queue_table.rowCount():
            return index - 1
        if 0 <= index < self.queue_table.rowCount():
            return index
        return -1

    @staticmethod
    def _friendly_stage(stage: object) -> str:
        normalized = str(stage or "").strip().casefold()
        return {
            "pending": "等待开始",
            "parsing_inputs": "读取并核验文件",
            "resolving_orbitals": "确定绘制轨道",
            "generating_reference_cube": "准备参考轨道",
            "waiting_viewpoint": "等待确认视角",
            "generating_orbital_cubes": "准备轨道图像",
            "validating_cubes": "检查轨道数据",
            "rendering_orbitals": "渲染轨道图像",
            "composing_diagram": "排版能级图",
            "collecting": "整理结果",
            "success": "完成",
            "completed": "完成",
            "done": "完成",
        }.get(normalized, "处理中")

    @staticmethod
    def _friendly_runtime_error(error: object) -> str:
        text = str(error or "").strip().casefold()
        if any(
            marker in text
            for marker in (
                "couldn't open",
                "could not open",
                "error opening",
                "no such file",
                "file not found",
                "路径不存在",
                "文件不存在",
            )
        ):
            return "绘图程序无法打开所需文件，请确认文件仍存在且所在目录可访问。"
        if "multiwfn" in text:
            return "轨道数据未能生成，请在结果目录中查看完整日志。"
        if "vmd" in text or "tachyon" in text:
            return "轨道图像未能生成，请在结果目录中查看完整日志。"
        return "任务未能完成，请在结果目录中查看完整日志。"

    @Slot(object)
    def _on_worker_event(self, raw_event: object) -> None:
        if isinstance(raw_event, str):
            # Console lines remain in the run's complete log file.  Showing
            # them here made normal VMD/Multiwfn internals look like user
            # instructions and flooded the useful progress summary.
            return
        event = _as_dict(raw_event)
        if not event:
            return
        kind = str(event.get("kind") or event.get("type") or event.get("stage") or "")
        text = str(event.get("text") or event.get("message") or event.get("detail") or "")
        normalized_kind = kind.casefold()
        if normalized_kind in {
            "output",
            "stdout",
            "stderr",
            "console",
            "process_output",
            "raw_output",
        }:
            return
        visible_summary_kinds = {
            "run_started",
            "pair_stage",
            "progress",
            "warning",
            "error",
            "failed",
            "job_started",
            "job_finished",
            "orbital_stage",
            "viewpoint_required",
            "viewpoint_captured",
        }
        display_text = text
        if normalized_kind in {"error", "failed"}:
            display_text = self._friendly_runtime_error(text)
        if normalized_kind == "orbital_stage" and not display_text:
            orbital = event.get("orbital")
            orbital_dict = orbital if isinstance(orbital, dict) else _as_dict(orbital)
            current_orbital = event.get("current", 0)
            total_orbitals = event.get("total", 0)
            display_text = (
                f"正在渲染 {orbital_dict.get('label') or '轨道'}"
                f"（{current_orbital}/{total_orbitals}）"
            )
        if (
            display_text
            and normalized_kind in visible_summary_kinds
            and display_text != self._last_progress_message
        ):
            self._append_log(display_text)
            self._last_progress_message = display_text
        if event.get("run_dir"):
            self.last_run_dir = str(event["run_dir"])
            self.open_results_button.setEnabled(True)
        if normalized_kind == "run_started":
            total_tasks = int(event.get("total") or len(self._active_pairs) or 1)
            self.run_state_label.setText(f"流程已开始，共 {total_tasks} 个任务。")
            if not display_text:
                self._append_log(f"流程已开始，共 {total_tasks} 个任务。")
        current = event.get("current", event.get("completed"))
        total = event.get("total")
        if normalized_kind == "progress":
            try:
                if event.get("percent") is not None:
                    percent = int(round(float(event.get("percent") or 0.0)))
                else:
                    total_value = int(total or len(self._active_pairs) or 1)
                    percent = int(round(int(current or 0) / max(1, total_value) * 100.0))
                ceiling = int(round(float(event.get("ceiling_percent", percent) or percent)))
                self._progress_ceiling = max(percent, min(100, ceiling))
                stage_text = self._friendly_stage(event.get("stage"))
                self._progress_stage_text = stage_text
                self._last_progress_tick = time.monotonic()
                self.progress.setRange(0, 100)
                self.progress.setValue(max(self.progress.value(), max(0, min(100, percent))))
                self.progress.setFormat(f"%p% · {stage_text}")
            except (TypeError, ValueError):
                pass
            return
        row = self._event_row(event)
        if row >= 0:
            raw_stage = str(event.get("stage") or "")
            stage_text = self._friendly_stage(raw_stage or kind)
            if kind == "orbital_stage":
                orbital = event.get("orbital")
                orbital_dict = orbital if isinstance(orbital, dict) else _as_dict(orbital)
                orbital_label = str(orbital_dict.get("label") or "轨道")
                stage_text = f"Tachyon · {orbital_label}"
            status = str(event.get("status") or "运行中")
            status_text = {
                "pending": "等待",
                "running": "进行中",
                "success": "完成",
                "failed": "失败",
                "cancelled": "已取消",
            }.get(status.casefold(), "状态未知")
            self.queue_table.setItem(row, 2, QTableWidgetItem(stage_text))
            self.queue_table.setItem(row, 3, QTableWidgetItem(status_text))
            if status_text == "进行中":
                task_item = self.queue_table.item(row, 0)
                row_key = str(
                    task_item.data(Qt.ItemDataRole.UserRole + 1) or row
                    if task_item is not None
                    else row
                )
                self._row_started_monotonic.setdefault(
                    row_key, self._run_started_monotonic or time.monotonic()
                )
            if display_text:
                self.queue_table.setItem(row, 5, QTableWidgetItem(display_text))
            elapsed = event.get("elapsed_seconds", event.get("duration_seconds"))
            if elapsed is not None:
                try:
                    self.queue_table.setItem(row, 4, QTableWidgetItem(f"{float(elapsed):.1f} 秒"))
                except (TypeError, ValueError):
                    pass
            image_path = (
                event.get("diagram_path")
                or event.get("image_path")
                or event.get("preview_path")
            )
            if image_path:
                item = self.queue_table.item(row, 5) or QTableWidgetItem()
                item.setText(display_text or "已生成轨道预览")
                item.setData(Qt.ItemDataRole.UserRole, str(image_path))
                self.queue_table.setItem(row, 5, item)
        if normalized_kind == "pair_stage":
            stage_text = self._friendly_stage(event.get("stage"))
            self.run_state_label.setText(
                f"当前进度：{stage_text}" + (f" — {display_text}" if display_text else "")
            )
        if kind == "viewpoint_required":
            self.run_state_label.setText(
                "VMD 已打开：自由调整一切，最后点“保存全部参数并确认”。"
            )
        elif kind == "viewpoint_captured":
            self.run_state_label.setText("已保存全部 VMD 参数，正在复用同一视角批量渲染。")

    def _result_jobs(self, result: object) -> list[object]:
        for name in ("jobs", "results", "tasks", "pair_results"):
            value = _result_value(result, name, None)
            if isinstance(value, (list, tuple)):
                return list(value)
        return []

    @Slot(object, object)
    def _on_worker_finished(self, result: object, error: object) -> None:
        self.last_result = result
        self._runtime_timer.stop()
        self.cancel_button.setEnabled(False)
        self.start_button.setEnabled(True)
        jobs = self._result_jobs(result)
        if error is not None:
            friendly_error = self._friendly_runtime_error(error)
            self.progress.setFormat(f"%p% · 运行失败")
            self.run_state_label.setText(f"运行失败：{friendly_error}")
            self._append_log(f"运行失败：{friendly_error}")
            for row in range(self.queue_table.rowCount()):
                status_item = self.queue_table.item(row, 3)
                if status_item and status_item.text() in {"待运行", "进行中"}:
                    self.queue_table.setItem(row, 3, QTableWidgetItem("失败"))
                    self.queue_table.setItem(row, 5, QTableWidgetItem(friendly_error))
            self.retry_button.setEnabled(self.queue_table.rowCount() > 0)
            return

        run_dir = _result_value(result, "run_dir", "")
        if run_dir:
            self.last_run_dir = str(run_dir)
            self.open_results_button.setEnabled(True)
        failed_count = 0
        matched_count = 0
        for job in jobs:
            job_dict = _as_dict(job)
            row = self._event_row(job_dict)
            if row < 0:
                continue
            matched_count += 1
            status = str(job_dict.get("status") or "success")
            success = status.casefold() in {"success", "completed", "done", "ok"}
            if not success:
                failed_count += 1
            self.queue_table.setItem(
                row,
                2,
                QTableWidgetItem(
                    "完成" if success else self._friendly_stage(job_dict.get("stage"))
                ),
            )
            self.queue_table.setItem(row, 3, QTableWidgetItem("完成" if success else "失败"))
            duration = job_dict.get("duration_seconds", job_dict.get("elapsed_seconds", 0))
            try:
                duration_text = f"{float(duration or 0):.1f} 秒"
            except (TypeError, ValueError):
                duration_text = "-"
            self.queue_table.setItem(row, 4, QTableWidgetItem(duration_text))
            image_path = str(
                job_dict.get("diagram_path")
                or job_dict.get("image_path")
                or job_dict.get("preview_path")
                or ""
            )
            message = (
                self._friendly_runtime_error(job_dict.get("error"))
                if job_dict.get("error")
                else "能级图已生成" if image_path else "完成"
            )
            result_item = QTableWidgetItem(message)
            if image_path:
                result_item.setData(Qt.ItemDataRole.UserRole, image_path)
            self.queue_table.setItem(row, 5, result_item)
        if not jobs:
            for row in range(self.queue_table.rowCount()):
                self.queue_table.setItem(row, 2, QTableWidgetItem("完成"))
                self.queue_table.setItem(row, 3, QTableWidgetItem("完成"))
        elif not matched_count:
            self.run_state_label.setText("运行记录已更新，但没有找到与当前队列匹配的任务。")
            self.retry_button.setEnabled(False)
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.progress.setFormat("100% · 已完成")
        remaining_failures = sum(
            1
            for row in range(self.queue_table.rowCount())
            if self.queue_table.item(row, 3) is not None
            and self.queue_table.item(row, 3).text() in {"失败", "已取消"}
        )
        if self._active_job_ids and remaining_failures:
            self.run_state_label.setText(
                f"所选任务已完成重试；队列中仍有 {remaining_failures} 个失败任务。"
            )
        else:
            self.run_state_label.setText(
                f"运行完成，{failed_count} 个任务失败。" if failed_count else "运行完成。"
            )
        if self.queue_table.rowCount() and not self.queue_table.selectedItems():
            self.queue_table.selectRow(0)
        self._sync_result_preview()

    def _sync_result_preview(self) -> None:
        rows = self.queue_table.selectionModel().selectedRows()
        if not rows:
            self.retry_button.setEnabled(False if not self.is_running() else False)
            return
        row = rows[0].row()
        status = self.queue_table.item(row, 3)
        self.retry_button.setEnabled(
            not self.is_running() and status is not None and status.text() in {"失败", "已取消"}
        )
        result_item = self.queue_table.item(row, 5)
        image_path = str(result_item.data(Qt.ItemDataRole.UserRole) or "") if result_item else ""
        if not image_path or not Path(image_path).is_file():
            self.result_preview.setPixmap(QPixmap())
            self.result_preview.setText("此任务暂无可预览图片")
            return
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            self.result_preview.setPixmap(QPixmap())
            self.result_preview.setText("图片无法预览")
            return
        self.result_preview.setText("")
        self.result_preview.setPixmap(
            pixmap.scaled(
                max(1, self.result_preview.width() - 12),
                max(1, self.result_preview.height() - 12),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _retry_selected(self) -> None:
        if self.is_running():
            return
        rows = self.queue_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "请选择任务", "请先在队列中选择一个失败任务。")
            return
        row = rows[0].row()
        item = self.queue_table.item(row, 0)
        path = str(item.data(Qt.ItemDataRole.UserRole + 1) or "") if item else ""
        pair = next(
            (
                candidate
                for candidate in self.pairs
                if os.path.normcase(str(candidate.wavefunction_path)) == os.path.normcase(path)
            ),
            None,
        )
        if pair is None:
            QMessageBox.warning(self, "无法重试", "没有找到该任务对应的输入配对。")
            return
        result_job = next(
            (
                _as_dict(item)
                for item in self._result_jobs(self.last_result)
                if os.path.normcase(
                    str((_as_dict(item).get("pair") or {}).get("wavefunction_path") or "")
                )
                == os.path.normcase(str(pair.wavefunction_path))
            ),
            {},
        )
        manifest = Path(str(_result_value(self.last_result, "manifest", "") or ""))
        stages = [
            str(item)
            for item in list(result_job.get("can_retry") or [])
            if str(item)
        ]
        job_id = str(result_job.get("id") or "")
        if not manifest.is_file() or not stages or not job_id:
            QMessageBox.warning(
                self,
                "无法断点重试",
                "该任务没有完整的运行记录。请返回流程设置后重新运行。",
            )
            return
        self._start_worker(
            [pair],
            resume_manifest=manifest,
            retry_stages=stages,
            job_ids=[job_id],
        )

    def _open_results(self) -> None:
        path = Path(self.last_run_dir) if self.last_run_dir else Path(self.output_dir_edit.text().strip())
        if not path.exists():
            QMessageBox.information(self, "结果目录不可用", "尚未生成可打开的结果目录。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _workflow_is_running(self) -> bool:
        return self.thread is not None and self.thread.isRunning()

    def is_running(self) -> bool:
        return bool(
            self._workflow_is_running()
            or (self.input_thread is not None and self.input_thread.isRunning())
        )

    def cancel(self) -> None:
        if self.is_input_processing():
            self._cancel_input_processing()
        if self._workflow_is_running():
            self.cancel_button.setEnabled(False)
            self.run_state_label.setText("正在停止当前绘图任务……")
            self._append_log("已请求停止当前任务。")
            if self.worker is not None:
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
        input_thread = self.input_thread
        if input_thread is not None and input_thread.isRunning():
            # The worker checks cancellation between files.  Waiting here is
            # only used during actual widget/application teardown; ordinary
            # interaction remains non-blocking.
            input_thread.wait()
        self.input_worker = None
        self.input_thread = None

    def load_settings(self, config: dict[str, Any]) -> None:
        self._loading_settings = True
        try:
            output = str(
                config.get("orbital_diagram_output_dir")
                or config.get("orbital_output_dir")
                or (self.storage_dir / "orbital_diagram_runs")
            )
            self.output_dir_edit.setText(output)
            saved = config.get("orbital_diagram_settings")
            if not isinstance(saved, dict):
                return
            selections = saved.get("orbital_selections", saved.get("selected_orbitals", []))
            if isinstance(selections, list):
                self._saved_orbital_selections = [
                    copy.deepcopy(item) for item in selections if isinstance(item, dict)
                ]
                self._restore_selection_pending = bool(self._saved_orbital_selections)
            snapshot = saved.get("style_snapshot")
            if (
                isinstance(snapshot, dict)
                and isinstance(snapshot.get("style"), dict)
                and str(snapshot["style"].get("surface_mode") or "signed") == "signed"
            ):
                self.style_snapshot = copy.deepcopy(snapshot)
                self.style_snapshot["hash"] = _style_hash(self.style_snapshot)
                self._sync_style_card()
            iso = saved.get("iso_value", saved.get("orbital_iso", 0.05))
            self.iso_spin.setValue(max(self.iso_spin.minimum(), min(self.iso_spin.maximum(), float(iso))))
            self.width_spin.setValue(max(320, min(7680, int(saved.get("width") or 1600))))
            self.height_spin.setValue(max(240, min(4320, int(saved.get("height") or 1200))))
            output_index = self.output_location_combo.findData(
                str(saved.get("output_location") or "result_root")
            )
            self.output_location_combo.setCurrentIndex(max(0, output_index))
            self.keep_cubes_check.setChecked(bool(saved.get("keep_cubes", True)))
            self.diagram_title_edit.setText(
                str(saved.get("title") or "Molecular orbital energy diagram")
            )
            self.diagram_title_check.setChecked(
                bool(saved.get("show_diagram_title", False))
            )
            energy_index = self.energy_unit_combo.findData(
                str(saved.get("energy_unit") or "eV")
            )
            self.energy_unit_combo.setCurrentIndex(max(0, energy_index))
            selection = saved.get("selection") if isinstance(saved.get("selection"), dict) else {}
            mode = str(selection.get("mode") or saved.get("selection_mode") or "")
            expression = str(
                selection.get("expression")
                or saved.get("selection_expression")
                or ""
            )
            legacy_expressions = {
                "homo": "HOMO",
                "lumo": "LUMO",
                "homo_lumo": "HOMO,LUMO",
                "homo_minus_1_to_lumo_plus_3": "HOMO-1..LUMO+3",
            }
            if not expression:
                expression = legacy_expressions.get(mode, "")
            self.manual_expression_edit.clear()
            range_match = re.fullmatch(
                r"\s*HOMO(?:([+-]\d+))?\s*\.\.\s*LUMO(?:([+-]\d+))?\s*",
                expression,
                flags=re.IGNORECASE,
            )
            start_anchor = str(selection.get("start_anchor") or "HOMO").upper()
            end_anchor = str(selection.get("end_anchor") or "LUMO").upper()
            has_structured_range = any(
                key in selection
                for key in ("start_anchor", "start_offset", "end_anchor", "end_offset")
            )
            has_top_level_offsets = "start_offset" in saved or "end_offset" in saved
            fixed_frontier_range = (
                mode in {"frontier_range", "homo_minus_1_to_lumo_plus_3"}
                or (
                    mode in {"", "custom", "text", "manual"}
                    and start_anchor == "HOMO"
                    and end_anchor == "LUMO"
                    and (has_structured_range or range_match is not None)
                )
                or (
                    not mode
                    and has_top_level_offsets
                    and (not expression or range_match is not None)
                )
            )
            if fixed_frontier_range:
                matched_start = (
                    int(range_match.group(1) or 0)
                    if range_match
                    else -1
                )
                matched_end = (
                    int(range_match.group(2) or 0)
                    if range_match
                    else 3
                )
                self._set_offset_combo(
                    self.start_offset_combo,
                    int(
                        selection.get(
                            "start_offset", saved.get("start_offset", matched_start)
                        )
                    ),
                )
                self._set_offset_combo(
                    self.end_offset_combo,
                    int(
                        selection.get(
                            "end_offset", saved.get("end_offset", matched_end)
                        )
                    ),
                )
            else:
                manual_expression = expression
                if not manual_expression and has_structured_range:
                    start_offset = int(selection.get("start_offset", 0))
                    end_offset = int(selection.get("end_offset", 0))
                    manual_expression = (
                        f"{self._frontier_text(start_anchor, start_offset)}.."
                        f"{self._frontier_text(end_anchor, end_offset)}"
                    )
                if manual_expression:
                    self.manual_expression_edit.setText(manual_expression)
            spin_mode = str(selection.get("spin_mode") or saved.get("spin_mode") or "auto")
            spin_index = self.spin_combo.findData(spin_mode)
            self.spin_combo.setCurrentIndex(max(0, spin_index))
        except (TypeError, ValueError):
            pass
        finally:
            self._loading_settings = False
        self._refresh_selection_preview()


__all__ = ["OrbitalDiagramPage"]
