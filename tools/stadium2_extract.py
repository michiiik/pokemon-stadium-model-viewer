#!/usr/bin/env python3
"""Extract Stadium 2 model and pose records into an external viewer cache.

The cache contains decoded FRAGMENT model payloads, so the viewer can build
its catalog without reopening the ROM or decompressing all 282 model records.
Pose records are preserved byte-for-byte for later format work; they are not
claimed to be playable animations yet.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from stadium1_viewer import (  # noqa: E402
    FormatError,
    STADIUM2_EXTRACT_FORMAT,
    Stadium2DataProvider,
    decode_yay0,
    inspect_stadium2_pose_metadata,
    parse_stadium2_indexed_archive,
    parse_resource,
    unpack_persszp,
)


def decode_model_record(raw: bytes) -> Tuple[bytes, str]:
    """Return the unwrapped model payload and the source wrapper name."""
    if raw[:8] == b"PERS-SZP":
        decoded, _ = unpack_persszp(raw)
        return decoded, "PERS-SZP"
    if raw[:4] == b"Yay0":
        return decode_yay0(raw), "Yay0"
    return raw, "raw"


def model_summary(parsed: Dict[str, Any]) -> Dict[str, Any]:
    skeleton = parsed.get("skeleton")
    bones = skeleton.get("bones", []) if isinstance(skeleton, dict) else []
    return {
        "modelId": parsed.get("modelId"),
        "meshCount": len(parsed.get("meshes", [])),
        "textureCount": len(parsed.get("textures", [])),
        "boneCount": len(bones),
        "diagnostics": list(parsed.get("diagnostics", [])),
    }


def extract(source: Path, output: Path, force: bool = False) -> Dict[str, Any]:
    provider = Stadium2DataProvider(source)
    if provider._extracted_manifest is not None:
        raise FormatError("the input is already a Stadium 2 extraction cache; pass a ROM or extracted bank")
    if provider._model_archive is None or provider._model_blob is None:
        raise FormatError("the Stadium 2 model bank could not be read")
    if provider._pose_archive is None or provider._pose_blob is None:
        raise FormatError("the Stadium 2 pose bank could not be read")
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and not force:
        raise FileExistsError(f"{manifest_path} already exists; use --force to refresh this cache")

    output.mkdir(parents=True, exist_ok=True)
    models_dir = output / "models"
    poses_dir = output / "poses"
    models_dir.mkdir(exist_ok=True)
    poses_dir.mkdir(exist_ok=True)

    model_records: List[Dict[str, Any]] = []
    for item in provider._model_archive["files"]:
        index = int(item["index"])
        raw = provider._model_blob[item["offset"]:item["offset"] + item["size"]]
        decoded, wrapper = decode_model_record(raw)
        relative = Path("models") / f"{index:03d}.fragment"
        (output / relative).write_bytes(decoded)
        parsed = parse_resource(decoded, f"Stadium 2 model {index:03d}", catalog_only=True)
        summary = model_summary(parsed)
        model_records.append({
            "index": index,
            "path": relative.as_posix(),
            "size": len(decoded),
            "sourceOffset": int(item["offset"]),
            "sourceSize": int(item["size"]),
            "wrapper": wrapper,
            "modelId": summary.pop("modelId") if summary.get("modelId") is not None else index,
            "poseCount": provider._pose_counts[index] if index < len(provider._pose_counts) else 0,
            **summary,
        })

    pose_groups: List[Dict[str, Any]] = []
    pose_record_count = 0
    for group_item in provider._pose_archive["files"]:
        group_index = int(group_item["index"])
        group_blob = provider._pose_blob[group_item["offset"]:group_item["offset"] + group_item["size"]]
        group_entry: Dict[str, Any] = {
            "index": group_index,
            "sourceOffset": int(group_item["offset"]),
            "sourceSize": int(group_item["size"]),
            "records": [],
        }
        # Keep this call explicit rather than relying on the provider's count
        # helper so the cache retains every raw pose record.
        try:
            nested = parse_stadium2_indexed_archive(group_blob, f"pose group {group_index}")
            group_dir = poses_dir / f"{group_index:03d}"
            group_dir.mkdir(exist_ok=True)
            for record in nested["files"]:
                pose_index = int(record["index"])
                relative = Path("poses") / f"{group_index:03d}" / f"{pose_index:03d}.bin"
                pose_blob = group_blob[record["offset"]:record["offset"] + record["size"]]
                (output / relative).write_bytes(pose_blob)
                group_entry["records"].append({
                    "index": pose_index,
                    "path": relative.as_posix(),
                    "size": int(record["size"]),
                    "sourceOffset": int(group_item["offset"] + record["offset"]),
                    "sourceSize": int(record["size"]),
                    "metadata": inspect_stadium2_pose_metadata(
                        pose_blob, f"pose group {group_index} record {pose_index}"
                    ),
                })
                pose_record_count += 1
        except (FormatError, OSError) as exc:
            group_entry["diagnostic"] = str(exc)
        pose_groups.append(group_entry)

    model_records.sort(key=lambda item: item["index"])
    manifest = {
        "format": STADIUM2_EXTRACT_FORMAT,
        "provider": "stadium2",
        "sourceName": source.name,
        "modelCount": len(model_records),
        "poseGroupCount": len(pose_groups),
        "poseRecordCount": pose_record_count,
        "models": model_records,
        "poseGroups": pose_groups,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Stadium 2 model and pose records for the standalone viewer")
    parser.add_argument("--assets", required=True, help="full S2 ROM, 437610.bin, or extracted S2 bank directory")
    parser.add_argument("--output", required=True, help="external cache directory; do not place it in the git repo")
    parser.add_argument("--force", action="store_true", help="refresh an existing manifest and generated records")
    args = parser.parse_args()
    try:
        manifest = extract(Path(args.assets).resolve(), Path(args.output).resolve(), args.force)
    except (FileNotFoundError, OSError, FormatError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "modelCount": manifest["modelCount"],
        "poseGroupCount": manifest["poseGroupCount"],
        "poseRecordCount": manifest["poseRecordCount"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
