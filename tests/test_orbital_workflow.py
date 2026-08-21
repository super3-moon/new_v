from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
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

    def test_odd_electron_completion_adds_only_matching_boundary_indices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "odd.fchk"
            text = "Synthetic odd UHF\nUHF STO-3G\n"
            text += self._fch_scalar("Number of atoms", "I", 1)
            text += self._fch_scalar("Number of alpha electrons", "I", 2)
            text += self._fch_scalar("Number of beta electrons", "I", 1)
            text += self._fch_scalar("Number of basis functions", "I", 4)
            text += self._fch_scalar("Number of independent functions", "I", 4)
            text += self._fch_array("Atomic numbers", "I", [1])
            text += self._fch_array("Current cartesian coordinates", "R", [0.0, 0.0, 0.0])
            text += self._fch_array("Alpha Orbital Energies", "R", [-0.8, -0.4, 0.1, 0.5])
            text += self._fch_array("Beta Orbital Energies", "R", [-0.7, 0.2, 0.4, 0.9])
            path.write_text(text, encoding="ascii")

            dataset = orbital_data.parse_wavefunction_file(path)
            refs = orbital_data.resolve_orbital_selection(
                dataset,
                mode="custom",
                text="HOMO..LUMO+1",
                spin_mode="both",
            )
            self.assertEqual(
                {(item.spin.value, item.channel_index) for item in refs},
                {("alpha", 2), ("alpha", 3), ("alpha", 4), ("beta", 1), ("beta", 2), ("beta", 3)},
            )
            completed = orbital_data.complete_odd_electron_boundary_pairs(dataset, refs)
            self.assertEqual(
                {(item.spin.value, item.channel_index) for item in completed},
                {
                    ("alpha", 1), ("alpha", 2), ("alpha", 3), ("alpha", 4),
                    ("beta", 1), ("beta", 2), ("beta", 3), ("beta", 4),
                },
            )


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

    def test_success_workspace_cleanup_stays_inside_run_jobs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root)
            runner = workflow.OrbitalDiagramRunner(
                plan, Path(__file__), Path(__file__)
            )
            job = plan.jobs[0]
            job.work_dir.mkdir(parents=True)
            (job.work_dir / "temporary.vmd").write_text("process", encoding="utf-8")
            job.status = workflow.STATUS_SUCCESS
            runner._discard_success_intermediates()
            self.assertFalse(job.work_dir.exists())

            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("user", encoding="utf-8")
            job.work_dir = outside
            runner._discard_success_intermediates()
            self.assertTrue((outside / "keep.txt").is_file())

    def test_single_job_progress_reports_useful_weighted_stages(self) -> None:
        events: list[dict] = []

        class StageRunner(workflow.OrbitalDiagramRunner):
            def _run_job(self, job: workflow.OrbitalDiagramJob) -> None:
                self._set_stage(job, workflow.STAGE_PARSE, "正在读取输入文件")
                self._set_stage(job, workflow.STAGE_RENDER, "正在渲染轨道图像")
                job.status = workflow.STATUS_SUCCESS
                job.error = ""

        with tempfile.TemporaryDirectory() as temporary:
            plan = self._plan(Path(temporary))
            runner = StageRunner(
                plan, Path(__file__), Path(__file__), event_callback=events.append
            )
            runner.run()

        progress = [event for event in events if event["kind"] == "progress"]
        percentages = [float(event["percent"]) for event in progress]
        self.assertGreaterEqual(len(percentages), 3)
        self.assertEqual(percentages, sorted(percentages))
        self.assertTrue(any(0.0 < value < 100.0 for value in percentages))
        self.assertIn(workflow.STAGE_PARSE, {event.get("stage") for event in progress})
        self.assertIn(workflow.STAGE_RENDER, {event.get("stage") for event in progress})
        self.assertEqual(percentages[-1], 100.0)

    def test_energy_anomaly_only_warns_for_an_isolated_endpoint(self) -> None:
        anomaly = workflow.detect_energy_spacing_anomaly(
            [
                {"label": "HOMO-9", "energy_ev": -60.0},
                {"label": "HOMO-2", "energy_ev": -8.2},
                {"label": "HOMO-1", "energy_ev": -7.9},
                {"label": "HOMO", "energy_ev": -7.5},
            ]
        )
        self.assertIsNotNone(anomaly)
        assert anomaly is not None
        self.assertEqual(anomaly["isolated_labels"], ["HOMO-9"])
        self.assertGreater(float(anomaly["gap_ev"]), 50.0)

        # A large but legitimate occupied/virtual separation has levels on
        # both sides, so it must not be mistaken for one accidental outlier.
        self.assertIsNone(
            workflow.detect_energy_spacing_anomaly(
                [
                    {"label": "HOMO-1", "energy_ev": -8.0},
                    {"label": "HOMO", "energy_ev": -7.8},
                    {"label": "LUMO", "energy_ev": -1.5},
                    {"label": "LUMO+1", "energy_ev": -1.2},
                ]
            )
        )

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

    def test_process_completion_marker_is_opt_in_and_terminates_hosted_vmd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root)
            runner = workflow.OrbitalDiagramRunner(
                plan, Path(sys.executable), Path(sys.executable)
            )
            job = plan.jobs[0]
            marker = root / "capture.confirmed"
            log = root / "hosted.log"
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys, time; "
                    "Path(sys.argv[1]).write_text('done', encoding='utf-8'); "
                    "time.sleep(30)"
                ),
                str(marker),
            ]
            started = time.monotonic()
            return_code, reason = runner._run_process(
                command,
                cwd=root,
                env={},
                stdin_text=None,
                timeout_seconds=20,
                log_path=log,
                source="VMD",
                job=job,
                hide_window=True,
                completion_markers={"viewpoint_confirmed": marker},
            )
            self.assertEqual(reason, "viewpoint_confirmed")
            self.assertNotEqual(return_code, 0)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn("viewpoint_confirmed", log.read_text(encoding="utf-8"))

            plain_log = root / "plain.log"
            return_code, reason = runner._run_process(
                [sys.executable, "-c", "print('ok')"],
                cwd=root,
                env={},
                stdin_text=None,
                timeout_seconds=20,
                log_path=plain_log,
                source="test",
                job=job,
                hide_window=True,
            )
            self.assertEqual((return_code, reason), (0, ""))


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
            self.assertIn("display reposition 32 40", script)
            self.assertIn("set ::MO_CANCEL_PATH", script)
            self.assertIn("set ::MO_ERROR_PATH", script)
            self.assertIn("_mo_write_marker $::MO_CANCEL_PATH cancelled", script)
            self.assertNotIn("    quit\n", script)
            self.assertNotIn("    exit\n", script)

    def test_vmd_orbital_artifact_names_are_ascii_only(self) -> None:
        ref = orbital_data.OrbitalRef(
            spin=orbital_data.SpinChannel.ALPHA,
            channel_index=267,
            global_index=267,
            energy_hartree=-0.25,
            energy_ev=-6.802846,
            occupation=1.0,
            label="α-HOMO-1",
        )
        stem = workflow._orbital_artifact_stem(ref)
        self.assertEqual(stem, "alpha_000267_000267")
        self.assertTrue(stem.isascii())
        self.assertNotIn("α", stem)

    def test_interactive_vmd_viewport_is_not_the_render_resolution(self) -> None:
        self.assertEqual(workflow.INTERACTIVE_VMD_VIEWPORT, (1160, 640))
        self.assertEqual(workflow.INTERACTIVE_VMD_WINDOW, (1180, 700))
        settings = workflow.OrbitalDiagramSettings.from_value(
            {"width": 1600, "height": 1200, "style_snapshot": signed_style_snapshot()}
        )
        self.assertEqual((settings.width, settings.height), (1600, 1200))

    def test_capture_marker_paths_are_paired_without_replacing_suffix(self) -> None:
        state = Path("work") / "viewpoint.capture"
        self.assertEqual(
            orbital_vmd.capture_cancel_marker_path(state),
            Path("work") / "viewpoint.capture.cancelled",
        )
        self.assertEqual(
            orbital_vmd.capture_error_log_path(state),
            Path("work") / "viewpoint.capture.error.log",
        )

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
