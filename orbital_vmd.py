"""Safe VMD view capture and deterministic orbital-cube rendering.

This module deliberately separates interactive scene editing from batch
rendering.  VMD writes both a validated, application-owned data snapshot and
its official ``save_state`` scene.  Batch rendering can replay that paired
native scene as the authority (so extra molecules, graphics, labels and user
changes survive) while the data snapshot validates geometry and restores
global fields omitted by VMD 1.9.3.  The data-only replay remains a fallback.

The implementation targets the Tcl 8.5 interface bundled with VMD 1.9.3.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import ctypes
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Any, Iterable, Mapping, Sequence


STATE_MAGIC = "ORBITAL_VMD_STATE\t1"
STATE_SCHEMA_VERSION = 1
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_STATE_LINES = 100_000
MAX_FIELD_BYTES = 256 * 1024
MAX_NATIVE_STATE_BYTES = 64 * 1024 * 1024


def capture_cancel_marker_path(state_path: Path | str) -> Path:
    """Return the atomic cancellation marker paired with a capture file."""
    return Path(f"{Path(state_path)}.cancelled")


def capture_error_log_path(state_path: Path | str) -> Path:
    """Return the VMD-side diagnostic log paired with a capture file."""
    return Path(f"{Path(state_path)}.error.log")


class OrbitalVmdError(RuntimeError):
    """Base error raised by orbital VMD helpers."""


class OrbitalVmdValidationError(OrbitalVmdError, ValueError):
    """Raised when an input, captured state, or render output is invalid."""


def vmd_display_window_handles(process_id: int | None = None) -> set[int]:
    """Return visible VMD OpenGL top-level windows on Windows.

    ``vmd.exe`` may create the OpenGL window in a child process, so callers can
    take a pre-launch snapshot and later identify newly created windows even
    when their owner PID differs from the launcher PID.
    """

    if os.name != "nt":
        return set()
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
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
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        matches: set[int] = set()

        @callback_type
        def collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if process_id is not None and int(owner.value) != int(process_id):
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
                raw = ctypes.cast(hwnd, ctypes.c_void_p).value
                if raw is not None:
                    matches.add(int(raw))
            return True

        user32.EnumWindows(collect, 0)
        return matches
    except (AttributeError, OSError, TypeError, ValueError):
        return set()


def restore_vmd_display_window(
    process_id: int,
    *,
    excluded_handles: Iterable[int] = (),
    width: int = 1180,
    height: int = 700,
    topmost: bool = True,
) -> bool:
    """Restore, size and foreground the newly launched VMD display window."""

    if os.name != "nt":
        return False
    excluded = {int(value) for value in excluded_handles}
    owned = vmd_display_window_handles(process_id)
    candidates = owned or (vmd_display_window_handles() - excluded)
    if not candidates:
        return False
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
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
        insert_after = wintypes.HWND(-1 if topmost else 0)
        restored = False
        for raw in candidates:
            hwnd = wintypes.HWND(raw)
            user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
            positioned = user32.SetWindowPos(
                hwnd,
                insert_after,
                24,
                32,
                max(640, int(width)),
                max(480, int(height)),
                0x0040,  # SWP_SHOWWINDOW
            )
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            restored = restored or bool(
                positioned and user32.IsWindowVisible(hwnd)
            )
        return restored
    except (AttributeError, OSError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class VmdLight:
    index: int
    enabled: bool
    highlighted: bool
    position: tuple[float, float, float]


@dataclass(frozen=True)
class VmdColor:
    index: int
    rgb: tuple[float, float, float]
    name: str = ""


@dataclass(frozen=True)
class VmdColorCategory:
    category: str
    item: str
    color: str


@dataclass(frozen=True)
class VmdColorScale:
    method: str
    colors: tuple[float, ...]


@dataclass(frozen=True)
class VmdMaterial:
    name: str
    ambient: float
    specular: float
    diffuse: float
    shininess: float
    mirror: float
    opacity: float
    outline: float
    outline_width: float
    transmode: float

    @property
    def values(self) -> tuple[float, ...]:
        return (
            self.ambient,
            self.specular,
            self.diffuse,
            self.shininess,
            self.mirror,
            self.opacity,
            self.outline,
            self.outline_width,
            self.transmode,
        )


@dataclass(frozen=True)
class VmdClipPlane:
    index: int
    center: tuple[float, float, float]
    color: tuple[float, float, float]
    normal: tuple[float, float, float]
    enabled: bool


@dataclass(frozen=True)
class VmdRepresentation:
    index: int
    style: str
    selection: str
    color: str
    material: str
    show_periodic: str = ""
    num_periodic: int = 1
    shown: bool = True
    selection_update: bool = False
    color_update: bool = False
    scale_minmax: tuple[float, float] = (0.0, 0.0)
    smoothing: int = 0
    draw_frames: str = "now"
    clip_planes: tuple[VmdClipPlane, ...] = ()


_DISPLAY_FLOAT_FIELDS = frozenset(
    {
        "eyesep",
        "focallength",
        "height",
        "distance",
        "nearclip",
        "farclip",
        "cuestart",
        "cueend",
        "cuedensity",
        "aoambient",
        "aodirect",
        "dof_fnumber",
        "dof_focaldist",
    }
)
_DISPLAY_BOOL_FIELDS = frozenset(
    {
        "antialias",
        "depthcue",
        "culling",
        "shadows",
        "ambientocclusion",
        "dof",
        "backgroundgradient",
    }
)
_DISPLAY_TOKEN_FIELDS = frozenset({"rendermode", "stereo", "projection", "cuemode"})
_DISPLAY_FIELDS = _DISPLAY_FLOAT_FIELDS | _DISPLAY_BOOL_FIELDS | _DISPLAY_TOKEN_FIELDS | {"size"}
_MATRIX_NAMES = ("center_matrix", "rotate_matrix", "scale_matrix", "global_matrix")
_MATERIAL_PARAMETER_NAMES = (
    "ambient",
    "specular",
    "diffuse",
    "shininess",
    "mirror",
    "opacity",
    "outline",
    "outlinewidth",
    "transmode",
)


@dataclass(frozen=True)
class VmdViewState:
    schema_version: int
    vmd_version: str
    geometry_fingerprint: str
    reference_cube_sha256: str
    display: dict[str, object]
    axes_location: str
    stage_location: str
    lights: tuple[VmdLight, ...]
    colors: tuple[VmdColor, ...]
    color_categories: tuple[VmdColorCategory, ...]
    color_scale_method: str
    color_scale_midpoint: float
    color_scale_min: float
    color_scale_max: float
    color_scales: tuple[VmdColorScale, ...]
    materials: tuple[VmdMaterial, ...]
    representations: tuple[VmdRepresentation, ...]
    matrices: dict[str, tuple[float, ...]]
    renderer_aa_samples: int = 12
    renderer_ao_samples: int = 12

    @property
    def viewport(self) -> tuple[int, int]:
        value = self.display.get("size")
        if not isinstance(value, tuple) or len(value) != 2:
            raise OrbitalVmdValidationError("Captured display size is invalid.")
        return int(value[0]), int(value[1])

    @property
    def aspect_ratio(self) -> float:
        width, height = self.viewport
        return width / height

    def validate(self) -> "VmdViewState":
        if self.schema_version != STATE_SCHEMA_VERSION:
            raise OrbitalVmdValidationError(
                f"Unsupported VMD state schema: {self.schema_version}."
            )
        _validate_text(self.vmd_version, "VMD version", 256)
        _validate_hex_digest(self.geometry_fingerprint, "geometry fingerprint")
        _validate_hex_digest(self.reference_cube_sha256, "reference Cube digest")

        missing = _DISPLAY_FIELDS - set(self.display)
        extra = set(self.display) - _DISPLAY_FIELDS
        if missing or extra:
            raise OrbitalVmdValidationError(
                f"Display state fields do not match the schema (missing={sorted(missing)}, "
                f"extra={sorted(extra)})."
            )
        for key in _DISPLAY_FLOAT_FIELDS:
            _finite_float(self.display[key], f"display.{key}", -1.0e9, 1.0e9)
        for key in _DISPLAY_BOOL_FIELDS:
            if not isinstance(self.display[key], bool):
                raise OrbitalVmdValidationError(f"display.{key} must be boolean.")
        for key in _DISPLAY_TOKEN_FIELDS:
            _validate_text(str(self.display[key]), f"display.{key}", 128)
        width, height = self.viewport
        _validate_dimensions(width, height)
        _validate_text(self.axes_location, "axes location", 64)
        _validate_text(self.stage_location, "stage location", 64)

        _validate_unique_indices(self.lights, "light", maximum=64)
        for light in self.lights:
            _validate_vector(light.position, 3, f"light {light.index} position")

        _validate_unique_indices(self.colors, "color", maximum=8192)
        for color in self.colors:
            _validate_vector(color.rgb, 3, f"color {color.index} RGB", 0.0, 1.0)
            _validate_text(color.name, f"color {color.index} name", 128)

        if len(self.color_categories) > 20_000:
            raise OrbitalVmdValidationError("Captured color-category table is unreasonably large.")
        seen_categories: set[tuple[str, str]] = set()
        for entry in self.color_categories:
            _validate_text(entry.category, "color category", 512)
            _validate_text(entry.item, "color category item", 2048)
            _validate_text(entry.color, "color category value", 128)
            key = (entry.category, entry.item)
            if key in seen_categories:
                raise OrbitalVmdValidationError(f"Duplicate color-category entry: {key!r}.")
            seen_categories.add(key)

        _validate_text(self.color_scale_method, "color scale method", 128)
        for name, value in (
            ("color scale midpoint", self.color_scale_midpoint),
            ("color scale min", self.color_scale_min),
            ("color scale max", self.color_scale_max),
        ):
            _finite_float(value, name, 0.0, 1.0)
        if self.color_scale_min > self.color_scale_max:
            raise OrbitalVmdValidationError("Color-scale minimum is greater than its maximum.")
        scale_names: set[str] = set()
        for scale in self.color_scales:
            _validate_text(scale.method, "color scale name", 128)
            if scale.method in scale_names:
                raise OrbitalVmdValidationError(f"Duplicate color scale: {scale.method}.")
            scale_names.add(scale.method)
            _validate_vector(scale.colors, 9, f"color scale {scale.method}", 0.0, 1.0)
        if self.color_scale_method not in scale_names:
            raise OrbitalVmdValidationError("The active color scale has no captured definition.")

        material_names: set[str] = set()
        if not self.materials:
            raise OrbitalVmdValidationError("No VMD materials were captured.")
        for material in self.materials:
            _validate_token(material.name, "material name", 128)
            if material.name in material_names:
                raise OrbitalVmdValidationError(f"Duplicate material: {material.name}.")
            material_names.add(material.name)
            for index, value in enumerate(material.values):
                _finite_float(value, f"material {material.name} parameter {index}", -100.0, 100.0)

        _validate_unique_indices(self.representations, "representation", maximum=4096)
        if not self.representations:
            raise OrbitalVmdValidationError("No molecular representations were captured.")
        expected_rep_indices = list(range(len(self.representations)))
        if sorted(rep.index for rep in self.representations) != expected_rep_indices:
            raise OrbitalVmdValidationError("Representation indices must be contiguous from zero.")
        for rep in self.representations:
            _validate_text(rep.style, f"representation {rep.index} style", 32_768)
            _validate_text(rep.selection, f"representation {rep.index} selection", 64_000)
            _validate_text(rep.color, f"representation {rep.index} color", 8192)
            _validate_token(rep.material, f"representation {rep.index} material", 128)
            if rep.material not in material_names:
                raise OrbitalVmdValidationError(
                    f"Representation {rep.index} uses an uncaptured material: {rep.material}."
                )
            _validate_text(rep.show_periodic, f"representation {rep.index} periodic flags", 128)
            if not 0 <= rep.num_periodic <= 100:
                raise OrbitalVmdValidationError("Periodic-image count is outside the safe range.")
            _validate_vector(rep.scale_minmax, 2, f"representation {rep.index} scale range")
            if not 0 <= rep.smoothing <= 1_000_000:
                raise OrbitalVmdValidationError("Representation smoothing is outside the safe range.")
            _validate_text(rep.draw_frames, f"representation {rep.index} frame selection", 8192)
            _validate_unique_indices(rep.clip_planes, "clip plane", maximum=64)
            for plane in rep.clip_planes:
                _validate_vector(plane.center, 3, "clip-plane center")
                _validate_vector(plane.color, 3, "clip-plane color", 0.0, 1.0)
                _validate_vector(plane.normal, 3, "clip-plane normal")

        if set(self.matrices) != set(_MATRIX_NAMES):
            raise OrbitalVmdValidationError("The four VMD view matrices were not captured completely.")
        for name in _MATRIX_NAMES:
            values = self.matrices[name]
            _validate_vector(values, 16, name, -1.0e12, 1.0e12)

        if not 0 <= self.renderer_aa_samples <= 4096:
            raise OrbitalVmdValidationError("Tachyon antialiasing sample count is invalid.")
        if not 0 <= self.renderer_ao_samples <= 4096:
            raise OrbitalVmdValidationError("Tachyon AO sample count is invalid.")
        return self

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "VmdViewState":
        try:
            lights = tuple(
                VmdLight(**{**item, "position": tuple(item["position"])})
                for item in _mapping_list(raw, "lights")
            )
            colors = tuple(
                VmdColor(**{**item, "rgb": tuple(item["rgb"])})
                for item in _mapping_list(raw, "colors")
            )
            categories = tuple(
                VmdColorCategory(**item) for item in _mapping_list(raw, "color_categories")
            )
            scales = tuple(
                VmdColorScale(**{**item, "colors": tuple(item["colors"])})
                for item in _mapping_list(raw, "color_scales")
            )
            materials = tuple(
                VmdMaterial(**item) for item in _mapping_list(raw, "materials")
            )
            reps: list[VmdRepresentation] = []
            for item in _mapping_list(raw, "representations"):
                rep_raw = dict(item)
                rep_raw["clip_planes"] = tuple(
                    VmdClipPlane(
                        **{
                            **plane,
                            "center": tuple(plane["center"]),
                            "color": tuple(plane["color"]),
                            "normal": tuple(plane["normal"]),
                        }
                    )
                    for plane in rep_raw.get("clip_planes", [])
                )
                for vector_key in ("scale_minmax",):
                    rep_raw[vector_key] = tuple(rep_raw[vector_key])
                reps.append(VmdRepresentation(**rep_raw))
            display = dict(_require_mapping(raw.get("display"), "display"))
            if isinstance(display.get("size"), list):
                display["size"] = tuple(display["size"])
            matrices_raw = _require_mapping(raw.get("matrices"), "matrices")
            matrices = {str(key): tuple(value) for key, value in matrices_raw.items()}
            state = cls(
                schema_version=int(raw["schema_version"]),
                vmd_version=str(raw["vmd_version"]),
                geometry_fingerprint=str(raw["geometry_fingerprint"]),
                reference_cube_sha256=str(raw["reference_cube_sha256"]),
                display=display,
                axes_location=str(raw["axes_location"]),
                stage_location=str(raw["stage_location"]),
                lights=lights,
                colors=colors,
                color_categories=categories,
                color_scale_method=str(raw["color_scale_method"]),
                color_scale_midpoint=float(raw["color_scale_midpoint"]),
                color_scale_min=float(raw["color_scale_min"]),
                color_scale_max=float(raw["color_scale_max"]),
                color_scales=scales,
                materials=materials,
                representations=tuple(reps),
                matrices=matrices,
                renderer_aa_samples=int(raw.get("renderer_aa_samples", 12)),
                renderer_ao_samples=int(raw.get("renderer_ao_samples", 12)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OrbitalVmdValidationError(f"Invalid normalized VMD state: {exc}") from exc
        return state.validate()

    def save_json(self, path: Path | str) -> Path:
        target = Path(path).expanduser().resolve()
        _write_text_atomic(
            target,
            json.dumps(self.validate().to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        return target


def _mapping_list(raw: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise OrbitalVmdValidationError(f"{key} must be a list.")
    return [_require_mapping(item, key) for item in value]


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OrbitalVmdValidationError(f"{name} must be an object.")
    return value


def _validate_text(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise OrbitalVmdValidationError(f"{name} must be text.")
    if len(value) > maximum or "\x00" in value:
        raise OrbitalVmdValidationError(f"{name} is invalid or too long.")
    if any(ord(char) < 32 and char not in "\t" for char in value):
        raise OrbitalVmdValidationError(f"{name} contains control characters.")


_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")


def _validate_token(value: str, name: str, maximum: int) -> None:
    _validate_text(value, name, maximum)
    if not value or not _SAFE_TOKEN_RE.fullmatch(value):
        raise OrbitalVmdValidationError(f"{name} is not a safe VMD token.")


def _validate_hex_digest(value: str, name: str) -> None:
    if len(value) != 64 or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise OrbitalVmdValidationError(f"Invalid {name}.")


def _finite_float(
    value: object, name: str, minimum: float = -math.inf, maximum: float = math.inf
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise OrbitalVmdValidationError(f"{name} is not numeric.") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise OrbitalVmdValidationError(f"{name} is outside the safe numeric range.")
    return parsed


def _validate_vector(
    values: Sequence[object],
    length: int,
    name: str,
    minimum: float = -1.0e12,
    maximum: float = 1.0e12,
) -> None:
    if not isinstance(values, (tuple, list)) or len(values) != length:
        raise OrbitalVmdValidationError(f"{name} must contain {length} values.")
    for value in values:
        _finite_float(value, name, minimum, maximum)


def _validate_unique_indices(values: Iterable[object], name: str, maximum: int) -> None:
    seen: set[int] = set()
    for value in values:
        index = getattr(value, "index", None)
        if not isinstance(index, int) or not 0 <= index < maximum:
            raise OrbitalVmdValidationError(f"Invalid {name} index: {index!r}.")
        if index in seen:
            raise OrbitalVmdValidationError(f"Duplicate {name} index: {index}.")
        seen.add(index)


def _validate_dimensions(width: int, height: int) -> None:
    if not isinstance(width, int) or not isinstance(height, int):
        raise OrbitalVmdValidationError("Image dimensions must be integers.")
    if not 240 <= width <= 16_384 or not 240 <= height <= 16_384:
        raise OrbitalVmdValidationError("Image dimensions are outside the supported range.")
    if width * height > 100_000_000:
        raise OrbitalVmdValidationError("The requested image is too large.")


def _validate_single_dimension(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise OrbitalVmdValidationError(f"{name} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OrbitalVmdValidationError(f"{name} must be an integer.") from exc
    if parsed != value or not 240 <= parsed <= 16_384:
        raise OrbitalVmdValidationError(f"{name} is outside the supported range.")
    return parsed


def resolve_render_dimensions(
    captured_size: tuple[int, int],
    *,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int]:
    """Resolve an aspect-preserving Tachyon viewport.

    The VMD window may be freely resized while the user composes the scene.
    Supplying just one output dimension derives the other from that captured
    viewport.  Supplying both treats them as a bounding box and fits the
    captured aspect ratio inside it.  Thus a harmless resize never makes an
    otherwise valid batch run fail merely because two ratios differ by a few
    pixels (or because the user intentionally chose a different ratio).
    """

    captured_width, captured_height = captured_size
    _validate_dimensions(captured_width, captured_height)
    if width is None and height is None:
        return captured_width, captured_height

    requested_width = (
        _validate_single_dimension(width, "Image width") if width is not None else None
    )
    requested_height = (
        _validate_single_dimension(height, "Image height") if height is not None else None
    )
    ratio = captured_width / captured_height

    if requested_width is None:
        assert requested_height is not None
        render_height = requested_height
        render_width = max(1, round(render_height * ratio))
    elif requested_height is None:
        render_width = requested_width
        render_height = max(1, round(render_width / ratio))
    else:
        requested_ratio = requested_width / requested_height
        # Preserve an exactly compatible request, including the unavoidable
        # one-pixel rounding error of integer viewport sizes.
        if abs(requested_width - requested_height * ratio) <= 1.0:
            render_width, render_height = requested_width, requested_height
        else:
            scale = min(
                requested_width / captured_width,
                requested_height / captured_height,
            )
            render_width = min(requested_width, max(1, round(captured_width * scale)))
            render_height = min(requested_height, max(1, round(captured_height * scale)))

    _validate_dimensions(render_width, render_height)
    return render_width, render_height


def _parse_bool(value: str, name: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "on", "true", "yes"}:
        return True
    if lowered in {"0", "off", "false", "no"}:
        return False
    raise OrbitalVmdValidationError(f"{name} is not boolean: {value!r}.")


def _parse_highlight(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"highlight", "highlighted", "on", "1"}:
        return True
    if lowered in {"unhighlight", "unhighlighted", "off", "0"}:
        return False
    raise OrbitalVmdValidationError(f"Invalid VMD light highlight state: {value!r}.")


_FLOAT_TOKEN_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_FLOAT_LIST_RE = re.compile(r"^[\s{}+\-0-9.eE]+$")


def _parse_float_vector(
    value: str,
    length: int,
    name: str,
    minimum: float = -1.0e12,
    maximum: float = 1.0e12,
) -> tuple[float, ...]:
    if not value or not _FLOAT_LIST_RE.fullmatch(value):
        raise OrbitalVmdValidationError(f"{name} is not a numeric Tcl list.")
    tokens = _FLOAT_TOKEN_RE.findall(value)
    if len(tokens) != length:
        raise OrbitalVmdValidationError(
            f"{name} contains {len(tokens)} values; expected {length}."
        )
    parsed = tuple(_finite_float(token, name, minimum, maximum) for token in tokens)
    return parsed


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cube_geometry_fingerprint(path: Path | str) -> str:
    """Hash Cube geometry and grid metadata, excluding volumetric values."""
    cube = Path(path).expanduser().resolve()
    if not cube.is_file():
        raise OrbitalVmdValidationError(f"Cube file does not exist: {cube}")
    try:
        with cube.open("r", encoding="ascii", errors="replace") as handle:
            if handle.readline() == "" or handle.readline() == "":
                raise OrbitalVmdValidationError("Cube header is incomplete.")
            atom_origin = handle.readline().split()
            if len(atom_origin) < 4:
                raise OrbitalVmdValidationError("Cube atom/origin line is invalid.")
            atom_count_raw = int(atom_origin[0])
            atom_count = abs(atom_count_raw)
            if atom_count <= 0 or atom_count > 2_000_000:
                raise OrbitalVmdValidationError("Cube atom count is invalid.")
            origin = tuple(_finite_float(token, "Cube origin") for token in atom_origin[1:4])
            grid: list[tuple[int, tuple[float, float, float]]] = []
            for axis_index in range(3):
                fields = handle.readline().split()
                if len(fields) < 4:
                    raise OrbitalVmdValidationError(f"Cube grid axis {axis_index} is invalid.")
                count = abs(int(fields[0]))
                if count <= 0 or count > 1_000_000:
                    raise OrbitalVmdValidationError("Cube grid dimension is invalid.")
                vector = tuple(_finite_float(token, "Cube grid vector") for token in fields[1:4])
                grid.append((count, vector))
            atoms: list[tuple[int, float, float, float]] = []
            for atom_index in range(atom_count):
                fields = handle.readline().split()
                if len(fields) < 5:
                    raise OrbitalVmdValidationError(
                        f"Cube atom line {atom_index + 1} is invalid."
                    )
                atomic_number = int(float(fields[0]))
                if not 0 <= atomic_number <= 200:
                    raise OrbitalVmdValidationError("Cube atomic number is invalid.")
                xyz = tuple(_finite_float(token, "Cube atom coordinate") for token in fields[2:5])
                atoms.append((atomic_number, xyz[0], xyz[1], xyz[2]))
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, OrbitalVmdValidationError):
            raise
        raise OrbitalVmdValidationError(f"Unable to read Cube geometry: {exc}") from exc

    def canonical_number(value: float) -> str:
        return format(value, ".12g")

    payload = {
        "atoms": [
            [atomic_number, canonical_number(x), canonical_number(y), canonical_number(z)]
            for atomic_number, x, y, z in atoms
        ],
        "grid": [
            [count, *(canonical_number(value) for value in vector)]
            for count, vector in grid
        ],
        "origin": [canonical_number(value) for value in origin],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return sha256(encoded).hexdigest()


def _hex_text(value: object) -> str:
    return str(value).encode("utf-8").hex()


def _tcl_hex_expression(value: object) -> str:
    return f"[_mo_unhex {_hex_text(value)}]"


_SAFE_STYLE_PREFIXES = (
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
_UNSAFE_TCL_CHARS_RE = re.compile(r"[\r\n;\[\]$\\]")


def _safe_style_commands(commands: object) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    source = commands if isinstance(commands, (list, tuple)) else []
    for raw in source:
        for line in str(raw or "").splitlines():
            command = line.strip()
            if not command.startswith(_SAFE_STYLE_PREFIXES):
                continue
            if _UNSAFE_TCL_CHARS_RE.search(command):
                continue
            if command not in seen:
                seen.add(command)
                result.append(command)
    return result


def _safe_rep0_commands(commands: object) -> list[str]:
    allowed = ("mol modstyle 0 top ", "mol modcolor 0 top ", "mol modmaterial 0 top ")
    result: list[str] = []
    for raw in commands if isinstance(commands, (list, tuple)) else []:
        command = str(raw or "").strip()
        if command.startswith(allowed) and not _UNSAFE_TCL_CHARS_RE.search(command):
            result.append(command)
    return result


def _safe_color_expression(value: object, fallback: str) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 256 or _UNSAFE_TCL_CHARS_RE.search(candidate):
        return fallback
    if not re.fullmatch(r"[A-Za-z0-9_.:+-]+(?:\s+[A-Za-z0-9_.:+-]+)*", candidate):
        return fallback
    return candidate


def _safe_style_token(value: object, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_TOKEN_RE.fullmatch(candidate) else fallback


def _initial_signed_scene_tcl(
    cube_path: Path,
    style: Mapping[str, object],
    rep0_commands: list[str] | None,
) -> list[str]:
    mode = str(style.get("surface_mode") or "signed")
    if mode != "signed":
        raise OrbitalVmdValidationError(
            "Orbital view capture requires a signed two-lobe drawing style."
        )
    iso_value = _finite_float(style.get("default_iso_value", 0.05), "orbital isovalue")
    if iso_value <= 0 or iso_value > 1.0e6:
        raise OrbitalVmdValidationError("Orbital isovalue must be positive.")
    material = _safe_style_token(style.get("material"), "Glossy")
    positive = _safe_color_expression(
        style.get("pos_color_expr"), f"ColorID {int(style.get('pos_color', 1))}"
    )
    negative = _safe_color_expression(
        style.get("neg_color_expr"), f"ColorID {int(style.get('neg_color', 0))}"
    )
    surface_draw = max(0, min(3, int(style.get("surface_draw", 0))))
    surface_boundary = max(0, min(3, int(style.get("surface_boundary", 0))))
    surface_step = max(1, min(20, int(style.get("surface_step", 1))))
    surface_size = max(1, min(20, int(style.get("surface_size", 1))))
    skeleton = _safe_rep0_commands(rep0_commands or [])
    if not skeleton:
        skeleton = [
            "mol modstyle 0 top CPK 0.800000 0.300000 22.000000 22.000000",
            "mol modcolor 0 top Name",
            "mol modmaterial 0 top Opaque",
        ]
    lines = [
        f"set ::MO_REFERENCE_CUBE {_tcl_hex_expression(cube_path)}",
        f"set ::MO_ISOVALUE {iso_value:.12g}",
        *_safe_style_commands(style.get("commands", [])),
        "mol new $::MO_REFERENCE_CUBE type cube waitfor all",
        "set ::MO_REFERENCE_MOL [molinfo top]",
        *skeleton,
        "mol addrep $::MO_REFERENCE_MOL",
        (
            "mol modstyle 1 $::MO_REFERENCE_MOL Isosurface $::MO_ISOVALUE 0 "
            f"{surface_boundary} {surface_draw} {surface_step} {surface_size}"
        ),
        f"mol modcolor 1 $::MO_REFERENCE_MOL {positive}",
        f"mol modmaterial 1 $::MO_REFERENCE_MOL {material}",
        "mol addrep $::MO_REFERENCE_MOL",
        "set ::MO_NEGATIVE_ISOVALUE [expr {-$::MO_ISOVALUE}]",
        (
            "mol modstyle 2 $::MO_REFERENCE_MOL Isosurface $::MO_NEGATIVE_ISOVALUE 0 "
            f"{surface_boundary} {surface_draw} {surface_step} {surface_size}"
        ),
        f"mol modcolor 2 $::MO_REFERENCE_MOL {negative}",
        f"mol modmaterial 2 $::MO_REFERENCE_MOL {material}",
    ]
    return lines


def _capture_helpers_tcl() -> list[str]:
    display_fields = " ".join(sorted(_DISPLAY_FIELDS))
    return [
        "proc _mo_unhex {value} {",
        "    return [encoding convertfrom utf-8 [binary format H* $value]]",
        "}",
        "proc _mo_hex {value} {",
        "    binary scan [encoding convertto utf-8 $value] H* result",
        "    return $result",
        "}",
        "proc _mo_record {handle tag args} {",
        "    set fields [list $tag]",
        "    foreach value $args { lappend fields [_mo_hex $value] }",
        "    puts $handle [join $fields \"\\t\"]",
        "    incr ::MO_RECORD_COUNT",
        "}",
        "proc _mo_write_marker {path value} {",
        "    set tmp \"$path.tmp.[pid]\"",
        "    file mkdir [file dirname $path]",
        "    catch {file delete -force $tmp}",
        "    set handle [open $tmp w]",
        "    fconfigure $handle -encoding utf-8 -translation lf",
        "    puts $handle $value",
        "    close $handle",
        "    file rename -force $tmp $path",
        "}",
        "proc _mo_set_controls {state} {",
        "    foreach widget {.moCapture.confirm .moCapture.reset .moCapture.cancel} {",
        "        if {[winfo exists $widget]} { catch {$widget configure -state $state} }",
        "    }",
        "}",
        "proc _mo_report_error {stage message options} {",
        "    catch {",
        "        file mkdir [file dirname $::MO_ERROR_PATH]",
        "        set errorHandle [open $::MO_ERROR_PATH a]",
        "        fconfigure $errorHandle -encoding utf-8 -translation lf",
        "        puts $errorHandle \"---- [clock format [clock seconds] -format {%Y-%m-%d %H:%M:%S}] ----\"",
        "        puts $errorHandle \"stage: $stage\"",
        "        puts $errorHandle \"message: $message\"",
        "        if {[dict exists $options -errorinfo]} {",
        "            puts $errorHandle [dict get $options -errorinfo]",
        "        }",
        "        puts $errorHandle \"\"",
        "        close $errorHandle",
        "    }",
        "    puts stderr \"MolecularStudio capture error ($stage): $message\"",
        "    _mo_set_controls normal",
        "    tk_messageBox -icon error -title \"VMD\" -message \"$message\\n\\nA diagnostic log was saved to:\\n$::MO_ERROR_PATH\"",
        "}",
        "proc _mo_write_state {} {",
        "    if {[lsearch -exact [molinfo list] $::MO_REFERENCE_MOL] < 0} {",
        "        error \"The reference molecule was deleted; reset or reopen this step.\"",
        "    }",
        "    rotate stop",
        "    display update ui",
        "    set tmp \"$::MO_STATE_PATH.tmp.[pid]\"",
        "    file mkdir [file dirname $::MO_STATE_PATH]",
        "    catch {file delete -force $tmp}",
        "    set handle [open $tmp w]",
        "    fconfigure $handle -encoding utf-8 -translation lf",
        "    set ::MO_RECORD_COUNT 0",
        f"    puts $handle {{{STATE_MAGIC}}}",
        "    set code [catch {",
        "        _mo_record $handle META confirmed 1",
        "        _mo_record $handle META schema 1",
        "        _mo_record $handle META vmd_version [vmdinfo version]",
        "        _mo_record $handle META geometry_fingerprint $::MO_GEOMETRY_FINGERPRINT",
        "        _mo_record $handle META reference_cube_sha256 $::MO_REFERENCE_SHA256",
        f"        foreach key {{{display_fields}}} {{",
        "            _mo_record $handle DISPLAY $key [display get $key]",
        "        }",
        "        _mo_record $handle SCENE axes [axes location]",
        "        _mo_record $handle SCENE stage [stage location]",
        "        for {set i 0} {$i < [light num]} {incr i} {",
        "            set status [light $i status]",
        "            _mo_record $handle LIGHT $i [lindex $status 0] [lindex $status 1] [light $i pos]",
        "        }",
        "        set colorNames [colorinfo colors]",
        "        for {set i 0} {$i < [colorinfo max]} {incr i} {",
        "            set colorName \"\"",
        "            if {$i < [llength $colorNames]} { set colorName [lindex $colorNames $i] }",
        "            _mo_record $handle COLOR $i $colorName [colorinfo rgb $i]",
        "        }",
        "        foreach category [colorinfo categories] {",
        "            foreach item [colorinfo category $category] {",
        "                _mo_record $handle COLOR_CATEGORY $category $item [colorinfo category $category $item]",
        "            }",
        "        }",
        "        _mo_record $handle COLOR_SCALE_ACTIVE [colorinfo scale method] [colorinfo scale midpoint] [colorinfo scale min] [colorinfo scale max]",
        "        foreach method [colorinfo scale methods] {",
        "            _mo_record $handle COLOR_SCALE $method [color scale colors $method]",
        "        }",
        "        foreach materialName [material list] {",
        "            _mo_record $handle MATERIAL $materialName [material settings $materialName]",
        "        }",
        "        set mol $::MO_REFERENCE_MOL",
        "        set repCount [molinfo $mol get numreps]",
        "        for {set repIndex 0} {$repIndex < $repCount} {incr repIndex} {",
        "            set repData [molinfo $mol get \"{rep $repIndex} {selection $repIndex} {color $repIndex} {material $repIndex}\"]",
        "            _mo_record $handle REP $repIndex [lindex $repData 0] [lindex $repData 1] [lindex $repData 2] [lindex $repData 3] [mol showperiodic $mol $repIndex] [mol numperiodic $mol $repIndex] [mol showrep $mol $repIndex] [mol selupdate $repIndex $mol] [mol colupdate $repIndex $mol] [mol scaleminmax $mol $repIndex] [mol smoothrep $mol $repIndex] [mol drawframes $mol $repIndex]",
        "            for {set cp 0} {$cp < [mol clipplane num]} {incr cp} {",
        "                _mo_record $handle CLIP $repIndex $cp [mol clipplane center $cp $repIndex $mol] [mol clipplane color $cp $repIndex $mol] [mol clipplane normal $cp $repIndex $mol] [mol clipplane status $cp $repIndex $mol]",
        "            }",
        "        }",
        "        foreach matrixName {center_matrix rotate_matrix scale_matrix global_matrix} {",
        "            _mo_record $handle MATRIX $matrixName [molinfo $mol get $matrixName]",
        "        }",
        "        _mo_record $handle RENDERER TachyonInternal [render aasamples TachyonInternal] [render aosamples TachyonInternal]",
        "        set countBeforeEnd $::MO_RECORD_COUNT",
        "        _mo_record $handle END $countBeforeEnd",
        "    } message options]",
        "    catch {close $handle}",
        "    if {$code != 0} {",
        "        catch {file delete -force $tmp}",
        "        return -options $options $message",
        "    }",
        "    file rename -force $tmp $::MO_STATE_PATH",
        "}",
        "proc _mo_confirm {} {",
        "    _mo_set_controls disabled",
        "    rotate stop",
        "    display update ui",
        "    if {$::MO_DEBUG_STATE_PATH ne \"\"} {",
        "        catch {file delete -force $::MO_DEBUG_STATE_PATH}",
        "        if {[catch {save_state $::MO_DEBUG_STATE_PATH} message options]} {",
        "            _mo_report_error \"official save_state\" $message $options",
        "            return",
        "        }",
        "        if {[catch {",
        "            set nativeHandle [open $::MO_DEBUG_STATE_PATH a]",
        "            fconfigure $nativeHandle -encoding utf-8 -translation lf",
        "            puts $nativeHandle \"# MolecularStudio managed native state\"",
        "            puts $nativeHandle \"# MolecularStudio geometry $::MO_GEOMETRY_FINGERPRINT\"",
        "            puts $nativeHandle \"# MolecularStudio cube_sha256 $::MO_REFERENCE_SHA256\"",
        "            close $nativeHandle",
        "        } message options]} {",
        "            catch {close $nativeHandle}",
        "            catch {file delete -force $::MO_DEBUG_STATE_PATH}",
        "            _mo_report_error \"native state metadata\" $message $options",
        "            return",
        "        }",
        "    }",
        "    if {[catch {_mo_write_state} message options]} {",
        "        if {$::MO_DEBUG_STATE_PATH ne \"\"} { catch {file delete -force $::MO_DEBUG_STATE_PATH} }",
        "        _mo_report_error \"validated capture protocol\" $message $options",
        "        return",
        "    }",
        "    catch {file delete -force $::MO_CANCEL_PATH}",
        "    set ::MO_CONFIRMED 1",
        "    catch {destroy .moCapture}",
        "    puts \"MolecularStudio: capture confirmed; the host will close VMD safely.\"",
        "}",
        "proc _mo_reset {} {",
        "    if {[lsearch -exact [molinfo list] $::MO_REFERENCE_MOL] >= 0} {",
        "        mol top $::MO_REFERENCE_MOL",
        "        display resetview",
        "        display update ui",
        "    }",
        "}",
        "proc _mo_cancel {} {",
        "    set ::MO_CONFIRMED 0",
        "    catch {file delete -force $::MO_STATE_PATH}",
        "    if {$::MO_DEBUG_STATE_PATH ne \"\"} { catch {file delete -force $::MO_DEBUG_STATE_PATH} }",
        "    catch {_mo_write_marker $::MO_CANCEL_PATH cancelled}",
        "    catch {destroy .moCapture}",
        "    puts \"MolecularStudio: capture cancelled; the host will close VMD safely.\"",
        "}",
        "proc _mo_quit_trace {name1 name2 operation} {",
        "    if {!$::MO_CONFIRMED} {",
        "        catch {file delete -force $::MO_STATE_PATH}",
        "        catch {_mo_write_marker $::MO_CANCEL_PATH cancelled}",
        "    }",
        "}",
    ]


def build_interactive_capture_tcl(
    cube_path: Path | str,
    state_path: Path | str,
    style: Mapping[str, object],
    *,
    rep0_commands: list[str] | None = None,
    width: int = 960,
    height: int = 720,
    debug_state_path: Path | str | None = None,
    initial_scene_tcl: str | None = None,
) -> str:
    """Build an interactive VMD script that saves a validated data snapshot.

    The caller launches VMD normally (not ``-dispdev text``) with this Tcl
    script.  A successfully confirmed session atomically creates ``state_path``;
    the host then closes VMD without invoking its crash-prone Windows 1.9.3
    shutdown path.  Cancel writes a paired atomic marker and leaves no
    confirmed state.
    """
    cube = Path(cube_path).expanduser().resolve()
    state = Path(state_path).expanduser().resolve()
    if not cube.is_file():
        raise OrbitalVmdValidationError(f"Reference Cube does not exist: {cube}")
    _validate_dimensions(width, height)
    if not isinstance(style, Mapping):
        raise OrbitalVmdValidationError("Drawing style must be an object.")
    geometry_fingerprint = cube_geometry_fingerprint(cube)
    cube_digest = _file_sha256(cube)
    debug = Path(debug_state_path).expanduser().resolve() if debug_state_path else None
    cancel_marker = capture_cancel_marker_path(state)
    error_log = capture_error_log_path(state)
    if initial_scene_tcl is None:
        initial_scene_lines = _initial_signed_scene_tcl(
            cube, style, rep0_commands
        )
    else:
        scene_text = str(initial_scene_tcl)
        if "\x00" in scene_text or len(scene_text.encode("utf-8")) > 2 * 1024 * 1024:
            raise OrbitalVmdValidationError("Initial VMD scene script is invalid.")
        initial_scene_lines = scene_text.rstrip().splitlines()
        initial_scene_lines.extend(
            [
                "if {[llength [molinfo list]] < 1} { error \"The initial VMD scene contains no molecule\" }",
                "set ::MO_REFERENCE_MOL [molinfo top]",
            ]
        )

    lines: list[str] = [
        "# Generated by orbital_vmd.py; VMD 1.9.3 / Tcl 8.5 compatible.",
        *_capture_helpers_tcl(),
        f"set ::MO_STATE_PATH {_tcl_hex_expression(state)}",
        f"set ::MO_CANCEL_PATH {_tcl_hex_expression(cancel_marker)}",
        f"set ::MO_ERROR_PATH {_tcl_hex_expression(error_log)}",
        f"set ::MO_DEBUG_STATE_PATH {_tcl_hex_expression(debug) if debug else '{}'}",
        f"set ::MO_GEOMETRY_FINGERPRINT {{{geometry_fingerprint}}}",
        f"set ::MO_REFERENCE_SHA256 {{{cube_digest}}}",
        "set ::MO_CONFIRMED 0",
        "catch {file delete -force $::MO_STATE_PATH}",
        "catch {file delete -force $::MO_CANCEL_PATH}",
        "catch {file delete -force $::MO_ERROR_PATH}",
        "if {$::MO_DEBUG_STATE_PATH ne \"\"} { catch {file delete -force $::MO_DEBUG_STATE_PATH} }",
        "trace add variable ::vmd_quit write _mo_quit_trace",
        *initial_scene_lines,
        f"display resize {width} {height}",
        # VMD remembers a maximized OpenGL window between sessions on some
        # Windows installations.  An explicit resize followed by reposition
        # restores a normal, fully visible window without affecting the
        # headless Tachyon resolution selected later by the workflow.
        "display reposition 32 40",
        "axes location Off",
        "display update ui",
        "package require Tk",
        "catch {destroy .moCapture}",
        "toplevel .moCapture",
        "wm title .moCapture \"Orbital view\"",
        "wm resizable .moCapture 0 0",
        "catch {wm attributes .moCapture -topmost 1}",
        "wm protocol .moCapture WM_DELETE_WINDOW _mo_cancel",
        "button .moCapture.confirm -text \"\\u4fdd\\u5b58\\u5168\\u90e8\\u53c2\\u6570\\u5e76\\u786e\\u8ba4\" -command _mo_confirm -padx 16 -pady 8",
        "button .moCapture.reset -text \"\\u91cd\\u7f6e\" -command _mo_reset -padx 16 -pady 8",
        "button .moCapture.cancel -text \"\\u53d6\\u6d88\" -command _mo_cancel -padx 16 -pady 8",
        "pack .moCapture.confirm .moCapture.reset .moCapture.cancel -side top -fill x -padx 8 -pady 5",
        "raise .moCapture",
        "focus -force .moCapture.confirm",
        "puts \"MolecularStudio: adjust the scene, then confirm in the floating control window.\"",
    ]
    return "\n".join(lines) + "\n"


def _decode_protocol_field(value: str, line_number: int) -> str:
    if len(value) % 2 or not re.fullmatch(r"[0-9A-Fa-f]*", value):
        raise OrbitalVmdValidationError(f"Invalid hexadecimal field on state line {line_number}.")
    if len(value) // 2 > MAX_FIELD_BYTES:
        raise OrbitalVmdValidationError(f"State field on line {line_number} is too large.")
    try:
        return bytes.fromhex(value).decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrbitalVmdValidationError(
            f"Invalid UTF-8 state field on line {line_number}."
        ) from exc


def _parse_state_protocol(text: str) -> VmdViewState:
    lines = text.splitlines()
    if not lines or lines[0] != STATE_MAGIC:
        raise OrbitalVmdValidationError("This is not a supported orbital VMD state file.")
    if len(lines) > MAX_STATE_LINES:
        raise OrbitalVmdValidationError("The captured VMD state contains too many records.")

    records: list[tuple[str, list[str], int]] = []
    for line_number, line in enumerate(lines[1:], start=2):
        if not line:
            raise OrbitalVmdValidationError(f"Unexpected blank state line {line_number}.")
        raw_fields = line.split("\t")
        tag = raw_fields[0]
        if not re.fullmatch(r"[A-Z_]+", tag):
            raise OrbitalVmdValidationError(f"Invalid state record tag on line {line_number}.")
        fields = [_decode_protocol_field(value, line_number) for value in raw_fields[1:]]
        records.append((tag, fields, line_number))
    if not records or records[-1][0] != "END":
        raise OrbitalVmdValidationError("The captured VMD state is incomplete.")
    if any(tag == "END" for tag, _, _ in records[:-1]):
        raise OrbitalVmdValidationError("The captured VMD state has an early END record.")
    end_fields = records[-1][1]
    if len(end_fields) != 1 or int(end_fields[0]) != len(records) - 1:
        raise OrbitalVmdValidationError("The captured VMD state record count is invalid.")

    meta: dict[str, str] = {}
    display_raw: dict[str, str] = {}
    scenes: dict[str, str] = {}
    lights: list[VmdLight] = []
    colors: list[VmdColor] = []
    categories: list[VmdColorCategory] = []
    scales: list[VmdColorScale] = []
    materials: list[VmdMaterial] = []
    reps: dict[int, VmdRepresentation] = {}
    clips_by_rep: dict[int, list[VmdClipPlane]] = {}
    matrices: dict[str, tuple[float, ...]] = {}
    scale_active: tuple[str, float, float, float] | None = None
    renderer_aa = 12
    renderer_ao = 12

    for tag, fields, line_number in records[:-1]:
        try:
            if tag == "META" and len(fields) == 2:
                if fields[0] in meta:
                    raise OrbitalVmdValidationError(f"Duplicate metadata: {fields[0]}.")
                meta[fields[0]] = fields[1]
            elif tag == "DISPLAY" and len(fields) == 2:
                if fields[0] in display_raw:
                    raise OrbitalVmdValidationError(f"Duplicate display field: {fields[0]}.")
                display_raw[fields[0]] = fields[1]
            elif tag == "SCENE" and len(fields) == 2:
                if fields[0] in scenes:
                    raise OrbitalVmdValidationError(f"Duplicate scene field: {fields[0]}.")
                scenes[fields[0]] = fields[1]
            elif tag == "LIGHT" and len(fields) == 4:
                lights.append(
                    VmdLight(
                        index=int(fields[0]),
                        enabled=_parse_bool(fields[1], "light enabled state"),
                        highlighted=_parse_highlight(fields[2]),
                        position=_parse_float_vector(fields[3], 3, "light position"),
                    )
                )
            elif tag == "COLOR" and len(fields) == 3:
                colors.append(
                    VmdColor(
                        index=int(fields[0]),
                        name=fields[1],
                        rgb=_parse_float_vector(fields[2], 3, "color RGB", 0.0, 1.0),
                    )
                )
            elif tag == "COLOR_CATEGORY" and len(fields) == 3:
                categories.append(VmdColorCategory(*fields))
            elif tag == "COLOR_SCALE_ACTIVE" and len(fields) == 4:
                if scale_active is not None:
                    raise OrbitalVmdValidationError("Duplicate active color-scale record.")
                scale_active = (
                    fields[0],
                    _finite_float(fields[1], "color-scale midpoint", 0.0, 1.0),
                    _finite_float(fields[2], "color-scale min", 0.0, 1.0),
                    _finite_float(fields[3], "color-scale max", 0.0, 1.0),
                )
            elif tag == "COLOR_SCALE" and len(fields) == 2:
                scales.append(
                    VmdColorScale(
                        method=fields[0],
                        colors=_parse_float_vector(
                            fields[1], 9, f"color scale {fields[0]}", 0.0, 1.0
                        ),
                    )
                )
            elif tag == "MATERIAL" and len(fields) == 2:
                values = _parse_float_vector(fields[1], 9, f"material {fields[0]}")
                materials.append(VmdMaterial(fields[0], *values))
            elif tag == "REP" and len(fields) == 13:
                index = int(fields[0])
                if index in reps:
                    raise OrbitalVmdValidationError(f"Duplicate representation {index}.")
                reps[index] = VmdRepresentation(
                    index=index,
                    style=fields[1],
                    selection=fields[2],
                    color=fields[3],
                    material=fields[4],
                    show_periodic=fields[5],
                    num_periodic=int(fields[6]),
                    shown=_parse_bool(fields[7], "representation shown state"),
                    selection_update=_parse_bool(fields[8], "selection update state"),
                    color_update=_parse_bool(fields[9], "color update state"),
                    scale_minmax=_parse_float_vector(fields[10], 2, "representation scale range"),
                    smoothing=int(fields[11]),
                    draw_frames=fields[12],
                )
            elif tag == "CLIP" and len(fields) == 6:
                rep_index = int(fields[0])
                clips_by_rep.setdefault(rep_index, []).append(
                    VmdClipPlane(
                        index=int(fields[1]),
                        center=_parse_float_vector(fields[2], 3, "clip-plane center"),
                        color=_parse_float_vector(fields[3], 3, "clip-plane color", 0.0, 1.0),
                        normal=_parse_float_vector(fields[4], 3, "clip-plane normal"),
                        enabled=_parse_bool(fields[5], "clip-plane status"),
                    )
                )
            elif tag == "MATRIX" and len(fields) == 2:
                if fields[0] in matrices:
                    raise OrbitalVmdValidationError(f"Duplicate matrix: {fields[0]}.")
                matrices[fields[0]] = _parse_float_vector(fields[1], 16, fields[0])
            elif tag == "RENDERER" and len(fields) == 3:
                if fields[0] != "TachyonInternal":
                    raise OrbitalVmdValidationError("Unexpected renderer state record.")
                renderer_aa = int(fields[1])
                renderer_ao = int(fields[2])
            else:
                raise OrbitalVmdValidationError(
                    f"Unsupported or malformed {tag} record on line {line_number}."
                )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, OrbitalVmdValidationError):
                raise
            raise OrbitalVmdValidationError(
                f"Invalid value in {tag} record on line {line_number}: {exc}"
            ) from exc

    required_meta = {
        "confirmed",
        "schema",
        "vmd_version",
        "geometry_fingerprint",
        "reference_cube_sha256",
    }
    if set(meta) != required_meta or meta.get("confirmed") != "1":
        raise OrbitalVmdValidationError("The VMD state was not explicitly confirmed.")
    if scale_active is None:
        raise OrbitalVmdValidationError("The active VMD color scale was not captured.")
    if set(scenes) != {"axes", "stage"}:
        raise OrbitalVmdValidationError("Scene axes/stage settings are incomplete.")

    display: dict[str, object] = {}
    if set(display_raw) != _DISPLAY_FIELDS:
        raise OrbitalVmdValidationError("Display settings are incomplete.")
    for key, raw_value in display_raw.items():
        if key in _DISPLAY_FLOAT_FIELDS:
            display[key] = _finite_float(raw_value, f"display.{key}")
        elif key in _DISPLAY_BOOL_FIELDS:
            display[key] = _parse_bool(raw_value, f"display.{key}")
        elif key == "size":
            size = _parse_float_vector(raw_value, 2, "display size", 1.0, 100_000.0)
            if any(not float(value).is_integer() for value in size):
                raise OrbitalVmdValidationError("Display size must contain integers.")
            display[key] = (int(size[0]), int(size[1]))
        else:
            display[key] = raw_value

    finalized_reps: list[VmdRepresentation] = []
    for index in sorted(reps):
        rep = reps[index]
        finalized_reps.append(
            VmdRepresentation(
                **{
                    **asdict(rep),
                    "clip_planes": tuple(sorted(clips_by_rep.pop(index, []), key=lambda item: item.index)),
                }
            )
        )
    if clips_by_rep:
        raise OrbitalVmdValidationError("A clip plane refers to an unknown representation.")

    state = VmdViewState(
        schema_version=int(meta["schema"]),
        vmd_version=meta["vmd_version"],
        geometry_fingerprint=meta["geometry_fingerprint"],
        reference_cube_sha256=meta["reference_cube_sha256"],
        display=display,
        axes_location=scenes["axes"],
        stage_location=scenes["stage"],
        lights=tuple(sorted(lights, key=lambda item: item.index)),
        colors=tuple(sorted(colors, key=lambda item: item.index)),
        color_categories=tuple(categories),
        color_scale_method=scale_active[0],
        color_scale_midpoint=scale_active[1],
        color_scale_min=scale_active[2],
        color_scale_max=scale_active[3],
        color_scales=tuple(scales),
        materials=tuple(materials),
        representations=tuple(finalized_reps),
        matrices=matrices,
        renderer_aa_samples=renderer_aa,
        renderer_ao_samples=renderer_ao,
    )
    return state.validate()


def load_view_state(
    path: Path | str, *, expected_geometry_fingerprint: str | None = None
) -> VmdViewState:
    """Load and validate either a VMD capture protocol or normalized JSON state."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise OrbitalVmdValidationError(f"VMD view state does not exist: {source}")
    try:
        size = source.stat().st_size
        if size <= 0 or size > MAX_STATE_BYTES:
            raise OrbitalVmdValidationError("VMD view state size is invalid.")
        raw = source.read_bytes()
        # VMD 1.9.3 writes save_state with Tcl's platform encoding.  Latin-1
        # is used only for ASCII signature/marker inspection and preserves all
        # bytes; VMD itself later sources the original file in the same locale.
        text = raw.decode("latin-1")
    except OSError as exc:
        raise OrbitalVmdValidationError(f"Unable to read VMD view state: {exc}") from exc
    if text.lstrip().startswith("{"):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrbitalVmdValidationError(f"Invalid VMD state JSON: {exc}") from exc
        state = VmdViewState.from_dict(_require_mapping(raw, "VMD state"))
    else:
        state = _parse_state_protocol(text)
    if expected_geometry_fingerprint is not None:
        _validate_hex_digest(expected_geometry_fingerprint, "expected geometry fingerprint")
        if state.geometry_fingerprint != expected_geometry_fingerprint:
            raise OrbitalVmdValidationError(
                "The captured view belongs to a different Cube geometry."
            )
    return state


def _state_from_value(state_or_path: VmdViewState | Path | str) -> VmdViewState:
    if isinstance(state_or_path, VmdViewState):
        return state_or_path.validate()
    return load_view_state(state_or_path)


def _read_native_state(path: Path | str) -> tuple[Path, str]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise OrbitalVmdValidationError(f"Native VMD state does not exist: {source}")
    try:
        size = source.stat().st_size
        if size <= 0 or size > MAX_NATIVE_STATE_BYTES:
            raise OrbitalVmdValidationError("Native VMD state size is invalid.")
        text = source.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise OrbitalVmdValidationError(f"Unable to read native VMD state: {exc}") from exc
    if "VMD script written by save_state" not in text or "# VMD version:" not in text:
        raise OrbitalVmdValidationError(
            "The native state is not an official VMD save_state script."
        )
    if "\x00" in text:
        raise OrbitalVmdValidationError("Native VMD state contains NUL bytes.")
    return source, text


def _native_state_loader_tcl(
    native_state_path: Path | str,
    *,
    reference_cube_path: Path | str,
    replacement_cube_path: Path,
    expected_geometry_fingerprint: str,
    expected_cube_sha256: str,
) -> list[str]:
    """Prepare a trusted, locally generated VMD ``save_state`` replay.

    The official script is authoritative for the scene because it includes
    objects that the normalized snapshot cannot fully describe (additional
    molecules, graphics primitives, labels, atom-selection macros, molecule
    names/visibility and per-molecule viewpoints).  The script is accepted
    only when it has the VMD save_state signature and it is never edited as
    raw Tcl text.  Instead, VMD temporarily wraps ``mol new``/``mol addfile``
    while sourcing it so only the exact captured reference Cube path is
    substituted with the current orbital Cube.  A finally-style restoration
    puts the original VMD commands back even when replay raises an error.
    """

    native_path, native_text = _read_native_state(native_state_path)
    reference = Path(reference_cube_path).expanduser().resolve()
    if not reference.is_file():
        raise OrbitalVmdValidationError(
            f"Native-state reference Cube does not exist: {reference}"
        )
    if _file_sha256(reference) != expected_cube_sha256:
        raise OrbitalVmdValidationError(
            "The native-state reference Cube does not match the captured state."
        )
    geometry_marker = re.search(
        r"(?m)^# MolecularStudio geometry ([0-9a-f]{64})$", native_text
    )
    digest_marker = re.search(
        r"(?m)^# MolecularStudio cube_sha256 ([0-9a-f]{64})$", native_text
    )
    if (
        geometry_marker is None
        or digest_marker is None
        or geometry_marker.group(1) != expected_geometry_fingerprint
        or digest_marker.group(1) != expected_cube_sha256
    ):
        raise OrbitalVmdValidationError(
            "Native VMD state is not paired with this confirmed data snapshot."
        )
    return [
        f"set MO_NATIVE_STATE {_tcl_hex_expression(native_path)}",
        f"set MO_NATIVE_REFERENCE [file normalize {_tcl_hex_expression(reference)}]",
        f"set MO_NATIVE_REPLACEMENT [file normalize {_tcl_hex_expression(replacement_cube_path)}]",
        "rename mol _mo_native_mol_command",
        "proc mol {subcommand args} {",
        "    if {$subcommand eq \"new\" && [llength $args] > 0} {",
        "        set candidate [lindex $args 0]",
        "        if {![catch {file normalize $candidate} normalized] && $normalized eq $::MO_NATIVE_REFERENCE} {",
        "            set args [lreplace $args 0 0 $::MO_NATIVE_REPLACEMENT]",
        "        }",
        "    } elseif {$subcommand eq \"addfile\" && [llength $args] > 0} {",
        "        set candidate [lindex $args 0]",
        "        if {![catch {file normalize $candidate} normalized] && $normalized eq $::MO_NATIVE_REFERENCE} {",
        "            set args [lreplace $args 0 0 $::MO_NATIVE_REPLACEMENT]",
        "        }",
        "    }",
        "    return [uplevel 1 [linsert $args 0 _mo_native_mol_command $subcommand]]",
        "}",
        "set MO_NATIVE_CODE [catch {source $MO_NATIVE_STATE} MO_NATIVE_MESSAGE MO_NATIVE_OPTIONS]",
        "rename mol {}",
        "rename _mo_native_mol_command mol",
        "if {$MO_NATIVE_CODE != 0} {",
        "    error \"Unable to replay confirmed VMD state: $MO_NATIVE_MESSAGE\"",
        "}",
    ]


def _format_number(value: object) -> str:
    return format(_finite_float(value, "VMD numeric value"), ".17g")


def _tcl_list(values: Iterable[object]) -> str:
    return "{" + " ".join(_format_number(value) for value in values) + "}"


def _tcl_matrix(values: Sequence[object]) -> str:
    _validate_vector(values, 16, "VMD matrix")
    rows = [_tcl_list(values[index : index + 4]) for index in range(0, 16, 4)]
    return "{" + " ".join(rows) + "}"


def _tcl_color_scale(values: Sequence[object]) -> str:
    _validate_vector(values, 9, "VMD color scale", 0.0, 1.0)
    return " ".join(
        _tcl_list(values[index : index + 3]) for index in range(0, 9, 3)
    )


def _restore_global_scene_tcl(
    state: VmdViewState, *, width: int, height: int
) -> list[str]:
    """Replay normalized global scene data omitted by VMD 1.9.3 save_state."""

    lines: list[str] = []
    for color in state.colors:
        lines.append(
            f"color change rgb {color.index} "
            + " ".join(_format_number(value) for value in color.rgb)
        )
    for entry in state.color_categories:
        lines.extend(
            [
                f"set MO_CAT {_tcl_hex_expression(entry.category)}",
                f"set MO_ITEM {_tcl_hex_expression(entry.item)}",
                f"set MO_COLOR_NAME {_tcl_hex_expression(entry.color)}",
                "color $MO_CAT $MO_ITEM $MO_COLOR_NAME",
            ]
        )
    for scale in state.color_scales:
        lines.extend(
            [
                f"set MO_SCALE_NAME {_tcl_hex_expression(scale.method)}",
                "color scale colors $MO_SCALE_NAME " + _tcl_color_scale(scale.colors),
            ]
        )
    lines.extend(
        [
            f"set MO_SCALE_ACTIVE {_tcl_hex_expression(state.color_scale_method)}",
            "color scale method $MO_SCALE_ACTIVE",
            # Repeat limits around midpoint to tolerate VMD's ordered-range
            # behavior when an old minimum is greater than the new maximum.
            "color scale min 0",
            "color scale max 1",
            f"color scale midpoint {_format_number(state.color_scale_midpoint)}",
            f"color scale min {_format_number(state.color_scale_min)}",
            f"color scale max {_format_number(state.color_scale_max)}",
        ]
    )
    for material in state.materials:
        lines.extend(
            [
                f"set MO_MATERIAL {_tcl_hex_expression(material.name)}",
                "if {[lsearch -exact [material list] $MO_MATERIAL] < 0} { material add $MO_MATERIAL }",
            ]
        )
        for parameter, value in zip(_MATERIAL_PARAMETER_NAMES, material.values):
            lines.append(f"material change {parameter} $MO_MATERIAL {_format_number(value)}")

    for key in sorted(_DISPLAY_TOKEN_FIELDS):
        lines.extend(
            [
                f"set MO_DISPLAY_VALUE {_tcl_hex_expression(state.display[key])}",
                f"display {key} $MO_DISPLAY_VALUE",
            ]
        )
    for key in sorted(_DISPLAY_BOOL_FIELDS):
        lines.append(f"display {key} {'on' if state.display[key] else 'off'}")
    for key in sorted(_DISPLAY_FLOAT_FIELDS - {"nearclip", "farclip"}):
        lines.append(f"display {key} {_format_number(state.display[key])}")
    lines.extend(
        [
            f"display nearclip set {_format_number(state.display['nearclip'])}",
            f"display farclip set {_format_number(state.display['farclip'])}",
            f"display resize {width} {height}",
            f"set MO_AXES {_tcl_hex_expression(state.axes_location)}",
            "axes location $MO_AXES",
            f"set MO_STAGE {_tcl_hex_expression(state.stage_location)}",
            "stage location $MO_STAGE",
        ]
    )
    for light in state.lights:
        lines.extend(
            [
                f"light {light.index} {'on' if light.enabled else 'off'}",
                f"light {light.index} {'highlight' if light.highlighted else 'unhighlight'}",
                f"light {light.index} pos {_tcl_list(light.position)}",
            ]
        )
    return lines


def _render_tail_tcl(
    state: VmdViewState, *, output: Path, renderer: str
) -> list[str]:
    return [
        f"set MO_OUTPUT {_tcl_hex_expression(output)}",
        "file mkdir [file dirname $MO_OUTPUT]",
        "catch {file delete -force $MO_OUTPUT}",
        "display update",
        f"render aasamples {renderer} {state.renderer_aa_samples}",
        f"render aosamples {renderer} {state.renderer_ao_samples}",
        f"render {renderer} $MO_OUTPUT",
        "if {![file isfile $MO_OUTPUT] || [file size $MO_OUTPUT] < 64} {",
        "    error \"Tachyon did not create a valid-size output file\"",
        "}",
        "puts \"MolecularStudio: orbital render finished\"",
        "quit",
    ]


def _restore_native_state_tcl(
    state: VmdViewState,
    cube: Path,
    output: Path,
    width: int,
    height: int,
    renderer: str,
    *,
    native_state_path: Path | str,
    reference_cube_path: Path | str,
) -> list[str]:
    lines = [
        "proc _mo_unhex {value} { return [encoding convertfrom utf-8 [binary format H* $value]] }",
        *_native_state_loader_tcl(
            native_state_path,
            reference_cube_path=reference_cube_path,
            replacement_cube_path=cube,
            expected_geometry_fingerprint=state.geometry_fingerprint,
            expected_cube_sha256=state.reference_cube_sha256,
        ),
        # VMD 1.9.3's official save_state preserves molecules, graphics,
        # representations, labels, macros and viewpoints, but omits several
        # global display/light fields.  Overlay the validated snapshot only
        # for those globals; native molecule state remains authoritative.
        *_restore_global_scene_tcl(state, width=width, height=height),
        *_render_tail_tcl(state, output=output, renderer=renderer),
    ]
    return lines


def _restore_state_tcl(
    state: VmdViewState, cube: Path, output: Path, width: int, height: int, renderer: str
) -> list[str]:
    lines = [
        "proc _mo_unhex {value} { return [encoding convertfrom utf-8 [binary format H* $value]] }",
        f"set MO_CUBE {_tcl_hex_expression(cube)}",
        f"set MO_OUTPUT {_tcl_hex_expression(output)}",
        "if {![file isfile $MO_CUBE]} { error \"Orbital Cube file is missing\" }",
        "file mkdir [file dirname $MO_OUTPUT]",
        "catch {file delete -force $MO_OUTPUT}",
        "mol new $MO_CUBE type cube waitfor all",
        "set MO_MOL [molinfo top]",
    ]

    for color in state.colors:
        lines.append(
            f"color change rgb {color.index} "
            + " ".join(_format_number(value) for value in color.rgb)
        )
    for entry in state.color_categories:
        lines.extend(
            [
                f"set MO_CAT {_tcl_hex_expression(entry.category)}",
                f"set MO_ITEM {_tcl_hex_expression(entry.item)}",
                f"set MO_COLOR_NAME {_tcl_hex_expression(entry.color)}",
                "color $MO_CAT $MO_ITEM $MO_COLOR_NAME",
            ]
        )
    for scale in state.color_scales:
        lines.extend(
            [
                f"set MO_SCALE_NAME {_tcl_hex_expression(scale.method)}",
                "color scale colors $MO_SCALE_NAME " + _tcl_color_scale(scale.colors),
            ]
        )
    lines.extend(
        [
            f"set MO_SCALE_ACTIVE {_tcl_hex_expression(state.color_scale_method)}",
            "color scale method $MO_SCALE_ACTIVE",
            "color scale min 0",
            "color scale max 1",
            "color scale midpoint 0.5",
            f"color scale min {_format_number(state.color_scale_min)}",
            f"color scale max {_format_number(state.color_scale_max)}",
            f"color scale midpoint {_format_number(state.color_scale_midpoint)}",
            f"color scale min {_format_number(state.color_scale_min)}",
            f"color scale max {_format_number(state.color_scale_max)}",
        ]
    )

    for material in state.materials:
        lines.extend(
            [
                f"set MO_MATERIAL {_tcl_hex_expression(material.name)}",
                "if {[lsearch -exact [material list] $MO_MATERIAL] < 0} { material add $MO_MATERIAL }",
            ]
        )
        for parameter, value in zip(_MATERIAL_PARAMETER_NAMES, material.values):
            lines.append(f"material change {parameter} $MO_MATERIAL {_format_number(value)}")

    while_delete = [
        "while {[molinfo $MO_MOL get numreps] > 0} { mol delrep 0 $MO_MOL }"
    ]
    lines.extend(while_delete)
    for rep in state.representations:
        lines.extend(
            [
                f"set MO_REP_STYLE {_tcl_hex_expression(rep.style)}",
                f"set MO_REP_SELECTION {_tcl_hex_expression(rep.selection)}",
                f"set MO_REP_COLOR {_tcl_hex_expression(rep.color)}",
                f"set MO_REP_MATERIAL {_tcl_hex_expression(rep.material)}",
                "mol representation $MO_REP_STYLE",
                "mol selection $MO_REP_SELECTION",
                "mol color $MO_REP_COLOR",
                "mol material $MO_REP_MATERIAL",
                "mol addrep $MO_MOL",
            ]
        )
        if rep.show_periodic:
            lines.extend(
                [
                    f"set MO_PERIODIC {_tcl_hex_expression(rep.show_periodic)}",
                    f"mol showperiodic $MO_MOL {rep.index} $MO_PERIODIC",
                    f"mol numperiodic $MO_MOL {rep.index} {rep.num_periodic}",
                ]
            )
        lines.extend(
            [
                f"mol selupdate {rep.index} $MO_MOL {1 if rep.selection_update else 0}",
                f"mol colupdate {rep.index} $MO_MOL {1 if rep.color_update else 0}",
                (
                    f"mol scaleminmax $MO_MOL {rep.index} "
                    f"{_format_number(rep.scale_minmax[0])} "
                    f"{_format_number(rep.scale_minmax[1])}"
                ),
                f"mol smoothrep $MO_MOL {rep.index} {rep.smoothing}",
                f"set MO_DRAW_FRAMES {_tcl_hex_expression(rep.draw_frames)}",
                f"mol drawframes $MO_MOL {rep.index} $MO_DRAW_FRAMES",
            ]
        )
        for plane in rep.clip_planes:
            lines.extend(
                [
                    f"mol clipplane center {plane.index} {rep.index} $MO_MOL {_tcl_list(plane.center)}",
                    f"mol clipplane color {plane.index} {rep.index} $MO_MOL {_tcl_list(plane.color)}",
                    f"mol clipplane normal {plane.index} {rep.index} $MO_MOL {_tcl_list(plane.normal)}",
                    f"mol clipplane status {plane.index} {rep.index} $MO_MOL {1 if plane.enabled else 0}",
                ]
            )
        if not rep.shown:
            lines.append(f"mol showrep $MO_MOL {rep.index} 0")

    for key in sorted(_DISPLAY_TOKEN_FIELDS):
        lines.extend(
            [
                f"set MO_DISPLAY_VALUE {_tcl_hex_expression(state.display[key])}",
                f"display {key} $MO_DISPLAY_VALUE",
            ]
        )
    for key in sorted(_DISPLAY_BOOL_FIELDS):
        lines.append(f"display {key} {'on' if state.display[key] else 'off'}")
    for key in sorted(_DISPLAY_FLOAT_FIELDS - {"nearclip", "farclip"}):
        lines.append(f"display {key} {_format_number(state.display[key])}")
    lines.extend(
        [
            f"display nearclip set {_format_number(state.display['nearclip'])}",
            f"display farclip set {_format_number(state.display['farclip'])}",
            f"display resize {width} {height}",
            f"set MO_AXES {_tcl_hex_expression(state.axes_location)}",
            "axes location $MO_AXES",
            f"set MO_STAGE {_tcl_hex_expression(state.stage_location)}",
            "stage location $MO_STAGE",
        ]
    )
    for light in state.lights:
        lines.extend(
            [
                f"light {light.index} {'on' if light.enabled else 'off'}",
                f"light {light.index} {'highlight' if light.highlighted else 'unhighlight'}",
                f"light {light.index} pos {_tcl_list(light.position)}",
            ]
        )
    matrix_variables: list[str] = []
    for name in _MATRIX_NAMES:
        variable = "MO_" + name.upper()
        matrix_variables.append(f"${variable}")
        lines.append(f"set {variable} {_tcl_matrix(state.matrices[name])}")
    lines.extend(
        [
            "molinfo $MO_MOL set {center_matrix rotate_matrix scale_matrix global_matrix} "
            + "[list " + " ".join(matrix_variables) + "]",
            "mol top $MO_MOL",
            "display update",
            f"render aasamples {renderer} {state.renderer_aa_samples}",
            f"render aosamples {renderer} {state.renderer_ao_samples}",
            f"render {renderer} $MO_OUTPUT",
            "if {![file isfile $MO_OUTPUT] || [file size $MO_OUTPUT] < 64} {",
            "    error \"Tachyon did not create a valid-size output file\"",
            "}",
            "puts \"MolecularStudio: orbital render finished\"",
            "quit",
        ]
    )
    return lines


def build_batch_render_tcl(
    cube_path: Path | str,
    output_tga: Path | str,
    state_or_path: VmdViewState | Path | str,
    *,
    width: int | None = None,
    height: int | None = None,
    renderer: str = "TachyonInternal",
    native_state_path: Path | str | None = None,
    reference_cube_path: Path | str | None = None,
) -> str:
    """Build a headless VMD Tcl script that replays a confirmed scene.

    When the paired ``native_state_path`` and ``reference_cube_path`` are
    supplied, the official VMD ``save_state`` output is authoritative.  The
    normalized data snapshot still validates the Cube geometry and restores
    global fields that VMD 1.9.3 omits from ``save_state``.  Without the native
    pair, a deterministic data-only replay remains available as a fallback.
    """
    cube = Path(cube_path).expanduser().resolve()
    output = Path(output_tga).expanduser().resolve()
    if not cube.is_file():
        raise OrbitalVmdValidationError(f"Orbital Cube does not exist: {cube}")
    if output.suffix.lower() != ".tga":
        raise OrbitalVmdValidationError("VMD 1.9.3 Tachyon output must use a .tga path.")
    state = _state_from_value(state_or_path)
    geometry = cube_geometry_fingerprint(cube)
    if geometry != state.geometry_fingerprint:
        raise OrbitalVmdValidationError(
            "Orbital Cube geometry/grid does not match the captured reference."
        )
    if renderer not in {"TachyonInternal", "Tachyon"}:
        raise OrbitalVmdValidationError("Only TachyonInternal or Tachyon is allowed.")
    render_width, render_height = resolve_render_dimensions(
        state.viewport, width=width, height=height
    )
    if (native_state_path is None) != (reference_cube_path is None):
        raise OrbitalVmdValidationError(
            "Native VMD state and its reference Cube must be supplied together."
        )
    if native_state_path is not None:
        restore_lines = _restore_native_state_tcl(
            state,
            cube,
            output,
            render_width,
            render_height,
            renderer,
            native_state_path=native_state_path,
            reference_cube_path=reference_cube_path,
        )
    else:
        restore_lines = _restore_state_tcl(
            state, cube, output, render_width, render_height, renderer
        )
    lines = [
        "# Generated by orbital_vmd.py from a confirmed VMD scene.",
        *restore_lines,
    ]
    return "\n".join(lines) + "\n"


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_interactive_capture_script(
    script_path: Path | str,
    cube_path: Path | str,
    state_path: Path | str,
    style: Mapping[str, object],
    **kwargs: object,
) -> Path:
    target = Path(script_path).expanduser().resolve()
    _write_text_atomic(
        target,
        build_interactive_capture_tcl(cube_path, state_path, style, **kwargs),
    )
    return target


def write_batch_render_script(
    script_path: Path | str,
    cube_path: Path | str,
    output_tga: Path | str,
    state_or_path: VmdViewState | Path | str,
    **kwargs: object,
) -> Path:
    target = Path(script_path).expanduser().resolve()
    _write_text_atomic(
        target,
        build_batch_render_tcl(cube_path, output_tga, state_or_path, **kwargs),
    )
    return target


def validate_render_output(
    path: Path | str,
    *,
    min_bytes: int = 64,
    expected_size: tuple[int, int] | None = None,
) -> Path:
    """Validate a Targa image produced by VMD 1.9.3 Tachyon."""
    output = Path(path).expanduser().resolve()
    if not output.is_file():
        raise OrbitalVmdValidationError(f"VMD render output is missing: {output}")
    try:
        size = output.stat().st_size
        if size < max(18, int(min_bytes)):
            raise OrbitalVmdValidationError("VMD render output is too small.")
        with output.open("rb") as handle:
            header = handle.read(18)
    except OSError as exc:
        raise OrbitalVmdValidationError(f"Unable to inspect VMD render output: {exc}") from exc
    if len(header) != 18:
        raise OrbitalVmdValidationError("Targa header is incomplete.")
    id_length, color_map_type, image_type = header[0], header[1], header[2]
    width, height, pixel_depth = struct.unpack_from("<HHB", header, 12)
    if color_map_type != 0 or image_type not in {2, 10}:
        raise OrbitalVmdValidationError("VMD output is not a supported true-color Targa image.")
    if width <= 0 or height <= 0 or pixel_depth not in {24, 32}:
        raise OrbitalVmdValidationError("VMD Targa dimensions or pixel depth are invalid.")
    if expected_size is not None and (width, height) != tuple(expected_size):
        raise OrbitalVmdValidationError(
            f"VMD render size is {width}x{height}, expected {expected_size[0]}x{expected_size[1]}."
        )
    if image_type == 2:
        minimum_payload = 18 + id_length + width * height * (pixel_depth // 8)
        if size < minimum_payload:
            raise OrbitalVmdValidationError("Uncompressed VMD Targa pixel data is incomplete.")
    return output


__all__ = [
    "OrbitalVmdError",
    "OrbitalVmdValidationError",
    "VmdClipPlane",
    "VmdColor",
    "VmdColorCategory",
    "VmdColorScale",
    "VmdLight",
    "VmdMaterial",
    "VmdRepresentation",
    "VmdViewState",
    "build_batch_render_tcl",
    "build_interactive_capture_tcl",
    "capture_cancel_marker_path",
    "capture_error_log_path",
    "cube_geometry_fingerprint",
    "load_view_state",
    "resolve_render_dimensions",
    "validate_render_output",
    "write_batch_render_script",
    "write_interactive_capture_script",
    "vmd_display_window_handles",
    "restore_vmd_display_window",
]
