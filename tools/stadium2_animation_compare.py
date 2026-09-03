#!/usr/bin/env python3
"""Compare shared Stadium 1/S2 model animation metadata.

S1 curves are parsed from the model fragments; S2 pose records are read from
the external extraction manifest and their trailers are compared by
channel/frame count.  With --verify, each S2 record is additionally decoded
through the shared curve evaluator and its payload streams are byte-compared
against the matching S1 curve, which is the evidence that S2 pose records are
the Stadium 1 curve format re-containerized.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import struct
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from stadium1_viewer import (  # noqa: E402
    Stadium1DataProvider,
    Stadium2DataProvider,
    inspect_stadium2_pose_metadata,
    parse_resource,
    parse_stadium2_pose,
    pointer_to_offset,
)


def unique_values(values: Iterable[int]) -> List[int]:
    return list(dict.fromkeys(values))


def relation(s1_frames: List[int], s2_frames: List[int]) -> str:
    distinct = unique_values(s1_frames)
    if s2_frames == s1_frames:
        return "exact-frame-sequence"
    if s2_frames == distinct:
        return "s1-sequence-with-duplicate-frames-removed"
    if s2_frames[:len(distinct)] == distinct:
        return "shared-prefix-plus-s2-poses"
    if all(frame in s1_frames for frame in s2_frames):
        return "s1-frame-subset-or-reordered"
    return "different-or-s2-specific"


def compare_species(s1: Stadium1DataProvider, s2_catalog: List[Dict[str, Any]], species: int) -> Dict[str, Any]:
    blob, _ = s1._load_blob(f"{species - 1}.bin")
    parsed = parse_resource(blob, f"Stadium 1 species {species}", catalog_only=True)
    s1_animations = [item for item in parsed.get("animations", []) if item.get("supported")]
    s1_frames = [int(item.get("frameCount", 0)) for item in s1_animations]
    s1_channels = unique_values([
        int(item.get("curve", {}).get("channelCount"))
        for item in s1_animations
        if item.get("curve", {}).get("channelCount") is not None
    ])
    entry = next(item for item in s2_catalog if item.get("s2ModelIndex") == species)
    s2_animations = entry.get("animations", [])
    s2_frames = [int(item.get("frameCount", 0)) for item in s2_animations]
    s2_channels = unique_values([
        int(item.get("metadata", {}).get("channelCount"))
        for item in s2_animations
        if item.get("metadata", {}).get("channelCount") is not None
    ])
    return {
        "species": species,
        "s1": {"frameCounts": s1_frames, "channelCounts": s1_channels, "animationCount": len(s1_frames)},
        "s2": {"frameCounts": s2_frames, "channelCounts": s2_channels, "poseCount": len(s2_animations)},
        "relation": relation(s1_frames, s2_frames),
        "channelCountMatch": bool(s1_channels) and s2_channels == s1_channels,
    }


def compare(s1_assets: Path, s2_cache: Path, species: Iterable[int]) -> Dict[str, Any]:
    s1 = Stadium1DataProvider(s1_assets)
    s2 = Stadium2DataProvider(s2_cache)
    s2_catalog = s2.catalog()
    rows = [compare_species(s1, s2_catalog, value) for value in species]
    return {
        "s1Assets": str(s1_assets.resolve()),
        "s2Cache": str(s2_cache.resolve()),
        "speciesCount": len(rows),
        "relationCounts": dict(Counter(row["relation"] for row in rows)),
        "channelMatchCount": sum(1 for row in rows if row["channelCountMatch"]),
        "rows": rows,
    }


def _u8(data: bytes, offset: int) -> int:
    return data[offset]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _s16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">h", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def s1_curve_streams(blob: bytes, curve_offset: int) -> Tuple[int, int, int, bytes, bytes, bytes, bytes]:
    """Return (flags, channels, frames, descriptor table, scale, rotation, position) bytes."""
    flags = _s16(blob, curve_offset)
    channel_count = _u16(blob, curve_offset + 8)
    frame_count = _u16(blob, curve_offset + 0x0A)
    descriptor_ptr = pointer_to_offset(_u32(blob, curve_offset + 0x0C), len(blob))
    scale_ptr = pointer_to_offset(_u32(blob, curve_offset + 0x10), len(blob))
    rotation_ptr = pointer_to_offset(_u32(blob, curve_offset + 0x14), len(blob))
    position_ptr = pointer_to_offset(_u32(blob, curve_offset + 0x18), len(blob))
    if descriptor_ptr is None:
        return (flags, channel_count, frame_count, b"", b"", b"", b"")
    descriptor_bytes = blob[descriptor_ptr:descriptor_ptr + channel_count * 10]
    scale_size = rotation_size = position_size = 0
    for channel in range(channel_count):
        at = descriptor_ptr + channel * 10
        scale_len, rot_len, pos_len, channel_flags = (_u8(blob, at), _u8(blob, at + 1), _u8(blob, at + 2), _u8(blob, at + 3))
        if flags & 8:
            scale_size += scale_len * (8 if channel_flags & 4 else 6) if scale_len >= 2 else 0
            rotation_size += rot_len * (8 if channel_flags & 2 else 6) if rot_len >= 2 else 0
            position_size += pos_len * (8 if channel_flags & 1 else 6) if pos_len >= 2 else 0
        else:
            position_width = 16 if flags & 4 else 12
            scale_size += scale_len * 2 if scale_len >= 2 else 0
            rotation_size += ((rot_len * 12 + 15) // 16) * 2 if rot_len >= 2 else 0
            position_size += ((pos_len * position_width + 15) // 16) * 2 if pos_len >= 2 else 0
    def _stream(ptr: Optional[int], size: int) -> bytes:
        return blob[ptr:ptr + size] if ptr is not None else b""

    return (flags, channel_count, frame_count, descriptor_bytes,
            _stream(scale_ptr, scale_size),
            _stream(rotation_ptr, rotation_size),
            _stream(position_ptr, position_size))


def verify(s1_assets: Path, s2_cache: Path, species: Iterable[int]) -> Dict[str, Any]:
    """Byte-compare and decode-check every S2 pose record against S1 curves."""
    s1 = Stadium1DataProvider(s1_assets)
    s2 = Stadium2DataProvider(s2_cache)
    counts: Counter = Counter()
    mismatches: List[Dict[str, Any]] = []
    for value in species:
        blob, _ = s1._load_blob(f"{value - 1}.bin")
        parsed = parse_resource(blob, f"Stadium 1 species {value}", catalog_only=True)
        curves = [
            s1_curve_streams(blob, int(item["curveOffset"]))
            for item in parsed.get("animations", [])
            if item.get("supported") and item.get("curveOffset") is not None
        ]
        group = (s2._extracted_pose_groups or {}).get(value, {})
        records = group.get("records", [])
        used: set = set()
        for record in records:
            pose_path = s2._cache_path(record["path"])
            pose = pose_path.read_bytes()
            counts["records"] += 1
            try:
                metadata = inspect_stadium2_pose_metadata(pose, record["path"])
            except Exception as exc:
                counts["trailer-unrecognized"] += 1
                mismatches.append({"species": value, "pose": record["path"], "issue": f"trailer: {exc}"})
                continue
            trailer = metadata["trailerOffset"]
            offsets = [_u32(pose, trailer + 12 + word * 4) for word in range(4)]
            candidates = [
                i for i, curve in enumerate(curves)
                if i not in used and curve[1] == metadata["channelCount"] and curve[2] == metadata["frameCount"]
                and curve[0] == metadata["headerWord0"]
            ]
            relaxed = False
            if not candidates:
                relaxed = True
                candidates = [
                    i for i, curve in enumerate(curves)
                    if i not in used and curve[1] == metadata["channelCount"] and curve[2] == metadata["frameCount"]
                ]
            curve_index: Optional[int] = candidates[0] if candidates else None
            if curve_index is not None:
                used.add(curve_index)
                flags, _, _, descriptor, scale, rotation, position = curves[curve_index]
                identical = (
                    pose[offsets[0]:offsets[0] + len(descriptor)] == descriptor
                    and pose[offsets[1]:offsets[1] + len(scale)] == scale
                    and pose[offsets[2]:offsets[2] + len(rotation)] == rotation
                    and pose[offsets[3]:offsets[3] + len(position)] == position
                )
                counts["byte-identical" if identical else "reencoded-same-shape"] += 1
                if not identical:
                    mismatches.append({
                        "species": value, "pose": record["path"],
                        "issue": f"payload differs (S1 flags {flags:#x}, S2 flags {metadata['headerWord0']:#x})",
                    })
            else:
                counts["s2-only-pose"] += 1
            try:
                decoded = parse_stadium2_pose(pose, record["path"])
                counts["decoded"] += 1 if decoded.get("supported") else 0
            except Exception as exc:
                counts["decode-failed"] += 1
                mismatches.append({"species": value, "pose": record["path"], "issue": f"decode: {exc}"})
    return {"verification": dict(counts), "mismatches": mismatches[:50], "mismatchCount": len(mismatches)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Stadium 1 and Stadium 2 shared model animation metadata")
    parser.add_argument("--s1-assets", required=True, help="Stadium 1 pokemon_models directory")
    parser.add_argument("--s2-cache", required=True, help="external Stadium 2 extraction cache")
    parser.add_argument("--species", default="1-151", help="comma-separated IDs and/or ranges, e.g. 1,2,25,150-151")
    parser.add_argument("--verify", action="store_true",
                        help="decode every S2 pose record and byte-compare its streams against the matching S1 curve")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    args = parser.parse_args()

    species: List[int] = []
    for part in args.species.split(","):
        if "-" in part:
            start, end = (int(value) for value in part.split("-", 1))
            species.extend(range(start, end + 1))
        else:
            species.append(int(part))
    species = sorted(set(value for value in species if 1 <= value <= 151))
    result = compare(Path(args.s1_assets), Path(args.s2_cache), species)
    if args.verify:
        result.update(verify(Path(args.s1_assets), Path(args.s2_cache), species))
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
