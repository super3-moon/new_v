"""Parse and validate orbital metadata for the orbital-diagram workflow.

The module deliberately separates the two input responsibilities used by the
workflow:

* Gaussian ``.fch``/``.fchk`` and ORCA ``.molden``/``.molden.input`` files are
  the authoritative source of orbital energies, occupations, spin channels,
  basis size, and the indices sent to Multiwfn.
* Gaussian/ORCA ``.out``/``.log`` files are companion records.  They are used
  to check termination, SCF convergence, calculation identity, final geometry,
  and (when printed) orbital energies.  Output-file orbital numbering is never
  allowed to replace the wavefunction-file numbering.

All public data objects are immutable dataclasses and expose :meth:`to_dict`
for Qt models, manifests, and JSON serialization.  Parsing is read-only and
does not touch Multiwfn's global ``settings.ini``.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence


HARTREE_TO_EV = 27.211386245988
BOHR_TO_ANGSTROM = 0.529177210903


class OrbitalDataError(Exception):
    """Base class for errors that are safe to present to the user."""


class UnsupportedFileError(OrbitalDataError):
    """Raised when an input is not a supported output or wavefunction file."""


class OrbitalParseError(OrbitalDataError):
    """Raised when a supported file is incomplete or internally inconsistent."""


class PairingError(OrbitalDataError):
    """Raised when files cannot be paired unambiguously or fail validation."""

    def __init__(
        self,
        message: str,
        *,
        validation: PairValidation | None = None,
    ) -> None:
        super().__init__(message)
        self.validation = validation


class OrbitalSelectionError(OrbitalDataError):
    """Raised when an orbital expression is ambiguous or outside the data."""


class CalculationProgram(str, Enum):
    """Supported electronic-structure program families."""

    GAUSSIAN = "gaussian"
    ORCA = "orca"


class WavefunctionType(str, Enum):
    """Single-determinant orbital layouts supported by the normal workflow."""

    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"
    RESTRICTED_OPEN_SHELL = "restricted_open_shell"


class SpinChannel(str, Enum):
    """Orbital channel as exposed to the workflow and the diagram renderer."""

    SPATIAL = "spatial"
    ALPHA = "alpha"
    BETA = "beta"


class ValidationLevel(str, Enum):
    """Severity of one input-pair validation check."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


def _json_value(value: Any) -> Any:
    """Convert nested module values to JSON-friendly built-in objects."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class _DictMixin:
    """Small serialization mixin shared by all public dataclasses."""

    def to_dict(self) -> dict[str, Any]:
        """Return a recursively JSON-serializable representation."""

        return _json_value(self)


@dataclass(frozen=True, slots=True)
class Atom(_DictMixin):
    """One atom in Angstrom coordinates."""

    atomic_number: int
    symbol: str
    x: float
    y: float
    z: float

    @property
    def coordinates(self) -> tuple[float, float, float]:
        """Return ``(x, y, z)`` in Angstrom."""

        return (self.x, self.y, self.z)


@dataclass(frozen=True, slots=True)
class Orbital(_DictMixin):
    """Authoritative orbital metadata read from fch/fchk or Molden.

    ``channel_index`` is always one-based within the displayed spin channel.
    ``global_index`` is the positive one-based number accepted by Multiwfn's
    main function 200, subfunction 3.  For unrestricted wavefunctions it is
    ``i`` for alpha and ``n_alpha_mo + i`` for beta.  The offset deliberately
    uses the number of alpha molecular orbitals actually stored in the file,
    rather than the basis-function count: linearly dependent bases can make
    those two values different.
    """

    spin: SpinChannel
    channel_index: int
    global_index: int
    energy_hartree: float
    occupation: float
    symmetry: str = ""

    @property
    def energy_ev(self) -> float:
        """Orbital energy in electron volts."""

        return self.energy_hartree * HARTREE_TO_EV

    @property
    def index(self) -> int:
        """Compatibility alias for the one-based channel-local index."""

        return self.channel_index

    @property
    def multiwfn_index(self) -> int:
        """Compatibility alias for :attr:`global_index`."""

        return self.global_index


@dataclass(frozen=True, slots=True)
class OutputOrbitalEnergy(_DictMixin):
    """Orbital energy printed in a companion output file.

    ORCA's printed ``source_index`` is zero-based.  ``channel_index`` is its
    normalized one-based counterpart and is used only for cross-checking the
    authoritative Molden data.
    """

    spin: SpinChannel
    source_index: int
    source_index_base: int
    channel_index: int
    energy_hartree: float
    occupation: float | None = None


@dataclass(frozen=True, slots=True)
class OutputMetadata(_DictMixin):
    """Metadata and health checks parsed from Gaussian or ORCA output."""

    path: Path
    program: CalculationProgram
    normal_termination: bool
    scf_converged: bool | None
    charge: int | None = None
    multiplicity: int | None = None
    alpha_electrons: int | None = None
    beta_electrons: int | None = None
    atoms: tuple[Atom, ...] = ()
    orbital_energies: tuple[OutputOrbitalEnergy, ...] = ()
    route_or_keywords: str = ""
    method: str = ""
    basis: str = ""
    termination_detail: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def geometry_fingerprint(self) -> str:
        """Return an orientation-independent fingerprint of the final geometry."""

        return geometry_fingerprint(self.atoms)


@dataclass(frozen=True, slots=True)
class ValidationCheck(_DictMixin):
    """One explicit result in companion-file validation."""

    name: str
    level: ValidationLevel
    message: str
    value: float | int | str | bool | None = None


@dataclass(frozen=True, slots=True)
class PairValidation(_DictMixin):
    """Complete validation report for one output/wavefunction pair."""

    checks: tuple[ValidationCheck, ...]
    geometry_distance_rmsd: float | None = None
    orbital_energy_max_difference_hartree: float | None = None
    compared_orbital_count: int = 0

    @property
    def is_valid(self) -> bool:
        """Whether no error-level validation check failed."""

        return not any(check.level is ValidationLevel.ERROR for check in self.checks)

    @property
    def errors(self) -> tuple[str, ...]:
        """User-facing error messages."""

        return tuple(
            check.message
            for check in self.checks
            if check.level is ValidationLevel.ERROR
        )

    @property
    def warnings(self) -> tuple[str, ...]:
        """User-facing warning messages."""

        return tuple(
            check.message
            for check in self.checks
            if check.level is ValidationLevel.WARNING
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the report plus convenient aggregate fields."""

        result = _json_value(self)
        result.update(
            {
                "is_valid": self.is_valid,
                "errors": list(self.errors),
                "warnings": list(self.warnings),
            }
        )
        return result


@dataclass(frozen=True, slots=True)
class InputPair(_DictMixin):
    """One unambiguous companion-output and wavefunction-file pair."""

    output_path: Path
    wavefunction_path: Path
    program: CalculationProgram
    label: str = ""
    pairing_reason: str = "matching base name"
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", Path(self.output_path).expanduser().resolve())
        object.__setattr__(
            self,
            "wavefunction_path",
            Path(self.wavefunction_path).expanduser().resolve(),
        )
        object.__setattr__(self, "program", _coerce_program(self.program))
        if not self.label:
            object.__setattr__(self, "label", _file_base(self.wavefunction_path))


@dataclass(frozen=True, slots=True)
class OrbitalDataset(_DictMixin):
    """Authoritative, normalized orbital data for one calculation."""

    wavefunction_path: Path
    program: CalculationProgram
    wavefunction_format: str
    wavefunction_type: WavefunctionType
    nbasis: int
    orbitals: tuple[Orbital, ...]
    atoms: tuple[Atom, ...]
    alpha_electrons: int | None
    beta_electrons: int | None
    charge: int | None = None
    multiplicity: int | None = None
    title: str = ""
    method: str = ""
    basis: str = ""
    route_or_keywords: str = ""
    orbital_set_kind: str = "canonical"
    fractional_occupations: bool = False
    warnings: tuple[str, ...] = ()
    output_metadata: OutputMetadata | None = None
    pair_validation: PairValidation | None = None

    @property
    def geometry_fingerprint(self) -> str:
        """Return an orientation-independent geometry fingerprint."""

        return geometry_fingerprint(self.atoms)

    @property
    def is_unrestricted(self) -> bool:
        """Whether alpha and beta have independent orbital sets."""

        return self.wavefunction_type is WavefunctionType.UNRESTRICTED

    @property
    def supports_frontier_labels(self) -> bool:
        """Whether ordinary HOMO/LUMO labels are scientifically unambiguous."""

        return self.orbital_set_kind == "canonical" and not self.fractional_occupations

    def available_spins(self) -> tuple[SpinChannel, ...]:
        """Return channels that can be independently selected and rendered."""

        if self.is_unrestricted:
            return (SpinChannel.ALPHA, SpinChannel.BETA)
        return (SpinChannel.SPATIAL,)

    def orbitals_for_spin(self, spin: SpinChannel | str) -> tuple[Orbital, ...]:
        """Return all orbitals in one normalized channel."""

        normalized = _coerce_spin(spin)
        return tuple(orbital for orbital in self.orbitals if orbital.spin is normalized)

    def homo_index(self, spin: SpinChannel | str | None = None) -> int:
        """Return the one-based channel index of the highest occupied orbital."""

        normalized = _default_dataset_spin(self, spin)
        occupied = [
            orbital.channel_index
            for orbital in self.orbitals_for_spin(normalized)
            if orbital.occupation > 1.0e-7
        ]
        if not occupied:
            raise OrbitalSelectionError(f"{normalized.value} 通道没有可识别的占据轨道。")
        return max(occupied)

    def lumo_index(self, spin: SpinChannel | str | None = None) -> int:
        """Return the first available orbital after the HOMO in one channel."""

        normalized = _default_dataset_spin(self, spin)
        homo = self.homo_index(normalized)
        virtual = [
            orbital.channel_index
            for orbital in self.orbitals_for_spin(normalized)
            if orbital.channel_index > homo
        ]
        if not virtual:
            raise OrbitalSelectionError(f"{normalized.value} 通道没有可用的 LUMO。")
        return min(virtual)


@dataclass(frozen=True, slots=True)
class OrbitalRef(_DictMixin):
    """A resolved orbital selection ready for Multiwfn and the renderer."""

    spin: SpinChannel
    channel_index: int
    global_index: int
    label: str
    occupation: float
    energy_hartree: float
    energy_ev: float
    symmetry: str = ""
    is_homo: bool = False
    is_lumo: bool = False

    @property
    def index(self) -> int:
        """Compatibility alias for the one-based channel-local index."""

        return self.channel_index

    @property
    def multiwfn_index(self) -> int:
        """Compatibility alias for the positive Multiwfn global index."""

        return self.global_index


@dataclass(frozen=True, slots=True)
class SelectionPreset(_DictMixin):
    """One friendly selection option suitable for a combo box or card."""

    id: str
    label: str
    expression: str
    description: str
    default_spin_mode: str = "auto"
    enabled: bool = True


_ELEMENT_SYMBOLS = (
    "",
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca",
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr",
    "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn",
    "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd",
    "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb",
    "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi", "Po", "At", "Rn", "Fr", "Ra", "Ac", "Th",
    "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm",
    "Md", "No", "Lr", "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds",
    "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
)
_ATOMIC_NUMBERS = {symbol.casefold(): index for index, symbol in enumerate(_ELEMENT_SYMBOLS) if symbol}


def _symbol_for_atomic_number(atomic_number: int) -> str:
    if atomic_number == 0:
        # Gaussian uses atomic number 0 for ghost/Bq centers.  Their ordered
        # coordinates still matter when proving that two companion files match.
        return "Bq"
    if not 0 < atomic_number < len(_ELEMENT_SYMBOLS):
        raise OrbitalParseError(f"不支持的原子序数：{atomic_number}")
    return _ELEMENT_SYMBOLS[atomic_number]


def _normalize_symbol(raw: str) -> str:
    match = re.match(r"\s*([A-Za-z]{1,3})", raw)
    if not match:
        raise OrbitalParseError(f"无法识别元素符号：{raw!r}")
    letters = match.group(1)
    symbol = letters[0].upper() + letters[1:].lower()
    if symbol.casefold() in {"x", "bq", "gh", "q"}:
        return "Bq"
    if symbol.casefold() not in _ATOMIC_NUMBERS:
        raise OrbitalParseError(f"无法识别元素符号：{raw!r}")
    return symbol


def _atomic_number_for_symbol(symbol: str) -> int:
    """Return an atomic number, retaining ghost centers as zero."""

    return 0 if symbol == "Bq" else _ATOMIC_NUMBERS[symbol.casefold()]


def _float(raw: str) -> float:
    try:
        value = float(raw.replace("D", "E").replace("d", "e"))
    except ValueError as exc:
        raise OrbitalParseError(f"无法解析浮点数：{raw!r}") from exc
    if not math.isfinite(value):
        raise OrbitalParseError(f"轨道文件包含非有限数值：{raw!r}")
    return value


def _near_integer(value: float, tolerance: float = 1.0e-5) -> int | None:
    nearest = round(value)
    return int(nearest) if abs(value - nearest) <= tolerance else None


def _coerce_program(value: CalculationProgram | str) -> CalculationProgram:
    if isinstance(value, CalculationProgram):
        return value
    normalized = str(value).strip().casefold()
    aliases = {
        "gaussian": CalculationProgram.GAUSSIAN,
        "g09": CalculationProgram.GAUSSIAN,
        "g16": CalculationProgram.GAUSSIAN,
        "orca": CalculationProgram.ORCA,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise UnsupportedFileError(f"不支持的量化程序：{value}") from exc


def _coerce_spin(value: SpinChannel | str) -> SpinChannel:
    if isinstance(value, SpinChannel):
        return value
    normalized = str(value).strip().casefold().replace("spin-", "")
    aliases = {
        "spatial": SpinChannel.SPATIAL,
        "restricted": SpinChannel.SPATIAL,
        "r": SpinChannel.SPATIAL,
        "alpha": SpinChannel.ALPHA,
        "a": SpinChannel.ALPHA,
        "α": SpinChannel.ALPHA,
        "up": SpinChannel.ALPHA,
        "beta": SpinChannel.BETA,
        "b": SpinChannel.BETA,
        "β": SpinChannel.BETA,
        "down": SpinChannel.BETA,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise OrbitalSelectionError(f"未知自旋通道：{value}") from exc


def _default_dataset_spin(
    dataset: OrbitalDataset,
    spin: SpinChannel | str | None,
) -> SpinChannel:
    if spin is None:
        if dataset.is_unrestricted:
            raise OrbitalSelectionError("非限制性波函数必须明确指定 alpha 或 beta 通道。")
        return SpinChannel.SPATIAL
    normalized = _coerce_spin(spin)
    if normalized not in dataset.available_spins():
        raise OrbitalSelectionError(
            f"{dataset.wavefunction_type.value} 波函数没有独立的 {normalized.value} 轨道集。"
        )
    return normalized


def geometry_fingerprint(atoms: Sequence[Atom], *, decimals: int = 4) -> str:
    """Return an orientation/translation-independent SHA-256 geometry key.

    Atom order is intentionally retained.  Pair validation treats a different
    atom order as a mismatch because orbital coefficients and VMD geometry must
    refer to the same ordered centers.
    """

    if not atoms:
        return ""
    components = [str(atom.atomic_number) for atom in atoms]
    for left in range(len(atoms)):
        for right in range(left + 1, len(atoms)):
            a = atoms[left]
            b = atoms[right]
            distance = math.sqrt(
                (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2
            )
            components.append(f"{distance:.{decimals}f}")
    return hashlib.sha256("|".join(components).encode("ascii")).hexdigest()


def _distance_matrix_rmsd(left: Sequence[Atom], right: Sequence[Atom]) -> float:
    if len(left) != len(right):
        return math.inf
    squared: list[float] = []
    for first in range(len(left)):
        for second in range(first + 1, len(left)):
            left_distance = math.dist(left[first].coordinates, left[second].coordinates)
            right_distance = math.dist(right[first].coordinates, right[second].coordinates)
            squared.append((left_distance - right_distance) ** 2)
    if not squared:
        return 0.0
    return math.sqrt(sum(squared) / len(squared))


def _fch_header(line: str) -> tuple[str, str, int | None, str] | None:
    if len(line) < 42:
        return None
    label = line[:40].strip()
    tail = line[40:].strip()
    match = re.match(r"^([IRCLH])(?:\s+N=\s*(\d+)|\s+(.+))$", tail)
    if not label or not match:
        return None
    return label, match.group(1), int(match.group(2)) if match.group(2) else None, match.group(3) or ""


def _read_fch_fields(path: Path) -> tuple[str, str, dict[str, Any], set[str]]:
    """Read only metadata arrays required by this workflow from an fch file."""

    wanted = {
        "Number of atoms",
        "Charge",
        "Multiplicity",
        "Number of electrons",
        "Number of alpha electrons",
        "Number of beta electrons",
        "Number of basis functions",
        "Number of independent functions",
        "Atomic numbers",
        "Nuclear charges",
        "Current cartesian coordinates",
        "IROHF",
        "Alpha Orbital Energies",
        "Beta Orbital Energies",
        "Orbital Occupation Numbers",
        "Alpha Orbital Occupation Numbers",
        "Beta Orbital Occupation Numbers",
        "Route",
        "Full Title",
    }
    result: dict[str, Any] = {}
    present: set[str] = set()
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OrbitalParseError(f"无法读取波函数文件：{path}") from exc
    with handle:
        first = handle.readline().rstrip("\r\n")
        second = handle.readline().rstrip("\r\n")
        while True:
            line = handle.readline()
            if not line:
                break
            header = _fch_header(line.rstrip("\r\n"))
            if header is None:
                continue
            label, data_type, count, scalar = header
            present.add(label)
            if count is None:
                if label not in wanted:
                    continue
                if data_type == "I":
                    try:
                        result[label] = int(scalar.split()[0])
                    except (ValueError, IndexError) as exc:
                        raise OrbitalParseError(f"fch 字段 {label!r} 无效。") from exc
                elif data_type == "R":
                    result[label] = _float(scalar.split()[0])
                elif data_type == "L":
                    result[label] = scalar.strip().upper().startswith("T")
                else:
                    result[label] = scalar.strip()
                continue

            if data_type in {"I", "R"}:
                values: list[int | float] = []
                consumed = 0
                while consumed < count:
                    data_line = handle.readline()
                    if not data_line:
                        raise OrbitalParseError(f"fch 数组 {label!r} 在文件末尾被截断。")
                    tokens = data_line.split()
                    consumed += len(tokens)
                    if label in wanted:
                        if data_type == "I":
                            try:
                                values.extend(int(token) for token in tokens)
                            except ValueError as exc:
                                raise OrbitalParseError(f"fch 整数数组 {label!r} 无效。") from exc
                        else:
                            values.extend(_float(token) for token in tokens)
                if consumed != count:
                    raise OrbitalParseError(f"fch 数组 {label!r} 的元素数与声明不一致。")
                if label in wanted:
                    result[label] = values
                continue

            per_line = 5 if data_type == "C" else 9 if data_type == "H" else 72
            line_count = max(1, math.ceil(count / per_line))
            raw_lines = []
            for _ in range(line_count):
                data_line = handle.readline()
                if not data_line:
                    raise OrbitalParseError(f"fch 字符数组 {label!r} 在文件末尾被截断。")
                if label in wanted:
                    raw_lines.append(data_line.rstrip("\r\n"))
            if label in wanted:
                result[label] = " ".join(part.strip() for part in raw_lines).strip()
    if not first and not second:
        raise OrbitalParseError(f"空的 fch/fchk 文件：{path}")
    return first.strip(), second.strip(), result, present


def _occupations_are_fractional(
    orbitals: Sequence[Orbital],
    wavefunction_type: WavefunctionType,
) -> bool:
    allowed = (
        (0.0, 1.0)
        if wavefunction_type is WavefunctionType.UNRESTRICTED
        else (0.0, 1.0, 2.0)
    )
    return any(min(abs(orbital.occupation - value) for value in allowed) > 1.0e-5 for orbital in orbitals)


def _parse_fch(path: Path) -> OrbitalDataset:
    title, method_line, data, present = _read_fch_fields(path)
    required = (
        "Number of atoms",
        "Number of alpha electrons",
        "Number of beta electrons",
        "Number of basis functions",
        "Atomic numbers",
        "Current cartesian coordinates",
        "Alpha Orbital Energies",
    )
    missing = [name for name in required if name not in data]
    if missing:
        raise OrbitalParseError(
            f"Gaussian fch/fchk 缺少必要字段：{', '.join(missing)}"
        )

    natoms = int(data["Number of atoms"])
    atomic_numbers = tuple(max(0, int(value)) for value in data["Atomic numbers"])
    coordinates = tuple(float(value) for value in data["Current cartesian coordinates"])
    if len(atomic_numbers) != natoms or len(coordinates) != natoms * 3:
        raise OrbitalParseError("Gaussian fch/fchk 的原子数与坐标数组不一致。")
    atoms = tuple(
        Atom(
            atomic_number=atomic_numbers[index],
            symbol=_symbol_for_atomic_number(atomic_numbers[index]),
            x=coordinates[index * 3] * BOHR_TO_ANGSTROM,
            y=coordinates[index * 3 + 1] * BOHR_TO_ANGSTROM,
            z=coordinates[index * 3 + 2] * BOHR_TO_ANGSTROM,
        )
        for index in range(natoms)
    )

    alpha_count = int(data["Number of alpha electrons"])
    beta_count = int(data["Number of beta electrons"])
    nbasis = int(data["Number of basis functions"])
    alpha_energies = tuple(float(value) for value in data["Alpha Orbital Energies"])
    beta_energies = tuple(float(value) for value in data.get("Beta Orbital Energies", ()))
    method_parts = method_line.split()
    method = method_parts[1] if len(method_parts) > 1 else ""
    basis = method_parts[2] if len(method_parts) > 2 else ""
    upper_method = method.upper()
    is_ro = bool(data.get("IROHF", 0)) or upper_method.startswith("RO")
    if beta_energies:
        wavefunction_type = WavefunctionType.UNRESTRICTED
    elif is_ro or alpha_count != beta_count:
        wavefunction_type = WavefunctionType.RESTRICTED_OPEN_SHELL
    else:
        wavefunction_type = WavefunctionType.RESTRICTED

    if nbasis <= 0 or not alpha_energies:
        raise OrbitalParseError("Gaussian fch/fchk 不包含可用的分子轨道。")
    if len(alpha_energies) > nbasis or (beta_energies and len(beta_energies) > nbasis):
        raise OrbitalParseError("Gaussian fch/fchk 的轨道数超过基函数数。")
    if wavefunction_type is WavefunctionType.UNRESTRICTED and not beta_energies:
        raise OrbitalParseError("非限制性 Gaussian 波函数缺少 beta 轨道能量。")

    alpha_occ_raw = data.get("Alpha Orbital Occupation Numbers")
    beta_occ_raw = data.get("Beta Orbital Occupation Numbers")
    spatial_occ_raw = data.get("Orbital Occupation Numbers")
    orbitals: list[Orbital] = []
    if wavefunction_type is WavefunctionType.UNRESTRICTED:
        for index, energy in enumerate(alpha_energies, 1):
            occupation = (
                float(alpha_occ_raw[index - 1])
                if alpha_occ_raw and index <= len(alpha_occ_raw)
                else (1.0 if index <= alpha_count else 0.0)
            )
            orbitals.append(Orbital(SpinChannel.ALPHA, index, index, energy, occupation))
        alpha_mo_count = len(alpha_energies)
        for index, energy in enumerate(beta_energies, 1):
            occupation = (
                float(beta_occ_raw[index - 1])
                if beta_occ_raw and index <= len(beta_occ_raw)
                else (1.0 if index <= beta_count else 0.0)
            )
            orbitals.append(
                Orbital(
                    SpinChannel.BETA,
                    index,
                    alpha_mo_count + index,
                    energy,
                    occupation,
                )
            )
    else:
        for index, energy in enumerate(alpha_energies, 1):
            if spatial_occ_raw and index <= len(spatial_occ_raw):
                occupation = float(spatial_occ_raw[index - 1])
            elif index <= beta_count:
                occupation = 2.0
            elif index <= alpha_count:
                occupation = 1.0
            else:
                occupation = 0.0
            orbitals.append(
                Orbital(SpinChannel.SPATIAL, index, index, energy, occupation)
            )

    route = str(data.get("Route", ""))
    full_title = str(data.get("Full Title", "")).strip()
    title = full_title or title
    descriptor = " ".join((title, route, method)).casefold()
    special = bool(re.search(r"\b(uno|uco|qro|natural orbital|localized orbital|nbo)\b", descriptor))
    fractional = _occupations_are_fractional(orbitals, wavefunction_type)
    warnings: list[str] = []
    if "Alpha MO coefficients" not in present:
        warnings.append("fch/fchk 未声明 Alpha MO coefficients，Multiwfn 可能无法生成轨道 Cube。")
    if wavefunction_type is WavefunctionType.UNRESTRICTED and "Beta MO coefficients" not in present:
        warnings.append("非限制性 fch/fchk 未声明 Beta MO coefficients。")
    if special:
        warnings.append("检测到非标准轨道集合；请优先使用绝对轨道编号。")
    if fractional:
        warnings.append("检测到分数占据；普通 HOMO/LUMO 标签已禁用。")

    return OrbitalDataset(
        wavefunction_path=path,
        program=CalculationProgram.GAUSSIAN,
        wavefunction_format="gaussian_fch",
        wavefunction_type=wavefunction_type,
        nbasis=nbasis,
        orbitals=tuple(orbitals),
        atoms=atoms,
        alpha_electrons=alpha_count,
        beta_electrons=beta_count,
        charge=int(data["Charge"]) if "Charge" in data else None,
        multiplicity=int(data["Multiplicity"]) if "Multiplicity" in data else None,
        title=title,
        method=method,
        basis=basis,
        route_or_keywords=route,
        orbital_set_kind="special" if special else "canonical",
        fractional_occupations=fractional,
        warnings=tuple(warnings),
    )


def _flush_molden_orbital(
    raw: dict[str, Any] | None,
    target: list[dict[str, Any]],
) -> None:
    if raw is None or not raw:
        return
    if "energy" not in raw or "occupation" not in raw:
        raise OrbitalParseError("Molden 的 [MO] 轨道块缺少 Ene 或 Occup。")
    target.append(raw)


def _parse_molden(path: Path) -> OrbitalDataset:
    atoms: list[Atom] = []
    raw_orbitals: list[dict[str, Any]] = []
    current_orbital: dict[str, Any] | None = None
    section = ""
    atom_factor = 1.0
    title_lines: list[str] = []
    max_basis_index = 0
    saw_molden_header = False
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OrbitalParseError(f"无法读取 Molden 文件：{path}") from exc
    with handle:
        for raw_line in handle:
            line = raw_line.strip()
            section_match = re.match(r"^\[([^]]+)\]\s*(.*)$", line)
            if section_match:
                new_section = section_match.group(1).strip().casefold()
                suffix = section_match.group(2).strip().casefold()
                if section == "mo" and new_section != "mo":
                    _flush_molden_orbital(current_orbital, raw_orbitals)
                    current_orbital = None
                section = new_section
                if section == "molden format":
                    saw_molden_header = True
                if section == "atoms":
                    atom_factor = BOHR_TO_ANGSTROM if "au" in suffix else 1.0
                continue
            if not line:
                continue
            if section == "title":
                title_lines.append(line)
                continue
            if section == "atoms":
                tokens = line.split()
                if len(tokens) < 6:
                    continue
                try:
                    atomic_number = int(float(tokens[2]))
                    x, y, z = (_float(token) * atom_factor for token in tokens[-3:])
                except (ValueError, OrbitalParseError):
                    continue
                symbol = "Bq" if atomic_number == 0 else _normalize_symbol(tokens[0])
                if atomic_number < 0:
                    atomic_number = _atomic_number_for_symbol(symbol)
                atoms.append(Atom(atomic_number, symbol, x, y, z))
                continue
            if section != "mo":
                continue
            item_match = re.match(r"^(Sym|Ene|Spin|Occup)\s*=\s*(.*)$", line, re.IGNORECASE)
            if item_match:
                key = item_match.group(1).casefold()
                value = item_match.group(2).strip()
                if key == "sym" and current_orbital and (
                    "energy" in current_orbital or "coefficients_started" in current_orbital
                ):
                    _flush_molden_orbital(current_orbital, raw_orbitals)
                    current_orbital = None
                if current_orbital is None:
                    current_orbital = {}
                if key == "sym":
                    current_orbital["symmetry"] = value
                elif key == "ene":
                    current_orbital["energy"] = _float(value)
                elif key == "occup":
                    current_orbital["occupation"] = _float(value)
                else:
                    current_orbital["spin"] = value.casefold()
                continue
            coefficient = re.match(r"^(\d+)\s+([-+0-9.EeDd]+)(?:\s+.*)?$", line)
            if coefficient:
                if current_orbital is None:
                    raise OrbitalParseError("Molden [MO] 中系数出现在轨道元数据之前。")
                current_orbital["coefficients_started"] = True
                max_basis_index = max(max_basis_index, int(coefficient.group(1)))
        if section == "mo":
            _flush_molden_orbital(current_orbital, raw_orbitals)

    if not saw_molden_header:
        raise OrbitalParseError("文件不含 [Molden Format] 标头。")
    if not atoms:
        raise OrbitalParseError("Molden 文件不含可用的 [Atoms] 坐标。")
    if not raw_orbitals or max_basis_index <= 0:
        raise OrbitalParseError("Molden 文件不含带基函数系数的 [MO] 数据。")

    has_beta = any(str(item.get("spin", "")).startswith("beta") for item in raw_orbitals)
    if has_beta:
        wavefunction_type = WavefunctionType.UNRESTRICTED
    elif any(abs(float(item["occupation"]) - 1.0) <= 1.0e-5 for item in raw_orbitals):
        wavefunction_type = WavefunctionType.RESTRICTED_OPEN_SHELL
    else:
        wavefunction_type = WavefunctionType.RESTRICTED

    alpha_raw = [item for item in raw_orbitals if not str(item.get("spin", "alpha")).startswith("beta")]
    beta_raw = [item for item in raw_orbitals if str(item.get("spin", "")).startswith("beta")]
    if wavefunction_type is WavefunctionType.UNRESTRICTED and (not alpha_raw or not beta_raw):
        raise OrbitalParseError("非限制性 Molden 文件必须同时包含 Alpha 和 Beta 轨道。")
    orbitals: list[Orbital] = []
    if wavefunction_type is WavefunctionType.UNRESTRICTED:
        for index, item in enumerate(alpha_raw, 1):
            orbitals.append(
                Orbital(
                    SpinChannel.ALPHA,
                    index,
                    index,
                    float(item["energy"]),
                    float(item["occupation"]),
                    str(item.get("symmetry", "")),
                )
            )
        alpha_mo_count = len(alpha_raw)
        for index, item in enumerate(beta_raw, 1):
            orbitals.append(
                Orbital(
                    SpinChannel.BETA,
                    index,
                    alpha_mo_count + index,
                    float(item["energy"]),
                    float(item["occupation"]),
                    str(item.get("symmetry", "")),
                )
            )
    else:
        for index, item in enumerate(alpha_raw, 1):
            orbitals.append(
                Orbital(
                    SpinChannel.SPATIAL,
                    index,
                    index,
                    float(item["energy"]),
                    float(item["occupation"]),
                    str(item.get("symmetry", "")),
                )
            )

    title = " ".join(title_lines).strip()
    descriptor = title.casefold()
    special = bool(re.search(r"\b(uno|uco|qro|natural orbital|localized orbital|nbo)\b", descriptor))
    fractional = _occupations_are_fractional(orbitals, wavefunction_type)
    if wavefunction_type is WavefunctionType.UNRESTRICTED:
        alpha_value = sum(orbital.occupation for orbital in orbitals if orbital.spin is SpinChannel.ALPHA)
        beta_value = sum(orbital.occupation for orbital in orbitals if orbital.spin is SpinChannel.BETA)
    else:
        double_count = sum(1 for orbital in orbitals if abs(orbital.occupation - 2.0) <= 1.0e-5)
        single_count = sum(1 for orbital in orbitals if abs(orbital.occupation - 1.0) <= 1.0e-5)
        alpha_value = float(double_count + single_count)
        beta_value = float(double_count)
        if fractional:
            alpha_value = beta_value = sum(orbital.occupation for orbital in orbitals) / 2.0
    alpha_count = _near_integer(alpha_value)
    beta_count = _near_integer(beta_value)
    multiplicity = (
        abs(alpha_count - beta_count) + 1
        if alpha_count is not None and beta_count is not None
        else None
    )
    warnings: list[str] = []
    if "orca" not in title.casefold():
        warnings.append("Molden 标题未明确标识 ORCA；请通过配对输出进一步核验来源。")
    if special:
        warnings.append("检测到 UNO/UCO/QRO、自然轨道或局域轨道集合；请使用绝对编号。")
    if fractional:
        warnings.append("检测到分数占据；普通 HOMO/LUMO 标签已禁用。")

    return OrbitalDataset(
        wavefunction_path=path,
        program=CalculationProgram.ORCA,
        wavefunction_format="orca_molden",
        wavefunction_type=wavefunction_type,
        nbasis=max_basis_index,
        orbitals=tuple(orbitals),
        atoms=tuple(atoms),
        alpha_electrons=alpha_count,
        beta_electrons=beta_count,
        charge=None,
        multiplicity=multiplicity,
        title=title,
        orbital_set_kind="special" if special else "canonical",
        fractional_occupations=fractional,
        warnings=tuple(warnings),
    )


def parse_wavefunction_file(path: str | Path) -> OrbitalDataset:
    """Parse authoritative orbital metadata from Gaussian fch or ORCA Molden.

    The returned indices are normalized to one-based channel indices plus the
    positive global index expected by Multiwfn.  The function never consults an
    output file and therefore cannot accidentally inherit ORCA's zero-based
    printed numbering.
    """

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise OrbitalParseError(f"波函数文件不存在：{resolved}")
    name = resolved.name.casefold()
    if name.endswith((".fch", ".fchk")):
        return _parse_fch(resolved)
    if name.endswith((".molden", ".molden.input")):
        return _parse_molden(resolved)
    try:
        prefix = resolved.read_text(encoding="utf-8", errors="replace")[:256]
    except OSError as exc:
        raise OrbitalParseError(f"无法读取波函数文件：{resolved}") from exc
    if "[molden format]" in prefix.casefold():
        return _parse_molden(resolved)
    raise UnsupportedFileError(
        "仅支持 Gaussian .fch/.fchk 与 ORCA .molden/.molden.input。"
    )


def _read_output_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OrbitalParseError(f"无法读取输出文件：{path}") from exc


def _detect_output_program(path: Path, text: str | None = None) -> CalculationProgram:
    source = text if text is not None else _read_output_text(path)
    folded = source.casefold()
    gaussian_markers = (
        "gaussian, inc.",
        "entering gaussian system",
        "normal termination of gaussian",
        "error termination via",
    )
    orca_markers = (
        "orca terminated normally",
        "o   r   c   a",
        "this program is part of the orca",
        "orca error termination",
    )
    gaussian_score = sum(marker in folded for marker in gaussian_markers)
    orca_score = sum(marker in folded for marker in orca_markers)
    if gaussian_score > orca_score:
        return CalculationProgram.GAUSSIAN
    if orca_score > gaussian_score:
        return CalculationProgram.ORCA
    raise OrbitalParseError(f"无法判断输出文件属于 Gaussian 还是 ORCA：{path}")


def _last_status(text: str, success_patterns: Sequence[str], failure_patterns: Sequence[str]) -> tuple[bool, str]:
    folded = text.casefold()
    successes = [(folded.rfind(pattern.casefold()), pattern) for pattern in success_patterns]
    failures = [(folded.rfind(pattern.casefold()), pattern) for pattern in failure_patterns]
    success_position, success_text = max(successes, default=(-1, ""))
    failure_position, failure_text = max(failures, default=(-1, ""))
    if success_position < 0 and failure_position < 0:
        return False, "未找到正常终止标记"
    if failure_position > success_position:
        return False, failure_text
    return success_position >= 0, success_text


def _last_int_match(patterns: Sequence[str], text: str) -> int | None:
    latest_position = -1
    latest_value: int | None = None
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            if match.start() >= latest_position:
                latest_position = match.start()
                latest_value = int(round(_float(match.group(1))))
    return latest_value


def _parse_gaussian_geometry(lines: Sequence[str]) -> tuple[Atom, ...]:
    latest: tuple[Atom, ...] = ()
    for index, line in enumerate(lines):
        if not re.search(r"(?:Standard|Input) orientation\s*:", line, re.IGNORECASE):
            continue
        cursor = index + 1
        dash_count = 0
        while cursor < len(lines):
            if re.match(r"^\s*-{5,}\s*$", lines[cursor]):
                dash_count += 1
                cursor += 1
                if dash_count == 2:
                    break
                continue
            cursor += 1
        atoms: list[Atom] = []
        while cursor < len(lines):
            if re.match(r"^\s*-{5,}\s*$", lines[cursor]):
                break
            match = re.match(
                r"^\s*\d+\s+(\d+)\s+\d+\s+"
                r"([-+0-9.EeDd]+)\s+([-+0-9.EeDd]+)\s+([-+0-9.EeDd]+)",
                lines[cursor],
            )
            if match:
                atomic_number = max(0, int(match.group(1)))
                atoms.append(
                    Atom(
                        atomic_number,
                        _symbol_for_atomic_number(atomic_number),
                        _float(match.group(2)),
                        _float(match.group(3)),
                        _float(match.group(4)),
                    )
                )
            cursor += 1
        if atoms:
            latest = tuple(atoms)
    return latest


def _parse_gaussian_orbitals(text: str) -> tuple[OutputOrbitalEnergy, ...]:
    last_scf = text.casefold().rfind("scf done:")
    source = text[last_scf:] if last_scf >= 0 else text
    line_pattern = re.compile(
        r"^\s*(?:(Alpha|Beta)\s+)?(occ|virt)\.\s+eigenvalues\s+--\s*(.+)$",
        re.IGNORECASE | re.MULTILINE,
    )
    channels: dict[SpinChannel, list[tuple[float, float]]] = {}
    seen_virtual: set[SpinChannel] = set()
    for match in line_pattern.finditer(source):
        spin_text = (match.group(1) or "").casefold()
        spin = (
            SpinChannel.ALPHA
            if spin_text == "alpha"
            else SpinChannel.BETA
            if spin_text == "beta"
            else SpinChannel.SPATIAL
        )
        kind = match.group(2).casefold()
        if kind == "occ" and spin in seen_virtual:
            channels[spin] = []
            seen_virtual.discard(spin)
        try:
            values = [_float(token) for token in match.group(3).split()]
        except OrbitalParseError:
            # Asterisks are occasionally printed for overflowing deep-core
            # energies.  Dropping only that token would shift every later
            # index, so disable this optional cross-check as a whole instead.
            return ()
        occupation = 2.0 if spin is SpinChannel.SPATIAL and kind == "occ" else 1.0 if kind == "occ" else 0.0
        channels.setdefault(spin, []).extend((value, occupation) for value in values)
        if kind == "virt":
            seen_virtual.add(spin)
    result: list[OutputOrbitalEnergy] = []
    for spin in (SpinChannel.SPATIAL, SpinChannel.ALPHA, SpinChannel.BETA):
        for index, (energy, occupation) in enumerate(channels.get(spin, ()), 1):
            result.append(OutputOrbitalEnergy(spin, index, 1, index, energy, occupation))
    return tuple(result)


def _gaussian_route(text: str) -> str:
    routes = []
    lines = text.splitlines()
    cursor = 0
    while cursor < len(lines):
        if re.match(r"^\s*#", lines[cursor]):
            parts = [lines[cursor].strip()]
            cursor += 1
            while cursor < len(lines) and not re.match(r"^\s*-{5,}\s*$", lines[cursor]):
                if lines[cursor].strip():
                    parts.append(lines[cursor].strip())
                cursor += 1
            routes.append(" ".join(parts))
        cursor += 1
    return routes[-1] if routes else ""


def _parse_gaussian_output(path: Path, text: str) -> OutputMetadata:
    normal, detail = _last_status(
        text,
        ("Normal termination of Gaussian",),
        ("Error termination via", "Convergence failure -- run terminated"),
    )
    folded = text.casefold()
    last_done = folded.rfind("scf done:")
    last_failure = max(
        folded.rfind("convergence failure"),
        folded.rfind("scf has not converged"),
    )
    scf_converged = None if last_done < 0 and last_failure < 0 else last_done > last_failure
    charge = _last_int_match((r"Charge\s*=\s*(-?\d+)\s+Multiplicity",), text)
    multiplicity = _last_int_match((r"Multiplicity\s*=\s*(\d+)",), text)
    electron_matches = list(
        re.finditer(r"(\d+)\s+alpha electrons\s+(\d+)\s+beta electrons", text, re.IGNORECASE)
    )
    alpha = int(electron_matches[-1].group(1)) if electron_matches else None
    beta = int(electron_matches[-1].group(2)) if electron_matches else None
    route = _gaussian_route(text)
    method = ""
    basis = ""
    method_match = re.search(r"#\S*\s+([^\s/]+)/([^\s]+)", route)
    if method_match:
        method, basis = method_match.groups()
    warnings = []
    if not route:
        warnings.append("Gaussian 输出中未识别到 route section。")
    return OutputMetadata(
        path=path,
        program=CalculationProgram.GAUSSIAN,
        normal_termination=normal,
        scf_converged=scf_converged,
        charge=charge,
        multiplicity=multiplicity,
        alpha_electrons=alpha,
        beta_electrons=beta,
        atoms=_parse_gaussian_geometry(text.splitlines()),
        orbital_energies=_parse_gaussian_orbitals(text),
        route_or_keywords=route,
        method=method,
        basis=basis,
        termination_detail=detail,
        warnings=tuple(warnings),
    )


def _parse_orca_geometry(lines: Sequence[str]) -> tuple[Atom, ...]:
    latest: tuple[Atom, ...] = ()
    for index, line in enumerate(lines):
        if "CARTESIAN COORDINATES (ANGSTROEM)" not in line.upper():
            continue
        atoms: list[Atom] = []
        cursor = index + 1
        while cursor < len(lines) and (
            not lines[cursor].strip() or re.match(r"^\s*-{5,}\s*$", lines[cursor])
        ):
            cursor += 1
        while cursor < len(lines):
            match = re.match(
                r"^\s*([A-Za-z]{1,3})\s+"
                r"([-+0-9.EeDd]+)\s+([-+0-9.EeDd]+)\s+([-+0-9.EeDd]+)\s*$",
                lines[cursor],
            )
            if not match:
                break
            symbol = _normalize_symbol(match.group(1))
            atoms.append(
                Atom(
                    _atomic_number_for_symbol(symbol),
                    symbol,
                    _float(match.group(2)),
                    _float(match.group(3)),
                    _float(match.group(4)),
                )
            )
            cursor += 1
        if atoms:
            latest = tuple(atoms)
    return latest


def _parse_orca_orbital_block(lines: Sequence[str], start: int) -> tuple[OutputOrbitalEnergy, ...]:
    result: list[OutputOrbitalEnergy] = []
    spin = SpinChannel.SPATIAL
    rows_started = False
    gap = 0
    for line in lines[start + 1 : start + 5000]:
        upper = line.upper()
        if "SPIN UP ORBITALS" in upper:
            spin = SpinChannel.ALPHA
            gap = 0
            continue
        if "SPIN DOWN ORBITALS" in upper:
            spin = SpinChannel.BETA
            gap = 0
            continue
        match = re.match(
            r"^\s*(\d+)\s+([-+0-9.EeDd]+)\s+([-+0-9.EeDd]+)\s+([-+0-9.EeDd]+)\s*$",
            line,
        )
        if match:
            rows_started = True
            gap = 0
            source_index = int(match.group(1))
            result.append(
                OutputOrbitalEnergy(
                    spin=spin,
                    source_index=source_index,
                    source_index_base=0,
                    channel_index=source_index + 1,
                    occupation=_float(match.group(2)),
                    energy_hartree=_float(match.group(3)),
                )
            )
            continue
        if rows_started:
            gap += 1
            if gap > 16 or re.search(
                r"MULLIKEN|LOEWDIN|TOTAL SCF ENERGY|DIPOLE MOMENT|TIMINGS",
                upper,
            ):
                break
    return tuple(result)


def _parse_orca_orbitals(lines: Sequence[str]) -> tuple[OutputOrbitalEnergy, ...]:
    latest: tuple[OutputOrbitalEnergy, ...] = ()
    for index, line in enumerate(lines):
        if line.strip().upper() == "ORBITAL ENERGIES":
            parsed = _parse_orca_orbital_block(lines, index)
            if parsed:
                latest = parsed
    return latest


def _orca_keywords(text: str) -> str:
    keywords = []
    for match in re.finditer(r"^\s*\|\s*\d+>\s*!\s*(.+)$", text, re.MULTILINE):
        keywords.append(match.group(1).strip())
    if keywords:
        return keywords[-1]
    direct = re.findall(r"^\s*!\s*(.+)$", text, re.MULTILINE)
    return direct[-1].strip() if direct else ""


def _parse_orca_output(path: Path, text: str) -> OutputMetadata:
    normal, detail = _last_status(
        text,
        ("ORCA TERMINATED NORMALLY",),
        ("ORCA finished by error termination", "ORCA ERROR TERMINATION", "ABORTING THE RUN"),
    )
    folded = text.casefold()
    last_converged = folded.rfind("scf converged")
    last_not_converged = max(
        folded.rfind("scf not converged"),
        folded.rfind("scf failed to converge"),
    )
    scf_converged = (
        None
        if last_converged < 0 and last_not_converged < 0
        else last_converged > last_not_converged
    )
    xyz_matches = list(
        re.finditer(
            r"(?:\|\s*\d+>\s*)?\*\s*xyz(?:file)?\s+(-?\d+)\s+(\d+)",
            text,
            re.IGNORECASE,
        )
    )
    charge = int(xyz_matches[-1].group(1)) if xyz_matches else None
    multiplicity = int(xyz_matches[-1].group(2)) if xyz_matches else None
    if multiplicity is None:
        multiplicity = _last_int_match((r"^\s*Multiplicity\s*:\s*(\d+)",), text)
    if charge is None:
        charge = _last_int_match(
            (
                r"^\s*Total Charge\s*(?:\.\.\.|:)?\s*(-?\d+)\s*$",
                r"^\s*Charge\s*:\s*(-?\d+)\s*$",
            ),
            text,
        )
    alpha = _last_int_match((r"N\(Alpha\)\s*:\s*([-+0-9.EeDd]+)",), text)
    beta = _last_int_match((r"N\(Beta\)\s*:\s*([-+0-9.EeDd]+)",), text)
    lines = text.splitlines()
    keywords = _orca_keywords(text)
    warnings = []
    if not keywords:
        warnings.append("ORCA 输出中未识别到简单输入关键字行。")
    return OutputMetadata(
        path=path,
        program=CalculationProgram.ORCA,
        normal_termination=normal,
        scf_converged=scf_converged,
        charge=charge,
        multiplicity=multiplicity,
        alpha_electrons=alpha,
        beta_electrons=beta,
        atoms=_parse_orca_geometry(lines),
        orbital_energies=_parse_orca_orbitals(lines),
        route_or_keywords=keywords,
        termination_detail=detail,
        warnings=tuple(warnings),
    )


def parse_output_file(
    path: str | Path,
    program: CalculationProgram | str | None = None,
) -> OutputMetadata:
    """Parse health, final geometry, and cross-check data from an output file.

    ORCA table numbers are preserved in ``source_index`` with base ``0`` and
    normalized to one-based ``channel_index``.  They never override Molden's
    authoritative ordering.
    """

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise OrbitalParseError(f"输出文件不存在：{resolved}")
    text = _read_output_text(resolved)
    detected = _detect_output_program(resolved, text)
    requested = _coerce_program(program) if program is not None else detected
    if requested is not detected:
        raise PairingError(
            f"输出文件被识别为 {detected.value}，但波函数文件属于 {requested.value}。"
        )
    if detected is CalculationProgram.GAUSSIAN:
        return _parse_gaussian_output(resolved, text)
    return _parse_orca_output(resolved, text)


def _wavefunction_orbital_map(
    dataset: OrbitalDataset,
) -> dict[tuple[SpinChannel, int], Orbital]:
    return {(orbital.spin, orbital.channel_index): orbital for orbital in dataset.orbitals}


def _output_orbital_map(
    dataset: OrbitalDataset,
    output: OutputMetadata,
) -> dict[tuple[SpinChannel, int], OutputOrbitalEnergy]:
    result: dict[tuple[SpinChannel, int], OutputOrbitalEnergy] = {}
    for orbital in output.orbital_energies:
        spin = orbital.spin
        if not dataset.is_unrestricted and spin in {SpinChannel.ALPHA, SpinChannel.BETA}:
            spin = SpinChannel.SPATIAL
        result[(spin, orbital.channel_index)] = orbital
    return result


def validate_input_pair(
    dataset: OrbitalDataset,
    output: OutputMetadata,
    *,
    geometry_tolerance_angstrom: float = 1.0e-3,
    orbital_energy_tolerance_hartree: float = 1.0e-4,
) -> PairValidation:
    """Validate that an output and wavefunction file describe the same job.

    Geometry comparison uses ordered interatomic distances and is independent
    of translation and rotation.  Orbital energies are compared only where the
    output printed a corresponding value; the wavefunction file remains the
    authority even when validation succeeds.
    """

    checks: list[ValidationCheck] = []
    checks.append(
        ValidationCheck(
            "program",
            ValidationLevel.OK if dataset.program is output.program else ValidationLevel.ERROR,
            (
                f"程序类型一致：{dataset.program.value}。"
                if dataset.program is output.program
                else f"程序类型不一致：波函数为 {dataset.program.value}，输出为 {output.program.value}。"
            ),
            dataset.program.value == output.program.value,
        )
    )
    checks.append(
        ValidationCheck(
            "normal_termination",
            ValidationLevel.OK if output.normal_termination else ValidationLevel.ERROR,
            "计算正常结束。" if output.normal_termination else f"输出未正常结束：{output.termination_detail}",
            output.normal_termination,
        )
    )
    if output.scf_converged is None:
        checks.append(
            ValidationCheck("scf_convergence", ValidationLevel.WARNING, "输出中未找到明确的 SCF 收敛标记。")
        )
    else:
        checks.append(
            ValidationCheck(
                "scf_convergence",
                ValidationLevel.OK if output.scf_converged else ValidationLevel.ERROR,
                "SCF 已收敛。" if output.scf_converged else "SCF 未收敛。",
                output.scf_converged,
            )
        )

    geometry_rmsd: float | None = None
    if not output.atoms:
        checks.append(
            ValidationCheck("geometry", ValidationLevel.ERROR, "输出文件中未找到最终几何，无法核验配对。")
        )
    elif len(dataset.atoms) != len(output.atoms):
        checks.append(
            ValidationCheck(
                "atom_count",
                ValidationLevel.ERROR,
                f"原子数不一致：波函数 {len(dataset.atoms)}，输出 {len(output.atoms)}。",
            )
        )
    elif tuple(atom.atomic_number for atom in dataset.atoms) != tuple(
        atom.atomic_number for atom in output.atoms
    ):
        checks.append(
            ValidationCheck("atom_order", ValidationLevel.ERROR, "元素或原子顺序不一致。")
        )
    else:
        geometry_rmsd = _distance_matrix_rmsd(dataset.atoms, output.atoms)
        checks.append(
            ValidationCheck(
                "geometry",
                ValidationLevel.OK
                if geometry_rmsd <= geometry_tolerance_angstrom
                else ValidationLevel.ERROR,
                (
                    f"最终几何一致（距离矩阵 RMSD={geometry_rmsd:.3g} Å）。"
                    if geometry_rmsd <= geometry_tolerance_angstrom
                    else f"最终几何不一致（距离矩阵 RMSD={geometry_rmsd:.3g} Å）。"
                ),
                geometry_rmsd,
            )
        )

    for name, wave_value, output_value, label in (
        ("charge", dataset.charge, output.charge, "电荷"),
        ("multiplicity", dataset.multiplicity, output.multiplicity, "多重度"),
        ("alpha_electrons", dataset.alpha_electrons, output.alpha_electrons, "alpha 电子数"),
        ("beta_electrons", dataset.beta_electrons, output.beta_electrons, "beta 电子数"),
    ):
        if wave_value is None or output_value is None:
            checks.append(
                ValidationCheck(name, ValidationLevel.WARNING, f"{label}信息不完整，已跳过该项核验。")
            )
        elif wave_value == output_value:
            checks.append(ValidationCheck(name, ValidationLevel.OK, f"{label}一致：{wave_value}。", wave_value))
        else:
            checks.append(
                ValidationCheck(
                    name,
                    ValidationLevel.ERROR,
                    f"{label}不一致：波函数 {wave_value}，输出 {output_value}。",
                )
            )

    wave_map = _wavefunction_orbital_map(dataset)
    output_map = _output_orbital_map(dataset, output)
    differences = [
        abs(wave_map[key].energy_hartree - output_map[key].energy_hartree)
        for key in wave_map.keys() & output_map.keys()
    ]
    max_difference = max(differences) if differences else None
    if max_difference is None:
        checks.append(
            ValidationCheck(
                "orbital_energies",
                ValidationLevel.WARNING,
                "输出未提供可对应的轨道能量；Cube 仍以波函数文件为准。",
            )
        )
    else:
        checks.append(
            ValidationCheck(
                "orbital_energies",
                ValidationLevel.OK
                if max_difference <= orbital_energy_tolerance_hartree
                else ValidationLevel.ERROR,
                (
                    f"已核验 {len(differences)} 条轨道能量，最大差值 {max_difference:.3g} Hartree。"
                    if max_difference <= orbital_energy_tolerance_hartree
                    else f"轨道能量不一致，最大差值 {max_difference:.3g} Hartree。"
                ),
                max_difference,
            )
        )
    return PairValidation(
        checks=tuple(checks),
        geometry_distance_rmsd=geometry_rmsd,
        orbital_energy_max_difference_hartree=max_difference,
        compared_orbital_count=len(differences),
    )


def parse_input_pair(
    output_path: str | Path,
    wavefunction_path: str | Path,
    *,
    strict: bool = True,
) -> OrbitalDataset:
    """Parse and validate one explicit output/wavefunction pair.

    With the safe default ``strict=True``, an invalid pair raises
    :class:`PairingError`; the full report is available as
    ``exception.validation``.  ``strict=False`` returns the dataset with the
    failed report attached, which is useful for a diagnostic UI.
    """

    dataset = parse_wavefunction_file(wavefunction_path)
    output = parse_output_file(output_path, dataset.program)
    validation = validate_input_pair(dataset, output)
    combined = replace(dataset, output_metadata=output, pair_validation=validation)
    if strict and not validation.is_valid:
        raise PairingError("输出文件与波函数文件未通过配对核验。", validation=validation)
    return combined


def _file_base(path: Path) -> str:
    name = path.name
    lower = name.casefold()
    for suffix in (".molden.input", ".fchk", ".fch", ".molden", ".out", ".log"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _pairing_key(path: Path) -> str:
    base = _file_base(path).casefold()
    base = re.sub(r"(?:[._-](?:molden|fchk|fch|output|out|log))$", "", base)
    return re.sub(r"[^a-z0-9]+", "", base)


def _wavefunction_program(path: Path) -> CalculationProgram | None:
    name = path.name.casefold()
    if name.endswith((".fch", ".fchk")):
        return CalculationProgram.GAUSSIAN
    if name.endswith((".molden", ".molden.input")):
        return CalculationProgram.ORCA
    return None


def _expand_input_paths(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            result.extend(item.resolve() for item in path.iterdir() if item.is_file())
        else:
            result.append(path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in result:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return tuple(unique)


def pair_input_files(paths: Iterable[str | Path]) -> list[InputPair]:
    """Pair uploaded output and wavefunction files without silent guessing.

    Exact normalized base names are preferred.  A single remaining output and
    wavefunction file of the same program may be paired as a documented
    singleton fallback.  Ambiguous or unmatched files raise :class:`PairingError`
    instead of being silently dropped.
    """

    expanded = _expand_input_paths(paths)
    missing = [path for path in expanded if not path.is_file()]
    if missing:
        raise PairingError(f"文件不存在：{missing[0]}")
    wavefunctions = [path for path in expanded if _wavefunction_program(path) is not None]
    outputs = [path for path in expanded if path.suffix.casefold() in {".out", ".log"}]
    if not wavefunctions:
        raise PairingError("没有找到 .fch/.fchk 或 .molden/.molden.input 波函数文件。")
    if not outputs:
        raise PairingError("没有找到 Gaussian/ORCA .out 或 .log 输出文件。")
    output_programs = {path: _detect_output_program(path) for path in outputs}
    remaining = set(outputs)
    pairs: list[InputPair] = []
    unmatched: list[Path] = []
    for wavefunction in wavefunctions:
        program = _wavefunction_program(wavefunction)
        candidates = [
            output
            for output in remaining
            if output_programs[output] is program
            and _pairing_key(output) == _pairing_key(wavefunction)
        ]
        if len(candidates) == 1:
            output = candidates[0]
            remaining.remove(output)
            pairs.append(InputPair(output, wavefunction, program))
        elif len(candidates) > 1:
            raise PairingError(f"{wavefunction.name} 对应多个同名输出文件，请手动指定。")
        else:
            unmatched.append(wavefunction)
    if len(unmatched) == 1:
        program = _wavefunction_program(unmatched[0])
        candidates = [output for output in remaining if output_programs[output] is program]
        if len(candidates) == 1:
            output = candidates[0]
            remaining.remove(output)
            pairs.append(
                InputPair(
                    output,
                    unmatched[0],
                    program,
                    pairing_reason="single compatible remainder",
                    warnings=("文件基础名不同，已按唯一同程序文件临时配对；运行前仍会核验内容。",),
                )
            )
            unmatched.clear()
    if unmatched:
        names = "、".join(path.name for path in unmatched)
        raise PairingError(f"以下波函数文件没有唯一对应的输出：{names}")
    if remaining:
        names = "、".join(path.name for path in sorted(remaining))
        raise PairingError(f"以下输出文件没有对应波函数文件：{names}")
    return sorted(pairs, key=lambda item: item.label.casefold())


def available_spin_modes(dataset: OrbitalDataset) -> tuple[str, ...]:
    """Return user-facing spin-mode identifiers valid for the dataset."""

    return ("both", "alpha", "beta") if dataset.is_unrestricted else ("spatial",)


def selection_presets(dataset: OrbitalDataset) -> tuple[SelectionPreset, ...]:
    """Return the standard compact selector options for the UI."""

    spin_description = "alpha 与 beta 通道" if dataset.is_unrestricted else "空间轨道"
    enabled = dataset.supports_frontier_labels
    return (
        SelectionPreset("homo", "HOMO", "HOMO", f"选择{spin_description}的最高占据轨道", "auto", enabled),
        SelectionPreset("lumo", "LUMO", "LUMO", f"选择{spin_description}的最低未占据轨道", "auto", enabled),
        SelectionPreset(
            "homo_lumo",
            "HOMO + LUMO",
            "HOMO,LUMO",
            f"选择{spin_description}的前线轨道对",
            "auto",
            enabled,
        ),
        SelectionPreset(
            "homo_minus_1_to_lumo_plus_3",
            "HOMO−1 至 LUMO+3",
            "HOMO-1..LUMO+3",
            f"连续选择{spin_description}的常用前线范围",
            "auto",
            enabled,
        ),
        SelectionPreset(
            "custom",
            "自定义轨道",
            "",
            "输入轨道号、范围或带 alpha/beta 的前线轨道表达式",
            "auto",
            True,
        ),
    )


def _format_frontier(anchor: str, offset: int) -> str:
    if offset == 0:
        return anchor
    return f"{anchor}{offset:+d}"


def _selection_spins(dataset: OrbitalDataset, spin_mode: str) -> tuple[SpinChannel, ...]:
    normalized = str(spin_mode or "auto").strip().casefold()
    if dataset.is_unrestricted:
        if normalized in {
            "auto",
            "both",
            "all",
            "alpha_beta",
            "alpha/beta",
            "alpha+beta",
            "αβ",
            "α/β",
            "α+β",
            "全部",
        }:
            return (SpinChannel.ALPHA, SpinChannel.BETA)
        spin = _coerce_spin(normalized)
        if spin is SpinChannel.SPATIAL:
            raise OrbitalSelectionError("非限制性波函数没有单一 spatial 轨道通道。")
        return (spin,)
    if normalized not in {"auto", "both", "all", "spatial", "restricted", "r"}:
        requested = _coerce_spin(normalized)
        raise OrbitalSelectionError(
            f"{dataset.wavefunction_type.value} 只有共享空间轨道，不能选择独立 {requested.value} 通道。"
        )
    return (SpinChannel.SPATIAL,)


def _frontier_endpoint(dataset: OrbitalDataset, spin: SpinChannel, token: str) -> int:
    match = re.fullmatch(r"(HOMO|LUMO)([+-]\d+)?", token.strip(), re.IGNORECASE)
    if not match:
        raise OrbitalSelectionError(f"无法识别前线轨道表达式：{token}")
    if not dataset.supports_frontier_labels:
        raise OrbitalSelectionError("当前轨道集合为分数占据或特殊轨道，请使用绝对编号。")
    anchor = match.group(1).upper()
    offset = int(match.group(2) or 0)
    base = dataset.homo_index(spin) if anchor == "HOMO" else dataset.lumo_index(spin)
    return base + offset


def _indices_from_segment(
    dataset: OrbitalDataset,
    spin: SpinChannel,
    segment: str,
) -> list[int]:
    segment = segment.strip()
    if not segment:
        return []
    frontier_range = re.fullmatch(
        r"(HOMO|LUMO)([+-]\d+)?\s*\.\.\s*(HOMO|LUMO)([+-]\d+)?",
        segment,
        re.IGNORECASE,
    )
    if frontier_range:
        left = _frontier_endpoint(
            dataset,
            spin,
            frontier_range.group(1) + (frontier_range.group(2) or ""),
        )
        right = _frontier_endpoint(
            dataset,
            spin,
            frontier_range.group(3) + (frontier_range.group(4) or ""),
        )
        step = 1 if right >= left else -1
        return list(range(left, right + step, step))
    frontier = re.fullmatch(r"(HOMO|LUMO)([+-]\d+)?", segment, re.IGNORECASE)
    if frontier:
        return [_frontier_endpoint(dataset, spin, segment)]
    numeric_range = re.fullmatch(r"(\d+)\s*(?:-|\.\.)\s*(\d+)", segment)
    if numeric_range:
        left, right = int(numeric_range.group(1)), int(numeric_range.group(2))
        step = 1 if right >= left else -1
        return list(range(left, right + step, step))
    if re.fullmatch(r"\d+", segment):
        return [int(segment)]
    raise OrbitalSelectionError(f"无法识别轨道选择片段：{segment}")


def _semantic_label(dataset: OrbitalDataset, orbital: Orbital) -> str:
    if not dataset.supports_frontier_labels:
        if dataset.is_unrestricted:
            prefix = "α" if orbital.spin is SpinChannel.ALPHA else "β"
            return f"{prefix}-MO {orbital.channel_index}"
        return f"MO {orbital.channel_index}"
    homo = dataset.homo_index(orbital.spin)
    lumo = dataset.lumo_index(orbital.spin)
    if orbital.channel_index <= homo:
        offset = orbital.channel_index - homo
        core = "HOMO" if offset == 0 else f"HOMO{offset:+d}"
    else:
        offset = orbital.channel_index - lumo
        core = "LUMO" if offset == 0 else f"LUMO{offset:+d}"
    if dataset.is_unrestricted:
        prefix = "α" if orbital.spin is SpinChannel.ALPHA else "β"
        return f"{prefix}-{core}"
    return core


def _orbital_ref(dataset: OrbitalDataset, orbital: Orbital) -> OrbitalRef:
    if dataset.supports_frontier_labels:
        homo = dataset.homo_index(orbital.spin)
        lumo = dataset.lumo_index(orbital.spin)
    else:
        homo = lumo = -1
    return OrbitalRef(
        spin=orbital.spin,
        channel_index=orbital.channel_index,
        global_index=orbital.global_index,
        label=_semantic_label(dataset, orbital),
        occupation=orbital.occupation,
        energy_hartree=orbital.energy_hartree,
        energy_ev=orbital.energy_ev,
        symmetry=orbital.symmetry,
        is_homo=orbital.channel_index == homo,
        is_lumo=orbital.channel_index == lumo,
    )


def _normalize_expression(text: str) -> str:
    normalized = text.strip().replace("−", "-").replace("～", "..").replace("~", "..")
    normalized = re.sub(r"\s*(?:到|至|\bto\b)\s*", "..", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bspin\s+up\b", "alpha", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bspin\s+down\b", "beta", normalized, flags=re.IGNORECASE)
    return normalized


def _channel_prefix(segment: str) -> tuple[str | None, str]:
    match = re.match(
        r"^\s*(alpha|beta|a|b|α|β|global|g)\s*[:：]\s*(.+)$",
        segment,
        re.IGNORECASE,
    )
    if not match:
        return None, segment
    return match.group(1).casefold(), match.group(2)


def resolve_orbital_selection(
    dataset: OrbitalDataset,
    mode: str = "preset",
    start_offset: int = -1,
    end_offset: int = 3,
    spin_mode: str = "auto",
    text: str | None = None,
) -> list[OrbitalRef]:
    """Resolve a friendly orbital selection to authoritative references.

    Supported modes include ``homo``, ``lumo``, ``homo_lumo``,
    ``frontier_range``/``preset``, and ``custom``/``text``.  Custom examples::

        25,27-30
        HOMO-2..LUMO+3
        alpha:HOMO-1..LUMO+3; beta:HOMO-1..LUMO+3

    Numeric indices are one-based *within the selected channel*.  A numeric
    expression on an unrestricted dataset therefore requires ``spin_mode`` to
    select one channel, or explicit ``alpha:``/``beta:`` groups.  ``global:``
    accepts an advanced Multiwfn global number.  The returned beta references
    use ``number_of_alpha_MOs + channel_index`` automatically.
    """

    normalized_mode = str(mode or "preset").strip().casefold().replace("-", "_")
    if normalized_mode == "homo":
        expression = "HOMO"
    elif normalized_mode == "lumo":
        expression = "LUMO"
    elif normalized_mode in {"homo_lumo", "frontier_pair"}:
        expression = "HOMO,LUMO"
    elif normalized_mode in {
        "preset",
        "frontier_range",
        "homo_minus_1_to_lumo_plus_3",
        "homo_1_to_lumo_3",
    }:
        if normalized_mode == "homo_minus_1_to_lumo_plus_3":
            start_offset, end_offset = -1, 3
        expression = f"{_format_frontier('HOMO', int(start_offset))}..{_format_frontier('LUMO', int(end_offset))}"
    elif normalized_mode in {"custom", "text", "manual"}:
        if not text or not text.strip():
            raise OrbitalSelectionError("自定义轨道选择不能为空。")
        expression = text
    else:
        # Treat a preset expression itself as a convenience mode.
        if re.search(r"HOMO|LUMO|\d", str(mode), re.IGNORECASE):
            expression = str(mode)
        else:
            raise OrbitalSelectionError(f"未知轨道选择模式：{mode}")

    expression = _normalize_expression(expression)
    default_spins = _selection_spins(dataset, spin_mode)
    groups = [group.strip() for group in expression.split(";") if group.strip()]
    if not groups:
        raise OrbitalSelectionError("轨道选择不能为空。")
    selected: dict[tuple[SpinChannel, int], Orbital] = {}
    orbital_lookup = _wavefunction_orbital_map(dataset)
    global_lookup = {orbital.global_index: orbital for orbital in dataset.orbitals}
    for group in groups:
        prefix, body = _channel_prefix(group)
        if prefix in {"global", "g"}:
            for segment in body.split(","):
                for global_index in _indices_from_segment(dataset, default_spins[0], segment):
                    orbital = global_lookup.get(global_index)
                    if orbital is None:
                        raise OrbitalSelectionError(f"Multiwfn 全局轨道号 {global_index} 不存在。")
                    selected[(orbital.spin, orbital.channel_index)] = orbital
            continue
        if prefix is not None:
            spins = (_coerce_spin(prefix),)
            if spins[0] not in dataset.available_spins():
                raise OrbitalSelectionError(f"当前波函数没有独立 {spins[0].value} 轨道集。")
        else:
            spins = default_spins
            numeric_only = all(
                re.fullmatch(r"\s*\d+(?:\s*(?:-|\.\.)\s*\d+)?\s*", item)
                for item in body.split(",")
            )
            if dataset.is_unrestricted and len(spins) > 1 and numeric_only:
                raise OrbitalSelectionError("非限制性波函数的绝对轨道号必须指定 alpha 或 beta 通道。")
        for spin in spins:
            for segment in body.split(","):
                for channel_index in _indices_from_segment(dataset, spin, segment):
                    orbital = orbital_lookup.get((spin, channel_index))
                    if orbital is None:
                        raise OrbitalSelectionError(
                            f"{spin.value} 通道不存在第 {channel_index} 号轨道。"
                        )
                    selected[(spin, channel_index)] = orbital
    spin_order = {SpinChannel.SPATIAL: 0, SpinChannel.ALPHA: 0, SpinChannel.BETA: 1}
    ordered = sorted(
        selected.values(),
        key=lambda orbital: (spin_order[orbital.spin], orbital.channel_index),
    )
    return [_orbital_ref(dataset, orbital) for orbital in ordered]


__all__ = [
    "HARTREE_TO_EV",
    "BOHR_TO_ANGSTROM",
    "OrbitalDataError",
    "UnsupportedFileError",
    "OrbitalParseError",
    "PairingError",
    "OrbitalSelectionError",
    "CalculationProgram",
    "WavefunctionType",
    "SpinChannel",
    "ValidationLevel",
    "Atom",
    "Orbital",
    "OutputOrbitalEnergy",
    "OutputMetadata",
    "ValidationCheck",
    "PairValidation",
    "InputPair",
    "OrbitalDataset",
    "OrbitalRef",
    "SelectionPreset",
    "geometry_fingerprint",
    "parse_wavefunction_file",
    "parse_output_file",
    "validate_input_pair",
    "parse_input_pair",
    "pair_input_files",
    "available_spin_modes",
    "selection_presets",
    "resolve_orbital_selection",
]
