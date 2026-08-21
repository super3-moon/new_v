from __future__ import annotations

import csv
import copy
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
from typing import Callable, Iterable, Mapping

import orbital_vmd
import vmd_style_tool as vmd_core


AUTOMATION_SCHEMA_VERSION = 1
WORKFLOW_SURFACE_ESP = "surface_esp"
WORKFLOW_ORBITAL_DIAGRAM = "orbital_energy_diagram"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"

STAGE_MULTIWFN = "multiwfn"
STAGE_CUBE_VALIDATION = "cube_validation"
STAGE_VMD_RENDER = "vmd_render"
STAGE_COLLECT = "collect"

SUPPORTED_WAVEFUNCTION_EXTENSIONS = (
    ".fch",
    ".fchk",
    ".wfn",
    ".wfx",
    ".mwfn",
    ".molden",
    ".molden.input",
)

# Both volumetric files must use exactly the same origin, dimensions and grid
# vectors.  Density is generated first on the normal ESP plotting grid, then
# Multiwfn mode 8 reuses density.cub's grid for the ESP calculation.
ESP_STDIN_SEQUENCE = "5\n1\n1\n2\n0\n5\n12\n8\ndensity.cub\n2\n0\nq\n"

AUTOMATION_STAGE_PROGRESS = {
    STAGE_MULTIWFN: (1.0, 74.0),
    STAGE_CUBE_VALIDATION: (74.0, 78.0),
    STAGE_VMD_RENDER: (78.0, 96.0),
    STAGE_COLLECT: (96.0, 99.0),
}
INTERACTIVE_VMD_VIEWPORT = (1160, 640)
INTERACTIVE_VMD_WINDOW = (1180, 700)


class AutomationValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    id: str
    name: str
    description: str
    engine: str
    input_extensions: tuple[str, ...]
    handler: str = "surface_esp"
    input_mode: str = "single_wavefunction"


def workflow_definitions() -> tuple[WorkflowDefinition, ...]:
    """Return the extensible catalog shown by the automatic-workflow module."""
    return (
        WorkflowDefinition(
            id=WORKFLOW_SURFACE_ESP,
            name="表面静电势图",
            description=(
                "由 Multiwfn 生成电子密度与 ESP Cube，检查空间网格后，"
                "套用绘图方案并交给 VMD 渲染。"
            ),
            engine="Multiwfn + VMD",
            input_extensions=SUPPORTED_WAVEFUNCTION_EXTENSIONS,
        ),
        WorkflowDefinition(
            id=WORKFLOW_ORBITAL_DIAGRAM,
            name="分子轨道能级图",
            description=(
                "配对 Gaussian/ORCA 输出与 FCH/Molden 波函数，批量生成指定轨道，"
                "在 VMD 中统一取景后用 Tachyon 渲染并自动排版。"
            ),
            engine="Multiwfn + VMD",
            input_extensions=(
                ".out",
                ".log",
                ".fch",
                ".fchk",
                ".molden",
                ".molden.input",
            ),
            handler="orbital_diagram",
            input_mode="paired_qc_wavefunction",
        ),
    )


def _definition_map() -> dict[str, WorkflowDefinition]:
    return {definition.id: definition for definition in workflow_definitions()}


def _write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_hash(payload: object) -> str:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _clean_file_part(value: str, fallback: str = "result") -> str:
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return (text or fallback)[:110]


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _is_supported_input(path: Path, definition: WorkflowDefinition) -> bool:
    name = path.name.casefold()
    return path.is_file() and any(
        name.endswith(extension) for extension in definition.input_extensions
    )


def normalize_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise AutomationValidationError("自动化流程设置格式无效。")
    normalized = copy.deepcopy(settings)
    try:
        rho_iso = float(str(normalized.get("rho_iso", "0.001")).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise AutomationValidationError("电子密度等值面必须是有效数字。") from exc
    if not math.isfinite(rho_iso) or rho_iso <= 0:
        raise AutomationValidationError("电子密度等值面必须是大于零的有限数字。")

    render_mode = str(normalized.get("render_mode") or "automatic")
    if render_mode not in {"automatic", "interactive", "cubes_only"}:
        raise AutomationValidationError("未知的 VMD 结果方式。")
    output_location = str(normalized.get("output_location") or "result_root")
    if output_location not in {"result_root", "input_directory"}:
        raise AutomationValidationError("未知的结果保存方式。")

    style_snapshot = normalized.get("style_snapshot")
    if not isinstance(style_snapshot, dict):
        raise AutomationValidationError("请选择一个绘图方案。")
    style = style_snapshot.get("style")
    if not isinstance(style, dict):
        raise AutomationValidationError("绘图方案快照缺少实际风格参数。")
    if str(style.get("surface_mode") or "") != "volume_mapped":
        raise AutomationValidationError("表面静电势流程只能使用 ESP 映射绘图方案。")
    style_snapshot = copy.deepcopy(style_snapshot)
    supplied_hash = str(style_snapshot.pop("hash", "") or "")
    calculated_hash = _canonical_hash(style_snapshot)
    if supplied_hash and supplied_hash != calculated_hash:
        raise AutomationValidationError(
            "绘图方案快照校验失败，请重新选择绘图方案。"
        )
    style_snapshot["hash"] = calculated_hash

    width = max(320, min(7680, int(normalized.get("width") or 1600)))
    height = max(240, min(4320, int(normalized.get("height") or 1200)))
    vmd_timeout = max(
        30, min(86400, int(normalized.get("vmd_timeout_seconds") or 600))
    )
    multiwfn_timeout = max(
        30, min(86400, int(normalized.get("multiwfn_timeout_seconds") or 1800))
    )
    return {
        **normalized,
        "rho_iso": format(rho_iso, ".12g"),
        "render_mode": render_mode,
        "output_location": output_location,
        "width": width,
        "height": height,
        "vmd_timeout_seconds": vmd_timeout,
        "multiwfn_timeout_seconds": multiwfn_timeout,
        "keep_cubes": bool(normalized.get("keep_cubes", True)),
        "style_snapshot": style_snapshot,
    }


@dataclass(slots=True)
class AutomationJob:
    id: str
    index: int
    input_path: Path
    work_dir: Path
    result_dir: Path
    status: str = STATUS_PENDING
    stage: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    failed_stage: str = ""
    multiwfn_status: str = STATUS_PENDING
    vmd_status: str = STATUS_PENDING
    multiwfn_return_code: int | None = None
    vmd_return_code: int | None = None
    density_cube: str = ""
    esp_cube: str = ""
    image_path: str = ""
    viewpoint_path: str = ""
    vmd_save_state_path: str = ""
    outputs: list[str] = field(default_factory=list)
    can_retry_drawing: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "AutomationJob":
        return cls(
            id=str(raw.get("id") or f"job_{uuid.uuid4().hex[:8]}"),
            index=int(raw.get("index") or 1),
            input_path=Path(str(raw.get("input_path") or "")).expanduser().resolve(),
            work_dir=Path(str(raw.get("work_dir") or "")).expanduser().resolve(),
            result_dir=Path(str(raw.get("result_dir") or "")).expanduser().resolve(),
            status=str(raw.get("status") or STATUS_PENDING),
            stage=str(raw.get("stage") or ""),
            started_at=str(raw.get("started_at") or ""),
            finished_at=str(raw.get("finished_at") or ""),
            duration_seconds=float(raw.get("duration_seconds") or 0.0),
            error=str(raw.get("error") or ""),
            failed_stage=str(raw.get("failed_stage") or ""),
            multiwfn_status=str(raw.get("multiwfn_status") or STATUS_PENDING),
            vmd_status=str(raw.get("vmd_status") or STATUS_PENDING),
            multiwfn_return_code=raw.get("multiwfn_return_code"),
            vmd_return_code=raw.get("vmd_return_code"),
            density_cube=str(raw.get("density_cube") or ""),
            esp_cube=str(raw.get("esp_cube") or ""),
            image_path=str(raw.get("image_path") or ""),
            viewpoint_path=str(raw.get("viewpoint_path") or ""),
            vmd_save_state_path=str(raw.get("vmd_save_state_path") or ""),
            outputs=[str(item) for item in raw.get("outputs") or []],
            can_retry_drawing=bool(raw.get("can_retry_drawing", False)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "index": self.index,
            "input_path": str(self.input_path),
            "work_dir": str(self.work_dir),
            "result_dir": str(self.result_dir),
            "status": self.status,
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
            "failed_stage": self.failed_stage,
            "multiwfn_status": self.multiwfn_status,
            "vmd_status": self.vmd_status,
            "multiwfn_return_code": self.multiwfn_return_code,
            "vmd_return_code": self.vmd_return_code,
            "density_cube": self.density_cube,
            "esp_cube": self.esp_cube,
            "image_path": self.image_path,
            "viewpoint_path": self.viewpoint_path,
            "vmd_save_state_path": self.vmd_save_state_path,
            "outputs": list(self.outputs),
            "can_retry_drawing": self.can_retry_drawing,
        }


@dataclass(slots=True)
class AutomationPlan:
    id: str
    created_at: str
    workflow: WorkflowDefinition
    run_dir: Path
    results_dir: Path
    settings: dict
    jobs: list[AutomationJob]
    status: str = STATUS_PENDING
    resume: bool = False

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def summary_path(self) -> Path:
        return self.run_dir / "summary.csv"

    def to_dict(self) -> dict:
        return {
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "id": self.id,
            "created_at": self.created_at,
            "workflow": {
                "id": self.workflow.id,
                "name": self.workflow.name,
                "engine": self.workflow.engine,
            },
            "run_dir": str(self.run_dir),
            "results_dir": str(self.results_dir),
            "status": self.status,
            "settings": copy.deepcopy(self.settings),
            "jobs": [job.to_dict() for job in self.jobs],
        }


def create_automation_plan(
    input_paths: Iterable[Path | str],
    workflow_id: str,
    output_root: Path | str,
    settings: dict,
    *,
    prefix: str = "automatic",
) -> AutomationPlan:
    definition = _definition_map().get(str(workflow_id))
    if definition is None:
        raise AutomationValidationError(f"未知的全自动流程：{workflow_id}")
    if definition.handler != "surface_esp":
        raise AutomationValidationError(
            f"{definition.name} 使用独立的配对文件工作流，请从对应流程页面启动。"
        )
    normalized_settings = normalize_settings(settings)

    unique: dict[str, Path] = {}
    unsupported: list[str] = []
    for raw_path in input_paths:
        path = Path(raw_path).expanduser().resolve()
        if not _is_supported_input(path, definition):
            unsupported.append(path.name or str(path))
            continue
        unique.setdefault(os.path.normcase(str(path)), path)
    if unsupported:
        shown = "、".join(unsupported[:5])
        suffix = f" 等 {len(unsupported)} 个文件" if len(unsupported) > 5 else ""
        raise AutomationValidationError(f"当前流程不支持：{shown}{suffix}")
    paths = list(unique.values())
    if not paths:
        raise AutomationValidationError("没有可运行的输入文件。")

    root = Path(output_root).expanduser().resolve()
    plan_id = uuid.uuid4().hex[:10]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = _clean_file_part(prefix, "automatic")
    run_dir = root / f"{safe_prefix}_{timestamp}_{plan_id[:6]}"
    results_dir = run_dir / "results"
    jobs: list[AutomationJob] = []
    for index, input_path in enumerate(paths, 1):
        work_dir = run_dir / "jobs" / f"{index:04d}_{_clean_file_part(input_path.stem)}"
        result_dir = (
            input_path.parent
            if normalized_settings["output_location"] == "input_directory"
            else results_dir
        )
        jobs.append(
            AutomationJob(
                id=f"job_{index:04d}_{uuid.uuid4().hex[:6]}",
                index=index,
                input_path=input_path,
                work_dir=work_dir,
                result_dir=result_dir,
            )
        )
    return AutomationPlan(
        id=plan_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        workflow=definition,
        run_dir=run_dir,
        results_dir=results_dir,
        settings=normalized_settings,
        jobs=jobs,
    )


def resume_automation_plan(
    manifest_path: Path | str,
    input_paths: Iterable[Path | str],
    settings: dict | None = None,
) -> AutomationPlan:
    """Append pending files to a successful trial run without rerunning it."""
    manifest = Path(manifest_path).expanduser().resolve()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AutomationValidationError(f"无法继续试运行结果：{exc}") from exc
    workflow_raw = payload.get("workflow") if isinstance(payload, dict) else None
    workflow_id = str((workflow_raw or {}).get("id") or "")
    definition = _definition_map().get(workflow_id)
    if definition is None:
        raise AutomationValidationError("试运行记录中的流程类型无效。")
    stored_settings = normalize_settings(dict(payload.get("settings") or {}))
    if settings is not None:
        expected_settings = normalize_settings(settings)
        if _canonical_hash(expected_settings) != _canonical_hash(stored_settings):
            raise AutomationValidationError("流程设置已经改变，请重新执行首文件试运行。")
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise AutomationValidationError("试运行记录中没有可继续的任务。")
    jobs = [AutomationJob.from_dict(item) for item in raw_jobs if isinstance(item, dict)]
    if not jobs or any(job.status != STATUS_SUCCESS for job in jobs):
        raise AutomationValidationError("只有全部成功的试运行才能继续剩余文件。")

    existing = {os.path.normcase(str(job.input_path)) for job in jobs}
    run_dir = Path(str(payload.get("run_dir") or manifest.parent)).resolve()
    results_dir = Path(str(payload.get("results_dir") or run_dir / "results")).resolve()
    next_index = max(job.index for job in jobs) + 1
    for raw_path in input_paths:
        path = Path(raw_path).expanduser().resolve()
        key = os.path.normcase(str(path))
        if key in existing:
            continue
        if not _is_supported_input(path, definition):
            raise AutomationValidationError(f"当前流程不支持：{path.name}")
        result_dir = (
            path.parent
            if stored_settings["output_location"] == "input_directory"
            else results_dir
        )
        jobs.append(
            AutomationJob(
                id=f"job_{next_index:04d}_{uuid.uuid4().hex[:6]}",
                index=next_index,
                input_path=path,
                work_dir=run_dir
                / "jobs"
                / f"{next_index:04d}_{_clean_file_part(path.stem)}",
                result_dir=result_dir,
            )
        )
        existing.add(key)
        next_index += 1
    return AutomationPlan(
        id=str(payload.get("id") or uuid.uuid4().hex[:10]),
        created_at=str(payload.get("created_at") or datetime.now().isoformat(timespec="seconds")),
        workflow=definition,
        run_dir=run_dir,
        results_dir=results_dir,
        settings=stored_settings,
        jobs=jobs,
        status=STATUS_PENDING,
        resume=True,
    )


def build_automatic_vmd_tcl(
    style: dict,
    rep0_commands: list[str] | None,
    *,
    width: int,
    height: int,
) -> str:
    """Build a headless rendering script while preserving the shared scene contract."""
    base = vmd_core.build_vmd_tcl(style, rep0_commands=rep0_commands)
    removed_prefixes = (
        "menu main on",
        "menu graphics on",
        "menu render on",
        'puts "AutoCube: Isosurface drawing is ready;',
        'puts "AutoCube: Render manually in VMD.',
        'puts "AutoCube: After each successful render,',
    )
    lines = [
        line
        for line in base.rstrip().splitlines()
        if not line.strip().startswith(removed_prefixes)
    ]
    lines.extend(
        [
            "",
            f"display resize {max(320, int(width))} {max(240, int(height))}",
            "axes location Off",
            "set AUTO_RENDER_REQUEST $::env(RENDER_FILE)",
            "render TachyonInternal $AUTO_RENDER_REQUEST",
            'puts "MolecularStudio: automatic render finished"',
            "quit",
        ]
    )
    return "\n".join(lines) + "\n"


def build_interactive_vmd_tcl(
    style: dict, rep0_commands: list[str] | None
) -> str:
    return vmd_core.build_vmd_tcl(style, rep0_commands=rep0_commands)


EventCallback = Callable[[dict], None]


class AutomaticWorkflowRunner:
    def __init__(
        self,
        plan: AutomationPlan,
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
        self._active_job_total = max(1, len(plan.jobs))
        self._job_started_monotonic: dict[int, float] = {}

    def _emit(self, kind: str, **payload: object) -> None:
        if self.event_callback is not None:
            self.event_callback({"kind": kind, **payload})

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
                [
                    "序号",
                    "输入文件",
                    "状态",
                    "失败阶段",
                    "Multiwfn",
                    "VMD",
                    "耗时（秒）",
                    "图片",
                    "输出文件",
                    "错误",
                ]
            )
            for job in self.plan.jobs:
                writer.writerow(
                    [
                        job.index,
                        str(job.input_path),
                        job.status,
                        job.failed_stage,
                        job.multiwfn_status,
                        job.vmd_status,
                        f"{job.duration_seconds:.3f}",
                        job.image_path,
                        " | ".join(job.outputs),
                        job.error,
                    ]
                )

    def run(self) -> dict:
        if not self.multiwfn_exe.is_file():
            raise AutomationValidationError(
                f"Multiwfn.exe 路径无效：{self.multiwfn_exe}"
            )
        if (
            self.plan.settings["render_mode"] != "cubes_only"
            and not self.vmd_exe.is_file()
        ):
            raise AutomationValidationError(f"vmd.exe 路径无效：{self.vmd_exe}")

        self.plan.run_dir.mkdir(parents=True, exist_ok=self.plan.resume)
        self.plan.results_dir.mkdir(parents=True, exist_ok=True)
        (self.plan.run_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.plan.status = STATUS_RUNNING
        self._active_job_position = 0
        self._active_job_total = max(1, len(self.plan.jobs))
        self._write_manifest()
        self._emit(
            "run_started",
            run_dir=str(self.plan.run_dir),
            total=len(self.plan.jobs),
            workflow=self.plan.workflow.name,
        )

        completed = 0
        for job in self.plan.jobs:
            if self.plan.resume and job.status == STATUS_SUCCESS:
                completed += 1
                self._active_job_position = completed
                self._emit(
                    "progress",
                    current=completed,
                    completed=completed,
                    total=len(self.plan.jobs),
                    index=job.index,
                )
                continue
            if self._cancel_event.is_set():
                job.status = STATUS_CANCELLED
                job.error = "自动化流程已由用户停止。"
                self._emit_job(job, message=job.error)
            else:
                self._active_job_position = completed
                self._run_job(job)
            completed += 1
            self._active_job_position = completed
            self._write_manifest()
            self._emit(
                "progress",
                current=completed,
                completed=completed,
                total=len(self.plan.jobs),
                index=job.index,
            )

        statuses = {job.status for job in self.plan.jobs}
        if statuses == {STATUS_SUCCESS}:
            self.plan.status = STATUS_SUCCESS
        elif self._cancel_event.is_set() or STATUS_CANCELLED in statuses:
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
            "total": len(self.plan.jobs),
            "jobs": [job.to_dict() for job in self.plan.jobs],
        }
        self._emit("run_finished", **result)
        return result

    def _emit_job(self, job: AutomationJob, *, message: str = "") -> None:
        self._emit(
            "job_stage",
            index=job.index,
            input_file=str(job.input_path),
            stage=job.stage,
            status=job.status,
            message=message,
            elapsed_seconds=job.duration_seconds,
            outputs=list(job.outputs),
            image_path=job.image_path,
            can_retry_drawing=job.can_retry_drawing,
        )

    def _set_stage(self, job: AutomationJob, stage: str, message: str) -> None:
        job.stage = stage
        job.status = STATUS_RUNNING
        self._emit_job(job, message=message)
        start, _ceiling = AUTOMATION_STAGE_PROGRESS.get(stage, (0.0, 0.0))
        self._emit_job_progress(job, start, message)

    def _emit_job_progress(
        self, job: AutomationJob, task_percent: float, message: str
    ) -> None:
        local = max(0.0, min(100.0, float(task_percent)))
        overall = (
            self._active_job_position + local / 100.0
        ) / max(1, self._active_job_total) * 100.0
        started = self._job_started_monotonic.get(job.index)
        elapsed = max(0.0, time.monotonic() - started) if started else 0.0
        self._emit(
            "job_progress",
            index=job.index,
            input_file=str(job.input_path),
            stage=job.stage,
            status=job.status,
            task_percent=round(local, 2),
            percent=round(overall, 2),
            elapsed_seconds=round(elapsed, 1),
            message=message,
        )

    def _run_job(self, job: AutomationJob) -> None:
        started = time.monotonic()
        self._job_started_monotonic[job.index] = started
        job.started_at = datetime.now().isoformat(timespec="seconds")
        job.work_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._run_multiwfn(job)
            self._validate_cubes(job)
            render_mode = self.plan.settings["render_mode"]
            if render_mode == "automatic":
                self._render_automatic(job)
            elif render_mode == "interactive":
                self._open_interactive_vmd(job)
            else:
                job.vmd_status = "skipped"
            self._collect_success_outputs(job)
            job.status = STATUS_SUCCESS
            job.error = ""
            job.can_retry_drawing = False
        except _CancelledError:
            job.status = STATUS_CANCELLED
            job.error = "任务已由用户停止。"
        except _TimeoutError as exc:
            job.status = STATUS_TIMEOUT
            job.failed_stage = job.stage
            job.error = str(exc)
            job.can_retry_drawing = (
                job.multiwfn_status == STATUS_SUCCESS
                and job.stage == STAGE_VMD_RENDER
            )
            self._archive_recovery_outputs(job)
        except Exception as exc:
            job.status = STATUS_FAILED
            job.failed_stage = job.stage
            job.error = str(exc)
            job.can_retry_drawing = (
                job.multiwfn_status == STATUS_SUCCESS
                and job.stage == STAGE_VMD_RENDER
            )
            self._archive_recovery_outputs(job)
        finally:
            job.duration_seconds = time.monotonic() - started
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            _write_text_atomic(
                job.work_dir / "result.json",
                json.dumps(job.to_dict(), ensure_ascii=False, indent=2) + "\n",
            )
            message = (
                f"{job.input_path.name} 已完成"
                if job.status == STATUS_SUCCESS
                else job.error
            )
            self._emit_job(job, message=message)

    def _run_multiwfn(self, job: AutomationJob) -> None:
        self._set_stage(job, STAGE_MULTIWFN, "正在生成电子密度与 ESP Cube")
        settings = self.plan.settings
        command = [
            str(self.multiwfn_exe),
            str(job.input_path),
            "-isilent",
            "1",
            "-ESPrhoiso",
            str(settings["rho_iso"]),
        ]
        _write_text_atomic(job.work_dir / "stdin.txt", ESP_STDIN_SEQUENCE)
        _write_text_atomic(
            job.work_dir / "job.json",
            json.dumps(
                {
                    "workflow": self.plan.workflow.id,
                    "input": str(job.input_path),
                    "command": command,
                    "rho_iso": settings["rho_iso"],
                    "style_hash": settings["style_snapshot"].get("hash", ""),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        env = os.environ.copy()
        env["Multiwfnpath"] = str(self.multiwfn_exe.parent)
        return_code, reason = self._run_process(
            command,
            cwd=job.work_dir,
            env=env,
            stdin_text=ESP_STDIN_SEQUENCE,
            timeout_seconds=int(settings["multiwfn_timeout_seconds"]),
            log_path=job.work_dir / "multiwfn.log",
            source="Multiwfn",
            index=job.index,
            hide_window=True,
        )
        job.multiwfn_return_code = return_code
        if reason == "cancelled":
            job.multiwfn_status = STATUS_CANCELLED
            raise _CancelledError
        if reason == "timeout":
            job.multiwfn_status = STATUS_TIMEOUT
            raise _TimeoutError(
                f"Multiwfn 运行超过 {settings['multiwfn_timeout_seconds']} 秒，已停止。"
            )
        if return_code != 0:
            job.multiwfn_status = STATUS_FAILED
            raise RuntimeError(
                f"Multiwfn 未正常完成（退出码 {return_code}），请查看运行记录。"
            )
        job.multiwfn_status = STATUS_SUCCESS

    def _validate_cubes(self, job: AutomationJob) -> None:
        self._set_stage(job, STAGE_CUBE_VALIDATION, "正在检查 Cube 配对与空间网格")
        candidates = [
            path
            for path in job.work_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in {".cub", ".cube"}
        ]
        density = job.work_dir / "density.cub"
        potential = job.work_dir / "totesp.cub"
        if not density.is_file() or not potential.is_file():
            seed = density if density.is_file() else potential if potential.is_file() else None
            pair = vmd_core.find_esp_cube_pair(seed or job.work_dir / "density.cub", candidates)
            if pair is not None:
                density, potential = pair
        if not density.is_file():
            raise RuntimeError("Multiwfn 已结束，但没有生成电子密度 Cube（density.cub）。")
        if not potential.is_file():
            raise RuntimeError("Multiwfn 已结束，但没有生成 ESP Cube（totesp.cub）。")
        if density.stat().st_size <= 0 or potential.stat().st_size <= 0:
            raise RuntimeError("生成的 Cube 文件为空。")
        # Record both files before validation so a failure still preserves the
        # exact artifacts needed for diagnosis or a drawing-only retry.
        job.density_cube = str(density.resolve())
        job.esp_cube = str(potential.resolve())
        vmd_core.cube_grid_signature(density)
        vmd_core.cube_grid_signature(potential)
        if not vmd_core.cube_grids_compatible(density, potential):
            raise RuntimeError("电子密度与 ESP Cube 的空间网格不兼容，已停止绘图。")

    def _style_parts(self) -> tuple[dict, list[str]]:
        snapshot = self.plan.settings["style_snapshot"]
        return copy.deepcopy(snapshot["style"]), list(snapshot.get("rep0_commands") or [])

    def _vmd_environment(self, job: AutomationJob) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "CUBE_FILE": job.density_cube,
                "COLOR_CUBE_FILE": job.esp_cube,
                "ISO_NORM": str(self.plan.settings["rho_iso"]),
                "A_DIR": str(job.work_dir),
            }
        )
        return env

    def _capture_vmd_view(self, job: AutomationJob) -> orbital_vmd.VmdViewState:
        self._set_stage(
            job,
            STAGE_VMD_RENDER,
            "请在 VMD 中调整角度与显示效果，完成后点击“保存全部参数并确认”",
        )
        style, rep0_commands = self._style_parts()
        protocol = job.work_dir / "esp_view.capture"
        cancel_marker = orbital_vmd.capture_cancel_marker_path(protocol)
        error_log = orbital_vmd.capture_error_log_path(protocol)
        native_state = job.work_dir / "esp_final_state.vmd"
        for path in (protocol, cancel_marker, error_log, native_state):
            path.unlink(missing_ok=True)
        initial_scene = build_interactive_vmd_tcl(style, rep0_commands)
        capture_script = orbital_vmd.build_interactive_capture_tcl(
            job.density_cube,
            protocol,
            style,
            rep0_commands=rep0_commands,
            width=INTERACTIVE_VMD_VIEWPORT[0],
            height=INTERACTIVE_VMD_VIEWPORT[1],
            debug_state_path=native_state,
            initial_scene_tcl=initial_scene,
        )
        script_path = job.work_dir / "adjust_esp_view.vmd"
        _write_text_atomic(script_path, capture_script)
        self._emit(
            "vmd_interaction_required",
            index=job.index,
            input_file=str(job.input_path),
            message="VMD 已打开，可自由调整；确认后将自动使用 Tachyon 渲染。",
        )
        return_code, reason = self._run_process(
            [str(self.vmd_exe), "-e", str(script_path)],
            cwd=job.work_dir,
            env=self._vmd_environment(job),
            stdin_text=None,
            timeout_seconds=int(self.plan.settings["vmd_timeout_seconds"]),
            log_path=job.work_dir / "vmd_viewpoint.log",
            source="VMD",
            index=job.index,
            hide_window=False,
            show_window=True,
            completion_markers={
                "viewpoint_confirmed": protocol,
                "viewpoint_cancelled": cancel_marker,
            },
        )
        job.vmd_return_code = return_code
        if reason != "cancelled":
            if cancel_marker.is_file() and not protocol.is_file():
                reason = "viewpoint_cancelled"
            elif protocol.is_file():
                reason = "viewpoint_confirmed"
        if reason in {"cancelled", "viewpoint_cancelled"}:
            job.vmd_status = STATUS_CANCELLED
            raise _CancelledError
        if reason == "timeout":
            job.vmd_status = STATUS_TIMEOUT
            raise _TimeoutError("等待 VMD 调整确认超时，Cube 已保留。")
        if return_code != 0 and reason != "viewpoint_confirmed":
            job.vmd_status = STATUS_FAILED
            detail = f"；诊断记录：{error_log}" if error_log.is_file() else ""
            raise RuntimeError(f"VMD 调整阶段未正常结束（退出码 {return_code}）{detail}。")
        if not protocol.is_file():
            job.vmd_status = STATUS_FAILED
            raise RuntimeError("没有确认 VMD 显示参数，Cube 已保留。")
        state = orbital_vmd.load_view_state(
            protocol,
            expected_geometry_fingerprint=orbital_vmd.cube_geometry_fingerprint(
                job.density_cube
            ),
        )
        normalized = job.work_dir / "esp_viewpoint.json"
        state.save_json(normalized)
        job.viewpoint_path = str(normalized.resolve())
        if native_state.is_file():
            job.vmd_save_state_path = str(native_state.resolve())
        self._emit_job_progress(job, 88.0, "VMD 参数已确认，正在准备 Tachyon 渲染")
        return state

    def _render_automatic(self, job: AutomationJob) -> None:
        state = self._capture_vmd_view(job)
        self._emit_job_progress(job, 89.0, "正在使用 Tachyon 渲染最终图片")
        script_path = job.work_dir / "automatic_render.vmd"
        native_name = f"{_clean_file_part(job.input_path.stem)}_ESP_render.tga"
        native_output = job.work_dir / native_name
        render_script = orbital_vmd.build_batch_render_tcl(
            job.density_cube,
            native_output,
            state,
            width=int(self.plan.settings["width"]),
            height=int(self.plan.settings["height"]),
            renderer="TachyonInternal",
            native_state_path=(
                job.vmd_save_state_path if job.vmd_save_state_path else None
            ),
            reference_cube_path=(
                job.density_cube if job.vmd_save_state_path else None
            ),
        )
        _write_text_atomic(script_path, render_script)
        marker = time.time_ns()
        command = [str(self.vmd_exe), "-dispdev", "text", "-eofexit", "-e", str(script_path)]
        return_code, reason = self._run_process(
            command,
            cwd=job.work_dir,
            env=self._vmd_environment(job),
            stdin_text=None,
            timeout_seconds=int(self.plan.settings["vmd_timeout_seconds"]),
            log_path=job.work_dir / "vmd.log",
            source="VMD",
            index=job.index,
            hide_window=True,
        )
        job.vmd_return_code = return_code
        if reason == "cancelled":
            job.vmd_status = STATUS_CANCELLED
            raise _CancelledError
        if reason == "timeout":
            job.vmd_status = STATUS_TIMEOUT
            raise _TimeoutError(
                f"VMD 渲染超过 {self.plan.settings['vmd_timeout_seconds']} 秒，已停止。"
            )
        if return_code != 0:
            job.vmd_status = STATUS_FAILED
            raise RuntimeError(f"VMD 渲染失败（退出码 {return_code}），Cube 已保留。")
        native = self._locate_render_output(job.work_dir, native_name, marker)
        if native is None:
            job.vmd_status = STATUS_FAILED
            raise RuntimeError("VMD 已退出，但没有生成有效图片；Cube 已保留。")
        png = job.work_dir / f"{_clean_file_part(job.input_path.stem)}_ESP.png"
        try:
            self._convert_image_to_png(native, png)
        except Exception:
            job.vmd_status = STATUS_FAILED
            raise
        if not png.is_file() or png.stat().st_size <= 0:
            job.vmd_status = STATUS_FAILED
            raise RuntimeError("VMD 图片转换为 PNG 后校验失败；原始渲染文件已保留。")
        job.image_path = str(png.resolve())
        job.vmd_status = STATUS_SUCCESS
        self._emit_job_progress(job, 96.0, "Tachyon 图片已生成")

    def _open_interactive_vmd(self, job: AutomationJob) -> None:
        self._capture_vmd_view(job)
        job.vmd_status = STATUS_SUCCESS

    @staticmethod
    def _locate_render_output(
        folder: Path, requested_name: str, marker_ns: int
    ) -> Path | None:
        requested = folder / Path(requested_name).name
        candidates = [requested]
        candidates.extend(
            sorted(
                (
                    path
                    for path in folder.iterdir()
                    if path.is_file()
                    and path.suffix.casefold() in {".tga", ".bmp", ".ppm", ".png"}
                    and path.stat().st_mtime_ns >= marker_ns
                ),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        )
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size > 64:
                return candidate
        return None

    @staticmethod
    def _convert_image_to_png(source: Path, target: Path) -> None:
        try:
            from PySide6.QtGui import QImage
        except ImportError as exc:  # pragma: no cover - application dependency
            raise RuntimeError("缺少图片转换组件，无法生成 PNG。") from exc
        image = QImage()
        if source.suffix.casefold() == ".tga":
            try:
                payload = source.read_bytes()
                if len(payload) >= 18 and payload[1] == 0 and payload[2] == 2:
                    id_length = payload[0]
                    width = int.from_bytes(payload[12:14], "little")
                    height = int.from_bytes(payload[14:16], "little")
                    depth = payload[16]
                    descriptor = payload[17]
                    bytes_per_pixel = depth // 8
                    offset = 18 + id_length
                    expected = width * height * bytes_per_pixel
                    pixels = payload[offset : offset + expected]
                    if width > 0 and height > 0 and len(pixels) == expected:
                        if depth == 24:
                            image = QImage(
                                pixels,
                                width,
                                height,
                                width * 3,
                                QImage.Format.Format_BGR888,
                            ).copy()
                        elif depth == 32:
                            image = QImage(
                                pixels,
                                width,
                                height,
                                width * 4,
                                QImage.Format.Format_ARGB32,
                            ).copy()
                        if not image.isNull():
                            if not descriptor & 0x20:
                                image = image.mirrored(False, True)
                            if descriptor & 0x10:
                                image = image.mirrored(True, False)
            except OSError:
                image = QImage()
        if image.isNull():
            image = QImage(str(source))
        if image.isNull():
            raise RuntimeError(f"无法读取 VMD 渲染图片：{source.name}")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp.png")
        try:
            if not image.save(str(temporary), "PNG"):
                raise RuntimeError("无法把 VMD 渲染结果转换为 PNG。")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _copy_unique(self, source: Path, target: Path) -> Path:
        target = _unique_target(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    def _collect_success_outputs(self, job: AutomationJob) -> None:
        self._set_stage(job, STAGE_COLLECT, "正在整理图片、Cube 与运行记录")
        job.result_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = self.plan.run_dir / "logs"
        stem = _clean_file_part(job.input_path.stem)
        for source, label in (
            (job.work_dir / "multiwfn.log", "Multiwfn"),
            (job.work_dir / "vmd.log", "VMD"),
            (job.work_dir / "vmd_viewpoint.log", "VMD_adjustment"),
        ):
            if source.is_file():
                copied = self._copy_unique(source, logs_dir / f"{stem}_{label}.log")
                job.outputs.append(str(copied))

        if self.plan.settings["keep_cubes"]:
            density = self._copy_unique(
                Path(job.density_cube), job.result_dir / f"{stem}_density.cub"
            )
            esp = self._copy_unique(
                Path(job.esp_cube), job.result_dir / f"{stem}_ESP.cub"
            )
            job.outputs.extend([str(density), str(esp)])

        if job.image_path:
            style = self.plan.settings["style_snapshot"].get("style") or {}
            style_id = _clean_file_part(str(style.get("id") or "ESP"), "ESP")
            image = self._copy_unique(
                Path(job.image_path), job.result_dir / f"{stem}_ESP_{style_id}.png"
            )
            job.image_path = str(image)
            job.outputs.append(str(image))
        viewpoint = Path(job.viewpoint_path) if job.viewpoint_path else Path()
        if job.viewpoint_path and viewpoint.is_file():
            copied = self._copy_unique(
                viewpoint, job.result_dir / f"{stem}_ESP_viewpoint.json"
            )
            job.outputs.append(str(copied))
        native_state = (
            Path(job.vmd_save_state_path) if job.vmd_save_state_path else Path()
        )
        if job.vmd_save_state_path and native_state.is_file():
            copied = self._copy_unique(
                native_state, job.result_dir / f"{stem}_ESP_final_state.vmd"
            )
            job.outputs.append(str(copied))

        if not self.plan.settings["keep_cubes"]:
            Path(job.density_cube).unlink(missing_ok=True)
            Path(job.esp_cube).unlink(missing_ok=True)

    def _archive_recovery_outputs(self, job: AutomationJob) -> None:
        recovery = self.plan.run_dir / "recovery" / f"{job.index:04d}_{_clean_file_part(job.input_path.stem)}"
        for source in (
            Path(job.density_cube) if job.density_cube else None,
            Path(job.esp_cube) if job.esp_cube else None,
            job.work_dir / "multiwfn.log",
            job.work_dir / "vmd.log",
            job.work_dir / "vmd_viewpoint.log",
            job.work_dir / "automatic_render.vmd",
            job.work_dir / "adjust_esp_view.vmd",
            Path(job.viewpoint_path) if job.viewpoint_path else None,
            Path(job.vmd_save_state_path) if job.vmd_save_state_path else None,
        ):
            if source is not None and source.is_file():
                copied = self._copy_unique(source, recovery / source.name)
                if str(copied) not in job.outputs:
                    job.outputs.append(str(copied))

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
        index: int,
        hide_window: bool,
        show_window: bool = False,
        completion_markers: Mapping[str, Path] | None = None,
    ) -> tuple[int, str]:
        creation_flags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" and hide_window
            else 0
        )
        encoding = locale.getpreferredencoding(False) or "utf-8"
        existing_vmd_windows = (
            orbital_vmd.vmd_display_window_handles() if show_window else set()
        )
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
            next_window_check = started
            next_heartbeat = started
            stream_finished = False
            reason = ""
            last_process_percent = 0.0
            last_raw_percent = -1.0
            progress_pass = 0
            markers = tuple(
                (str(marker_reason), Path(marker_path))
                for marker_reason, marker_path in (completion_markers or {}).items()
            )
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                while process.poll() is None or not stream_finished:
                    now = time.monotonic()
                    if (
                        show_window
                        and now >= next_window_check
                        and now - started <= 20.0
                    ):
                        orbital_vmd.restore_vmd_display_window(
                            process.pid,
                            excluded_handles=existing_vmd_windows,
                            width=INTERACTIVE_VMD_WINDOW[0],
                            height=INTERACTIVE_VMD_WINDOW[1],
                            topmost=True,
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
                        text = item.rstrip("\r\n")
                        if text:
                            self._emit("output", index=index, source=source, text=text)
                            progress_match = re.search(
                                r"Progress:\s*\[[^\]]*\]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
                                text,
                                re.IGNORECASE,
                            )
                            if progress_match and source.casefold() == "multiwfn":
                                raw_percent = max(
                                    0.0,
                                    min(100.0, float(progress_match.group(1))),
                                )
                                if (
                                    last_raw_percent >= 75.0
                                    and raw_percent <= 25.0
                                ):
                                    progress_pass = min(1, progress_pass + 1)
                                last_raw_percent = raw_percent
                                process_fraction = (
                                    progress_pass + raw_percent / 100.0
                                ) / 2.0
                                start, ceiling = AUTOMATION_STAGE_PROGRESS[
                                    STAGE_MULTIWFN
                                ]
                                last_process_percent = start + (
                                    ceiling - start
                                ) * process_fraction
                                job = next(
                                    (
                                        candidate
                                        for candidate in self.plan.jobs
                                        if candidate.index == index
                                    ),
                                    None,
                                )
                                if job is not None:
                                    self._emit_job_progress(
                                        job,
                                        last_process_percent,
                                        f"正在生成表面数据 · {raw_percent:.0f}%",
                                    )
                    if now >= next_heartbeat:
                        job = next(
                            (
                                candidate
                                for candidate in self.plan.jobs
                                if candidate.index == index
                            ),
                            None,
                        )
                        if job is not None:
                            start, _ceiling = AUTOMATION_STAGE_PROGRESS.get(
                                job.stage, (last_process_percent, last_process_percent)
                            )
                            heartbeat_percent = max(start, last_process_percent)
                            heartbeat_message = {
                                STAGE_MULTIWFN: "正在生成电子密度与静电势数据",
                                STAGE_VMD_RENDER: "正在等待 VMD 调整或完成渲染",
                            }.get(job.stage, "正在处理")
                            self._emit_job_progress(
                                job, heartbeat_percent, heartbeat_message
                            )
                        next_heartbeat = now + 1.0
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
                reader.join(timeout=1.0)
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


class _CancelledError(RuntimeError):
    pass


class _TimeoutError(RuntimeError):
    pass


def retry_drawing_from_manifest(
    manifest_path: Path | str,
    job_id: str,
    vmd_exe: Path | str,
    *,
    event_callback: EventCallback | None = None,
    runner_ready: Callable[[AutomaticWorkflowRunner], None] | None = None,
) -> dict:
    """Retry only the VMD phase using preserved Cube files from a failed job."""
    manifest_file = Path(manifest_path).expanduser().resolve()
    try:
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AutomationValidationError(f"无法读取自动化运行记录：{exc}") from exc
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise AutomationValidationError("运行记录中没有可重试的任务。")
    raw_job = next((item for item in jobs if str(item.get("id")) == str(job_id)), None)
    if not isinstance(raw_job, dict):
        raise AutomationValidationError("没有找到需要重试的任务。")
    settings = normalize_settings(dict(payload.get("settings") or {}))
    density = Path(str(raw_job.get("density_cube") or ""))
    potential = Path(str(raw_job.get("esp_cube") or ""))
    if not density.is_file() or not potential.is_file():
        recovery = manifest_file.parent / "recovery" / f"{int(raw_job.get('index') or 0):04d}_{_clean_file_part(Path(str(raw_job.get('input_path') or 'input')).stem)}"
        density = recovery / "density.cub"
        potential = recovery / "totesp.cub"
    if not density.is_file() or not potential.is_file():
        raise AutomationValidationError("重试绘图所需的两个 Cube 文件不存在。")
    if not vmd_core.cube_grids_compatible(density, potential):
        raise AutomationValidationError("重试绘图所需的 Cube 空间网格不兼容。")

    input_path = Path(str(raw_job.get("input_path") or "input.fch"))
    work_dir = manifest_file.parent / "retries" / f"{_clean_file_part(input_path.stem)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    work_dir.mkdir(parents=True, exist_ok=False)
    retry_job = AutomationJob(
        id=str(raw_job.get("id") or job_id),
        index=int(raw_job.get("index") or 1),
        input_path=input_path,
        work_dir=work_dir,
        result_dir=Path(str(raw_job.get("result_dir") or manifest_file.parent / "results")),
        multiwfn_status=STATUS_SUCCESS,
        density_cube=str(density.resolve()),
        esp_cube=str(potential.resolve()),
    )
    definition = _definition_map()[WORKFLOW_SURFACE_ESP]
    plan = AutomationPlan(
        id=str(payload.get("id") or uuid.uuid4().hex[:10]),
        created_at=datetime.now().isoformat(timespec="seconds"),
        workflow=definition,
        run_dir=manifest_file.parent,
        results_dir=Path(str(payload.get("results_dir") or manifest_file.parent / "results")),
        settings=settings,
        jobs=[retry_job],
    )
    runner = AutomaticWorkflowRunner(
        plan,
        Path(sys_executable_placeholder()),
        vmd_exe,
        event_callback=event_callback,
    )
    if runner_ready is not None:
        runner_ready(runner)
    started = time.monotonic()
    retry_job.started_at = datetime.now().isoformat(timespec="seconds")
    try:
        runner._render_automatic(retry_job)
        runner._collect_success_outputs(retry_job)
        retry_job.status = STATUS_SUCCESS
        retry_job.can_retry_drawing = False
    except _CancelledError:
        retry_job.status = STATUS_CANCELLED
        retry_job.error = "重试绘图已由用户停止。"
    except _TimeoutError as exc:
        retry_job.status = STATUS_TIMEOUT
        retry_job.error = str(exc)
        retry_job.can_retry_drawing = True
    except Exception as exc:
        retry_job.status = STATUS_FAILED
        retry_job.error = str(exc)
        retry_job.can_retry_drawing = True
    retry_job.duration_seconds = time.monotonic() - started
    retry_job.finished_at = datetime.now().isoformat(timespec="seconds")
    result = retry_job.to_dict()
    raw_job.update(result)
    payload.setdefault("drawing_retries", []).append(
        {
            "job_id": retry_job.id,
            "started_at": retry_job.started_at,
            "finished_at": retry_job.finished_at,
            "status": retry_job.status,
            "image_path": retry_job.image_path,
            "error": retry_job.error,
        }
    )
    _write_text_atomic(
        manifest_file,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return result


def sys_executable_placeholder() -> str:
    """Return an existing path for retry-only runners; Multiwfn is never invoked."""
    import sys

    return sys.executable
