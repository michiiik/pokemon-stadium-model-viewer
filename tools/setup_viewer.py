#!/usr/bin/env python3
"""Create a local viewer config and optionally prepare user-owned ROM assets.

The generated config and extraction cache are intentionally external to Git.
This helper never copies a ROM into the repository and never uploads anything.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))
from stadium1_viewer import VIEWER_CONFIG_FORMAT, load_viewer_config  # noqa: E402
from stadium2_extract import extract  # noqa: E402


def default_cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "pokemon-stadium-model-viewer" / "stadium2"


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def existing_provider_paths(config_path: Path) -> Dict[str, str]:
    if not config_path.is_file():
        return {}
    return load_viewer_config(config_path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Configure external assets and optionally prepare user-owned Stadium ROMs"
    )
    stadium1 = parser.add_mutually_exclusive_group()
    stadium1.add_argument("--stadium1-assets", help="external extracted Stadium 1 pokemon_models directory")
    stadium1.add_argument("--stadium1-rom", help="user-owned Stadium 1 .z64/.n64/.v64/.rom image")
    stadium2 = parser.add_mutually_exclusive_group()
    stadium2.add_argument("--stadium2-rom", help="user-owned Stadium 2 ROM or extracted model-bank input")
    stadium2.add_argument("--stadium2-cache", help="existing external Stadium 2 extraction cache")
    parser.add_argument("--cache-dir", help="external output directory for a new Stadium 2 extraction")
    parser.add_argument("--config", default="viewer.local.json", help="ignored local config to write")
    parser.add_argument("--force", action="store_true", help="refresh an existing Stadium 2 cache")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config).expanduser().resolve()
    try:
        paths = existing_provider_paths(config_path)
        kinds: Dict[str, str] = {}
        if args.stadium1_rom:
            paths["stadium1"] = str(Path(args.stadium1_rom).expanduser().resolve())
            kinds["stadium1"] = "rom"
        elif args.stadium1_assets:
            paths["stadium1"] = str(Path(args.stadium1_assets).expanduser().resolve())
            kinds["stadium1"] = "assets"

        if args.stadium2_rom:
            cache_dir = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else default_cache_dir().resolve()
            if is_inside(cache_dir, repo_root):
                raise ValueError("Stadium 2 extraction cache must be outside the Git repository")
            manifest = extract(Path(args.stadium2_rom).expanduser().resolve(), cache_dir, args.force)
            print(
                f"Extracted Stadium 2: {manifest['modelCount']} models, "
                f"{manifest['poseRecordCount']} pose records -> {cache_dir}"
            )
            paths["stadium2"] = str(cache_dir)
            kinds["stadium2"] = "cache"
        elif args.stadium2_cache:
            paths["stadium2"] = str(Path(args.stadium2_cache).expanduser().resolve())
            kinds["stadium2"] = "cache"
        elif args.cache_dir:
            raise ValueError("--cache-dir requires --stadium2-rom")

        if not paths:
            raise ValueError(
                "provide --stadium1-assets, --stadium1-rom, --stadium2-rom, or --stadium2-cache"
            )
        config = {
            "format": VIEWER_CONFIG_FORMAT,
            "providers": {
                provider: {kinds.get(provider, "assets"): path}
                for provider, path in sorted(paths.items())
                if provider in ("stadium1", "stadium2")
            },
        }
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote local config: {config_path}")
        print("The config is ignored by Git; keep ROMs and extraction caches outside this repository.")
        return 0
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
