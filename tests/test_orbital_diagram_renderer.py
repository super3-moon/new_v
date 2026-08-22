from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication

import orbital_diagram_renderer as renderer


class OrbitalDiagramRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _paper_like_values(root: Path) -> list[dict]:
        # These are the frontier energies of the user-provided Gaussian pair
        # and reproduce the levels shown in the reference publication figure.
        rows = [
            ("alpha", 266, -5.79483793, 1.0),
            ("alpha", 267, -5.73631459, 1.0),
            ("alpha", 268, -5.55506526, 1.0),
            ("alpha", 269, -2.55069234, 0.0),
            ("alpha", 270, -2.51117407, 0.0),
            ("beta", 266, -5.79483768, 1.0),
            ("beta", 267, -5.55506586, 1.0),
            ("beta", 268, -2.55069164, 0.0),
            ("beta", 269, -2.51117420, 0.0),
        ]
        result = []
        for number, (spin, index, energy, occupation) in enumerate(rows):
            path = root / f"{spin}_{index}.png"
            image = Image.new("RGB", (640, 360), "white")
            draw = ImageDraw.Draw(image)
            draw.line((110, 180, 530, 180), fill="#8c7a62", width=5)
            draw.ellipse((245, 92, 395, 268), fill="#d43b32" if number % 2 else "#2a927e")
            draw.ellipse((290, 120, 440, 240), fill="#2a927e" if number % 2 else "#d43b32")
            image.save(path)
            result.append(
                {
                    "key": f"{spin}:{index}",
                    "spin": spin,
                    "label": f"{spin}-{index}",
                    "energy": energy,
                    "occupation": occupation,
                    "image_path": path,
                }
            )
        return result

    def test_reference_energies_form_five_clean_levels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            values = self._paper_like_values(Path(temporary))
            layout = renderer.build_diagram_layout(
                values, image_aspect=640 / 360, show_title=False
            )
            self.assertEqual(len(layout.levels), 5)
            self.assertEqual(
                [round(level.energy, 2) for level in layout.levels],
                [-2.51, -2.55, -5.56, -5.74, -5.79],
            )
            self.assertTrue(
                all(left.y < right.y for left, right in zip(layout.levels, layout.levels[1:]))
            )
            self.assertGreater(layout.width / layout.height, 1.05)
            self.assertLess(layout.width / layout.height, 1.3)
            self.assertGreaterEqual(layout.energy_font_size / layout.width, 0.018)
            self.assertIsNone(layout.title_y)

            for spin in ("alpha", "beta"):
                lanes = [
                    round(item.connector[1].x(), 2)
                    for item in layout.placements
                    if item.spin == spin
                ]
                self.assertEqual(len(lanes), len(set(lanes)))

    def test_png_and_svg_share_borderless_publication_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = self._paper_like_values(root)
            result = renderer.render_orbital_energy_diagram(
                values,
                root / "diagram.png",
                root / "diagram.svg",
                output_width=1200,
            )
            self.assertTrue(result.png_path.is_file())
            self.assertTrue(result.svg_path.is_file())
            self.assertGreater(result.png_path.stat().st_size, 1000)
            self.assertGreater(result.svg_path.stat().st_size, 1000)
            with Image.open(result.png_path) as image:
                self.assertEqual(image.size, (result.width, result.height))
                self.assertEqual(image.getpixel((4, 4))[:3], (255, 255, 255))
            svg = result.svg_path.read_text(encoding="utf-8")
            self.assertGreaterEqual(svg.count("> MOs</text>"), 2)
            self.assertNotIn("��", svg)
            self.assertIn("-5.56 eV", svg)
            self.assertNotIn("rounded", svg.casefold())

    def test_connector_lanes_do_not_cross_for_compressed_energy_ranges(self) -> None:
        energies = {
            "alpha": [-8.69817, -7.79104, -7.14975, -7.01730, -1.25387, -1.21564],
            "beta": [-8.70635, -8.69817, -7.14975, -7.01730, -1.25387, -1.21564],
        }

        def proper_cross(first, second) -> bool:
            a, b = first
            c, d = second
            first_horizontal = abs(a.y() - b.y()) < 1.0e-6
            second_horizontal = abs(c.y() - d.y()) < 1.0e-6
            if first_horizontal == second_horizontal:
                return False
            horizontal = (a, b) if first_horizontal else (c, d)
            vertical = (c, d) if first_horizontal else (a, b)
            x = vertical[0].x()
            y = horizontal[0].y()
            epsilon = 1.0e-6
            return (
                min(horizontal[0].x(), horizontal[1].x()) + epsilon
                < x
                < max(horizontal[0].x(), horizontal[1].x()) - epsilon
                and min(vertical[0].y(), vertical[1].y()) + epsilon
                < y
                < max(vertical[0].y(), vertical[1].y()) - epsilon
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = []
            for spin, channel_energies in energies.items():
                for index, energy in enumerate(channel_energies, 1):
                    path = root / f"{spin}_{index}.png"
                    Image.new("RGB", (640, 360), "white").save(path)
                    values.append(
                        {
                            "key": f"{spin}:{index}",
                            "spin": spin,
                            "label": f"{spin}-{index}",
                            "energy": energy,
                            "occupation": 1.0,
                            "image_path": path,
                        }
                    )
            layout = renderer.build_diagram_layout(values, image_aspect=640 / 360)
            for spin in energies:
                placements = [item for item in layout.placements if item.spin == spin]
                for index, first in enumerate(placements):
                    first_segments = list(zip(first.connector, first.connector[1:]))
                    for second in placements[index + 1 :]:
                        second_segments = list(zip(second.connector, second.connector[1:]))
                        self.assertFalse(
                            any(
                                proper_cross(left, right)
                                for left in first_segments
                                for right in second_segments
                            ),
                            f"{first.key} and {second.key} connectors cross",
                        )


if __name__ == "__main__":
    unittest.main()
