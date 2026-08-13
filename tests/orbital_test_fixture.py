from __future__ import annotations

from pathlib import Path


def _scalar(label: str, kind: str, value: object) -> str:
    return f"{label:<40}{kind}   {value}\n"


def _array(label: str, kind: str, values: list[object]) -> str:
    return (
        f"{label:<40}{kind}   N= {len(values)}\n"
        + " ".join(str(value) for value in values)
        + "\n"
    )


def write_gaussian_pair(
    directory: Path,
    *,
    output_name: str = "sample.out",
    wavefunction_name: str = "sample.fchk",
) -> tuple[Path, Path]:
    """Create a small, internally consistent Gaussian output/FCHK test pair."""

    directory.mkdir(parents=True, exist_ok=True)
    output = directory / output_name
    wavefunction = directory / wavefunction_name
    energies = [-0.75, -0.50, 0.10, 0.20, 0.30, 0.40]
    wavefunction.write_text(
        "Synthetic helium dimer\n"
        "SP        RHF/STO-3G\n"
        + _scalar("Number of atoms", "I", 2)
        + _scalar("Charge", "I", 0)
        + _scalar("Multiplicity", "I", 1)
        + _scalar("Number of electrons", "I", 4)
        + _scalar("Number of alpha electrons", "I", 2)
        + _scalar("Number of beta electrons", "I", 2)
        + _scalar("Number of basis functions", "I", 6)
        + _scalar("Number of independent functions", "I", 6)
        + _array("Atomic numbers", "I", [2, 2])
        + _array(
            "Current cartesian coordinates",
            "R",
            [0.0, 0.0, -1.0, 0.0, 0.0, 1.0],
        )
        + _array("Alpha Orbital Energies", "R", energies)
        + _array("Alpha MO coefficients", "R", [1.0]),
        encoding="utf-8",
    )
    bohr = 0.529177210903
    output.write_text(
        " Entering Gaussian System\n"
        " #p rhf/sto-3g\n"
        " ----------------------------------------\n"
        " Charge = 0 Multiplicity = 1\n"
        " 2 alpha electrons 2 beta electrons\n"
        " SCF Done:  E(RHF) = -2.000000 A.U. after 8 cycles\n"
        " Standard orientation:\n"
        " ---------------------------------------------------------------------\n"
        " Center     Atomic      Atomic             Coordinates (Angstroms)\n"
        " Number     Number       Type             X           Y           Z\n"
        " ---------------------------------------------------------------------\n"
        f" 1 2 0 0.000000 0.000000 {-bohr:.12f}\n"
        f" 2 2 0 0.000000 0.000000 {bohr:.12f}\n"
        " ---------------------------------------------------------------------\n"
        " occ. eigenvalues -- -0.750000 -0.500000\n"
        " virt. eigenvalues -- 0.100000 0.200000 0.300000 0.400000\n"
        " Normal termination of Gaussian 16\n",
        encoding="utf-8",
    )
    return output, wavefunction
