from __future__ import annotations

import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

import vmd_style_tool as core


class CoreBehaviorTests(unittest.TestCase):
    def test_ai_normalization_handles_string_booleans_and_non_finite_numbers(self) -> None:
        guess = core.normalize_ai_style_guess(
            {
                "depthcue": "false",
                "lights": {"0": "on", "1": "off", "2": 1, "3": 0},
                "opacity": math.nan,
                "specular": math.inf,
            }
        )
        self.assertFalse(guess["depthcue"])
        self.assertEqual(
            guess["lights"], {"0": True, "1": False, "2": True, "3": False}
        )
        self.assertEqual(guess["opacity"], 1.0)
        self.assertEqual(guess["specular"], 0.20)

    def test_output_name_is_windows_safe(self) -> None:
        self.assertEqual(core._sanitize_output_name("CON", "style"), "_CON.cmd")
        self.assertEqual(
            core._sanitize_output_name("../bad:name?.cmd", "style"),
            "_bad_name_.cmd",
        )
        self.assertTrue(core._sanitize_output_name(". ", "abc").endswith(".cmd"))

    def test_multiwfn_path_candidates_prefer_latest_dated_folder(self) -> None:
        candidates = [
            r"E:\test\Multiwfn_2026.3.27_bin_Win64\Multiwfn.exe",
            r"E:\test\Multiwfn_2026.7.11_bin_Win64\Multiwfn.exe",
        ]
        ordered = sorted(
            candidates, key=core._multiwfn_candidate_key, reverse=True
        )
        self.assertIn("2026.7.11", ordered[0])

    def test_generated_script_is_filtered_portable_and_non_destructive(self) -> None:
        style = {
            "id": "test",
            "name": "100% demo\nexec hidden",
            "material": "Glossy; exec hidden",
            "pos_color": 12,
            "neg_color": 22,
            "sources": ["local%source"],
            "commands": [
                "display depthcue off",
                "exec hidden",
                "light 0 on\nexec hidden",
                "color Display Background white; exec hidden",
            ],
        }
        script = core.build_cmd_script(
            style, r"C:\Tools\%APP%\Multiwfn.exe", r"C:\Tools\VMD\vmd.exe"
        )
        self.assertIn("set AUTO_CUBE_FILE [file normalize $::env^(CUBE_FILE^)]", script)
        self.assertIn("display depthcue off", script)
        self.assertIn("light 0 on", script)
        executable_lines = [
            line for line in script.splitlines() if "echo #" not in line
        ]
        self.assertNotIn("exec hidden", "\n".join(executable_lines))
        self.assertNotIn("for %%E in (cub dat)", script)
        self.assertIn("Generated .cub/.dat files were preserved", script)
        self.assertIn(r"C:\Tools\%%APP%%\Multiwfn.exe", script)
        cube_lookup = next(
            line
            for line in script.splitlines()
            if "Get-ChildItem" in line and "latestStamp" in line
        )
        self.assertNotIn("^|", cube_lookup)
        self.assertNotIn(" | ", cube_lookup)
        self.assertIn("foreach ($file in Get-ChildItem", cube_lookup)
        self.assertIn("$file.LastWriteTimeUtc -ge $marker", cube_lookup)
        self.assertTrue(script.endswith("\r\n"))
        self.assertNotIn("\n", script.replace("\r\n", ""))

    def test_direct_and_exported_workflows_share_runtime_vmd_contract(self) -> None:
        style = {
            "id": "shared",
            "name": "Shared style",
            "material": "Glossy",
            "pos_color": 12,
            "neg_color": 22,
            "commands": ["display depthcue off"],
            "sources": ["test"],
        }
        tcl = core.build_vmd_tcl(style)
        script = core.build_cmd_script(
            style, r"C:\Tools\Multiwfn.exe", r"C:\Tools\VMD\vmd.exe"
        )
        self.assertIn("$::env(CUBE_FILE)", tcl)
        self.assertIn("$::env(ISO_NORM)", tcl)
        self.assertIn("$::env(A_DIR)", tcl)
        self.assertIn("file join $AUTO_OUTDIR $filenameOnly", tcl)
        self.assertIn("display depthcue off", tcl)
        self.assertIn("display depthcue off", script)

    def test_save_state_import_drops_tcl_control_flow(self) -> None:
        state = """# VMD script written by test
proc vmdrestoremymaterials {} {
  material change ambient Glossy 0.25
  exec calc.exe
}
mol representation CPK 0.8 0.3 22 22
mol color Name
mol material Opaque
mol addrep top
mol representation Isosurface 0.05 0 0 0 1 1
mol color ColorID 12
mol material Glossy
mol addrep top
"""
        style = core.parse_save_state_to_custom_style(state, "Imported", "")
        self.assertIn("material change ambient Glossy 0.25", style["commands"])
        self.assertFalse(any("exec" in command for command in style["commands"]))
        self.assertFalse(any("proc " in command for command in style["commands"]))

    def test_visual_parameter_extraction_reports_only_explicit_overrides(self) -> None:
        style = core.STYLE_BY_ID["soft_glossy_449"]
        parameters = core.extract_style_visual_parameters(style)
        self.assertEqual(parameters["material"], "Glossy")
        self.assertEqual(parameters["pos_color_id"], 12)
        self.assertEqual(parameters["neg_color_id"], 22)
        self.assertEqual(parameters["material_values"]["ambient"], 0.1)
        self.assertEqual(parameters["material_values"]["diffuse"], 0.6)
        self.assertEqual(parameters["material_values"]["opacity"], 0.75)
        self.assertIsNone(parameters["material_values"]["specular"])
        self.assertFalse(parameters["depthcue"])
        self.assertIsNone(parameters["projection"])
        self.assertTrue(parameters["lights"]["3"])
        self.assertIsNone(parameters["lights"]["0"])

    def test_manual_parameter_edit_creates_copy_and_preserves_unrelated_commands(self) -> None:
        source = core.STYLE_BY_ID["soft_glossy_449"]
        parameters = core.extract_style_visual_parameters(source)
        parameters["original_rep0_commands"] = parameters["rep0_commands"]
        edited = core.build_custom_style_from_visual_parameters(
            parameters, source, "Soft Glossy 自定义", "manual edit"
        )
        self.assertTrue(edited["is_custom"])
        self.assertNotEqual(edited["id"], source["id"])
        self.assertEqual(edited["material"], source["material"])
        self.assertIn("material change mirror Opaque 0.15", edited["commands"])
        self.assertIn("material change ambient Glossy 0.100000", edited["commands"])
        self.assertEqual(source["name"], "Soft Artistic Glossy")
        self.assertNotIn("code", source)

    def test_legacy_presets_are_unchanged_and_new_presets_are_appended(self) -> None:
        canonical = lambda value: json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical(core.ORIGINAL_SURFACE_STYLES)).hexdigest(),
            "0b8a3162eeaecf45cca3a73e613fa29a126fb18e74d22dc054ef2fb7ddbf7a82",
        )
        self.assertEqual(
            hashlib.sha256(canonical(core.ORIGINAL_SKELETON_STYLES)).hexdigest(),
            "8cc70caa8a512f58983ce50b5740dd537ee90d36e698a7630079f6b73baa700c",
        )
        self.assertEqual(
            [style["id"] for style in core.STYLES[:10]],
            [
                "classic_glossy_447",
                "soft_glossy_449",
                "edgyglass_overlap_483",
                "bright_bule_yellow_userpack",
                "modern_cool_palette_userpack",
                "edgyglass_tuned_443",
                "goodsell_58009",
                "edgy_58009",
                "translucent_clean_447",
                "rdg_clarity_291",
            ],
        )
        self.assertEqual(
            [style["id"] for style in core.STYLES[10:]],
            list(core._APPENDED_STYLE_IDS),
        )
        self.assertEqual(
            [style["id"] for style in core.SKELETON_STYLES[:4]],
            [
                "skeleton_default_opaque",
                "skeleton_tan_opaque_449",
                "skeleton_goodsell_58009",
                "skeleton_edgy_58009",
            ],
        )
        self.assertEqual(
            [style["id"] for style in core.SKELETON_STYLES[4:]],
            list(core._NEW_SKELETON_IDS),
        )
        self.assertEqual(
            core.DUPLICATES,
            [{"removed": "classic_glossy_483", "kept": "classic_glossy_447"}],
        )
        self.assertEqual(
            len({core._style_signature(style) for style in core.STYLES}),
            len(core.STYLES),
        )

    def test_selected_codes_reuse_legacy_density_styles_without_duplicates(self) -> None:
        self.assertEqual(
            core.DENSITY_DIFFERENCE_STYLE_MAP,
            {
                "D1": "density_d1_green_blue_glossy",
                "D2": "classic_glossy_447",
                "D3": "soft_glossy_449",
                "D4": "edgyglass_overlap_483",
                "D5": "bright_bule_yellow_userpack",
                "D6": "density_d6_green_orange_diffuse",
                "D7": "modern_cool_palette_userpack",
            },
        )
        self.assertEqual(
            {
                style["code"]: style["image"]
                for style in core.NEW_SURFACE_STYLES
                if str(style.get("code", "")).startswith("E")
            },
            {
                "E1": "04_isosurface_iso_tachyon.png",
                "E2": "02_color_mapped_transparent.jpg",
                "E3": "05_edgyglass_tuned_opacity.jpg",
                "E4": "06_turbo_colorscale_edgyglass.jpg",
                "E5": "03_iso2_overlap_cube.png",
                "E7": "esp_e7_wireframe_reference.png",
            },
        )

    def test_esp_style_maps_a_second_volume_to_one_density_isosurface(self) -> None:
        style = core.STYLE_BY_ID["esp_e1_bwr_glossy"]
        tcl = core.build_vmd_tcl(style)
        self.assertIn("$::env(COLOR_CUBE_FILE)", tcl)
        self.assertIn("mol addfile $AUTO_COLOR_CUBE_FILE type cube waitfor all", tcl)
        self.assertIn("mol modcolor 1 top Volume 1", tcl)
        self.assertIn("mol scaleminmax top 1 -0.03 0.03", tcl)
        self.assertNotIn("set negiso", tcl)
        self.assertEqual(
            len([line for line in tcl.splitlines() if line.startswith("mol modstyle") and " Isosurface " in line]),
            1,
        )
        script = core.build_cmd_script(
            style, r"C:\Tools\Multiwfn.exe", r"C:\Tools\VMD\vmd.exe"
        )
        self.assertIn('set "COLOR_CUBE_FILE="', script)
        self.assertIn("density|dens|rho", script)
        self.assertIn("totesp|esp|mep|potential|electrostatic", script)
        self.assertIn("press Enter for 0.001", script)

    def test_wireframe_and_turbo_presets_are_vmd_193_compatible(self) -> None:
        wireframe = core.build_vmd_tcl(core.STYLE_BY_ID["esp_e7_bwr_wireframe"])
        self.assertIn("Isosurface $AUTO_ISOVAL 0 0 1 3 1", wireframe)

        turbo = core.build_vmd_tcl(core.STYLE_BY_ID["esp_e4_turbo_edgyglass_443"])
        self.assertNotIn("color scale method turbo", turbo.lower())
        self.assertIn("color scale method RGB", turbo)
        self.assertIn("$AUTO_TURBO_I < 1024", turbo)
        self.assertIn("color change rgb [expr {33 + $AUTO_TURBO_I}]", turbo)
        self.assertEqual(len(core._turbo_vmd_commands()), 12)
        turbo_cmd = core.build_cmd_script(
            core.STYLE_BY_ID["esp_e4_turbo_edgyglass_443"],
            r"C:\Tools\Multiwfn.exe",
            r"C:\Tools\VMD\vmd.exe",
        )
        self.assertLess(max(len(line) for line in turbo_cmd.splitlines()), 8191)
        self.assertNotIn('> "%TCL_FILE%" (', turbo_cmd)

    def test_esp_pair_detection_and_cube_grid_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            density = root / "density1.cub"
            esp = root / "ESP1.cub"
            header = (
                "cube title\n"
                "generated for test\n"
                " 1 0.0 0.0 0.0\n"
                " 2 0.5 0.0 0.0\n"
                " 2 0.0 0.5 0.0\n"
                " 2 0.0 0.0 0.5\n"
                " 1 0.0 0.0 0.0 0.0\n"
            )
            density.write_text(header, encoding="utf-8")
            esp.write_text(header, encoding="utf-8")
            self.assertEqual(core.find_esp_cube_pair(esp), (density.resolve(), esp.resolve()))
            self.assertTrue(core.cube_grids_compatible(density, esp))

            mismatch = root / "ESP2.cub"
            mismatch.write_text(header.replace(" 2 0.0 0.5 0.0", " 3 0.0 0.5 0.0"), encoding="utf-8")
            self.assertEqual(
                core.find_esp_cube_pair(density),
                (density.resolve(), esp.resolve()),
            )
            self.assertFalse(core.cube_grids_compatible(density, mismatch))

    def test_manual_edit_of_esp_style_preserves_mapping_contract(self) -> None:
        source = core.STYLE_BY_ID["esp_e3_bwr_edgyglass_443"]
        parameters = core.extract_style_visual_parameters(source)
        parameters["color_scale_min"] = -0.04
        parameters["color_scale_max"] = 0.04
        edited = core.build_custom_style_from_visual_parameters(
            parameters, source, "ESP custom", "mapping edit"
        )
        self.assertEqual(edited["surface_mode"], "volume_mapped")
        self.assertEqual(edited["color_scale_min"], -0.04)
        self.assertEqual(edited["color_scale_max"], 0.04)
        self.assertIn("mol modcolor 1 top Volume 1", core.build_vmd_tcl(edited))


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_custom = core.CUSTOM_STYLES_FILE
        self.original_config = core.CONFIG_FILE
        core.CUSTOM_STYLES_FILE = self.root / "styles.json"
        core.CONFIG_FILE = self.root / "config.json"

    def tearDown(self) -> None:
        core.CUSTOM_STYLES_FILE = self.original_custom
        core.CONFIG_FILE = self.original_config
        core.CUSTOM_STYLES_LOAD_ERROR = ""
        self.temp_dir.cleanup()

    def test_atomic_custom_style_round_trip_and_corruption_guard(self) -> None:
        styles = [{"id": "custom_a", "name": "A"}]
        core.save_custom_styles(styles)
        self.assertEqual(core.load_custom_styles(strict=True), styles)
        self.assertFalse(list(self.root.glob("*.tmp")))

        core.CUSTOM_STYLES_FILE.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "写入已取消"):
            core.upsert_custom_style({"id": "custom_b", "name": "B"})
        self.assertEqual(core.CUSTOM_STYLES_FILE.read_text(encoding="utf-8"), "{broken")

    def test_partial_config_update_preserves_other_settings(self) -> None:
        core.save_config({"theme": "dark", "output_dir": str(self.root)})
        core.save_config({"mode": "split"})
        data = json.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["output_dir"], str(self.root))
        self.assertEqual(data["mode"], "split")


if __name__ == "__main__":
    unittest.main()
