"""Shared execution core for the additional Multiwfn + VMD workflows.

The five user-facing workflow families are intentionally data-driven.  They
share one process runner, one result layout, one VMD renderer and one progress
event contract; only the documented Multiwfn menu sequence and expected
scientific products differ.
"""

from __future__ import annotations

import copy
import json
import os
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
import vmd_style_tool as vmd_core


WORKFLOW_WEAK = "weak_interaction"
WORKFLOW_FUKUI = "fukui_descriptor"
WORKFLOW_EXCITED = "excited_state_density"
WORKFLOW_SPIN = "spin_density"
WORKFLOW_LOCAL = "local_reactivity_surface"

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
        "volume_mapped",
        "IRI · RDG/NCI · IGMH · VMD",
    ),
    ScientificWorkflowSpec(
        WORKFLOW_FUKUI,
        "Fukui 与双描述符",
        "CDFT",
        "使用同一几何和计算水平的 N、N+1、N-1 波函数生成严格 CDFT 网格。",
        (("strict", "严格有限差分 CDFT"),),
        (
            ("neutral", "中性体系 N", WAVEFUNCTION_EXTENSIONS),
            ("cation", "N-1 体系", WAVEFUNCTION_EXTENSIONS),
            ("anion", "N+1 体系", WAVEFUNCTION_EXTENSIONS),
        ),
        "signed",
        "f+ · f− · f0 · 双描述符",
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
        WORKFLOW_LOCAL,
        "局域反应性表面",
        "ALIE",
        "生成 ALIE、LEA 或 LEAE 数据，并映射到对应电子密度等值面。",
        (("alie", "ALIE"), ("lea", "LEA"), ("leae", "LEAE")),
        (("wavefunction", "波函数文件", WAVEFUNCTION_EXTENSIONS),),
        "volume_mapped",
        "ALIE · LEA · LEAE · 表面映射",
    ),
)


def workflow_specs() -> tuple[ScientificWorkflowSpec, ...]:
    return _SPECS


def workflow_spec(workflow_id: str) -> ScientificWorkflowSpec:
    for spec in _SPECS:
        if spec.id == workflow_id:
            return spec
    raise ScientificWorkflowValidationError(f"未知的自动化流程：{workflow_id}")


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

    if workflow_id == WORKFLOW_FUKUI:
        return "\n".join(
            [
                "22",
                "3",
                str(inputs["neutral"]),
                str(inputs["anion"]),
                str(inputs["cation"]),
                str(grid),
                "5",
                "6",
                "7",
                "8",
                "0",
                "0",
                "q",
                "",
            ]
        )

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

    if workflow_id == WORKFLOW_LOCAL:
        if method == "alie":
            return f"5\n1\n{grid}\n2\n0\n5\n18\n{grid}\n2\n0\nq\n"
        function = "27" if method == "lea" else "-27" if method == "leae" else ""
        if not function:
            raise ScientificWorkflowValidationError("未知的局域反应性方法。")
        return f"1000\n2\n{function}\n5\n1\n{grid}\n2\n0\n5\n100\n{grid}\n2\n0\nq\n"

    raise ScientificWorkflowValidationError(f"未知的自动化流程：{workflow_id}")


def _expected_products(workflow_id: str, method: str) -> tuple[str, ...]:
    if workflow_id == WORKFLOW_WEAK:
        return ("sl2r.cub", "dg_inter.cub", "dg_intra.cub", "dg.cub") if method == "igmh" else ("func1.cub", "func2.cub")
    if workflow_id == WORKFLOW_FUKUI:
        return ("f+.cub", "f-.cub", "f0.cub", "DD.cub")
    if workflow_id == WORKFLOW_EXCITED and method == "hole_electron":
        return ("hole.cub", "electron.cub", "CDD.cub")
    if workflow_id == WORKFLOW_SPIN:
        return ("spindensity.cub",)
    if workflow_id == WORKFLOW_LOCAL:
        return ("density.cub", "avglocion.cub" if method == "alie" else "userfunc.cub")
    return ()


def _render_pairs(workflow_id: str, method: str, cubes: Mapping[str, Path]) -> list[tuple[str, Path, Path | None, float]]:
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
    if workflow_id == WORKFLOW_LOCAL:
        mapped = "avglocion.cub" if method == "alie" else "userfunc.cub"
        iso = 0.0005 if method == "alie" else 0.004 if method == "leae" else 0.01
        return [(method.upper(), cubes["density.cub"], cubes[mapped], iso)]
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
        self.output_root = Path(output_root).expanduser().resolve()
        self.multiwfn_exe = Path(multiwfn_exe).expanduser().resolve()
        self.vmd_exe = Path(vmd_exe).expanduser().resolve()
        if not self.multiwfn_exe.is_file():
            raise ScientificWorkflowValidationError("Multiwfn.exe 路径无效。")
        if not self.vmd_exe.is_file():
            raise ScientificWorkflowValidationError("vmd.exe 路径无效。")
        snapshot = self.options.get("style_snapshot")
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

    def _render(self, label: str, surface_cube: Path, color_cube: Path | None, iso: float, work: Path, root: Path, log_dir: Path) -> Path:
        style = copy.deepcopy(dict(self.style_snapshot["style"]))
        rep0 = list(self.style_snapshot.get("rep0_commands") or [])
        style["default_iso_value"] = float(iso)
        # These ranges define the scientific mapping, not the cosmetic style.
        # They follow Multiwfn's bundled VMD examples; the selected scheme is
        # still authoritative for skeleton, material, lighting and geometry.
        mapped_ranges = {
            (WORKFLOW_WEAK, "iri"): ("BGR", -0.04, 0.02),
            (WORKFLOW_WEAK, "rdg"): ("BGR", -0.035, 0.02),
            (WORKFLOW_WEAK, "igmh"): ("BGR", -0.05, 0.05),
            (WORKFLOW_LOCAL, "alie"): ("BWR", 0.32, 0.36),
            (WORKFLOW_LOCAL, "lea"): ("BWR", -0.8, -0.3),
            (WORKFLOW_LOCAL, "leae"): ("BWR", -0.03, 0.0),
        }
        scientific_range = mapped_ranges.get((self.workflow_id, self.method))
        if color_cube is not None and scientific_range is not None:
            method, minimum, maximum = scientific_range
            style["color_scale_method"] = method
            style["color_scale_min"] = minimum
            style["color_scale_max"] = maximum
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
            render_pairs = _render_pairs(self.workflow_id, self.method, cubes)
            images: list[str] = []
            total = max(1, len(render_pairs))
            for index, (label, surface, color, iso) in enumerate(render_pairs, 1):
                if self._cancelled.is_set():
                    raise ScientificWorkflowError("任务已取消。")
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
