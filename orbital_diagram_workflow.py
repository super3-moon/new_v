"""Execution core for the automatic molecular-orbital energy diagram workflow.

The module intentionally has no Qt dependency.  A GUI may run
:class:`OrbitalDiagramRunner` in a worker thread and translate its dictionary
events to signals.  Scientific input parsing lives in :mod:`orbital_data` and
safe VMD view capture/replay lives in :mod:`orbital_vmd`; this module owns the
recoverable pipeline, process lifecycle, result collection and final diagram.
"""

from __future__ import annotations

import copy
import ctypes
import csv
import hashlib
import json
import locale
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import orbital_data
import orbital_vmd


WORKFLOW_ID = "orbital_energy_diagram"
MANIFEST_SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"

STAGE_PARSE = "parsing_inputs"
STAGE_RESOLVE = "resolving_orbitals"
STAGE_REFERENCE_CUBE = "generating_reference_cube"
STAGE_VIEWPOINT = "waiting_viewpoint"
STAGE_ORBITAL_CUBES = "generating_orbital_cubes"
STAGE_CUBE_VALIDATION = "validating_cubes"
STAGE_RENDER = "rendering_orbitals"
STAGE_COMPOSE = "composing_diagram"
STAGE_COLLECT = "collecting"

# The OpenGL window is an editing surface, not the requested Tachyon output.
# 1160x640 matches a comfortable, non-maximized VMD window on a 1080p desktop.
INTERACTIVE_VMD_VIEWPORT = (1160, 640)
INTERACTIVE_VMD_WINDOW = (1180, 700)

# Coarse milestones are intentionally weighted by the work users wait for,
# rather than by the number of Python functions in the pipeline.  The UI may
# gently advance between a milestone and its ceiling so a one-file run never
# looks frozen at 0% for several minutes.
STAGE_PROGRESS = {
    STAGE_PARSE: (2.0, 7.0),
    STAGE_RESOLVE: (7.0, 11.0),
    STAGE_REFERENCE_CUBE: (11.0, 23.0),
    STAGE_VIEWPOINT: (23.0, 34.0),
    STAGE_ORBITAL_CUBES: (34.0, 52.0),
    STAGE_CUBE_VALIDATION: (52.0, 58.0),
    STAGE_RENDER: (58.0, 89.0),
    STAGE_COMPOSE: (89.0, 96.0),
    STAGE_COLLECT: (96.0, 99.0),
}

RETRY_STAGES = {
    STAGE_PARSE,
    STAGE_RESOLVE,
    STAGE_REFERENCE_CUBE,
    STAGE_VIEWPOINT,
    STAGE_ORBITAL_CUBES,
    STAGE_CUBE_VALIDATION,
    STAGE_RENDER,
    STAGE_COMPOSE,
    STAGE_COLLECT,
}

STAGE_ORDER = (
    STAGE_PARSE,
    STAGE_RESOLVE,
    STAGE_REFERENCE_CUBE,
    STAGE_VIEWPOINT,
    STAGE_ORBITAL_CUBES,
    STAGE_CUBE_VALIDATION,
    STAGE_RENDER,
    STAGE_COMPOSE,
    STAGE_COLLECT,
)

# Earliest stage whose cached artifacts may be invalid after a setting changes.
# Timeout-only changes intentionally do not appear here because they do not
# invalidate an artifact.
SETTING_RETRY_STAGE = {
    "strict_pair_validation": STAGE_PARSE,
    # Resolution itself is always repeated on resume; only the selected
    # orbital Cube/render artifacts need invalidation.
    "selection_mode": STAGE_ORBITAL_CUBES,
    "start_offset": STAGE_ORBITAL_CUBES,
    "end_offset": STAGE_ORBITAL_CUBES,
    "spin_mode": STAGE_ORBITAL_CUBES,
    "selection_text": STAGE_ORBITAL_CUBES,
    "orbital_selections": STAGE_ORBITAL_CUBES,
    "grid_quality": STAGE_REFERENCE_CUBE,
    "iso_value": STAGE_VIEWPOINT,
    "style_snapshot": STAGE_VIEWPOINT,
    "view_state_paths": STAGE_VIEWPOINT,
    "width": STAGE_RENDER,
    "height": STAGE_RENDER,
    "diagram_width": STAGE_COMPOSE,
    "energy_unit": STAGE_COMPOSE,
    "energy_decimals": STAGE_COMPOSE,
    "title": STAGE_COMPOSE,
    "output_location": STAGE_COLLECT,
    "keep_cubes": STAGE_COLLECT,
}

EventCallback = Callable[[dict], None]


class OrbitalDiagramError(RuntimeError):
    """Base error safe to show in the application UI."""


class OrbitalDiagramValidationError(OrbitalDiagramError, ValueError):
    """Invalid workflow configuration, input pair, or resume record."""


class OrbitalDiagramDependencyError(OrbitalDiagramError):
    """A runtime component required for an output is unavailable."""


class _Cancelled(OrbitalDiagramError):
    pass


class _TimedOut(OrbitalDiagramError):
    pass


def _json_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def detect_energy_spacing_anomaly(
    orbitals: Sequence[Mapping[str, object] | orbital_data.OrbitalRef],
) -> dict[str, object] | None:
    """Detect one isolated frontier level that would dominate a diagram.

    Alpha/beta partners and ordinary near-degeneracies are clustered first.
    A warning is returned only when an endpoint cluster is separated from all
    remaining selected levels by both a sizeable absolute gap and a gap far
    larger than the normal spacings inside the remaining group.  This avoids
    warning merely because a molecule has a legitimate HOMO-LUMO gap.
    """

    points: list[tuple[float, str]] = []
    for item in orbitals:
        if isinstance(item, Mapping):
            raw_energy = item.get("energy_ev")
            label = str(item.get("label") or "未命名轨道")
        else:
            raw_energy = item.energy_ev
            label = item.label
        try:
            value = float(raw_energy)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            points.append((value, label))
    if len(points) < 3:
        return None

    clusters: list[list[tuple[float, str]]] = []
    for point in sorted(points, key=lambda entry: entry[0]):
        if clusters and abs(point[0] - sum(v for v, _ in clusters[-1]) / len(clusters[-1])) <= 0.03:
            clusters[-1].append(point)
        else:
            clusters.append([point])
    if len(clusters) < 3:
        return None

    centers = [sum(value for value, _ in group) / len(group) for group in clusters]
    gaps = [centers[index + 1] - centers[index] for index in range(len(centers) - 1)]
    largest_index = max(range(len(gaps)), key=gaps.__getitem__)
    largest_gap = gaps[largest_index]
    isolated_low = largest_index == 0
    isolated_high = largest_index == len(clusters) - 2
    if not (isolated_low or isolated_high):
        return None
    ordinary = sorted(gap for index, gap in enumerate(gaps) if index != largest_index and gap > 1.0e-9)
    if ordinary:
        midpoint = len(ordinary) // 2
        baseline = (
            ordinary[midpoint]
            if len(ordinary) % 2
            else (ordinary[midpoint - 1] + ordinary[midpoint]) / 2.0
        )
        threshold = max(3.0, baseline * 4.0)
    else:
        threshold = 12.0
    if largest_gap < threshold:
        return None

    isolated_cluster = clusters[0] if isolated_low else clusters[-1]
    neighbor_cluster = clusters[1] if isolated_low else clusters[-2]
    return {
        "gap_ev": largest_gap,
        "isolated_energy_ev": centers[0] if isolated_low else centers[-1],
        "neighbor_energy_ev": centers[1] if isolated_low else centers[-2],
        "isolated_labels": [label for _value, label in isolated_cluster],
        "neighbor_labels": [label for _value, label in neighbor_cluster],
        "direction": "low" if isolated_low else "high",
    }


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _clean_part(value: object, fallback: str = "molecule") -> str:
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return (text or fallback)[:100]


def _orbital_artifact_stem(ref: orbital_data.OrbitalRef) -> str:
    """Return a stable ASCII-only name for files passed to VMD 1.9.3.

    The Windows build of VMD 1.9.3 opens the ``-e`` script argument through
    the active ANSI code page.  A scientifically useful display label such as
    ``α-HOMO-1`` therefore cannot safely be used as a script or render
    filename: VMD either rejects the existing script or writes the image under
    a different, mojibake name.  Spin plus both orbital indices are unique
    within a job and keep these implementation filenames readable without
    leaking the display label into the legacy command line.
    """
    spin = re.sub(r"[^a-z0-9]+", "_", _enum_value(ref.spin).casefold()).strip("_")
    spin = spin or "orbital"
    return f"{spin}_{int(ref.channel_index):06d}_{int(ref.global_index):06d}"


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    number = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _orbital_key(value: orbital_data.OrbitalRef | Mapping[str, object]) -> str:
    spin = _enum_value(
        value.spin if isinstance(value, orbital_data.OrbitalRef) else value.get("spin", "spatial")
    )
    index = int(
        value.global_index
        if isinstance(value, orbital_data.OrbitalRef)
        else value.get("global_index", value.get("multiwfn_index", 0))
    )
    return f"{spin}:{index}"


def _fallback_cube_for_index(directory: Path, global_index: int) -> Path | None:
    """Return a fallback Cube whose filename encodes exactly *global_index*.

    Multiwfn normally emits ``orb000017.cub``.  A few builds append a suffix,
    so a conservative fallback is useful, but substring globs are unsafe:
    orbital 17 must never silently consume orbital 117.
    """

    pattern = re.compile(r"^orb0*(\d+)(?:\D.*)?\.cub$", re.IGNORECASE)
    matches: list[Path] = []
    for candidate in directory.glob("orb*.cub"):
        match = pattern.fullmatch(candidate.name)
        if match is not None and int(match.group(1)) == int(global_index):
            matches.append(candidate)
    return sorted(matches)[-1] if matches else None


def _ref_to_dict(ref: orbital_data.OrbitalRef) -> dict:
    return dict(ref.to_dict())


def _coerce_pair(value: orbital_data.InputPair | Mapping[str, object]) -> orbital_data.InputPair:
    if isinstance(value, orbital_data.InputPair):
        return value
    if not isinstance(value, Mapping):
        raise OrbitalDiagramValidationError("每个任务都必须包含计算输出与波函数文件。")
    output = Path(str(value.get("output_path") or value.get("output") or "")).expanduser()
    wavefunction = Path(
        str(value.get("wavefunction_path") or value.get("wavefunction") or "")
    ).expanduser()
    if not output.is_file() or not wavefunction.is_file():
        raise OrbitalDiagramValidationError("计算输出或波函数文件不存在。")
    program = value.get("program")
    if not program:
        program = orbital_data.parse_output_file(output).program
    return orbital_data.InputPair(
        output_path=output,
        wavefunction_path=wavefunction,
        program=program,
        label=str(value.get("label") or ""),
        pairing_reason=str(value.get("pairing_reason") or "manual pairing"),
        warnings=tuple(str(item) for item in value.get("warnings", ()) or ()),
    )


@dataclass(slots=True)
class OrbitalDiagramSettings:
    """Serializable settings shared by input preview, runner, and manifest."""

    selection_mode: str = "preset"
    start_offset: int = -1
    end_offset: int = 3
    spin_mode: str = "auto"
    selection_text: str = ""
    grid_quality: int = 2
    iso_value: float = 0.05
    style_snapshot: dict = field(default_factory=dict)
    width: int = 960
    height: int = 720
    diagram_width: int = 1800
    energy_unit: str = "eV"
    energy_decimals: int = 2
    title: str = "Molecular orbital energy diagram"
    output_location: str = "result_root"
    keep_cubes: bool = True
    strict_pair_validation: bool = True
    multiwfn_timeout_seconds: int = 3600
    viewpoint_timeout_seconds: int = 86400
    vmd_timeout_seconds: int = 900
    view_state_paths: dict[str, str] = field(default_factory=dict)
    orbital_selections: list[dict] = field(default_factory=list)

    @classmethod
    def from_value(cls, value: "OrbitalDiagramSettings | Mapping[str, object]") -> "OrbitalDiagramSettings":
        if isinstance(value, cls):
            result = copy.deepcopy(value)
        elif isinstance(value, Mapping):
            known = cls.__dataclass_fields__
            result = cls(**{key: copy.deepcopy(item) for key, item in value.items() if key in known})
        else:
            raise OrbitalDiagramValidationError("分子轨道能级图设置格式无效。")
        return result.validate()

    def validate(self) -> "OrbitalDiagramSettings":
        self.selection_mode = str(self.selection_mode or "preset")
        self.start_offset = max(-5000, min(5000, int(self.start_offset)))
        self.end_offset = max(-5000, min(5000, int(self.end_offset)))
        self.spin_mode = str(self.spin_mode or "auto")
        self.selection_text = str(self.selection_text or "")
        self.grid_quality = max(1, min(4, int(self.grid_quality)))
        self.iso_value = float(self.iso_value)
        if not math.isfinite(self.iso_value) or self.iso_value <= 0:
            raise OrbitalDiagramValidationError("轨道等值面必须是大于零的有限数字。")
        self.width = max(320, min(7680, int(self.width)))
        self.height = max(240, min(4320, int(self.height)))
        self.diagram_width = max(900, min(12000, int(self.diagram_width)))
        self.energy_unit = "Hartree" if str(self.energy_unit).casefold() in {"hartree", "au", "a.u."} else "eV"
        self.energy_decimals = max(0, min(8, int(self.energy_decimals)))
        self.title = str(self.title or "Molecular orbital energy diagram").strip()
        if self.output_location not in {"result_root", "input_directory"}:
            raise OrbitalDiagramValidationError("未知的结果保存位置。")
        self.multiwfn_timeout_seconds = max(30, min(172800, int(self.multiwfn_timeout_seconds)))
        self.viewpoint_timeout_seconds = max(60, min(604800, int(self.viewpoint_timeout_seconds)))
        self.vmd_timeout_seconds = max(30, min(86400, int(self.vmd_timeout_seconds)))
        if not isinstance(self.style_snapshot, dict):
            raise OrbitalDiagramValidationError("请选择轨道等值面绘图方案。")
        style = self.style_snapshot.get("style", self.style_snapshot)
        if not isinstance(style, dict):
            raise OrbitalDiagramValidationError("绘图方案缺少风格参数。")
        style = copy.deepcopy(style)
        if str(style.get("surface_mode") or "signed") != "signed":
            raise OrbitalDiagramValidationError("分子轨道只能使用正负相位等值面方案。")
        style["surface_mode"] = "signed"
        style["default_iso_value"] = self.iso_value
        if "style" in self.style_snapshot:
            self.style_snapshot = copy.deepcopy(self.style_snapshot)
            self.style_snapshot["style"] = style
        else:
            self.style_snapshot = {"style": style, "rep0_commands": []}
        self.view_state_paths = {
            str(key): str(value)
            for key, value in dict(self.view_state_paths or {}).items()
            if str(key) and str(value)
        }
        self.orbital_selections = [
            copy.deepcopy(item)
            for item in list(self.orbital_selections or [])
            if isinstance(item, dict)
        ]
        return self

    @property
    def style(self) -> dict:
        return copy.deepcopy(self.style_snapshot["style"])

    @property
    def rep0_commands(self) -> list[str]:
        return [str(item) for item in self.style_snapshot.get("rep0_commands", []) or []]

    def to_dict(self) -> dict:
        return {
            key: copy.deepcopy(getattr(self, key))
            for key in self.__dataclass_fields__
        }


@dataclass(slots=True)
class OrbitalDiagramJob:
    id: str
    index: int
    pair: orbital_data.InputPair
    work_dir: Path
    result_dir: Path
    status: str = STATUS_PENDING
    stage: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    failed_stage: str = ""
    validation: dict = field(default_factory=dict)
    dataset: dict = field(default_factory=dict)
    orbitals: list[dict] = field(default_factory=list)
    reference_orbital: dict = field(default_factory=dict)
    reference_cube: str = ""
    cubes: dict[str, str] = field(default_factory=dict)
    cube_status: dict[str, str] = field(default_factory=dict)
    viewpoint_path: str = ""
    vmd_save_state_path: str = ""
    viewpoint_status: str = STATUS_PENDING
    images: dict[str, str] = field(default_factory=dict)
    render_status: dict[str, str] = field(default_factory=dict)
    diagram_path: str = ""
    outputs: list[str] = field(default_factory=list)
    can_retry: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "index": self.index,
            "pair": self.pair.to_dict(),
            "work_dir": str(self.work_dir),
            "result_dir": str(self.result_dir),
            "status": self.status,
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
            "failed_stage": self.failed_stage,
            "validation": copy.deepcopy(self.validation),
            "dataset": copy.deepcopy(self.dataset),
            "orbitals": copy.deepcopy(self.orbitals),
            "reference_orbital": copy.deepcopy(self.reference_orbital),
            "reference_cube": self.reference_cube,
            "cubes": dict(self.cubes),
            "cube_status": dict(self.cube_status),
            "viewpoint_path": self.viewpoint_path,
            "vmd_save_state_path": self.vmd_save_state_path,
            "viewpoint_status": self.viewpoint_status,
            "images": dict(self.images),
            "render_status": dict(self.render_status),
            "diagram_path": self.diagram_path,
            "outputs": list(self.outputs),
            "can_retry": list(self.can_retry),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "OrbitalDiagramJob":
        pair = _coerce_pair(raw.get("pair") if isinstance(raw.get("pair"), Mapping) else {})
        return cls(
            id=str(raw.get("id") or f"job_{uuid.uuid4().hex[:8]}"),
            index=int(raw.get("index") or 1),
            pair=pair,
            work_dir=Path(str(raw.get("work_dir") or "")).resolve(),
            result_dir=Path(str(raw.get("result_dir") or "")).resolve(),
            status=str(raw.get("status") or STATUS_PENDING),
            stage=str(raw.get("stage") or ""),
            started_at=str(raw.get("started_at") or ""),
            finished_at=str(raw.get("finished_at") or ""),
            duration_seconds=float(raw.get("duration_seconds") or 0),
            error=str(raw.get("error") or ""),
            failed_stage=str(raw.get("failed_stage") or ""),
            validation=dict(raw.get("validation") or {}),
            dataset=dict(raw.get("dataset") or {}),
            orbitals=[dict(item) for item in raw.get("orbitals", []) or []],
            reference_orbital=dict(raw.get("reference_orbital") or {}),
            reference_cube=str(raw.get("reference_cube") or ""),
            cubes={str(k): str(v) for k, v in dict(raw.get("cubes") or {}).items()},
            cube_status={str(k): str(v) for k, v in dict(raw.get("cube_status") or {}).items()},
            viewpoint_path=str(raw.get("viewpoint_path") or ""),
            vmd_save_state_path=str(raw.get("vmd_save_state_path") or ""),
            viewpoint_status=str(raw.get("viewpoint_status") or STATUS_PENDING),
            images={str(k): str(v) for k, v in dict(raw.get("images") or {}).items()},
            render_status={str(k): str(v) for k, v in dict(raw.get("render_status") or {}).items()},
            diagram_path=str(raw.get("diagram_path") or ""),
            outputs=[str(item) for item in raw.get("outputs", []) or []],
            can_retry=[str(item) for item in raw.get("can_retry", []) or []],
        )


@dataclass(slots=True)
class OrbitalDiagramPlan:
    id: str
    created_at: str
    run_dir: Path
    results_dir: Path
    settings: OrbitalDiagramSettings
    jobs: list[OrbitalDiagramJob]
    status: str = STATUS_PENDING
    resume: bool = False
    retry_stages: set[str] = field(default_factory=set)
    retry_job_ids: set[str] = field(default_factory=set)

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def summary_path(self) -> Path:
        return self.run_dir / "summary.csv"

    def to_dict(self) -> dict:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "workflow": {"id": WORKFLOW_ID, "name": "分子轨道能级图", "engine": "Multiwfn + VMD"},
            "id": self.id,
            "created_at": self.created_at,
            "run_dir": str(self.run_dir),
            "results_dir": str(self.results_dir),
            "status": self.status,
            "settings": self.settings.to_dict(),
            "jobs": [job.to_dict() for job in self.jobs],
        }


def create_orbital_diagram_plan(
    pairs: Iterable[orbital_data.InputPair | Mapping[str, object]],
    output_root: Path | str,
    settings: OrbitalDiagramSettings | Mapping[str, object],
    *,
    prefix: str = "orbital_diagram",
) -> OrbitalDiagramPlan:
    """Validate paths and create an isolated, not-yet-written execution plan."""

    normalized = OrbitalDiagramSettings.from_value(settings)
    unique: dict[tuple[str, str], orbital_data.InputPair] = {}
    for raw in pairs:
        pair = _coerce_pair(raw)
        if not pair.output_path.is_file() or not pair.wavefunction_path.is_file():
            raise OrbitalDiagramValidationError(f"输入文件不存在：{pair.label}")
        key = (
            os.path.normcase(str(pair.output_path)),
            os.path.normcase(str(pair.wavefunction_path)),
        )
        unique.setdefault(key, pair)
    if not unique:
        raise OrbitalDiagramValidationError("没有可运行的输出文件/波函数文件配对。")
    root = Path(output_root).expanduser().resolve()
    plan_id = uuid.uuid4().hex[:10]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = root / f"{_clean_part(prefix, 'orbital_diagram')}_{timestamp}_{plan_id[:6]}"
    results = run_dir / "results"
    jobs: list[OrbitalDiagramJob] = []
    for index, pair in enumerate(unique.values(), 1):
        label = _clean_part(pair.label or pair.wavefunction_path.stem)
        result_dir = pair.wavefunction_path.parent if normalized.output_location == "input_directory" else results
        jobs.append(
            OrbitalDiagramJob(
                id=f"job_{index:04d}_{uuid.uuid4().hex[:6]}",
                index=index,
                pair=pair,
                work_dir=run_dir / "jobs" / f"{index:04d}_{label}",
                result_dir=result_dir,
            )
        )
    return OrbitalDiagramPlan(
        id=plan_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        run_dir=run_dir,
        results_dir=results,
        settings=normalized,
        jobs=jobs,
    )


def inspect_orbital_pair(
    pair: orbital_data.InputPair | Mapping[str, object],
    settings: OrbitalDiagramSettings | Mapping[str, object],
    *,
    strict: bool | None = None,
) -> tuple[orbital_data.OrbitalDataset, list[orbital_data.OrbitalRef]]:
    """Read-only helper for a configuration-page preview."""

    item = _coerce_pair(pair)
    normalized = OrbitalDiagramSettings.from_value(settings)
    dataset = orbital_data.parse_input_pair(
        item.output_path,
        item.wavefunction_path,
        strict=normalized.strict_pair_validation if strict is None else bool(strict),
    )
    refs = orbital_data.resolve_orbital_selection(
        dataset,
        mode=normalized.selection_mode,
        start_offset=normalized.start_offset,
        end_offset=normalized.end_offset,
        spin_mode=normalized.spin_mode,
        text=normalized.selection_text or None,
    )
    if not refs:
        raise OrbitalDiagramValidationError("轨道选择没有得到任何可绘制轨道。")
    return dataset, refs


def resume_orbital_diagram_plan(
    manifest_path: Path | str,
    *,
    retry_stages: Iterable[str] | None = None,
    settings: OrbitalDiagramSettings | Mapping[str, object] | None = None,
    job_ids: Iterable[str] | None = None,
) -> OrbitalDiagramPlan:
    """Resume a manifest, preserving valid Cube/render artifacts by default."""

    manifest = Path(manifest_path).expanduser().resolve()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrbitalDiagramValidationError(f"无法读取运行记录：{exc}") from exc
    if str((payload.get("workflow") or {}).get("id") or "") != WORKFLOW_ID:
        raise OrbitalDiagramValidationError("运行记录不属于分子轨道能级图流程。")
    stored = OrbitalDiagramSettings.from_value(payload.get("settings") or {})
    requested = (
        OrbitalDiagramSettings.from_value(settings)
        if settings is not None
        else copy.deepcopy(stored)
    )
    requested_stages = {str(item) for item in retry_stages or ()}
    requested_jobs = {str(item) for item in job_ids or () if str(item)}
    unknown = requested_stages - RETRY_STAGES
    if unknown:
        raise OrbitalDiagramValidationError("未知的重试阶段：" + "、".join(sorted(unknown)))
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise OrbitalDiagramValidationError("运行记录中没有任务。")
    jobs = [OrbitalDiagramJob.from_dict(item) for item in raw_jobs if isinstance(item, Mapping)]
    known_job_ids = {job.id for job in jobs}
    if requested_jobs - known_job_ids:
        raise OrbitalDiagramValidationError("重试任务已不在运行记录中。")

    stored_values = stored.to_dict()
    requested_values = requested.to_dict()
    changed_settings = {
        key
        for key in stored_values
        if _json_hash(stored_values[key]) != _json_hash(requested_values[key])
    }
    if changed_settings and requested_jobs and requested_jobs != known_job_ids:
        raise OrbitalDiagramValidationError(
            "断点设置属于整批任务；修改设置时必须重试全部任务，不能只更新其中一部分。"
        )
    invalidating_stages = {
        SETTING_RETRY_STAGE[key]
        for key in changed_settings
        if key in SETTING_RETRY_STAGE
    }
    if invalidating_stages:
        requested_stages.add(min(invalidating_stages, key=STAGE_ORDER.index))

    for job in jobs:
        selected_job = not requested_jobs or job.id in requested_jobs
        if selected_job and (requested_stages or job.status != STATUS_SUCCESS):
            job.status = STATUS_PENDING
            job.error = ""
            job.failed_stage = ""
            job.can_retry = []
        # Unselected jobs retain their exact status and artifact metadata.  The
        # runner uses retry_job_ids to skip them without rewriting or cleaning
        # anything, including prior failures.
    run_dir = Path(str(payload.get("run_dir") or manifest.parent)).resolve()
    results_dir = Path(str(payload.get("results_dir") or run_dir / "results")).resolve()
    if "output_location" in changed_settings:
        for job in jobs:
            job.result_dir = (
                job.pair.wavefunction_path.parent
                if requested.output_location == "input_directory"
                else results_dir
            )
    return OrbitalDiagramPlan(
        id=str(payload.get("id") or uuid.uuid4().hex[:10]),
        created_at=str(payload.get("created_at") or datetime.now().isoformat(timespec="seconds")),
        run_dir=run_dir,
        results_dir=results_dir,
        settings=requested,
        jobs=jobs,
        resume=True,
        retry_stages=requested_stages,
        retry_job_ids=requested_jobs,
    )


class OrbitalDiagramRunner:
    """Run one plan synchronously; suitable for a Qt worker thread."""

    def __init__(
        self,
        plan: OrbitalDiagramPlan,
        multiwfn_exe: Path | str,
        vmd_exe: Path | str,
        *,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.plan = plan
        self.multiwfn_exe = Path(multiwfn_exe).expanduser().resolve()
        self.vmd_exe = Path(vmd_exe).expanduser().resolve()
        self.event_callback = event_callback
        self._cancel_event = threading.Event()
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_job_position = 0
        self._active_job_total = 1

    def _emit(self, kind: str, **payload: object) -> None:
        if self.event_callback is not None:
            self.event_callback({"kind": kind, **payload})

    def _overall_progress(self, local_percent: float) -> float:
        local = max(0.0, min(100.0, float(local_percent)))
        total = max(1, int(self._active_job_total))
        return max(
            0.0,
            min(100.0, (self._active_job_position + local / 100.0) / total * 100.0),
        )

    def _emit_progress(
        self,
        job: OrbitalDiagramJob,
        local_percent: float,
        *,
        ceiling_local: float | None = None,
        stage: str | None = None,
        message: str = "",
    ) -> None:
        ceiling = local_percent if ceiling_local is None else ceiling_local
        self._emit(
            "progress",
            percent=round(self._overall_progress(local_percent), 2),
            ceiling_percent=round(self._overall_progress(ceiling), 2),
            completed=self._active_job_position,
            total=max(1, self._active_job_total),
            index=job.index,
            job_id=job.id,
            wavefunction_path=str(job.pair.wavefunction_path),
            stage=stage or job.stage,
            status=job.status,
            message=message,
        )

    def cancel(self) -> None:
        self._cancel_event.set()
        with self._process_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            self._terminate_process(process)

    def _write_manifest(self) -> None:
        _write_text_atomic(
            self.plan.manifest_path,
            json.dumps(self.plan.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )

    def _write_summary(self) -> None:
        self.plan.summary_path.parent.mkdir(parents=True, exist_ok=True)
        with self.plan.summary_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["序号", "任务", "计算输出", "波函数", "状态", "失败阶段", "轨道数", "能级图", "耗时（秒）", "错误"]
            )
            for job in self.plan.jobs:
                writer.writerow(
                    [
                        job.index,
                        job.pair.label,
                        str(job.pair.output_path),
                        str(job.pair.wavefunction_path),
                        job.status,
                        job.failed_stage,
                        len(job.orbitals),
                        job.diagram_path,
                        f"{job.duration_seconds:.3f}",
                        job.error,
                    ]
                )

    def run(self) -> dict:
        if not self.multiwfn_exe.is_file():
            raise OrbitalDiagramValidationError(f"Multiwfn.exe 路径无效：{self.multiwfn_exe}")
        if not self.vmd_exe.is_file():
            raise OrbitalDiagramValidationError(f"vmd.exe 路径无效：{self.vmd_exe}")
        self.plan.run_dir.mkdir(parents=True, exist_ok=self.plan.resume)
        self.plan.results_dir.mkdir(parents=True, exist_ok=True)
        (self.plan.run_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.plan.status = STATUS_RUNNING
        self._write_manifest()
        active_total = (
            len(self.plan.retry_job_ids)
            if self.plan.resume and self.plan.retry_job_ids
            else len(self.plan.jobs)
        )
        self._active_job_total = max(1, active_total)
        self._active_job_position = 0
        self._emit(
            "run_started",
            workflow=WORKFLOW_ID,
            run_dir=str(self.plan.run_dir),
            total=active_total,
            percent=0.0,
        )
        completed = 0
        for job in self.plan.jobs:
            if (
                self.plan.resume
                and self.plan.retry_job_ids
                and job.id not in self.plan.retry_job_ids
            ):
                continue
            if self.plan.resume and job.status == STATUS_SKIPPED:
                completed += 1
                self._active_job_position = completed
                self._emit(
                    "progress", completed=completed, total=active_total,
                    percent=round(completed / max(1, active_total) * 100.0, 2),
                    ceiling_percent=round(completed / max(1, active_total) * 100.0, 2),
                    index=job.index, job_id=job.id,
                    wavefunction_path=str(job.pair.wavefunction_path),
                )
                continue
            if self.plan.resume and job.status == STATUS_SUCCESS and not self.plan.retry_stages:
                completed += 1
                self._active_job_position = completed
                self._emit(
                    "progress", completed=completed, total=active_total,
                    percent=round(completed / max(1, active_total) * 100.0, 2),
                    ceiling_percent=round(completed / max(1, active_total) * 100.0, 2),
                    index=job.index, job_id=job.id,
                    wavefunction_path=str(job.pair.wavefunction_path),
                )
                continue
            self._active_job_position = completed
            if self._cancel_event.is_set():
                job.status = STATUS_CANCELLED
                job.error = "流程已由用户停止。"
            else:
                self._run_job(job)
            completed += 1
            self._active_job_position = completed
            self._write_manifest()
            self._emit(
                "progress", completed=completed, total=active_total,
                percent=round(completed / max(1, active_total) * 100.0, 2),
                ceiling_percent=round(completed / max(1, active_total) * 100.0, 2),
                index=job.index, job_id=job.id,
                wavefunction_path=str(job.pair.wavefunction_path),
                stage=job.stage,
                status=job.status,
                message=job.error or f"{job.pair.label} 已完成",
            )
        statuses = {job.status for job in self.plan.jobs}
        effective_statuses = statuses - {STATUS_SKIPPED}
        if effective_statuses == {STATUS_SUCCESS}:
            self.plan.status = STATUS_SUCCESS
        elif self._cancel_event.is_set() or STATUS_CANCELLED in effective_statuses:
            self.plan.status = STATUS_CANCELLED
        else:
            self.plan.status = STATUS_FAILED
        self._write_summary()
        self._write_manifest()
        result = {
            "status": self.plan.status,
            "run_dir": str(self.plan.run_dir),
            "manifest": str(self.plan.manifest_path),
            "summary": str(self.plan.summary_path),
            "success": sum(job.status == STATUS_SUCCESS for job in self.plan.jobs),
            "failed": sum(job.status in {STATUS_FAILED, STATUS_TIMEOUT} for job in self.plan.jobs),
            "cancelled": sum(job.status == STATUS_CANCELLED for job in self.plan.jobs),
            "skipped": sum(job.status == STATUS_SKIPPED for job in self.plan.jobs),
            "total": len(self.plan.jobs),
            "jobs": [job.to_dict() for job in self.plan.jobs],
        }
        self._emit("run_finished", **result)
        return result

    def _set_stage(self, job: OrbitalDiagramJob, stage: str, message: str) -> None:
        if self._cancel_event.is_set():
            raise _Cancelled
        job.stage = stage
        job.status = STATUS_RUNNING
        self._write_manifest()
        self._emit(
            "pair_stage",
            index=job.index,
            job_id=job.id,
            label=job.pair.label,
            wavefunction_path=str(job.pair.wavefunction_path),
            stage=stage,
            status=job.status,
            message=message,
        )
        start, ceiling = STAGE_PROGRESS.get(stage, (0.0, 0.0))
        self._emit_progress(
            job,
            start,
            ceiling_local=ceiling,
            stage=stage,
            message=message,
        )

    def _run_job(self, job: OrbitalDiagramJob) -> None:
        started = time.monotonic()
        job.started_at = datetime.now().isoformat(timespec="seconds")
        job.work_dir.mkdir(parents=True, exist_ok=self.plan.resume)
        try:
            self._prepare_retry_artifacts(job)
            dataset, refs = self._parse_and_resolve(job)
            reference = self._reference_orbital(dataset)
            reference_cube = self._ensure_reference_cube(job, reference)
            view_state = self._ensure_viewpoint(job, reference_cube, dataset)
            self._ensure_orbital_cubes(job, refs)
            self._validate_cubes(job, refs, view_state)
            self._render_orbitals(job, refs, view_state)
            self._compose(job, refs, dataset)
            self._collect(job)
            if not self.plan.settings.keep_cubes:
                self._discard_working_cubes(job)
            job.status = STATUS_SUCCESS
            job.error = ""
            job.failed_stage = ""
            job.can_retry = []
        except _Cancelled:
            job.status = STATUS_CANCELLED
            job.error = "任务已由用户停止。"
            job.failed_stage = job.stage
            job.can_retry = self._retry_suggestions(job.stage)
        except _TimedOut as exc:
            job.status = STATUS_TIMEOUT
            job.error = str(exc)
            job.failed_stage = job.stage
            job.can_retry = self._retry_suggestions(job.stage)
        except Exception as exc:
            job.status = STATUS_FAILED
            job.error = str(exc)
            job.failed_stage = job.stage
            job.can_retry = self._retry_suggestions(job.stage)
        finally:
            job.duration_seconds = time.monotonic() - started
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            _write_text_atomic(
                job.work_dir / "result.json",
                json.dumps(job.to_dict(), ensure_ascii=False, indent=2) + "\n",
            )
            self._emit(
                "pair_stage",
                index=job.index,
                job_id=job.id,
                label=job.pair.label,
                wavefunction_path=str(job.pair.wavefunction_path),
                stage=job.stage,
                status=job.status,
                message=job.error or f"{job.pair.label} 已完成",
                can_retry=list(job.can_retry),
                diagram_path=job.diagram_path,
            )

    def _prepare_retry_artifacts(self, job: OrbitalDiagramJob) -> None:
        stages = self.plan.retry_stages
        if not stages:
            return
        earliest = min((STAGE_ORDER.index(item) for item in stages), default=len(STAGE_ORDER))
        if earliest <= STAGE_ORDER.index(STAGE_VIEWPOINT):
            job.viewpoint_path = ""
            job.viewpoint_status = STATUS_PENDING
            job.images = {}
            job.render_status = {}
            job.diagram_path = ""
        elif earliest <= STAGE_ORDER.index(STAGE_RENDER):
            job.images = {}
            job.render_status = {}
            job.diagram_path = ""
        elif earliest <= STAGE_ORDER.index(STAGE_COMPOSE):
            job.diagram_path = ""
        if earliest <= STAGE_ORDER.index(STAGE_REFERENCE_CUBE):
            job.reference_cube = ""
        if earliest <= STAGE_ORDER.index(STAGE_ORBITAL_CUBES):
            job.cubes = {}
            job.cube_status = {}

    @staticmethod
    def _retry_suggestions(stage: str) -> list[str]:
        if stage in {STAGE_RENDER, STAGE_COMPOSE, STAGE_COLLECT}:
            return [stage]
        if stage == STAGE_VIEWPOINT:
            return [STAGE_VIEWPOINT]
        if stage in {STAGE_ORBITAL_CUBES, STAGE_CUBE_VALIDATION}:
            return [STAGE_ORBITAL_CUBES]
        return [stage] if stage in RETRY_STAGES else [STAGE_PARSE]

    def _parse_and_resolve(
        self, job: OrbitalDiagramJob
    ) -> tuple[orbital_data.OrbitalDataset, list[orbital_data.OrbitalRef]]:
        self._set_stage(job, STAGE_PARSE, "正在解析并核验计算输出与波函数文件")
        settings = self.plan.settings
        dataset = orbital_data.parse_input_pair(
            job.pair.output_path,
            job.pair.wavefunction_path,
            strict=settings.strict_pair_validation,
        )
        job.dataset = dataset.to_dict()
        if dataset.pair_validation is not None:
            job.validation = dataset.pair_validation.to_dict()
        self._set_stage(job, STAGE_RESOLVE, "正在解析轨道范围、占据与能量")
        refs = orbital_data.resolve_orbital_selection(
            dataset,
            mode=settings.selection_mode,
            start_offset=settings.start_offset,
            end_offset=settings.end_offset,
            spin_mode=settings.spin_mode,
            text=settings.selection_text or None,
        )
        selected = next(
            (
                item
                for item in settings.orbital_selections
                if os.path.normcase(str(item.get("wavefunction_path") or ""))
                == os.path.normcase(str(job.pair.wavefunction_path))
            ),
            None,
        )
        if selected is not None:
            allowed = {
                (_enum_value(item.get("spin", "spatial")), int(item.get("global_index", 0)))
                for item in list(selected.get("orbitals") or [])
                if isinstance(item, Mapping) and int(item.get("global_index", 0) or 0) > 0
            }
            refs = [
                ref
                for ref in refs
                if (_enum_value(ref.spin), ref.global_index) in allowed
            ]
        if not refs:
            raise OrbitalDiagramValidationError("轨道选择没有得到任何可绘制轨道。")
        job.orbitals = [_ref_to_dict(ref) for ref in refs]
        return dataset, refs

    @staticmethod
    def _reference_orbital(dataset: orbital_data.OrbitalDataset) -> orbital_data.OrbitalRef:
        spin = "alpha" if dataset.is_unrestricted else "auto"
        refs = orbital_data.resolve_orbital_selection(dataset, mode="homo", spin_mode=spin)
        if not refs:
            raise OrbitalDiagramValidationError("无法确定用于角度校准的 HOMO。")
        return refs[0]

    def _ensure_reference_cube(
        self, job: OrbitalDiagramJob, reference: orbital_data.OrbitalRef
    ) -> Path:
        self._set_stage(job, STAGE_REFERENCE_CUBE, f"正在生成参考轨道 {reference.label} 的 Cube")
        job.reference_orbital = _ref_to_dict(reference)
        existing = Path(job.reference_cube) if job.reference_cube else Path()
        if job.reference_cube and existing.is_file() and existing.stat().st_size > 64:
            return existing.resolve()
        cubes = self._generate_cubes(job, [reference], "reference")
        cube = cubes[_orbital_key(reference)]
        job.reference_cube = str(cube)
        job.cubes.setdefault(_orbital_key(reference), str(cube))
        job.cube_status[_orbital_key(reference)] = STATUS_SUCCESS
        return cube

    def _multiwfn_sequence(self, refs: Sequence[orbital_data.OrbitalRef]) -> str:
        indices = ",".join(str(ref.global_index) for ref in refs)
        # 1 exports the selected orbitals and returns to menu 200.  Leave that
        # submenu explicitly before quitting; ending stdin immediately after
        # export makes Windows Multiwfn terminate with a Fortran EOF error.
        return f"200\n3\n{indices}\n{self.plan.settings.grid_quality}\n1\n0\nq\n"

    def _generate_cubes(
        self,
        job: OrbitalDiagramJob,
        refs: Sequence[orbital_data.OrbitalRef],
        label: str,
    ) -> dict[str, Path]:
        if not refs:
            return {}
        work = job.work_dir / "cubes"
        work.mkdir(parents=True, exist_ok=True)
        stdin = self._multiwfn_sequence(refs)
        sequence_path = job.work_dir / f"multiwfn_{label}_stdin.txt"
        _write_text_atomic(sequence_path, stdin)
        log_path = job.work_dir / f"multiwfn_{label}.log"
        env = os.environ.copy()
        env["Multiwfnpath"] = str(self.multiwfn_exe.parent)
        command = [str(self.multiwfn_exe), str(job.pair.wavefunction_path), "-isilent", "1"]
        return_code, reason = self._run_process(
            command,
            cwd=work,
            env=env,
            stdin_text=stdin,
            timeout_seconds=self.plan.settings.multiwfn_timeout_seconds,
            log_path=log_path,
            source="Multiwfn",
            job=job,
            hide_window=True,
        )
        if reason == "cancelled":
            raise _Cancelled
        if reason == "timeout":
            raise _TimedOut("Multiwfn 生成轨道 Cube 超时，已停止当前进程。")
        if return_code != 0:
            raise OrbitalDiagramError(f"Multiwfn 生成轨道 Cube 失败（退出码 {return_code}）。")
        found: dict[str, Path] = {}
        missing: list[str] = []
        for ref in refs:
            expected = work / f"orb{ref.global_index:06d}.cub"
            if not expected.is_file():
                fallback = _fallback_cube_for_index(work, ref.global_index)
                expected = fallback if fallback is not None else expected
            if not expected.is_file() or expected.stat().st_size <= 64:
                missing.append(ref.label)
                continue
            found[_orbital_key(ref)] = expected.resolve()
        if missing:
            raise OrbitalDiagramError("缺少预期轨道 Cube：" + "、".join(missing))
        return found

    def _ensure_viewpoint(
        self,
        job: OrbitalDiagramJob,
        reference_cube: Path,
        dataset: orbital_data.OrbitalDataset,
    ) -> orbital_vmd.VmdViewState:
        self._set_stage(job, STAGE_VIEWPOINT, "请在 VMD 中调整参考 HOMO，随后点击“保存全部参数并确认”")
        expected = orbital_vmd.cube_geometry_fingerprint(reference_cube)
        candidates = [
            job.viewpoint_path,
            self.plan.settings.view_state_paths.get(dataset.geometry_fingerprint, ""),
            self.plan.settings.view_state_paths.get(expected, ""),
        ]
        for raw in candidates:
            path = Path(raw) if raw else Path()
            if raw and path.is_file():
                try:
                    state = orbital_vmd.load_view_state(path, expected_geometry_fingerprint=expected)
                except orbital_vmd.OrbitalVmdError:
                    continue
                normalized = job.work_dir / "viewpoint.json"
                state.save_json(normalized)
                job.viewpoint_path = str(normalized.resolve())
                job.viewpoint_status = STATUS_SUCCESS
                return state

        protocol = job.work_dir / "viewpoint.capture"
        cancel_marker = orbital_vmd.capture_cancel_marker_path(protocol)
        error_log = orbital_vmd.capture_error_log_path(protocol)
        debug_state = job.work_dir / "vmd_final_state.vmd"
        protocol.unlink(missing_ok=True)
        cancel_marker.unlink(missing_ok=True)
        error_log.unlink(missing_ok=True)
        script = job.work_dir / "capture_viewpoint.vmd"
        _write_text_atomic(
            script,
            orbital_vmd.build_interactive_capture_tcl(
                reference_cube,
                protocol,
                self.plan.settings.style,
                rep0_commands=self.plan.settings.rep0_commands,
                # This is only the interactive OpenGL viewport.  Keeping it
                # separate from the requested Tachyon resolution prevents a
                # 1600x1200 render setting from opening VMD maximized/off-screen.
                width=INTERACTIVE_VMD_VIEWPORT[0],
                height=INTERACTIVE_VMD_VIEWPORT[1],
                debug_state_path=debug_state,
            ),
        )
        self._emit(
            "viewpoint_required",
            index=job.index,
            job_id=job.id,
            label=job.pair.label,
            wavefunction_path=str(job.pair.wavefunction_path),
            reference_orbital=copy.deepcopy(job.reference_orbital),
            reference_cube=str(reference_cube),
        )
        return_code, reason = self._run_process(
            [str(self.vmd_exe), "-e", str(script)],
            cwd=job.work_dir,
            env=os.environ.copy(),
            stdin_text=None,
            timeout_seconds=self.plan.settings.viewpoint_timeout_seconds,
            log_path=job.work_dir / "vmd_viewpoint.log",
            source="VMD",
            job=job,
            hide_window=False,
            show_window=True,
            completion_markers={
                "viewpoint_confirmed": protocol,
                "viewpoint_cancelled": cancel_marker,
            },
        )
        if reason != "cancelled":
            if cancel_marker.is_file() and not protocol.is_file():
                reason = "viewpoint_cancelled"
            elif protocol.is_file():
                reason = "viewpoint_confirmed"
        if reason in {"cancelled", "viewpoint_cancelled"}:
            job.viewpoint_status = STATUS_CANCELLED
            raise _Cancelled
        if reason == "timeout":
            job.viewpoint_status = STATUS_TIMEOUT
            raise _TimedOut("等待 VMD 角度确认超时。参考 Cube 已保留，可仅重试角度校准。")
        if return_code != 0 and reason != "viewpoint_confirmed":
            job.viewpoint_status = STATUS_FAILED
            detail = f"；诊断日志：{error_log}" if error_log.is_file() else ""
            raise OrbitalDiagramError(
                f"VMD 角度校准未正常结束（退出码 {return_code}）{detail}。"
            )
        if not protocol.is_file():
            job.viewpoint_status = STATUS_CANCELLED
            raise OrbitalDiagramError("没有确认 VMD 视角；轨道 Cube 已保留，可重新校准角度。")
        state = orbital_vmd.load_view_state(protocol, expected_geometry_fingerprint=expected)
        normalized = job.work_dir / "viewpoint.json"
        state.save_json(normalized)
        job.viewpoint_path = str(normalized.resolve())
        if debug_state.is_file():
            job.vmd_save_state_path = str(debug_state.resolve())
        job.viewpoint_status = STATUS_SUCCESS
        self._emit(
            "viewpoint_captured",
            index=job.index,
            job_id=job.id,
            viewpoint_path=job.viewpoint_path,
            wavefunction_path=str(job.pair.wavefunction_path),
        )
        return state

    def _ensure_orbital_cubes(
        self, job: OrbitalDiagramJob, refs: Sequence[orbital_data.OrbitalRef]
    ) -> None:
        self._set_stage(job, STAGE_ORBITAL_CUBES, "正在批量生成所选轨道 Cube")
        missing: list[orbital_data.OrbitalRef] = []
        for ref in refs:
            key = _orbital_key(ref)
            path = Path(job.cubes.get(key, ""))
            if not path.is_file() or path.stat().st_size <= 64:
                missing.append(ref)
        generated = self._generate_cubes(job, missing, "orbitals") if missing else {}
        for ref in refs:
            key = _orbital_key(ref)
            if key in generated:
                job.cubes[key] = str(generated[key])
            job.cube_status[key] = STATUS_SUCCESS if Path(job.cubes.get(key, "")).is_file() else STATUS_FAILED

    def _validate_cubes(
        self,
        job: OrbitalDiagramJob,
        refs: Sequence[orbital_data.OrbitalRef],
        state: orbital_vmd.VmdViewState,
    ) -> None:
        self._set_stage(job, STAGE_CUBE_VALIDATION, "正在核验所有轨道 Cube 的分子与网格")
        expected = state.geometry_fingerprint
        for ref in refs:
            key = _orbital_key(ref)
            path = Path(job.cubes.get(key, ""))
            if not path.is_file() or path.stat().st_size <= 64:
                job.cube_status[key] = STATUS_FAILED
                raise OrbitalDiagramError(f"{ref.label} 的 Cube 不存在或为空。")
            if orbital_vmd.cube_geometry_fingerprint(path) != expected:
                job.cube_status[key] = STATUS_FAILED
                raise OrbitalDiagramError(f"{ref.label} 的 Cube 分子或网格与参考轨道不一致。")
            job.cube_status[key] = STATUS_SUCCESS

    def _render_orbitals(
        self,
        job: OrbitalDiagramJob,
        refs: Sequence[orbital_data.OrbitalRef],
        state: orbital_vmd.VmdViewState,
    ) -> None:
        self._set_stage(job, STAGE_RENDER, "正在以确认的相同视角逐轨道渲染")
        render_dir = job.work_dir / "rendered"
        render_dir.mkdir(parents=True, exist_ok=True)
        native_state = Path(job.vmd_save_state_path) if job.vmd_save_state_path else None
        reference_cube = Path(job.reference_cube) if job.reference_cube else None
        use_native_state = bool(
            native_state is not None
            and native_state.is_file()
            and reference_cube is not None
            and reference_cube.is_file()
        )
        total = len(refs)
        render_start, render_end = STAGE_PROGRESS[STAGE_RENDER]
        for number, ref in enumerate(refs, 1):
            if self._cancel_event.is_set():
                raise _Cancelled
            key = _orbital_key(ref)
            existing = Path(job.images.get(key, ""))
            if existing.is_file() and existing.stat().st_size > 64:
                job.render_status[key] = STATUS_SUCCESS
                local = render_start + (render_end - render_start) * number / max(1, total)
                self._emit_progress(
                    job,
                    local,
                    ceiling_local=local,
                    stage=STAGE_RENDER,
                    message=f"已复用 {ref.label} 的轨道图像",
                )
                continue
            safe = _orbital_artifact_stem(ref)
            tga = render_dir / f"{safe}.tga"
            png = render_dir / f"{safe}.png"
            tcl = render_dir / f"{safe}.vmd"
            _write_text_atomic(
                tcl,
                orbital_vmd.build_batch_render_tcl(
                    job.cubes[key],
                    tga,
                    state,
                    width=self.plan.settings.width,
                    height=self.plan.settings.height,
                    native_state_path=native_state if use_native_state else None,
                    reference_cube_path=reference_cube if use_native_state else None,
                ),
            )
            self._emit(
                "orbital_stage",
                index=job.index,
                job_id=job.id,
                wavefunction_path=str(job.pair.wavefunction_path),
                orbital=ref.to_dict(),
                current=number,
                total=total,
                stage=STAGE_RENDER,
                status=STATUS_RUNNING,
            )
            local_before = render_start + (render_end - render_start) * (number - 1) / max(1, total)
            local_after = render_start + (render_end - render_start) * number / max(1, total)
            self._emit_progress(
                job,
                local_before,
                ceiling_local=max(local_before, local_after - 0.5),
                stage=STAGE_RENDER,
                message=f"正在渲染 {ref.label}（{number}/{total}）",
            )
            return_code, reason = self._run_process(
                [str(self.vmd_exe), "-dispdev", "text", "-eofexit", "-e", str(tcl)],
                cwd=render_dir,
                env=os.environ.copy(),
                stdin_text=None,
                timeout_seconds=self.plan.settings.vmd_timeout_seconds,
                log_path=render_dir / f"{safe}.log",
                source="VMD",
                job=job,
                hide_window=True,
            )
            if reason == "cancelled":
                job.render_status[key] = STATUS_CANCELLED
                raise _Cancelled
            if reason == "timeout":
                job.render_status[key] = STATUS_TIMEOUT
                raise _TimedOut(f"VMD 渲染 {ref.label} 超时；已完成图片可继续复用。")
            if return_code != 0:
                job.render_status[key] = STATUS_FAILED
                raise OrbitalDiagramError(f"VMD 渲染 {ref.label} 失败（退出码 {return_code}）。")
            orbital_vmd.validate_render_output(tga)
            self._convert_to_png(tga, png)
            job.images[key] = str(png.resolve())
            job.render_status[key] = STATUS_SUCCESS
            self._write_manifest()
            self._emit(
                "orbital_stage",
                index=job.index,
                job_id=job.id,
                wavefunction_path=str(job.pair.wavefunction_path),
                orbital=ref.to_dict(),
                current=number,
                total=total,
                stage=STAGE_RENDER,
                status=STATUS_SUCCESS,
                image_path=str(png.resolve()),
            )
            self._emit_progress(
                job,
                local_after,
                ceiling_local=local_after,
                stage=STAGE_RENDER,
                message=f"已完成 {ref.label}（{number}/{total}）",
            )

    @staticmethod
    def _pillow():
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise OrbitalDiagramDependencyError(
                "缺少 Pillow 图片组件：轨道 TGA 已保留，但无法转换 PNG 或合成能级图。"
            ) from exc
        return Image, ImageDraw, ImageFont

    @classmethod
    def _convert_to_png(cls, source: Path, target: Path) -> None:
        Image, _ImageDraw, _ImageFont = cls._pillow()
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp.png")
        try:
            with Image.open(source) as image:
                image.convert("RGB").save(temporary, "PNG", optimize=True)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _font(ImageFont, size: int, *, bold: bool = False):
        candidates = [
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / ("msyhbd.ttc" if bold else "msyh.ttc"),
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / ("segoeuib.ttf" if bold else "segoeui.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
        for path in candidates:
            if path.is_file():
                try:
                    return ImageFont.truetype(str(path), size=size)
                except OSError:
                    pass
        return ImageFont.load_default()

    def _compose(
        self,
        job: OrbitalDiagramJob,
        refs: Sequence[orbital_data.OrbitalRef],
        dataset: orbital_data.OrbitalDataset,
    ) -> None:
        self._set_stage(job, STAGE_COMPOSE, "正在合成分子轨道能级图")
        existing = Path(job.diagram_path) if job.diagram_path else Path()
        if job.diagram_path and existing.is_file() and existing.stat().st_size > 64:
            return
        Image, ImageDraw, ImageFont = self._pillow()
        from PIL import ImageChops

        groups: dict[str, list[orbital_data.OrbitalRef]] = {}
        for ref in refs:
            groups.setdefault(_enum_value(ref.spin), []).append(ref)
        unrestricted = dataset.is_unrestricted
        width = self.plan.settings.diagram_width
        image_w = max(270, min(350, width // 5))
        image_h = max(
            150,
            min(
                245,
                int(image_w * self.plan.settings.height / self.plan.settings.width),
            ),
        )
        card_w, card_h = image_w + 28, image_h + 28
        row_gap = card_h + 74
        largest = max(len(group) for group in groups.values())
        content_top = 170
        height = max(920, content_top + card_h + (largest - 1) * row_gap + 120)
        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)
        title_font = self._font(ImageFont, max(29, width // 56), bold=True)
        header_font = self._font(ImageFont, max(22, width // 76), bold=True)
        label_font = self._font(ImageFont, max(16, width // 100), bold=True)
        small_font = self._font(ImageFont, max(13, width // 125))
        note_font = self._font(ImageFont, max(12, width // 140))
        draw.text(
            (width // 2, 42),
            self.plan.settings.title,
            fill="#172b45",
            font=title_font,
            anchor="ma",
        )
        draw.line((58, 112, width - 58, 112), fill="#e2e8f0", width=2)

        def energy(ref: orbital_data.OrbitalRef) -> float:
            return (
                ref.energy_hartree
                if self.plan.settings.energy_unit == "Hartree"
                else ref.energy_ev
            )

        def energy_label(ref: orbital_data.OrbitalRef) -> str:
            suffix = "a.u." if self.plan.settings.energy_unit == "Hartree" else "eV"
            return f"{energy(ref):.{self.plan.settings.energy_decimals}f} {suffix}"

        # Build visually compressed energy clusters.  Ordering remains exact,
        # ordinary spacings remain visible, and a single extreme value can no
        # longer push every useful frontier level to the canvas edge.
        clusters: list[list[orbital_data.OrbitalRef]] = []
        for ref in sorted(refs, key=lambda item: item.energy_ev, reverse=True):
            if clusters:
                center = sum(item.energy_ev for item in clusters[-1]) / len(clusters[-1])
                if abs(ref.energy_ev - center) <= 0.03:
                    clusters[-1].append(ref)
                    continue
            clusters.append([ref])
        centers = [sum(item.energy_ev for item in group) / len(group) for group in clusters]
        gaps = [max(0.0, centers[index] - centers[index + 1]) for index in range(len(centers) - 1)]
        positive_gaps = sorted(gap for gap in gaps if gap > 1.0e-9)
        typical_gap = positive_gaps[len(positive_gaps) // 2] if positive_gaps else 1.0
        intra_cluster_spacing = 70.0
        cluster_half_spans: list[float] = []
        for cluster in clusters:
            counts: dict[str, int] = {}
            for ref in cluster:
                channel = _enum_value(ref.spin)
                counts[channel] = counts.get(channel, 0) + 1
            largest_channel_count = max(counts.values(), default=1)
            cluster_half_spans.append(
                (largest_channel_count - 1) * intra_cluster_spacing / 2.0
            )
        relative_positions = [0.0]
        for gap_index, gap in enumerate(gaps):
            extra = min(88.0, 34.0 * math.log1p(gap / max(typical_gap, 0.05)))
            relative_positions.append(
                relative_positions[-1]
                + cluster_half_spans[gap_index]
                + 78.0
                + extra
                + cluster_half_spans[gap_index + 1]
            )
        raw_span = (
            (relative_positions[-1] if relative_positions else 0.0)
            + (cluster_half_spans[0] if cluster_half_spans else 0.0)
            + (cluster_half_spans[-1] if cluster_half_spans else 0.0)
        )
        level_start = (
            (height - raw_span) / 2.0
            + (cluster_half_spans[0] if cluster_half_spans else 0.0)
            + 12.0
        )
        level_y: dict[str, int] = {}
        for cluster_index, cluster in enumerate(clusters):
            base_y = level_start + relative_positions[cluster_index]
            by_channel: dict[str, list[orbital_data.OrbitalRef]] = {}
            for ref in cluster:
                by_channel.setdefault(_enum_value(ref.spin), []).append(ref)
            for channel_items in by_channel.values():
                ordered = sorted(channel_items, key=lambda item: item.energy_ev, reverse=True)
                for index, ref in enumerate(ordered):
                    offset = (
                        index - (len(ordered) - 1) / 2.0
                    ) * intra_cluster_spacing
                    level_y[_orbital_key(ref)] = int(round(base_y + offset))

        if unrestricted:
            center_x = width // 2
            layout = {
                "alpha": (48, center_x - 255, center_x - 76, "α MOs", "#426b9c"),
                "beta": (width - card_w - 48, center_x + 76, center_x + 255, "β MOs", "#66558f"),
            }
            panel_left, panel_right = center_x - 310, center_x + 310
        else:
            channel = next(iter(groups))
            layout = {
                channel: (64, width // 2 - 110, width // 2 + 110, "MOs", "#426b9c")
            }
            panel_left, panel_right = width // 2 - 175, width // 2 + 175
        panel_top = max(134, min(level_y.values()) - 70)
        panel_bottom = min(height - 74, max(level_y.values()) + 72)
        draw.rounded_rectangle(
            (panel_left, panel_top, panel_right, panel_bottom),
            radius=22,
            fill="#f8fafc",
            outline="#dbe5ef",
            width=2,
        )

        # All orbitals use one shared crop rectangle, preserving the user's
        # captured orientation, scale and relative molecular placement.
        source_images: dict[str, object] = {}
        crop_box: tuple[int, int, int, int] | None = None
        for ref in refs:
            key = _orbital_key(ref)
            with Image.open(job.images[key]) as opened:
                source = opened.convert("RGB")
            source_images[key] = source
            white = Image.new("RGB", source.size, "white")
            mask = ImageChops.difference(source, white).convert("L").point(
                lambda value: 255 if value > 10 else 0
            )
            bounds = mask.getbbox()
            if bounds:
                crop_box = (
                    bounds
                    if crop_box is None
                    else (
                        min(crop_box[0], bounds[0]),
                        min(crop_box[1], bounds[1]),
                        max(crop_box[2], bounds[2]),
                        max(crop_box[3], bounds[3]),
                    )
                )
        if crop_box is not None:
            source_width, source_height = next(iter(source_images.values())).size
            padding = max(10, int(min(source_width, source_height) * 0.025))
            crop_box = (
                max(0, crop_box[0] - padding),
                max(0, crop_box[1] - padding),
                min(source_width, crop_box[2] + padding),
                min(source_height, crop_box[3] + padding),
            )

        for channel, items in groups.items():
            image_x, line_a, line_b, header, accent = layout[channel]
            draw.text(
                ((line_a + line_b) // 2, 136),
                header,
                fill=accent,
                font=header_font,
                anchor="ma",
            )
            ordered = sorted(items, key=lambda item: item.energy_ev, reverse=True)
            group_offset = (largest - len(ordered)) * row_gap / 2.0
            first_center = content_top + card_h / 2.0 + group_offset
            for order, ref in enumerate(ordered):
                key = _orbital_key(ref)
                center_y = int(round(first_center + order * row_gap))
                card_y = center_y - card_h // 2
                draw.rounded_rectangle(
                    (image_x, card_y, image_x + card_w, card_y + card_h),
                    radius=18,
                    fill="#fbfcfe",
                    outline="#d8e2ed",
                    width=2,
                )
                picture = source_images[key]
                if crop_box is not None:
                    picture = picture.crop(crop_box)
                picture.thumbnail((image_w, image_h), Image.Resampling.LANCZOS)
                px = image_x + (card_w - picture.width) // 2
                py = center_y - picture.height // 2
                canvas.paste(picture, (px, py))

                target_y = level_y[key]
                image_is_left = image_x < line_a
                card_edge = image_x + card_w if image_is_left else image_x
                line_edge = line_a if image_is_left else line_b
                # Every orbital gets its own lane, so the vertical connector
                # segments are parallel rather than drawn on top of each other.
                lane = (
                    line_edge - 32 - order * 15
                    if image_is_left
                    else line_edge + 32 + order * 15
                )
                draw.line(
                    (card_edge, center_y, lane, center_y, lane, target_y, line_edge, target_y),
                    fill="#7890ad",
                    width=2,
                )
                draw.line((line_a, target_y, line_b, target_y), fill="#263b55", width=3)

                line_mid = (line_a + line_b) // 2
                draw.text(
                    (line_mid, target_y - 9),
                    energy_label(ref),
                    fill="#172b45",
                    font=label_font,
                    anchor="ms",
                )
                draw.text(
                    (line_mid, target_y + 10),
                    ref.label,
                    fill="#60748c",
                    font=small_font,
                    anchor="ma",
                )
                arrow_x = line_a + 24 if image_is_left else line_b - 24
                arrows = 2 if ref.occupation > 1.5 else 1 if ref.occupation > 0.1 else 0
                if arrows:
                    if _enum_value(ref.spin) == "beta":
                        self._draw_arrow(draw, arrow_x, target_y - 19, target_y + 19, "#2f6fca")
                    else:
                        self._draw_arrow(draw, arrow_x, target_y + 19, target_y - 19, "#2f6fca")
                if arrows == 2:
                    self._draw_arrow(draw, arrow_x + 10, target_y - 19, target_y + 19, "#2f6fca")

        draw.text(
            (width // 2, height - 38),
            "能量高低顺序保持不变；纵向间距已为清晰阅读进行优化",
            fill="#7a899b",
            font=note_font,
            anchor="ma",
        )
        diagram = job.work_dir / f"{_clean_part(job.pair.label)}_MO_energy_diagram.png"
        temporary = diagram.with_name(f".{diagram.name}.{uuid.uuid4().hex}.tmp.png")
        try:
            canvas.save(temporary, "PNG", optimize=True)
            os.replace(temporary, diagram)
        finally:
            temporary.unlink(missing_ok=True)
        job.diagram_path = str(diagram.resolve())

    @staticmethod
    def _draw_arrow(draw, x: int, start_y: int, end_y: int, color: str) -> None:
        draw.line((x, start_y, x, end_y), fill=color, width=3)
        direction = -1 if end_y < start_y else 1
        draw.polygon(
            [(x, end_y), (x - 6, end_y - direction * 10), (x + 6, end_y - direction * 10)],
            fill=color,
        )

    def _collect(self, job: OrbitalDiagramJob) -> None:
        self._set_stage(job, STAGE_COLLECT, "正在整理能级图、轨道数据、图片与日志")
        job.result_dir.mkdir(parents=True, exist_ok=True)
        label = _clean_part(job.pair.label)
        diagram_target = _unique_target(job.result_dir / f"{label}_MO_energy_diagram.png")
        shutil.copy2(job.diagram_path, diagram_target)
        job.diagram_path = str(diagram_target.resolve())
        job.outputs.append(job.diagram_path)
        image_dir = job.result_dir / f"{label}_orbitals"
        image_dir.mkdir(parents=True, exist_ok=True)
        collected_images: dict[str, str] = {}
        for key, raw in job.images.items():
            source = Path(raw)
            if source.is_file():
                target = _unique_target(image_dir / source.name)
                shutil.copy2(source, target)
                collected_images[key] = str(target.resolve())
                job.outputs.append(str(target.resolve()))
        data_target = _unique_target(job.result_dir / f"{label}_orbitals.csv")
        with data_target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["spin", "label", "channel_index", "multiwfn_index", "occupation", "energy_hartree", "energy_ev", "image"])
            for ref in job.orbitals:
                key = _orbital_key(ref)
                writer.writerow(
                    [
                        _enum_value(ref.get("spin", "")),
                        ref.get("label", ""),
                        ref.get("channel_index", ""),
                        ref.get("global_index", ""),
                        ref.get("occupation", ""),
                        ref.get("energy_hartree", ""),
                        ref.get("energy_ev", ""),
                        collected_images.get(key, job.images.get(key, "")),
                    ]
                )
        job.outputs.append(str(data_target.resolve()))
        view_source = Path(job.viewpoint_path)
        if view_source.is_file():
            view_target = _unique_target(job.result_dir / f"{label}_viewpoint.json")
            shutil.copy2(view_source, view_target)
            job.outputs.append(str(view_target.resolve()))
        save_state_source = Path(job.vmd_save_state_path) if job.vmd_save_state_path else Path()
        if job.vmd_save_state_path and save_state_source.is_file():
            save_state_target = _unique_target(job.result_dir / f"{label}_VMD_final_state.vmd")
            shutil.copy2(save_state_source, save_state_target)
            job.outputs.append(str(save_state_target.resolve()))
        logs = self.plan.run_dir / "logs" / label
        for source in job.work_dir.rglob("*.log"):
            target = _unique_target(logs / source.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            job.outputs.append(str(target.resolve()))
        if self.plan.settings.keep_cubes:
            cube_dir = job.result_dir / f"{label}_cubes"
            cube_dir.mkdir(parents=True, exist_ok=True)
            for raw in sorted(set(job.cubes.values())):
                source = Path(raw)
                if source.is_file():
                    target = _unique_target(cube_dir / source.name)
                    shutil.copy2(source, target)
                    job.outputs.append(str(target.resolve()))

    @staticmethod
    def _discard_working_cubes(job: OrbitalDiagramJob) -> None:
        """Remove generated Cube work copies only after successful collection."""

        paths = {
            Path(raw).resolve()
            for raw in [job.reference_cube, *job.cubes.values()]
            if raw
        }
        work_root = job.work_dir.resolve()
        for path in paths:
            try:
                path.relative_to(work_root)
            except ValueError:
                # Never delete an input or a user-selected file outside this
                # task's isolated work directory.
                continue
            if path.is_file() and path.suffix.casefold() in {".cub", ".cube"}:
                path.unlink()

    def _run_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdin_text: str | None,
        timeout_seconds: int,
        log_path: Path,
        source: str,
        job: OrbitalDiagramJob,
        hide_window: bool,
        show_window: bool = False,
        completion_markers: Mapping[str, Path] | None = None,
    ) -> tuple[int, str]:
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hide_window else 0
        encoding = locale.getpreferredencoding(False) or "utf-8"
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=encoding,
            errors="replace",
            creationflags=creation_flags,
        )
        with self._process_lock:
            self._active_process = process
        try:
            if stdin_text is not None:
                assert process.stdin is not None
                process.stdin.write(stdin_text)
                process.stdin.close()
            assert process.stdout is not None
            output_queue: queue.Queue[object] = queue.Queue()
            sentinel = object()

            def reader() -> None:
                try:
                    assert process.stdout is not None
                    for line in process.stdout:
                        output_queue.put(line)
                finally:
                    output_queue.put(sentinel)

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            started = time.monotonic()
            next_window_check = started
            window_restored = False
            stream_finished = False
            reason = ""
            markers = tuple(
                (str(marker_reason), Path(marker_path))
                for marker_reason, marker_path in (completion_markers or {}).items()
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                while process.poll() is None or not stream_finished:
                    now = time.monotonic()
                    if (
                        show_window
                        and not window_restored
                        and now >= next_window_check
                        and now - started <= 20.0
                    ):
                        window_restored = self._restore_vmd_window(process.pid)
                        next_window_check = now + 0.35
                    try:
                        item = output_queue.get(timeout=0.1)
                    except queue.Empty:
                        item = None
                    if item is sentinel:
                        stream_finished = True
                    elif isinstance(item, str):
                        log.write(item)
                        log.flush()
                        text = item.rstrip("\r\n")
                        if text:
                            self._emit(
                                "output", index=job.index, job_id=job.id,
                                wavefunction_path=str(job.pair.wavefunction_path),
                                source=source, text=text,
                            )
                    if self._cancel_event.is_set() and process.poll() is None:
                        reason = "cancelled"
                        self._terminate_process(process)
                    elif not reason:
                        completed = next(
                            (
                                marker_reason
                                for marker_reason, marker_path in markers
                                if marker_path.is_file()
                            ),
                            "",
                        )
                        if completed:
                            reason = completed
                            log.write(
                                f"MolecularStudio host: detected {completed}; "
                                "closed the interactive VMD process safely.\n"
                            )
                            log.flush()
                            if process.poll() is None:
                                self._terminate_process(process)
                        elif (
                            time.monotonic() - started > timeout_seconds
                            and process.poll() is None
                        ):
                            reason = "timeout"
                            self._terminate_process(process)
            thread.join(timeout=1)
            return process.wait(timeout=5), reason
        finally:
            if process.stdout is not None:
                process.stdout.close()
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    @staticmethod
    def _restore_vmd_window(process_id: int) -> bool:
        """Restore and foreground the interactive VMD OpenGL window on Windows."""

        if os.name != "nt":
            return False
        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            matches: list[int] = []
            callback_type = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
            )
            user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            ]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [
                wintypes.HWND,
                wintypes.LPWSTR,
                ctypes.c_int,
            ]
            user32.GetWindowTextW.restype = ctypes.c_int
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            user32.BringWindowToTop.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL

            @callback_type
            def collect(hwnd, _lparam):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if int(owner.value) != int(process_id):
                    return True
                length = int(user32.GetWindowTextLengthW(hwnd))
                if length <= 0:
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value.casefold()
                if "opengl display" in title or (
                    title.startswith("vmd ") and "display" in title
                ):
                    raw_hwnd = ctypes.cast(hwnd, ctypes.c_void_p).value
                    if raw_hwnd is not None:
                        matches.append(raw_hwnd)
                return True

            user32.EnumWindows(collect, 0)
            if not matches:
                return False
            width, height = INTERACTIVE_VMD_WINDOW
            restored = False
            for raw_hwnd in matches:
                hwnd = wintypes.HWND(raw_hwnd)
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE also clears minimized state.
                positioned = user32.SetWindowPos(
                    hwnd,
                    wintypes.HWND(0),
                    24,
                    32,
                    width,
                    height,
                    0x0040,
                )
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                restored = restored or bool(
                    positioned and user32.IsWindowVisible(hwnd)
                )
            return restored
        except (AttributeError, OSError, TypeError, ValueError):
            return False


# Concise aliases for clients that prefer the generic workflow terminology.
create_plan = create_orbital_diagram_plan
resume_plan = resume_orbital_diagram_plan
Runner = OrbitalDiagramRunner


__all__ = [
    "WORKFLOW_ID",
    "MANIFEST_SCHEMA_VERSION",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "STATUS_TIMEOUT",
    "STAGE_PARSE",
    "STAGE_RESOLVE",
    "STAGE_REFERENCE_CUBE",
    "STAGE_VIEWPOINT",
    "STAGE_ORBITAL_CUBES",
    "STAGE_CUBE_VALIDATION",
    "STAGE_RENDER",
    "STAGE_COMPOSE",
    "STAGE_COLLECT",
    "INTERACTIVE_VMD_VIEWPORT",
    "INTERACTIVE_VMD_WINDOW",
    "STAGE_PROGRESS",
    "OrbitalDiagramError",
    "OrbitalDiagramValidationError",
    "OrbitalDiagramDependencyError",
    "OrbitalDiagramSettings",
    "OrbitalDiagramJob",
    "OrbitalDiagramPlan",
    "detect_energy_spacing_anomaly",
    "inspect_orbital_pair",
    "create_orbital_diagram_plan",
    "resume_orbital_diagram_plan",
    "OrbitalDiagramRunner",
    "create_plan",
    "resume_plan",
    "Runner",
]
