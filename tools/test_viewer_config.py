#!/usr/bin/env python3
"""Tests for the local-only provider path configuration."""

from __future__ import annotations

import json
from pathlib import Path
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
                    "stadium1": {"assets": "../assets/stadium1"},
                    "stadium2": {"cache": "../assets/stadium2"},
                },
            }), encoding="utf-8")
            paths = viewer.load_viewer_config(config_path)
            self.assertEqual(paths["stadium1"], str((config_path.parent / "../assets/stadium1").resolve()))
            self.assertEqual(paths["stadium2"], str((config_path.parent / "../assets/stadium2").resolve()))

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
