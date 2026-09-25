from __future__ import annotations

import json
import locale
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable, Mapping

import orbital_vmd


INTERACTIVE_VIEWPORT = (1160, 640)
INTERACTIVE_WINDOW = (1180, 700)


class BatchCubeVmdError(RuntimeError):
    pass


class BatchCubeVmdCancelled(BatchCubeVmdError):
    pass


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _clean_file_part(value: str, fallback: str = "cube") -> str:
    text = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return (text or fallback)[:100]


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def normalize_settings(settings: Mapping[str, object] | None) -> dict[str, object]:
    raw = dict(settings or {})
    enabled = bool(raw.get("enabled", False))
    snapshot = raw.get("style_snapshot")
    if enabled:
        if not isinstance(snapshot, Mapping):
            raise BatchCubeVmdError("请先选择用于自动绘图的等值面样式。")
        style = snapshot.get("style")
        if not isinstance(style, Mapping):
            raise BatchCubeVmdError("自动绘图样式缺少实际参数。")
        if str(style.get("surface_mode") or "signed") != "signed":
            raise BatchCubeVmdError("批量 Cube 自动绘图需要正、负等值面样式。")
    width = max(320, min(7680, int(raw.get("width") or 1600)))
    height = max(240, min(4320, int(raw.get("height") or 1200)))
    timeout = max(30, min(86400, int(raw.get("timeout_seconds") or 600)))
    return {
        "enabled": enabled,
        "style_snapshot": json.loads(json.dumps(snapshot, ensure_ascii=False))
        if isinstance(snapshot, Mapping)
        else {},
        "width": width,
        "height": height,
        "timeout_seconds": timeout,
        "template": dict(raw.get("template") or {})
        if isinstance(raw.get("template"), Mapping)
        else {},
    }


class BatchCubeVmdRenderer:
    """Capture one VMD scene and replay it for collected batch Cube files."""

    def __init__(
        self,
        vmd_exe: Path | str,
        run_dir: Path,
        settings: Mapping[str, object],
        *,
        event_callback: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.vmd_exe = Path(vmd_exe).expanduser().resolve()
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.settings = normalize_settings(settings)
        self.event_callback = event_callback
        self.cancel_event = cancel_event or threading.Event()
        self.root = self.run_dir / "vmd"
        self.template_dir = self.root / "template"
        self.render_dir = self.root / "rendered"
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self.state: orbital_vmd.VmdViewState | None = None
        self.state_path: Path | None = None
        self.native_state_path: Path | None = None
        self.reference_cube_path: Path | None = None
        self._load_reusable_template()

    def _emit(self, kind: str, **payload: object) -> None:
        if self.event_callback is not None:
            self.event_callback({"kind": kind, **payload})

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._process_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            self._terminate_process(process)

    def template_payload(self) -> dict[str, str]:
        if self.state_path is None or not self.state_path.is_file():
            return {}
        return {
            "state_path": str(self.state_path),
            "native_state_path": (
                str(self.native_state_path)
                if self.native_state_path is not None and self.native_state_path.is_file()
                else ""
            ),
            "reference_cube_path": (
                str(self.reference_cube_path)
                if self.reference_cube_path is not None and self.reference_cube_path.is_file()
                else ""
            ),
        }

    def _load_reusable_template(self) -> None:
        template = self.settings.get("template")
        if not isinstance(template, Mapping):
            return
        state_path = Path(str(template.get("state_path") or ""))
        if not state_path.is_file():
            return
        try:
            state = orbital_vmd.load_view_state(state_path)
        except orbital_vmd.OrbitalVmdError:
            return
        native = Path(str(template.get("native_state_path") or ""))
        reference = Path(str(template.get("reference_cube_path") or ""))
        self.state = state
        self.state_path = state_path.resolve()
        if native.is_file() and reference.is_file():
            self.native_state_path = native.resolve()
            self.reference_cube_path = reference.resolve()

    def render_cubes(
        self,
        cube_paths: Iterable[Path | str],
        *,
        job_index: int,
        input_name: str,
    ) -> list[Path]:
        cubes = [Path(path).expanduser().resolve() for path in cube_paths]
        cubes = [path for path in cubes if path.is_file()]
        if not cubes:
            raise BatchCubeVmdError("没有找到可交给 VMD 的 Cube 文件。")
        for cube in cubes:
            orbital_vmd.cube_geometry_fingerprint(cube)
        if self.state is None:
            self._capture_first_cube(cubes[0], job_index=job_index, input_name=input_name)
        assert self.state is not None
        return self._render_group(cubes, job_index=job_index, input_name=input_name)

    def _capture_first_cube(self, cube: Path, *, job_index: int, input_name: str) -> None:
        snapshot = self.settings.get("style_snapshot")
        if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("style"), Mapping):
            raise BatchCubeVmdError("自动绘图样式无效。")
        style = dict(snapshot["style"])
        rep0_commands = [str(item) for item in snapshot.get("rep0_commands") or []]
        self.template_dir.mkdir(parents=True, exist_ok=True)
        protocol = self.template_dir / "cube_view.capture"
        cancel_marker = orbital_vmd.capture_cancel_marker_path(protocol)
        error_log = orbital_vmd.capture_error_log_path(protocol)
        native_state = self.template_dir / "cube_final_state.vmd"
        for path in (protocol, cancel_marker, error_log, native_state):
            path.unlink(missing_ok=True)
        script = self.template_dir / "adjust_first_cube.vmd"
        _write_text_atomic(
            script,
            orbital_vmd.build_interactive_capture_tcl(
                cube,
                protocol,
                style,
                rep0_commands=rep0_commands,
                width=INTERACTIVE_VIEWPORT[0],
                height=INTERACTIVE_VIEWPORT[1],
                debug_state_path=native_state,
            ),
        )
        message = (
            "首个 Cube 已在 VMD 中打开；请自由调整样式和角度，"
            "完成后点击“保存全部参数并确认”。"
        )
        self._emit(
            "vmd_interaction_required",
            index=job_index,
            input=input_name,
            cube=str(cube),
            message=message,
        )
        return_code, reason = self._run_process(
            [str(self.vmd_exe), "-e", str(script)],
            cwd=self.template_dir,
            log_path=self.template_dir / "vmd_adjustment.log",
            timeout_seconds=int(self.settings["timeout_seconds"]),
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
            raise BatchCubeVmdCancelled("已取消首个 Cube 的 VMD 调整。")
        if reason == "timeout":
            raise BatchCubeVmdError("等待 VMD 调整确认超时，Cube 文件已保留。")
        if return_code != 0 and reason != "viewpoint_confirmed":
            detail = f"；诊断记录：{error_log}" if error_log.is_file() else ""
            raise BatchCubeVmdError(
                f"VMD 调整阶段未正常结束（退出码 {return_code}）{detail}。"
            )
        if not protocol.is_file():
            raise BatchCubeVmdError("没有取得已确认的 VMD 显示参数。")
        state = orbital_vmd.load_view_state(
            protocol,
            expected_geometry_fingerprint=orbital_vmd.cube_geometry_fingerprint(cube),
        )
        normalized = self.template_dir / "cube_viewpoint.json"
        state.save_json(normalized)
        self.state = state
        self.state_path = normalized.resolve()
        self.reference_cube_path = cube.resolve()
        if native_state.is_file():
            self.native_state_path = native_state.resolve()
        self._emit(
            "vmd_template_saved",
            index=job_index,
            message="VMD 样式与角度已保存，正在批量渲染 Cube。",
        )

    def _render_group(
        self, cubes: list[Path], *, job_index: int, input_name: str
    ) -> list[Path]:
        assert self.state is not None
        folder = self.render_dir / f"job_{job_index:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        render_items: list[dict[str, object]] = []
        targets: list[tuple[Path, Path]] = []
        total = len(cubes)
        for position, cube in enumerate(cubes, 1):
            safe = _clean_file_part(cube.stem, f"cube_{position}")
            scene = _unique_path(folder / f"{position:03d}_{safe}.dat")
            target = _unique_path(cube.with_suffix(".png"))
            token = f"j{job_index}_c{position}"
            render_items.append(
                {
                    "token": token,
                    "cube_path": cube,
                    "output_path": scene,
                    "position": position,
                    "total": total,
                }
            )
            targets.append((Path(str(scene) + ".bmp"), target))

        native = self.native_state_path
        reference = self.reference_cube_path
        use_native = bool(
            native is not None
            and native.is_file()
            and reference is not None
            and reference.is_file()
        )
        script = folder / "render_cubes.vmd"
        _write_text_atomic(
            script,
            orbital_vmd.build_multi_batch_render_tcl(
                render_items,
                self.state,
                width=int(self.settings["width"]),
                height=int(self.settings["height"]),
                renderer="Tachyon",
                native_state_path=native if use_native else None,
                reference_cube_path=reference if use_native else None,
                allow_geometry_mismatch=True,
            ),
        )
        marker = re.compile(
            r"^MolecularStudio: orbital batch (begin|done|failed)\t(\d+)\t(\d+)\t([A-Za-z0-9_.:-]+)$"
        )

        def handle_line(text: str) -> None:
            matched = marker.match(text)
            if matched is None:
                return
            action, current, marker_total, _token = matched.groups()
            if action == "begin":
                message = f"正在用 VMD 渲染 Cube（{current}/{marker_total}）"
            elif action == "done":
                message = f"已完成 VMD 渲染（{current}/{marker_total}）"
            else:
                message = f"VMD 渲染第 {current} 个 Cube 时失败"
            self._emit(
                "vmd_progress",
                index=job_index,
                input=input_name,
                current=int(current),
                total=int(marker_total),
                message=message,
            )

        return_code, reason = self._run_process(
            [
                str(self.vmd_exe),
                "-dispdev",
                "text",
                "-eofexit",
                "-e",
                str(script),
            ],
            cwd=folder,
            log_path=folder / "vmd_render.log",
            timeout_seconds=min(
                86400, int(self.settings["timeout_seconds"]) * max(1, len(cubes))
            ),
            show_window=False,
            line_callback=handle_line,
        )
        if reason == "cancelled":
            raise BatchCubeVmdCancelled("VMD 批量绘图已停止。")
        if reason == "timeout":
            raise BatchCubeVmdError("VMD 批量渲染超时，Cube 文件已保留。")
        if return_code != 0:
            raise BatchCubeVmdError(
                f"VMD 批量渲染失败（退出码 {return_code}），请查看运行记录。"
            )
        images: list[Path] = []
        for native_image, target in targets:
            if not native_image.is_file() or native_image.stat().st_size <= 64:
                raise BatchCubeVmdError(
                    f"VMD 没有生成有效图片：{native_image.name}。"
                )
            self._convert_to_png(native_image, target)
            images.append(target.resolve())
        return images

    @staticmethod
    def _convert_to_png(source: Path, target: Path) -> None:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - packaged dependency
            raise BatchCubeVmdError("缺少图片转换组件，无法保存 PNG。") from exc
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp.png")
        try:
            with Image.open(source) as image:
                image.save(temporary, "PNG")
            if not temporary.is_file() or temporary.stat().st_size <= 64:
                raise BatchCubeVmdError("VMD 图片转换为 PNG 后校验失败。")
            os.replace(temporary, target)
        except OSError as exc:
            raise BatchCubeVmdError(f"无法保存 VMD 图片：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _run_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        log_path: Path,
        timeout_seconds: int,
        show_window: bool,
        completion_markers: Mapping[str, Path] | None = None,
        line_callback: Callable[[str], None] | None = None,
    ) -> tuple[int, str]:
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and not show_window else 0
        existing_windows = orbital_vmd.vmd_display_window_handles() if show_window else set()
        encoding = locale.getpreferredencoding(False) or "utf-8"
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=encoding,
            errors="replace",
            creationflags=creation_flags,
        )
        with self._process_lock:
            self._active_process = process
        sentinel = object()
        output_queue: queue.Queue[object] = queue.Queue()

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
        ready_at: float | None = None
        next_window_check = started
        window_restored = not show_window
        stream_finished = False
        reason = ""
        markers = tuple(
            (str(name), Path(path)) for name, path in (completion_markers or {}).items()
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                while process.poll() is None or not stream_finished:
                    now = time.monotonic()
                    if (
                        show_window
                        and ready_at is not None
                        and not window_restored
                        and now >= next_window_check
                        and now - ready_at <= 20.0
                    ):
                        window_restored = orbital_vmd.restore_vmd_display_window(
                            process.pid,
                            excluded_handles=existing_windows,
                            width=INTERACTIVE_WINDOW[0],
                            height=INTERACTIVE_WINDOW[1],
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
                        text = item.rstrip("\r\n")
                        if text:
                            if show_window and "MolecularStudio: adjust the scene" in text:
                                ready_at = now
                                next_window_check = now
                            if line_callback is not None:
                                line_callback(text)
                    if self.cancel_event.is_set() and process.poll() is None:
                        reason = "cancelled"
                        self._terminate_process(process)
                    elif not reason:
                        completed = next(
                            (name for name, path in markers if path.is_file()), ""
                        )
                        if completed:
                            reason = completed
                            if process.poll() is None:
                                self._terminate_process(process)
                        elif now - started > timeout_seconds and process.poll() is None:
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


__all__ = [
    "BatchCubeVmdCancelled",
    "BatchCubeVmdError",
    "BatchCubeVmdRenderer",
    "normalize_settings",
]
