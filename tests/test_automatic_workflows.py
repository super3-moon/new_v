from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

import automatic_workflows as automation
import vmd_style_tool as core


def cube_text(*, dimensions: int, step: float) -> str:
    lines = [
        "generated",
        "generated",
        "1 0.0 0.0 0.0",
        f"{dimensions} {step} 0.0 0.0",
        f"{dimensions} 0.0 {step} 0.0",
        f"{dimensions} 0.0 0.0 {step}",
        "1 0.0 0.0 0.0 0.0",
        "0.0 0.0 0.0 0.0",
    ]
    return "\n".join(lines) + "\n"


def esp_style_snapshot() -> dict:
    style = next(
        style
        for style in core.get_all_bundle_styles()
        if style.get("id") == "esp_e3_bwr_edgyglass_443"
    )
    return {
        "style": style,
        "rep0_commands": list(style.get("rep0_commands") or []),
        "selection_text": f"套装风格：{style['name']}",
        "mode": "bundle",
        "bundle_id": style["id"],
    }


class FakeProcessRunner(automation.AutomaticWorkflowRunner):
    def __init__(self, *args, fail_vmd: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_vmd = fail_vmd

    def _run_process(self, command, *, cwd, env, stdin_text, timeout_seconds, log_path, source, index, hide_window):  # type: ignore[override]
        log_path.write_text(f"{source} completed\n", encoding="utf-8")
        if source == "Multiwfn":
            (cwd / "density.cub").write_text(
                cube_text(dimensions=3, step=0.5), encoding="utf-8"
            )
            (cwd / "totesp.cub").write_text(
                cube_text(dimensions=2, step=1.0), encoding="utf-8"
            )
            return 0, ""
        if self.fail_vmd:
            return 7, ""
        from PySide6.QtGui import QColor, QImage

        image = QImage(4, 4, QImage.Format.Format_ARGB32)
        image.fill(QColor("#4f86d9"))
        self.assert_image_saved = image.save(
            str(cwd / str(env["RENDER_FILE"])), "PNG"
        )
        return 0, ""


class AutomaticWorkflowTests(unittest.TestCase):
    def _settings(self, **overrides) -> dict:
        return {
            "rho_iso": "0.001",
            "style_snapshot": esp_style_snapshot(),
            "render_mode": "automatic",
            "width": 1600,
            "height": 1200,
            "output_location": "result_root",
            "keep_cubes": True,
            "vmd_timeout_seconds": 120,
            **overrides,
        }

    def test_catalog_registers_esp_as_one_automatic_workflow(self) -> None:
        definitions = automation.workflow_definitions()
        self.assertEqual([item.id for item in definitions], ["surface_esp"])
        self.assertEqual(definitions[0].engine, "Multiwfn + VMD")
        self.assertIn(".fch", definitions[0].input_extensions)

    def test_settings_keep_scientific_iso_separate_from_style(self) -> None:
        normalized = automation.normalize_settings(self._settings(rho_iso="0.002"))
        self.assertEqual(normalized["rho_iso"], "0.002")
        self.assertEqual(
            normalized["style_snapshot"]["style"]["default_iso_value"],
            esp_style_snapshot()["style"]["default_iso_value"],
        )
        self.assertEqual(len(normalized["style_snapshot"]["hash"]), 64)

    def test_headless_tcl_reuses_volume_mapping_and_renders_then_quits(self) -> None:
        snapshot = esp_style_snapshot()
        tcl = automation.build_automatic_vmd_tcl(
            snapshot["style"], snapshot["rep0_commands"], width=1600, height=1200
        )
        self.assertIn("mol addfile $AUTO_COLOR_CUBE_FILE", tcl)
        self.assertIn("mol modcolor 1 top Volume 1", tcl)
        self.assertIn("display resize 1600 1200", tcl)
        self.assertIn("render TachyonInternal", tcl)
        self.assertTrue(tcl.rstrip().endswith("quit"))
        self.assertNotIn("menu render on", tcl)

    def test_full_pipeline_collects_png_cubes_logs_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_file = root / "ethanol.fch"
            input_file.write_text("wavefunction", encoding="utf-8")
            plan = automation.create_automation_plan(
                [input_file], "surface_esp", root / "runs", self._settings()
            )
            runner = FakeProcessRunner(plan, Path(sys.executable), Path(sys.executable))
            result = runner.run()
            self.assertEqual(result["status"], automation.STATUS_SUCCESS)
            self.assertEqual(result["success"], 1)
            job = plan.jobs[0]
            self.assertTrue(Path(job.image_path).is_file())
            self.assertEqual(Path(job.image_path).suffix.lower(), ".png")
            self.assertTrue(any(path.endswith("_density.cub") for path in job.outputs))
            self.assertTrue(any(path.endswith("_ESP.cub") for path in job.outputs))
            self.assertTrue(any(path.endswith("_Multiwfn.log") for path in job.outputs))
            self.assertTrue(plan.manifest_path.is_file())
            self.assertTrue(plan.summary_path.is_file())
            payload = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["jobs"][0]["multiwfn_status"], "success")
            self.assertEqual(payload["jobs"][0]["vmd_status"], "success")

    def test_vmd_failure_is_distinguished_and_preserves_recovery_cubes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_file = root / "failed.fch"
            input_file.write_text("wavefunction", encoding="utf-8")
            plan = automation.create_automation_plan(
                [input_file], "surface_esp", root / "runs", self._settings()
            )
            runner = FakeProcessRunner(
                plan,
                Path(sys.executable),
                Path(sys.executable),
                fail_vmd=True,
            )
            result = runner.run()
            job = plan.jobs[0]
            self.assertEqual(result["status"], automation.STATUS_FAILED)
            self.assertEqual(job.multiwfn_status, automation.STATUS_SUCCESS)
            self.assertEqual(job.vmd_status, automation.STATUS_FAILED)
            self.assertEqual(job.failed_stage, automation.STAGE_VMD_RENDER)
            self.assertTrue(job.can_retry_drawing)
            recovery = plan.run_dir / "recovery" / f"0001_{input_file.stem}"
            self.assertTrue((recovery / "density.cub").is_file())
            self.assertTrue((recovery / "totesp.cub").is_file())

            test_case = self

            def prepare_retry(retry_runner) -> None:
                def fake_process(_runner, command, *, cwd, env, stdin_text, timeout_seconds, log_path, source, index, hide_window):
                    from PySide6.QtGui import QColor, QImage

                    log_path.write_text("VMD retry completed\n", encoding="utf-8")
                    image = QImage(4, 4, QImage.Format.Format_ARGB32)
                    image.fill(QColor("#4f86d9"))
                    test_case.assertTrue(
                        image.save(str(cwd / str(env["RENDER_FILE"])), "PNG")
                    )
                    return 0, ""

                retry_runner._run_process = types.MethodType(fake_process, retry_runner)

            retry = automation.retry_drawing_from_manifest(
                plan.manifest_path,
                job.id,
                Path(sys.executable),
                runner_ready=prepare_retry,
            )
            self.assertEqual(retry["status"], automation.STATUS_SUCCESS)
            self.assertTrue(Path(retry["image_path"]).is_file())
            updated = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["jobs"][0]["vmd_status"], "success")
            self.assertEqual(updated["drawing_retries"][-1]["status"], "success")

    def test_input_directory_mode_places_results_next_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "source"
            source_dir.mkdir()
            input_file = source_dir / "sample.fchk"
            input_file.write_text("wavefunction", encoding="utf-8")
            plan = automation.create_automation_plan(
                [input_file],
                "surface_esp",
                root / "runs",
                self._settings(output_location="input_directory"),
            )
            self.assertEqual(plan.jobs[0].result_dir, source_dir.resolve())

    def test_successful_trial_can_continue_without_rerunning_first_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.fch"
            second = root / "second.fch"
            first.write_text("wavefunction", encoding="utf-8")
            second.write_text("wavefunction", encoding="utf-8")

            trial_plan = automation.create_automation_plan(
                [first], "surface_esp", root / "runs", self._settings(), prefix="trial"
            )
            trial_result = FakeProcessRunner(
                trial_plan, Path(sys.executable), Path(sys.executable)
            ).run()
            self.assertEqual(trial_result["success"], 1)
            first_image = Path(trial_plan.jobs[0].image_path)
            first_image_mtime = first_image.stat().st_mtime_ns

            resumed_plan = automation.resume_automation_plan(
                trial_plan.manifest_path, [second], self._settings()
            )
            self.assertTrue(resumed_plan.resume)
            self.assertEqual(len(resumed_plan.jobs), 2)
            self.assertEqual(resumed_plan.jobs[0].status, automation.STATUS_SUCCESS)
            resumed_result = FakeProcessRunner(
                resumed_plan, Path(sys.executable), Path(sys.executable)
            ).run()

            self.assertEqual(resumed_result["success"], 2)
            self.assertEqual(first_image.stat().st_mtime_ns, first_image_mtime)
            self.assertEqual(resumed_plan.jobs[1].status, automation.STATUS_SUCCESS)
            manifest = json.loads(
                resumed_plan.manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["jobs"]), 2)

            with self.assertRaises(automation.AutomationValidationError):
                automation.resume_automation_plan(
                    resumed_plan.manifest_path,
                    [],
                    self._settings(rho_iso="0.002"),
                )

    def test_last_automatic_workflow_settings_round_trip_through_app_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original = core.CONFIG_FILE
            core.CONFIG_FILE = Path(temp) / "config.json"
            try:
                settings = self._settings(width=2400, height=1800)
                core.save_config(
                    {
                        "automatic_output_dir": str(Path(temp) / "runs"),
                        "automatic_workflow_settings": settings,
                    }
                )
                loaded = core.load_config()
                self.assertEqual(loaded["automatic_workflow_settings"]["width"], 2400)
                self.assertEqual(
                    loaded["automatic_workflow_settings"]["style_snapshot"]["style"]["id"],
                    "esp_e3_bwr_edgyglass_443",
                )
            finally:
                core.CONFIG_FILE = original


if __name__ == "__main__":
    unittest.main()
