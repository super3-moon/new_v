from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import socket
import tempfile
import threading
import webbrowser
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
STYLE_DIR = ROOT / "vmd_cube_styles"
CONFIG_FILE = ROOT / "vmd_style_tool_config.json"
CUSTOM_STYLES_FILE = ROOT / "vmd_custom_styles.json"

_DATA_LOCK = threading.RLock()
CUSTOM_STYLES_LOAD_ERROR = ""


def write_text_atomic(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    """Atomically replace a text file without exposing a partially written result."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def write_bytes_atomic(path: Path | str, data: bytes) -> Path:
    """Atomically replace a binary file without exposing a partial copy."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target

BASE_VIEW = [
    "color Display Background white",
    "display depthcue off",
    "display rendermode GLSL",
    "axes location Off",
]

RAW_STYLES = [
    {
        "id": "classic_glossy_447",
        "name": "Classic Glossy (Red/Blue)",
        "image": "07_glossy_default.jpg",
        "material": "Glossy",
        "pos_color": 1,
        "neg_color": 0,
        "commands": BASE_VIEW + [
            "light 0 on",
            "light 1 on",
            "light 2 on",
            "light 3 off",
        ],
        "sources": ["http://sobereva.com/447"],
        "notes": "Default showorb-style dual-color glossy isosurface.",
    },
    {
        "id": "classic_glossy_483",
        "name": "Classic Glossy (showcub)",
        "image": "01_dual_color_basic.png",
        "material": "Glossy",
        "pos_color": 1,
        "neg_color": 0,
        "commands": BASE_VIEW + [
            "light 0 on",
            "light 1 on",
            "light 2 on",
            "light 3 off",
        ],
        "sources": ["http://sobereva.com/483"],
        "notes": "Same visual core as showorb classic style.",
    },
    {
        "id": "soft_glossy_449",
        "name": "Soft Artistic Glossy",
        "image": "08_vmdrender_soft_material.jpg",
        "material": "Glossy",
        "pos_color": 12,
        "neg_color": 22,
        "commands": BASE_VIEW + [
            "color Name C tan",
            "color change rgb tan 0.700000 0.560000 0.360000",
            "material change mirror Opaque 0.15",
            "material change outline Opaque 4.000000",
            "material change outlinewidth Opaque 0.5",
            "material change ambient Glossy 0.1",
            "material change diffuse Glossy 0.600000",
            "material change opacity Glossy 0.75",
            "material change shininess Glossy 1.0",
            "light 3 on",
        ],
        "sources": ["http://sobereva.com/449"],
        "notes": "Color and material tuning from VMDrender.txt logic.",
    },
    {
        "id": "edgyglass_overlap_483",
        "name": "Overlap Emphasis (EdgyGlass)",
        "image": "03_iso2_overlap_cube.png",
        "material": "EdgyGlass",
        "pos_color": 12,
        "neg_color": 22,
        "commands": BASE_VIEW + [
            "light 0 on",
            "light 1 on",
            "light 2 on",
            "light 3 on",
        ],
        "sources": ["http://sobereva.com/483"],
        "notes": "Good for two-cube overlap readability.",
    },
    {
        "id": "bright_bule_yellow_userpack",
        "name": "Bright Blue + Yellow",
        "image": "23_bright_blue_yellow.png",
        "material": "Glossy",
        "pos_color": 12,
        "neg_color": 22,
        "commands": BASE_VIEW + [
            "color Display Background silver",
            "color change rgb silver 0.960000 0.960000 0.960000",
            "color change rgb 12 0.030000 0.740000 0.830000",
            "color change rgb 22 0.920000 0.770000 0.090000",
            "display projection Orthographic",
            "display depthcue off",
            "axes location Off",
            "material change ambient Glossy 0.100000",
            "material change specular Glossy 0.080000",
            "material change diffuse Glossy 0.920000",
            "material change shininess Glossy 0.040000",
            "material change opacity Glossy 1.000000",
        ],
        "sources": ["user-upload:param-pack"],
        "notes": "Imported from Bright_Bule+Yellow.txt",
    },
    {
        "id": "modern_cool_palette_userpack",
        "name": "Modern Cool Palette",
        "image": "24_modern_cool_palette.png",
        "material": "Glossy",
        "pos_color": 13,
        "neg_color": 24,
        "commands": BASE_VIEW + [
            "color Display Background silver",
            "color change rgb silver 0.965000 0.965000 0.965000",
            "color change rgb 13 0.360000 0.340000 0.580000",
            "color change rgb 24 0.820000 0.830000 0.860000",
            "display projection Orthographic",
            "display depthcue off",
            "material change ambient Glossy 0.100000",
            "material change specular Glossy 0.060000",
            "material change diffuse Glossy 0.920000",
            "material change shininess Glossy 0.030000",
            "material change mirror Glossy 0.000000",
            "material change opacity Glossy 1.000000",
        ],
        "sources": ["user-upload:param-pack"],
        "notes": "Imported from Modern_cool palette.txt",
    },
    {
        "id": "edgyglass_tuned_443",
        "name": "EdgyGlass Tuned Opacity",
        "image": "05_edgyglass_tuned_opacity.jpg",
        "material": "EdgyGlass",
        "pos_color": 12,
        "neg_color": 22,
        "commands": BASE_VIEW + [
            "display projection Orthographic",
            "material change outline EdgyGlass 0.590000",
            "material change outlinewidth EdgyGlass 0.340000",
            "material change opacity EdgyGlass 0.730000",
            "material change shininess EdgyGlass 0.800000",
            "material change diffuse EdgyGlass 0.800000",
            "material change specular EdgyGlass 0.250000",
        ],
        "sources": ["http://sobereva.com/443"],
        "notes": "Reduced glare for crowded surfaces.",
    },
    {
        "id": "goodsell_58009",
        "name": "Goodsell Pastel",
        "image": "13_bbs_goodsell_example.jpg",
        "material": "Goodsell",
        "pos_color": 12,
        "neg_color": 22,
        "commands": BASE_VIEW + [
            "light 0 on",
            "light 1 off",
            "light 2 on",
            "light 3 off",
            "material change ambient Goodsell 0.650000",
            "material change diffuse Goodsell 1.000000",
            "material change specular Goodsell 0.100000",
            "material change shininess Goodsell 1.000000",
            "material change mirror Goodsell 0.000000",
            "material change opacity Goodsell 0.700000",
            "material change outline Goodsell 3.300000",
            "material change outlinewidth Goodsell 0.600000",
        ],
        "sources": ["http://bbs.keinsci.com/forum.php?mod=viewthread&tid=58009"],
        "notes": "Soft palette style shared in forum.",
    },
    {
        "id": "edgy_58009",
        "name": "Edgy Contrast",
        "image": "14_bbs_edgy_example1.jpg",
        "material": "Edgy",
        "pos_color": 12,
        "neg_color": 22,
        "commands": BASE_VIEW + [
            "light 0 on",
            "light 1 on",
            "light 2 off",
            "light 3 on",
            "material change ambient Edgy 0.400000",
            "material change diffuse Edgy 0.880000",
            "material change specular Edgy 0.000000",
            "material change shininess Edgy 0.750000",
            "material change mirror Edgy 0.000000",
            "material change opacity Edgy 1.000000",
            "material change outline Edgy 1.500000",
            "material change outlinewidth Edgy 0.800000",
        ],
        "sources": ["http://bbs.keinsci.com/forum.php?mod=viewthread&tid=58009"],
        "notes": "Edgy material and three-lights setup from forum share.",
    },
    {
        "id": "translucent_clean_447",
        "name": "Translucent Clean",
        "image": "10_tachyon_mediumshade_vmd.jpg",
        "material": "Translucent",
        "pos_color": 12,
        "neg_color": 22,
        "commands": BASE_VIEW + [
            "light 0 on",
            "light 1 on",
            "light 2 on",
            "light 3 on",
        ],
        "sources": ["http://sobereva.com/447", "http://sobereva.com/483"],
        "notes": "Transparent look for cleaner overlap visibility.",
    },
    {
        "id": "rdg_clarity_291",
        "name": "RDG Clarity",
        "image": "12_rdg_light3_on.png",
        "material": "Glossy",
        "pos_color": 12,
        "neg_color": 22,
        "commands": BASE_VIEW + [
            "light 0 on",
            "light 1 on",
            "light 2 on",
            "light 3 on",
        ],
        "sources": ["http://sobereva.com/291"],
        "notes": "Depthcue off with brighter lights for analysis snapshots.",
    },
]


def _style_signature(style: dict) -> tuple:
    return (
        style["material"],
        int(style["pos_color"]),
        int(style["neg_color"]),
        tuple(style["commands"]),
    )


def dedupe_styles(raw_styles: list[dict]) -> tuple[list[dict], list[dict]]:
    merged: dict[tuple, dict] = {}
    duplicates: list[dict] = []
    for entry in raw_styles:
        style = deepcopy(entry)
        key = _style_signature(style)
        if key in merged:
            base = merged[key]
            base["sources"] = sorted(set(base["sources"] + style["sources"]))
            base.setdefault("alias_ids", []).append(style["id"])
            duplicates.append({"removed": style["id"], "kept": base["id"]})
            if not (STYLE_DIR / base["image"]).exists() and (STYLE_DIR / style["image"]).exists():
                base["image"] = style["image"]
            continue
        style["alias_ids"] = []
        merged[key] = style
    return list(merged.values()), duplicates


STYLES, DUPLICATES = dedupe_styles(RAW_STYLES)
STYLE_BY_ID = {style["id"]: style for style in STYLES}

SKELETON_STYLES = [
    {
        "id": "skeleton_default_opaque",
        "name": "Skeleton Default Opaque",
        "image": "18_sob449_1.jpg",
        "pre_commands": [],
        "rep0_commands": [
            "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000",
            "mol modcolor 0 top Name",
            "mol modmaterial 0 top Opaque",
        ],
        "sources": ["http://sobereva.com/449"],
        "notes": "Baseline CPK+Opaque skeleton style.",
    },
    {
        "id": "skeleton_tan_opaque_449",
        "name": "Skeleton Tan Opaque",
        "image": "19_sob449_2.jpg",
        "pre_commands": [
            "color Name C tan",
            "color change rgb tan 0.700000 0.560000 0.360000",
            "material change mirror Opaque 0.15",
            "material change outline Opaque 4.000000",
            "material change outlinewidth Opaque 0.5",
        ],
        "rep0_commands": [
            "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000",
            "mol modcolor 0 top Name",
            "mol modmaterial 0 top Opaque",
        ],
        "sources": ["http://sobereva.com/449"],
        "notes": "Soft tan carbon color with stronger Opaque outline.",
    },
    {
        "id": "skeleton_goodsell_58009",
        "name": "Skeleton Goodsell",
        "image": "13_bbs_goodsell_example.jpg",
        "pre_commands": [
            "material change ambient Goodsell 0.650000",
            "material change diffuse Goodsell 1.000000",
            "material change specular Goodsell 0.100000",
            "material change shininess Goodsell 1.000000",
            "material change mirror Goodsell 0.000000",
            "material change opacity Goodsell 0.700000",
            "material change outline Goodsell 3.300000",
            "material change outlinewidth Goodsell 0.600000",
        ],
        "rep0_commands": [
            "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000",
            "mol modcolor 0 top Name",
            "mol modmaterial 0 top Goodsell",
        ],
        "sources": ["http://bbs.keinsci.com/forum.php?mod=viewthread&tid=58009"],
        "notes": "Forum-shared soft Goodsell skeleton look.",
    },
    {
        "id": "skeleton_edgy_58009",
        "name": "Skeleton Edgy",
        "image": "14_bbs_edgy_example1.jpg",
        "pre_commands": [
            "material change ambient Edgy 0.400000",
            "material change diffuse Edgy 0.880000",
            "material change specular Edgy 0.000000",
            "material change shininess Edgy 0.750000",
            "material change mirror Edgy 0.000000",
            "material change opacity Edgy 1.000000",
            "material change outline Edgy 1.500000",
            "material change outlinewidth Edgy 0.800000",
        ],
        "rep0_commands": [
            "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000",
            "mol modcolor 0 top Name",
            "mol modmaterial 0 top Edgy",
        ],
        "sources": ["http://bbs.keinsci.com/forum.php?mod=viewthread&tid=58009"],
        "notes": "Forum-shared sharp Edgy skeleton look.",
    },
]
SKELETON_BY_ID = {style["id"]: style for style in SKELETON_STYLES}

VMD_MATERIALS = [
    "Opaque",
    "Transparent",
    "BrushedMetal",
    "Diffuse",
    "Ghost",
    "Glass1",
    "Glass2",
    "Glass3",
    "Glossy",
    "HardPlastic",
    "MetallicPastel",
    "Steel",
    "Translucent",
    "Edgy",
    "EdgyShiny",
    "EdgyGlass",
    "Goodsell",
    "AOShiny",
    "AOChalky",
    "AOEdgy",
    "BlownGlass",
    "GlassBubble",
    "RTChrome",
]

VMD_COLOR_RGB = {
    0: (0.000, 0.000, 1.000),
    1: (1.000, 0.000, 0.000),
    2: (0.350, 0.350, 0.350),
    3: (1.000, 0.500, 0.000),
    4: (1.000, 1.000, 0.000),
    5: (0.500, 0.500, 0.200),
    6: (0.600, 0.600, 0.600),
    7: (0.000, 1.000, 0.000),
    8: (1.000, 1.000, 1.000),
    9: (1.000, 0.600, 0.600),
    10: (0.250, 0.750, 0.750),
    11: (0.650, 0.000, 0.650),
    12: (0.500, 0.900, 0.400),
    13: (0.900, 0.400, 0.700),
    14: (0.500, 0.300, 0.000),
    15: (0.500, 0.500, 0.750),
    16: (0.000, 0.000, 0.000),
    17: (0.880, 0.970, 0.020),
    18: (0.550, 0.900, 0.020),
    19: (0.000, 0.900, 0.040),
    20: (0.000, 0.900, 0.500),
    21: (0.000, 0.880, 1.000),
    22: (0.000, 0.760, 1.000),
    23: (0.020, 0.380, 0.670),
    24: (0.010, 0.040, 0.930),
    25: (0.270, 0.000, 0.980),
    26: (0.450, 0.000, 0.900),
    27: (0.900, 0.000, 0.900),
    28: (1.000, 0.000, 0.660),
    29: (0.980, 0.000, 0.230),
    30: (0.810, 0.000, 0.000),
    31: (0.890, 0.350, 0.000),
    32: (0.960, 0.720, 0.000),
}

VMD_NAMED_COLOR_RGB = {
    "blue": VMD_COLOR_RGB[0],
    "red": VMD_COLOR_RGB[1],
    "gray": VMD_COLOR_RGB[2],
    "orange": VMD_COLOR_RGB[3],
    "yellow": VMD_COLOR_RGB[4],
    "tan": VMD_COLOR_RGB[5],
    "silver": VMD_COLOR_RGB[6],
    "green": VMD_COLOR_RGB[7],
    "white": VMD_COLOR_RGB[8],
    "pink": VMD_COLOR_RGB[9],
    "cyan": VMD_COLOR_RGB[10],
    "purple": VMD_COLOR_RGB[11],
    "lime": VMD_COLOR_RGB[12],
    "mauve": VMD_COLOR_RGB[13],
    "ochre": VMD_COLOR_RGB[14],
    "iceblue": VMD_COLOR_RGB[15],
    "black": VMD_COLOR_RGB[16],
}

MATERIAL_PARAMETER_NAMES = (
    "ambient",
    "diffuse",
    "specular",
    "shininess",
    "mirror",
    "opacity",
    "outline",
    "outlinewidth",
)


def _color_id_from_expr(expr: object, fallback: int) -> int:
    match = re.fullmatch(r"\s*ColorID\s+(\d+)\s*", str(expr or ""), re.IGNORECASE)
    if match:
        return int(match.group(1))
    try:
        return int(fallback)
    except (TypeError, ValueError):
        return 0


def _known_color_rgb(token: str, rgb_changes: dict[str, tuple[float, float, float]]) -> tuple[float, float, float]:
    normalized = token.strip().lower()
    if normalized in rgb_changes:
        return rgb_changes[normalized]
    try:
        color_id = int(normalized)
    except ValueError:
        return VMD_NAMED_COLOR_RGB.get(normalized, (0.5, 0.5, 0.5))
    return VMD_COLOR_RGB.get(color_id, (0.5, 0.5, 0.5))


def extract_style_visual_parameters(
    style: dict, rep0_commands: list[str] | None = None
) -> dict:
    """Return the VMD visual controls actually encoded by a style.

    Values left as ``None`` are deliberately inherited from the selected VMD
    material or display defaults rather than guessed.
    """

    commands = [str(command).strip() for command in style.get("commands", [])]
    rep_commands = [
        str(command).strip()
        for command in (rep0_commands or style.get("rep0_commands") or [])
    ]
    material = _allowed_material(str(style.get("material") or "Glossy"), "Glossy")
    pos_id = _color_id_from_expr(style.get("pos_color_expr"), style.get("pos_color", 1))
    neg_id = _color_id_from_expr(style.get("neg_color_expr"), style.get("neg_color", 0))

    rgb_changes: dict[str, tuple[float, float, float]] = {}
    background_token = ""
    carbon_token = ""
    projection: str | None = None
    depthcue: bool | None = None
    rendermode: str | None = None
    axes: str | None = None
    ambient_occlusion: bool | None = None
    shadows: bool | None = None
    antialias: bool | None = None
    lights: dict[str, bool | None] = {str(i): None for i in range(4)}
    material_values_by_name: dict[str, dict[str, float]] = {}

    for command in commands:
        parts = command.split()
        lowered = [part.lower() for part in parts]
        if len(parts) == 7 and lowered[:3] == ["color", "change", "rgb"]:
            try:
                rgb_changes[parts[3].lower()] = tuple(float(value) for value in parts[4:7])  # type: ignore[assignment]
            except (TypeError, ValueError):
                pass
            continue
        if len(parts) >= 4 and lowered[:3] == ["color", "display", "background"]:
            background_token = parts[3]
            continue
        if len(parts) >= 4 and lowered[:3] == ["color", "name", "c"]:
            carbon_token = parts[3]
            continue
        if len(parts) >= 3 and lowered[:2] == ["display", "projection"]:
            projection = parts[2]
            continue
        if len(parts) >= 3 and lowered[:2] == ["display", "depthcue"]:
            depthcue = lowered[2] == "on"
            continue
        if len(parts) >= 3 and lowered[:2] == ["display", "rendermode"]:
            rendermode = parts[2]
            continue
        if len(parts) >= 3 and lowered[:2] == ["display", "ambientocclusion"]:
            ambient_occlusion = lowered[2] == "on"
            continue
        if len(parts) >= 3 and lowered[:2] == ["display", "shadows"]:
            shadows = lowered[2] == "on"
            continue
        if len(parts) >= 3 and lowered[:2] == ["display", "antialias"]:
            antialias = lowered[2] == "on"
            continue
        if len(parts) >= 3 and lowered[:2] == ["axes", "location"]:
            axes = parts[2]
            continue
        if len(parts) == 3 and lowered[0] == "light" and parts[1] in lights:
            lights[parts[1]] = lowered[2] == "on"
            continue
        if (
            len(parts) == 5
            and lowered[:2] == ["material", "change"]
            and lowered[2] in MATERIAL_PARAMETER_NAMES
        ):
            try:
                value = float(parts[4])
            except ValueError:
                continue
            material_values_by_name.setdefault(parts[3], {})[lowered[2]] = value

    skeleton_style = "CPK"
    skeleton_style_command = "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000"
    skeleton_color_method = "Name"
    skeleton_material = "Opaque"
    for command in rep_commands:
        parts = command.split()
        lowered = [part.lower() for part in parts]
        if len(parts) >= 5 and lowered[:4] == ["mol", "modstyle", "0", "top"]:
            skeleton_style = parts[4]
            skeleton_style_command = command
        elif len(parts) >= 5 and lowered[:4] == ["mol", "modcolor", "0", "top"]:
            skeleton_color_method = " ".join(parts[4:])
        elif len(parts) >= 5 and lowered[:4] == ["mol", "modmaterial", "0", "top"]:
            skeleton_material = parts[4]

    pos_token = str(pos_id)
    neg_token = str(neg_id)
    background_token = background_token or "VMD 当前背景"
    carbon_token = carbon_token or "tan"
    return {
        "name": str(style.get("name") or "未命名风格"),
        "description": str(style.get("notes") or ""),
        "is_custom": bool(style.get("is_custom")),
        "material": material,
        "pos_color_id": pos_id,
        "neg_color_id": neg_id,
        "positive_rgb": _known_color_rgb(pos_token, rgb_changes),
        "negative_rgb": _known_color_rgb(neg_token, rgb_changes),
        "positive_rgb_explicit": pos_token in rgb_changes,
        "negative_rgb_explicit": neg_token in rgb_changes,
        "background_token": background_token,
        "background_rgb": _known_color_rgb(background_token, rgb_changes),
        "background_rgb_explicit": background_token.lower() in rgb_changes,
        "carbon_token": carbon_token,
        "skeleton_rgb": _known_color_rgb(carbon_token, rgb_changes),
        "skeleton_rgb_explicit": carbon_token.lower() in rgb_changes,
        "projection": projection,
        "depthcue": depthcue,
        "rendermode": rendermode,
        "axes": axes,
        "ambient_occlusion": ambient_occlusion,
        "shadows": shadows,
        "antialias": antialias,
        "lights": lights,
        "material_values": {
            parameter: material_values_by_name.get(material, {}).get(parameter)
            for parameter in MATERIAL_PARAMETER_NAMES
        },
        "material_values_by_name": material_values_by_name,
        "skeleton_style": skeleton_style,
        "skeleton_style_command": skeleton_style_command,
        "skeleton_color_method": skeleton_color_method,
        "skeleton_material": skeleton_material,
        "commands": commands,
        "rep0_commands": rep_commands,
        "sources": [str(source) for source in style.get("sources", [])],
    }


def _ai_rgb_schema(description: str) -> dict:
    return {
        "type": "array",
        "description": description,
        "items": {"type": "number"},
    }


AI_STYLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "style_name": {
            "type": "string",
            "description": "Short preset name inferred from the visible style, not from hidden data.",
        },
        "style_summary": {
            "type": "string",
            "description": "One concise sentence describing visible colors, material, skeleton, and background.",
        },
        "material": {
            "type": "string",
            "enum": VMD_MATERIALS,
            "description": "VMD material for the isosurface representation. Choose conservatively.",
        },
        "positive_color_rgb": _ai_rgb_schema(
            "Candidate positive isosurface color as normalized RGB floats. Use supplied measurements when available."
        ),
        "negative_color_rgb": _ai_rgb_schema(
            "Candidate negative isosurface color as normalized RGB floats. Use supplied measurements when available."
        ),
        "background_rgb": _ai_rgb_schema(
            "Display background color as normalized RGB floats. Use supplied measurements when available."
        ),
        "skeleton_color_rgb": _ai_rgb_schema(
            "Dominant carbon/skeleton color as normalized RGB floats. Use supplied measurements when available."
        ),
        "projection": {
            "type": "string",
            "enum": ["Orthographic", "Perspective"],
            "description": "VMD display projection. Default to Orthographic unless perspective distortion is clear.",
        },
        "depthcue": {
            "type": "boolean",
            "description": "True only when distance fog/fading is visibly present.",
        },
        "ambient": {"type": "number", "description": "VMD material ambient value, usually 0-1."},
        "diffuse": {"type": "number", "description": "VMD material diffuse value, usually 0-1."},
        "specular": {"type": "number", "description": "VMD material specular value, usually 0-1."},
        "shininess": {"type": "number", "description": "VMD material shininess value, usually 0-1."},
        "mirror": {"type": "number", "description": "VMD material mirror value, usually 0-1."},
        "opacity": {"type": "number", "description": "VMD material opacity value, usually 0-1."},
        "outline": {"type": "number", "description": "VMD material outline value, usually 0-4."},
        "outline_width": {"type": "number", "description": "VMD material outline width value, usually 0-1."},
        "lights": {
            "type": "object",
            "description": "VMD light 0-3 on/off states. Use defaults when exact light setup is not recoverable.",
            "additionalProperties": False,
            "properties": {
                "0": {"type": "boolean"},
                "1": {"type": "boolean"},
                "2": {"type": "boolean"},
                "3": {"type": "boolean"},
            },
            "required": ["0", "1", "2", "3"],
        },
        "skeleton_style": {
            "type": "string",
            "enum": ["CPK", "Licorice", "Bonds", "Lines", "Unknown"],
            "description": "Visible molecular skeleton drawing method, not the isosurface drawing method.",
        },
        "skeleton_material": {
            "type": "string",
            "enum": VMD_MATERIALS,
            "description": "VMD material for the skeleton representation.",
        },
        "confidence": {
            "type": "number",
            "description": "0-1 confidence for visible style only. Keep modest for screenshot-only inference.",
        },
        "uncertain_fields": {
            "type": "array",
            "description": "Fields that cannot be reliably recovered from the image, e.g. isovalue, lights, material.",
            "items": {"type": "string"},
        },
    },
    "required": [
        "style_name",
        "style_summary",
        "material",
        "positive_color_rgb",
        "negative_color_rgb",
        "background_rgb",
        "skeleton_color_rgb",
        "projection",
        "depthcue",
        "ambient",
        "diffuse",
        "specular",
        "shininess",
        "mirror",
        "opacity",
        "outline",
        "outline_width",
        "lights",
        "skeleton_style",
        "skeleton_material",
        "confidence",
        "uncertain_fields",
    ],
}

AI_STYLE_PROMPT = """\
Analyze this molecular visualization image and estimate a VMD style preset for this application.

Use the VMD model precisely:
- A molecular representation is controlled by selection, drawing method, coloring method, and material.
- This application saves one isosurface representation plus one molecular skeleton representation.
- Isosurfaces use VMD ColorID 12 and 22 for the two lobe colors.
- The skeleton uses Name coloring, with the carbon color stored as a custom tan RGB value.
- Output only visible rendering style. Do not infer hidden scientific data such as cube dataset,
  isovalue, orbital identity, basis set, volume source, or selection.

Field guidance:
- RGB values must be normalized floats from 0 to 1. If local pixel measurements are supplied, treat
  them as stronger evidence than your visual color impression.
- Positive/negative lobe assignment is usually not visible. Keep the measured candidate order unless
  labels or context make the sign obvious; otherwise include "positive_negative_assignment" in
  uncertain_fields.
- Choose Glass1, Glass2, Glass3, Transparent, Translucent, or EdgyGlass only when the isosurface is
  visibly see-through. Use lower opacity for glassy transparent lobes.
- Choose Glossy/Opaque when the isosurface looks solid with clear highlights.
- Default to Orthographic projection unless perspective distortion is clear.
- Set depthcue true only when distant parts visibly fade into fog.
- Exact light states and light positions are not reliably recoverable from screenshots; use the
  application default lights 0/1/2 on and 3 off unless there is strong evidence, and mark "lights"
  uncertain.
- Use CPK when atoms are spheres connected by bonds; use Licorice/Bonds/Lines only when that skeleton
  style is visibly present.
- Keep confidence modest for screenshot-only inference. Prefer uncertain_fields over confident guesses.

Return only the JSON object required by the schema.
"""


def _gemini_schema(schema):
    if isinstance(schema, dict):
        return {
            key: _gemini_schema(value)
            for key, value in schema.items()
            if key != "additionalProperties"
        }
    if isinstance(schema, list):
        return [_gemini_schema(value) for value in schema]
    return schema


def _dedupe_commands(commands: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for cmd in commands:
        c = (cmd or "").strip()
        if not c:
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def compose_combo_style(skeleton_style: dict, iso_style: dict) -> dict:
    combo_commands = _dedupe_commands(
        BASE_VIEW + skeleton_style.get("pre_commands", []) + iso_style.get("commands", [])
    )
    return {
        "id": f"combo_{skeleton_style['id']}__{iso_style['id']}",
        "name": f"{skeleton_style['name']} + {iso_style['name']}",
        "image": iso_style["image"],
        "material": iso_style["material"],
        "pos_color": iso_style["pos_color"],
        "neg_color": iso_style["neg_color"],
        "commands": combo_commands,
        "sources": sorted(
            set(skeleton_style.get("sources", []) + iso_style.get("sources", []))
        ),
        "notes": "Composed from skeleton + isosurface styles.",
        "rep0_commands": skeleton_style.get("rep0_commands", []),
        "bundle_id": iso_style["id"],
        "skeleton_id": skeleton_style["id"],
    }


def _clamp_number(value, default: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    if number < low:
        return low
    if number > high:
        return high
    return number


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return default


def _rgb_triplet(value, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return default
    nums = []
    for i in range(3):
        try:
            nums.append(float(value[i]))
        except Exception:
            nums.append(default[i])
    if any(v > 1.5 for v in nums):
        nums = [v / 255.0 for v in nums]
    return tuple(_clamp_number(v, default[i]) for i, v in enumerate(nums))


def _fmt_rgb(rgb: tuple[float, float, float]) -> str:
    return f"{rgb[0]:.6f} {rgb[1]:.6f} {rgb[2]:.6f}"


def _fmt_context_rgb(value, default: tuple[float, float, float]) -> str:
    rgb = _rgb_triplet(value, default)
    return f"[{rgb[0]:.3f}, {rgb[1]:.3f}, {rgb[2]:.3f}]"


def build_ai_image_context(
    measured_style: dict | None = None,
    style_name: str = "",
    source_hint: str = "",
) -> str:
    lines = [
        "Application save target:",
        "- One VMD isosurface style plus one molecular skeleton style will be generated.",
        "- Isosurface positive/negative colors are saved as ColorID 12 and ColorID 22.",
        "- Skeleton is saved with Name coloring and a custom carbon tan RGB color.",
        "- Hidden scientific parameters cannot be recovered from a screenshot; mark isovalue, dataset, orbital identity, and selection as uncertain.",
        "Default values when uncertain:",
        "- material=Glossy, projection=Orthographic, depthcue=false, lights={0:true,1:true,2:true,3:false}.",
        "- skeleton_style=CPK, skeleton_material=Glossy, opacity=1.0, specular=0.45, shininess=0.70.",
        "Material visual anchors:",
        "- Glass1-like transparent lobes: ambient about 0.30, diffuse about 0.60, specular about 0.50, shininess about 1.00, opacity about 0.60.",
        "- Glossy solid lobes: high opacity, moderate-to-strong highlights, no visible skeleton through the surface.",
        "- Edgy/EdgyGlass: visible dark outlines around surfaces.",
    ]
    style_name = (style_name or "").strip()
    if style_name:
        lines.append(f"User/current style name hint: {style_name}")
    source_hint = (source_hint or "").strip()
    if source_hint:
        lines.append(f"Image source hint: {source_hint}")

    if measured_style:
        lines.extend(
            [
                "Local pixel measurement report:",
                f"- positive_color_rgb candidate: {_fmt_context_rgb(measured_style.get('positive_color_rgb'), (0.48, 0.76, 0.42))}",
                f"- negative_color_rgb candidate: {_fmt_context_rgb(measured_style.get('negative_color_rgb'), (0.06, 0.72, 0.88))}",
                f"- skeleton_color_rgb candidate: {_fmt_context_rgb(measured_style.get('skeleton_color_rgb'), (0.70, 0.56, 0.36))}",
                f"- background_rgb candidate: {_fmt_context_rgb(measured_style.get('background_rgb'), (1.0, 1.0, 1.0))}",
                "- Use these measured RGB candidates in the JSON unless the crop contains clear non-rendering UI contamination.",
            ]
        )
    return "\n".join(lines)


def build_ai_style_prompt(image_context: str = "") -> str:
    context = str(image_context or "").strip()
    if not context:
        return AI_STYLE_PROMPT
    if len(context) > 3500:
        context = context[:3500].rsplit("\n", 1)[0] or context[:3500]
    return (
        f"{AI_STYLE_PROMPT}\n\n"
        "Additional application context and local measurements:\n"
        f"{context}\n\n"
        "Use the additional context as evidence, but keep uncertain_fields explicit when the "
        "image cannot determine a field."
    )


def _allowed_material(value: str, default: str = "Glossy") -> str:
    value = (value or "").strip()
    return value if value in VMD_MATERIALS else default


def normalize_ai_style_guess(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("AI 返回内容不是 JSON 对象。")

    material = _allowed_material(str(raw.get("material") or ""), "Glossy")
    projection = str(raw.get("projection") or "Orthographic").strip()
    if projection not in {"Orthographic", "Perspective"}:
        projection = "Orthographic"

    lights = raw.get("lights") if isinstance(raw.get("lights"), dict) else {}
    normalized_lights = {
        str(i): _coerce_bool(lights.get(str(i)), True if i < 3 else False)
        for i in range(4)
    }

    skeleton_style = str(raw.get("skeleton_style") or "CPK").strip()
    if skeleton_style not in {"CPK", "Licorice", "Bonds", "Lines", "Unknown"}:
        skeleton_style = "CPK"
    if skeleton_style == "Unknown":
        skeleton_style = "CPK"

    uncertain = raw.get("uncertain_fields")
    if not isinstance(uncertain, list):
        uncertain = []

    return {
        "style_name": str(raw.get("style_name") or "AI Image Style").strip()
        or "AI Image Style",
        "style_summary": str(raw.get("style_summary") or "").strip(),
        "material": material,
        "positive_color_rgb": _rgb_triplet(raw.get("positive_color_rgb"), (0.03, 0.74, 0.83)),
        "negative_color_rgb": _rgb_triplet(raw.get("negative_color_rgb"), (0.92, 0.77, 0.09)),
        "background_rgb": _rgb_triplet(raw.get("background_rgb"), (0.96, 0.96, 0.96)),
        "skeleton_color_rgb": _rgb_triplet(raw.get("skeleton_color_rgb"), (0.70, 0.56, 0.36)),
        "projection": projection,
        "depthcue": _coerce_bool(raw.get("depthcue"), False),
        "ambient": _clamp_number(raw.get("ambient"), 0.10),
        "diffuse": _clamp_number(raw.get("diffuse"), 0.85),
        "specular": _clamp_number(raw.get("specular"), 0.20),
        "shininess": _clamp_number(raw.get("shininess"), 0.35),
        "mirror": _clamp_number(raw.get("mirror"), 0.0),
        "opacity": _clamp_number(raw.get("opacity"), 1.0),
        "outline": _clamp_number(raw.get("outline"), 0.0, 0.0, 4.0),
        "outline_width": _clamp_number(raw.get("outline_width"), 0.0),
        "lights": normalized_lights,
        "skeleton_style": skeleton_style,
        "skeleton_material": _allowed_material(str(raw.get("skeleton_material") or ""), "Opaque"),
        "confidence": _clamp_number(raw.get("confidence"), 0.5),
        "uncertain_fields": [str(x) for x in uncertain if str(x).strip()],
    }


def build_custom_style_from_ai_guess(
    raw_guess: dict,
    name: str,
    description: str,
    image: str = "07_glossy_default.jpg",
    provider: str = "openai",
) -> dict:
    guess = normalize_ai_style_guess(raw_guess)
    style_name = (name or "").strip() or guess["style_name"]
    material = guess["material"]
    pos_id = 12
    neg_id = 22
    lights = guess["lights"]

    commands = _dedupe_commands(
        BASE_VIEW
        + [
            "color Display Background silver",
            f"color change rgb silver {_fmt_rgb(guess['background_rgb'])}",
            f"color change rgb {pos_id} {_fmt_rgb(guess['positive_color_rgb'])}",
            f"color change rgb {neg_id} {_fmt_rgb(guess['negative_color_rgb'])}",
            "color Name C tan",
            f"color change rgb tan {_fmt_rgb(guess['skeleton_color_rgb'])}",
            f"display projection {guess['projection']}",
            f"display depthcue {'on' if guess['depthcue'] else 'off'}",
            "display rendermode GLSL",
            "axes location Off",
            f"light 0 {'on' if lights['0'] else 'off'}",
            f"light 1 {'on' if lights['1'] else 'off'}",
            f"light 2 {'on' if lights['2'] else 'off'}",
            f"light 3 {'on' if lights['3'] else 'off'}",
            f"material change ambient {material} {guess['ambient']:.6f}",
            f"material change diffuse {material} {guess['diffuse']:.6f}",
            f"material change specular {material} {guess['specular']:.6f}",
            f"material change shininess {material} {guess['shininess']:.6f}",
            f"material change mirror {material} {guess['mirror']:.6f}",
            f"material change opacity {material} {guess['opacity']:.6f}",
            f"material change outline {material} {guess['outline']:.6f}",
            f"material change outlinewidth {material} {guess['outline_width']:.6f}",
        ]
    )

    skeleton_style = guess["skeleton_style"]
    if skeleton_style == "Licorice":
        rep_style = "Licorice 0.200000 12.000000 12.000000"
    elif skeleton_style == "Bonds":
        rep_style = "Bonds 0.300000 12.000000"
    elif skeleton_style == "Lines":
        rep_style = "Lines 1.000000"
    else:
        rep_style = "CPK 0.800000 0.300000 22.000000 22.000000"

    rep0_commands = [
        f"mol modstyle 0 top {rep_style}",
        "mol modcolor 0 top Name",
        f"mol modmaterial 0 top {guess['skeleton_material']}",
    ]

    notes = (description or "").strip()
    summary = guess.get("style_summary", "")
    if summary:
        notes = f"{notes} | {summary}" if notes else summary
    uncertain = ", ".join(guess["uncertain_fields"]) or "none"
    notes = (
        f"{notes} | " if notes else ""
    ) + f"AI confidence {guess['confidence']:.2f}; uncertain: {uncertain}"

    style_id_seed = json.dumps(
        {
            "name": style_name,
            "description": description,
            "guess": guess,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    style_id = "custom_ai_" + hashlib.md5(style_id_seed.encode("utf-8")).hexdigest()[:12]

    return {
        "id": style_id,
        "name": style_name,
        "image": image,
        "material": material,
        "pos_color": pos_id,
        "neg_color": neg_id,
        "pos_color_expr": f"ColorID {pos_id}",
        "neg_color_expr": f"ColorID {neg_id}",
        "commands": commands,
        "sources": [f"ai-image-recognition:{provider or 'unknown'}"],
        "notes": notes,
        "rep0_commands": rep0_commands,
        "ai_guess": guess,
        "is_custom": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def build_custom_style_from_visual_parameters(
    parameters: dict,
    base_style: dict,
    name: str,
    description: str,
) -> dict:
    """Create or update a custom style from the manual visual-parameter editor."""

    original = extract_style_visual_parameters(
        base_style, parameters.get("original_rep0_commands")
    )
    material = _allowed_material(str(parameters.get("material") or ""), "Glossy")
    pos_id = max(0, min(32, int(parameters.get("pos_color_id", 1))))
    neg_id = max(0, min(32, int(parameters.get("neg_color_id", 0))))
    color_tokens = {
        str(original["pos_color_id"]).lower(),
        str(original["neg_color_id"]).lower(),
        str(original["background_token"]).lower(),
        str(original["carbon_token"]).lower(),
        str(pos_id),
        str(neg_id),
    }
    materials_to_replace = {str(original["material"]), material}

    commands: list[str] = []
    for command in original["commands"]:
        parts = command.split()
        lowered = [part.lower() for part in parts]
        if len(parts) >= 4 and lowered[:3] in (
            ["color", "display", "background"],
            ["color", "name", "c"],
        ):
            continue
        if (
            len(parts) == 7
            and lowered[:3] == ["color", "change", "rgb"]
            and parts[3].lower() in color_tokens
        ):
            continue
        if len(parts) >= 2 and lowered[0] == "display" and lowered[1] in {
            "projection",
            "depthcue",
            "rendermode",
            "ambientocclusion",
            "shadows",
            "antialias",
        }:
            continue
        if len(parts) >= 2 and lowered[:2] == ["axes", "location"]:
            continue
        if len(parts) == 3 and lowered[0] == "light" and parts[1] in {"0", "1", "2", "3"}:
            continue
        if (
            len(parts) == 5
            and lowered[:2] == ["material", "change"]
            and lowered[2] in MATERIAL_PARAMETER_NAMES
            and parts[3] in materials_to_replace
        ):
            continue
        commands.append(command)

    background_token = str(parameters.get("background_token") or "").strip()
    if background_token and background_token != "VMD 当前背景":
        commands.append(f"color Display Background {background_token}")
    if parameters.get("background_rgb_explicit"):
        token = background_token if background_token and background_token != "VMD 当前背景" else "silver"
        if token == "VMD 当前背景":
            token = "silver"
        commands.append(f"color Display Background {token}")
        commands.append(
            f"color change rgb {token} {_fmt_rgb(_rgb_triplet(parameters.get('background_rgb'), (1.0, 1.0, 1.0)))}"
        )
    if parameters.get("positive_rgb_explicit"):
        commands.append(
            f"color change rgb {pos_id} {_fmt_rgb(_rgb_triplet(parameters.get('positive_rgb'), VMD_COLOR_RGB.get(pos_id, (0.5, 0.5, 0.5))))}"
        )
    if parameters.get("negative_rgb_explicit"):
        commands.append(
            f"color change rgb {neg_id} {_fmt_rgb(_rgb_triplet(parameters.get('negative_rgb'), VMD_COLOR_RGB.get(neg_id, (0.5, 0.5, 0.5))))}"
        )

    carbon_token = str(parameters.get("carbon_token") or "tan").strip() or "tan"
    commands.append(f"color Name C {carbon_token}")
    if parameters.get("skeleton_rgb_explicit"):
        commands.append(
            f"color change rgb {carbon_token} {_fmt_rgb(_rgb_triplet(parameters.get('skeleton_rgb'), VMD_NAMED_COLOR_RGB['tan']))}"
        )

    simple_display = {
        "projection": parameters.get("projection"),
        "rendermode": parameters.get("rendermode"),
    }
    for key, value in simple_display.items():
        if value:
            commands.append(f"display {key} {value}")
    for key in ("depthcue", "ambient_occlusion", "shadows", "antialias"):
        value = parameters.get(key)
        if value is not None:
            command_key = "ambientocclusion" if key == "ambient_occlusion" else key
            commands.append(f"display {command_key} {'on' if bool(value) else 'off'}")
    if parameters.get("axes"):
        commands.append(f"axes location {parameters['axes']}")

    lights = parameters.get("lights") if isinstance(parameters.get("lights"), dict) else {}
    for index in range(4):
        value = lights.get(str(index))
        if value is not None:
            commands.append(f"light {index} {'on' if bool(value) else 'off'}")

    material_values = (
        parameters.get("material_values")
        if isinstance(parameters.get("material_values"), dict)
        else {}
    )
    for parameter in MATERIAL_PARAMETER_NAMES:
        value = material_values.get(parameter)
        if value is not None:
            commands.append(f"material change {parameter} {material} {float(value):.6f}")

    skeleton_style = str(parameters.get("skeleton_style") or "CPK")
    if skeleton_style not in {"CPK", "Licorice", "Bonds", "Lines"}:
        skeleton_style = "CPK"
    if skeleton_style == original["skeleton_style"]:
        style_command = str(original["skeleton_style_command"])
    else:
        defaults = {
            "CPK": "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000",
            "Licorice": "mol modstyle 0 top Licorice 0.200000 12.000000 12.000000",
            "Bonds": "mol modstyle 0 top Bonds 0.300000 12.000000",
            "Lines": "mol modstyle 0 top Lines 1.000000",
        }
        style_command = defaults[skeleton_style]
    skeleton_material = _allowed_material(
        str(parameters.get("skeleton_material") or ""), "Opaque"
    )
    skeleton_color_method = str(parameters.get("skeleton_color_method") or "Name").strip() or "Name"
    rep0_commands = [
        style_command,
        f"mol modcolor 0 top {skeleton_color_method}",
        f"mol modmaterial 0 top {skeleton_material}",
    ]

    style_name = (name or "").strip() or f"{original['name']} 自定义"
    base_id = str(base_style.get("id") or "style")
    if base_style.get("is_custom"):
        style_id = base_id
        created_at = str(base_style.get("created_at") or datetime.now().isoformat(timespec="seconds"))
    else:
        seed = f"{base_id}|{style_name}|{datetime.now().isoformat(timespec='microseconds')}"
        style_id = "custom_manual_" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]
        created_at = datetime.now().isoformat(timespec="seconds")
    sources = list(dict.fromkeys(
        [str(source) for source in base_style.get("sources", [])]
        + [f"manual-style-editor:{base_id}"]
    ))
    return {
        "id": style_id,
        "name": style_name,
        "image": str(base_style.get("image") or "07_glossy_default.jpg"),
        "material": material,
        "pos_color": pos_id,
        "neg_color": neg_id,
        "pos_color_expr": f"ColorID {pos_id}",
        "neg_color_expr": f"ColorID {neg_id}",
        "commands": _dedupe_commands(commands),
        "sources": sources,
        "notes": (description or "").strip(),
        "rep0_commands": rep0_commands,
        "is_custom": True,
        "created_at": created_at,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _extract_response_text(payload: dict) -> str:
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text
    chunks: list[str] = []
    output = payload.get("output", [])
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        contents = item.get("content", [])
        if not isinstance(contents, list):
            continue
        for content in contents:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and isinstance(
                content.get("text"), str
            ):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


def _extract_gemini_response_text(payload: dict) -> str:
    chunks: list[str] = []
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        return ""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content", {})
        if not isinstance(content, dict):
            continue
        parts = content.get("parts", [])
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _parse_ai_json_text(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def _read_ai_image(image_path: Path | str) -> tuple[Path, bytes, str]:
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        raise ValueError("请先选择有效的图片文件。")

    raw = path.read_bytes()
    if len(raw) > 20_000_000:
        raise ValueError("图片过大，请先裁剪或压缩到 20MB 以内。")

    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError("图片格式不支持，请使用 PNG/JPG/WEBP/GIF。")
    return path, raw, mime


def _recognize_openai_style_from_image(
    image_path: Path | str,
    api_key: str = "",
    model: str = "",
    image_context: str = "",
) -> dict:
    _path, raw, mime = _read_ai_image(image_path)
    key = (api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ValueError("请填写 OpenAI API Key，或设置 OPENAI_API_KEY 环境变量。")

    model_name = (model or os.environ.get("OPENAI_VMD_STYLE_MODEL") or "gpt-4.1-mini").strip()
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    prompt = build_ai_style_prompt(image_context)
    body = {
        "model": model_name,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url, "detail": "high"},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "vmd_style_guess",
                "strict": True,
                "schema": AI_STYLE_SCHEMA,
            }
        },
    }

    request = urllib_request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=90) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"OpenAI API 请求失败（{exc.code}）：{detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"OpenAI API 连接失败：{exc.reason}") from exc

    text = _extract_response_text(response_payload)
    if not text:
        raise RuntimeError("OpenAI API 未返回可解析的结构化结果。")
    try:
        return normalize_ai_style_guess(_parse_ai_json_text(text))
    except Exception as exc:
        raise RuntimeError(f"AI 返回 JSON 解析失败：{exc}") from exc


def _recognize_gemini_style_from_image(
    image_path: Path | str,
    api_key: str = "",
    model: str = "",
    image_context: str = "",
) -> dict:
    _path, raw, mime = _read_ai_image(image_path)
    key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        raise ValueError("请填写 Gemini API Key，或设置 GEMINI_API_KEY 环境变量。")

    model_name = (
        model or os.environ.get("GEMINI_VMD_STYLE_MODEL") or "gemini-3.5-flash"
    ).strip()
    if model_name.startswith("models/"):
        model_name = model_name[len("models/") :]

    prompt = build_ai_style_prompt(image_context)
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime,
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(AI_STYLE_SCHEMA),
        },
    }

    request = urllib_request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-goog-api-key": key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=90) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Gemini API 请求失败（{exc.code}）：{detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Gemini API 连接失败：{exc.reason}") from exc

    text = _extract_gemini_response_text(response_payload)
    if not text:
        raise RuntimeError("Gemini API 未返回可解析的结构化结果。")
    try:
        return normalize_ai_style_guess(_parse_ai_json_text(text))
    except Exception as exc:
        raise RuntimeError(f"AI 返回 JSON 解析失败：{exc}") from exc


def recognize_ai_style_from_image(
    image_path: Path | str,
    api_key: str = "",
    model: str = "",
    provider: str = "openai",
    image_context: str = "",
) -> dict:
    provider_key = (provider or "openai").strip().lower()
    if provider_key == "gemini":
        return _recognize_gemini_style_from_image(
            image_path, api_key=api_key, model=model, image_context=image_context
        )
    if provider_key == "openai":
        return _recognize_openai_style_from_image(
            image_path, api_key=api_key, model=model, image_context=image_context
        )
    raise ValueError(f"未知 AI 提供商：{provider}")


def load_custom_styles(*, strict: bool = False) -> list[dict]:
    global CUSTOM_STYLES_LOAD_ERROR

    with _DATA_LOCK:
        if not CUSTOM_STYLES_FILE.exists():
            CUSTOM_STYLES_LOAD_ERROR = ""
            return []
        try:
            raw = json.loads(CUSTOM_STYLES_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("根节点必须是 JSON 数组")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            CUSTOM_STYLES_LOAD_ERROR = f"自定义风格文件读取失败：{exc}"
            if strict:
                raise ValueError(
                    f"{CUSTOM_STYLES_LOAD_ERROR}。为避免覆盖原数据，本次写入已取消。"
                ) from exc
            return []

        out: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if not str(entry.get("id") or "").strip() or not str(
                entry.get("name") or ""
            ).strip():
                continue
            out.append(entry)
        CUSTOM_STYLES_LOAD_ERROR = ""
        return out


def save_custom_styles(styles: list[dict]) -> None:
    if not isinstance(styles, list):
        raise TypeError("styles 必须是列表。")
    with _DATA_LOCK:
        write_text_atomic(
            CUSTOM_STYLES_FILE,
            json.dumps(styles, indent=2, ensure_ascii=False) + "\n",
        )


def upsert_custom_style(style: dict) -> None:
    if not isinstance(style, dict) or not str(style.get("id") or "").strip():
        raise ValueError("自定义风格缺少有效 id。")
    with _DATA_LOCK:
        custom_styles = load_custom_styles(strict=True)
        custom_styles = [x for x in custom_styles if x.get("id") != style["id"]]
        custom_styles.append(style)
        save_custom_styles(custom_styles)


def delete_custom_style(style_id: str) -> dict | None:
    style_id = (style_id or "").strip()
    if not style_id:
        return None

    with _DATA_LOCK:
        custom_styles = load_custom_styles(strict=True)
        removed = None
        remaining = []
        for style in custom_styles:
            if str(style.get("id", "")) == style_id:
                removed = style
            else:
                remaining.append(style)

        if removed is None:
            return None

        save_custom_styles(remaining)
        _delete_custom_cover_if_owned(removed, remaining)
        return removed


def _delete_custom_cover_if_owned(style: dict, remaining_styles: list[dict]) -> None:
    image = str(style.get("image") or "").strip()
    style_id = str(style.get("id") or "").strip()
    if not image or not style_id:
        return
    if any(str(s.get("image") or "").strip() == image for s in remaining_styles):
        return

    target = (STYLE_DIR / image).resolve()
    try:
        target.relative_to(STYLE_DIR.resolve())
    except Exception:
        return
    if not target.name.startswith(f"{style_id}_"):
        return
    try:
        if target.exists() and target.is_file():
            target.unlink()
    except Exception:
        pass


def get_all_bundle_styles() -> list[dict]:
    return STYLES + load_custom_styles()


def get_bundle_style_map() -> dict[str, dict]:
    return {s["id"]: s for s in get_all_bundle_styles()}


SAFE_VMD_COMMAND_PREFIXES = (
    "axes ",
    "color Display ",
    "color Name ",
    "color change rgb ",
    "color scale ",
    "display ",
    "label textsize",
    "light ",
    "material add ",
    "material change ",
)
_UNSAFE_TCL_CHARS = re.compile(r"[\r\n;\[\]$\\]")
_SAFE_TCL_TOKEN = re.compile(r"^[A-Za-z0-9_.:+-]+$")


def _is_safe_tcl_fragment(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text) and not _UNSAFE_TCL_CHARS.search(text)


def _safe_tcl_token(value: object, default: str) -> str:
    text = str(value or "").strip()
    return text if _SAFE_TCL_TOKEN.fullmatch(text) else default


def _sanitize_vmd_commands(commands) -> list[str]:
    """Keep only declarative VMD style commands and reject Tcl control syntax."""
    out: list[str] = []
    for raw in commands if isinstance(commands, (list, tuple)) else []:
        for line in str(raw or "").splitlines():
            command = line.strip()
            if not command.startswith(SAFE_VMD_COMMAND_PREFIXES):
                continue
            if not _is_safe_tcl_fragment(command):
                continue
            out.append(command)
    return _dedupe_commands(out)


def _sanitize_rep0_commands(commands) -> list[str]:
    allowed = ("mol modstyle 0 top ", "mol modcolor 0 top ", "mol modmaterial 0 top ")
    out: list[str] = []
    for raw in commands if isinstance(commands, (list, tuple)) else []:
        command = str(raw or "").strip()
        if command.startswith(allowed) and _is_safe_tcl_fragment(command):
            out.append(command)
    return _dedupe_commands(out)


def _extract_proc_block(lines: list[str], proc_name: str) -> tuple[list[str], set[int]]:
    start = None
    pat = re.compile(rf"^\s*proc\s+{re.escape(proc_name)}\s+\{{\}}\s+\{{")
    for i, line in enumerate(lines):
        if pat.match(line):
            start = i
            break
    if start is None:
        return [], set()

    block: list[str] = []
    used: set[int] = set()
    depth = 0
    for j in range(start, len(lines)):
        line = lines[j]
        block.append(line.rstrip("\n\r"))
        used.add(j)
        depth += line.count("{") - line.count("}")
        if j > start and depth <= 0:
            break
    return block, used


def _parse_vmd_rep_blocks(lines: list[str]) -> list[dict]:
    reps: list[dict] = []
    current = None
    for raw in lines:
        s = raw.strip()
        if s.startswith("mol representation "):
            current = {
                "repr": s[len("mol representation ") :].strip(),
                "color": "",
                "material": "",
            }
            continue
        if current is None:
            continue
        if s.startswith("mol color "):
            current["color"] = s[len("mol color ") :].strip()
            continue
        if s.startswith("mol material "):
            current["material"] = s[len("mol material ") :].strip()
            continue
        if s.startswith("mol addrep "):
            reps.append(current)
            current = None
            continue
    return reps


def _safe_float(text: str) -> float | None:
    try:
        return float(text)
    except Exception:
        return None


def _color_expr_to_id(expr: str, fallback: int) -> int:
    m = re.match(r"^\s*ColorID\s+(-?\d+)\s*$", expr or "")
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return fallback


def parse_save_state_to_custom_style(
    state_text: str, name: str, description: str
) -> dict:
    lines = state_text.splitlines()
    if len(lines) < 5:
        raise ValueError("save state 文件内容过短。")
    if "save_state" not in state_text and "VMD script written by" not in state_text:
        raise ValueError("文件看起来不像 VMD Save State 导出的状态脚本。")

    mat_proc, mat_idx = _extract_proc_block(lines, "vmdrestoremymaterials")
    col_proc, col_idx = _extract_proc_block(lines, "vmdrestoremycolors")
    skip_idx = set(mat_idx) | set(col_idx)

    # Save State restore procedures contain Tcl control flow. Extract only their
    # declarative style commands so imported files cannot inject arbitrary Tcl.
    global_cmds: list[str] = _sanitize_vmd_commands(mat_proc + col_proc)
    for i, raw in enumerate(lines):
        if i in skip_idx:
            continue
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith(SAFE_VMD_COMMAND_PREFIXES):
            global_cmds.append(s)
    global_cmds = _sanitize_vmd_commands(global_cmds)

    reps = [
        rep
        for rep in _parse_vmd_rep_blocks(lines)
        if _is_safe_tcl_fragment(rep.get("repr"))
        and (not rep.get("color") or _is_safe_tcl_fragment(rep.get("color")))
        and (not rep.get("material") or _is_safe_tcl_fragment(rep.get("material")))
    ]
    if not reps:
        raise ValueError("未找到可解析的 mol representation 样式块。")

    skeleton_rep = None
    for rep in reps:
        if not rep["repr"].startswith("Isosurface"):
            skeleton_rep = rep
            break
    if skeleton_rep is None:
        skeleton_rep = reps[0]

    rep0_commands = [f"mol modstyle 0 top {skeleton_rep['repr']}"]
    if skeleton_rep.get("color"):
        rep0_commands.append(f"mol modcolor 0 top {skeleton_rep['color']}")
    else:
        rep0_commands.append("mol modcolor 0 top Name")
    if skeleton_rep.get("material"):
        rep0_commands.append(f"mol modmaterial 0 top {skeleton_rep['material']}")
    else:
        rep0_commands.append("mol modmaterial 0 top Opaque")

    iso_reps = [rep for rep in reps if rep["repr"].startswith("Isosurface")]
    pos_rep = None
    neg_rep = None
    for rep in iso_reps:
        toks = rep["repr"].split()
        val = _safe_float(toks[1]) if len(toks) > 1 else None
        if val is not None and val > 0 and pos_rep is None:
            pos_rep = rep
        if val is not None and val < 0 and neg_rep is None:
            neg_rep = rep
    if pos_rep is None and iso_reps:
        pos_rep = iso_reps[0]
    if neg_rep is None and len(iso_reps) > 1:
        neg_rep = iso_reps[1]
    if neg_rep is None:
        neg_rep = pos_rep

    iso_material = "Glossy"
    if pos_rep and pos_rep.get("material"):
        iso_material = pos_rep["material"]
    elif neg_rep and neg_rep.get("material"):
        iso_material = neg_rep["material"]

    pos_color_expr = "ColorID 1"
    neg_color_expr = "ColorID 0"
    if pos_rep and pos_rep.get("color"):
        pos_color_expr = pos_rep["color"]
    if neg_rep and neg_rep.get("color"):
        neg_color_expr = neg_rep["color"]

    pos_color = _color_expr_to_id(pos_color_expr, 1)
    neg_color = _color_expr_to_id(neg_color_expr, 0)

    style_id_seed = f"{name}|{description}|{len(state_text)}|{hashlib.md5(state_text.encode('utf-8', 'ignore')).hexdigest()}"
    style_id = "custom_" + hashlib.md5(style_id_seed.encode("utf-8")).hexdigest()[:12]

    return {
        "id": style_id,
        "name": name.strip() or style_id,
        "image": "07_glossy_default.jpg",
        "material": iso_material,
        "pos_color": pos_color,
        "neg_color": neg_color,
        "pos_color_expr": pos_color_expr,
        "neg_color_expr": neg_color_expr,
        "commands": global_cmds,
        "sources": ["user-upload:save_state"],
        "notes": description.strip(),
        "rep0_commands": rep0_commands,
        "is_custom": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

def _walk_with_depth(root: Path, max_depth: int = 5):
    root = root.resolve()
    for current, dirs, files in os.walk(root):
        rel = Path(current).resolve().relative_to(root)
        if len(rel.parts) >= max_depth:
            dirs[:] = []
        yield Path(current), files


def _multiwfn_candidate_key(value: str) -> tuple[tuple[int, int, int], str]:
    match = re.search(
        r"Multiwfn_(\d{4})\.(\d+)\.(\d+)_bin_Win64",
        str(value),
        re.IGNORECASE,
    )
    version = tuple(int(part) for part in match.groups()) if match else (0, 0, 0)
    return version, str(value).casefold()


def find_path_candidates() -> dict:
    roots: list[Path] = []
    current = ROOT.resolve()
    for _ in range(3):
        # Never recursively scan an entire drive; source and packaged layouts
        # place the bundled tools within at most two non-drive ancestors.
        if current.parent == current:
            break
        roots.append(current)
        current = current.parent

    multi = set()
    vmd = set()

    for search_root in roots:
        for base, files in _walk_with_depth(search_root, max_depth=4):
            lower = {f.lower(): f for f in files}
            if "multiwfn.exe" in lower:
                multi.add(str((base / lower["multiwfn.exe"]).resolve()))
            if "vmd.exe" in lower:
                vmd.add(str((base / lower["vmd.exe"]).resolve()))

    return {
        "multiwfn": sorted(multi, key=_multiwfn_candidate_key, reverse=True),
        "vmd": sorted(vmd),
    }


def load_config() -> dict:
    defaults = {
        "multiwfn_exe": "",
        "vmd_exe": "",
        "output_name": "AutoCube_OneClick_custom.cmd",
        "output_dir": str(ROOT),
        "mode": "bundle",
        "theme": "light",
        "last_style": STYLES[0]["id"] if STYLES else "",
        "last_skeleton": SKELETON_STYLES[0]["id"] if SKELETON_STYLES else "",
        "last_iso_style": STYLES[0]["id"] if STYLES else "",
        "batch_output_dir": str(ROOT / "batch_runs"),
        "batch_last_preset": "builtin_export_xyz",
    }
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            defaults.update({k: saved.get(k, defaults[k]) for k in defaults})
        except Exception:
            pass

    if not defaults["multiwfn_exe"] or not defaults["vmd_exe"]:
        found = find_path_candidates()
        if not defaults["multiwfn_exe"] and found["multiwfn"]:
            defaults["multiwfn_exe"] = found["multiwfn"][0]
        if not defaults["vmd_exe"] and found["vmd"]:
            defaults["vmd_exe"] = found["vmd"][0]
    return defaults


def save_config(config: dict) -> None:
    with _DATA_LOCK:
        existing: dict = {}
        if CONFIG_FILE.exists():
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass

        def value(key: str, default):
            return config[key] if key in config else existing.get(key, default)

        payload = {
            "multiwfn_exe": value("multiwfn_exe", ""),
            "vmd_exe": value("vmd_exe", ""),
            "output_name": value("output_name", "AutoCube_OneClick_custom.cmd"),
            "output_dir": value("output_dir", str(ROOT)),
            "mode": value("mode", "bundle"),
            "theme": value("theme", "light"),
            "last_style": value("last_style", ""),
            "last_skeleton": value("last_skeleton", ""),
            "last_iso_style": value("last_iso_style", ""),
            "batch_output_dir": value("batch_output_dir", str(ROOT / "batch_runs")),
            "batch_last_preset": value("batch_last_preset", "builtin_export_xyz"),
        }
        write_text_atomic(
            CONFIG_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )


def _sanitize_output_name(name: str, style_id: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        candidate = f"AutoCube_OneClick_{style_id}.cmd"
    candidate = re.sub(r"[\x00-\x1f<>:\\|?*\"]", "_", candidate)
    candidate = candidate.replace("/", "_")
    candidate = candidate.strip(" .")
    if candidate.lower().endswith(".cmd"):
        stem = candidate[:-4].rstrip(" .")
    else:
        stem = candidate.rstrip(" .")
    if not stem:
        stem = f"AutoCube_OneClick_{style_id or 'style'}"
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", stem):
        stem = f"_{stem}"
    stem = stem[:120].rstrip(" .") or "AutoCube_OneClick_style"
    return f"{stem}.cmd"


def _clean_executable_path(value: str, label: str) -> str:
    path = str(value or "").strip()
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1].strip()
    if not path or '"' in path or "\r" in path or "\n" in path:
        raise ValueError(f"{label} 路径无效。")
    return path


def _escape_batch_value(value: str) -> str:
    # Delayed expansion is disabled in generated scripts, so ! stays literal.
    return value.replace("^", "^^").replace("%", "%%")


def build_vmd_tcl(
    style: dict, rep0_commands: list[str] | None = None
) -> str:
    """Build the VMD drawing workflow shared by direct runs and CMD exports."""
    rep0_commands = _sanitize_rep0_commands(rep0_commands or [])
    if not rep0_commands:
        rep0_commands = [
            "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000",
            "mol modcolor 0 top Name",
            "mol modmaterial 0 top Opaque",
        ]

    pos_default = f"ColorID {int(style.get('pos_color', 1))}"
    neg_default = f"ColorID {int(style.get('neg_color', 0))}"
    pos_candidate = str(style.get("pos_color_expr") or pos_default).strip()
    neg_candidate = str(style.get("neg_color_expr") or neg_default).strip()
    pos_color_expr = pos_candidate if _is_safe_tcl_fragment(pos_candidate) else pos_default
    neg_color_expr = neg_candidate if _is_safe_tcl_fragment(neg_candidate) else neg_default
    material = _safe_tcl_token(style.get("material"), "Glossy")
    style_commands = _sanitize_vmd_commands(style.get("commands", []))

    lines: list[str] = []
    a = lines.append
    a("# Auto-generated single-file AutoCube workflow")
    a("set AUTO_CUBE_FILE [file normalize $::env(CUBE_FILE)]")
    a("set AUTO_ISOVAL [expr {abs(double($::env(ISO_NORM)))}]")
    a("set AUTO_OUTDIR [file normalize $::env(A_DIR)]")
    a("set AUTO_BASENAME [file rootname [file tail $AUTO_CUBE_FILE]]")
    a("")
    a("proc _autocube_unique_path {target} {")
    a("    set candidate $target")
    a("    if {![file exists $candidate]} {")
    a("        return $candidate")
    a("    }")
    a("    set ext [file extension $target]")
    a("    set root [file rootname $target]")
    a('    if {$ext eq ""} {')
    a("        set root $target")
    a("    }")
    a("    set i 1")
    a("    while {[file exists $candidate]} {")
    a('        set candidate "${root}_$i$ext"')
    a("        incr i")
    a("    }")
    a("    return $candidate")
    a("}")
    a("")
    a("if {[llength [info commands _autocube_builtin_render]] == 0} {")
    a("    rename render _autocube_builtin_render")
    a("    proc render {args} {")
    a("        global AUTO_OUTDIR AUTO_BASENAME")
    a("        set passthrough [list list hasaa aasamples aosamples formats format options default]")
    a("        if {[llength $args] == 0} {")
    a("            return [uplevel 1 [list _autocube_builtin_render]]")
    a("        }")
    a("        set cmd0 [lindex $args 0]")
    a("        if {[lsearch -exact $passthrough $cmd0] >= 0} {")
    a("            return [uplevel 1 [list _autocube_builtin_render {*}$args]]")
    a("        }")
    a("        if {[llength $args] < 2} {")
    a("            return [uplevel 1 [list _autocube_builtin_render {*}$args]]")
    a("        }")
    a("")
    a("        set method [lindex $args 0]")
    a("        set requested [lindex $args 1]")
    a('        if {$requested eq ""} {')
    a('            set requested "${AUTO_BASENAME}_render"')
    a("        }")
    a("")
    a("        set filenameOnly [file tail $requested]")
    a('        if {$filenameOnly eq ""} {')
    a('            set filenameOnly "${AUTO_BASENAME}_render"')
    a("        }")
    a("")
    a("        set target [file normalize [file join $AUTO_OUTDIR $filenameOnly]]")
    a("        set target [_autocube_unique_path $target]")
    a("")
    a("        set newargs [list $method $target]")
    a("        if {[llength $args] > 2} {")
    a("            set newargs [concat $newargs [lrange $args 2 end]]")
    a("        }")
    a("")
    a("        set code [catch {uplevel 1 [list _autocube_builtin_render {*}$newargs]} msg opts]")
    a("        if {$code != 0} {")
    a("            return -options $opts $msg")
    a("        }")
    a("")
    a('        puts "AutoCube: Render output saved to $target"')
    a("        foreach i [molinfo list] {")
    a("            mol delete $i")
    a("        }")
    a('        puts "AutoCube: Deleted current molecule and isosurfaces in VMD."')
    a("        return $msg")
    a("    }")
    a("}")
    a("")
    style_name = str(style.get("name") or "Style").replace("\r", " ").replace("\n", " ")
    a(f"# Style: {style_name}")
    for source in style.get("sources", []):
        source_text = str(source).replace("\r", " ").replace("\n", " ")
        a(f"# Source: {source_text}")
    a(f"set mater {material}")
    lines.extend(style_commands)
    a("")
    a("foreach i [molinfo list] {")
    a("    mol delete $i")
    a("}")
    a("")
    a("mol new $AUTO_CUBE_FILE type cube waitfor all")
    lines.extend(rep0_commands)
    a("mol addrep top")
    a("mol modstyle 1 top Isosurface $AUTO_ISOVAL 0 0 0 1 1")
    a(f"mol modcolor 1 top {pos_color_expr}")
    a("mol modmaterial 1 top $mater")
    a("mol addrep top")
    a("set negiso [expr {-$AUTO_ISOVAL}]")
    a("mol modstyle 2 top Isosurface $negiso 0 0 0 1 1")
    a(f"mol modcolor 2 top {neg_color_expr}")
    a("mol modmaterial 2 top $mater")
    a("display distance -8.0")
    a("display height 10")
    a("")
    a("menu main on")
    a("menu graphics on")
    a("menu render on")
    a("")
    a('puts "AutoCube: Isosurface drawing is ready; not rendered automatically."')
    a('puts "AutoCube: Render manually in VMD. Output will be forced into: $AUTO_OUTDIR"')
    a('puts "AutoCube: After each successful render, current molecule and surfaces are deleted."')
    return "\n".join(lines) + "\n"


def build_cmd_script(
    style: dict, multiwfn_exe: str, vmd_exe: str, rep0_commands: list[str] | None = None
) -> str:
    multiwfn_exe = _clean_executable_path(multiwfn_exe, "Multiwfn")
    vmd_exe = _clean_executable_path(vmd_exe, "VMD")

    lines: list[str] = []
    a = lines.append

    a("@echo off")
    a("setlocal DisableDelayedExpansion")
    a("chcp 65001 >nul")
    a("")
    a("set \"A_DIR=%CD%\"")
    a("if \"%A_DIR:~-1%\"==\"\\\" set \"A_DIR=%A_DIR:~0,-1%\"")
    a("cd /d \"%A_DIR%\"")
    a("")
    a('echo [INFO] A folder: "%A_DIR%"')
    a("echo.")
    a("")
    a("rem ===== Auto-generated software paths =====")
    a(f"set \"MULTIWFN_EXE={_escape_batch_value(multiwfn_exe)}\"")
    a(f"set \"VMD_EXE={_escape_batch_value(vmd_exe)}\"")
    a("rem ========================================")
    a("")
    a("if not exist \"%MULTIWFN_EXE%\" (")
    a('  echo [ERROR] Multiwfn path is invalid: "%MULTIWFN_EXE%"')
    a("  pause")
    a("  exit /b 1")
    a(")")
    a("")
    a("if not exist \"%VMD_EXE%\" (")
    a('  echo [ERROR] VMD path is invalid: "%VMD_EXE%"')
    a("  pause")
    a("  exit /b 1")
    a(")")
    a("")
    a('echo [INFO] Multiwfn: "%MULTIWFN_EXE%"')
    a('echo [INFO] VMD: "%VMD_EXE%"')
    a("for %%D in (\"%MULTIWFN_EXE%\") do set \"MULTIWFN_DIR=%%~dpD\"")
    a("if \"%MULTIWFN_DIR:~-1%\"==\"\\\" set \"MULTIWFN_DIR=%MULTIWFN_DIR:~0,-1%\"")
    a("set \"Multiwfnpath=%MULTIWFN_DIR%\"")
    a('echo [INFO] Multiwfnpath: "%Multiwfnpath%"')
    a("echo.")
    a("echo [INFO] Launching Multiwfn...")
    a("echo [INFO] Generate ONE .cub file in this A folder, then exit Multiwfn.")
    a('set "RUN_MARKER=%TEMP%\\autocube_marker_%RANDOM%%RANDOM%%RANDOM%.tmp"')
    a('type nul > "%RUN_MARKER%"')
    a("start \"\" /wait \"%MULTIWFN_EXE%\"")
    a("")
    a("set \"CUBE_FILE=\"")
    # Avoid PowerShell pipelines here: cmd.exe parses a FOR /F backquoted command
    # once more, which can either expose an unescaped pipe to cmd or pass a literal
    # caret through to PowerShell.  A small loop is unambiguous in both parsers.
    a('for /f "usebackq delims=" %%F in (`powershell -NoLogo -NoProfile -Command "$marker=(Get-Item -LiteralPath $env:RUN_MARKER).LastWriteTimeUtc; $latest=$null; $latestStamp=[datetime]::MinValue; foreach ($file in Get-ChildItem -LiteralPath $env:A_DIR -Filter \'*.cub\' -File) { if ($file.LastWriteTimeUtc -ge $marker -and $file.LastWriteTimeUtc -gt $latestStamp) { $latest=$file.FullName; $latestStamp=$file.LastWriteTimeUtc } }; if ($null -ne $latest) { $latest }"`) do set "CUBE_FILE=%%F"')
    a('del /q "%RUN_MARKER%" >nul 2>nul')
    a("")
    a("if not defined CUBE_FILE (")
    a('  echo [ERROR] No new or updated .cub file was found in: "%A_DIR%"')
    a("  pause")
    a("  exit /b 1")
    a(")")
    a("")
    a("for %%B in (\"%CUBE_FILE%\") do set \"CUBE_BASE=%%~nB\"")
    a('echo [INFO] Using cube file: "%CUBE_FILE%"')
    a("")
    a(":ask_iso")
    a("set \"ISO_RAW=\"")
    a("set \"ISO_NORM=\"")
    a("set /p ISO_RAW=Enter isovalue (positive number, e.g. 0.05): ")
    a("if not defined ISO_RAW goto ask_iso")
    a("")
    a("for /f \"usebackq delims=\" %%I in (`powershell -NoLogo -NoProfile -Command \"$v=0.0; $raw=$env:ISO_RAW; $ok=[double]::TryParse($raw,[Globalization.NumberStyles]::Float,[Globalization.CultureInfo]::InvariantCulture,[ref]$v); if(-not $ok){$ok=[double]::TryParse($raw,[ref]$v)}; if($ok){$v=[Math]::Abs($v); if($v -gt 0){$v.ToString('0.############',[Globalization.CultureInfo]::InvariantCulture)}}\"`) do set \"ISO_NORM=%%I\"")
    a("")
    a("if not defined ISO_NORM (")
    a("  echo [WARN] Invalid number. Try again.")
    a("  goto ask_iso")
    a(")")
    a("")
    a("set \"TCL_FILE=%TEMP%\\autocube_%RANDOM%%RANDOM%%RANDOM%.tcl\"")

    def _escape_batch_echo(text: str) -> str:
        return (
            str(text).replace("\r", " ").replace("\n", " ")
            .replace("^", "^^")
            .replace("%", "%%")
            .replace("&", "^&")
            .replace("|", "^|")
            .replace("<", "^<")
            .replace(">", "^>")
            .replace("(", "^(")
            .replace(")", "^)")
        )

    def _echo_tcl(text: str) -> None:
        if text == "":
            a("  echo.")
            return
        a(f"  echo {_escape_batch_echo(text)}")

    a('> "%TCL_FILE%" (')
    for tcl_line in build_vmd_tcl(style, rep0_commands=rep0_commands).splitlines():
        _echo_tcl(tcl_line)
    a(")")
    a('')
    a('if not exist "%TCL_FILE%" (')
    a('  echo [ERROR] Failed to generate temporary VMD Tcl script.')
    a('  pause')
    a('  exit /b 1')
    a(')')
    a('')
    a('echo [INFO] Launching VMD and loading drawing script...')
    a('start "" /wait "%VMD_EXE%" -e "%TCL_FILE%"')
    a('set "VMD_EXIT=%ERRORLEVEL%"')
    a('')
    a('del /q "%TCL_FILE%" >nul 2>nul')
    a('if not "%VMD_EXIT%"=="0" (')
    a('  echo [ERROR] VMD exited with code %VMD_EXIT%.')
    a('  echo [INFO] Generated data files were preserved for retry.')
    a('  pause')
    a('  exit /b %VMD_EXIT%')
    a(')')
    a('echo [INFO] Generated .cub/.dat files were preserved in the A folder.')
    a('echo [INFO] Workflow finished.')
    a('pause')
    a('exit /b 0')
    a('')

    return "\r\n".join(lines) + "\r\n"

def style_payload(style: dict) -> dict:
    return {
        "id": style["id"],
        "name": style["name"],
        "material": style["material"],
        "pos_color": style["pos_color"],
        "neg_color": style["neg_color"],
        "pos_color_expr": style.get("pos_color_expr", f"ColorID {style['pos_color']}"),
        "neg_color_expr": style.get("neg_color_expr", f"ColorID {style['neg_color']}"),
        "image": style["image"],
        "image_url": f"/img/{style['image']}",
        "sources": style["sources"],
        "notes": style.get("notes", ""),
        "aliases": style.get("alias_ids", []),
        "is_custom": bool(style.get("is_custom", False)),
    }


def skeleton_payload(style: dict) -> dict:
    return {
        "id": style["id"],
        "name": style["name"],
        "image": style["image"],
        "image_url": f"/img/{style['image']}",
        "sources": style["sources"],
        "notes": style.get("notes", ""),
    }


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>VMD 风格脚本生成器</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --ink: #13233a;
      --muted: #5f7086;
      --line: #d7e1ee;
      --accent: #1273c7;
      --accent2: #14a780;
      --accent-soft: #e9f3ff;
      --shadow: 0 10px 28px rgba(19, 44, 74, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1000px 500px at -10% -20%, #e6f3ff 0%, transparent 60%),
        radial-gradient(900px 520px at 110% -10%, #e8fff6 0%, transparent 58%),
        var(--bg);
    }
    .wrap { max-width: 1360px; margin: 0 auto; padding: 20px; }
    .hero {
      background: linear-gradient(118deg, #0f4f81 0%, #1273c7 56%, #14a780 100%);
      color: #fff;
      border-radius: 18px;
      padding: 20px 22px;
      box-shadow: 0 14px 34px rgba(10, 54, 95, 0.22);
    }
    .hero h1 { margin: 0 0 8px 0; font-size: 1.36rem; letter-spacing: .2px; }
    .hero p { margin: 0; opacity: .94; }
    .grid {
      margin-top: 16px;
      display: grid;
      grid-template-columns: 380px 1fr;
      gap: 14px;
      align-items: start;
    }
    .panel {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      box-shadow: var(--shadow);
    }
    .panel h2 { margin: 0 0 10px 0; font-size: 1.03rem; }
    .panel h3 { margin: 12px 0 8px 0; font-size: .95rem; color: #294565; }
    .row { margin-bottom: 10px; }
    label { display: block; margin-bottom: 4px; font-size: .9rem; color: var(--muted); }
    input[type="text"], textarea, input[type="file"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 10px;
      font-size: .9rem;
      background: #fff;
      color: #223650;
      outline: none;
    }
    input[type="text"]:focus, textarea:focus, input[type="file"]:focus {
      border-color: #9bc7ef;
      box-shadow: 0 0 0 3px rgba(18, 115, 199, 0.08);
    }
    .mode-switch {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #f8fbff;
    }
    .mode-btn {
      border: 0;
      border-radius: 9px;
      padding: 9px 10px;
      cursor: pointer;
      font-weight: 700;
      font-size: .9rem;
      color: #33506f;
      background: transparent;
      transition: all .14s ease;
    }
    .mode-btn.active {
      color: #083968;
      background: var(--accent-soft);
      box-shadow: inset 0 0 0 1px #b8d8f5;
    }
    .btnbar { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    button.action {
      border: 0;
      border-radius: 10px;
      padding: 9px 12px;
      font-weight: 700;
      cursor: pointer;
      color: #fff;
      background: var(--accent);
    }
    button.action.ghost { background: #556b84; }
    button.action.success { background: var(--accent2); }
    .small { font-size: .85rem; color: var(--muted); line-height: 1.45; }
    .warn {
      margin-top: 8px;
      color: #8a5b00;
      background: #fff7df;
      border: 1px solid #f1df9f;
      border-radius: 9px;
      padding: 8px;
    }
    .subtle { color: #5f7086; font-size: .84rem; margin-bottom: 6px; }
    .card-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 12px;
    }
    .split-row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 14px;
    }
    .card {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      cursor: pointer;
      transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
    }
    .card:hover { transform: translateY(-2px); box-shadow: 0 10px 18px rgba(20, 49, 84, 0.12); }
    .card.sel { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(18, 115, 199, 0.14); }
    .card img { width: 100%; height: 148px; object-fit: cover; background: #d9e8f7; }
    .meta { padding: 9px; }
    .meta h3 { margin: 0 0 6px 0; font-size: .94rem; color: #1b3553; }
    .meta .m { margin: 0 0 7px 0; min-height: 32px; color: var(--muted); font-size: .82rem; }
    .tags { display: flex; flex-wrap: wrap; gap: 6px; }
    .tag { font-size: .74rem; color: #35516e; background: #eaf3fc; border-radius: 999px; padding: 3px 8px; }
    .card-actions { display: flex; justify-content: flex-end; margin-top: 8px; }
    button.mini-danger {
      border: 1px solid #f0b8b1;
      border-radius: 9px;
      padding: 5px 9px;
      font-weight: 700;
      cursor: pointer;
      color: #b42318;
      background: #fff5f4;
    }
    button.mini-danger:hover { background: #ffe7e4; border-color: #e07568; }
    .log {
      margin-top: 10px;
      background: #111e31;
      color: #dce8fb;
      border-radius: 10px;
      padding: 10px;
      min-height: 86px;
      font-family: Consolas, "Courier New", monospace;
      font-size: .82rem;
      white-space: pre-wrap;
      word-break: break-word;
    }
    @media (max-width: 980px) {
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <h1>VMD + Multiwfn 风格脚本生成器</h1>
    <p>按图片选择风格，自动生成对应名称的 CMD 脚本。</p>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>配置与生成</h2>
      <div class="row">
        <label>模式</label>
        <div class="mode-switch">
          <button type="button" class="mode-btn active" id="modeBundle">套装模式</button>
          <button type="button" class="mode-btn" id="modeSplit">拆分模式</button>
        </div>
      </div>
      <div class="row">
        <label for="multi">Multiwfn.exe 路径</label>
        <input id="multi" type="text" list="multi_list" placeholder="E:\\...\\Multiwfn.exe" />
        <datalist id="multi_list"></datalist>
      </div>
      <div class="row">
        <label for="vmd">vmd.exe 路径</label>
        <input id="vmd" type="text" list="vmd_list" placeholder="E:\\...\\vmd.exe" />
        <datalist id="vmd_list"></datalist>
      </div>
      <div class="row">
        <label for="out">输出脚本名称（会随风格自动更新）</label>
        <input id="out" type="text" placeholder="AutoCube_StyleName.cmd" />
      </div>
      <div class="btnbar">
        <button class="action ghost" id="scanBtn">扫描路径</button>
        <button class="action success" id="genBtn">生成脚本</button>
      </div>
      <p class="small" id="dupInfo"></p>
      <div id="missingInfo" class="small"></div>
      <h3>自定义导入（VMD Save State）</h3>
      <p class="subtle">上传 save state 文件，可选上传封面图与简介。</p>
      <div class="row">
        <label for="customName">风格名称（可选）</label>
        <input id="customName" type="text" placeholder="例如：My Glassy Style" />
      </div>
      <div class="row">
        <label for="customDesc">简介（可选）</label>
        <textarea id="customDesc" rows="3" placeholder="例如：用于轨道图，透明度较高，背景白色。"></textarea>
      </div>
      <div class="row">
        <label for="stateFile">上传 Save State 文件（必选）</label>
        <input id="stateFile" type="file" />
      </div>
      <div class="row">
        <label for="coverFile">上传封面图（可选）</label>
        <input id="coverFile" type="file" accept="image/*" />
      </div>
      <div class="btnbar">
        <button class="action" id="importCustomBtn">导入自定义风格</button>
      </div>
      <div id="log" class="log"></div>
    </div>

    <div class="panel">
      <div id="bundleWrap">
        <h2>套装风格</h2>
        <div id="bundleStyles" class="card-grid"></div>
      </div>

      <div id="splitWrap" style="display:none;">
        <h2>拆分风格</h2>
        <div class="split-row">
          <div>
            <h3>骨架样式</h3>
            <div id="skeletonStyles" class="card-grid"></div>
          </div>
          <div>
            <h3>等值面样式</h3>
            <div id="isoStyles" class="card-grid"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const state = {
  bundleStyles: [],
  isoStyles: [],
  skeletonStyles: [],
  selectedBundle: null,
  selectedIso: null,
  selectedSkeleton: null,
  mode: 'bundle',
  config: null,
  missingSkeletonPreviews: [],
};

const log = (txt) => {
  const el = document.getElementById('log');
  const now = new Date().toLocaleTimeString();
  el.textContent = `[${now}] ${txt}\\n` + el.textContent;
};

function fillList(id, items) {
  const dl = document.getElementById(id);
  dl.innerHTML = '';
  for (const x of items || []) {
    const op = document.createElement('option');
    op.value = x;
    dl.appendChild(op);
  }
}

function getById(styles, id) {
  return (styles || []).find(x => x.id === id) || null;
}

function validPicked(styles, candidate) {
  if (candidate && (styles || []).some(x => x.id === candidate)) return candidate;
  return styles && styles[0] ? styles[0].id : null;
}

function cleanFilePart(text) {
  let safe = String(text || '').trim();
  const invalid = ['<', '>', ':', '"', '/', '|', '?', '*'];
  for (const ch of invalid) {
    safe = safe.split(ch).join(' ');
  }
  safe = safe.split(String.fromCharCode(92)).join(' ');
  safe = safe.replace(/\\s+/g, '_').replace(/_+/g, '_').replace(/^_+|_+$/g, '');
  if (!safe) safe = 'Style';
  return safe.slice(0, 90);
}

function buildAutoOutputName() {
  if (state.mode === 'split') {
    const sk = getById(state.skeletonStyles, state.selectedSkeleton);
    const iso = getById(state.isoStyles, state.selectedIso);
    const skName = cleanFilePart(sk ? sk.name : 'Skeleton');
    const isoName = cleanFilePart(iso ? iso.name : 'Iso');
    return `AutoCube_${skName}_${isoName}.cmd`;
  }
  const style = getById(state.bundleStyles, state.selectedBundle);
  const styleName = cleanFilePart(style ? style.name : 'Style');
  return `AutoCube_${styleName}.cmd`;
}

function syncOutputName() {
  document.getElementById('out').value = buildAutoOutputName();
}

function refreshModeUI() {
  const isBundle = state.mode === 'bundle';
  document.getElementById('bundleWrap').style.display = isBundle ? 'block' : 'none';
  document.getElementById('splitWrap').style.display = isBundle ? 'none' : 'block';
  document.getElementById('modeBundle').classList.toggle('active', isBundle);
  document.getElementById('modeSplit').classList.toggle('active', !isBundle);
}

function setMode(mode) {
  state.mode = mode === 'split' ? 'split' : 'bundle';
  refreshModeUI();
  syncOutputName();
}

function renderCardList(containerId, styles, selectedId, onPick, subtitleFn) {
  const box = document.getElementById(containerId);
  box.innerHTML = '';
  for (const st of styles) {
    const card = document.createElement('div');
    card.className = 'card' + (selectedId === st.id ? ' sel' : '');
    card.onclick = () => onPick(st.id);
    const tags = (st.sources || []).map(s => {
      const show = String(s || '').replace('http://', '').replace('https://', '');
      return `<span class="tag">${show}</span>`;
    }).join('');
    const customTag = st.is_custom ? '<span class="tag">自定义</span>' : '';
    const deleteAction = st.is_custom
      ? '<div class="card-actions"><button type="button" class="mini-danger">删除</button></div>'
      : '';
    const subtitle = subtitleFn ? subtitleFn(st) : (st.notes || '');
    card.innerHTML = `
      <img src="${st.image_url}" alt="${st.name}" loading="lazy" />
      <div class="meta">
        <h3>${st.name}</h3>
        <p class="m">${subtitle}</p>
        <div class="tags">${customTag}${tags}</div>
        ${deleteAction}
      </div>
    `;
    const deleteBtn = card.querySelector('.mini-danger');
    if (deleteBtn) {
      deleteBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        deleteCustomStyle(st.id, st.name).catch(e => log('删除出错: ' + e.message));
      });
    }
    box.appendChild(card);
  }
}

function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.readAsText(file, 'utf-8');
  });
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.readAsDataURL(file);
  });
}

function renderAllCards() {
  renderCardList(
    'bundleStyles',
    state.bundleStyles,
    state.selectedBundle,
    (id) => { state.selectedBundle = id; renderAllCards(); syncOutputName(); },
    (st) => `Material: ${st.material} | ${st.pos_color_expr || ('ColorID ' + st.pos_color)} / ${st.neg_color_expr || ('ColorID ' + st.neg_color)}`
  );
  renderCardList(
    'skeletonStyles',
    state.skeletonStyles,
    state.selectedSkeleton,
    (id) => { state.selectedSkeleton = id; renderAllCards(); syncOutputName(); },
    (st) => st.notes || 'Skeleton style preset'
  );
  renderCardList(
    'isoStyles',
    state.isoStyles,
    state.selectedIso,
    (id) => { state.selectedIso = id; renderAllCards(); syncOutputName(); },
    (st) => `Material: ${st.material} | ${st.pos_color_expr || ('ColorID ' + st.pos_color)} / ${st.neg_color_expr || ('ColorID ' + st.neg_color)}`
  );
}

async function loadInit() {
  const stylesRes = await fetch('/api/styles');
  const stylesData = await stylesRes.json();
  state.bundleStyles = stylesData.bundle_styles || [];
  state.isoStyles = stylesData.isosurface_styles || [];
  state.skeletonStyles = stylesData.skeleton_styles || [];
  state.missingSkeletonPreviews = stylesData.skeleton_missing_previews || [];

  document.getElementById('dupInfo').textContent =
    `已加载 ${stylesData.style_count || 0} 个等值面风格（含自定义 ${stylesData.custom_count || 0} 个），${state.skeletonStyles.length} 个骨架样式。`;

  if (state.missingSkeletonPreviews.length > 0) {
    document.getElementById('missingInfo').innerHTML =
      `<div class="warn">部分骨架样式暂无封面图：${state.missingSkeletonPreviews.join(', ')}。不影响脚本生成。</div>`;
  } else {
    document.getElementById('missingInfo').textContent = '';
  }

  const confRes = await fetch('/api/config');
  state.config = await confRes.json();
  document.getElementById('multi').value = state.config.multiwfn_exe || '';
  document.getElementById('vmd').value = state.config.vmd_exe || '';

  state.selectedBundle = validPicked(state.bundleStyles, state.config.last_style);
  state.selectedIso = validPicked(state.isoStyles, state.config.last_iso_style);
  state.selectedSkeleton = validPicked(state.skeletonStyles, state.config.last_skeleton);

  fillList('multi_list', state.config.candidates.multiwfn || []);
  fillList('vmd_list', state.config.candidates.vmd || []);

  renderAllCards();
  setMode(state.config.mode || 'bundle');
  log('初始化完成，请选择风格并生成脚本。');
}

async function scanPaths() {
  const r = await fetch('/api/scan_paths', { method:'POST' });
  const data = await r.json();
  fillList('multi_list', data.multiwfn || []);
  fillList('vmd_list', data.vmd || []);
  log(`路径扫描完成：Multiwfn ${data.multiwfn.length}，VMD ${data.vmd.length}`);
}

async function generate() {
  const outEl = document.getElementById('out');
  let outputName = outEl.value.trim();
  if (!outputName) {
    outputName = buildAutoOutputName();
    outEl.value = outputName;
  }

  const payload = {
    mode: state.mode,
    style_id: state.selectedBundle,
    iso_style_id: state.selectedIso,
    skeleton_id: state.selectedSkeleton,
    multiwfn_exe: document.getElementById('multi').value.trim(),
    vmd_exe: document.getElementById('vmd').value.trim(),
    output_name: outputName,
  };

  if (state.mode === 'bundle' && !payload.style_id) {
    log('套装模式未选择风格。');
    return;
  }
  if (state.mode === 'split' && (!payload.iso_style_id || !payload.skeleton_id)) {
    log('拆分模式需要同时选择骨架样式和等值面样式。');
    return;
  }

  const r = await fetch('/api/generate', {
    method:'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await r.json();
  if (!r.ok) {
    log('生成失败: ' + data.error);
    return;
  }
  log('已生成: ' + data.output_path + ' | 风格=' + data.style_name);
}

async function importCustomStyle() {
  const stateFile = document.getElementById('stateFile').files[0];
  if (!stateFile) {
    log('请先上传 VMD Save State 文件。');
    return;
  }
  const coverFile = document.getElementById('coverFile').files[0] || null;
  const stateText = await readFileAsText(stateFile);
  const coverDataUrl = coverFile ? await readFileAsDataURL(coverFile) : '';

  const payload = {
    name: document.getElementById('customName').value.trim(),
    description: document.getElementById('customDesc').value.trim(),
    state_filename: stateFile.name || 'save_state.vmd',
    state_text: stateText,
    cover_name: coverFile ? (coverFile.name || 'cover.png') : '',
    cover_data_url: coverDataUrl,
  };

  const r = await fetch('/api/custom/import', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await r.json();
  if (!r.ok) {
    log('导入失败: ' + data.error);
    return;
  }
  log('导入成功: ' + data.style_name + '，已保存为自定义风格。');
  await loadInit();
}

async function deleteCustomStyle(styleId, styleName) {
  if (!styleId) return;
  if (!confirm(`确定删除自定义风格“${styleName || styleId}”吗？`)) return;

  const r = await fetch('/api/custom/delete', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({style_id: styleId}),
  });
  const data = await r.json();
  if (!r.ok) {
    log('删除失败: ' + data.error);
    return;
  }
  log('已删除自定义风格: ' + data.style_name);
  await loadInit();
}

document.getElementById('modeBundle').addEventListener('click', () => {
  setMode('bundle');
});

document.getElementById('modeSplit').addEventListener('click', () => {
  setMode('split');
});

document.getElementById('scanBtn').addEventListener('click', () => {
  scanPaths().catch(e => log('扫描出错: ' + e.message));
});

document.getElementById('genBtn').addEventListener('click', () => {
  generate().catch(e => log('生成出错: ' + e.message));
});

document.getElementById('importCustomBtn').addEventListener('click', () => {
  importCustomStyle().catch(e => log('导入出错: ' + e.message));
});

loadInit().catch(e => log('初始化失败: ' + e.message));
</script>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send_json(self, payload: dict, code: int = 200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, text: str, code: int = 200):
        raw = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, file_path: Path):
        suffix = file_path.suffix.lower()
        ctype = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(suffix, "application/octet-stream")
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(INDEX_HTML)
            return

        if path == "/api/styles":
            all_bundle = get_all_bundle_styles()
            missing_skeleton_previews = [
                s["name"] for s in SKELETON_STYLES if not (STYLE_DIR / s["image"]).exists()
            ]
            self._send_json(
                {
                    "bundle_styles": [style_payload(s) for s in all_bundle],
                    "isosurface_styles": [style_payload(s) for s in all_bundle],
                    "skeleton_styles": [skeleton_payload(s) for s in SKELETON_STYLES],
                    "style_count": len(all_bundle),
                    "duplicate_count": len(DUPLICATES),
                    "duplicates": DUPLICATES,
                    "skeleton_missing_previews": missing_skeleton_previews,
                    "custom_count": len(load_custom_styles()),
                }
            )
            return

        if path == "/api/config":
            conf = load_config()
            conf["candidates"] = find_path_candidates()
            self._send_json(conf)
            return

        if path.startswith("/img/"):
            name = unquote(path[len("/img/"):])
            target = (STYLE_DIR / name).resolve()
            try:
                target.relative_to(STYLE_DIR.resolve())
            except Exception:
                self._send_json({"error": "invalid image path"}, code=400)
                return
            if not target.exists() or not target.is_file():
                self._send_json({"error": "image not found"}, code=404)
                return
            self._send_file(target)
            return

        self._send_json({"error": "not found"}, code=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/scan_paths":
            self._send_json(find_path_candidates())
            return

        if path == "/api/custom/delete":
            try:
                body = self._json_body()
            except Exception:
                self._send_json({"error": "invalid json body"}, code=400)
                return

            style_id = (body.get("style_id") or "").strip()
            if not style_id:
                self._send_json({"error": "style_id is required"}, code=400)
                return
            removed = delete_custom_style(style_id)
            if removed is None:
                self._send_json({"error": "custom style not found"}, code=404)
                return

            self._send_json(
                {
                    "ok": True,
                    "style_id": style_id,
                    "style_name": removed.get("name", style_id),
                }
            )
            return

        if path == "/api/custom/import":
            try:
                body = self._json_body()
            except Exception:
                self._send_json({"error": "invalid json body"}, code=400)
                return

            state_text = (body.get("state_text") or "")
            state_filename = (body.get("state_filename") or "save_state").strip()
            name = (body.get("name") or "").strip()
            description = (body.get("description") or "").strip()
            cover_name = (body.get("cover_name") or "").strip()
            cover_data_url = (body.get("cover_data_url") or "").strip()

            if not state_text.strip():
                self._send_json({"error": "state_text is required"}, code=400)
                return
            if len(state_text) > 3_000_000:
                self._send_json({"error": "state_text is too large"}, code=400)
                return

            try:
                style = parse_save_state_to_custom_style(
                    state_text,
                    name=name or Path(state_filename).stem,
                    description=description,
                )
            except Exception as exc:
                self._send_json({"error": f"解析 save state 失败: {exc}"}, code=400)
                return

            if cover_data_url:
                m = re.match(
                    r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$",
                    cover_data_url,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if not m:
                    self._send_json({"error": "封面图片格式不支持"}, code=400)
                    return
                ext = m.group(1).lower()
                if ext == "jpeg":
                    ext = "jpg"
                raw_b64 = m.group(2).strip()
                try:
                    image_bytes = base64.b64decode(raw_b64, validate=True)
                except Exception:
                    self._send_json({"error": "封面图片 base64 解码失败"}, code=400)
                    return
                if len(image_bytes) > 8_000_000:
                    self._send_json({"error": "封面图片过大（>8MB）"}, code=400)
                    return
                image_file = f"{style['id']}_cover.{ext}"
                write_bytes_atomic(STYLE_DIR / image_file, image_bytes)
                style["image"] = image_file
            elif cover_name:
                style["notes"] = (
                    (style.get("notes", "") + " | ")
                    if style.get("notes")
                    else ""
                ) + f"cover filename hint: {cover_name}"

            upsert_custom_style(style)

            self._send_json(
                {
                    "ok": True,
                    "style_id": style["id"],
                    "style_name": style["name"],
                    "image": style["image"],
                }
            )
            return

        if path == "/api/generate":
            try:
                body = self._json_body()
            except Exception:
                self._send_json({"error": "invalid json body"}, code=400)
                return

            mode = (body.get("mode") or "bundle").strip().lower()
            style_id = (body.get("style_id") or "").strip()
            iso_style_id = (body.get("iso_style_id") or "").strip()
            skeleton_id = (body.get("skeleton_id") or "").strip()
            multi = (body.get("multiwfn_exe") or "").strip()
            vmd = (body.get("vmd_exe") or "").strip()
            out_name = (body.get("output_name") or "").strip()

            if not multi:
                self._send_json({"error": "multiwfn_exe is required"}, code=400)
                return
            if not vmd:
                self._send_json({"error": "vmd_exe is required"}, code=400)
                return

            style = None
            rep0_commands = None
            selected_bundle = ""
            selected_iso = ""
            selected_skeleton = ""
            bundle_map = get_bundle_style_map()

            if mode == "split":
                skeleton = SKELETON_BY_ID.get(skeleton_id)
                iso_style = bundle_map.get(iso_style_id)
                if not skeleton:
                    self._send_json(
                        {"error": f"unknown skeleton_id: {skeleton_id}"}, code=400
                    )
                    return
                if not iso_style:
                    self._send_json(
                        {"error": f"unknown iso_style_id: {iso_style_id}"}, code=400
                    )
                    return
                style = compose_combo_style(skeleton, iso_style)
                rep0_commands = skeleton.get("rep0_commands", [])
                selected_bundle = iso_style["id"]
                selected_iso = iso_style["id"]
                selected_skeleton = skeleton["id"]
                default_id = f"{skeleton['id']}_{iso_style['id']}"
            else:
                mode = "bundle"
                style = bundle_map.get(style_id)
                if not style:
                    self._send_json({"error": f"unknown style_id: {style_id}"}, code=400)
                    return
                rep0_commands = style.get("rep0_commands") or None
                selected_bundle = style["id"]
                selected_iso = style["id"]
                selected_skeleton = (
                    skeleton_id
                    if skeleton_id in SKELETON_BY_ID
                    else (SKELETON_STYLES[0]["id"] if SKELETON_STYLES else "")
                )
                default_id = style["id"]

            safe_out = _sanitize_output_name(out_name, default_id)
            output_path = (ROOT / safe_out).resolve()

            script_text = build_cmd_script(style, multi, vmd, rep0_commands=rep0_commands)
            write_text_atomic(output_path, script_text)

            save_config(
                {
                    "multiwfn_exe": multi,
                    "vmd_exe": vmd,
                    "output_name": safe_out,
                    "mode": mode,
                    "last_style": selected_bundle,
                    "last_skeleton": selected_skeleton,
                    "last_iso_style": selected_iso,
                }
            )

            self._send_json(
                {
                    "ok": True,
                    "output_path": str(output_path),
                    "style_name": style["name"],
                }
            )
            return

        self._send_json({"error": "not found"}, code=404)


def pick_port(start: int = 8765) -> int:
    port = start
    while port < start + 200:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    raise RuntimeError("No free port available.")


def main():
    port = pick_port(8765)
    addr = ("127.0.0.1", port)
    server = ThreadingHTTPServer(addr, AppHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"[VMD Style Integrator] Listening on {url}")
    print(f"[VMD Style Integrator] Style images folder: {STYLE_DIR}")
    print("[VMD Style Integrator] Press Ctrl+C to stop.")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[VMD Style Integrator] Stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
