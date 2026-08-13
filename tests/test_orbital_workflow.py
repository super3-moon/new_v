from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import orbital_data
import orbital_diagram_workflow as workflow
import orbital_vmd
import vmd_style_tool as core
from tests.orbital_test_fixture import write_gaussian_pair


class GaussianFixtureMixin:
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_directory = tempfile.TemporaryDirectory()
        cls.gaussian_out, cls.gaussian_fchk = write_gaussian_pair(
            Path(cls.fixture_directory.name)
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture_directory.cleanup()


def signed_style_snapshot() -> dict:
    style = next(
        item
        for item in core.get_all_bundle_styles()
        if str(item.get("surface_mode") or "signed") == "signed"
    )
    return {
        "style": style,
        "rep0_commands": list(style.get("rep0_commands") or []),
        "selection_text": f"套装风格：{style['name']}",
        "mode": "bundle",
        "bundle_id": style["id"],
    }


class OrbitalDataTests(GaussianFixtureMixin, unittest.TestCase):
    @staticmethod
    def _fch_scalar(label: str, kind: str, value: object) -> str:
        return f"{label:<40}{kind}   {value}\n"

    @staticmethod
    def _fch_array(label: str, kind: str, values: list[object]) -> str:
        return f"{label:<40}{kind}   N= {len(values)}\n" + " ".join(
            str(item) for item in values
        ) + "\n"

    def test_real_gaussian_pair_and_friendly_frontier_selection(self) -> None:
        pair = orbital_data.InputPair(
            self.gaussian_out, self.gaussian_fchk, orbital_data.CalculationProgram.GAUSSIAN
        )
        dataset = orbital_data.parse_input_pair(pair.output_path, pair.wavefunction_path)
        refs = orbital_data.resolve_orbital_selection(
            dataset, mode="homo_minus_1_to_lumo_plus_3"
        )
        self.assertEqual(
            [item.label for item in refs],
            ["HOMO-1", "HOMO", "LUMO", "LUMO+1", "LUMO+2", "LUMO+3"],
        )
        self.assertEqual([item.global_index for item in refs], [1, 2, 3, 4, 5, 6])

    def test_manual_expression_and_per_orbital_filter_reach_plan(self) -> None:
        pair = orbital_data.InputPair(
            self.gaussian_out, self.gaussian_fchk, orbital_data.CalculationProgram.GAUSSIAN
        )
        dataset = orbital_data.parse_input_pair(pair.output_path, pair.wavefunction_path)
        refs = orbital_data.resolve_orbital_selection(
            dataset, mode="custom", text="HOMO-1..LUMO+1"
        )
        with tempfile.TemporaryDirectory() as temporary:
            settings = {
                "selection_mode": "custom",
                "selection_text": "HOMO-1..LUMO+1",
                "style_snapshot": signed_style_snapshot(),
                "orbital_selections": [
                    {
                        "wavefunction_path": str(pair.wavefunction_path.resolve()),
                        "orbitals": [refs[0].to_dict(), refs[-1].to_dict()],
                    }
                ],
            }
            plan = workflow.create_orbital_diagram_plan(
                [pair], Path(temporary), settings
            )
            runner = workflow.OrbitalDiagramRunner(
                plan, Path(__file__), Path(__file__)
            )
            resolved_dataset, selected = runner._parse_and_resolve(plan.jobs[0])
            self.assertEqual(resolved_dataset.nbasis, dataset.nbasis)
            self.assertEqual([item.label for item in selected], ["HOMO-1", "LUMO+1"])

    def test_multiwfn_sequence_returns_from_submenu_before_quit(self) -> None:
        pair = orbital_data.InputPair(
            self.gaussian_out, self.gaussian_fchk, orbital_data.CalculationProgram.GAUSSIAN
        )
        with tempfile.TemporaryDirectory() as temporary:
            plan = workflow.create_orbital_diagram_plan(
                [pair],
                Path(temporary),
                {"style_snapshot": signed_style_snapshot()},
            )
            runner = workflow.OrbitalDiagramRunner(plan, Path(__file__), Path(__file__))
            dataset, refs = workflow.inspect_orbital_pair(pair, plan.settings)
            self.assertGreater(dataset.nbasis, 0)
            sequence = runner._multiwfn_sequence(refs)
            self.assertTrue(sequence.endswith("\n1\n0\nq\n"))

    def test_uhf_fchk_beta_offset_uses_actual_alpha_mo_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "linear_dependence.fchk"
            text = "Synthetic UHF\nUHF STO-3G\n"
            text += self._fch_scalar("Number of atoms", "I", 1)
            text += self._fch_scalar("Number of alpha electrons", "I", 1)
            text += self._fch_scalar("Number of beta electrons", "I", 1)
            text += self._fch_scalar("Number of basis functions", "I", 5)
            text += self._fch_scalar("Number of independent functions", "I", 3)
            text += self._fch_array("Atomic numbers", "I", [1])
            text += self._fch_array("Current cartesian coordinates", "R", [0.0, 0.0, 0.0])
            text += self._fch_array("Alpha Orbital Energies", "R", [-0.5, 0.1, 0.2])
            text += self._fch_array("Beta Orbital Energies", "R", [-0.4, 0.3])
            path.write_text(text, encoding="ascii")

            dataset = orbital_data.parse_wavefunction_file(path)
            alpha = dataset.orbitals_for_spin(orbital_data.SpinChannel.ALPHA)
            beta = dataset.orbitals_for_spin(orbital_data.SpinChannel.BETA)
            self.assertEqual(dataset.nbasis, 5)
            self.assertEqual([item.global_index for item in alpha], [1, 2, 3])
            self.assertEqual([item.global_index for item in beta], [4, 5])

    def test_uhf_molden_beta_offset_uses_actual_alpha_mo_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "linear_dependence.molden.input"
            blocks = ["[Molden Format]", "[Atoms] Angs", "H 1 1 0 0 0", "[MO]"]
            for spin, energies in (("Alpha", [-0.5, 0.1, 0.2]), ("Beta", [-0.4, 0.3])):
                for number, energy in enumerate(energies, 1):
                    blocks.extend(
                        [
                            f"Sym= {number}a",
                            f"Ene= {energy}",
                            f"Spin= {spin}",
                            f"Occup= {1.0 if number == 1 else 0.0}",
                            "5 0.1",
                        ]
                    )
            path.write_text("\n".join(blocks) + "\n", encoding="ascii")

            dataset = orbital_data.parse_wavefunction_file(path)
            beta = dataset.orbitals_for_spin(orbital_data.SpinChannel.BETA)
            self.assertEqual(dataset.nbasis, 5)
            self.assertEqual([item.global_index for item in beta], [4, 5])


class OrbitalWorkflowRecoveryTests(GaussianFixtureMixin, unittest.TestCase):
    def _plan(self, root: Path) -> workflow.OrbitalDiagramPlan:
        pair = orbital_data.InputPair(
            self.gaussian_out, self.gaussian_fchk, orbital_data.CalculationProgram.GAUSSIAN
        )
        return workflow.create_orbital_diagram_plan(
            [pair], root, {"style_snapshot": signed_style_snapshot()}
        )

    def test_cube_fallback_matches_complete_numeric_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrong = root / "orb000117.cub"
            wrong.write_text("wrong", encoding="ascii")
            self.assertIsNone(workflow._fallback_cube_for_index(root, 17))
            exact = root / "orb000017_retry.cub"
            exact.write_text("right", encoding="ascii")
            self.assertEqual(workflow._fallback_cube_for_index(root, 17), exact)

    def test_targeted_retry_preserves_unselected_jobs_and_global_failure(self) -> None:
        events: list[dict] = []

        class RecordingRunner(workflow.OrbitalDiagramRunner):
            calls: list[str] = []

            def _run_job(self, job: workflow.OrbitalDiagramJob) -> None:
                self.calls.append(job.id)
                job.status = workflow.STATUS_SUCCESS
                job.error = ""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root)
            first = plan.jobs[0]
            first.status = workflow.STATUS_SUCCESS
            selected = copy.deepcopy(first)
            selected.id = "selected"
            selected.index = 2
            selected.status = workflow.STATUS_FAILED
            selected.error = "selected failure"
            selected.work_dir = plan.run_dir / "jobs" / "selected"
            untouched = copy.deepcopy(first)
            untouched.id = "untouched"
            untouched.index = 3
            untouched.status = workflow.STATUS_FAILED
            untouched.error = "must remain"
            untouched.work_dir = plan.run_dir / "jobs" / "untouched"
            marker = untouched.work_dir / "artifact.bin"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_bytes(b"unchanged")
            plan.jobs = [first, selected, untouched]
            plan.run_dir.mkdir(parents=True, exist_ok=True)
            plan.manifest_path.write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False), encoding="utf-8"
            )

            resumed = workflow.resume_orbital_diagram_plan(
                plan.manifest_path, job_ids=[selected.id]
            )
            self.assertEqual(resumed.jobs[2].status, workflow.STATUS_FAILED)
            runner = RecordingRunner(
                resumed, Path(__file__), Path(__file__), event_callback=events.append
            )
            result = runner.run()

            self.assertEqual(runner.calls, [selected.id])
            self.assertEqual(resumed.jobs[0].status, workflow.STATUS_SUCCESS)
            self.assertEqual(resumed.jobs[2].status, workflow.STATUS_FAILED)
            self.assertEqual(resumed.jobs[2].error, "must remain")
            self.assertEqual(marker.read_bytes(), b"unchanged")
            self.assertEqual(result["status"], workflow.STATUS_FAILED)
            self.assertEqual(result["failed"], 1)
            progress = [event for event in events if event["kind"] == "progress"]
            self.assertEqual([(item["completed"], item["total"]) for item in progress], [(1, 1)])

    def test_changed_resume_settings_restart_only_earliest_affected_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary))
            plan.jobs[0].status = workflow.STATUS_SUCCESS
            plan.run_dir.mkdir(parents=True, exist_ok=True)
            plan.manifest_path.write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            changed = plan.settings.to_dict()
            changed["title"] = "Revised title"
            resumed = workflow.resume_orbital_diagram_plan(
                plan.manifest_path, settings=changed
            )
            self.assertEqual(resumed.retry_stages, {workflow.STAGE_COMPOSE})
            self.assertEqual(resumed.jobs[0].status, workflow.STATUS_PENDING)

            selection_changed = plan.settings.to_dict()
            selection_changed["end_offset"] += 1
            resumed_selection = workflow.resume_orbital_diagram_plan(
                plan.manifest_path, settings=selection_changed
            )
            self.assertEqual(
                resumed_selection.retry_stages, {workflow.STAGE_ORBITAL_CUBES}
            )

            timeout_only = plan.settings.to_dict()
            timeout_only["vmd_timeout_seconds"] += 1
            resumed_timeout = workflow.resume_orbital_diagram_plan(
                plan.manifest_path, settings=timeout_only
            )
            self.assertEqual(resumed_timeout.retry_stages, set())
            self.assertEqual(resumed_timeout.jobs[0].status, workflow.STATUS_SUCCESS)

    def test_working_cubes_are_removed_safely_when_not_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root)
            job = plan.jobs[0]
            inside = job.work_dir / "cubes" / "orb000001.cub"
            inside.parent.mkdir(parents=True, exist_ok=True)
            inside.write_bytes(b"cube")
            outside = root / "user_input.cub"
            outside.write_bytes(b"input")
            job.reference_cube = str(inside)
            job.cubes = {"spatial:1": str(inside), "spatial:2": str(outside)}
            workflow.OrbitalDiagramRunner._discard_working_cubes(job)
            self.assertFalse(inside.exists())
            self.assertTrue(outside.exists())


class OrbitalVmdTests(unittest.TestCase):
    @staticmethod
    def _cube(path: Path) -> Path:
        path.write_text(
            "title\ntitle\n1 0 0 0\n2 1 0 0\n2 0 1 0\n2 0 0 1\n"
            "1 0 0 0 0\n0 0 0 0 0 0 0 0\n",
            encoding="ascii",
        )
        return path

    @staticmethod
    def _state(cube: Path, *, viewport: tuple[int, int] = (1000, 700)) -> orbital_vmd.VmdViewState:
        display: dict[str, object] = {
            key: 1.0 for key in orbital_vmd._DISPLAY_FLOAT_FIELDS
        }
        display.update({key: False for key in orbital_vmd._DISPLAY_BOOL_FIELDS})
        display.update(
            {
                "rendermode": "Normal",
                "stereo": "Off",
                "projection": "Orthographic",
                "cuemode": "Exp2",
                "size": viewport,
            }
        )
        identity = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        return orbital_vmd.VmdViewState(
            schema_version=orbital_vmd.STATE_SCHEMA_VERSION,
            vmd_version="1.9.3",
            geometry_fingerprint=orbital_vmd.cube_geometry_fingerprint(cube),
            reference_cube_sha256=orbital_vmd._file_sha256(cube),
            display=display,
            axes_location="Off",
            stage_location="Off",
            lights=(),
            colors=(),
            color_categories=(),
            color_scale_method="RWB",
            color_scale_midpoint=0.5,
            color_scale_min=0.0,
            color_scale_max=1.0,
            color_scales=(
                orbital_vmd.VmdColorScale(
                    "RWB", (1.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 1.0)
                ),
            ),
            materials=(
                orbital_vmd.VmdMaterial(
                    "Opaque", 0.0, 0.5, 0.65, 0.5, 0.0, 1.0, 0.0, 0.0, 0.0
                ),
            ),
            representations=(
                orbital_vmd.VmdRepresentation(
                    0, "CPK 1.0 0.3 12 12", "all", "Name", "Opaque",
                    scale_minmax=(-1.25, 2.5),
                ),
            ),
            matrices={name: identity for name in orbital_vmd._MATRIX_NAMES},
        ).validate()

    def test_capture_script_saves_complete_state_only_on_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cube = self._cube(root / "orb.cub")
            script = orbital_vmd.build_interactive_capture_tcl(
                cube,
                root / "state.capture",
                signed_style_snapshot()["style"],
            )
            self.assertIn("保存全部参数并确认".encode("unicode_escape").decode(), script)
            self.assertIn("center_matrix rotate_matrix scale_matrix global_matrix", script)
            self.assertIn("COLOR_CATEGORY", script)
            self.assertIn("MATERIAL", script)
            self.assertIn("REP", script)
            self.assertIn("MolecularStudio managed native state", script)

    def test_batch_script_uses_tachyon_and_managed_native_state(self) -> None:
        source = Path("orbital_vmd.py").read_text(encoding="utf-8")
        self.assertIn('renderer: str = "TachyonInternal"', source)
        self.assertIn("render {renderer}", source)
        self.assertIn("source $MO_NATIVE_STATE", source)

    def test_batch_render_fits_requested_box_to_captured_aspect(self) -> None:
        self.assertEqual(
            orbital_vmd.resolve_render_dimensions((1000, 700), width=900),
            (900, 630),
        )
        self.assertEqual(
            orbital_vmd.resolve_render_dimensions((1000, 700), height=630),
            (900, 630),
        )
        self.assertEqual(
            orbital_vmd.resolve_render_dimensions(
                (1000, 700), width=900, height=900
            ),
            (900, 630),
        )

    def test_scaleminmax_uses_two_numeric_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cube = self._cube(root / "orb.cub")
            state = self._state(cube)
            script = "\n".join(
                orbital_vmd._restore_state_tcl(
                    state, cube, root / "orb.tga", 1000, 700, "TachyonInternal"
                )
            )
            self.assertIn("mol scaleminmax $MO_MOL 0 -1.25 2.5", script)
            self.assertNotIn("mol scaleminmax $MO_MOL 0 {-1.25 2.5}", script)
            self.assertIn(
                "color scale colors $MO_SCALE_NAME {1 0 0} {1 1 1} {0 0 1}",
                script,
            )

    def test_native_state_is_paired_and_reference_cube_is_substituted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = self._cube(root / "reference.cub")
            target = self._cube(root / "target.cub")
            state = self._state(reference)
            native = root / "final.vmd"
            native.write_text(
                "#!/usr/local/bin/vmd\n"
                "# VMD script written by save_state $Revision: 1.47 $\n"
                "# VMD version: 1.9.3\n"
                f"mol new {{{reference}}} type cube waitfor all\n"
                "# MolecularStudio managed native state\n"
                f"# MolecularStudio geometry {state.geometry_fingerprint}\n"
                f"# MolecularStudio cube_sha256 {state.reference_cube_sha256}\n",
                encoding="utf-8",
            )
            script = orbital_vmd.build_batch_render_tcl(
                target,
                root / "target.tga",
                state,
                width=900,
                height=900,
                native_state_path=native,
                reference_cube_path=reference,
            )
            self.assertIn("source $MO_NATIVE_STATE", script)
            self.assertIn("rename mol _mo_native_mol_command", script)
            self.assertIn("set args [lreplace $args 0 0 $::MO_NATIVE_REPLACEMENT]", script)
            self.assertIn("display resize 900 630", script)


if __name__ == "__main__":
    unittest.main()
