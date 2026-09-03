#!/usr/bin/env python3
"""Tests for local provider references and privacy behavior."""

from __future__ import annotations

import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import stadium1_viewer as viewer


class ViewerConfigTests(unittest.TestCase):
    def test_relative_provider_paths_resolve_from_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config" / "viewer.local.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps({
                "format": viewer.VIEWER_CONFIG_FORMAT,
                "providers": {
                    "stadium1": {"rom": "../assets/stadium1.z64"},
                    "stadium2": {"cache": "../assets/stadium2"},
                },
            }), encoding="utf-8")
            paths = viewer.load_viewer_config(config_path)
            self.assertEqual(paths["stadium1"], str((config_path.parent / "../assets/stadium1.z64").resolve()))
            self.assertEqual(paths["stadium2"], str((config_path.parent / "../assets/stadium2").resolve()))

    def test_stadium1_rom_provider_reads_source_archive_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rom_path = Path(temporary) / "fixture.z64"
            archive_start = viewer.STADIUM1_MODEL_ARCHIVE_ROM_START
            archive = bytearray(0x24)
            struct.pack_into(">2I", archive, 8, len(archive), 1)
            struct.pack_into(">4I", archive, 0x10, 0x20, 4, 0, 0)
            archive[0x20:0x24] = b"TEST"
            with rom_path.open("wb") as handle:
                handle.seek(viewer.STADIUM1_MODEL_ARCHIVE_ROM_END - 1)
                handle.write(b"\0")
                handle.seek(0)
                handle.write(viewer.N64_ROM_MAGIC_Z64)
                handle.seek(archive_start)
                handle.write(archive)
            provider = viewer.Stadium1RomDataProvider(rom_path)
            self.assertEqual(provider._model_archive["fileCount"], 1)
            self.assertEqual(provider._archive_blob[0x20:0x24], b"TEST")

    def test_n64_byte_orders_normalize_to_z64(self) -> None:
        z64 = viewer.N64_ROM_MAGIC_Z64 + b"ABCD"
        v64 = viewer.N64_ROM_MAGIC_V64 + b"BADC"
        n64 = viewer.N64_ROM_MAGIC_N64 + b"DCBA"
        self.assertEqual(viewer.normalize_n64_rom(z64), z64)
        self.assertEqual(viewer.normalize_n64_rom(v64), z64)
        self.assertEqual(viewer.normalize_n64_rom(n64), z64)

    def test_health_does_not_expose_provider_root(self) -> None:
        class DummyProvider:
            game_id = "stadium1"
            root = Path("C:/private/user/roms")
            diagnostics = []

            @staticmethod
            def catalog():
                return [{"kind": "model"}]

        result = viewer.health(DummyProvider())
        self.assertNotIn("root", result)
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
