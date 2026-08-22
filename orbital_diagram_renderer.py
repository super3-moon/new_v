"""Publication-style molecular-orbital energy diagram rendering.

The workflow supplies already-rendered orbital images and scientific metadata.
This module owns only deterministic layout and drawing.  Layout is expressed in
logical coordinates and painted through Qt so the same geometry produces a
vector SVG and a high-resolution PNG.
"""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from PIL import Image, ImageChops
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtSvg import QSvgGenerator


LOGICAL_WIDTH = 1100.0
PAIR_ENERGY_TOLERANCE_EV = 0.012
_FONT_FAMILY = ""


class OrbitalDiagramRenderError(RuntimeError):
    """Raised when the final scientific figure cannot be rendered."""


@dataclass(frozen=True, slots=True)
class DiagramOrbital:
    key: str
    spin: str
    label: str
    energy: float
    occupation: float
    image_path: Path


@dataclass(frozen=True, slots=True)
class DiagramLevel:
    energy: float
    keys: tuple[str, ...]
    y: float


@dataclass(frozen=True, slots=True)
class OrbitalPlacement:
    key: str
    spin: str
    image_rect: QRectF
    connector: tuple[QPointF, ...]
    level_y: float
    line_start_x: float
    line_end_x: float
    arrow_x: float


@dataclass(frozen=True, slots=True)
class DiagramLayout:
    width: float
    height: float
    energy_font_size: float
    header_font_size: float
    title_font_size: float
    levels: tuple[DiagramLevel, ...]
    placements: tuple[OrbitalPlacement, ...]
    channel_headers: tuple[tuple[str, float, float], ...]
    title_y: float | None


@dataclass(frozen=True, slots=True)
class DiagramRenderResult:
    png_path: Path
    svg_path: Path
    width: int
    height: int
    layout: DiagramLayout


def _spin_value(value: object) -> str:
    return str(getattr(value, "value", value) or "spatial").casefold()


def _ensure_font_family() -> str:
    """Load one known Unicode-capable system font when Qt has no font paths."""

    global _FONT_FAMILY
    if QGuiApplication.instance() is None:
        raise OrbitalDiagramRenderError(
            "轨道能级图需要在图形应用环境中生成。"
        )
    if _FONT_FAMILY:
        return _FONT_FAMILY
    candidates = (
        (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf", "Arial"),
        (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf", "Segoe UI"),
        (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc", "Microsoft YaHei"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), "DejaVu Sans"),
    )
    available = set(QFontDatabase.families())
    for path, family in candidates:
        if family not in available and path.is_file():
            QFontDatabase.addApplicationFont(str(path))
            available = set(QFontDatabase.families())
        if family in available:
            _FONT_FAMILY = family
            return family
    _FONT_FAMILY = QFont().defaultFamily()
    return _FONT_FAMILY


def _coerce_orbitals(values: Sequence[DiagramOrbital | Mapping[str, object]]) -> list[DiagramOrbital]:
    orbitals: list[DiagramOrbital] = []
    for value in values:
        if isinstance(value, DiagramOrbital):
            item = value
        elif isinstance(value, Mapping):
            image_path = Path(str(value.get("image_path") or "")).expanduser()
            item = DiagramOrbital(
                key=str(value.get("key") or ""),
                spin=_spin_value(value.get("spin", "spatial")),
                label=str(value.get("label") or ""),
                energy=float(value.get("energy")),
                occupation=float(value.get("occupation") or 0.0),
                image_path=image_path,
            )
        else:
            raise OrbitalDiagramRenderError("轨道图数据格式无效。")
        if not item.key or not math.isfinite(item.energy):
            raise OrbitalDiagramRenderError("轨道图缺少有效的轨道编号或能量。")
        if not item.image_path.is_file():
            raise OrbitalDiagramRenderError(f"找不到轨道图片：{item.image_path}")
        orbitals.append(item)
    if not orbitals:
        raise OrbitalDiagramRenderError("没有可用于合成能级图的轨道图片。")
    return orbitals


def _shared_crop(paths: Sequence[Path]) -> tuple[QRectF, float]:
    """Return one crop rectangle for every orbital to preserve view alignment."""

    union: tuple[int, int, int, int] | None = None
    common_size: tuple[int, int] | None = None
    for path in paths:
        with Image.open(path) as opened:
            source = opened.convert("RGB")
            if common_size is None:
                common_size = source.size
            elif source.size != common_size:
                raise OrbitalDiagramRenderError("同一任务的轨道图片尺寸不一致，无法保持统一视角。")
            white = Image.new("RGB", source.size, "white")
            mask = ImageChops.difference(source, white).convert("L").point(
                lambda value: 255 if value > 10 else 0
            )
            bounds = mask.getbbox()
            if bounds is not None:
                union = (
                    bounds
                    if union is None
                    else (
                        min(union[0], bounds[0]),
                        min(union[1], bounds[1]),
                        max(union[2], bounds[2]),
                        max(union[3], bounds[3]),
                    )
                )
    if common_size is None:
        raise OrbitalDiagramRenderError("无法读取轨道图片尺寸。")
    if union is None:
        union = (0, 0, common_size[0], common_size[1])
    padding = max(8, round(min(common_size) * 0.025))
    left = max(0, union[0] - padding)
    top = max(0, union[1] - padding)
    right = min(common_size[0], union[2] + padding)
    bottom = min(common_size[1], union[3] + padding)
    width = max(1, right - left)
    height = max(1, bottom - top)
    return QRectF(float(left), float(top), float(width), float(height)), width / height


def _fit_rect(box: QRectF, aspect: float) -> QRectF:
    if aspect <= 0:
        return QRectF(box)
    if box.width() / box.height() > aspect:
        height = box.height()
        width = height * aspect
    else:
        width = box.width()
        height = width / aspect
    return QRectF(
        box.center().x() - width / 2.0,
        box.center().y() - height / 2.0,
        width,
        height,
    )


def _pair_levels(
    orbitals: Sequence[DiagramOrbital], *, tolerance: float
) -> list[list[DiagramOrbital]]:
    """Pair only opposite-spin, nearly equal levels; never merge same-spin MOs."""

    levels: list[list[DiagramOrbital]] = []
    for orbital in sorted(orbitals, key=lambda item: (-item.energy, item.spin, item.key)):
        target: list[DiagramOrbital] | None = None
        for level in levels:
            center = sum(item.energy for item in level) / len(level)
            spins = {item.spin for item in level}
            if orbital.spin not in spins and abs(orbital.energy - center) <= tolerance:
                target = level
                break
        if target is None:
            levels.append([orbital])
        else:
            target.append(orbital)
    levels.sort(key=lambda level: -sum(item.energy for item in level) / len(level))
    return levels


def _level_positions(
    levels: Sequence[Sequence[DiagramOrbital]],
    first_image_y: float,
    last_image_y: float,
    *,
    minimum_typical_gap: float,
) -> list[float]:
    if len(levels) == 1:
        return [(first_image_y + last_image_y) / 2.0]
    centers = [sum(item.energy for item in level) / len(level) for level in levels]
    energy_gaps = [max(0.0, centers[index] - centers[index + 1]) for index in range(len(centers) - 1)]
    ordinary = [gap for gap in energy_gaps if gap > 1.0e-9]
    typical = max(minimum_typical_gap, median(ordinary) if ordinary else 1.0)
    visual_gaps = [
        48.0 + min(160.0, 48.0 * math.log1p(gap / typical))
        for gap in energy_gaps
    ]
    available = max(120.0, last_image_y - first_image_y - 18.0)
    raw_span = sum(visual_gaps)
    if raw_span > available:
        scale = available / raw_span
        visual_gaps = [max(34.0, gap * scale) for gap in visual_gaps]
        compressed = sum(visual_gaps)
        if compressed > available:
            visual_gaps = [gap * available / compressed for gap in visual_gaps]
    span = sum(visual_gaps)
    midpoint = (first_image_y + last_image_y) / 2.0
    positions = [midpoint - span / 2.0]
    for gap in visual_gaps:
        positions.append(positions[-1] + gap)
    return positions


def build_diagram_layout(
    values: Sequence[DiagramOrbital | Mapping[str, object]],
    *,
    image_aspect: float,
    show_title: bool = False,
    energy_unit: str = "eV",
) -> DiagramLayout:
    """Build a compact, reference-like layout without UI cards or panels."""

    orbitals = _coerce_orbitals(values)
    family = _ensure_font_family()
    by_channel: dict[str, list[DiagramOrbital]] = {}
    for item in orbitals:
        by_channel.setdefault(item.spin, []).append(item)
    for items in by_channel.values():
        items.sort(key=lambda item: (-item.energy, item.key))

    has_alpha_beta = "alpha" in by_channel and "beta" in by_channel
    if has_alpha_beta:
        channels = ["alpha", "beta"]
    else:
        channels = sorted(by_channel, key=lambda item: (item not in {"spatial", "alpha"}, item))
    is_hartree = str(energy_unit).casefold() in {"hartree", "au", "a.u."}
    tolerance = (
        PAIR_ENERGY_TOLERANCE_EV / 27.211386245988
        if is_hartree
        else PAIR_ENERGY_TOLERANCE_EV
    )
    levels = _pair_levels(orbitals, tolerance=tolerance)
    row_slots = max(len(levels), max(len(by_channel[channel]) for channel in channels))
    top_margin = 82.0 if not show_title else 126.0
    image_box_width = 300.0 if has_alpha_beta else 380.0
    image_box_height = 150.0 if has_alpha_beta else 185.0
    row_pitch = max(172.0, image_box_height + 22.0)
    first_center = top_margin + image_box_height / 2.0
    last_center = first_center + max(0, row_slots - 1) * row_pitch
    height = max(680.0, last_center + image_box_height / 2.0 + 58.0)

    energy_font_size = 21.0
    header_font_size = 24.0
    title_font_size = 27.0
    energy_font = QFont(family)
    energy_font.setPixelSize(round(energy_font_size))
    energy_metrics = QFontMetricsF(energy_font)
    widest_label = max(
        energy_metrics.horizontalAdvance(f"{item.energy:.2f} eV")
        for item in orbitals
    )

    level_positions = _level_positions(
        levels,
        first_center,
        last_center,
        minimum_typical_gap=0.05 / 27.211386245988 if is_hartree else 0.05,
    )
    level_y_by_key: dict[str, float] = {}
    image_y_by_key: dict[str, float] = {}
    diagram_levels: list[DiagramLevel] = []
    for level_index, (members, y) in enumerate(zip(levels, level_positions)):
        energy = sum(item.energy for item in members) / len(members)
        keys = tuple(item.key for item in members)
        diagram_levels.append(DiagramLevel(energy=energy, keys=keys, y=y))
        for key in keys:
            level_y_by_key[key] = y
            image_y_by_key[key] = first_center + level_index * row_pitch

    placements: list[OrbitalPlacement] = []
    headers: list[tuple[str, float, float]] = []
    title_y = 34.0 if show_title else None
    if has_alpha_beta:
        center_x = LOGICAL_WIDTH / 2.0
        text_half_gap = widest_label / 2.0 + 15.0
        alpha_line = (410.0, center_x - text_half_gap)
        beta_line = (center_x + text_half_gap, 690.0)
        channel_geometry = {
            "alpha": (22.0, alpha_line, "α MOs", "left"),
            "beta": (LOGICAL_WIDTH - 22.0 - image_box_width, beta_line, "β MOs", "right"),
        }
        for channel in channels:
            items = by_channel[channel]
            # Long vertical detours use lanes nearest the energy axis.  This
            # small ordering rule prevents them from cutting through the short
            # horizontal connector of an adjacent level while preserving the
            # existing dimensions and publication layout.
            lane_order = sorted(
                items,
                key=lambda orbital: (
                    abs(image_y_by_key[orbital.key] - level_y_by_key[orbital.key]),
                    image_y_by_key[orbital.key],
                    orbital.key,
                ),
            )
            lane_rank = {item.key: rank for rank, item in enumerate(lane_order, 1)}
            image_x, line, header, side = channel_geometry[channel]
            header_x = 365.0 if side == "left" else LOGICAL_WIDTH - 365.0
            headers.append((header, header_x, 36.0 if not show_title else 78.0))
            lane_span = 70.0
            for item in items:
                center_y = image_y_by_key[item.key]
                image_box = QRectF(
                    image_x,
                    center_y - image_box_height / 2.0,
                    image_box_width,
                    image_box_height,
                )
                image_rect = _fit_rect(image_box, image_aspect)
                level_y = level_y_by_key[item.key]
                fraction = lane_rank[item.key] / (len(items) + 1)
                if side == "left":
                    lane_x = image_box.right() + 18.0 + lane_span * fraction
                    line_outer, line_inner = line
                    connector = (
                        QPointF(image_rect.right(), center_y),
                        QPointF(lane_x, center_y),
                        QPointF(lane_x, level_y),
                        QPointF(line_outer, level_y),
                    )
                    arrow_x = line_inner - 15.0
                else:
                    lane_x = image_box.left() - 18.0 - lane_span * fraction
                    line_inner, line_outer = line
                    connector = (
                        QPointF(image_rect.left(), center_y),
                        QPointF(lane_x, center_y),
                        QPointF(lane_x, level_y),
                        QPointF(line_outer, level_y),
                    )
                    arrow_x = line_inner + 15.0
                placements.append(
                    OrbitalPlacement(
                        key=item.key,
                        spin=item.spin,
                        image_rect=image_rect,
                        connector=connector,
                        level_y=level_y,
                        line_start_x=line[0],
                        line_end_x=line[1],
                        arrow_x=arrow_x,
                    )
                )
    else:
        channel = channels[0]
        items = by_channel[channel]
        image_x = 54.0
        line = (565.0, 785.0)
        header = {"alpha": "α MOs", "beta": "β MOs"}.get(channel, "MOs")
        headers.append((header, 500.0, 36.0 if not show_title else 78.0))
        lane_order = sorted(
            items,
            key=lambda orbital: (
                abs(image_y_by_key[orbital.key] - level_y_by_key[orbital.key]),
                image_y_by_key[orbital.key],
                orbital.key,
            ),
        )
        lane_rank = {item.key: rank for rank, item in enumerate(lane_order, 1)}
        for item in items:
            center_y = image_y_by_key[item.key]
            image_box = QRectF(
                image_x,
                center_y - image_box_height / 2.0,
                image_box_width,
                image_box_height,
            )
            image_rect = _fit_rect(image_box, image_aspect)
            level_y = level_y_by_key[item.key]
            lane_x = image_box.right() + 30.0 + lane_rank[item.key] * 10.0
            placements.append(
                OrbitalPlacement(
                    key=item.key,
                    spin=item.spin,
                    image_rect=image_rect,
                    connector=(
                        QPointF(image_rect.right(), center_y),
                        QPointF(lane_x, center_y),
                        QPointF(lane_x, level_y),
                        QPointF(line[0], level_y),
                    ),
                    level_y=level_y,
                    line_start_x=line[0],
                    line_end_x=line[1],
                    arrow_x=line[0] + 22.0,
                )
            )

    return DiagramLayout(
        width=LOGICAL_WIDTH,
        height=height,
        energy_font_size=energy_font_size,
        header_font_size=header_font_size,
        title_font_size=title_font_size,
        levels=tuple(diagram_levels),
        placements=tuple(placements),
        channel_headers=tuple(headers),
        title_y=title_y,
    )


def _font(size: float, *, bold: bool = False) -> QFont:
    font = QFont(_ensure_font_family())
    font.setPixelSize(max(1, round(size)))
    font.setBold(bold)
    return font


def _draw_polyline(painter: QPainter, points: Sequence[QPointF]) -> None:
    for start, end in zip(points, points[1:]):
        painter.drawLine(start, end)


def _draw_arrow(painter: QPainter, x: float, y: float, direction: int) -> None:
    half = 22.0
    start_y = y + half if direction < 0 else y - half
    end_y = y - half if direction < 0 else y + half
    painter.drawLine(QPointF(x, start_y), QPointF(x, end_y))
    tip = QPolygonF(
        [
            QPointF(x, end_y),
            QPointF(x - 5.5, end_y - direction * 9.0),
            QPointF(x + 5.5, end_y - direction * 9.0),
        ]
    )
    painter.drawPolygon(tip)


def _paint(
    painter: QPainter,
    layout: DiagramLayout,
    orbitals: Sequence[DiagramOrbital],
    source_crop: QRectF,
    *,
    energy_unit: str,
    energy_decimals: int,
    title: str,
    show_title: bool,
) -> None:
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.TextAntialiasing
        | QPainter.RenderHint.SmoothPixmapTransform,
        True,
    )
    painter.fillRect(QRectF(0.0, 0.0, layout.width, layout.height), QColor("white"))
    by_key = {item.key: item for item in orbitals}
    placement_by_key = {item.key: item for item in layout.placements}

    if show_title and layout.title_y is not None and title:
        painter.setPen(QColor("#111111"))
        painter.setFont(_font(layout.title_font_size, bold=True))
        painter.drawText(
            QRectF(30.0, layout.title_y - 20.0, layout.width - 60.0, 40.0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            title,
        )

    painter.setPen(QColor("#111111"))
    painter.setFont(_font(layout.header_font_size))
    for text, x, y in layout.channel_headers:
        metrics = QFontMetricsF(painter.font())
        width = metrics.horizontalAdvance(text)
        left = x - width / 2.0
        if text[:1] in {"α", "β"}:
            # Qt 6.11's Tiny-SVG writer can replace Greek text with U+FFFD on
            # Windows even though the raster painter displays it correctly.
            # Converting just that glyph to a vector outline keeps SVG and PNG
            # visually identical while ordinary text remains searchable.
            glyph = QPainterPath()
            glyph.addText(QPointF(left, y), painter.font(), text[0])
            painter.fillPath(glyph, QColor("#111111"))
            rest = text[1:]
            painter.drawText(
                QPointF(left + metrics.horizontalAdvance(text[0]), y), rest
            )
        else:
            painter.drawText(QPointF(left, y), text)

    connector_pen = QPen(QColor("#4a4a4a"))
    connector_pen.setWidthF(1.35)
    connector_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    connector_pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    painter.setPen(connector_pen)
    for placement in layout.placements:
        _draw_polyline(painter, placement.connector)

    for placement in layout.placements:
        image = QImage(str(by_key[placement.key].image_path))
        if image.isNull():
            raise OrbitalDiagramRenderError(f"无法读取轨道图片：{by_key[placement.key].image_path}")
        painter.drawImage(placement.image_rect, image, source_crop)

    level_pen = QPen(QColor("#202020"))
    level_pen.setWidthF(1.8)
    level_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(level_pen)
    for placement in layout.placements:
        painter.drawLine(
            QPointF(placement.line_start_x, placement.level_y),
            QPointF(placement.line_end_x, placement.level_y),
        )

    suffix = "a.u." if energy_unit == "Hartree" else "eV"
    painter.setFont(_font(layout.energy_font_size))
    painter.setPen(QColor("#111111"))
    metrics = QFontMetricsF(painter.font())
    for level in layout.levels:
        label = f"{level.energy:.{energy_decimals}f} {suffix}"
        width = metrics.horizontalAdvance(label)
        baseline = level.y + (metrics.ascent() - metrics.descent()) / 2.0
        painter.drawText(QPointF(layout.width / 2.0 - width / 2.0, baseline), label)

    arrow_pen = QPen(QColor("#3569b5"))
    arrow_pen.setWidthF(2.2)
    painter.setPen(arrow_pen)
    painter.setBrush(QColor("#3569b5"))
    for item in orbitals:
        if item.occupation <= 0.1:
            continue
        placement = placement_by_key[item.key]
        if item.spin == "beta":
            _draw_arrow(painter, placement.arrow_x, placement.level_y, 1)
        elif item.spin == "spatial" and item.occupation > 1.5:
            _draw_arrow(painter, placement.arrow_x - 6.0, placement.level_y, -1)
            _draw_arrow(painter, placement.arrow_x + 6.0, placement.level_y, 1)
        else:
            _draw_arrow(painter, placement.arrow_x, placement.level_y, -1)


def render_orbital_energy_diagram(
    values: Sequence[DiagramOrbital | Mapping[str, object]],
    png_path: Path | str,
    svg_path: Path | str,
    *,
    output_width: int = 1800,
    energy_unit: str = "eV",
    energy_decimals: int = 2,
    title: str = "Molecular orbital energy diagram",
    show_title: bool = False,
) -> DiagramRenderResult:
    """Render matching PNG and SVG figures with no decorative UI framing."""

    orbitals = _coerce_orbitals(values)
    source_crop, image_aspect = _shared_crop([item.image_path for item in orbitals])
    layout = build_diagram_layout(
        orbitals,
        image_aspect=image_aspect,
        show_title=show_title,
        energy_unit=energy_unit,
    )
    output_width = max(900, min(12000, int(output_width)))
    output_height = max(1, round(output_width * layout.height / layout.width))
    png = Path(png_path).expanduser().resolve()
    svg = Path(svg_path).expanduser().resolve()
    png.parent.mkdir(parents=True, exist_ok=True)
    svg.parent.mkdir(parents=True, exist_ok=True)
    png_temporary = png.with_name(f".{png.name}.{uuid.uuid4().hex}.tmp.png")
    svg_temporary = svg.with_name(f".{svg.name}.{uuid.uuid4().hex}.tmp.svg")
    try:
        image = QImage(output_width, output_height, QImage.Format.Format_ARGB32)
        image.fill(QColor("white"))
        painter = QPainter(image)
        if not painter.isActive():
            raise OrbitalDiagramRenderError("无法创建轨道能级图画布。")
        painter.scale(output_width / layout.width, output_height / layout.height)
        try:
            _paint(
                painter,
                layout,
                orbitals,
                source_crop,
                energy_unit=energy_unit,
                energy_decimals=energy_decimals,
                title=title,
                show_title=show_title,
            )
        finally:
            painter.end()
        if not image.save(str(png_temporary), "PNG"):
            raise OrbitalDiagramRenderError("无法保存轨道能级图 PNG。")

        generator = QSvgGenerator()
        generator.setFileName(str(svg_temporary))
        generator.setSize(QSize(output_width, output_height))
        generator.setViewBox(QRectF(0.0, 0.0, layout.width, layout.height))
        generator.setTitle(title if show_title else "Molecular orbital energy diagram")
        generator.setDescription("Publication-style molecular orbital energy diagram")
        svg_painter = QPainter(generator)
        if not svg_painter.isActive():
            raise OrbitalDiagramRenderError("无法创建轨道能级图 SVG。")
        try:
            _paint(
                svg_painter,
                layout,
                orbitals,
                source_crop,
                energy_unit=energy_unit,
                energy_decimals=energy_decimals,
                title=title,
                show_title=show_title,
            )
        finally:
            svg_painter.end()
        # QSvgGenerator keeps its output device open for its own lifetime on
        # Windows.  Drop both wrappers before the atomic rename.
        del svg_painter
        del generator

        os.replace(png_temporary, png)
        os.replace(svg_temporary, svg)
    finally:
        png_temporary.unlink(missing_ok=True)
        svg_temporary.unlink(missing_ok=True)
    return DiagramRenderResult(
        png_path=png,
        svg_path=svg,
        width=output_width,
        height=output_height,
        layout=layout,
    )


__all__ = [
    "DiagramLayout",
    "DiagramLevel",
    "DiagramOrbital",
    "DiagramRenderResult",
    "OrbitalDiagramRenderError",
    "OrbitalPlacement",
    "build_diagram_layout",
    "render_orbital_energy_diagram",
]
