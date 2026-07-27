from __future__ import annotations

from typing import Any

import vmd_style_tool as core
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


MATERIAL_LABELS = {
    "ambient": "环境光 Ambient",
    "diffuse": "漫反射 Diffuse",
    "specular": "高光 Specular",
    "shininess": "高光锐度 Shininess",
    "mirror": "镜面反射 Mirror",
    "opacity": "不透明度 Opacity",
    "outline": "轮廓强度 Outline",
    "outlinewidth": "轮廓宽度 Outline Width",
}


def _qcolor(rgb: tuple[float, float, float]) -> QColor:
    return QColor.fromRgbF(*(max(0.0, min(1.0, float(value))) for value in rgb))


def _rgb_from_qcolor(color: QColor) -> tuple[float, float, float]:
    return color.redF(), color.greenF(), color.blueF()


class ColorButton(QPushButton):
    def __init__(self, rgb: tuple[float, float, float]) -> None:
        super().__init__()
        self.rgb = rgb
        self.setFixedSize(64, 34)
        self._sync_style()

    def set_rgb(self, rgb: tuple[float, float, float]) -> None:
        self.rgb = tuple(max(0.0, min(1.0, float(value))) for value in rgb)  # type: ignore[assignment]
        self._sync_style()

    def _sync_style(self) -> None:
        color = _qcolor(self.rgb)
        text_color = "#0B172A" if color.lightnessF() > 0.62 else "#FFFFFF"
        self.setText("选择")
        self.setStyleSheet(
            "QPushButton {"
            f"background: {color.name()}; color: {text_color};"
            "border: 1px solid rgba(15, 46, 82, 0.22); border-radius: 8px; font-weight: 700;"
            "}"
        )


class StyleParameterDialog(QDialog):
    def __init__(
        self,
        style: dict,
        rep0_commands: list[str] | None,
        selection_text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.base_style = dict(style)
        self.rep0_commands = list(rep0_commands or style.get("rep0_commands") or [])
        self.parameters = core.extract_style_visual_parameters(style, self.rep0_commands)
        self.saved_style: dict | None = None
        self.editing = False
        self.edit_widgets: list[QWidget] = []
        self.color_rows: dict[str, dict[str, Any]] = {}
        self.material_rows: dict[str, tuple[QCheckBox, QDoubleSpinBox]] = {}
        self.light_combos: dict[str, QComboBox] = {}
        self.state_combos: dict[str, QComboBox] = {}

        self.setWindowTitle("风格详细参数")
        self.setModal(True)
        self.resize(860, 720)
        self.setMinimumSize(700, 560)
        self._build_ui(selection_text)
        self._set_editing(False)

    def _card(self, title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("parameterCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("paneTitle")
        layout.addWidget(heading)
        if hint:
            helper = QLabel(hint)
            helper.setObjectName("helperText")
            helper.setWordWrap(True)
            layout.addWidget(helper)
        return card, layout

    @staticmethod
    def _add_combo(combo: QComboBox, label: str, value: Any) -> None:
        combo.addItem(label, value)

    def _state_combo(self, value: bool | None) -> QComboBox:
        combo = QComboBox()
        self._add_combo(combo, "不修改", None)
        self._add_combo(combo, "开启", True)
        self._add_combo(combo, "关闭", False)
        index = combo.findData(value)
        combo.setCurrentIndex(max(0, index))
        self.edit_widgets.append(combo)
        return combo

    def _build_ui(self, selection_text: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("parameterHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 15, 18, 15)
        title_row = QHBoxLayout()
        self.title_label = QLabel(self.parameters["name"])
        self.title_label.setObjectName("dialogTitle")
        title_row.addWidget(self.title_label, 1)
        header_layout.addLayout(title_row)
        selection = QLabel(selection_text)
        selection.setObjectName("helperText")
        selection.setWordWrap(True)
        header_layout.addWidget(selection)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(2, 2, 8, 8)
        body_layout.setSpacing(12)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        iso_card, iso_layout = self._card(
            "等值面表现",
            "在这里设置材质和颜色。等值面数值在开始绘图时填写。",
        )
        material_form = QFormLayout()
        material_form.setHorizontalSpacing(16)
        self.material_combo = QComboBox()
        self.material_combo.addItems(core.VMD_MATERIALS)
        self.material_combo.setCurrentText(self.parameters["material"])
        self.edit_widgets.append(self.material_combo)
        material_form.addRow("等值面材质", self.material_combo)
        iso_layout.addLayout(material_form)
        iso_layout.addWidget(self._make_color_row("positive", "正等值面", self.parameters["pos_color_id"], self.parameters["positive_rgb"], self.parameters["positive_rgb_explicit"]))
        iso_layout.addWidget(self._make_color_row("negative", "负等值面", self.parameters["neg_color_id"], self.parameters["negative_rgb"], self.parameters["negative_rgb_explicit"]))
        body_layout.addWidget(iso_card)

        material_card, material_layout = self._card(
            "材质光学参数",
            "勾选要自定义的参数。未勾选的参数保持该材质的默认效果。",
        )
        material_grid = QGridLayout()
        material_grid.setHorizontalSpacing(12)
        material_grid.setVerticalSpacing(8)
        for index, key in enumerate(core.MATERIAL_PARAMETER_NAMES):
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setSpacing(7)
            override = QCheckBox(MATERIAL_LABELS[key])
            spin = QDoubleSpinBox()
            spin.setDecimals(3)
            spin.setSingleStep(0.05)
            spin.setRange(0.0, 4.0 if key == "outline" else 1.0)
            spin.setFixedWidth(110)
            value = self.parameters["material_values"].get(key)
            override.setChecked(value is not None)
            if value is not None:
                spin.setValue(float(value))
            spin.setEnabled(value is not None)
            override.toggled.connect(spin.setEnabled)
            holder_layout.addWidget(override, 1)
            holder_layout.addWidget(spin)
            material_grid.addWidget(holder, index, 0)
            self.edit_widgets.extend((override, spin))
            self.material_rows[key] = override, spin
        material_layout.addLayout(material_grid)
        body_layout.addWidget(material_card)

        display_card, display_layout = self._card("显示与灯光")
        display_form = QFormLayout()
        display_form.setHorizontalSpacing(16)
        self.projection_combo = QComboBox()
        for label, value in (("不修改", None), ("正交投影 Orthographic", "Orthographic"), ("透视投影 Perspective", "Perspective")):
            self._add_combo(self.projection_combo, label, value)
        self.projection_combo.setCurrentIndex(max(0, self.projection_combo.findData(self.parameters["projection"])))
        self.edit_widgets.append(self.projection_combo)
        display_form.addRow("投影", self.projection_combo)

        self.rendermode_combo = QComboBox()
        for label, value in (("不修改", None), ("GLSL", "GLSL"), ("Normal", "Normal")):
            self._add_combo(self.rendermode_combo, label, value)
        self.rendermode_combo.setCurrentIndex(max(0, self.rendermode_combo.findData(self.parameters["rendermode"])))
        self.edit_widgets.append(self.rendermode_combo)
        display_form.addRow("渲染模式", self.rendermode_combo)

        self.axes_combo = QComboBox()
        for label, value in (
            ("不修改", None),
            ("隐藏", "Off"),
            ("左下角", "LowerLeft"),
            ("原点", "Origin"),
        ):
            self._add_combo(self.axes_combo, label, value)
        self.axes_combo.setCurrentIndex(max(0, self.axes_combo.findData(self.parameters["axes"])))
        self.edit_widgets.append(self.axes_combo)
        display_form.addRow("坐标轴", self.axes_combo)

        for key, label in (
            ("depthcue", "景深雾化 Depth Cue"),
            ("ambient_occlusion", "环境光遮蔽 AO"),
            ("shadows", "阴影 Shadows"),
            ("antialias", "抗锯齿 Antialias"),
        ):
            combo = self._state_combo(self.parameters[key])
            self.state_combos[key] = combo
            display_form.addRow(label, combo)
        display_layout.addLayout(display_form)

        display_layout.addWidget(QLabel("灯光 0 到 3"))
        light_grid = QGridLayout()
        light_grid.setHorizontalSpacing(12)
        light_grid.setVerticalSpacing(8)
        for index in range(4):
            combo = self._state_combo(self.parameters["lights"][str(index)])
            self.light_combos[str(index)] = combo
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.addWidget(QLabel(f"灯光 {index}"))
            holder_layout.addWidget(combo, 1)
            row, column = divmod(index, 2)
            light_grid.addWidget(holder, row, column)
        display_layout.addLayout(light_grid)
        body_layout.addWidget(display_card)

        skeleton_card, skeleton_layout = self._card(
            "分子骨架",
            "选择骨架的显示方式、材质和颜色。",
        )
        skeleton_form = QFormLayout()
        skeleton_form.setHorizontalSpacing(16)
        self.skeleton_style_combo = QComboBox()
        self.skeleton_style_combo.addItems(["CPK", "Licorice", "Bonds", "Lines"])
        self.skeleton_style_combo.setCurrentText(self.parameters["skeleton_style"])
        self.edit_widgets.append(self.skeleton_style_combo)
        skeleton_form.addRow("Drawing Method", self.skeleton_style_combo)
        self.skeleton_material_combo = QComboBox()
        self.skeleton_material_combo.addItems(core.VMD_MATERIALS)
        self.skeleton_material_combo.setCurrentText(self.parameters["skeleton_material"])
        self.edit_widgets.append(self.skeleton_material_combo)
        skeleton_form.addRow("骨架材质", self.skeleton_material_combo)
        skeleton_layout.addLayout(skeleton_form)
        skeleton_values = self.parameters["material_values_by_name"].get(
            self.parameters["skeleton_material"], {}
        )
        if skeleton_values and self.parameters["skeleton_material"] != self.parameters["material"]:
            explicit_text = " · ".join(
                f"{MATERIAL_LABELS[key].split()[0]} {value:.3f}"
                for key, value in skeleton_values.items()
                if key in MATERIAL_LABELS
            )
            skeleton_material_detail = QLabel(f"当前骨架材质参数：{explicit_text}")
            skeleton_material_detail.setObjectName("helperText")
            skeleton_material_detail.setWordWrap(True)
            skeleton_layout.addWidget(skeleton_material_detail)
        skeleton_layout.addWidget(self._make_color_row("skeleton", "碳原子/骨架颜色", None, self.parameters["skeleton_rgb"], self.parameters["skeleton_rgb_explicit"]))
        body_layout.addWidget(skeleton_card)

        background_card, background_layout = self._card("背景")
        background_layout.addWidget(self._make_color_row("background", "显示背景", None, self.parameters["background_rgb"], self.parameters["background_rgb_explicit"]))
        body_layout.addWidget(background_card)

        save_card, save_layout = self._card(
            "保存信息",
            "保存内置风格的修改时会创建一个自定义风格；自定义风格会直接更新。",
        )
        save_form = QFormLayout()
        self.name_edit = QLineEdit(
            self.parameters["name"] if self.parameters["is_custom"] else f"{self.parameters['name']} 自定义"
        )
        self.description_edit = QPlainTextEdit(self.parameters["description"])
        self.description_edit.setFixedHeight(78)
        self.edit_widgets.extend((self.name_edit, self.description_edit))
        save_form.addRow("风格名称", self.name_edit)
        save_form.addRow("说明", self.description_edit)
        save_layout.addLayout(save_form)
        self.save_card = save_card
        body_layout.addWidget(save_card)
        body_layout.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        self.cancel_edit_button = QPushButton("放弃修改")
        self.cancel_edit_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_edit_button)
        self.edit_button = QPushButton("编辑参数")
        self.edit_button.setObjectName("primaryBtn")
        self.edit_button.clicked.connect(lambda: self._set_editing(True))
        footer.addWidget(self.edit_button)
        self.save_button = QPushButton("保存风格")
        self.save_button.setObjectName("primaryBtn")
        self.save_button.clicked.connect(self._save)
        footer.addWidget(self.save_button)
        root.addLayout(footer)

    def _make_color_row(
        self,
        key: str,
        title: str,
        color_id: int | None,
        rgb: tuple[float, float, float],
        explicit: bool,
    ) -> QWidget:
        frame = QFrame()
        frame.setObjectName("parameterColorRow")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(10)
        label = QLabel(title)
        label.setMinimumWidth(105)
        layout.addWidget(label)
        id_spin: QSpinBox | None = None
        if color_id is not None:
            id_spin = QSpinBox()
            id_spin.setRange(0, 32)
            id_spin.setValue(int(color_id))
            id_spin.setPrefix("ID ")
            id_spin.setFixedWidth(88)
            layout.addWidget(id_spin)
            self.edit_widgets.append(id_spin)
        color_button = ColorButton(rgb)
        color_button.clicked.connect(lambda _checked=False, row_key=key: self._pick_color(row_key))
        layout.addWidget(color_button)
        explicit_check = QCheckBox("自定义")
        explicit_check.setChecked(bool(explicit))
        layout.addWidget(explicit_check)
        rgb_label = QLabel(
            self._rgb_text(rgb)
            if explicit
            else (
                f"使用 VMD 色号 {color_id}"
                if color_id is not None
                else "保持风格原色"
            )
        )
        rgb_label.setObjectName("helperText")
        layout.addWidget(rgb_label, 1)
        self.edit_widgets.extend((color_button, explicit_check))
        self.color_rows[key] = {
            "button": color_button,
            "check": explicit_check,
            "label": rgb_label,
            "id": id_spin,
            "initial_rgb": rgb,
        }
        explicit_check.toggled.connect(
            lambda checked, row_key=key: self._color_override_changed(row_key, checked)
        )
        if id_spin is not None:
            id_spin.valueChanged.connect(lambda value, row_key=key: self._color_id_changed(row_key, value))
        return frame

    @staticmethod
    def _rgb_text(rgb: tuple[float, float, float]) -> str:
        return "RGB " + " / ".join(f"{value:.3f}" for value in rgb)

    def _color_id_changed(self, key: str, color_id: int) -> None:
        row = self.color_rows[key]
        if row["check"].isChecked():
            return
        rgb = core.VMD_COLOR_RGB.get(color_id, (0.5, 0.5, 0.5))
        row["button"].set_rgb(rgb)
        row["label"].setText(f"使用 VMD 色号 {color_id}")

    def _color_override_changed(self, key: str, checked: bool) -> None:
        row = self.color_rows[key]
        if checked:
            row["label"].setText(self._rgb_text(row["button"].rgb))
            return
        id_spin = row["id"]
        if id_spin is not None:
            rgb = core.VMD_COLOR_RGB.get(id_spin.value(), (0.5, 0.5, 0.5))
            row["button"].set_rgb(rgb)
            row["label"].setText(f"使用 VMD 色号 {id_spin.value()}")
        else:
            row["button"].set_rgb(row["initial_rgb"])
            row["label"].setText("保持风格原色")

    def _pick_color(self, key: str) -> None:
        if not self.editing:
            return
        row = self.color_rows[key]
        selected = QColorDialog.getColor(_qcolor(row["button"].rgb), self, "选择颜色")
        if not selected.isValid():
            return
        rgb = _rgb_from_qcolor(selected)
        row["button"].set_rgb(rgb)
        row["label"].setText(self._rgb_text(rgb))
        row["check"].setChecked(True)

    def _set_editing(self, editing: bool) -> None:
        self.editing = editing
        for widget in self.edit_widgets:
            widget.setEnabled(editing)
        for override, spin in self.material_rows.values():
            spin.setEnabled(editing and override.isChecked())
        self.save_card.setVisible(editing)
        self.close_button.setVisible(not editing)
        self.edit_button.setVisible(not editing)
        self.cancel_edit_button.setVisible(editing)
        self.save_button.setVisible(editing)

    def _collect(self) -> dict:
        output = dict(self.parameters)
        output["material"] = self.material_combo.currentText()
        output["projection"] = self.projection_combo.currentData()
        output["rendermode"] = self.rendermode_combo.currentData()
        output["axes"] = self.axes_combo.currentData()
        for key, combo in self.state_combos.items():
            output[key] = combo.currentData()
        output["lights"] = {key: combo.currentData() for key, combo in self.light_combos.items()}
        output["material_values"] = {
            key: spin.value() if override.isChecked() else None
            for key, (override, spin) in self.material_rows.items()
        }
        output["skeleton_style"] = self.skeleton_style_combo.currentText()
        output["skeleton_material"] = self.skeleton_material_combo.currentText()
        output["skeleton_color_method"] = self.parameters["skeleton_color_method"]
        output["original_rep0_commands"] = self.rep0_commands
        for key, prefix in (("positive", "positive"), ("negative", "negative"), ("skeleton", "skeleton"), ("background", "background")):
            row = self.color_rows[key]
            output[f"{prefix}_rgb"] = row["button"].rgb
            output[f"{prefix}_rgb_explicit"] = row["check"].isChecked()
        output["pos_color_id"] = self.color_rows["positive"]["id"].value()
        output["neg_color_id"] = self.color_rows["negative"]["id"].value()
        if output["background_rgb_explicit"] and not self.parameters["background_rgb_explicit"]:
            output["background_token"] = "silver"
        return output

    def _save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "无法保存", "请填写风格名称。")
            return
        try:
            self.saved_style = core.build_custom_style_from_visual_parameters(
                self._collect(),
                self.base_style,
                name,
                self.description_edit.toPlainText().strip(),
            )
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "参数无效", str(exc))
            return
        self.accept()
