#!/usr/bin/env python3
"""Print structural Stadium 2 model/pose-bank inventory as JSON.

This tool reads an external ROM or extracted bank and emits only archive
metadata plus decoded model summaries. It never writes extracted resources.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from stadium1_viewer import Stadium2DataProvider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory the external Stadium 2 model and pose banks")
    parser.add_argument("--assets", required=True, help="full S2 ROM, 437610.bin, or extracted bank directory")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    args = parser.parse_args()

    provider = Stadium2DataProvider(Path(args.assets))
    catalog = provider.catalog()
    result = {
        "provider": provider.game_id,
        "root": str(provider.root),
        "diagnostics": provider.diagnostics,
        "modelCount": len(catalog),
        "poseRecordCount": sum(len(item.get("animations", [])) for item in catalog),
        "supportedAnimationCount": sum(
            1 for item in catalog for animation in item.get("animations", []) if animation.get("supported")
        ),
        "models": [
            {
                "index": item.get("s2ModelIndex"),
                "name": item.get("name"),
                "size": item.get("size"),
                "modelId": item.get("modelId"),
                "poseCount": len(item.get("animations", [])),
                "diagnosticCodes": sorted({d.get("code") for d in item.get("diagnostics", [])}),
            }
            for item in catalog
        ],
    }
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
