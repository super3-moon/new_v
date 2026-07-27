from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import multiwfn_batch as batch


class BatchPresetTests(unittest.TestCase):
    def test_builtin_presets_are_valid_and_versioned(self) -> None:
        presets = batch.builtin_presets()
        self.assertGreaterEqual(len(presets), 3)
        self.assertEqual(len({preset.id for preset in presets}), len(presets))
        self.assertTrue(
            all(
                preset.multiwfn_version == batch.CURRENT_MULTIWFN_VERSION
                for preset in presets
            )
        )

    def test_template_rendering_requires_declared_values(self) -> None:
        self.assertEqual(
            batch.render_template("${stem}_${index}.xyz", {"stem": "H2O", "index": 2}),
            "H2O_2.xyz",
        )
        with self.assertRaisesRegex(batch.BatchValidationError, "missing"):
            batch.render_template("${missing}", {})

    def test_custom_preset_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "presets.json"
            preset = batch.BatchPreset(
                id="user_demo",
                name="Demo",
                description="test",
                input_extensions=["fch"],
                arguments=["-isilent", "1"],
                stdin_template="${choice}\nq\n",
                output_rules=[batch.OutputRule("stdout.log", "${stem}.txt")],
                variables={"choice": "1"},
            )
            batch.save_user_presets(path, [preset])
            loaded = batch.load_user_presets(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].to_dict(), preset.to_dict())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)

    def test_command_text_import_preserves_blank_lines_and_common_encodings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            utf8 = root / "commands_utf8.txt"
            utf8.write_text("5\n\n2\nq\n", encoding="utf-8-sig")
            self.assertEqual(batch.read_command_text_file(utf8), "5\n\n2\nq\n")

            gbk = root / "commands_gbk.txt"
            gbk.write_bytes("1\n文件.cub\n0\nq\n".encode("gb18030"))
            self.assertEqual(
                batch.read_command_text_file(gbk), "1\n文件.cub\n0\nq\n"
            )


class BatchRunnerTests(unittest.TestCase):
    def _fake_preset(self, output_pattern: str = "fixed.out") -> batch.BatchPreset:
        return batch.BatchPreset(
            id="fake",
            name="Fake runner",
            description="",
            input_extensions=[".py"],
            arguments=[],
            stdin_template="make\n",
            output_rules=[batch.OutputRule(output_pattern, "${stem}_result.txt")],
            timeout_seconds=10,
            multiwfn_version=batch.CURRENT_MULTIWFN_VERSION,
        )

    def test_runner_isolates_job_and_collects_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "fake_multiwfn.py"
            fake.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "commands = sys.stdin.read()\n"
                f"print('Version {batch.CURRENT_MULTIWFN_VERSION}')\n"
                "if 'make' in commands:\n"
                "    Path('fixed.out').write_text('ok', encoding='utf-8')\n",
                encoding="utf-8",
            )
            events: list[dict] = []
            plan = batch.create_batch_plan([fake], self._fake_preset(), root / "runs")
            runner = batch.MultiwfnBatchRunner(
                plan, Path(sys.executable), event_callback=events.append
            )
            result = runner.run()
            self.assertEqual(result["status"], batch.STATUS_SUCCESS)
            self.assertEqual(result["success"], 1)
            self.assertEqual(
                plan.detected_multiwfn_version, batch.CURRENT_MULTIWFN_VERSION
            )
            self.assertTrue((plan.results_dir / "fake_multiwfn_result.txt").is_file())
            self.assertTrue((plan.jobs[0].work_dir / "stdin.txt").is_file())
            self.assertTrue(plan.manifest_path.is_file())
            self.assertTrue(plan.summary_path.is_file())
            self.assertTrue(any(event["kind"] == "batch_finished" for event in events))

    def test_missing_required_output_marks_job_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "fake_multiwfn.py"
            fake.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
            plan = batch.create_batch_plan(
                [fake], self._fake_preset("missing.out"), root / "runs"
            )
            result = batch.MultiwfnBatchRunner(plan, Path(sys.executable)).run()
            self.assertEqual(result["status"], batch.STATUS_FAILED)
            self.assertEqual(plan.jobs[0].status, batch.STATUS_FAILED)
            self.assertIn("缺少预期输出", plan.jobs[0].error)

    def test_duplicate_stems_receive_unique_result_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = []
            for folder_name in ("a", "b"):
                folder = root / folder_name
                folder.mkdir()
                fake = folder / "same.py"
                fake.write_text(
                    "from pathlib import Path\n"
                    "import sys\n"
                    "sys.stdin.read()\n"
                    "Path('fixed.out').write_text('ok', encoding='utf-8')\n",
                    encoding="utf-8",
                )
                inputs.append(fake)
            plan = batch.create_batch_plan(inputs, self._fake_preset(), root / "runs")
            result = batch.MultiwfnBatchRunner(plan, Path(sys.executable)).run()
            self.assertEqual(result["success"], 2)
            self.assertEqual(
                {path.name for path in plan.results_dir.iterdir()},
                {
                    "same_result.txt",
                    "same_result_2.txt",
                    "same_Multiwfn.log",
                    "same_Multiwfn_2.log",
                },
            )

    def test_stdout_is_always_collected_without_output_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "log_only.py"
            fake.write_text(
                "import sys\nsys.stdin.read()\nprint('finished')\n", encoding="utf-8"
            )
            preset = self._fake_preset()
            preset.output_rules = []
            plan = batch.create_batch_plan([fake], preset, root / "runs")
            result = batch.MultiwfnBatchRunner(plan, Path(sys.executable)).run()
            self.assertEqual(result["status"], batch.STATUS_SUCCESS)
            self.assertTrue((plan.results_dir / "log_only_Multiwfn.log").is_file())

    def test_wildcard_outputs_keep_descriptive_source_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "molecule.py"
            fake.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "Path('density.cub').write_text('rho', encoding='utf-8')\n"
                "Path('totesp.cub').write_text('esp', encoding='utf-8')\n",
                encoding="utf-8",
            )
            preset = self._fake_preset()
            preset.output_rules = [batch.OutputRule("*.cub", "", False)]
            plan = batch.create_batch_plan([fake], preset, root / "runs")
            result = batch.MultiwfnBatchRunner(plan, Path(sys.executable)).run()
            self.assertEqual(result["status"], batch.STATUS_SUCCESS)
            self.assertEqual(
                {path.name for path in plan.results_dir.iterdir()},
                {
                    "molecule_density.cub",
                    "molecule_totesp.cub",
                    "molecule_Multiwfn.log",
                },
            )

    def test_scan_respects_extensions_and_recursion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.fch").write_text("", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "b.fchk").write_text("", encoding="utf-8")
            (nested / "ignore.txt").write_text("", encoding="utf-8")
            shallow = batch.scan_input_files([root], [".fch", ".fchk"], recursive=False)
            deep = batch.scan_input_files([root], [".fch", ".fchk"], recursive=True)
            self.assertEqual([path.name for path in shallow], ["a.fch"])
            self.assertEqual({path.name for path in deep}, {"a.fch", "b.fchk"})


if __name__ == "__main__":
    unittest.main()
