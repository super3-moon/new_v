from __future__ import annotations

import json
import os
import re
import socket
import webbrowser
import base64
import hashlib
from datetime import datetime
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
STYLE_DIR = ROOT / "vmd_cube_styles"
CONFIG_FILE = ROOT / "vmd_style_tool_config.json"
CUSTOM_STYLES_FILE = ROOT / "vmd_custom_styles.json"

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
        "name": "Bright Bule+Yellow",
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
        "name": "Modern cool palette",
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


def load_custom_styles() -> list[dict]:
    if not CUSTOM_STYLES_FILE.exists():
        return []
    try:
        raw = json.loads(CUSTOM_STYLES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        if "id" not in entry or "name" not in entry:
            continue
        out.append(entry)
    return out


def save_custom_styles(styles: list[dict]) -> None:
    CUSTOM_STYLES_FILE.write_text(
        json.dumps(styles, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_all_bundle_styles() -> list[dict]:
    return STYLES + load_custom_styles()


def get_bundle_style_map() -> dict[str, dict]:
    return {s["id"]: s for s in get_all_bundle_styles()}


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

    global_cmds: list[str] = []
    if mat_proc:
        global_cmds.extend(mat_proc)
        global_cmds.append("vmdrestoremymaterials")
    if col_proc:
        global_cmds.extend(col_proc)
        global_cmds.append("vmdrestoremycolors")

    keep_prefixes = (
        "display ",
        "light ",
        "axes ",
        "label textsize",
        "material add ",
        "material change ",
        "color scale ",
        "color change rgb ",
        "color Display ",
    )
    for i, raw in enumerate(lines):
        if i in skip_idx:
            continue
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith(keep_prefixes):
            global_cmds.append(s)

    reps = _parse_vmd_rep_blocks(lines)
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


def find_path_candidates() -> dict:
    roots = [ROOT]
    if ROOT.parent != ROOT:
        roots.append(ROOT.parent)

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
        "multiwfn": sorted(multi),
        "vmd": sorted(vmd),
    }


def load_config() -> dict:
    defaults = {
        "multiwfn_exe": "",
        "vmd_exe": "",
        "output_name": "AutoCube_OneClick_custom.cmd",
        "mode": "bundle",
        "last_style": STYLES[0]["id"] if STYLES else "",
        "last_skeleton": SKELETON_STYLES[0]["id"] if SKELETON_STYLES else "",
        "last_iso_style": STYLES[0]["id"] if STYLES else "",
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
    payload = {
        "multiwfn_exe": config.get("multiwfn_exe", ""),
        "vmd_exe": config.get("vmd_exe", ""),
        "output_name": config.get("output_name", "AutoCube_OneClick_custom.cmd"),
        "mode": config.get("mode", "bundle"),
        "last_style": config.get("last_style", ""),
        "last_skeleton": config.get("last_skeleton", ""),
        "last_iso_style": config.get("last_iso_style", ""),
    }
    CONFIG_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _sanitize_output_name(name: str, style_id: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        candidate = f"AutoCube_OneClick_{style_id}.cmd"
    if not candidate.lower().endswith(".cmd"):
        candidate += ".cmd"
    candidate = re.sub(r"[<>:\\|?*\"]", "_", candidate)
    candidate = candidate.replace("/", "_")
    return candidate


def build_cmd_script(
    style: dict, multiwfn_exe: str, vmd_exe: str, rep0_commands: list[str] | None = None
) -> str:
    multiwfn_exe = multiwfn_exe.replace('"', "")
    vmd_exe = vmd_exe.replace('"', "")
    if not rep0_commands:
        rep0_commands = [
            "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000",
            "mol modcolor 0 top Name",
            "mol modmaterial 0 top Opaque",
        ]

    lines: list[str] = []
    a = lines.append
    pos_color_expr = (style.get("pos_color_expr") or f"ColorID {int(style['pos_color'])}").strip()
    neg_color_expr = (style.get("neg_color_expr") or f"ColorID {int(style['neg_color'])}").strip()

    a("@echo off")
    a("setlocal DisableDelayedExpansion")
    a("chcp 65001 >nul")
    a("")
    a("set \"A_DIR=%CD%\"")
    a("if \"%A_DIR:~-1%\"==\"\\\" set \"A_DIR=%A_DIR:~0,-1%\"")
    a("cd /d \"%A_DIR%\"")
    a("")
    a("echo [INFO] A folder: %A_DIR%")
    a("echo.")
    a("")
    a("rem ===== Auto-generated software paths =====")
    a(f"set \"MULTIWFN_EXE={multiwfn_exe}\"")
    a(f"set \"VMD_EXE={vmd_exe}\"")
    a("rem ========================================")
    a("")
    a("if not exist \"%MULTIWFN_EXE%\" (")
    a("  echo [ERROR] Multiwfn path is invalid: %MULTIWFN_EXE%")
    a("  pause")
    a("  exit /b 1")
    a(")")
    a("")
    a("if not exist \"%VMD_EXE%\" (")
    a("  echo [ERROR] VMD path is invalid: %VMD_EXE%")
    a("  pause")
    a("  exit /b 1")
    a(")")
    a("")
    a("echo [INFO] Multiwfn: %MULTIWFN_EXE%")
    a("echo [INFO] VMD: %VMD_EXE%")
    a("for %%D in (\"%MULTIWFN_EXE%\") do set \"MULTIWFN_DIR=%%~dpD\"")
    a("if \"%MULTIWFN_DIR:~-1%\"==\"\\\" set \"MULTIWFN_DIR=%MULTIWFN_DIR:~0,-1%\"")
    a("set \"Multiwfnpath=%MULTIWFN_DIR%\"")
    a("echo [INFO] Multiwfnpath: %Multiwfnpath%")
    a("echo.")
    a("echo [INFO] Launching Multiwfn...")
    a("echo [INFO] Generate ONE .cub file in this A folder, then exit Multiwfn.")
    a("start \"\" /wait \"%MULTIWFN_EXE%\"")
    a("")
    a("set \"CUBE_FILE=\"")
    a("for /f \"delims=\" %%F in ('dir /b /a:-d /o:-d \"%A_DIR%\\*.cub\" 2^>nul') do (")
    a("  if not defined CUBE_FILE set \"CUBE_FILE=%A_DIR%\\%%F\"")
    a(")")
    a("")
    a("if not defined CUBE_FILE (")
    a("  echo [ERROR] No .cub file found in A folder: %A_DIR%")
    a("  pause")
    a("  exit /b 1")
    a(")")
    a("")
    a("for %%B in (\"%CUBE_FILE%\") do set \"CUBE_BASE=%%~nB\"")
    a("echo [INFO] Using cube file: %CUBE_FILE%")
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
    a("set \"CUBE_TCL=%CUBE_FILE:\\=/%\"")
    a("set \"A_TCL=%A_DIR:\\=/%\"")
    a("set \"TCL_FILE=%TEMP%\\autocube_%RANDOM%%RANDOM%%RANDOM%.tcl\"")

    def _escape_batch_echo(text: str) -> str:
        return (
            text.replace("^", "^^")
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
    _echo_tcl("# Auto-generated single-file AutoCube workflow")
    _echo_tcl('set AUTO_CUBE_FILE "%CUBE_TCL%"')
    _echo_tcl("set AUTO_ISOVAL %ISO_NORM%")
    _echo_tcl('set AUTO_OUTDIR "%A_TCL%"')
    _echo_tcl('set AUTO_BASENAME "%CUBE_BASE%"')
    _echo_tcl("")
    _echo_tcl("proc _autocube_unique_path {target} {")
    _echo_tcl("    set candidate $target")
    _echo_tcl("    if {![file exists $candidate]} {")
    _echo_tcl("        return $candidate")
    _echo_tcl("    }")
    _echo_tcl("    set ext [file extension $target]")
    _echo_tcl("    set root [file rootname $target]")
    _echo_tcl('    if {$ext eq ""} {')
    _echo_tcl("        set root $target")
    _echo_tcl("    }")
    _echo_tcl("    set i 1")
    _echo_tcl("    while {[file exists $candidate]} {")
    _echo_tcl('        set candidate "${root}_$i$ext"')
    _echo_tcl("        incr i")
    _echo_tcl("    }")
    _echo_tcl("    return $candidate")
    _echo_tcl("}")
    _echo_tcl("")
    _echo_tcl("if {[llength [info commands _autocube_builtin_render]] == 0} {")
    _echo_tcl("    rename render _autocube_builtin_render")
    _echo_tcl("    proc render {args} {")
    _echo_tcl("        global AUTO_OUTDIR AUTO_BASENAME")
    _echo_tcl("        set passthrough [list list hasaa aasamples aosamples formats format options default]")
    _echo_tcl("        if {[llength $args] == 0} {")
    _echo_tcl("            return [uplevel 1 [list _autocube_builtin_render]]")
    _echo_tcl("        }")
    _echo_tcl("        set cmd0 [lindex $args 0]")
    _echo_tcl("        if {[lsearch -exact $passthrough $cmd0] >= 0} {")
    _echo_tcl("            return [uplevel 1 [list _autocube_builtin_render {*}$args]]")
    _echo_tcl("        }")
    _echo_tcl("        if {[llength $args] < 2} {")
    _echo_tcl("            return [uplevel 1 [list _autocube_builtin_render {*}$args]]")
    _echo_tcl("        }")
    _echo_tcl("")
    _echo_tcl("        set method [lindex $args 0]")
    _echo_tcl("        set requested [lindex $args 1]")
    _echo_tcl('        if {$requested eq ""} {')
    _echo_tcl('            set requested "${AUTO_BASENAME}_render"')
    _echo_tcl("        }")
    _echo_tcl("")
    _echo_tcl("        set filenameOnly [file tail $requested]")
    _echo_tcl('        if {$filenameOnly eq ""} {')
    _echo_tcl('            set filenameOnly "${AUTO_BASENAME}_render"')
    _echo_tcl("        }")
    _echo_tcl("")
    _echo_tcl("        set target [file normalize [file join $AUTO_OUTDIR $filenameOnly]]")
    _echo_tcl("        set target [_autocube_unique_path $target]")
    _echo_tcl("")
    _echo_tcl("        set newargs [list $method $target]")
    _echo_tcl("        if {[llength $args] > 2} {")
    _echo_tcl("            set newargs [concat $newargs [lrange $args 2 end]]")
    _echo_tcl("        }")
    _echo_tcl("")
    _echo_tcl("        set code [catch {uplevel 1 [list _autocube_builtin_render {*}$newargs]} msg opts]")
    _echo_tcl("        if {$code != 0} {")
    _echo_tcl("            return -options $opts $msg")
    _echo_tcl("        }")
    _echo_tcl("")
    _echo_tcl('        puts "AutoCube: Render output saved to $target"')
    _echo_tcl("        foreach i [molinfo list] {")
    _echo_tcl("            mol delete $i")
    _echo_tcl("        }")
    _echo_tcl('        puts "AutoCube: Deleted current molecule and isosurfaces in VMD."')
    _echo_tcl("        return $msg")
    _echo_tcl("    }")
    _echo_tcl("}")
    _echo_tcl("")
    _echo_tcl(f'# Style: {style["name"]}')
    for src in style["sources"]:
        _echo_tcl(f"# Source: {src}")
    _echo_tcl(f'set mater {style["material"]}')
    for cmd in style["commands"]:
        _echo_tcl(cmd)
    _echo_tcl("")
    _echo_tcl("foreach i [molinfo list] {")
    _echo_tcl("    mol delete $i")
    _echo_tcl("}")
    _echo_tcl("")
    _echo_tcl("mol new $AUTO_CUBE_FILE type cube waitfor all")
    for rep0_cmd in rep0_commands:
        _echo_tcl(rep0_cmd)
    _echo_tcl("mol addrep top")
    _echo_tcl("mol modstyle 1 top Isosurface $AUTO_ISOVAL 0 0 0 1 1")
    _echo_tcl(f"mol modcolor 1 top {pos_color_expr}")
    _echo_tcl("mol modmaterial 1 top $mater")
    _echo_tcl("mol addrep top")
    _echo_tcl("set negiso [expr {-$AUTO_ISOVAL}]")
    _echo_tcl("mol modstyle 2 top Isosurface $negiso 0 0 0 1 1")
    _echo_tcl(f"mol modcolor 2 top {neg_color_expr}")
    _echo_tcl("mol modmaterial 2 top $mater")
    _echo_tcl("display distance -8.0")
    _echo_tcl("display height 10")
    _echo_tcl("")
    _echo_tcl("menu main on")
    _echo_tcl("menu graphics on")
    _echo_tcl("menu render on")
    _echo_tcl("")
    _echo_tcl('puts "AutoCube: Isosurface drawing is ready; not rendered automatically."')
    _echo_tcl('puts "AutoCube: Render manually in VMD. Output will be forced into A folder."')
    _echo_tcl('puts "AutoCube: After each successful render, current molecule and surfaces are deleted."')
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
    a('')
    a('del /q "%TCL_FILE%" >nul 2>nul')
    a('for %%E in (cub dat) do (')
    a('  for /f "delims=" %%F in (\'dir /b /a:-d "%A_DIR%\\*.%%E" 2^>nul\') do (')
    a('    del /q "%A_DIR%\\%%F" >nul 2>nul')
    a('  )')
    a(')')
    a('for /f "delims=" %%F in (\'dir /b /a:-d "%A_DIR%\\*" 2^>nul\') do (')
    a('  if "%%~xF"=="" del /q "%A_DIR%\\%%F" >nul 2>nul')
    a(')')
    a('echo [INFO] Deleted .cub and .dat files in A folder.')
    a('echo [INFO] Workflow finished.')
    a('pause')
    a('exit /b 0')
    a('')

    return "\n".join(lines)

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
    const subtitle = subtitleFn ? subtitleFn(st) : (st.notes || '');
    card.innerHTML = `
      <img src="${st.image_url}" alt="${st.name}" loading="lazy" />
      <div class="meta">
        <h3>${st.name}</h3>
        <p class="m">${subtitle}</p>
        <div class="tags">${customTag}${tags}</div>
      </div>
    `;
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
  log('导入成功: ' + data.style_name + '，已加入风格库。');
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
                (STYLE_DIR / image_file).write_bytes(image_bytes)
                style["image"] = image_file
            elif cover_name:
                style["notes"] = (
                    (style.get("notes", "") + " | ")
                    if style.get("notes")
                    else ""
                ) + f"cover filename hint: {cover_name}"

            custom_styles = load_custom_styles()
            custom_styles = [s for s in custom_styles if s.get("id") != style["id"]]
            custom_styles.append(style)
            save_custom_styles(custom_styles)

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
            output_path.write_text(script_text, encoding="utf-8")

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

