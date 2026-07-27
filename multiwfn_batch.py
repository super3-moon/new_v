from __future__ import annotations

import csv
import json
import locale
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
from typing import Callable, Iterable


PRESET_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
CURRENT_MULTIWFN_VERSION = "2026.7.11"
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
VERSION_RE = re.compile(r"\bVersion\s+(\d{4}\.\d+\.\d+)\b", re.IGNORECASE)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"


class BatchValidationError(ValueError):
    pass


def _write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _clean_identifier(value: str, fallback: str = "preset") -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip()).strip("_")
    return (text or fallback)[:80]


def _clean_file_part(value: str, fallback: str = "job") -> str:
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return (text or fallback)[:100]


def normalize_extensions(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        extension = str(value or "").strip().lower()
        if not extension:
            continue
        extension = extension.removeprefix("*")
        if not extension.startswith("."):
            extension = f".{extension}"
        if not re.fullmatch(r"\.[a-z0-9][a-z0-9._+-]*", extension):
            raise BatchValidationError(f"无效的输入扩展名：{value}")
        if extension not in result:
            result.append(extension)
    if not result:
        raise BatchValidationError("批量流程至少需要一个输入扩展名。")
    return result


def read_command_text_file(path: Path | str) -> str:
    """Read a Multiwfn input stream without losing blank command lines."""
    source = Path(path).expanduser().resolve()
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise BatchValidationError(f"无法读取命令文件：{exc}") from exc
    if not payload:
        raise BatchValidationError("命令文件为空。")

    preferred = locale.getpreferredencoding(False) or "utf-8"
    encodings = (
        ["utf-16", "utf-8-sig", preferred, "gb18030"]
        if payload.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ["utf-8-sig", preferred, "gb18030", "utf-16"]
    )
    text = ""
    error: UnicodeDecodeError | None = None
    for encoding in dict.fromkeys(encodings):
        try:
            decoded = payload.decode(encoding)
            if "\x00" in decoded and encoding != "utf-16":
                continue
            text = decoded
            break
        except (LookupError, UnicodeDecodeError) as exc:
            if isinstance(exc, UnicodeDecodeError):
                error = exc
    else:
        raise BatchValidationError(f"无法识别命令文件的文字编码：{error}")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise BatchValidationError("命令文件包含无法识别的二进制内容。")
    if not text.strip():
        raise BatchValidationError("命令文件没有可执行的 Multiwfn 输入。")
    return text


def extract_placeholders(*templates: str) -> list[str]:
    names: set[str] = set()
    for template in templates:
        names.update(PLACEHOLDER_RE.findall(str(template or "")))
    return sorted(names)


def render_template(template: str, values: dict[str, object]) -> str:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            missing.add(name)
            return match.group(0)
        return str(values[name])

    rendered = PLACEHOLDER_RE.sub(replace, str(template or ""))
    if missing:
        raise BatchValidationError(
            "缺少模板参数：" + "、".join(sorted(missing))
        )
    return rendered


@dataclass(slots=True)
class OutputRule:
    pattern: str
    rename: str = ""
    required: bool = True

    @classmethod
    def from_dict(cls, raw: dict) -> "OutputRule":
        if not isinstance(raw, dict):
            raise BatchValidationError("输出规则格式无效。")
        pattern = str(raw.get("pattern") or "").strip()
        if not pattern:
            raise BatchValidationError("输出规则缺少匹配模式。")
        return cls(
            pattern=pattern,
            rename=str(raw.get("rename") or "").strip(),
            required=bool(raw.get("required", True)),
        )

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "rename": self.rename,
            "required": self.required,
        }


@dataclass(slots=True)
class BatchPreset:
    id: str
    name: str
    description: str
    input_extensions: list[str]
    arguments: list[str]
    stdin_template: str
    output_rules: list[OutputRule]
    variables: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 600
    multiwfn_version: str = ""
    builtin: bool = False

    def validate(self) -> None:
        self.id = _clean_identifier(self.id, f"preset_{uuid.uuid4().hex[:8]}")
        self.name = str(self.name or "").strip()
        if not self.name:
            raise BatchValidationError("批量流程名称不能为空。")
        self.description = str(self.description or "").strip()
        self.input_extensions = normalize_extensions(self.input_extensions)
        self.arguments = [str(item) for item in self.arguments if str(item).strip()]
        self.stdin_template = str(self.stdin_template or "")
        if not self.stdin_template.strip():
            raise BatchValidationError("Multiwfn 操作流程不能为空。")
        self.output_rules = [
            rule if isinstance(rule, OutputRule) else OutputRule.from_dict(rule)
            for rule in self.output_rules
        ]
        self.variables = {
            str(key).strip(): str(value)
            for key, value in dict(self.variables).items()
            if str(key).strip()
        }
        self.timeout_seconds = max(1, min(86400, int(self.timeout_seconds)))
        self.multiwfn_version = str(self.multiwfn_version or "").strip()

        builtins = {
            "input",
            "input_dir",
            "name",
            "stem",
            "ext",
            "index",
            "job_dir",
            "output_dir",
            "source_name",
            "source_stem",
            "source_ext",
            "match_index",
        }
        referenced = set(
            extract_placeholders(
                self.stdin_template,
                *self.arguments,
                *(rule.pattern for rule in self.output_rules),
                *(rule.rename for rule in self.output_rules),
            )
        )
        missing_defaults = referenced - builtins - set(self.variables)
        if missing_defaults:
            raise BatchValidationError(
                "以下自定义参数没有默认值：" + "、".join(sorted(missing_defaults))
            )

    @classmethod
    def from_dict(cls, raw: dict, *, builtin: bool = False) -> "BatchPreset":
        if not isinstance(raw, dict):
            raise BatchValidationError("批量流程必须是 JSON 对象。")
        preset = cls(
            id=str(raw.get("id") or f"preset_{uuid.uuid4().hex[:8]}"),
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            input_extensions=list(raw.get("input_extensions") or []),
            arguments=list(raw.get("arguments") or []),
            stdin_template=str(raw.get("stdin_template") or ""),
            output_rules=[
                OutputRule.from_dict(item) for item in (raw.get("output_rules") or [])
            ],
            variables=dict(raw.get("variables") or {}),
            timeout_seconds=int(raw.get("timeout_seconds") or 600),
            multiwfn_version=str(raw.get("multiwfn_version") or ""),
            builtin=builtin,
        )
        preset.validate()
        return preset

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "input_extensions": list(self.input_extensions),
            "arguments": list(self.arguments),
            "stdin_template": self.stdin_template,
            "output_rules": [rule.to_dict() for rule in self.output_rules],
            "variables": dict(self.variables),
            "timeout_seconds": self.timeout_seconds,
            "multiwfn_version": self.multiwfn_version,
        }


def builtin_presets() -> list[BatchPreset]:
    presets = [
        BatchPreset(
            id="builtin_export_xyz",
            name="格式转换：导出 XYZ",
            description="将常见结构/波函数文件批量导出为同名 XYZ 文件。",
            input_extensions=[
                ".fch",
                ".fchk",
                ".wfn",
                ".wfx",
                ".mwfn",
                ".molden",
                ".molden.input",
                ".gjf",
                ".out",
                ".xyz",
                ".pdb",
            ],
            arguments=["-isilent", "1"],
            stdin_template="100\n2\n2\n${stem}.xyz\n0\nq\n",
            output_rules=[OutputRule("${stem}.xyz", "${stem}.xyz")],
            timeout_seconds=300,
            multiwfn_version=CURRENT_MULTIWFN_VERSION,
            builtin=True,
        ),
        BatchPreset(
            id="builtin_esp_density_cube",
            name="ESP + 电子密度 Cube",
            description="在电子密度等值面上计算静电势，并分别保存密度与 ESP Cube。",
            input_extensions=[
                ".fch",
                ".fchk",
                ".wfn",
                ".wfx",
                ".mwfn",
                ".molden",
                ".molden.input",
            ],
            arguments=["-isilent", "1", "-ESPrhoiso", "${rho_iso}"],
            stdin_template="5\n1\n3\n2\n0\n5\n12\n1\n2\n0\nq\n",
            output_rules=[
                OutputRule("density.cub", "${stem}_density.cub"),
                OutputRule("totesp.cub", "${stem}_ESP.cub"),
            ],
            variables={"rho_iso": "0.001"},
            timeout_seconds=1800,
            multiwfn_version=CURRENT_MULTIWFN_VERSION,
            builtin=True,
        ),
        BatchPreset(
            id="builtin_surface_esp",
            name="分子表面 ESP 描述符",
            description="计算分子范德华表面的静电势描述符，并为每个文件保留完整文本结果。",
            input_extensions=[
                ".fch",
                ".fchk",
                ".wfn",
                ".wfx",
                ".mwfn",
                ".molden",
                ".molden.input",
            ],
            arguments=["-isilent", "1"],
            stdin_template="12\n0\n-1\n-1\nq\n",
            output_rules=[
                OutputRule("stdout.log", "${stem}_surface_esp.txt")
            ],
            timeout_seconds=1200,
            multiwfn_version=CURRENT_MULTIWFN_VERSION,
            builtin=True,
        ),
    ]
    for preset in presets:
        preset.validate()
    return presets


def load_user_presets(path: Path) -> list[BatchPreset]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchValidationError(f"无法读取自定义批量流程：{exc}") from exc
    raw_presets = payload.get("presets", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_presets, list):
        raise BatchValidationError("自定义批量流程文件格式无效。")
    return [BatchPreset.from_dict(item) for item in raw_presets]


def save_user_presets(path: Path, presets: Iterable[BatchPreset]) -> None:
    unique: dict[str, BatchPreset] = {}
    for preset in presets:
        preset.validate()
        if preset.builtin:
            continue
        unique[preset.id] = preset
    payload = {
        "schema_version": PRESET_SCHEMA_VERSION,
        "presets": [preset.to_dict() for preset in unique.values()],
    }
    _write_text_atomic(
        Path(path), json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )


def load_preset_file(path: Path) -> list[BatchPreset]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchValidationError(f"无法导入批量流程：{exc}") from exc
    if isinstance(payload, dict) and "presets" in payload:
        raw_items = payload["presets"]
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = [payload]
    if not isinstance(raw_items, list):
        raise BatchValidationError("导入文件不包含有效的批量流程。")
    return [BatchPreset.from_dict(item) for item in raw_items]


def save_preset_file(path: Path, presets: Iterable[BatchPreset]) -> None:
    items = [preset.to_dict() for preset in presets]
    payload = {
        "schema_version": PRESET_SCHEMA_VERSION,
        "presets": items,
    }
    _write_text_atomic(
        Path(path), json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )


@dataclass(slots=True)
class BatchJob:
    id: str
    index: int
    input_path: Path
    work_dir: Path
    status: str = STATUS_PENDING
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    return_code: int | None = None
    error: str = ""
    outputs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "index": self.index,
            "input_path": str(self.input_path),
            "work_dir": str(self.work_dir),
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "return_code": self.return_code,
            "error": self.error,
            "outputs": list(self.outputs),
        }


@dataclass(slots=True)
class BatchPlan:
    id: str
    created_at: str
    run_dir: Path
    results_dir: Path
    preset: BatchPreset
    variables: dict[str, str]
    jobs: list[BatchJob]
    status: str = STATUS_PENDING
    detected_multiwfn_version: str = ""

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def summary_path(self) -> Path:
        return self.run_dir / "summary.csv"

    def to_dict(self) -> dict:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "id": self.id,
            "created_at": self.created_at,
            "run_dir": str(self.run_dir),
            "results_dir": str(self.results_dir),
            "status": self.status,
            "preset": self.preset.to_dict(),
            "variables": dict(self.variables),
            "detected_multiwfn_version": self.detected_multiwfn_version,
            "jobs": [job.to_dict() for job in self.jobs],
        }


def create_batch_plan(
    input_paths: Iterable[Path | str],
    preset: BatchPreset,
    output_root: Path | str,
    variables: dict[str, object] | None = None,
    *,
    prefix: str = "batch",
) -> BatchPlan:
    preset.validate()
    allowed = set(preset.input_extensions)
    unique: dict[str, Path] = {}
    unsupported: list[str] = []
    for raw_path in input_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise BatchValidationError(f"输入文件不存在：{path}")
        suffixes = "".join(path.suffixes).lower()
        matched = path.suffix.lower() in allowed or suffixes in allowed
        if not matched:
            unsupported.append(path.name)
            continue
        unique.setdefault(os.path.normcase(str(path)), path)
    if unsupported:
        shown = "、".join(unsupported[:5])
        more = f" 等 {len(unsupported)} 个文件" if len(unsupported) > 5 else ""
        raise BatchValidationError(f"当前模板不支持：{shown}{more}")
    paths = list(unique.values())
    if not paths:
        raise BatchValidationError("没有可运行的输入文件。")

    resolved_variables = dict(preset.variables)
    resolved_variables.update(
        {str(key): str(value) for key, value in dict(variables or {}).items()}
    )
    missing = set(preset.variables) - set(resolved_variables)
    if missing:
        raise BatchValidationError("缺少参数：" + "、".join(sorted(missing)))

    root = Path(output_root).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_id = uuid.uuid4().hex[:10]
    run_dir = root / f"{_clean_identifier(prefix, 'batch')}_{timestamp}_{plan_id[:6]}"
    results_dir = run_dir / "results"
    jobs: list[BatchJob] = []
    for index, path in enumerate(paths, 1):
        job_id = f"job_{index:04d}_{uuid.uuid4().hex[:6]}"
        folder = f"{index:04d}_{_clean_file_part(path.stem)}"
        jobs.append(
            BatchJob(
                id=job_id,
                index=index,
                input_path=path,
                work_dir=run_dir / "jobs" / folder,
            )
        )
    return BatchPlan(
        id=plan_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        run_dir=run_dir,
        results_dir=results_dir,
        preset=preset,
        variables=resolved_variables,
        jobs=jobs,
    )


def job_template_values(plan: BatchPlan, job: BatchJob) -> dict[str, object]:
    path = job.input_path
    return {
        **plan.variables,
        "input": str(path),
        "input_dir": str(path.parent),
        "name": path.name,
        "stem": path.stem,
        "ext": path.suffix,
        "index": job.index,
        "job_dir": str(job.work_dir),
        "output_dir": str(plan.results_dir),
    }


def render_job_preview(
    plan: BatchPlan, job: BatchJob, multiwfn_exe: Path | str
) -> dict[str, object]:
    values = job_template_values(plan, job)
    arguments = [render_template(item, values) for item in plan.preset.arguments]
    stdin_text = render_template(plan.preset.stdin_template, values)
    if not stdin_text.endswith("\n"):
        stdin_text += "\n"
    return {
        "command": [str(Path(multiwfn_exe)), str(job.input_path), *arguments],
        "stdin": stdin_text,
        "work_dir": str(job.work_dir),
        "values": values,
    }


EventCallback = Callable[[dict], None]


class MultiwfnBatchRunner:
    def __init__(
        self,
        plan: BatchPlan,
        multiwfn_exe: Path | str,
        *,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.plan = plan
        self.multiwfn_exe = Path(multiwfn_exe).expanduser().resolve()
        self.event_callback = event_callback
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _emit(self, kind: str, **payload: object) -> None:
        if self.event_callback is not None:
            self.event_callback({"kind": kind, **payload})

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
                    "耗时（秒）",
                    "退出码",
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
                        f"{job.duration_seconds:.3f}",
                        "" if job.return_code is None else job.return_code,
                        " | ".join(job.outputs),
                        job.error,
                    ]
                )

    def run(self) -> dict:
        if not self.multiwfn_exe.is_file():
            raise BatchValidationError(f"Multiwfn.exe 路径无效：{self.multiwfn_exe}")
        self.plan.run_dir.mkdir(parents=True, exist_ok=False)
        self.plan.results_dir.mkdir(parents=True, exist_ok=True)
        self.plan.status = STATUS_RUNNING
        self._write_manifest()
        self._emit(
            "batch_started",
            run_dir=str(self.plan.run_dir),
            total=len(self.plan.jobs),
            preset=self.plan.preset.name,
        )

        completed = 0
        for job in self.plan.jobs:
            if self._cancel_event.is_set():
                job.status = STATUS_CANCELLED
                job.error = "批处理已由用户取消。"
                self._emit(
                    "job_status",
                    index=job.index,
                    status=job.status,
                    message=job.error,
                )
                continue
            self._run_job(job)
            completed += 1
            self._write_manifest()
            self._emit(
                "progress",
                completed=completed,
                total=len(self.plan.jobs),
                index=job.index,
            )

        statuses = {job.status for job in self.plan.jobs}
        if statuses == {STATUS_SUCCESS}:
            self.plan.status = STATUS_SUCCESS
        elif self._cancel_event.is_set():
            self.plan.status = STATUS_CANCELLED
        else:
            self.plan.status = STATUS_FAILED
        self._write_summary()
        self._write_manifest()
        summary = {
            "status": self.plan.status,
            "run_dir": str(self.plan.run_dir),
            "manifest": str(self.plan.manifest_path),
            "summary": str(self.plan.summary_path),
            "success": sum(job.status == STATUS_SUCCESS for job in self.plan.jobs),
            "failed": sum(
                job.status in {STATUS_FAILED, STATUS_TIMEOUT} for job in self.plan.jobs
            ),
            "cancelled": sum(job.status == STATUS_CANCELLED for job in self.plan.jobs),
            "total": len(self.plan.jobs),
        }
        self._emit("batch_finished", **summary)
        return summary

    def _job_values(self, job: BatchJob) -> dict[str, object]:
        return job_template_values(self.plan, job)

    def _run_job(self, job: BatchJob) -> None:
        started = time.monotonic()
        job.started_at = datetime.now().isoformat(timespec="seconds")
        job.status = STATUS_RUNNING
        job.work_dir.mkdir(parents=True, exist_ok=False)
        values = self._job_values(job)
        stdin_text = render_template(self.plan.preset.stdin_template, values)
        if not stdin_text.endswith("\n"):
            stdin_text += "\n"
        arguments = [render_template(item, values) for item in self.plan.preset.arguments]
        _write_text_atomic(job.work_dir / "stdin.txt", stdin_text)
        _write_text_atomic(
            job.work_dir / "job.json",
            json.dumps(
                {
                    "input": str(job.input_path),
                    "command": [str(self.multiwfn_exe), str(job.input_path), *arguments],
                    "preset": self.plan.preset.to_dict(),
                    "variables": values,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        self._emit(
            "job_status",
            index=job.index,
            status=job.status,
            input=str(job.input_path),
            message=f"正在处理 {job.input_path.name}",
        )

        encoding = locale.getpreferredencoding(False) or "utf-8"
        env = os.environ.copy()
        env["Multiwfnpath"] = str(self.multiwfn_exe.parent)
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process: subprocess.Popen[str] | None = None
        reader_done = object()
        output_queue: queue.Queue[object] = queue.Queue()
        termination_reason = ""
        log_path = job.work_dir / "stdout.log"
        try:
            process = subprocess.Popen(
                [str(self.multiwfn_exe), str(job.input_path), *arguments],
                cwd=str(job.work_dir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding=encoding,
                errors="replace",
                creationflags=creation_flags,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(stdin_text)
            process.stdin.close()

            def read_output() -> None:
                assert process is not None and process.stdout is not None
                try:
                    for line in process.stdout:
                        output_queue.put(line)
                finally:
                    output_queue.put(reader_done)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            stream_finished = False
            with log_path.open("w", encoding="utf-8", errors="replace") as log_handle:
                while process.poll() is None or not stream_finished:
                    try:
                        item = output_queue.get(timeout=0.1)
                    except queue.Empty:
                        item = None
                    if item is reader_done:
                        stream_finished = True
                    elif isinstance(item, str):
                        log_handle.write(item)
                        log_handle.flush()
                        match = VERSION_RE.search(item)
                        if match and not self.plan.detected_multiwfn_version:
                            self.plan.detected_multiwfn_version = match.group(1)
                            expected = self.plan.preset.multiwfn_version
                            if expected and expected != self.plan.detected_multiwfn_version:
                                self._emit(
                                    "warning",
                                    index=job.index,
                                    message=(
                                        f"流程标注适配 {expected}，当前检测到 "
                                        f"{self.plan.detected_multiwfn_version}。"
                                    ),
                                )
                        self._emit("output", index=job.index, text=item.rstrip("\r\n"))

                    elapsed = time.monotonic() - started
                    if self._cancel_event.is_set() and process.poll() is None:
                        termination_reason = "cancelled"
                        self._terminate_process(process)
                    elif (
                        elapsed > self.plan.preset.timeout_seconds
                        and process.poll() is None
                    ):
                        termination_reason = "timeout"
                        self._terminate_process(process)
                reader.join(timeout=1.0)
            job.return_code = process.wait(timeout=5)
        except Exception as exc:
            if process is not None and process.poll() is None:
                self._terminate_process(process)
            job.status = STATUS_FAILED
            job.error = f"启动或执行 Multiwfn 失败：{exc}"
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()

        if termination_reason == "cancelled":
            job.status = STATUS_CANCELLED
            job.error = "任务已由用户取消。"
        elif termination_reason == "timeout":
            job.status = STATUS_TIMEOUT
            job.error = f"运行超过 {self.plan.preset.timeout_seconds} 秒，已终止。"
        elif job.status != STATUS_FAILED:
            try:
                output_errors = self._collect_outputs(job, values)
            except Exception as exc:
                output_errors = [f"处理输出规则失败：{exc}"]
            if job.return_code != 0:
                job.status = STATUS_FAILED
                messages = [
                    "Multiwfn 未正常完成，请查看该文件的运行记录。",
                    *output_errors,
                ]
                job.error = "；".join(message for message in messages if message)
            elif output_errors:
                job.status = STATUS_FAILED
                job.error = "；".join(output_errors)
            else:
                job.status = STATUS_SUCCESS

        job.duration_seconds = time.monotonic() - started
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        _write_text_atomic(
            job.work_dir / "result.json",
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        message = (
            f"{job.input_path.name} 完成，输出 {len(job.outputs)} 个文件"
            if job.status == STATUS_SUCCESS
            else job.error
        )
        self._emit(
            "job_status",
            index=job.index,
            status=job.status,
            input=str(job.input_path),
            message=message,
            duration=job.duration_seconds,
            outputs=list(job.outputs),
        )

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
    def _safe_result_relative(value: str) -> Path:
        normalized = str(value or "").replace("\\", "/").strip().lstrip("/")
        candidate = Path(normalized)
        if not normalized or candidate.is_absolute() or ".." in candidate.parts:
            raise BatchValidationError(f"无效的结果文件名：{value}")
        cleaned_parts = [_clean_file_part(part) for part in candidate.parts]
        return Path(*cleaned_parts)

    @staticmethod
    def _unique_target(path: Path) -> Path:
        if not path.exists():
            return path
        counter = 2
        while True:
            candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    @staticmethod
    def _default_result_name(input_path: Path, source: Path) -> str:
        input_stem = input_path.stem
        if source.stem.casefold().startswith(input_stem.casefold()):
            return source.name
        return f"{input_stem}_{source.name}"

    def _collect_outputs(self, job: BatchJob, values: dict[str, object]) -> list[str]:
        errors: list[str] = []
        collected_sources: set[Path] = set()
        self.plan.results_dir.mkdir(parents=True, exist_ok=True)
        has_explicit_log_rule = any(
            rule.pattern.strip().replace("\\", "/") == "stdout.log"
            for rule in self.plan.preset.output_rules
        )
        if not has_explicit_log_rule:
            source_log = job.work_dir / "stdout.log"
            if source_log.is_file():
                target_log = self._unique_target(
                    self.plan.results_dir / f"{_clean_file_part(job.input_path.stem)}_Multiwfn.log"
                )
                shutil.copy2(source_log, target_log)
                job.outputs.append(str(target_log))
        for rule in self.plan.preset.output_rules:
            pattern = render_template(rule.pattern, values)
            matches = sorted(path for path in job.work_dir.glob(pattern) if path.is_file())
            if not matches:
                if rule.required:
                    errors.append(f"缺少预期输出：{pattern}")
                continue
            for match_index, source in enumerate(matches, 1):
                source_key = source.resolve()
                if source_key in collected_sources:
                    continue
                source_values = {
                    **values,
                    "source_name": source.name,
                    "source_stem": source.stem,
                    "source_ext": source.suffix,
                    "match_index": match_index,
                }
                if rule.rename:
                    requested = render_template(rule.rename, source_values)
                else:
                    requested = self._default_result_name(job.input_path, source)
                relative = self._safe_result_relative(requested)
                match_specific_rename = bool(
                    set(extract_placeholders(rule.rename))
                    & {"source_name", "source_stem", "source_ext", "match_index"}
                )
                if len(matches) > 1 and rule.rename and not match_specific_rename:
                    relative = relative.with_name(
                        f"{relative.stem}_{match_index}{relative.suffix}"
                    )
                target = self._unique_target(self.plan.results_dir / relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                job.outputs.append(str(target))
                collected_sources.add(source_key)
        return errors


def scan_input_files(
    folders: Iterable[Path | str],
    extensions: Iterable[str],
    *,
    recursive: bool = True,
) -> list[Path]:
    allowed = set(normalize_extensions(extensions))
    found: dict[str, Path] = {}
    for raw_folder in folders:
        folder = Path(raw_folder).expanduser().resolve()
        if not folder.is_dir():
            continue
        iterator = folder.rglob("*") if recursive else folder.glob("*")
        for path in iterator:
            if not path.is_file():
                continue
            suffixes = "".join(path.suffixes).lower()
            if path.suffix.lower() in allowed or suffixes in allowed:
                found.setdefault(os.path.normcase(str(path)), path)
    return sorted(found.values(), key=lambda item: str(item).casefold())
