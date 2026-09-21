"""Shared execution core for the additional Multiwfn + VMD workflows.

The four user-facing workflow families are intentionally data-driven.  They
share one process runner, one result layout, one VMD renderer and one progress
event contract; only the documented Multiwfn menu sequence and expected
scientific products differ.
"""

from __future__ import annotations

import copy
import json
import locale
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from PIL import Image

import orbital_data
import orbital_vmd
import vmd_style_tool as vmd_core


WORKFLOW_WEAK = "weak_interaction"
WORKFLOW_EXCITED = "excited_state_density"
WORKFLOW_SPIN = "spin_density"
WORKFLOW_DEFORMATION = "density_difference_deformation"

WAVEFUNCTION_EXTENSIONS = (
    ".fch",
    ".fchk",
    ".wfn",
    ".wfx",
    ".mwfn",
    ".molden",
    ".molden.input",
)
OUTPUT_EXTENSIONS = (".out", ".log")


class ScientificWorkflowError(RuntimeError):
    pass


class ScientificWorkflowValidationError(ScientificWorkflowError):
    pass


@dataclass(frozen=True, slots=True)
class ScientificWorkflowSpec:
    id: str
    name: str
    icon: str
    description: str
    methods: tuple[tuple[str, str], ...]
    input_roles: tuple[tuple[str, str, tuple[str, ...]], ...]
    surface_mode: str
    tags: str


_SPECS = (
    ScientificWorkflowSpec(
        WORKFLOW_WEAK,
        "弱相互作用分析",
        "IRI",
        "统一生成 IRI、RDG/NCI 或 IGMH 网格，并按推荐的指标等值面着色。",
        (("iri", "IRI"), ("rdg", "RDG / NCI"), ("igmh", "IGMH（片段间）")),
        (("wavefunction", "波函数文件", WAVEFUNCTION_EXTENSIONS),),
        "weak_interaction",
        "IRI · RDG/NCI · IGMH · VMD",
    ),
    ScientificWorkflowSpec(
        WORKFLOW_EXCITED,
        "激发态空穴-电子与 NTO",
        "EX",
        "读取激发态输出，生成空穴/电子/电荷密度差，或导出并绘制主导 NTO 对。",
        (("hole_electron", "空穴-电子与 CDD"), ("nto", "自然跃迁轨道 NTO")),
        (
            ("wavefunction", "波函数文件", WAVEFUNCTION_EXTENSIONS),
            ("output", "激发态输出文件", OUTPUT_EXTENSIONS),
        ),
        "signed",
        "Gaussian / ORCA · Hole/Electron · NTO",
    ),
    ScientificWorkflowSpec(
        WORKFLOW_SPIN,
        "自旋密度",
        "SPIN",
        "为开壳层体系生成带正负相位的自旋密度 Cube 与图片。",
        (("spin", "自旋密度"),),
        (("wavefunction", "开壳层波函数", WAVEFUNCTION_EXTENSIONS),),
        "signed",
        "开壳层 · 正负自旋密度 · VMD",
    ),
    ScientificWorkflowSpec(
        WORKFLOW_DEFORMATION,
        "电子密度差／变形密度",
        "Δρ",
        "既可计算分子与自由原子叠加密度之差，也可由目标体系减去一个或多个参考体系。",
        (
            ("deformation", "分子变形密度（分子 − 自由原子叠加）"),
            ("fragment_difference", "多文件电子密度差（目标体系 − 参考体系）"),
        ),
        (("wavefunction", "目标体系波函数", WAVEFUNCTION_EXTENSIONS),),
        "signed",
        "变形密度 · 多文件密度差 · 电子积累/耗散 · VMD",
    ),
)


def workflow_specs() -> tuple[ScientificWorkflowSpec, ...]:
    return _SPECS


def workflow_spec(workflow_id: str) -> ScientificWorkflowSpec:
    for spec in _SPECS:
        if spec.id == workflow_id:
            return spec
    raise ScientificWorkflowValidationError(f"未知的自动化流程：{workflow_id}")


_WEAK_DISPLAY_PROFILES: dict[str, dict[str, object]] = {
    # Multiwfn 2026.7.11 examples/IRIfill.vmd
    "iri": {
        "name": "Multiwfn IRI 推荐显示",
        "surface_field": "IRI",
        "color_field": "sign(λ₂)ρ",
        "iso_value": 1.0,
        "color_min": -0.04,
        "color_max": 0.02,
        "color_midpoint": 0.666,
        "skeleton_scale": 0.7,
    },
    # Multiwfn 2026.7.11 examples/RDGfill2.vmd explicitly identifies this
    # scale as the more reasonable variant of RDGfill.vmd.
    "rdg": {
        "name": "Multiwfn RDG/NCI 推荐显示",
        "surface_field": "RDG",
        "color_field": "sign(λ₂)ρ",
        "iso_value": 0.5,
        "color_min": -0.04,
        "color_max": 0.02,
        "color_midpoint": 0.666,
        "skeleton_scale": 1.0,
    },
    # Multiwfn 2026.7.11 examples/IGM_inter.vmd
    "igmh": {
        "name": "Multiwfn IGMH 片段间推荐显示",
        "surface_field": "δginter",
        "color_field": "sign(λ₂)ρ",
        "iso_value": 0.01,
        "color_min": -0.05,
        "color_max": 0.05,
        "color_midpoint": 0.5,
        "skeleton_scale": 1.0,
    },
}


def weak_interaction_display_profile(method: str) -> dict[str, object]:
    """Return the method-specific display parameters bundled with Multiwfn."""
    try:
        return copy.deepcopy(_WEAK_DISPLAY_PROFILES[str(method)])
    except KeyError as exc:
        raise ScientificWorkflowValidationError("未知的弱相互作用显示方法。") from exc


def _tcl_utf8_path(path: Path | str) -> str:
    encoded = str(Path(path).expanduser().resolve()).encode("utf-8").hex()
    return f"[encoding convertfrom utf-8 [binary format H* {encoded}]]"


def build_weak_interaction_scene_tcl(
    method: str,
    surface_cube: Path | str,
    color_cube: Path | str,
) -> str:
    """Build the initial VMD scene from Multiwfn's method-specific scripts.

    The first Cube is the sign(lambda2)rho color field and the second supplies
    the IRI, RDG or delta-g_inter isosurface. This deliberately does not pass
    through the generic ESP/style-library renderer.
    """
    profile = weak_interaction_display_profile(method)
    surface = Path(surface_cube).expanduser().resolve()
    color = Path(color_cube).expanduser().resolve()
    if not surface.is_file() or not color.is_file():
        raise ScientificWorkflowValidationError("弱相互作用绘图所需的 Cube 文件不完整。")
    iso = float(profile["iso_value"])
    minimum = float(profile["color_min"])
    maximum = float(profile["color_max"])
    midpoint = float(profile["color_midpoint"])
    skeleton = float(profile["skeleton_scale"])
    lines = [
        f"set MO_COLOR_CUBE {_tcl_utf8_path(color)}",
        f"set MO_SURFACE_CUBE {_tcl_utf8_path(surface)}",
        "if {![file isfile $MO_COLOR_CUBE]} { error \"Weak-interaction color Cube is missing\" }",
        "if {![file isfile $MO_SURFACE_CUBE]} { error \"Weak-interaction surface Cube is missing\" }",
        "mol new $MO_COLOR_CUBE type cube waitfor all",
        "mol addfile $MO_SURFACE_CUBE type cube waitfor all",
        "set MO_MOL [molinfo top]",
        "mol delrep 0 $MO_MOL",
        f"mol representation CPK {skeleton:.6f} 0.300000 18.000000 16.000000",
        "mol color Element",
        "mol material Opaque",
        "mol addrep $MO_MOL",
        f"mol representation Isosurface {iso:.8g} 1 0 0 1 1",
        "mol color Volume 0",
        "mol material Opaque",
        "mol addrep $MO_MOL",
        f"mol scaleminmax $MO_MOL 1 {minimum:.8g} {maximum:.8g}",
        "color scale method BGR",
        f"color scale midpoint {midpoint:.8g}",
        "color Display Background white",
        "axes location Off",
        "display depthcue off",
        "display rendermode GLSL",
        "light 3 on",
    ]
    if method == "iri":
        lines.extend(["color Element N iceblue", "mol modcolor 0 $MO_MOL Element"])
    if method == "igmh":
        lines.append("material change specular Opaque 0.300000")
    lines.extend(["display resetview", "display update ui"])
    return "\n".join(lines) + "\n"


def _clean_part(value: str, fallback: str = "result") -> str:
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return (text or fallback)[:100]


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    number = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _path_for_role(
    raw_inputs: Mapping[str, object], role: str, extensions: tuple[str, ...]
) -> Path:
    path = Path(str(raw_inputs.get(role) or "")).expanduser().resolve()
    if not path.is_file():
        raise ScientificWorkflowValidationError(f"{role} 文件不存在：{path}")
    name = path.name.casefold()
    if not any(name.endswith(ext) for ext in extensions):
        raise ScientificWorkflowValidationError(f"不支持的文件格式：{path.name}")
    return path


def _density_difference_reference_paths(
    raw: object,
    *,
    primary: Path | None = None,
    require_files: bool = True,
) -> tuple[Path, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ScientificWorkflowValidationError("多文件电子密度差至少需要一个参考体系波函数。")
    references: list[Path] = []
    seen: set[str] = set()
    primary_key = os.path.normcase(str(primary)) if primary is not None else ""
    for value in raw:
        path = Path(str(value or "")).expanduser().resolve()
        key = os.path.normcase(str(path))
        if key == primary_key:
            raise ScientificWorkflowValidationError("目标体系不能同时作为减去的参考体系。")
        if key in seen:
            continue
        if "," in str(path):
            raise ScientificWorkflowValidationError(
                f"Multiwfn 自定义运算不支持路径中包含逗号：{path.name}"
            )
        if require_files and not path.is_file():
            raise ScientificWorkflowValidationError(f"参考体系文件不存在：{path}")
        name = path.name.casefold()
        if not any(name.endswith(ext) for ext in WAVEFUNCTION_EXTENSIONS):
            raise ScientificWorkflowValidationError(f"不支持的参考体系格式：{path.name}")
        seen.add(key)
        references.append(path)
    if not references:
        raise ScientificWorkflowValidationError("多文件电子密度差至少需要一个参考体系波函数。")
    return tuple(references)


def _validate_density_difference_coordinate_frame(
    primary: Path, references: tuple[Path, ...]
) -> None:
    """Reject supported files whose atoms are not in the target coordinate frame.

    Multiwfn evaluates every reference wavefunction on the first file's grid;
    a fragment that Gaussian rotated to its standard orientation therefore
    produces a numerically valid but scientifically meaningless difference.
    FCH and Molden inputs expose enough geometry for a safe subset check. Other
    Multiwfn formats remain supported and are accompanied by an explicit UI
    warning because this application does not reinterpret their coordinates.
    """

    def parsed_atoms(path: Path):
        folded = path.name.casefold()
        if not folded.endswith((".fch", ".fchk", ".molden", ".molden.input")):
            return None
        try:
            return orbital_data.parse_wavefunction_file(path).atoms
        except orbital_data.OrbitalDataError as exc:
            raise ScientificWorkflowValidationError(
                f"无法核验波函数坐标：{path.name}（{exc}）"
            ) from exc

    target_atoms = parsed_atoms(primary)
    if not target_atoms:
        return
    tolerance_squared = 0.01**2
    for reference in references:
        atoms = parsed_atoms(reference)
        if not atoms:
            continue
        available = set(range(len(target_atoms)))
        for atom in atoms:
            match = next(
                (
                    index
                    for index in available
                    if target_atoms[index].atomic_number == atom.atomic_number
                    and (target_atoms[index].x - atom.x) ** 2
                    + (target_atoms[index].y - atom.y) ** 2
                    + (target_atoms[index].z - atom.z) ** 2
                    <= tolerance_squared
                ),
                None,
            )
            if match is None:
                raise ScientificWorkflowValidationError(
                    f"{reference.name} 与目标体系不在同一坐标系。请在目标体系几何上计算参考体系，"
                    "并关闭会旋转坐标的标准取向。"
                )
            available.remove(match)


def build_multiwfn_sequence(
    workflow_id: str,
    method: str,
    inputs: Mapping[str, Path],
    options: Mapping[str, object],
    *,
    nto_output: Path | None = None,
) -> str:
    """Return a tested Multiwfn 2026.7.11 menu sequence."""
    grid = max(1, min(3, int(options.get("grid_quality") or 2)))
    if workflow_id == WORKFLOW_WEAK:
        if method == "iri":
            return f"20\n4\n{grid}\n3\n0\n0\nq\n"
        if method == "rdg":
            return f"20\n1\n{grid}\n3\n0\n0\nq\n"
        if method == "igmh":
            fragments = [
                value.strip()
                for value in str(options.get("fragments") or "").split(";")
                if value.strip()
            ]
            if len(fragments) < 2:
                raise ScientificWorkflowValidationError(
                    "IGMH 至少需要两个片段；请用分号分隔，例如 1-12;13-25。"
                )
            return "\n".join(
                ["20", "11", str(len(fragments)), *fragments, str(grid), "3", "0", "0", "q", ""]
            )
        raise ScientificWorkflowValidationError("未知的弱相互作用方法。")

    if workflow_id == WORKFLOW_EXCITED:
        state = max(1, int(options.get("excited_state") or 1))
        if method == "hole_electron":
            return "\n".join(
                [
                    "18",
                    "1",
                    str(inputs["output"]),
                    str(state),
                    "1",
                    str(grid),
                    "10",
                    "1",
                    "11",
                    "1",
                    "15",
                    "0",
                    "0",
                    "0",
                    "q",
                    "",
                ]
            )
        if method == "nto":
            if nto_output is None:
                raise ScientificWorkflowValidationError("NTO 输出路径未设置。")
            return "\n".join(
                ["18", "6", str(inputs["output"]), str(state), "2", str(nto_output), "0", "q", ""]
            )
        raise ScientificWorkflowValidationError("未知的激发态分析方法。")

    if workflow_id == WORKFLOW_SPIN:
        return f"5\n5\n{grid}\n2\n0\nq\n"

    if workflow_id == WORKFLOW_DEFORMATION:
        if method == "deformation":
            # Multiwfn manual 3.7.2 / tutorial 4.2.8: main function 5,
            # deformation property (-2), electron density (1), export Cube (2).
            return f"5\n-2\n1\n{grid}\n2\n0\nq\n"
        if method == "fragment_difference":
            # Multiwfn manual 3.7.1 and tutorial 4.5.5: custom operation
            # starts from the initially loaded target wavefunction, then
            # subtracts each reference density on the target's common grid.
            references = _density_difference_reference_paths(
                options.get("reference_files"),
                primary=inputs.get("wavefunction"),
                require_files=False,
            )
            operations = [f"-,{path}" for path in references]
            return "\n".join(
                [
                    "5",
                    "0",
                    str(len(references)),
                    *operations,
                    "1",
                    str(grid),
                    "2",
                    "0",
                    "q",
                    "",
                ]
            )
        raise ScientificWorkflowValidationError("未知的电子密度差方法。")

    raise ScientificWorkflowValidationError(f"未知的自动化流程：{workflow_id}")


def _expected_products(workflow_id: str, method: str) -> tuple[str, ...]:
    if workflow_id == WORKFLOW_WEAK:
        return ("sl2r.cub", "dg_inter.cub", "dg_intra.cub", "dg.cub") if method == "igmh" else ("func1.cub", "func2.cub")
    if workflow_id == WORKFLOW_EXCITED and method == "hole_electron":
        return ("hole.cub", "electron.cub", "CDD.cub")
    if workflow_id == WORKFLOW_SPIN:
        return ("spindensity.cub",)
    if workflow_id == WORKFLOW_DEFORMATION:
        return ("density.cub",)
    return ()


def _render_pairs(
    workflow_id: str,
    method: str,
    cubes: Mapping[str, Path],
    options: Mapping[str, object] | None = None,
) -> list[tuple[str, Path, Path | None, float]]:
    if workflow_id == WORKFLOW_WEAK:
        if method == "igmh":
            return [("IGMH_inter", cubes["dg_inter.cub"], cubes["sl2r.cub"], 0.01)]
        return [
            (
                "IRI" if method == "iri" else "RDG",
                cubes["func2.cub"],
                cubes["func1.cub"],
                1.0 if method == "iri" else 0.5,
            )
        ]
    if workflow_id == WORKFLOW_DEFORMATION:
        default_iso = 0.0012 if method == "fragment_difference" else 0.05
        requested_iso = float((options or {}).get("iso_value") or default_iso)
        iso = requested_iso if 0.00001 <= requested_iso <= 1.0 else default_iso
        label = "Electron_Density_Difference" if method == "fragment_difference" else "Deformation_Density"
        return [(label, cubes["density.cub"], None, iso)]
    if workflow_id == WORKFLOW_EXCITED and method == "hole_electron":
        return [
            ("Hole_Electron", cubes["hole.cub"], cubes["electron.cub"], 0.002),
            ("CDD", cubes["CDD.cub"], None, 0.002),
        ]
    return [(Path(name).stem, path, None, 0.05) for name, path in cubes.items()]


class ScientificWorkflowRunner:
    def __init__(
        self,
        workflow_id: str,
        method: str,
        inputs: Mapping[str, object],
        options: Mapping[str, object],
        output_root: Path | str,
        multiwfn_exe: Path | str,
        vmd_exe: Path | str,
        *,
        event_callback: Callable[[dict], None] | None = None,
    ) -> None:
        self.spec = workflow_spec(workflow_id)
        self.workflow_id = workflow_id
        self.method = method
        if method not in {value for value, _label in self.spec.methods}:
            raise ScientificWorkflowValidationError("所选分析方法不属于当前流程。")
        self.inputs = {
            role: _path_for_role(inputs, role, extensions)
            for role, _label, extensions in self.spec.input_roles
        }
        self.options = copy.deepcopy(dict(options))
        self.reference_files: tuple[Path, ...] = ()
        if self.workflow_id == WORKFLOW_DEFORMATION and self.method == "fragment_difference":
            self.reference_files = _density_difference_reference_paths(
                self.options.get("reference_files"),
                primary=self.inputs.get("wavefunction"),
            )
            _validate_density_difference_coordinate_frame(
                self.inputs["wavefunction"], self.reference_files
            )
            self.options["reference_files"] = [str(path) for path in self.reference_files]
        self.output_root = Path(output_root).expanduser().resolve()
        self.multiwfn_exe = Path(multiwfn_exe).expanduser().resolve()
        self.vmd_exe = Path(vmd_exe).expanduser().resolve()
        if not self.multiwfn_exe.is_file():
            raise ScientificWorkflowValidationError("Multiwfn.exe 路径无效。")
        if not self.vmd_exe.is_file():
            raise ScientificWorkflowValidationError("vmd.exe 路径无效。")
        snapshot = self.options.get("style_snapshot")
        if self.workflow_id == WORKFLOW_WEAK:
            # Weak-interaction maps use method-specific scientific fields and
            # parameters from Multiwfn's bundled VMD scripts. They are not an
            # ESP-style surface and therefore deliberately bypass the shared
            # drawing-style library.
            weak_interaction_display_profile(self.method)
            self.style_snapshot = {}
        else:
            if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("style"), Mapping):
                raise ScientificWorkflowValidationError("请选择兼容的绘图方案。")
            style = dict(snapshot["style"])
            if str(style.get("surface_mode") or "signed") != self.spec.surface_mode:
                raise ScientificWorkflowValidationError("绘图方案与当前科学数据类型不兼容。")
            self.style_snapshot = copy.deepcopy(dict(snapshot))
        self.event_callback = event_callback
        self._cancelled = threading.Event()
        self._process: subprocess.Popen | None = None

    def cancel(self) -> None:
        self._cancelled.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _emit(self, progress: float, message: str, **extra: object) -> None:
        if self.event_callback is not None:
            self.event_callback(
                {"type": "progress", "progress": max(0.0, min(100.0, progress)), "message": message, **extra}
            )

    def _run_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        stdin_text: str | None,
        log_path: Path,
        timeout: int,
        base_progress: float,
        progress_span: float,
    ) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = os.environ.copy()
        env["Multiwfnpath"] = str(self.multiwfn_exe.parent)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            self._process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            if stdin_text is not None and self._process.stdin is not None:
                self._process.stdin.write(stdin_text)
                self._process.stdin.close()
            last_percent = -1.0
            last_heartbeat = -1
            while self._process.poll() is None:
                if self._cancelled.is_set():
                    self._process.terminate()
                    raise ScientificWorkflowError("任务已取消。")
                if time.monotonic() - started > timeout:
                    self._process.terminate()
                    raise ScientificWorkflowError("计算超时，已停止当前进程。")
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="ignore")[-12000:]
                    matches = re.findall(r"(\d+(?:\.\d+)?)\s*%", tail)
                    if matches:
                        percent = min(100.0, float(matches[-1]))
                        if percent != last_percent:
                            last_percent = percent
                            self._emit(base_progress + progress_span * percent / 100.0, "Multiwfn 正在计算空间网格")
                    else:
                        elapsed_seconds = int(time.monotonic() - started)
                        if elapsed_seconds != last_heartbeat:
                            last_heartbeat = elapsed_seconds
                            reference = min(0.86, elapsed_seconds / 180.0)
                            self._emit(
                                base_progress + progress_span * reference,
                                "Multiwfn 正在分析并生成数据",
                            )
                except OSError:
                    pass
                time.sleep(0.12)
            return_code = self._process.returncode
            self._process = None
        if return_code != 0:
            raise ScientificWorkflowError(f"外部程序运行失败（退出码 {return_code}），请查看日志。")

    def _run_multiwfn(
        self,
        wavefunction: Path,
        sequence: str,
        work: Path,
        log: Path,
        *,
        base_progress: float = 5.0,
        progress_span: float = 58.0,
    ) -> None:
        _write_text(work / "multiwfn_input.txt", sequence)
        self._run_process(
            [str(self.multiwfn_exe), str(wavefunction), "-isilent", "1"],
            cwd=work,
            stdin_text=sequence,
            log_path=log,
            timeout=max(60, int(self.options.get("multiwfn_timeout_seconds") or 3600)),
            base_progress=base_progress,
            progress_span=progress_span,
        )

    def _generate_nto_cubes(self, nto_file: Path, work: Path, log_dir: Path) -> dict[str, Path]:
        dataset = orbital_data.parse_wavefunction_file(nto_file)
        occupied = sorted(
            (orb for orb in dataset.orbitals if orb.occupation > 0 and orb.energy_hartree > 1.0e-10),
            key=lambda orb: orb.energy_hartree,
            reverse=True,
        )
        virtual = sorted(
            (orb for orb in dataset.orbitals if orb.occupation <= 0 and orb.energy_hartree > 1.0e-10),
            key=lambda orb: orb.energy_hartree,
            reverse=True,
        )
        pair_count = max(1, min(10, int(self.options.get("nto_pairs") or 1)))
        selected = [*occupied[:pair_count], *virtual[:pair_count]]
        if not selected:
            raise ScientificWorkflowError("NTO 文件中没有可绘制的主导轨道。")
        indices = ",".join(str(orb.global_index) for orb in selected)
        grid = max(1, min(3, int(self.options.get("grid_quality") or 2)))
        sequence = f"200\n3\n{indices}\n{grid}\n1\n0\nq\n"
        self._run_multiwfn(
            nto_file,
            sequence,
            work,
            log_dir / "multiwfn_nto_cubes.log",
            base_progress=65.0,
            progress_span=8.0,
        )
        cubes: dict[str, Path] = {}
        for number, orb in enumerate(selected, 1):
            path = work / f"orb{orb.global_index:06d}.cub"
            if not path.is_file():
                raise ScientificWorkflowError(f"缺少 NTO Cube：轨道 {orb.global_index}")
            side = "hole" if orb.occupation > 0 else "electron"
            cubes[f"NTO_{number:02d}_{side}.cub"] = path
        return cubes

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _capture_weak_interaction_view(
        self,
        surface_cube: Path,
        color_cube: Path,
        work: Path,
        log_dir: Path,
    ) -> tuple[orbital_vmd.VmdViewState, Path]:
        """Open the exact Multiwfn weak-interaction scene for free editing."""
        protocol = work / "weak_interaction_view.capture"
        native_state = work / "weak_interaction_final_state.vmd"
        cancel_marker = orbital_vmd.capture_cancel_marker_path(protocol)
        error_log = orbital_vmd.capture_error_log_path(protocol)
        for path in (protocol, native_state, cancel_marker, error_log):
            path.unlink(missing_ok=True)
        initial_scene = build_weak_interaction_scene_tcl(
            self.method, surface_cube, color_cube
        )
        capture_script = orbital_vmd.build_interactive_capture_tcl(
            color_cube,
            protocol,
            {},
            width=1160,
            height=640,
            debug_state_path=native_state,
            initial_scene_tcl=initial_scene,
        )
        script_path = work / "adjust_weak_interaction_view.vmd"
        _write_text(script_path, capture_script)
        log_path = log_dir / "vmd_adjust_view.log"
        self._emit(
            76,
            "VMD 已打开：可自由调整弱相互作用等值面、角度与显示效果，确认后再渲染",
        )

        existing_windows = orbital_vmd.vmd_display_window_handles()
        encoding = locale.getpreferredencoding(False) or "utf-8"
        process = subprocess.Popen(
            [str(self.vmd_exe), "-e", str(script_path)],
            cwd=str(work),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=encoding,
            errors="replace",
        )
        self._process = process
        assert process.stdout is not None
        output_queue: queue.Queue[object] = queue.Queue()
        sentinel = object()

        def read_output() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(sentinel)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        timeout = max(60, int(self.options.get("vmd_timeout_seconds") or 3600))
        ready_at: float | None = None
        next_window_check = started
        window_restored = False
        stream_finished = False
        reason = ""
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                while process.poll() is None or not stream_finished:
                    now = time.monotonic()
                    if (
                        ready_at is not None
                        and not window_restored
                        and now >= next_window_check
                        and now - ready_at <= 20.0
                    ):
                        # Retry only until the newly created window is found.
                        # Once restored successfully it is never repositioned,
                        # so the user remains free to move or resize it.
                        window_restored = orbital_vmd.restore_vmd_display_window(
                            process.pid,
                            excluded_handles=existing_windows,
                            width=1180,
                            height=700,
                            topmost=False,
                        )
                        next_window_check = now + 0.7
                    try:
                        item = output_queue.get(timeout=0.1)
                    except queue.Empty:
                        item = None
                    if item is sentinel:
                        stream_finished = True
                    elif isinstance(item, str):
                        log.write(item)
                        log.flush()
                        if "MolecularStudio: adjust the scene" in item:
                            ready_at = now
                            next_window_check = now
                    if self._cancelled.is_set() and process.poll() is None:
                        reason = "cancelled"
                        self._terminate_process(process)
                    elif not reason and protocol.is_file():
                        reason = "confirmed"
                        if process.poll() is None:
                            self._terminate_process(process)
                    elif not reason and cancel_marker.is_file():
                        reason = "cancelled"
                        if process.poll() is None:
                            self._terminate_process(process)
                    elif not reason and now - started > timeout:
                        reason = "timeout"
                        if process.poll() is None:
                            self._terminate_process(process)
                reader.join(timeout=1.0)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            self._process = None

        if reason == "cancelled":
            raise ScientificWorkflowError("VMD 调整已取消。")
        if reason == "timeout":
            raise ScientificWorkflowError("等待 VMD 调整确认超时，Cube 已保留。")
        if not protocol.is_file():
            detail = f"请查看 {error_log.name}。" if error_log.is_file() else ""
            raise ScientificWorkflowError(f"没有取得已确认的 VMD 显示参数。{detail}")
        if not native_state.is_file():
            raise ScientificWorkflowError("VMD 未保存完整场景，无法保证最终图片与调整结果一致。")
        state = orbital_vmd.load_view_state(
            protocol,
            expected_geometry_fingerprint=orbital_vmd.cube_geometry_fingerprint(
                color_cube
            ),
        )
        state.save_json(work / "weak_interaction_viewpoint.json")
        self._emit(86, "VMD 参数已确认，正在使用 Tachyon 渲染")
        return state, native_state

    def _render_weak_interaction(
        self,
        label: str,
        surface_cube: Path,
        color_cube: Path,
        work: Path,
        root: Path,
        log_dir: Path,
    ) -> Path:
        state, native_state = self._capture_weak_interaction_view(
            surface_cube, color_cube, work, log_dir
        )
        scene_output = work / f"{_clean_part(label)}_render.dat"
        render_script = orbital_vmd.build_batch_render_tcl(
            color_cube,
            scene_output,
            state,
            width=max(640, int(self.options.get("width") or 1400)),
            height=max(480, int(self.options.get("height") or 1050)),
            renderer="Tachyon",
            native_state_path=native_state,
            reference_cube_path=color_cube,
            # VMD 1.9.3 reports a stale global color-scale window and RGB-slot
            # table for these volume-colored isosurfaces. The visible palette
            # and per-representation range are already authoritative in the
            # native save_state; replaying the stale normalized values would
            # turn green interaction zones red after confirmation.
            restore_exact_color_slots=False,
            restore_color_slots=False,
            restore_color_scale_window=False,
        )
        script_path = work / "render_weak_interaction.vmd"
        _write_text(script_path, render_script)
        self._run_process(
            [str(self.vmd_exe), "-dispdev", "text", "-eofexit", "-e", str(script_path)],
            cwd=work,
            stdin_text=None,
            log_path=log_dir / f"vmd_{_clean_part(label)}.log",
            timeout=max(60, int(self.options.get("vmd_timeout_seconds") or 900)),
            base_progress=87,
            progress_span=8,
        )
        raw = Path(str(scene_output) + ".bmp")
        if not raw.is_file() or raw.stat().st_size <= 64:
            raise ScientificWorkflowError(f"VMD 未生成 {label} 的 Tachyon 渲染文件。")
        png = _unique_path(root / f"{_clean_part(label)}.png")
        with Image.open(raw) as image:
            image.convert("RGB").save(png, format="PNG", optimize=True)
        return png

    def _render(self, label: str, surface_cube: Path, color_cube: Path | None, iso: float, work: Path, root: Path, log_dir: Path) -> Path:
        style = copy.deepcopy(dict(self.style_snapshot["style"]))
        rep0 = list(self.style_snapshot.get("rep0_commands") or [])
        style["default_iso_value"] = float(iso)
        script = vmd_core.build_vmd_tcl(style, rep0)
        if color_cube is not None and self.spec.surface_mode == "signed":
            secondary = color_cube.resolve().as_posix()
            negative_color = max(0, min(32, int(style.get("neg_color", 0))))
            material = re.sub(r"[^A-Za-z0-9_.-]", "", str(style.get("material") or "Glossy")) or "Glossy"
            script += "\n".join(
                [
                    f"mol addfile {{{secondary}}} type cube waitfor all",
                    "mol addrep top",
                    f"mol modstyle 3 top Isosurface $AUTO_ISOVAL 1 0 0 1 1",
                    f"mol modcolor 3 top ColorID {negative_color}",
                    f"mol modmaterial 3 top {material}",
                    "",
                ]
            )
        raw_name = f"{_clean_part(label)}.tga"
        script += "\n".join(
            [
                f"display resize {max(640, int(self.options.get('width') or 1400))} {max(480, int(self.options.get('height') or 1050))}",
                "display update",
                f"render TachyonInternal {{{raw_name}}}",
                "quit",
                "",
            ]
        )
        tcl_path = work / f"render_{_clean_part(label)}.tcl"
        _write_text(tcl_path, script)
        env = os.environ.copy()
        env.update(
            {
                "CUBE_FILE": str(surface_cube),
                "COLOR_CUBE_FILE": str(color_cube or ""),
                "ISO_NORM": format(float(iso), ".12g"),
                "A_DIR": str(work),
            }
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log_path = log_dir / f"vmd_{_clean_part(label)}.log"
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            completed = subprocess.run(
                [str(self.vmd_exe), "-dispdev", "text", "-eofexit", "-e", str(tcl_path)],
                cwd=str(work),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=max(60, int(self.options.get("vmd_timeout_seconds") or 900)),
                creationflags=creationflags,
                check=False,
            )
        if completed.returncode != 0:
            raise ScientificWorkflowError(f"VMD 渲染 {label} 失败，请查看日志。")
        raw = work / raw_name
        if not raw.is_file():
            candidates = sorted(work.glob(f"{Path(raw_name).stem}*.tga"), key=lambda item: item.stat().st_mtime_ns)
            raw = candidates[-1] if candidates else raw
        if not raw.is_file():
            raise ScientificWorkflowError(f"VMD 未生成 {label} 的渲染文件。")
        png = _unique_path(root / f"{_clean_part(label)}.png")
        with Image.open(raw) as image:
            image.convert("RGB").save(png, format="PNG", optimize=True)
        return png

    def run(self) -> dict:
        self.output_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # VMD 1.9.3 on Windows cannot reliably open Tcl/Cube paths containing
        # non-ASCII characters.  Keep the generated task folder ASCII even
        # though all user-facing labels remain Chinese.
        run_dir = _unique_path(self.output_root / f"{self.workflow_id}_{stamp}")
        staging = (
            Path(tempfile.gettempdir())
            / "MolecularStudioScientific"
            / f"{self.workflow_id}_{uuid.uuid4().hex[:10]}"
        )
        work = staging / "process"
        cube_dir = run_dir / "cubes"
        log_dir = staging / "logs"
        final_log_dir = run_dir / "logs"
        for directory in (work, cube_dir, log_dir, final_log_dir):
            directory.mkdir(parents=True, exist_ok=True)

        def collect_logs() -> None:
            for source in log_dir.glob("*"):
                if source.is_file():
                    shutil.copy2(source, _unique_path(final_log_dir / source.name))
        started = time.monotonic()
        result: dict[str, object] = {
            "workflow_id": self.workflow_id,
            "workflow_name": self.spec.name,
            "method": self.method,
            "run_dir": str(run_dir),
            "status": "running",
            "images": [],
            "cubes": [],
        }
        try:
            self._emit(2, "正在准备计算输入")
            primary = self.inputs.get("wavefunction") or self.inputs.get("neutral")
            if primary is None:
                raise ScientificWorkflowValidationError("缺少主波函数文件。")
            if self.workflow_id == WORKFLOW_DEFORMATION and self.method == "deformation":
                atomwfn_source = self.multiwfn_exe.parent / "examples" / "atomwfn"
                if not atomwfn_source.is_dir() or not any(atomwfn_source.glob("*.wfn")):
                    raise ScientificWorkflowValidationError(
                        "Multiwfn 目录中缺少 examples\\atomwfn，无法计算变形密度。"
                    )
                # Multiwfn looks for atomwfn in the calculation working folder.
                # Copying its bundled spherical free-atom wavefunctions also
                # prevents an unexpected attempt to invoke Gaussian.
                shutil.copytree(atomwfn_source, work / "atomwfn")
            nto_file = work / f"NTO_state{max(1, int(self.options.get('excited_state') or 1))}.fch"
            sequence = build_multiwfn_sequence(
                self.workflow_id,
                self.method,
                self.inputs,
                self.options,
                nto_output=nto_file,
            )
            self._run_multiwfn(primary, sequence, work, log_dir / "multiwfn.log")
            self._emit(65, "正在检查 Multiwfn 输出")
            if self.workflow_id == WORKFLOW_EXCITED and self.method == "nto":
                if not nto_file.is_file():
                    raise ScientificWorkflowError("Multiwfn 未生成预期的 NTO 波函数文件。")
                shutil.copy2(nto_file, run_dir / nto_file.name)
                cubes = self._generate_nto_cubes(nto_file, work, log_dir)
            else:
                cubes = {}
                for name in _expected_products(self.workflow_id, self.method):
                    path = work / name
                    if not path.is_file() or path.stat().st_size <= 64:
                        raise ScientificWorkflowError(f"Multiwfn 未生成预期文件：{name}")
                    cubes[name] = path
            self._emit(73, "正在用 VMD 与 Tachyon 生成图片")
            render_pairs = _render_pairs(
                self.workflow_id, self.method, cubes, self.options
            )
            images: list[str] = []
            total = max(1, len(render_pairs))
            for index, (label, surface, color, iso) in enumerate(render_pairs, 1):
                if self._cancelled.is_set():
                    raise ScientificWorkflowError("任务已取消。")
                if self.workflow_id == WORKFLOW_WEAK:
                    if color is None:
                        raise ScientificWorkflowError("弱相互作用着色场缺失。")
                    self._emit(74, "正在准备 Multiwfn 弱相互作用显示场景")
                    images.append(
                        str(
                            self._render_weak_interaction(
                                label, surface, color, work, run_dir, log_dir
                            )
                        )
                    )
                else:
                    self._emit(73 + 20 * (index - 1) / total, f"正在渲染 {label}")
                    images.append(str(self._render(label, surface, color, iso, work, run_dir, log_dir)))
            keep_cubes = bool(self.options.get("keep_cubes", True))
            collected: list[str] = []
            if keep_cubes:
                for name, source in cubes.items():
                    target = _unique_path(cube_dir / name)
                    shutil.copy2(source, target)
                    collected.append(str(target))
            self._emit(96, "正在整理结果")
            result.update(
                {
                    "status": "success",
                    "images": images,
                    "cubes": collected,
                    "duration_seconds": round(time.monotonic() - started, 2),
                }
            )
            _write_text(run_dir / "result.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            collect_logs()
            shutil.rmtree(staging, ignore_errors=True)
            self._emit(100, "流程已完成", result=result)
            return result
        except Exception as exc:
            result.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "duration_seconds": round(time.monotonic() - started, 2),
                }
            )
            _write_text(run_dir / "result.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            collect_logs()
            debug_dir = run_dir / "process_debug"
            if work.is_dir():
                diagnostics = [*work.glob("*.txt"), *work.glob("*.tcl")]
                if diagnostics:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    for source in diagnostics:
                        shutil.copy2(source, _unique_path(debug_dir / source.name))
            shutil.rmtree(staging, ignore_errors=True)
            self._emit(100, f"流程失败：{exc}", result=result)
            raise
