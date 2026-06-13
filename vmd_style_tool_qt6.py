from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import vmd_style_tool as core
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
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
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

CARD_W = 256
CARD_IMG_W = 232
CARD_IMG_H = 144
CARD_GAP = 12


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

    src_custom = bundle_root / "vmd_custom_styles.json"
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


class StyleCard(QFrame):
    clicked = Signal(str)

    def __init__(self, style: dict, subtitle: str) -> None:
        super().__init__()
        self.style_id = str(style.get("id", ""))
        self.setObjectName("styleCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("selected", False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(CARD_W)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(7)

        img = QLabel()
        img.setObjectName("cardImage")
        img.setFixedSize(CARD_IMG_W, CARD_IMG_H)
        img.setAlignment(Qt.AlignCenter)
        self._set_pixmap(img, str(style.get("image", "")))
        lay.addWidget(img, alignment=Qt.AlignHCenter)

        name = str(style.get("name", self.style_id))
        if style.get("is_custom"):
            name = "[自定义] " + name
        t = QLabel(name)
        t.setObjectName("cardTitle")
        t.setWordWrap(True)
        t.setAlignment(Qt.AlignHCenter)
        lay.addWidget(t)

        s = QLabel(subtitle)
        s.setObjectName("cardSubtitle")
        s.setWordWrap(True)
        s.setAlignment(Qt.AlignHCenter)
        lay.addWidget(s)

    def _set_pixmap(self, label: QLabel, image_name: str) -> None:
        p = core.STYLE_DIR / image_name
        pix = QPixmap(str(p))
        if pix.isNull():
            blank = QPixmap(CARD_IMG_W, CARD_IMG_H)
            blank.fill(Qt.lightGray)
            label.setPixmap(blank)
            return
        scaled = pix.scaled(
            CARD_IMG_W, CARD_IMG_H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        x = max(0, (scaled.width() - CARD_IMG_W) // 2)
        y = max(0, (scaled.height() - CARD_IMG_H) // 2)
        label.setPixmap(scaled.copy(x, y, CARD_IMG_W, CARD_IMG_H))

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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._body = QWidget()
        self._grid = QGridLayout(self._body)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setHorizontalSpacing(CARD_GAP)
        self._grid.setVerticalSpacing(CARD_GAP)
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
            return ""

        for st in styles:
            sid = str(st.get("id", ""))
            if not sid:
                continue
            sub = subtitle_fn(st) if callable(subtitle_fn) else str(st.get("notes", ""))
            card = StyleCard(st, sub)
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
                else:
                    pass

    def _cols(self) -> int:
        w = max(260, self.viewport().width() - 8)
        return max(1, w // (CARD_W + CARD_GAP))

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
        self.setWindowTitle("VMD + Multiwfn 风格脚本生成器")
        self.resize(1500, 920)
        self.setMinimumSize(1260, 780)

        self.bundle_styles: list[dict] = []
        self.skeleton_styles: list[dict] = []
        self.bundle_map: dict[str, dict] = {}
        self.skeleton_map: dict[str, dict] = {}
        self.selected_bundle_id = ""
        self.selected_iso_id = ""
        self.selected_skeleton_id = ""
        self.mode = "bundle"

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

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)

        main_split = QSplitter(Qt.Horizontal)
        main_split.setChildrenCollapsible(False)
        main_split.setHandleWidth(8)
        outer.addWidget(main_split, 1)

        left_panel = QFrame()
        left_panel.setObjectName("leftPanel")
        left_panel.setMinimumWidth(360)
        left_panel.setMaximumWidth(430)
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
        main_split.addWidget(left_panel)

        right_panel = QFrame()
        right_panel.setObjectName("rightPanel")
        right_col = QVBoxLayout(right_panel)
        right_col.setContentsMargins(12, 12, 12, 12)
        right_col.setSpacing(10)
        main_split.addWidget(right_panel)
        main_split.setStretchFactor(1, 1)
        main_split.setSizes([395, 1060])

        log_sec, log_l = self._section("日志")
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        log_l.addWidget(self.log_view)
        log_sec.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        left_col.addWidget(log_sec)

        mode_sec, mode_l = self._section("模式")
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
        left_col.addWidget(mode_sec)

        path_sec, path_l = self._section("程序路径")
        path_grid = QGridLayout()
        path_grid.setHorizontalSpacing(6)
        path_grid.setVerticalSpacing(6)

        path_grid.addWidget(QLabel("Multiwfn.exe"), 0, 0)
        self.multi_edit = QLineEdit()
        self.multi_edit.setPlaceholderText(r"E:\...\Multiwfn.exe")
        path_grid.addWidget(self.multi_edit, 0, 1)
        btn_multi = QPushButton("浏览")
        btn_multi.clicked.connect(self._pick_multi)
        path_grid.addWidget(btn_multi, 0, 2)

        path_grid.addWidget(QLabel("vmd.exe"), 1, 0)
        self.vmd_edit = QLineEdit()
        self.vmd_edit.setPlaceholderText(r"E:\...\vmd.exe")
        path_grid.addWidget(self.vmd_edit, 1, 1)
        btn_vmd = QPushButton("浏览")
        btn_vmd.clicked.connect(self._pick_vmd)
        path_grid.addWidget(btn_vmd, 1, 2)
        path_l.addLayout(path_grid)
        left_col.addWidget(path_sec)

        out_sec, out_l = self._section("输出脚本")
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("风格名.cmd")
        out_l.addWidget(self.out_edit)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        btn_scan = QPushButton("扫描路径")
        btn_scan.clicked.connect(self._scan_paths)
        btn_auto = QPushButton("按风格命名")
        btn_auto.clicked.connect(self._sync_output_name)
        btn_gen = QPushButton("生成脚本")
        btn_gen.setObjectName("generateBtn")
        btn_gen.clicked.connect(self._generate_script)
        action_row.addWidget(btn_scan)
        action_row.addWidget(btn_auto)
        action_row.addWidget(btn_gen)
        out_l.addLayout(action_row)
        left_col.addWidget(out_sec)

        custom_sec, custom_l = self._section("自定义导入（VMD Save State）")
        cgrid = QGridLayout()
        cgrid.setHorizontalSpacing(6)
        cgrid.setVerticalSpacing(6)

        cgrid.addWidget(QLabel("名称"), 0, 0)
        self.custom_name_edit = QLineEdit()
        self.custom_name_edit.setMinimumHeight(30)
        cgrid.addWidget(self.custom_name_edit, 0, 1, 1, 2)

        cgrid.addWidget(QLabel("简介"), 1, 0)
        self.custom_desc_edit = QLineEdit()
        self.custom_desc_edit.setMinimumHeight(30)
        cgrid.addWidget(self.custom_desc_edit, 1, 1, 1, 2)

        cgrid.addWidget(QLabel("State 文件"), 2, 0)
        self.state_file_edit = QLineEdit()
        self.state_file_edit.setMinimumHeight(30)
        cgrid.addWidget(self.state_file_edit, 2, 1)
        btn_state = QPushButton("选择")
        btn_state.setMinimumHeight(30)
        btn_state.clicked.connect(self._pick_state_file)
        cgrid.addWidget(btn_state, 2, 2)

        cgrid.addWidget(QLabel("封面图"), 3, 0)
        self.cover_file_edit = QLineEdit()
        self.cover_file_edit.setMinimumHeight(30)
        cgrid.addWidget(self.cover_file_edit, 3, 1)
        btn_cover = QPushButton("选择")
        btn_cover.setMinimumHeight(30)
        btn_cover.clicked.connect(self._pick_cover_file)
        cgrid.addWidget(btn_cover, 3, 2)
        custom_l.addLayout(cgrid)

        btn_import = QPushButton("导入自定义风格")
        btn_import.setMinimumHeight(36)
        btn_import.clicked.connect(self._import_custom_style)
        custom_l.addWidget(btn_import)
        custom_sec.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        left_col.addWidget(custom_sec)
        left_col.addStretch(1)

        head_row = QHBoxLayout()
        title = QLabel("风格选择")
        title.setObjectName("sectionTitle")
        head_row.addWidget(title)
        head_row.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("countPill")
        head_row.addWidget(self.count_label)
        right_col.addLayout(head_row)

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
        split_inner.setHandleWidth(8)

        sk_wrap = QFrame()
        sk_wrap.setObjectName("stylePane")
        sk_l = QVBoxLayout(sk_wrap)
        sk_l.setContentsMargins(10, 10, 10, 10)
        sk_l.setSpacing(8)
        sk_title = QLabel("骨架样式")
        sk_title.setObjectName("paneTitle")
        sk_l.addWidget(sk_title)
        self.skeleton_grid = CardGrid()
        self.skeleton_grid.stylePicked.connect(self._on_skeleton_picked)
        sk_l.addWidget(self.skeleton_grid, 1)
        split_inner.addWidget(sk_wrap)

        iso_wrap = QFrame()
        iso_wrap.setObjectName("stylePane")
        iso_l = QVBoxLayout(iso_wrap)
        iso_l.setContentsMargins(10, 10, 10, 10)
        iso_l.setSpacing(8)
        iso_title = QLabel("等值面样式")
        iso_title.setObjectName("paneTitle")
        iso_l.addWidget(iso_title)
        self.iso_grid = CardGrid()
        self.iso_grid.stylePicked.connect(self._on_iso_picked)
        iso_l.addWidget(self.iso_grid, 1)
        split_inner.addWidget(iso_wrap)

        split_inner.setSizes([530, 530])
        split_l.addWidget(split_inner, 1)
        self.stack.addWidget(split_page)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("detailLabel")
        self.detail_label.setWordWrap(True)
        right_col.addWidget(self.detail_label)

        self._apply_styles()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f5f7fb;
                color: #1f2c3a;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 13px;
            }
            QFrame#heroCard {
                background: #ffffff;
                border: 1px solid #d8e2ed;
                border-radius: 14px;
            }
            QLabel#appTitle { font-size: 22px; font-weight: 700; color: #102a43; }
            QLabel#appSubTitle { font-size: 13px; color: #5d7489; }
            QLabel#countPill {
                background: #eaf2ff;
                color: #214f8b;
                border: 1px solid #c9dcf6;
                border-radius: 999px;
                padding: 6px 12px;
                font-weight: 600;
            }

            QFrame#leftPanel, QFrame#rightPanel {
                background: #ffffff;
                border: 1px solid #d8e2ed;
                border-radius: 14px;
            }
            QFrame#sectionCard {
                background: #fbfdff;
                border: 1px solid #dbe5f0;
                border-radius: 12px;
            }
            QLabel#sectionCaption {
                font-size: 13px;
                font-weight: 700;
                color: #2a445b;
                padding-bottom: 2px;
            }

            QLineEdit {
                border: 1px solid #cfd9e4;
                border-radius: 8px;
                padding: 6px 8px;
                min-height: 30px;
                background: #ffffff;
                color: #17334b;
                selection-background-color: #2d76dc;
            }
            QLineEdit:focus {
                border: 1px solid #5ea2ec;
                background: #fcfeff;
            }

            QPushButton {
                border: 1px solid #c5d5e6;
                border-radius: 8px;
                padding: 7px 10px;
                background: #f7fbff;
                color: #21405a;
            }
            QPushButton:hover {
                background: #edf5ff;
                border: 1px solid #aec8e2;
            }
            QPushButton#modeButton {
                min-height: 34px;
                font-size: 16px;
                font-weight: 600;
            }
            QPushButton#modeButton:checked {
                border: 2px solid #1f6feb;
                background: #e8f0ff;
                color: #0f3766;
            }
            QPushButton#generateBtn {
                border: 1px solid #11956b;
                background: #14a579;
                color: #ffffff;
                font-weight: 700;
            }
            QPushButton#generateBtn:hover { background: #10966d; }

            QScrollArea {
                border: 1px solid #dbe5f0;
                border-radius: 12px;
                background: #f7fafe;
            }
            QScrollArea#leftScroll {
                border: none;
                border-radius: 0px;
                background: transparent;
            }
            QFrame#stylePane {
                background: #f9fcff;
                border: 1px solid #dbe5f0;
                border-radius: 12px;
            }
            QLabel#paneTitle { font-size: 14px; font-weight: 700; color: #25445e; }

            QFrame#styleCard {
                background: #ffffff;
                border: 1px solid #d7e1ec;
                border-radius: 14px;
            }
            QFrame#styleCard:hover {
                border: 1px solid #8db9e6;
                background: #fdfefe;
            }
            QFrame#styleCard[selected="true"] {
                border: 3px solid #1f6feb;
                background: #eef5ff;
            }
            QLabel#cardImage {
                border: 1px solid #d8e3ef;
                border-radius: 10px;
                background: #e8eff8;
            }
            QLabel#cardTitle { font-size: 15px; font-weight: 700; color: #173854; }
            QLabel#cardSubtitle { font-size: 12px; color: #557086; }
            QLabel#emptyLabel { color: #5f788f; font-size: 13px; padding: 20px; }

            QLabel#sectionTitle { font-size: 18px; font-weight: 700; color: #173854; }
            QLabel#detailLabel {
                background: #f2f8ff;
                border: 1px solid #d4e2f1;
                border-radius: 10px;
                padding: 9px 12px;
                color: #3a566f;
            }
            QPlainTextEdit#logView {
                border: 1px solid #20354a;
                border-radius: 10px;
                background: #0f253a;
                color: #d5e5f8;
            }
            """
        )

    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _set_mode(self, mode: str) -> None:
        self.mode = "split" if mode == "split" else "bundle"
        self.mode_bundle_btn.setChecked(self.mode == "bundle")
        self.mode_split_btn.setChecked(self.mode == "split")
        self.stack.setCurrentIndex(1 if self.mode == "split" else 0)
        self._sync_output_name()
        self._update_detail()

    def _subtitle_iso(self, st: dict) -> str:
        pos = st.get("pos_color_expr", f"ColorID {st.get('pos_color', 1)}")
        neg = st.get("neg_color_expr", f"ColorID {st.get('neg_color', 0)}")
        return f"{st.get('material', 'Glossy')} | {pos}/{neg}"

    def _subtitle_skeleton(self, st: dict) -> str:
        return st.get("notes", "Skeleton style preset")

    def _refresh_lists(self) -> None:
        self.selected_bundle_id = self.bundle_grid.load_styles(
            self.bundle_styles, self.selected_bundle_id, self._subtitle_iso
        )
        self.selected_iso_id = self.iso_grid.load_styles(
            self.bundle_styles, self.selected_iso_id, self._subtitle_iso
        )
        self.selected_skeleton_id = self.skeleton_grid.load_styles(
            self.skeleton_styles, self.selected_skeleton_id, self._subtitle_skeleton
        )
        self.count_label.setText(f"套装 {len(self.bundle_styles)} · 骨架 {len(self.skeleton_styles)}")
        self._sync_output_name()
        self._update_detail()

    def _refresh_styles(self) -> None:
        self.bundle_styles = core.get_all_bundle_styles()
        self.skeleton_styles = list(core.SKELETON_STYLES)
        self.bundle_map = {s["id"]: s for s in self.bundle_styles}
        self.skeleton_map = {s["id"]: s for s in self.skeleton_styles}

        if self.selected_bundle_id not in self.bundle_map and self.bundle_styles:
            self.selected_bundle_id = self.bundle_styles[0]["id"]
        if self.selected_iso_id not in self.bundle_map and self.bundle_styles:
            self.selected_iso_id = self.bundle_styles[0]["id"]
        if self.selected_skeleton_id not in self.skeleton_map and self.skeleton_styles:
            self.selected_skeleton_id = self.skeleton_styles[0]["id"]
        self._refresh_lists()

    def _load_initial(self) -> None:
        conf = core.load_config()
        self.multi_edit.setText(conf.get("multiwfn_exe", ""))
        self.vmd_edit.setText(conf.get("vmd_exe", ""))
        self.selected_bundle_id = conf.get("last_style", "")
        self.selected_iso_id = conf.get("last_iso_style", "")
        self.selected_skeleton_id = conf.get("last_skeleton", "")
        self._refresh_styles()
        self._set_mode(conf.get("mode", "bundle"))
        self._log("软件已启动。")

    def _style_name_by_id(self, style_id: str, default: str = "Style") -> str:
        style = self.bundle_map.get(style_id)
        return style["name"] if style else default

    def _skeleton_name_by_id(self, skeleton_id: str, default: str = "Skeleton") -> str:
        style = self.skeleton_map.get(skeleton_id)
        return style["name"] if style else default

    def _auto_output_name(self) -> str:
        if self.mode == "split":
            iso_name = clean_name_for_file(self._style_name_by_id(self.selected_iso_id, "Iso"))
            return f"{iso_name}.cmd"
        bundle_name = clean_name_for_file(self._style_name_by_id(self.selected_bundle_id, "Style"))
        return f"{bundle_name}.cmd"

    def _sync_output_name(self) -> None:
        self.out_edit.setText(self._auto_output_name())

    def _update_detail(self) -> None:
        if self.mode == "split":
            sk = self._skeleton_name_by_id(self.selected_skeleton_id, "Skeleton")
            iso = self._style_name_by_id(self.selected_iso_id, "Iso")
            self.detail_label.setText(f"拆分模式：骨架 = {sk} | 等值面 = {iso}")
        else:
            bundle = self._style_name_by_id(self.selected_bundle_id, "Style")
            self.detail_label.setText(f"套装模式：{bundle}")

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

    def _scan_paths(self) -> None:
        found = core.find_path_candidates()
        if not self.multi_edit.text().strip() and found["multiwfn"]:
            self.multi_edit.setText(found["multiwfn"][0])
        if not self.vmd_edit.text().strip() and found["vmd"]:
            self.vmd_edit.setText(found["vmd"][0])
        self._log(
            f"扫描完成：Multiwfn 候选 {len(found['multiwfn'])} 个，VMD 候选 {len(found['vmd'])} 个。"
        )

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
                (core.STYLE_DIR / image_file).write_bytes(cover_path.read_bytes())
                style["image"] = image_file

        custom_styles = core.load_custom_styles()
        custom_styles = [x for x in custom_styles if x.get("id") != style["id"]]
        custom_styles.append(style)
        core.save_custom_styles(custom_styles)

        self.selected_bundle_id = style["id"]
        self.selected_iso_id = style["id"]
        self._refresh_styles()
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

        rep0_commands = None
        selected_bundle = self.selected_bundle_id
        selected_iso = self.selected_bundle_id
        selected_skeleton = self.selected_skeleton_id

        if self.mode == "split":
            skeleton = self.skeleton_map.get(self.selected_skeleton_id)
            iso_style = self.bundle_map.get(self.selected_iso_id)
            if skeleton is None or iso_style is None:
                QMessageBox.critical(self, "生成失败", "拆分模式需要同时选择骨架和等值面样式。")
                return
            style = core.compose_combo_style(skeleton, iso_style)
            rep0_commands = skeleton.get("rep0_commands", [])
            selected_bundle = self.selected_iso_id
            selected_iso = self.selected_iso_id
            selected_skeleton = self.selected_skeleton_id
            default_id = f"{selected_skeleton}_{selected_iso}"
        else:
            style = self.bundle_map.get(self.selected_bundle_id)
            if style is None:
                QMessageBox.critical(self, "生成失败", "套装模式下请选择一个样式。")
                return
            rep0_commands = style.get("rep0_commands") or None
            selected_bundle = self.selected_bundle_id
            selected_iso = self.selected_bundle_id
            default_id = self.selected_bundle_id

        safe_out = core._sanitize_output_name(out_name, default_id)
        out_path = (core.ROOT / safe_out).resolve()
        script = core.build_cmd_script(style, multi, vmd, rep0_commands=rep0_commands)
        out_path.write_text(script, encoding="utf-8")

        core.save_config(
            {
                "multiwfn_exe": multi,
                "vmd_exe": vmd,
                "output_name": safe_out,
                "mode": self.mode,
                "last_style": selected_bundle,
                "last_skeleton": selected_skeleton,
                "last_iso_style": selected_iso,
            }
        )
        self.out_edit.setText(safe_out)
        self._log(f"已生成脚本：{out_path}")
        QMessageBox.information(self, "生成成功", f"脚本已生成：\n{out_path}")


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
