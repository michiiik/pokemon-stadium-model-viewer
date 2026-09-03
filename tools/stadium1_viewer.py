#!/usr/bin/env python3
"""Standalone Pokémon Stadium model/animation viewer.

The viewer deliberately keeps game-specific loading in separate providers.
The binary reader follows the layouts documented by the Stadium 1 decomp:

    BinArchive -> PERS-SZP/Yay0 -> FRAGMENT -> model root/Geo/F3DEX

It is a best-effort inspection tool.  A bad pointer, an unknown display-list
command, or an absent texture becomes a diagnostic attached to the resource;
it never turns into an uncaught request-handler exception.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import math
import mimetypes
import os
from pathlib import Path
import struct
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pokemon_names import pokemon_name as canonical_pokemon_name


MAX_ARCHIVE_FILES = 4096
MAX_DISPLAY_LIST_COMMANDS = 20000
MAX_TEXTURE_BYTES = 4 * 1024 * 1024
MAX_SCAN_FILES = 20000
STADIUM1_MODEL_ID_MIN = 1
STADIUM1_MODEL_ID_MAX = 151
STADIUM1_MODEL_FILE_MIN = 0
STADIUM1_MODEL_FILE_MAX = 150
STADIUM2_TABLE_MAGIC = 0xEF
STADIUM2_MODEL_TABLE_ROM_OFFSET = 0x027ED000
STADIUM2_POSE_TABLE_ROM_OFFSET = 0x02D7D000
STADIUM2_POSE_TABLE_ROM_END = 0x03FD5000
STADIUM2_MODEL_TABLE_ROM_END = STADIUM2_POSE_TABLE_ROM_OFFSET
STADIUM2_MODEL_COUNT_EXPECTED = 282
STADIUM2_ARCHIVE_HEADER_SIZE = 0x10
STADIUM2_ARCHIVE_ENTRY_SIZE = 0x10
STADIUM1_MODEL_ARCHIVE_ROM_START = 0x00920000
STADIUM1_MODEL_ARCHIVE_ROM_END = 0x015C0000
STADIUM2_CATALOG_CACHE_FORMAT = "pms-stadium2-catalog-v1"

N64_ROM_MAGIC_Z64 = b"\x80\x37\x12\x40"
N64_ROM_MAGIC_V64 = b"\x37\x80\x40\x12"
N64_ROM_MAGIC_N64 = b"\x40\x12\x37\x80"


def normalize_n64_rom(data: bytes) -> bytes:
    """Return a big-endian ROM image from z64, v64, or n64 byte order."""
    if len(data) < 4:
        raise FormatError("ROM is too short to contain an N64 header")
    magic = data[:4]
    if magic == N64_ROM_MAGIC_Z64:
        return data
    if magic == N64_ROM_MAGIC_V64:
        if len(data) % 2:
            raise FormatError("byte-swapped N64 ROM has an odd length")
        return b"".join(data[offset + 1:offset + 2] + data[offset:offset + 1]
                        for offset in range(0, len(data), 2))
    if magic == N64_ROM_MAGIC_N64:
        if len(data) % 4:
            raise FormatError("word-swapped N64 ROM length is not a multiple of four")
        return b"".join(data[offset + 3:offset + 4] + data[offset + 2:offset + 3]
                        + data[offset + 1:offset + 2] + data[offset:offset + 1]
                        for offset in range(0, len(data), 4))
    raise FormatError("unsupported N64 ROM byte order; expected z64, v64, or n64 format")
STADIUM2_EXTRACT_FORMAT = "pms-stadium2-extract-v1"
VIEWER_CONFIG_FORMAT = "pokemon-stadium-model-viewer-config-v1"


def pokemon_name(model_id: object) -> Optional[str]:
    """Return the canonical species name when the record is a Pokédex model."""
    return canonical_pokemon_name(model_id)


def provider_alias(game_id: str) -> str:
    return "s1" if game_id == "stadium1" else "s2" if game_id == "stadium2" else game_id


def model_identity(game_id: str, model_id: object, reference: str, fallback_name: str) -> Dict[str, Any]:
    """Build the shared name/search fields exposed by both game providers."""
    species = pokemon_name(model_id)
    alias = provider_alias(game_id)
    fields: Dict[str, Any] = {
        "provider": game_id,
        "providerAlias": alias,
        "pokemonId": model_id if isinstance(model_id, int) else None,
        "pokemonName": species,
        "searchAliases": [alias, game_id, reference],
    }
    if isinstance(model_id, int):
        fields["searchAliases"].extend([str(model_id), f"{model_id:03d}"])
    if species:
        fields["searchAliases"].append(species)
        fields["name"] = f"{alias.upper()} #{model_id:03d} · {species} · {reference}"
    else:
        fields["name"] = fallback_name
    return fields

# Stadium 2's extracted FRAGMENT records leave the main body branches of a
# small set of dark-bodied species as mode-1 cmd23 materials with descriptor
# -1.  The source-side renderer supplies the species colour outside the
# FRAGMENT payload; the viewer has no equivalent external material table yet.
# Keep the temporary visual correction isolated to the Stadium 2 provider and
# record its provenance on the returned model so it cannot silently affect
# Stadium 1 or be mistaken for a recovered texture.
#
# Values are deliberately kept in one table so they can be replaced by the
# recovered S2 runtime material table without touching the parser/renderer.
STADIUM2_DARK_BODY_TINTS: Dict[int, Tuple[int, int, int, int]] = {
    197: (35, 31, 46, 255),    # Umbreon
    198: (48, 49, 62, 255),    # Murkrow
    200: (77, 48, 96, 255),    # Misdreavus
    201: (46, 40, 48, 255),    # Unown
    212: (125, 30, 32, 255),   # Scizor: dark red body; red source tex 14
    214: (50, 105, 180, 255),  # Heracross: blue source tex 0/1 on upper/limbs
    205: (61, 86, 98, 255),    # Forretress
    215: (47, 59, 87, 255),    # Sneasel
    218: (145, 43, 26, 255),   # Slugma: lava-red source tex 1/2
    219: (145, 43, 26, 255),   # Magcargo: lava-red body, not the old brown proxy
    228: (34, 29, 47, 255),    # Houndour: dark body source tex 8/9
    229: (30, 30, 50, 255),     # Houndoom: dark body source tex 7/10
    233: (160, 160, 160, 255),  # Porygon2: gray body; blue/pink detail tex 5/6
}

# Most S2 unbound body branches use mode 1.  Unown is the one known exception
# in this group: its white body branches are mode 3, while the mode-1 branch
# remains a normal bound detail path.  Keep this exception explicit instead
# of broadening the bridge to every material mode.
STADIUM2_TINT_TEXTURE_MODES: Dict[int, Tuple[int, ...]] = {
    201: (1, 3),
}

GEO_LAYOUT_SIZES = {
    0x00: 0x08, 0x01: 0x04, 0x02: 0x08, 0x03: 0x08, 0x04: 0x04,
    0x05: 0x04, 0x06: 0x04, 0x07: 0x08, 0x08: 0x0C, 0x09: 0x04,
    0x0A: 0x08, 0x0B: 0x18, 0x0C: 0x04, 0x0D: 0x04, 0x0E: 0x04,
    0x0F: 0x04, 0x10: 0x04, 0x11: 0x04, 0x12: 0x04, 0x13: 0x08,
    0x14: 0x0C, 0x15: 0x0C, 0x16: 0x04, 0x17: 0x14, 0x18: 0x08,
    0x19: 0x08, 0x1A: 0x04, 0x1B: 0x10, 0x1C: 0x10, 0x1D: 0x1C,
    0x1E: 0x08, 0x1F: 0x18, 0x20: 0x14, 0x21: 0x10, 0x22: 0x08,
    0x23: 0x10, 0x24: 0x04, 0x25: 0x04, 0x26: 0x14,
    # Stadium 2 extends the Stadium 1 command table with two four-byte
    # graph-state markers.  They carry no pointer or payload; the next
    # command begins immediately at +4.  The Stadium 1 decomp ends at 0x26,
    # so keep these extension opcodes isolated here until their S2 graph-node
    # callbacks are recovered.
    0x28: 0x04, 0x29: 0x04,
}

# Marker for graph registrations that are not animated parts (display lists,
# texture bindings, anchors, ...), used by parse_geo_layout's graph tracking.
GRAPH_OTHER = -1


class FormatError(Exception):
    """A malformed or incomplete resource, kept separate from server errors."""


@dataclass
class ParseContext:
    data: bytes
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def warn(self, code: str, message: str, **extra: Any) -> None:
        item: Dict[str, Any] = {"severity": "warning", "code": code, "message": message}
        item.update(extra)
        self.diagnostics.append(item)

    def error(self, code: str, message: str, **extra: Any) -> None:
        item: Dict[str, Any] = {"severity": "error", "code": code, "message": message}
        item.update(extra)
        self.diagnostics.append(item)


class Reader:
    def __init__(self, data: bytes, context: Optional[ParseContext] = None):
        self.data = data
        self.context = context

    def can(self, offset: int, size: int = 1) -> bool:
        return offset >= 0 and size >= 0 and offset + size <= len(self.data)

    def _need(self, offset: int, size: int) -> None:
        if not self.can(offset, size):
            raise FormatError(f"read outside resource at 0x{offset:X} (+0x{size:X})")

    def u8(self, offset: int) -> int:
        self._need(offset, 1)
        return self.data[offset]

    def s8(self, offset: int) -> int:
        return self.u8(offset) - 256 if self.u8(offset) & 0x80 else self.u8(offset)

    def u16(self, offset: int) -> int:
        self._need(offset, 2)
        return struct.unpack_from(">H", self.data, offset)[0]

    def s16(self, offset: int) -> int:
        self._need(offset, 2)
        return struct.unpack_from(">h", self.data, offset)[0]

    def u32(self, offset: int) -> int:
        self._need(offset, 4)
        return struct.unpack_from(">I", self.data, offset)[0]

    def s32(self, offset: int) -> int:
        self._need(offset, 4)
        return struct.unpack_from(">i", self.data, offset)[0]

    def f32(self, offset: int) -> float:
        self._need(offset, 4)
        return struct.unpack_from(">f", self.data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def decode_yay0(data: bytes, expected_size: Optional[int] = None) -> bytes:
    """Decode Nintendo's Yay0 stream, matching the game's control-bit order."""
    if len(data) < 0x10 or data[:4] != b"Yay0":
        raise FormatError("missing Yay0 header")
    output_size = u32(data, 4)
    link_offset = u32(data, 8)
    chunk_offset = u32(data, 12)
    if expected_size is not None and expected_size > 0 and expected_size != output_size:
        # A mismatch is useful to callers, but the stream's own size is the
        # authoritative bound used by Yay0_Decompress.
        output_size = min(output_size, expected_size)
    if not (0x10 <= link_offset <= len(data)) or not (0x10 <= chunk_offset <= len(data)):
        raise FormatError("Yay0 table offset outside stream")

    result = bytearray()
    control_offset = 0x10
    link_cursor = link_offset
    chunk_cursor = chunk_offset
    control = 0
    mask = 0
    while len(result) < output_size:
        if mask == 0:
            if control_offset + 4 > len(data):
                raise FormatError("Yay0 control table ended early")
            control = u32(data, control_offset)
            control_offset += 4
            mask = 0x80000000
        if control & mask:
            if chunk_cursor >= len(data):
                raise FormatError("Yay0 literal table ended early")
            result.append(data[chunk_cursor])
            chunk_cursor += 1
        else:
            if link_cursor + 2 > len(data):
                raise FormatError("Yay0 link table ended early")
            link = struct.unpack_from(">H", data, link_cursor)[0]
            link_cursor += 2
            count = (link >> 12) + 2
            distance = (link & 0x0FFF) + 1
            if count == 2:
                if chunk_cursor >= len(data):
                    raise FormatError("Yay0 extended count ended early")
                count = data[chunk_cursor] + 0x12
                chunk_cursor += 1
            if distance > len(result):
                raise FormatError("Yay0 back-reference before output start")
            for _ in range(count):
                if len(result) >= output_size:
                    break
                result.append(result[-distance])
        mask >>= 1
    return bytes(result)


def unpack_persszp(blob: bytes) -> Tuple[bytes, Dict[str, Any]]:
    """Unpack PERS-SZP and apply the small post-decompression fixup table."""
    if len(blob) < 0x18 or blob[:8] != b"PERS-SZP":
        raise FormatError("not a PERS-SZP resource")
    header_size, size1, size2, fixup_count = struct.unpack_from(">4I", blob, 8)
    if header_size < 0x18 or header_size > len(blob):
        raise FormatError(f"invalid PERS-SZP header size 0x{header_size:X}")
    packed = blob[header_size:]
    decoded = decode_yay0(packed, size2)
    if size2 and len(decoded) != size2:
        raise FormatError("Yay0 size does not match PERS-SZP sizeInRam")
    out = bytearray(decoded)
    fixups = 0
    for i in range(fixup_count):
        pair = 0x18 + i * 8
        if pair + 8 > len(blob):
            raise FormatError("PERS-SZP fixup table ended early")
        value, target = struct.unpack_from(">2I", blob, pair)
        if target + 4 > len(out):
            raise FormatError(f"PERS-SZP fixup target 0x{target:X} outside output")
        struct.pack_into(">I", out, target, value)
        fixups += 1
    if size1 and size2 > size1:
        # The game zero-fills the tail after Yay0 decompression.
        out[size1:size2] = b"\0" * (size2 - size1)
    return bytes(out), {
        "format": "PERS-SZP",
        "headerSize": header_size,
        "decompressedSize": len(out),
        "fixups": fixups,
    }


def parse_archive(data: bytes) -> Optional[Dict[str, Any]]:
    """Read the source-backed 0x10 header and 0x10 file records."""
    if len(data) < 0x10:
        return None
    total_size, count = u32(data, 8), u32(data, 0xC)
    if not (0 < count <= MAX_ARCHIVE_FILES):
        return None
    table_end = 0x10 + count * 0x10
    if table_end > len(data):
        return None
    if total_size and total_size > len(data):
        return None
    files = []
    for index in range(count):
        entry = 0x10 + index * 0x10
        offset, size = u32(data, entry), u32(data, entry + 4)
        if offset + size > len(data):
            return None
        files.append({"index": index, "offset": offset, "size": size})
    return {"format": "BinArchive", "totalSize": total_size, "fileCount": count, "files": files}


def parse_stadium2_indexed_archive(data: bytes, name: str = "Stadium 2 archive") -> Dict[str, Any]:
    """Parse the verified Stadium 2 table used by model and pose banks.

    The table header is ``0xEF, 0, payload_size, record_count`` followed by
    0x10-byte records.  Each record begins with a table-relative offset and a
    byte length; the remaining two words are retained as unknown fields.
    This is deliberately separate from ``parse_archive`` because the S2
    table's header and record semantics are not the S1 BinArchive contract.
    """
    if len(data) < STADIUM2_ARCHIVE_HEADER_SIZE:
        raise FormatError(f"{name} is shorter than the Stadium 2 archive header")
    magic, reserved, payload_size, count = struct.unpack_from(">4I", data, 0)
    if magic != STADIUM2_TABLE_MAGIC:
        raise FormatError(f"{name} has unknown header 0x{magic:08X}, expected 0x{STADIUM2_TABLE_MAGIC:X}")
    if count <= 0 or count > MAX_ARCHIVE_FILES:
        raise FormatError(f"{name} has unreasonable record count {count}")
    table_end = STADIUM2_ARCHIVE_HEADER_SIZE + count * STADIUM2_ARCHIVE_ENTRY_SIZE
    if table_end > len(data):
        raise FormatError(f"{name} index ends at 0x{table_end:X}, beyond the supplied bytes")
    files: List[Dict[str, Any]] = []
    for index in range(count):
        entry = STADIUM2_ARCHIVE_HEADER_SIZE + index * STADIUM2_ARCHIVE_ENTRY_SIZE
        offset, size, unknown0, unknown1 = struct.unpack_from(">4I", data, entry)
        if offset < table_end:
            raise FormatError(f"{name} record {index} points into its index at 0x{offset:X}")
        if offset + size > len(data):
            raise FormatError(f"{name} record {index} ends at 0x{offset + size:X}, beyond the supplied bytes")
        files.append({"index": index, "offset": offset, "size": size,
                      "unknown0": unknown0, "unknown1": unknown1})
    return {"format": "Stadium2IndexedArchive", "magic": magic, "reserved": reserved,
            "payloadSize": payload_size, "fileCount": count, "tableSize": table_end,
            "files": files}


def pointer_to_offset(value: int, data_length: int) -> Optional[int]:
    """Resolve a fragment-local encoded pointer without pretending to relocate it."""
    if value == 0:
        return None
    # Memmap_GetFragmentVaddr accepts 0x81000000..0x8FFFFFFF and uses the low
    # 20 bits as the offset into the fragment map.  The viewer is inspecting a
    # single fragment, so the fragment id is intentionally not guessed.
    if 0x81000000 <= value < 0x90000000:
        offset = value & 0x000FFFFF
        return offset if offset < data_length else None
    # A few checked-in/hand-authored assets use a raw offset in place of the
    # relocated mask.  Accept it only when it is unambiguously local.
    if value < data_length:
        return value
    return None


def decode_entry_offset(data: bytes, context: ParseContext) -> Optional[int]:
    if len(data) < 4:
        return None
    word = u32(data, 0)
    if word >> 26 != 2:
        context.warn("fragment-entry", "FRAGMENT header does not start with a MIPS J instruction")
        return None
    target = 0x80000000 | ((word & 0x03FFFFFF) << 2)
    offset = pointer_to_offset(target, len(data))
    if offset is None:
        context.warn("fragment-entry", f"entry target 0x{target:08X} is not local")
    return offset


def decode_payload_pointer(data: bytes, entry_offset: Optional[int], context: ParseContext) -> Optional[int]:
    if entry_offset is None or entry_offset + 0x18 > len(data):
        return None
    # Model accessor stubs shown in MODEL_PORTING.md use LUI/ADDIU on v1/v1.
    lui = u32(data, entry_offset + 0x0C)
    addiu = u32(data, entry_offset + 0x10)
    if (lui & 0xFFFF0000) != 0x3C030000 or (addiu & 0xFFFF0000) != 0x24630000:
        context.warn("model-entry", "entry stub is not the documented model data accessor")
        return None
    virtual = ((lui & 0xFFFF) << 16) + struct.unpack(">h", struct.pack(">H", addiu & 0xFFFF))[0]
    return pointer_to_offset(virtual & 0xFFFFFFFF, len(data))


def read_vec3s(reader: Reader, offset: int) -> List[int]:
    return [reader.s16(offset), reader.s16(offset + 2), reader.s16(offset + 4)]


def read_vec3f(reader: Reader, offset: int) -> List[float]:
    return [reader.f32(offset), reader.f32(offset + 4), reader.f32(offset + 8)]


def hermite(keys: Sequence[Tuple[int, float, float, float]], frame: float, alternate_tangents: bool = False) -> float:
    if not keys:
        return 0.0
    if frame <= keys[0][0]:
        return keys[0][1]
    if frame >= keys[-1][0]:
        return keys[-1][1]
    for left, right in zip(keys, keys[1:]):
        if frame < right[0]:
            span = float(right[0] - left[0])
            # This is the decomp's ModelAnim_InterpolateKeyframe basis:
            # key tangents are normalized to a 30-frame interval.
            x = (frame - left[0]) / 30.0
            y = 30.0 / span
            x2, x3 = x * x, x * x * x
            y2, y3 = y * y, y * y * y
            h00 = (2.0 * x3 * y3) - (3.0 * x2 * y2) + 1.0
            h01 = (-2.0 * x3 * y3) + (3.0 * x2 * y2)
            h10 = (x3 * y2) - (2.0 * x2 * y) + x
            h11 = (x3 * y2) - (x2 * y)
            # The source has two key-record layouts.  Vec3s records use the
            # z component as the tangent on both sides.  The alternate
            # func_80016B30 record has an explicit left tangent at +0x06,
            # while the source uses the right record's Vec3s.z at +0x04.
            left_tangent = left[3] if alternate_tangents else left[2]
            right_tangent = right[2]
            return h00 * left[1] + h10 * left_tangent + h01 * right[1] + h11 * right_tangent
    return keys[-1][1]


def curve_keys(reader: Reader, offset: Optional[int], count: int, scale: float,
               stride: int = 6) -> List[Tuple[int, float, float, float]]:
    if offset is None or count < 1 or count > 4096 or stride not in (6, 8) or not reader.can(offset, count * stride):
        return []
    result = []
    for i in range(count):
        at = offset + i * stride
        y = reader.s16(at + 2) / scale
        z = reader.s16(at + 4) / scale
        tangent = reader.s16(at + 6) / scale if stride == 8 else z
        result.append((reader.s16(at), y, z, tangent))
    return result


def packed_s16(reader: Reader, offset: Optional[int], index: int, width: int) -> Optional[int]:
    """Read one signed packed channel using the decomp's func_80010500 layout."""
    if offset is None or width <= 0 or width > 16 or index < 0:
        return None
    bit = index * width
    word = offset + (bit // 16) * 2
    shift = bit % 16
    if not reader.can(word, 4):
        return None
    pair = (reader.u16(word) << 16) | reader.u16(word + 2)
    value = ((pair << shift) & 0xFFFFFFFF) >> (32 - width)
    if value & (1 << (width - 1)):
        value -= 1 << width
    return value


def parse_curve(reader: Reader, curve_offset: Optional[int], context: ParseContext,
                max_samples: int = 600, metadata_only: bool = False) -> Dict[str, Any]:
    if curve_offset is None or not reader.can(curve_offset, 0x1C):
        return {"supported": False, "frameCount": 0, "reason": "curve pointer is missing or outside fragment"}
    try:
        flags = reader.s16(curve_offset)
        channel_count = reader.u16(curve_offset + 8)
        frame_count = reader.u16(curve_offset + 0x0A)
        descriptor_ptr = pointer_to_offset(reader.u32(curve_offset + 0x0C), len(reader.data))
        # func_80017090 assigns the curve payloads to the resident evaluator
        # as scale (+0x10), rotation (+0x14), position (+0x18). This order is
        # easy to invert because the descriptor bytes store scale/rotation/
        # position lengths in the same order, but the source pointer fields
        # are not position/rotation/scale.
        scale_ptr = pointer_to_offset(reader.u32(curve_offset + 0x10), len(reader.data))
        rotation_ptr = pointer_to_offset(reader.u32(curve_offset + 0x14), len(reader.data))
        position_ptr = pointer_to_offset(reader.u32(curve_offset + 0x18), len(reader.data))
    except FormatError:
        return {"supported": False, "frameCount": 0, "reason": "curve header is truncated"}
    if channel_count == 0 or frame_count == 0:
        return {"supported": False, "frameCount": frame_count, "reason": "curve has no channels or frames"}
    if channel_count > 4096 or frame_count > 4096:
        return {"supported": False, "frameCount": frame_count, "reason": "curve dimensions are unreasonable"}
    if descriptor_ptr is None or not reader.can(descriptor_ptr, channel_count * 0x0A):
        return {"supported": False, "frameCount": frame_count, "reason": "curve descriptor table is missing"}

    if metadata_only:
        return {
            "supported": True, "frameCount": frame_count, "channelCount": channel_count,
            "flags": flags, "poses": [], "sampledFrames": 0, "truncatedSamples": False,
        }

    bones = max(1, channel_count // 3)
    sample_count = min(frame_count, max_samples)
    poses: List[List[Dict[str, List[float]]]] = []
    descriptors = []
    for channel in range(channel_count):
        at = descriptor_ptr + channel * 0x0A
        # Source fields are scale, rotation, position in that order.  The
        # evaluator uses position channel index 2, rotation index 1, scale 0.
        descriptors.append((reader.u8(at), reader.u8(at + 1), reader.u8(at + 2), reader.u8(at + 3),
                            reader.u16(at + 4), reader.u16(at + 6), reader.u16(at + 8)))

    def channel_value(channel: int, frame: int) -> Tuple[float, float, float]:
        scale_length, rot_length, pos_length, channel_flags, scale_index, rot_index, pos_index = descriptors[channel]
        at = descriptor_ptr + channel * 0x0A
        if flags & 8:
            # func_80016F20/DE0/D20 receive s16* streams and add the
            # descriptor offsets directly.  They are s16 offsets, not key
            # record numbers; the key stride is selected by the descriptor's
            # interpolation flags.
            position_alt = bool(channel_flags & 1)
            rotation_alt = bool(channel_flags & 2)
            scale_alt = bool(channel_flags & 4)
            position_keys = curve_keys(
                reader,
                None if position_ptr is None else position_ptr + pos_index * 2,
                pos_length,
                1.0,
                8 if position_alt else 6,
            )
            rotation_keys = curve_keys(
                reader,
                None if rotation_ptr is None else rotation_ptr + rot_index * 2,
                rot_length,
                10.0,
                8 if rotation_alt else 6,
            )
            scale_keys = curve_keys(
                reader,
                None if scale_ptr is None else scale_ptr + scale_index * 2,
                scale_length,
                100.0,
                8 if scale_alt else 6,
            )
            position = float(reader.s16(at + 8)) if pos_length < 2 else hermite(position_keys, frame, position_alt)
            rotation_degrees = (reader.s16(at + 6) / 10.0) if rot_length < 2 else hermite(rotation_keys, frame, rotation_alt)
            rotation = ((rotation_degrees % 360.0) / 360.0) * 65536.0
            scale = (reader.s16(at + 4) / 100.0) if scale_length < 2 else hermite(scale_keys, frame, scale_alt)
            return position, rotation, scale

        def signed_packed(value: int, width: int) -> int:
            mask = (1 << width) - 1
            value &= mask
            return value - (1 << width) if value & (1 << (width - 1)) else value

        def scalar(stream: Optional[int], index: int, length: int, width: Optional[int] = None) -> int:
            if length < 2:
                raw = reader.u16(at + 8)
                return raw if width == 16 else signed_packed(raw, width or 16)
            if stream is None:
                return 0
            selected = index + min(frame, length - 1)
            if width is not None:
                return packed_s16(reader, stream, selected, width) or 0
            return reader.s16(stream + selected * 2) if reader.can(stream + selected * 2, 2) else 0

        if pos_length < 2:
            position = float(scalar(None, pos_index, pos_length, 16 if (flags & 4) else 12))
        else:
            selected = pos_index + min(frame, pos_length - 1)
            packed = packed_s16(reader, position_ptr, selected, 16 if (flags & 4) else 12)
            position = float(packed if packed is not None else 0)
        rotation = float(scalar(rotation_ptr, rot_index, rot_length, 12) * 16)
        scale = float(scalar(scale_ptr, scale_index, scale_length)) / 1000.0
        if rot_length < 2:
            raw_rotation = reader.u16(at + 6)
            rotation = float(raw_rotation if (flags & 4) else signed_packed(raw_rotation, 12)) * 16
        if scale_length < 2:
            scale = float(reader.u16(at + 4)) / 1000.0
        return position, rotation, scale

    for frame in range(sample_count):
        frame_pose = []
        for bone in range(bones):
            p = [channel_value(bone * 3 + axis, frame)[0] for axis in range(3) if bone * 3 + axis < channel_count]
            r = [channel_value(bone * 3 + axis, frame)[1] for axis in range(3) if bone * 3 + axis < channel_count]
            s = [channel_value(bone * 3 + axis, frame)[2] for axis in range(3) if bone * 3 + axis < channel_count]
            while len(p) < 3: p.append(0.0)
            while len(r) < 3: r.append(0.0)
            while len(s) < 3: s.append(1.0)
            frame_pose.append({"position": p, "rotation": r, "scale": s})
        poses.append(frame_pose)
    return {
        "supported": True,
        "frameCount": frame_count,
        "channelCount": channel_count,
        "flags": flags,
        "poses": poses,
        "sampledFrames": sample_count,
        "truncatedSamples": sample_count != frame_count,
    }


def decode_rgba16(data: bytes, offset: int, width: int, height: int) -> Optional[bytes]:
    size = width * height * 2
    if width <= 0 or height <= 0 or size > MAX_TEXTURE_BYTES or offset < 0 or offset + size > len(data):
        return None
    out = bytearray()
    for i in range(0, size, 2):
        value = struct.unpack_from(">H", data, offset + i)[0]
        r = ((value >> 11) & 0x1F) * 255 // 31
        g = ((value >> 6) & 0x1F) * 255 // 31
        b = ((value >> 1) & 0x1F) * 255 // 31
        a = 255 if value & 1 else 0
        out.extend((r, g, b, a))
    return bytes(out)


def decode_rgba32(data: bytes, offset: int, width: int, height: int) -> Optional[bytes]:
    size = width * height * 4
    if width <= 0 or height <= 0 or size > MAX_TEXTURE_BYTES or offset < 0 or offset + size > len(data):
        return None
    # N64 RGBA32 is stored as all R/G bytes followed by all B/A bytes per
    # 16-byte pair of scanline texels.  Keep the decoder conservative; if a
    # source uses another arrangement the texture remains a diagnostic.
    out = bytearray(size)
    for y in range(height):
        row = offset + y * width * 4
        half = (width + 1) // 2
        for x in range(width):
            bank = row + (x // 2) * 8
            if x % 2 == 0:
                src = bank
            else:
                src = bank + 4
            if src + 4 > len(data):
                return None
            out[(y * width + x) * 4:(y * width + x + 1) * 4] = data[src:src + 4]
    return bytes(out)


def decode_ia8(data: bytes, offset: int, width: int, height: int) -> Optional[bytes]:
    size = width * height
    if width <= 0 or height <= 0 or size > MAX_TEXTURE_BYTES or offset < 0 or offset + size > len(data):
        return None
    out = bytearray()
    for value in data[offset:offset + size]:
        intensity = ((value >> 4) & 0x0F) * 17
        alpha = (value & 0x0F) * 17
        out.extend((intensity, intensity, intensity, alpha))
    return bytes(out)


def decode_i8(data: bytes, offset: int, width: int, height: int) -> Optional[bytes]:
    size = width * height
    if width <= 0 or height <= 0 or size > MAX_TEXTURE_BYTES or offset < 0 or offset + size > len(data):
        return None
    out = bytearray()
    for value in data[offset:offset + size]:
        out.extend((value, value, value, 255))
    return bytes(out)


def decode_ia16(data: bytes, offset: int, width: int, height: int) -> Optional[bytes]:
    # IA16 texels are two bytes: intensity in the high byte, alpha in the low.
    size = width * height * 2
    if width <= 0 or height <= 0 or size > MAX_TEXTURE_BYTES or offset < 0 or offset + size > len(data):
        return None
    out = bytearray()
    for at in range(offset, offset + size, 2):
        intensity, alpha = data[at], data[at + 1]
        out.extend((intensity, intensity, intensity, alpha))
    return bytes(out)


def decode_ci(data: bytes, offset: int, width: int, height: int, size: int,
              palette: Sequence[Tuple[int, int, int, int]]) -> Optional[bytes]:
    """Decode a source CI4/CI8 texel stream through one RGBA16 TLUT.

    Stadium's model texture table keeps CI texels and palette tables in the
    same GeoLayout cmd17 resource.  The renderer later selects the palette
    with cmd23's second descriptor field.  The extracted texel streams are
    row-major in the fragment, while the N64 display list supplies the
    palette entries as RGBA16 values.
    """
    texel_count = width * height
    if width <= 0 or height <= 0 or texel_count > MAX_TEXTURE_BYTES * 2:
        return None
    bytes_needed = (texel_count + 1) // 2 if size == 0 else texel_count
    if size not in (0, 1) or offset < 0 or offset + bytes_needed > len(data):
        return None
    if not palette:
        return None
    out = bytearray()
    for index in range(texel_count):
        if size == 0:  # CI4: two texels per byte, high nibble first
            packed = data[offset + (index // 2)]
            palette_index = (packed >> 4) if (index & 1) == 0 else packed & 0x0F
        else:  # CI8: one texel per byte
            palette_index = data[offset + index]
        if palette_index >= len(palette):
            return None
        out.extend(palette[palette_index])
    return bytes(out)


def texture_alpha_mode(rgba: Optional[bytes]) -> str:
    """Classify decoded texel alpha for the viewer's normalized material state."""
    if not rgba:
        return "opaque"
    alpha = rgba[3::4]
    if not any(value != 255 for value in alpha):
        return "opaque"
    # Stadium model RGBA16/CI textures commonly use the N64 one-bit alpha as
    # a cutout mask. Keep intermediate alpha as a genuinely blended surface.
    return "cutout" if all(value in (0, 255) for value in alpha) else "blend"


def parse_palette_descriptors(reader: Reader, palette_offset: Optional[int], count: int,
                              context: ParseContext, palettes: List[Dict[str, Any]]) -> None:
    """Read the cmd17 ``unk_01C`` RGBA16 palette descriptor array."""
    if palette_offset is None:
        # Do not report a missing palette table until the descriptor table
        # proves that a CI texture actually needs it. Several models carry a
        # non-zero/placeholder palette count while using only direct RGBA16
        # descriptors; reporting this here makes healthy models look broken.
        return
    count = min(max(0, count), 256)
    for palette_index in range(count):
        at = palette_offset + palette_index * 0x0C
        if not reader.can(at, 0x0C):
            context.warn("texture-palette-truncated", "texture palette descriptor table ended early", index=palette_index)
            break
        entry_count = max(0, reader.s32(at))
        pointer = pointer_to_offset(reader.u32(at + 4), len(reader.data))
        entry_count = min(entry_count, 256)
        colors: List[Tuple[int, int, int, int]] = []
        if pointer is not None and entry_count and reader.can(pointer, entry_count * 2):
            decoded = decode_rgba16(reader.data, pointer, entry_count, 1)
            if decoded is not None:
                colors = [tuple(decoded[i:i + 4]) for i in range(0, len(decoded), 4)]
        item: Dict[str, Any] = {
            "descriptor": palette_index,
            "entryCount": entry_count,
            "sourceOffset": pointer,
        }
        if colors:
            item["colors"] = colors
        elif pointer is None:
            context.warn("texture-palette-pointer", f"texture palette {palette_index} has an unavailable pointer")
        elif entry_count:
            context.warn("texture-palette-truncated", f"texture palette {palette_index} is outside the fragment")
        palettes.append(item)


def texture_setup_dimensions(reader: Reader, setup_offset: Optional[int], fallback: Tuple[int, int]) -> Tuple[int, int]:
    """Read the G_SETTILESIZE dimensions from the setup list named by Geo cmd23."""
    if setup_offset is None:
        return fallback
    width, height = fallback
    cursor = setup_offset
    for _ in range(128):
        if not reader.can(cursor, 8):
            break
        w0, w1 = reader.u32(cursor), reader.u32(cursor + 4)
        cursor += 8
        op = w0 >> 24
        if op == 0xF2:
            width = (((w1 >> 12) & 0xFFF) >> 2) + 1
            height = ((w1 & 0xFFF) >> 2) + 1
            break
        if op in (0xB8, 0xDF):
            break
    return max(1, min(width, 2048)), max(1, min(height, 2048))


def texture_setup_sampler(reader: Reader, setup_offset: Optional[int]) -> Tuple[str, str]:
    """Read the source G_SETTILE S/T wrap modes from a cmd23 setup list."""
    wrap_s, wrap_t = "repeat", "repeat"
    if setup_offset is None:
        return wrap_s, wrap_t

    # G_SETTILE is an RDP state command.  The CMS/CMT two-bit fields combine
    # G_TX_MIRROR (1) and G_TX_CLAMP (2); 3 is mirror+clamp. Keep this as a
    # distinct state: WebGL has no mirror-then-clamp sampler, so the frontend
    # uses CLAMP_TO_EDGE and emulates one mirrored adjacent tile before clamp.
    # Pokémon model setup lists use tile 0; ignore other tile records.
    modes = {0: "repeat", 1: "mirror", 2: "clamp", 3: "mirror-clamp"}
    cursor = setup_offset
    for _ in range(128):
        if not reader.can(cursor, 8):
            break
        w0, w1 = reader.u32(cursor), reader.u32(cursor + 4)
        cursor += 8
        op = w0 >> 24
        if op == 0xF5 and ((w1 >> 24) & 7) == 0:  # G_SETTILE, tile 0
            wrap_t = modes[(w1 >> 18) & 3]
            wrap_s = modes[(w1 >> 8) & 3]
        if op in (0xB8, 0xDF):
            break
    return wrap_s, wrap_t


def geo_scale_component(reader: Reader, offset: int) -> float:
    """Decode an animated-part Q16.16 scale, including the S2 unit sentinel."""
    raw = reader.u32(offset)
    return 1.0 if raw == 0xFFFFFFFF else raw / 65536.0


def parse_event_track(reader: Reader, track_offset: Optional[int], context: ParseContext) -> Dict[str, Any]:
    """Decode the model event track used for source texture remapping.

    The layout follows ``unk_D_86002F58_004_000_054_004`` and
    ``func_80017540``/``func_800176DC``: one segment record per texture slot
    and a byte mapping table containing descriptor indices.
    """
    result: Dict[str, Any] = {
        "supported": False, "flags": 0, "initialFrame": 0, "fallbackFrame": 0,
        "slotCount": 0, "frameCount": 0, "segments": [], "mapping": [],
    }
    if track_offset is None:
        result["reason"] = "event track pointer is missing"
        return result
    if not reader.can(track_offset, 0x14):
        result["reason"] = "event track header is truncated"
        context.warn("event-track-truncated", "event track header is outside the fragment", offset=track_offset)
        return result

    result["flags"] = reader.s16(track_offset)
    result["initialFrame"] = reader.s16(track_offset + 4)
    result["fallbackFrame"] = reader.s16(track_offset + 6)
    slot_count = min(reader.u16(track_offset + 8), 256)
    frame_count = min(reader.u16(track_offset + 0xA), 4096)
    segment_offset = pointer_to_offset(reader.u32(track_offset + 0xC), len(reader.data))
    mapping_offset = pointer_to_offset(reader.u32(track_offset + 0x10), len(reader.data))
    result["slotCount"] = slot_count
    result["frameCount"] = frame_count

    if slot_count and (segment_offset is None or not reader.can(segment_offset, slot_count * 4)):
        context.warn("event-track-segments", "event track segment table is missing or truncated", offset=track_offset)
        result["reason"] = "event track segment table is unavailable"
        return result
    if frame_count and mapping_offset is None:
        context.warn("event-track-mapping", "event track mapping table is missing", offset=track_offset)
        result["reason"] = "event track mapping table is unavailable"
        return result

    segments: List[List[int]] = []
    max_mapping_index = -1
    for slot in range(slot_count):
        start = reader.u16(segment_offset + slot * 4)
        delta = reader.u16(segment_offset + slot * 4 + 2)
        segments.append([start, delta])
        if frame_count:
            max_mapping_index = max(max_mapping_index, (frame_count - 1 if frame_count < start else max(0, start - 1)) + delta)
    mapping_length = max_mapping_index + 1
    if mapping_length and not reader.can(mapping_offset, mapping_length):
        context.warn("event-track-mapping", "event track mapping table is truncated", offset=track_offset)
        result["reason"] = "event track mapping table is truncated"
        return result

    result["segments"] = segments
    result["mapping"] = list(reader.data[mapping_offset:mapping_offset + mapping_length]) if mapping_length else []
    result["supported"] = True
    return result


def parse_texture_descriptors(reader: Reader, descriptor_offset: Optional[int], count: int,
                              context: ParseContext, textures: List[Dict[str, Any]],
                              palettes: Optional[List[Dict[str, Any]]] = None) -> Dict[int, int]:
    """Decode the S1 cmd17 texture table and return descriptor-index -> API texture id."""
    bindings: Dict[int, int] = {}
    if descriptor_offset is None:
        context.warn("texture-descriptors-missing", "GeoLayout texture descriptor table is missing")
        return bindings
    count = min(max(0, count), 256)
    for descriptor_index in range(count):
        at = descriptor_offset + descriptor_index * 0x0C
        if not reader.can(at, 0x0C):
            context.warn("texture-descriptor-truncated", "texture descriptor table ended early", index=descriptor_index)
            break
        fmt = reader.u8(at)
        size = reader.u8(at + 1)
        width = reader.s16(at + 2)
        height = reader.s16(at + 4)
        texel_count = reader.s16(at + 6)
        pointer = pointer_to_offset(reader.u32(at + 8), len(reader.data))
        texture_id = len(textures)
        item: Dict[str, Any] = {
            "id": texture_id, "descriptor": descriptor_index, "format": f"fmt{fmt}/siz{size}",
            "width": max(1, width), "height": max(1, height), "texelCount": max(0, texel_count),
            "sourceOffset": pointer,
        }
        rgba = None
        palette_variants: Dict[str, str] = {}
        palette_alpha: Dict[str, bool] = {}
        palette_warning_reported = False
        if pointer is not None:
            if fmt == 0 and size == 2:
                rgba = decode_rgba16(reader.data, pointer, item["width"], item["height"])
            elif fmt == 0 and size == 3:
                rgba = decode_rgba32(reader.data, pointer, item["width"], item["height"])
            elif fmt == 3 and size == 1:
                rgba = decode_ia8(reader.data, pointer, item["width"], item["height"])
            elif fmt == 3 and size == 2:
                rgba = decode_ia16(reader.data, pointer, item["width"], item["height"])
            elif fmt == 4 and size == 1:
                rgba = decode_i8(reader.data, pointer, item["width"], item["height"])
            elif fmt == 2 and size in (0, 1):
                if not palettes:
                    if not palette_warning_reported:
                        context.warn("texture-palettes-missing", "CI texture palettes are unavailable",
                                     descriptor=descriptor_index)
                        palette_warning_reported = True
                else:
                    for palette in palettes:
                        colors = [tuple(color) for color in palette.get("colors", [])]
                        decoded = decode_ci(reader.data, pointer, item["width"], item["height"], size, colors)
                        if decoded is not None:
                            key = str(palette["descriptor"])
                            palette_variants[key] = base64.b64encode(decoded).decode("ascii")
                            palette_alpha[key] = any(decoded[index] != 255 for index in range(3, len(decoded), 4))
                            item.setdefault("paletteAlphaMode", {})[key] = texture_alpha_mode(decoded)
                    if palette_variants:
                        item["paletteVariants"] = palette_variants
                        item["paletteAlpha"] = palette_alpha
                        item["hasAlpha"] = any(palette_alpha.values())
        if rgba is not None and len(rgba) <= MAX_TEXTURE_BYTES:
            item["rgba"] = base64.b64encode(rgba).decode("ascii")
            # RGBA16/IA8 alpha is part of the source texel data.  Preserve
            # that fact for the renderer so descriptor-bound surfaces can
            # use blending even when cmd23 supplies an opaque modulation
            # color.
            item["hasAlpha"] = any(rgba[index] != 255 for index in range(3, len(rgba), 4))
            item["alphaMode"] = texture_alpha_mode(rgba)
        elif not palette_variants:
            if pointer is None:
                context.warn("texture-pointer", f"texture descriptor {descriptor_index} has an unavailable pointer",
                             descriptor=descriptor_index)
            else:
                context.warn("texture-unsupported", f"texture descriptor {descriptor_index} uses unsupported format or dimensions",
                             descriptor=descriptor_index, format=fmt, size=size)
        textures.append(item)
        bindings[descriptor_index] = texture_id
    return bindings


def parse_display_list(reader: Reader, list_offset: Optional[int], context: ParseContext,
                       textures: List[Dict[str, Any]], list_name: str = "mesh",
                       shared_vertex_cache: Optional[List[Optional[Dict[str, Any]]]] = None,
                       bound_material: Optional[Dict[str, Any]] = None,
                       active_bone: Optional[int] = None,
                       shared_bone_cache: Optional[List[Optional[int]]] = None,
                       shared_geo_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if list_offset is None:
        return {"name": list_name, "vertices": [], "indices": [], "material": {"texture": None, "translucent": False}}
    commands: List[Tuple[int, int]] = []
    pending = [list_offset]
    seen_lists = set()
    vertices: List[Dict[str, Any]] = []
    indices: List[int] = []
    vertex_cache = shared_vertex_cache if shared_vertex_cache is not None else [None] * 32
    # The RSP transforms vertices at G_VTX load time with the matrix that is
    # current then; later triangles read the already-transformed cache. The
    # source models exploit this (a display list under one bone draws triangles
    # that reference vertices loaded under another bone), so the loader bone
    # must be tracked per cache slot, not per display list.
    bone_cache = shared_bone_cache if shared_bone_cache is not None else [None] * 32
    # The RSP geometry mode (lighting, face culling) persists across display
    # lists in traversal order, exactly like the vertex cache: the Beedrill
    # wing lists clear G_LIGHTING and G_CULL_BACK around their quads and
    # restore them afterwards, expecting later lists to see the restored state.
    geo_state = shared_geo_state if shared_geo_state is not None else {"lighting": True, "cullFront": False, "cullBack": True}
    geo_snapshotted = False
    texture_state: Dict[str, Any] = {"pointer": None, "format": None, "size": None, "width": 32, "height": 32}
    material: Dict[str, Any] = {"texture": None, "translucent": False, "lighting": True}
    command_count = 0

    def add_triangle(a: int, b: int, c: int) -> None:
        nonlocal geo_snapshotted
        if not geo_snapshotted:
            # A display list can flip the geometry mode around its triangles
            # (unlit, double-sided wings), so the mesh snapshots the state in
            # effect at its first triangle rather than at end of list.
            material["lighting"] = geo_state["lighting"]
            material["doubleSided"] = not (geo_state["cullFront"] or geo_state["cullBack"])
            geo_snapshotted = True
        if not (0 <= a < 32 and 0 <= b < 32 and 0 <= c < 32):
            context.warn("display-list-index", f"triangle references an unloaded vertex ({a},{b},{c})", list=list_name)
            return
        tri = [vertex_cache[a], vertex_cache[b], vertex_cache[c]]
        if any(item is None for item in tri):
            context.warn("display-list-vertex", "triangle references a missing vertex", list=list_name)
            return
        base = len(vertices)
        vertices.extend([dict(item, bone=bone_cache[slot]) for item, slot in zip(tri, (a, b, c)) if item is not None])
        indices.extend([base, base + 1, base + 2])

    while pending and command_count < MAX_DISPLAY_LIST_COMMANDS:
        current = pending.pop()
        if current in seen_lists:
            continue
        seen_lists.add(current)
        cursor = current
        for _ in range(MAX_DISPLAY_LIST_COMMANDS):
            if not reader.can(cursor, 8):
                context.warn("display-list-truncated", f"display list at 0x{current:X} ended outside fragment", list=list_name)
                break
            w0, w1 = reader.u32(cursor), reader.u32(cursor + 4)
            command_count += 1
            op = w0 >> 24
            cursor += 8
            if op in (0xB8, 0xDF):  # G_ENDDL (F3DEX/F3DEX2)
                break
            if op == 0xDE:  # G_DL in F3DEX2
                target = pointer_to_offset(w1, len(reader.data))
                if target is None:
                    context.warn("display-list-pointer", f"G_DL pointer 0x{w1:08X} is unavailable", list=list_name)
                else:
                    pending.append(target)
                continue
            if op in (0x04, 0x01):  # G_VTX in F3DEX/F3DEX2
                count = (w0 >> 12) & 0xFF
                if count == 0:
                    count = 32
                count = min(count, 32)
                if op == 0x01:  # F3DEX2 encodes the end index (v0 + n).
                    first = ((w0 >> 1) & 0x7F) - count
                else:
                    first = (w0 >> 16) & 0x7F
                if first < 0 or first >= 32:
                    context.warn("vertex-index", f"G_VTX start index {first} is outside the 32-entry cache", list=list_name)
                    continue
                target = pointer_to_offset(w1, len(reader.data))
                if target is None or not reader.can(target, count * 16):
                    context.warn("vertex-pointer", f"G_VTX pointer 0x{w1:08X} is unavailable", list=list_name)
                    continue
                for i in range(count):
                    at = target + i * 16
                    slot = (first + i) & 31
                    bone_cache[slot] = active_bone
                    vertex_cache[slot] = {
                        "position": [reader.s16(at), reader.s16(at + 2), reader.s16(at + 4)],
                        "uv": [reader.s16(at + 8) / 32.0, reader.s16(at + 10) / 32.0],
                        # Stadium 1 model display lists use the Vtx color bytes
                        # as packed normals when lighting is enabled. Keep the
                        # raw bytes for genuinely unlit lists, but do not show
                        # them as rainbow vertex colors for lit model geometry.
                        "normal": [reader.s8(at + 12) / 127.0, reader.s8(at + 13) / 127.0,
                                   reader.s8(at + 14) / 127.0],
                        "color": [reader.u8(at + 12), reader.u8(at + 13), reader.u8(at + 14), reader.u8(at + 15)],
                    }
                continue
            if op in (0xBF, 0x05):  # G_TRI1 in F3DEX/F3DEX2
                add_triangle((w0 >> 17) & 0x7F, (w0 >> 9) & 0x7F, (w0 >> 1) & 0x7F)
                continue
            if op in (0xB1, 0x06):  # G_TRI2 variants (0x06 is F3DEX2)
                add_triangle((w0 >> 17) & 0x7F, (w0 >> 9) & 0x7F, (w0 >> 1) & 0x7F)
                add_triangle((w1 >> 17) & 0x7F, (w1 >> 9) & 0x7F, (w1 >> 1) & 0x7F)
                continue
            if op == 0xFD:  # G_SETTIMG
                texture_state["format"] = (w0 >> 21) & 7
                texture_state["size"] = (w0 >> 19) & 3
                texture_state["pointer"] = pointer_to_offset(w1, len(reader.data))
                continue
            if op == 0xF2:  # G_SETTILESIZE, dimensions are in 10.2 texel units
                width = (((w1 >> 12) & 0xFFF) >> 2) + 1
                height = ((w1 & 0xFFF) >> 2) + 1
                if width > 0 and height > 0:
                    texture_state["width"], texture_state["height"] = min(width, 2048), min(height, 2048)
                continue
            if op in (0xF3, 0xF4, 0xF5, 0xF0):
                continue
            if op in (0xE2, 0xE3):
                # Other-mode L/H carries the RDP render mode.  The alpha
                # blender is intentionally treated as an indicator, not as a
                # claim about every pixel's final alpha.
                if op == 0xE2 and (w1 & 0x0000C000):
                    material["translucent"] = True
                    material["alphaMode"] = "blend"
                continue
            if op == 0xD9:  # G_GEOMETRYMODE in F3DEX2: clear = ~w0, set = w1
                clear = (~w0) & 0xFFFFFF
                mode = (0x200 if geo_state["cullFront"] else 0) | (0x400 if geo_state["cullBack"] else 0) | (0x20000 if geo_state["lighting"] else 0)
                mode = (mode & ~clear) | (w1 & 0xFFFFFF)
                geo_state["cullFront"] = bool(mode & 0x200)
                geo_state["cullBack"] = bool(mode & 0x400)
                geo_state["lighting"] = bool(mode & 0x20000)
                continue
            if op == 0xD8:  # G_POPMTX in F3DEX2; the viewer does not track the matrix stack
                continue
            # State-only RDP commands are safe to ignore.  Unknown commands are
            # reported once per list so a new format is visible to the user.
            if op not in (0x03, 0x07, 0xD7, 0xDA, 0xDB, 0xDC, 0xDF, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF, 0xF1, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFE, 0xFF):
                context.warn("display-list-command", f"unsupported F3DEX opcode 0x{op:02X}", list=list_name)
        if command_count >= MAX_DISPLAY_LIST_COMMANDS:
            context.warn("display-list-limit", "display-list command limit reached", list=list_name)

    if texture_state["pointer"] is not None:
        pointer = texture_state["pointer"]
        width, height = texture_state["width"], texture_state["height"]
        fmt, size = texture_state["format"], texture_state["size"]
        rgba = None
        if fmt == 0 and size == 2:  # RGBA16
            rgba = decode_rgba16(reader.data, pointer, width, height)
        elif fmt == 0 and size == 3:  # RGBA32
            rgba = decode_rgba32(reader.data, pointer, width, height)
        if rgba is not None and len(rgba) <= MAX_TEXTURE_BYTES:
            texture_id = len(textures)
            textures.append({
                "id": texture_id, "width": width, "height": height,
                "format": f"fmt{fmt}/siz{size}",
                "rgba": base64.b64encode(rgba).decode("ascii"),
                "hasAlpha": any(rgba[index] != 255 for index in range(3, len(rgba), 4)),
            })
            material["texture"] = texture_id
            if not material.get("translucent"):
                material["alphaMode"] = textures[texture_id].get("alphaMode", "opaque")
        else:
            material["textureRef"] = {"offset": pointer, "format": fmt, "size": size, "width": width, "height": height}
            context.warn("texture-unsupported", f"texture at 0x{pointer:X} could not be decoded", list=list_name)
    if bound_material:
        if bound_material.get("texture") is not None:
            material["texture"] = bound_material["texture"]
            material.pop("textureRef", None)
        if bound_material.get("color") is not None:
            material["color"] = bound_material["color"]
        if bound_material.get("translucent"):
            material["translucent"] = True
            material["alphaMode"] = "blend"
        for key in ("textureDescriptor", "textureSecondDescriptor", "textureAnimIndex", "textureMode", "textureSetup", "wrapS", "wrapT", "nodeMode"):
            if key in bound_material:
                material[key] = bound_material[key]
        texture_id = material.get("texture")
        if texture_id is not None and 0 <= texture_id < len(textures):
            if not material.get("translucent"):
                material["alphaMode"] = textures[texture_id].get("alphaMode", "opaque")
    return {"name": list_name, "vertices": vertices, "indices": indices, "material": material}


def parse_geo_layout(reader: Reader, layout_offset: Optional[int], context: ParseContext,
                     textures: List[Dict[str, Any]], palettes: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Interpret the source-backed GeoLayout stream used by model header A."""
    if layout_offset is None or not reader.can(layout_offset, 1):
        context.warn("geo-layout-missing", "model GeoLayout pointer is missing or outside the fragment")
        return [], []
    meshes: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    returns: List[int] = []
    # gCurGraphNodeList analog (geo_layout.c func_80017AC4): every created
    # node is registered at the current graph index, and the open/close node
    # commands only move that index — the slot at a level is not restored when
    # a sibling subtree closes. A part's parent and a display list's bone are
    # the nearest animated-part ancestor, found by walking the registration
    # list downwards from the current index.
    graph_nodes: List[Optional[int]] = [None] * 64  # part id or GRAPH_OTHER
    graph_depth = 0
    vertex_cache: List[Optional[Dict[str, Any]]] = [None] * 32
    vertex_bone_cache: List[Optional[int]] = [None] * 32
    geo_state: Dict[str, Any] = {"lighting": True, "cullFront": False, "cullBack": True}
    texture_bindings: Dict[int, int] = {}
    palette_table = palettes if palettes is not None else []
    # cmd23 (Geo_NodeShadowTexture) updates the renderer's active texture
    # state. Geo_NodeShadowTexture does not restore the previous state after
    # processing its children; later display-list nodes keep the last bound
    # state until another cmd23 changes it. This is observable in Haunter
    # (92.bin): the purple spike branch binds descriptor 12, and the later
    # spike display lists retain that binding after the branch closes.
    active_material: Optional[Dict[str, Any]] = None
    pc = layout_offset
    command_count = 0
    unsupported = set()

    def current_material() -> Optional[Dict[str, Any]]:
        return active_material

    def current_part() -> Optional[int]:
        level = graph_depth - 1
        while level >= 0:
            node = graph_nodes[level]
            if node is not None and node != GRAPH_OTHER:
                return node
            level -= 1
        return None

    def add_display_list(pointer_value: int, bone: Optional[int], label: str, node_mode: Optional[int] = None) -> None:
        # Geo_NodeDisplayList* treats a null display-list pointer as an
        # intentional no-op. Several model layouts keep optional visual/effect
        # branches in this form, so it is not a missing resource diagnostic.
        if pointer_value == 0:
            return
        display_list = pointer_to_offset(pointer_value, len(reader.data))
        if display_list is None:
            context.warn("display-list-pointer", f"GeoLayout display-list pointer 0x{pointer_value:08X} is unavailable", list=label)
            return
        material = current_material()
        material = dict(material) if material else None
        if material is not None and node_mode is not None:
            material["nodeMode"] = node_mode
        parsed = parse_display_list(reader, display_list, context, textures, label, vertex_cache, material,
                                    active_bone=bone, shared_bone_cache=vertex_bone_cache, shared_geo_state=geo_state)
        parsed_material = parsed.get("material") or {}
        # Event-track surfaces are the model's expression/overlay meshes.  A
        # number of S2 records place these cutout quads inside the head volume;
        # retain their source identity so the frontend can draw them in the
        # expression pass after opaque body surfaces.
        if (parsed_material.get("textureAnimIndex", -1) >= 0
                and parsed_material.get("textureDescriptor", -1) >= 0):
            parsed_material["renderLayer"] = "expression"
        if parsed.get("indices"):
            parsed["bone"] = bone
            meshes.append(parsed)

    while pc is not None and command_count < MAX_DISPLAY_LIST_COMMANDS:
        if not reader.can(pc, 1):
            context.warn("geo-layout-truncated", f"GeoLayout command at 0x{pc:X} is outside the fragment")
            break
        command = reader.u8(pc)
        size = GEO_LAYOUT_SIZES.get(command)
        if size is None or not reader.can(pc, size):
            context.warn("geo-layout-command", f"unsupported or truncated GeoLayout command 0x{command:02X}", offset=pc)
            break
        command_count += 1

        if command in (0x00, 0x02, 0x03):
            target = pointer_to_offset(reader.u32(pc + 4), len(reader.data))
            if target is None:
                context.warn("geo-layout-pointer", f"GeoLayout command 0x{command:02X} has an unavailable target", offset=pc)
                if command == 0x03:
                    pc += size
                else:
                    break
                continue
            if command in (0x00, 0x03):
                returns.append(pc + size)
            pc = target
            continue
        if command in (0x01, 0x04):
            if returns:
                pc = returns.pop()
                continue
            break
        if command == 0x05:
            graph_depth += 1
            if graph_depth >= len(graph_nodes):
                context.warn("geo-layout-stack", "GeoLayout open-node exceeds the graph depth limit", offset=pc)
                graph_depth = len(graph_nodes) - 1
        elif command == 0x06:
            if graph_depth > 0:
                graph_depth -= 1
            else:
                context.warn("geo-layout-stack", "GeoLayout close-node has no matching open-node", offset=pc)
        elif command == 0x1D:  # geo_layout_cmd_animated_part / ModelPart
            part_id = reader.u8(pc + 1)
            command_flags = reader.u8(pc + 2)
            # The GeoLayout byte is translated by func_80018490 before it is
            # stored in the runtime node. Bit 0 selects the normal
            # scale-stack branch (node bit 0 clear); command bit 1 is carried
            # through to node bit 1. Passing command_flags through unchanged
            # makes the non-unit-scale animation families use the TRS branch.
            flags = 1
            if command_flags & 1:
                flags = 0
            if command_flags & 2:
                flags |= 2
            joint = reader.u8(pc + 3)
            parent = current_part()
            nodes.append({
                "kind": "bone", "offset": pc, "type": 0x14, "part": part_id,
                "joint": joint, "flags": flags, "parent": parent,
                "position": read_vec3s(reader, pc + 4),
                "rotation": read_vec3s(reader, pc + 0x0A),
                # The S2 model bank uses 0xFFFFFFFF for the unit scale of a
                # billboard/effect branch.  The S1 source conversion divides
                # this field by 65536.0, but carrying the sentinel through as
                # 65536 makes camera fitting include an enormous off-model
                # quad (Espeon #196).  S1 model fragments do not use this
                # sentinel; preserve ordinary Q16.16 values unchanged.
                "scale": [geo_scale_component(reader, pc + 0x10),
                          geo_scale_component(reader, pc + 0x14),
                          geo_scale_component(reader, pc + 0x18)],
            })
            graph_nodes[graph_depth] = part_id
        elif command == 0x17:  # source GeoLayout texture descriptor table
            parse_palette_descriptors(
                reader, pointer_to_offset(reader.u32(pc + 0x0C), len(reader.data)), reader.s16(pc + 4), context, palette_table
            )
            texture_bindings = parse_texture_descriptors(
                reader, pointer_to_offset(reader.u32(pc + 8), len(reader.data)), reader.s16(pc + 2), context, textures,
                palette_table,
            )
            graph_nodes[graph_depth] = GRAPH_OTHER
        elif command == 0x23:  # source GeoLayout texture/shadow binding
            descriptor_index = reader.s16(pc + 8)
            second_descriptor_index = reader.s16(pc + 0x0A)
            texture_anim_index = reader.s16(pc + 2)
            setup = pointer_to_offset(reader.u32(pc + 4), len(reader.data))
            color = [reader.u8(pc + 0x0C), reader.u8(pc + 0x0D), reader.u8(pc + 0x0E), reader.u8(pc + 0x0F)]
            wrap_s, wrap_t = texture_setup_sampler(reader, setup)
            # cmd23's mode 1 tells the game to use white rather than the
            # command color; the descriptor index is the signed +0x08 field
            # in the source struct, not the setup display-list pointer.
            bound_material = {
                "texture": texture_bindings.get(descriptor_index),
                "textureDescriptor": descriptor_index,
                "textureSecondDescriptor": second_descriptor_index,
                "textureAnimIndex": texture_anim_index,
                "textureMode": reader.u8(pc + 1),
                "color": [255, 255, 255, 255] if reader.u8(pc + 1) == 1 else color,
                "translucent": color[3] < 255,
                "textureSetup": setup,
                "wrapS": wrap_s,
                "wrapT": wrap_t,
            }
            active_material = bound_material
            graph_nodes[graph_depth] = GRAPH_OTHER
        elif command == 0x1E:  # geo_layout_cmd_display_list_part
            part_id = reader.s16(pc + 2)
            add_display_list(reader.u32(pc + 4), part_id if part_id >= 0 else current_part(), f"geo-dl@0x{pc:X}", reader.u8(pc + 1))
            graph_nodes[graph_depth] = GRAPH_OTHER
        elif command == 0x20:  # display-list matrix node
            add_display_list(reader.u32(pc + 0x10), current_part(), f"geo-matrix-dl@0x{pc:X}", reader.u8(pc + 1))
            graph_nodes[graph_depth] = GRAPH_OTHER
        elif command == 0x21:  # display-list scale node
            add_display_list(reader.u32(pc + 0x0C), current_part(), f"geo-scale-dl@0x{pc:X}", reader.u8(pc + 1))
            graph_nodes[graph_depth] = GRAPH_OTHER
        elif command == 0x22:  # ordinary display-list node
            add_display_list(reader.u32(pc + 4), current_part(), f"geo-display-dl@0x{pc:X}", reader.u8(pc + 1))
            graph_nodes[graph_depth] = GRAPH_OTHER
        elif command not in (0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
                             0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x18, 0x19,
                             0x1A, 0x1B, 0x1C, 0x1F, 0x21, 0x24, 0x25, 0x26, 0x28, 0x29):
            if command not in unsupported:
                context.warn("geo-layout-command", f"GeoLayout command 0x{command:02X} is not projected into the viewer", offset=pc)
                unsupported.add(command)
        else:
            # Remaining known commands create or touch graph nodes without a
            # transform; track the registration so parent walks stay faithful.
            # cmd 0x08 only modifies the current node and registers nothing.
            if command != 0x08:
                graph_nodes[graph_depth] = GRAPH_OTHER
        pc += size

    if command_count >= MAX_DISPLAY_LIST_COMMANDS:
        context.warn("geo-layout-limit", "GeoLayout command limit reached")
    return meshes, nodes


def parse_geo(reader: Reader, geometry_offset: Optional[int], context: ParseContext,
              textures: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if geometry_offset is None or not reader.can(geometry_offset, 0x18):
        context.warn("geometry-missing", "model geometry/display-list root is missing")
        return [], []
    nodes: List[Dict[str, Any]] = []
    meshes: List[Dict[str, Any]] = []
    visited = set()

    def walk(offset: Optional[int], parent_bone: Optional[int] = None) -> None:
        if offset is None or offset in visited or not reader.can(offset, 0x18):
            return
        visited.add(offset)
        try:
            node_type = reader.u8(offset)
            child = pointer_to_offset(reader.u32(offset + 0x0C), len(reader.data))
            next_node = pointer_to_offset(reader.u32(offset + 0x08), len(reader.data))
        except FormatError:
            return
        current_bone = parent_bone
        node: Dict[str, Any] = {"offset": offset, "type": node_type}
        if node_type == 0x12 and reader.can(offset, 0x34):  # Geo_NodeAnimatedPart
            current_bone = reader.u8(offset + 0x30)
            node.update({
                "kind": "bone", "part": current_bone, "flags": reader.u8(offset + 0x31),
                "joint": reader.s16(offset + 0x32), "position": read_vec3s(reader, offset + 0x18),
                "rotation": read_vec3s(reader, offset + 0x1E), "scale": read_vec3f(reader, offset + 0x24),
                "parent": parent_bone,
            })
            nodes.append(node)
        elif node_type in (0x13, 0x17) and reader.can(offset, 0x20):
            dl = pointer_to_offset(reader.u32(offset + 0x18), len(reader.data))
            if node_type == 0x13:
                current_bone = reader.s16(offset + 0x1C)
            node.update({"kind": "mesh", "bone": current_bone, "displayList": dl})
            parsed = parse_display_list(reader, dl, context, textures, f"dl@0x{offset:X}", active_bone=current_bone)
            parsed["bone"] = current_bone
            meshes.append(parsed)
            nodes.append(node)
        elif node_type == 0x14:
            node["kind"] = "model"
            nodes.append(node)
        else:
            node["kind"] = "node"
            nodes.append(node)
        walk(child, current_bone)
        walk(next_node, parent_bone)

    walk(geometry_offset)
    return meshes, nodes


def find_model_root(data: bytes, context: ParseContext) -> Optional[int]:
    entry = decode_entry_offset(data, context)
    payload = decode_payload_pointer(data, entry, context)
    candidates = []
    if payload is not None:
        candidates.append(payload)
    if entry is not None:
        candidates.append(entry)
    # Fallback for incomplete hand-built assets: locate the documented root
    # shape without assuming the entry stub is at +0x20.
    scan_end = min(len(data) - 0x18, 0x20000)
    for offset in range(0x20, max(0x20, scan_end), 4):
        if data[offset + 2:offset + 4] == b"\0\1" and data[offset + 6:offset + 8] == b"\0\0":
            animation_count = data[offset + 5]
            geometry = pointer_to_offset(u32(data, offset + 8), len(data))
            animation_list = pointer_to_offset(u32(data, offset + 0x0C), len(data))
            if animation_count <= 64 and geometry is not None and (animation_count == 0 or animation_list is not None):
                candidates.append(offset)
    for candidate in candidates:
        if candidate is None or candidate + 0x18 > len(data):
            continue
        try:
            if context.data[candidate + 2:candidate + 4] == b"\0\1":
                return candidate
        except Exception:
            pass
    context.error("model-root", "could not locate the Stadium 1 model root")
    return None


def parse_fragment(data: bytes, name: str = "model", catalog_only: bool = False) -> Dict[str, Any]:
    context = ParseContext(data)
    reader = Reader(data, context)
    result: Dict[str, Any] = {
        "kind": "model", "name": name, "format": "FRAGMENT", "size": len(data),
        "fragment": {}, "animations": [], "meshes": [], "skeleton": {"bones": []}, "textures": [], "palettes": [],
        "diagnostics": context.diagnostics,
    }
    if len(data) < 0x20 or data[8:16] != b"FRAGMENT":
        context.error("fragment-magic", "resource is not a FRAGMENT overlay")
        return result
    try:
        header_size, reloc_offset, size_rom, size_ram = struct.unpack_from(">4I", data, 0x10)
        result["fragment"] = {"headerSize": header_size, "relocOffset": reloc_offset, "sizeInRom": size_rom, "sizeInRam": size_ram}
        if reloc_offset + 4 <= len(data):
            count = u32(data, reloc_offset)
            entries = []
            kinds: Dict[str, int] = {}
            for i in range(min(count, max(0, (len(data) - reloc_offset - 4) // 4))):
                word = u32(data, reloc_offset + 4 + i * 4)
                kind = {2: "R_MIPS_32", 4: "R_MIPS_26", 5: "R_MIPS_HI16", 6: "R_MIPS_LO16"}.get(word >> 24, f"type{word >> 24}")
                kinds[kind] = kinds.get(kind, 0) + 1
                entries.append({"type": kind, "offset": word & 0xFFFFFF})
            result["fragment"]["relocations"] = {"count": count, "kinds": kinds, "entries": entries[:64]}
            if count > len(entries):
                context.warn("relocations-truncated", "relocation table is shorter than its declared count")
        else:
            context.warn("relocations-missing", "fragment relocation table is outside the resource")
    except struct.error:
        context.error("fragment-header", "FRAGMENT header is truncated")
        return result

    root = find_model_root(data, context)
    if root is None:
        return result
    try:
        model_id, model_version = reader.u16(root), reader.u16(root + 2)
        static_variant, animation_count = reader.u8(root + 4), reader.u8(root + 5)
        geometry_offset = pointer_to_offset(reader.u32(root + 8), len(data))
        animation_list_offset = pointer_to_offset(reader.u32(root + 0x0C), len(data))
        descriptor_list_offset = pointer_to_offset(reader.u32(root + 0x10), len(data))
        result["modelId"] = model_id
        result["modelVersion"] = model_version
        result["animationSlotCount"] = animation_count
        result["rootOffset"] = root
        result["fragment"]["payloadOffset"] = root
        if geometry_offset is None:
            context.warn("geometry-pointer", "geometry pointer is missing or non-local")
        geometry_header_offset = geometry_offset
        geo_layout_offset = None
        if geometry_header_offset is not None and reader.can(geometry_header_offset, 4):
            first_pointer = pointer_to_offset(reader.u32(geometry_header_offset), len(data))
            if first_pointer is not None and reader.u8(first_pointer) in GEO_LAYOUT_SIZES:
                geo_layout_offset = first_pointer
        result["geometryHeaderOffset"] = geometry_header_offset
        if not catalog_only:
            if geo_layout_offset is not None:
                result["geoLayoutOffset"] = geo_layout_offset
                meshes, nodes = parse_geo_layout(reader, geo_layout_offset, context, result["textures"], result["palettes"])
            else:
                meshes, nodes = parse_geo(reader, geometry_offset, context, result["textures"])
            result["meshes"] = meshes
            bones = []
            for node in nodes:
                if node.get("kind") == "bone":
                    bones.append({
                        "id": node["part"], "joint": node["joint"], "parent": node.get("parent"),
                        "poseIndex": node["joint"] if node["joint"] >= 0 else None,
                        "flags": node.get("flags", 0),
                        "position": [float(x) for x in node["position"]],
                        "rotation": [float(x) for x in node["rotation"]],
                        "scale": [float(x) for x in node["scale"]],
                        "name": f"bone_{node['part']}",
                    })
            result["skeleton"] = {"bones": bones, "nodeCount": len(nodes)}
        elif geo_layout_offset is not None:
            result["geoLayoutOffset"] = geo_layout_offset
        event_tracks: List[Dict[str, Any]] = []
        if descriptor_list_offset is not None:
            for event_id in range(animation_count):
                pointer_at = descriptor_list_offset + event_id * 4
                track_offset = pointer_to_offset(reader.u32(pointer_at), len(data)) if reader.can(pointer_at, 4) else None
                event_tracks.append(parse_event_track(reader, track_offset, context))
        result["eventTracks"] = event_tracks
        for animation_id in range(animation_count):
            curve = None
            curve_offset = None
            if animation_list_offset is not None:
                pointer_at = animation_list_offset + animation_id * 4
                if reader.can(pointer_at, 4):
                    curve_offset = pointer_to_offset(reader.u32(pointer_at), len(data))
                    curve = parse_curve(reader, curve_offset, context, metadata_only=catalog_only)
            if curve is None:
                curve = {"supported": False, "frameCount": 0, "reason": "animation list is missing"}
            result["animations"].append({
                "id": animation_id, "name": f"animation_{animation_id}",
                "frameCount": curve.get("frameCount", 0), "curveOffset": curve_offset,
                "supported": curve.get("supported", False), "curve": curve,
                "eventTrack": event_tracks[animation_id] if animation_id < len(event_tracks) else None,
            })
        # Cmd17 tables often reserve trailing placeholder descriptors.  They
        # are not missing viewer resources when no mesh or event-track mapping
        # references them. Keep diagnostics for referenced unavailable data,
        # but avoid presenting unused source placeholders as broken textures.
        used_texture_descriptors = set()
        for mesh in result["meshes"]:
            material = mesh.get("material") or {}
            descriptor = material.get("textureDescriptor")
            if isinstance(descriptor, int) and descriptor >= 0:
                used_texture_descriptors.add(descriptor)
        for track in event_tracks:
            used_texture_descriptors.update(
                int(value) for value in track.get("mapping", [])
                if isinstance(value, int) and value >= 0
            )
        result["diagnostics"][:] = [
            diagnostic for diagnostic in result["diagnostics"]
            if not (
                diagnostic.get("code") in {"texture-pointer", "texture-unsupported"}
                and isinstance(diagnostic.get("descriptor"), int)
                and diagnostic["descriptor"] not in used_texture_descriptors
            )
        ]
        if animation_count and animation_list_offset is None:
            context.warn("animation-list", "model declares animations but its animation list is unavailable")
        if animation_count and descriptor_list_offset is None:
            context.warn("descriptor-list", "animation descriptor list is unavailable; names remain generic")
        if not catalog_only and not result["meshes"]:
            context.warn("empty-model", "no supported Geo display-list meshes were found")
    except (FormatError, struct.error) as exc:
        context.error("model-layout", str(exc))
    return result


def derived_cache_dir() -> Path:
    """Return an OS cache directory outside the repository and ROM folders."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "pokemon-stadium-model-viewer" / "derived"
    return Path.home() / ".cache" / "pokemon-stadium-model-viewer" / "derived"


def decode_stadium2_model_blob(blob: bytes) -> bytes:
    """Decode an S2 model record before interpreting model-local pointers."""
    if blob[:8] == b"PERS-SZP":
        payload, _ = unpack_persszp(blob)
        return payload
    if blob[:4] == b"Yay0":
        return decode_yay0(blob)
    return blob


def parse_resource(blob: bytes, name: str = "resource", catalog_only: bool = False) -> Dict[str, Any]:
    """Parse a direct resource and return a JSON-safe catalog/model object."""
    try:
        if blob[:1] == b"{" or blob[:1] == b"[":
            loaded = json.loads(blob.decode("utf-8"))
            if isinstance(loaded, dict):
                loaded.setdefault("name", name)
                loaded.setdefault("diagnostics", [])
                return loaded
        archive = parse_archive(blob)
        if archive is not None:
            return {"kind": "archive", "name": name, "format": "BinArchive", "size": len(blob),
                    "archive": archive, "diagnostics": []}
        if blob[:8] == b"PERS-SZP":
            decoded, info = unpack_persszp(blob)
            parsed = parse_fragment(decoded, name, catalog_only=catalog_only)
            parsed["packed"] = info
            return parsed
        if blob[8:16] == b"FRAGMENT":
            return parse_fragment(blob, name, catalog_only=catalog_only)
        return {"kind": "resource", "name": name, "format": "unknown", "size": len(blob),
                "diagnostics": [{"severity": "warning", "code": "unsupported-resource",
                                 "message": "resource is neither BinArchive, PERS-SZP, FRAGMENT, nor viewer JSON"}]}
    except (FormatError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"kind": "resource", "name": name, "format": "invalid", "size": len(blob),
                "diagnostics": [{"severity": "error", "code": "parse-failed", "message": str(exc)}]}


def apply_stadium2_runtime_tint(model: Dict[str, Any], model_index: int) -> int:
    """Apply the isolated S2 body-colour bridge to unbound model branches.

    The extracted model remains authoritative for topology, textures, and
    command bindings.  This only fills the external colour state that the
    Stadium 2 runtime supplies for the listed mode-1/descriptor--1 body
    branches.  Returning the count makes the bridge easy to test and audit.
    """
    tint = STADIUM2_DARK_BODY_TINTS.get(model_index)
    if tint is None:
        return 0
    applied = 0
    for mesh in model.get("meshes", []):
        material = mesh.get("material") or {}
        # Mode 0 is a separate untextured detail/material path in the S2
        # layouts (Murkrow's eyes/beak use it).  It must retain its source
        # vertex colour.  Unown's body is the documented mode-3 exception.
        allowed_modes = STADIUM2_TINT_TEXTURE_MODES.get(model_index, (1,))
        if (material.get("textureDescriptor") != -1
                or material.get("texture") is not None
                or material.get("textureMode") not in allowed_modes):
            continue
        material["color"] = list(tint)
        material["runtimeTint"] = "stadium2-dark-body-bridge"
        applied += 1
    if applied:
        model["s2RuntimeTint"] = {
            "source": "external Stadium 2 material state; temporary bridge",
            "modelIndex": model_index,
            "rgba": list(tint),
            "meshCount": applied,
            "textureModes": list(allowed_modes),
            "exact": False,
        }
    return applied


def infer_stadium2_auxiliary_textures(model: Dict[str, Any]) -> int:
    """Recover narrow S2 auxiliary bindings from their source UV footprints.

    A few S2 model fragments leave an auxiliary marking/material branch at
    ``descriptor == -1`` even though the branch's UVs exactly span a small
    texture descriptor in the same cmd17 table.  This is especially clear in
    Umbreon: the yellow bands/rings use the 64x2 descriptor, while the body
    UVs extend far beyond that footprint.  Only an unbound branch with one
    unambiguous small-texture fit is repaired; ordinary 32x32 body candidates
    and ambiguous equal-sized textures remain untouched.
    """
    textures = [
        item for item in model.get("textures", [])
        if isinstance(item.get("descriptor"), int)
        and int(item.get("width", 0)) > 1
        and 1 < int(item.get("height", 0)) <= 4
        and item.get("rgba")
    ]
    inferred = 0
    records: List[Dict[str, Any]] = []
    for mesh_index, mesh in enumerate(model.get("meshes", [])):
        material = mesh.get("material") or {}
        if (material.get("textureDescriptor") != -1
                or material.get("texture") is not None
                or material.get("textureMode") != 1):
            continue
        uvs = [vertex.get("uv", [0, 0]) for vertex in mesh.get("vertices", [])]
        if not uvs:
            continue
        candidates = [
            item for item in textures
            if all(
                -0.01 <= float(uv[0]) <= int(item["width"]) + 0.01
                and -0.01 <= float(uv[1]) <= int(item["height"]) + 0.01
                for uv in uvs
            )
        ]
        if len(candidates) != 1:
            continue
        texture = candidates[0]
        descriptor = int(texture["descriptor"])
        material["texture"] = int(texture["id"])
        material["textureDescriptor"] = descriptor
        material["color"] = [255, 255, 255, 255]
        material["alphaMode"] = texture.get("alphaMode", "opaque")
        material["textureInference"] = "s2-uv-footprint"
        inferred += 1
        records.append({"mesh": mesh_index, "descriptor": descriptor,
                        "width": int(texture["width"]), "height": int(texture["height"])})
    if records:
        model["s2InferredTextureBindings"] = {
            "source": "same-fragment cmd17 descriptor and exact auxiliary UV footprint",
            "exact": False,
            "bindings": records,
        }
    return inferred


class Stadium1DataProvider:
    """Provider for Stadium 1 files and extracted assets.

    The viewer only consumes this normalized provider contract.  A future
    Stadium2DataProvider must implement its own archive/index/curve decoding;
    Stadium 2's separate, length-prefixed animation archive is not accepted by
    this class as a Stadium 1 pointer list.
    """

    game_id = "stadium1"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._catalog_cache: Optional[List[Dict[str, Any]]] = None

    def _safe_path(self, relative: str) -> Path:
        relative = relative.split("#", 1)[0]
        if self.root.is_file():
            if relative in ("", self.root.name, str(self.root)):
                return self.root
            raise ValueError("single-file asset provider cannot resolve another path")
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("resource path escapes the provider root")
        return candidate

    def _iter_files(self) -> Iterable[Path]:
        if self.root.is_file():
            yield self.root
            return
        supported_suffixes = {"", ".asset", ".bin", ".dat", ".fragment", ".frag", ".json", ".pers", ".persszp", ".szp"}
        count = 0
        for path in self.root.rglob("*"):
            # The decomp's extracted pokemon_models directory contains the
            # complete Stadium 1 roster at archive indices 0..150 (species
            # 1..151), followed by unrelated/other model resources. Keep the
            # provider's Stadium 1 boundary explicit and avoid parsing those
            # extra files just to discard them later.
            if path.parent == self.root and self.root.name.casefold() == "pokemon_models":
                if path.suffix.lower() == ".bin" and path.stem.isdigit():
                    file_index = int(path.stem)
                    if not STADIUM1_MODEL_FILE_MIN <= file_index <= STADIUM1_MODEL_FILE_MAX:
                        continue
            if path.is_file() and path.suffix.lower() in supported_suffixes:
                yield path
                count += 1
                if count >= MAX_SCAN_FILES:
                    break

    def _ref_name(self, path: Path) -> str:
        if self.root.is_file():
            return path.name
        return path.relative_to(self.root).as_posix()

    def _load_blob(self, reference: str) -> Tuple[bytes, str]:
        path = self._safe_path(reference)
        if not path.is_file():
            raise FileNotFoundError(reference)
        data = path.read_bytes()
        if "#" in reference:
            suffix = reference.rsplit("#", 1)[1]
            if suffix.isdigit():
                archive = parse_archive(data)
                if archive is None:
                    raise FormatError("resource does not contain a BinArchive")
                index = int(suffix)
                if index < 0 or index >= archive["fileCount"]:
                    raise IndexError(f"archive file {index} out of range")
                item = archive["files"][index]
                data = data[item["offset"]:item["offset"] + item["size"]]
                return data, f"{path.name}[{index}]"
        return data, path.name

    def catalog(self) -> List[Dict[str, Any]]:
        if self._catalog_cache is not None:
            return self._catalog_cache
        entries: List[Dict[str, Any]] = []
        for path in self._iter_files():
            reference = self._ref_name(path)
            try:
                data = path.read_bytes()
                archive = parse_archive(data)
                if archive is not None:
                    for item in archive["files"]:
                        child_ref = f"{reference}#{item['index']}"
                        child = data[item["offset"]:item["offset"] + item["size"]]
                        parsed = parse_resource(child, child_ref, catalog_only=True)
                        if parsed.get("kind") == "model" and stadium1_model_id_supported(parsed):
                            model_id = int(parsed["modelId"])
                            identity = model_identity("stadium1", model_id, child_ref, stadium1_model_name(model_id, child_ref))
                            entries.append({
                                "kind": "model", **identity, "path": child_ref,
                                "size": item["size"], "animations": summarize_animations(parsed),
                                "diagnostics": parsed.get("diagnostics", []),
                            })
                    continue
                parsed = parse_resource(data, reference, catalog_only=True)
                if parsed.get("kind") == "model" and stadium1_model_id_supported(parsed):
                    model_id = int(parsed["modelId"])
                    identity = model_identity("stadium1", model_id, reference, stadium1_model_name(model_id, reference))
                    entries.append({
                        "kind": "model", **identity, "path": reference,
                        "size": len(data), "animations": summarize_animations(parsed),
                        "diagnostics": parsed.get("diagnostics", []),
                    })
            except Exception as exc:
                entries.append({"kind": "resource", "provider": "stadium1", "providerAlias": "s1", "name": reference, "path": reference,
                                "size": path.stat().st_size if path.exists() else 0,
                                "animations": [], "diagnostics": [{"severity": "error", "code": "catalog-failed", "message": str(exc)}]})
        self._catalog_cache = sorted(entries, key=lambda item: item["name"].lower())
        return self._catalog_cache

    def load_model(self, reference: str) -> Dict[str, Any]:
        data, display_name = self._load_blob(reference)
        model = parse_resource(data, display_name)
        if model.get("kind") == "model" and not stadium1_model_id_supported(model):
            model_id = model.get("modelId", "unknown")
            raise FormatError(
                f"Stadium 1 provider accepts model IDs {STADIUM1_MODEL_ID_MIN}..{STADIUM1_MODEL_ID_MAX}; "
                f"resource has model ID {model_id}"
            )
        if model.get("kind") == "model":
            model_id = int(model["modelId"])
            model.update(model_identity("stadium1", model_id, reference, stadium1_model_name(model_id, display_name)))
            model["name"] = model_identity("stadium1", model_id, display_name, stadium1_model_name(model_id, display_name))["name"]
        return model


class Stadium1RomDataProvider(Stadium1DataProvider):
    """Provider for the source-defined Stadium 1 model archive inside a ROM."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_file():
            raise FileNotFoundError(f"Stadium 1 ROM not found: {self.root}")
        data = normalize_n64_rom(self.root.read_bytes())
        data = data[STADIUM1_MODEL_ARCHIVE_ROM_START:STADIUM1_MODEL_ARCHIVE_ROM_END]
        if len(data) != STADIUM1_MODEL_ARCHIVE_ROM_END - STADIUM1_MODEL_ARCHIVE_ROM_START:
            raise FormatError(
                f"{self.root.name} is shorter than the Stadium 1 model archive section "
                f"(0x{STADIUM1_MODEL_ARCHIVE_ROM_END:X} bytes required)"
            )
        archive = parse_archive(data)
        if archive is None:
            raise FormatError(
                f"{self.root.name} has no valid Stadium 1 BinArchive at "
                f"ROM 0x{STADIUM1_MODEL_ARCHIVE_ROM_START:X}"
            )
        self._archive_blob = data
        self._model_archive = archive
        self._catalog_cache: Optional[List[Dict[str, Any]]] = None

    def _reference_index(self, reference: str) -> int:
        if not reference.startswith("s1-rom:"):
            raise ValueError("Stadium 1 ROM model references must use s1-rom:NNN")
        value = reference.split(":", 1)[1]
        if not value.isdigit():
            raise ValueError(f"invalid Stadium 1 ROM model reference {reference}")
        index = int(value)
        if index < 0 or index >= self._model_archive["fileCount"]:
            raise IndexError(f"Stadium 1 ROM model index {index} is out of range")
        return index

    def _entry_blob(self, index: int) -> Tuple[bytes, Dict[str, Any]]:
        item = self._model_archive["files"][index]
        return (
            self._archive_blob[item["offset"]:item["offset"] + item["size"]],
            item,
        )

    def catalog(self) -> List[Dict[str, Any]]:
        if self._catalog_cache is not None:
            return self._catalog_cache
        entries: List[Dict[str, Any]] = []
        for item in self._model_archive["files"]:
            index = int(item["index"])
            reference = f"s1-rom:{index:03d}"
            try:
                child, _ = self._entry_blob(index)
                parsed = parse_resource(child, reference, catalog_only=True)
                if parsed.get("kind") != "model" or not stadium1_model_id_supported(parsed):
                    continue
                model_id = int(parsed["modelId"])
                identity = model_identity("stadium1", model_id, reference, stadium1_model_name(model_id, reference))
                entries.append({
                    "kind": "model", **identity, "path": reference,
                    "size": item["size"], "animations": summarize_animations(parsed),
                    "diagnostics": parsed.get("diagnostics", []),
                })
            except Exception as exc:
                entries.append({
                    "kind": "resource", "provider": "stadium1", "providerAlias": "s1",
                    "name": reference, "path": reference, "size": item["size"],
                    "animations": [],
                    "diagnostics": [{"severity": "error", "code": "catalog-failed", "message": str(exc)}],
                })
        self._catalog_cache = sorted(entries, key=lambda item: item["name"].lower())
        return self._catalog_cache

    def load_model(self, reference: str) -> Dict[str, Any]:
        index = self._reference_index(reference)
        data, item = self._entry_blob(index)
        model = parse_resource(data, reference)
        if model.get("kind") != "model":
            raise FormatError(f"Stadium 1 ROM record {index} did not decode as a model")
        if not stadium1_model_id_supported(model):
            raise FormatError(
                f"Stadium 1 provider accepts model IDs {STADIUM1_MODEL_ID_MIN}..{STADIUM1_MODEL_ID_MAX}; "
                f"resource has model ID {model.get('modelId', 'unknown')}"
            )
        model_id = int(model["modelId"])
        identity = model_identity("stadium1", model_id, reference, stadium1_model_name(model_id, reference))
        model.update(identity)
        model["name"] = identity["name"]
        model["s1RomRecord"] = {
            "index": index,
            "romOffset": STADIUM1_MODEL_ARCHIVE_ROM_START + int(item["offset"]),
            "size": int(item["size"]),
        }
        return model

def inspect_stadium2_pose_metadata(data: bytes, name: str = "Stadium 2 pose") -> Dict[str, Any]:
    """Inspect the repeated S2 pose trailer without claiming curve decoding.

    Every extracted S2 pose record currently ends with one structurally
    consistent trailer: a small first header word, two zeroed reserved fields,
    a channel count, a frame count, and offset/length words.  The channel/frame pair was
    cross-checked against the corresponding S1 model curves for all 151 shared
    species.  The remaining offsets are intentionally retained as untyped.
    """
    candidates: List[Tuple[int, int, int, int]] = []
    start = max(0, len(data) - 128)
    for offset in range(start, max(start, len(data) - 11), 2):
        try:
            record_count, reserved16 = struct.unpack_from(">HH", data, offset)
            reserved32 = u32(data, offset + 4)
            channel_count, frame_count = struct.unpack_from(">HH", data, offset + 8)
        except (FormatError, struct.error):
            continue
        if (1 <= record_count <= 16 and reserved16 == 0 and reserved32 == 0
                and 1 <= channel_count <= 4096 and 1 <= frame_count <= 4096):
            candidates.append((offset, record_count, channel_count, frame_count))
    if len(candidates) != 1:
        raise FormatError(f"{name} has {len(candidates)} plausible pose trailers; expected exactly one")
    offset, record_count, channel_count, frame_count = candidates[0]
    return {
        "supported": False,
        "recordSize": len(data),
        "trailerOffset": offset,
        "trailerSize": len(data) - offset,
        "headerWord0": record_count,
        "channelCount": channel_count,
        "frameCount": frame_count,
        "reason": "S2 pose trailer is identified, but its curve streams are not decoded",
    }


def parse_stadium2_pose(data: bytes, name: str = "Stadium 2 pose", max_samples: int = 600) -> Dict[str, Any]:
    """Decode a Stadium 2 pose record with the shared curve evaluator.

    A pose record is the Stadium 1 transform curve re-containerized: a u32
    trailer offset, then the scale/rotation/position streams and the channel
    descriptor table, then a trailer carrying the curve flags, the channel
    and frame counts, and record-relative stream offsets in the Stadium 1
    curve header field order.  The streams keep the Stadium 1 packed/Hermite
    encodings selected by the same flag bits; 717 of the shared-species
    records are byte-identical to their Stadium 1 curve payloads, so the
    Stadium 1 evaluator samples them unmodified.
    """
    metadata = inspect_stadium2_pose_metadata(data, name)
    trailer = metadata["trailerOffset"]
    if len(data) < 4 or u32(data, 0) != trailer:
        raise FormatError(f"{name} does not point at its pose trailer")
    if len(data) < trailer + 28:
        raise FormatError(f"{name} pose trailer is truncated")
    descriptor_offset = u32(data, trailer + 12)
    scale_offset = u32(data, trailer + 16)
    rotation_offset = u32(data, trailer + 20)
    position_offset = u32(data, trailer + 24)
    # Rebuild the Stadium 1 curve header in front of the record so parse_curve
    # resolves the raw record-relative offsets through its local-pointer path.
    header = struct.pack(
        ">hHHHHHIIII", metadata["headerWord0"], 0, 0, 0,
        metadata["channelCount"], metadata["frameCount"],
        descriptor_offset + 0x1C, scale_offset + 0x1C, rotation_offset + 0x1C, position_offset + 0x1C,
    )
    blob = header + data
    context = ParseContext(blob)
    curve = parse_curve(Reader(blob), 0, context, max_samples=max_samples)
    if not curve.get("supported"):
        raise FormatError(f"{name} pose streams did not decode: {curve.get('reason', 'unknown')}")
    return curve


def stadium2_animation_summaries(count: int, metadata: Optional[List[Dict[str, Any]]] = None,
                                 slot_indices: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """Summarize S2 source animation slots and any unreferenced pose records.

    Stadium 2 keeps a pose-record bank separate from the model's animation
    slot list.  A slot can reference the same pose record as another slot but
    still select a different event track (facial/expression texture sequence),
    so catalog entries must be keyed by source slot first.  Unreferenced pose
    records remain browseable after those slots.
    """
    metadata = metadata or []
    entries = []
    slot_indices = slot_indices if slot_indices is not None else []
    used_pose_records = set()

    def summary(animation_id: int, name: str, pose_index: Optional[int], source_slot: Optional[int]) -> Dict[str, Any]:
        record = metadata[pose_index] if pose_index is not None and 0 <= pose_index < len(metadata) else {}
        recognized = bool(record.get("frameCount"))
        item = {
            "id": animation_id,
            "name": name if recognized else f"{name} (unsupported)",
            "frameCount": int(record.get("frameCount", 0)),
            "supported": recognized,
            "metadata": record if pose_index is not None and 0 <= pose_index < len(metadata) else None,
            "poseRecord": pose_index,
        }
        if source_slot is not None:
            item["sourceSlot"] = source_slot
        if not recognized:
            item["reason"] = "Stadium 2 pose trailer was not recognized"
        return item

    for slot, pose_index in enumerate(slot_indices):
        valid_pose = pose_index if 0 <= pose_index < count else None
        if valid_pose is not None:
            used_pose_records.add(valid_pose)
        pose_label = f"pose_{pose_index:03d}" if valid_pose is not None else "pose_unavailable"
        entries.append(summary(slot, f"slot_{slot:03d} · {pose_label}", valid_pose, slot))

    # Keep S2-only records visible even if no model animation slot references
    # them. IDs are disjoint from source-slot IDs so saved selections remain
    # unambiguous within one loaded model.
    extra_id_base = len(slot_indices)
    for index in range(count):
        if index in used_pose_records:
            continue
        entries.append(summary(extra_id_base + index, f"pose_{index:03d}", index, None))
    return entries


class Stadium2DataProvider:
    """Provider for the Stadium 2 Pokémon model bank.

    Source evidence identifies the model bank at ROM 0x27ED000 and the
    per-model pose bank at 0x2D7D000.  The model records are PERS-SZP/Yay0
    resources whose decoded payloads match the existing FRAGMENT model path;
    the pose records are indexed separately and are decoded through the shared
    Stadium 1 curve evaluator after the selected model is loaded.
    """

    game_id = "stadium2"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._catalog_cache: Optional[List[Dict[str, Any]]] = None
        self._model_cache: Dict[str, Dict[str, Any]] = {}
        self._rom_blob: Optional[bytes] = None
        self._rom_sha256: Optional[str] = None
        self._catalog_cache_path: Optional[Path] = None
        self._model_archive: Optional[Dict[str, Any]] = None
        self._pose_archive: Optional[Dict[str, Any]] = None
        self._model_blob: Optional[bytes] = None
        self._pose_blob: Optional[bytes] = None
        self._extracted_manifest: Optional[Dict[str, Any]] = None
        self._extracted_models: Dict[int, Dict[str, Any]] = {}
        self._extracted_pose_groups: Optional[Dict[int, Dict[str, Any]]] = None
        self._pose_metadata: List[List[Dict[str, Any]]] = []
        self._pose_counts: List[int] = []
        self.diagnostics: List[Dict[str, Any]] = []
        if self._load_extracted_cache():
            return
        try:
            model_data, model_origin = self._read_bank("models")
            self._model_blob = model_data
            self._model_archive = parse_stadium2_indexed_archive(model_data, f"{model_origin} model bank")
            if self._model_archive["fileCount"] != STADIUM2_MODEL_COUNT_EXPECTED:
                self.diagnostics.append({
                    "severity": "warning", "code": "s2-model-count",
                    "message": f"model bank declares {self._model_archive['fileCount']} records; source evidence expected {STADIUM2_MODEL_COUNT_EXPECTED}",
                })
        except (FileNotFoundError, FormatError, OSError) as exc:
            self.diagnostics.append({"severity": "error", "code": "s2-model-bank", "message": str(exc)})
        try:
            pose_data, pose_origin = self._read_bank("poses")
            self._pose_blob = pose_data
            self._pose_archive = parse_stadium2_indexed_archive(pose_data, f"{pose_origin} pose bank")
            self._pose_metadata = [self._nested_pose_metadata(pose_data, item) for item in self._pose_archive["files"]]
            self._pose_counts = [len(metadata) for metadata in self._pose_metadata]
        except (FileNotFoundError, FormatError, OSError) as exc:
            self.diagnostics.append({"severity": "warning", "code": "s2-pose-bank", "message": str(exc)})

    def _cache_path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise FormatError("Stadium 2 extraction manifest contains an empty path")
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise FormatError(f"Stadium 2 extraction path escapes the cache root: {relative}")
        return candidate

    def _load_extracted_cache(self) -> bool:
        manifest_path = self.root / "manifest.json"
        if not self.root.is_dir() or not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("format") != STADIUM2_EXTRACT_FORMAT:
                raise FormatError(f"{manifest_path} is not a {STADIUM2_EXTRACT_FORMAT} manifest")
            model_records = manifest.get("models")
            if not isinstance(model_records, list) or not model_records:
                raise FormatError(f"{manifest_path} has no extracted model records")
            extracted_models: Dict[int, Dict[str, Any]] = {}
            for record in model_records:
                if not isinstance(record, dict):
                    raise FormatError(f"{manifest_path} contains a non-object model record")
                index = int(record["index"])
                relative = record["path"]
                if index < 0 or index >= MAX_ARCHIVE_FILES or index in extracted_models:
                    raise FormatError(f"{manifest_path} contains invalid or duplicate model index {index}")
                model_path = self._cache_path(relative)
                if not model_path.is_file():
                    raise FileNotFoundError(f"extracted Stadium 2 model is missing: {model_path}")
                extracted_models[index] = dict(record)
            pose_groups: Dict[int, Dict[str, Any]] = {}
            raw_pose_groups = manifest.get("poseGroups")
            if raw_pose_groups is not None:
                if not isinstance(raw_pose_groups, list):
                    raise FormatError(f"{manifest_path} poseGroups is not a list")
                for group in raw_pose_groups:
                    if not isinstance(group, dict):
                        raise FormatError(f"{manifest_path} contains a non-object pose group")
                    index = int(group["index"])
                    records = group.get("records", [])
                    if index in pose_groups or not isinstance(records, list):
                        raise FormatError(f"{manifest_path} contains an invalid pose group {index}")
                    for record in records:
                        if not isinstance(record, dict):
                            raise FormatError(f"{manifest_path} contains an invalid pose record")
                        self._cache_path(record["path"])
                    pose_groups[index] = dict(group)
            self._extracted_manifest = manifest
            self._extracted_models = extracted_models
            self._extracted_pose_groups = pose_groups if raw_pose_groups is not None else None
            self._pose_metadata = []
            if self._extracted_pose_groups is not None:
                for index in range(max(extracted_models) + 1):
                    group = self._extracted_pose_groups.get(index, {})
                    self._pose_metadata.append([
                        dict(record.get("metadata", {}))
                        for record in group.get("records", [])
                        if isinstance(record, dict)
                    ])
            self._pose_counts = [int(extracted_models[index].get("poseCount", 0))
                                 if index in extracted_models else 0
                                 for index in range(max(extracted_models) + 1)]
            if len(extracted_models) != STADIUM2_MODEL_COUNT_EXPECTED:
                self.diagnostics.append({
                    "severity": "warning", "code": "s2-model-count",
                    "message": f"extraction manifest contains {len(extracted_models)} records; source evidence expected {STADIUM2_MODEL_COUNT_EXPECTED}",
                })
            return True
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, FormatError) as exc:
            self.diagnostics.append({
                "severity": "error", "code": "s2-extracted-cache",
                "message": f"could not use {manifest_path}: {exc}",
            })
            return False

    def _read_bank(self, bank: str) -> Tuple[bytes, str]:
        if bank == "models":
            start, end = STADIUM2_MODEL_TABLE_ROM_OFFSET, STADIUM2_MODEL_TABLE_ROM_END
            candidates = ("pokemon_models.bin", "model_table.bin", "s2_models.bin", "0x27ed000.bin", "27ed000.bin")
        else:
            start, end = STADIUM2_POSE_TABLE_ROM_OFFSET, STADIUM2_POSE_TABLE_ROM_END
            candidates = ("pokemon_poses.bin", "pose_table.bin", "s2_poses.bin", "0x2d7d000.bin", "2d7d000.bin")

        def from_file(path: Path) -> Tuple[bytes, str]:
            name = path.name.casefold()
            if path.suffix.casefold() in (".z64", ".n64", ".v64", ".rom"):
                raw = self._read_rom(path)
                if len(raw) >= end:
                    return raw[start:end], f"{path.name}@0x{start:X}"
            else:
                raw = path.read_bytes()
            if name == "437610.bin":
                relative_start = start - 0x00437610
                relative_end = end - 0x00437610
                if relative_start >= 0 and len(raw) >= relative_end:
                    return raw[relative_start:relative_end], f"{path.name}@ROM+0x{start:X}"
            if len(raw) >= STADIUM2_ARCHIVE_HEADER_SIZE and u32(raw, 0) == STADIUM2_TABLE_MAGIC:
                return raw, path.name
            raise FormatError(f"{path} is not a Stadium 2 {bank} bank or supported ROM image")

        if self.root.is_file():
            return from_file(self.root)
        for candidate in candidates:
            path = self.root / candidate
            if path.is_file():
                return from_file(path)
        rest = self.root / "437610.bin"
        if rest.is_file():
            return from_file(rest)
        raise FileNotFoundError(
            f"could not find the Stadium 2 {bank} bank; pass the full S2 ROM, 437610.bin, "
            f"or an extracted bank named {candidates[0]}"
        )

    def _read_rom(self, path: Path) -> bytes:
        if self._rom_blob is None:
            self._rom_blob = normalize_n64_rom(path.read_bytes())
            self._rom_sha256 = hashlib.sha256(self._rom_blob).hexdigest()
            self._catalog_cache_path = derived_cache_dir() / f"stadium2-catalog-{self._rom_sha256}.json"
        return self._rom_blob

    @staticmethod
    def _decoded_model_blob(blob: bytes) -> bytes:
        return decode_stadium2_model_blob(blob)

    @staticmethod
    def _nested_pose_metadata(data: bytes, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        start = item["offset"]
        blob = data[start:start + item["size"]]
        try:
            nested = parse_stadium2_indexed_archive(blob, f"pose group {item['index']}")
            metadata = []
            for record in nested["files"]:
                pose = blob[record["offset"]:record["offset"] + record["size"]]
                try:
                    metadata.append(inspect_stadium2_pose_metadata(pose, f"pose group {item['index']} record {record['index']}"))
                except FormatError:
                    metadata.append({"supported": False, "recordSize": len(pose), "frameCount": 0,
                                     "reason": "S2 pose trailer was not recognized"})
            return metadata
        except FormatError:
            return []

    def _entry_blob(self, index: int) -> Tuple[bytes, Dict[str, Any]]:
        if self._extracted_manifest is not None:
            item = self._extracted_models.get(index)
            if item is None:
                raise IndexError(f"Stadium 2 model index {index} is out of range")
            path = self._cache_path(item["path"])
            blob = path.read_bytes()
            return blob, {
                "index": index,
                "offset": int(item.get("sourceOffset", 0)),
                "size": int(item.get("sourceSize", len(blob))),
                "decodedSize": len(blob),
                "extractedPath": str(path),
            }
        if self._model_archive is None or self._model_blob is None:
            raise FormatError("Stadium 2 model bank is unavailable")
        if index < 0 or index >= self._model_archive["fileCount"]:
            raise IndexError(f"Stadium 2 model index {index} is out of range")
        item = self._model_archive["files"][index]
        return self._model_blob[item["offset"]:item["offset"] + item["size"]], item

    def _reference_index(self, reference: str) -> int:
        if not reference.startswith("s2-model:"):
            raise ValueError("Stadium 2 model references must use s2-model:NNN")
        value = reference.split(":", 1)[1]
        if not value.isdigit():
            raise ValueError(f"invalid Stadium 2 model reference {reference}")
        return int(value)

    def _model_diagnostics(self, index: int) -> List[Dict[str, Any]]:
        if self._extracted_manifest is not None:
            if self._extracted_pose_groups is None:
                return [{"severity": "warning", "code": "s2-animation-bank-unavailable",
                         "message": "the Stadium 2 pose bank was not included in the extraction cache; animation records cannot be listed"}]
        elif self._pose_archive is None:
            return [{"severity": "warning", "code": "s2-animation-bank-unavailable",
                     "message": "the Stadium 2 pose bank is unavailable; animation records cannot be listed"}]
        count = self._pose_counts[index] if index < len(self._pose_counts) else 0
        if not count:
            return [{"severity": "warning", "code": "s2-animation-records-empty",
                     "message": "the Stadium 2 model has no indexed pose records"}]
        return []

    @staticmethod
    def _animation_slot_indices(model: Dict[str, Any], blob: bytes) -> List[int]:
        """Read the S2 model animation list as pose-record indices.

        Unlike Stadium 1, the S2 list entries are not relocated curve
        pointers.  They are indices into the adjacent pose group, and the
        event-track table is parallel to this slot list.
        """
        root = model.get("rootOffset")
        slot_count = int(model.get("animationSlotCount", 0) or 0)
        if not isinstance(root, int) or slot_count <= 0 or len(blob) < root + 0x10:
            return []
        list_offset = pointer_to_offset(u32(blob, root + 0x0C), len(blob))
        if list_offset is None:
            return []
        indices: List[int] = []
        for slot in range(slot_count):
            pointer_at = list_offset + slot * 4
            if not (0 <= pointer_at <= len(blob) - 4):
                break
            indices.append(u32(blob, pointer_at))
        return indices

    def _pose_record_blob(self, index: int, record_index: int) -> bytes:
        if self._extracted_pose_groups is not None:
            group = self._extracted_pose_groups.get(index) or {}
            records = group.get("records", [])
            if record_index < 0 or record_index >= len(records):
                raise IndexError(f"Stadium 2 pose record {record_index} is out of range for model {index}")
            return self._cache_path(records[record_index]["path"]).read_bytes()
        if self._pose_archive is None or self._pose_blob is None:
            raise FormatError("Stadium 2 pose bank is unavailable")
        if index < 0 or index >= self._pose_archive["fileCount"]:
            raise IndexError(f"Stadium 2 pose group {index} is out of range")
        group_item = self._pose_archive["files"][index]
        group_blob = self._pose_blob[group_item["offset"]:group_item["offset"] + group_item["size"]]
        nested = parse_stadium2_indexed_archive(group_blob, f"pose group {index}")
        if record_index < 0 or record_index >= nested["fileCount"]:
            raise IndexError(f"Stadium 2 pose record {record_index} is out of range for model {index}")
        record = nested["files"][record_index]
        return group_blob[record["offset"]:record["offset"] + record["size"]]

    def _pose_animations(self, index: int, model: Dict[str, Any], blob: bytes) -> List[Dict[str, Any]]:
        """Decode S2 pose records and preserve source slot/event-track pairs.

        The fragment's animation list stores pose-record indices instead of
        Stadium 1 curve pointers.  Duplicate slots can share one transform
        record while selecting different event tracks, so collapsing them
        loses facial/expression texture animation.  Source slots are exposed
        first; pose records not referenced by a slot remain browseable after
        them.
        """
        count = self._pose_counts[index] if index < len(self._pose_counts) else 0
        metadata = self._pose_metadata[index] if index < len(self._pose_metadata) else []
        pose_records: List[Dict[str, Any]] = []
        for record_index in range(count):
            record = metadata[record_index] if record_index < len(metadata) else {}
            entry: Dict[str, Any] = {
                "id": record_index,
                "name": f"pose_{record_index:03d}",
                "frameCount": int(record.get("frameCount", 0)),
                "supported": False,
                "metadata": record or None,
            }
            try:
                curve = parse_stadium2_pose(
                    self._pose_record_blob(index, record_index),
                    f"Stadium 2 model {index:03d} pose {record_index}",
                )
                entry["supported"] = True
                entry["frameCount"] = curve.get("frameCount", 0)
                entry["curve"] = curve
            except (FormatError, IndexError, OSError, struct.error) as exc:
                entry["name"] += " (unsupported)"
                entry["reason"] = str(exc)
            pose_records.append(entry)

        # Fragment animation slots hold pose-record indices (Stadium 1 holds
        # curve pointers there). Keep the slot and its parallel event track as
        # one normalized viewer animation even when several slots share a
        # transform record.
        event_tracks = model.get("eventTracks") or []
        slot_indices = self._animation_slot_indices(model, blob)
        slot_count = int(model.get("animationSlotCount", 0) or 0)
        animations: List[Dict[str, Any]] = []
        used_pose_records = set()
        for slot in range(slot_count):
            pose_index = slot_indices[slot] if slot < len(slot_indices) else None
            valid_pose = pose_index if isinstance(pose_index, int) and 0 <= pose_index < len(pose_records) else None
            if valid_pose is None:
                entry = {
                    "id": slot,
                    "name": f"slot_{slot:03d} · pose_unavailable (unsupported)",
                    "frameCount": 0,
                    "supported": False,
                    "reason": "Stadium 2 animation slot does not reference an indexed pose record",
                    "poseRecord": pose_index,
                    "sourceSlot": slot,
                }
            else:
                entry = dict(pose_records[valid_pose])
                entry["id"] = slot
                entry["name"] = f"slot_{slot:03d} · pose_{valid_pose:03d}"
                entry["poseRecord"] = valid_pose
                entry["sourceSlot"] = slot
                used_pose_records.add(valid_pose)
            if slot < len(event_tracks):
                entry["eventTrack"] = event_tracks[slot]
                entry["eventTrackSlot"] = slot
            animations.append(entry)

        # Some S2 pose-bank records are not referenced by the model's source
        # slot list. Keep them available for comparison and inspection, but do
        # not invent an event track for them.
        extra_id_base = slot_count
        for pose_index, pose_record in enumerate(pose_records):
            if pose_index in used_pose_records:
                continue
            entry = dict(pose_record)
            entry["id"] = extra_id_base + pose_index
            entry["name"] = f"pose_{pose_index:03d} (unreferenced)"
            entry["poseRecord"] = pose_index
            entry["unreferencedPose"] = True
            animations.append(entry)
        return animations

    def _load_catalog_cache(self) -> Optional[List[Dict[str, Any]]]:
        path = self._catalog_cache_path
        if path is None or not path.is_file() or not self._rom_sha256:
            return None
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(cached, dict)
                    or cached.get("format") != STADIUM2_CATALOG_CACHE_FORMAT
                    or cached.get("sourceSha256") != self._rom_sha256
                    or not isinstance(cached.get("models"), list)):
                return None
            return [dict(item) for item in cached["models"] if isinstance(item, dict)]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save_catalog_cache(self, entries: List[Dict[str, Any]]) -> None:
        path = self._catalog_cache_path
        if path is None or not self._rom_sha256:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "format": STADIUM2_CATALOG_CACHE_FORMAT,
                "sourceSha256": self._rom_sha256,
                "models": entries,
            }
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            self.diagnostics.append({
                "severity": "warning", "code": "s2-catalog-cache-write",
                "message": f"could not write derived catalog cache: {exc}",
            })

    def catalog(self) -> List[Dict[str, Any]]:
        if self._catalog_cache is not None:
            return self._catalog_cache
        cached = self._load_catalog_cache()
        if cached is not None:
            self._catalog_cache = cached
            return cached
        entries: List[Dict[str, Any]] = []
        if self._extracted_manifest is not None:
            for index in sorted(self._extracted_models):
                item = self._extracted_models[index]
                diagnostics = list(item.get("diagnostics", []))
                diagnostics.extend(self._model_diagnostics(index))
                pose_count = self._pose_counts[index] if index < len(self._pose_counts) else int(item.get("poseCount", 0))
                metadata = self._pose_metadata[index] if index < len(self._pose_metadata) else []
                slot_indices: List[int] = []
                try:
                    blob, _ = self._entry_blob(index)
                    decoded = self._decoded_model_blob(blob)
                    parsed = parse_resource(decoded, f"Stadium 2 model {index:03d}", catalog_only=True)
                    slot_indices = self._animation_slot_indices(parsed, decoded)
                except (FormatError, IndexError, OSError, struct.error):
                    pass
                reference = f"s2-model:{index:03d}"
                model_id = item.get("modelId", index)
                identity = model_identity("stadium2", model_id, reference, f"S2 model {index:03d}")
                entries.append({
                    "kind": "model", **identity, "path": reference,
                    "size": int(item.get("sourceSize", item.get("size", 0))),
                    "decodedSize": int(item.get("size", 0)),
                    "modelId": model_id, "s2ModelIndex": index,
                    "animations": stadium2_animation_summaries(pose_count, metadata, slot_indices),
                    "diagnostics": diagnostics,
                })
            self._catalog_cache = entries
            self._save_catalog_cache(entries)
            return entries
        if self._model_archive is None:
            self._catalog_cache = entries
            self._save_catalog_cache(entries)
            return entries
        for item in self._model_archive["files"]:
            index = int(item["index"])
            reference = f"s2-model:{index:03d}"
            diagnostics = self._model_diagnostics(index)
            slot_indices: List[int] = []
            try:
                blob, _ = self._entry_blob(index)
                decoded = self._decoded_model_blob(blob)
                parsed = parse_resource(decoded, f"Stadium 2 model {index:03d}", catalog_only=True)
                slot_indices = self._animation_slot_indices(parsed, decoded)
                diagnostics = list(parsed.get("diagnostics", [])) + diagnostics
                if parsed.get("kind") != "model":
                    diagnostics.append({"severity": "error", "code": "s2-model-not-model",
                                        "message": "indexed record did not decode as a model"})
                pose_count = self._pose_counts[index] if index < len(self._pose_counts) else 0
            except Exception as exc:
                parsed = {}
                pose_count = self._pose_counts[index] if index < len(self._pose_counts) else 0
                diagnostics.append({"severity": "error", "code": "s2-model-parse", "message": str(exc)})
            model_id = parsed.get("modelId", index)
            identity = model_identity("stadium2", model_id, reference, f"S2 model {index:03d}")
            entries.append({
                "kind": "model", **identity, "path": reference,
                "size": item["size"], "modelId": model_id,
                "s2ModelIndex": index, "animations": stadium2_animation_summaries(pose_count, self._pose_metadata[index] if index < len(self._pose_metadata) else [], slot_indices),
                "diagnostics": diagnostics,
            })
        self._catalog_cache = entries
        self._save_catalog_cache(entries)
        return entries

    def load_model(self, reference: str) -> Dict[str, Any]:
        if reference in self._model_cache:
            return self._model_cache[reference]
        index = self._reference_index(reference)
        blob, item = self._entry_blob(index)
        decoded = self._decoded_model_blob(blob)
        model = parse_resource(decoded, f"Stadium 2 model {index:03d}")
        if model.get("kind") != "model":
            raise FormatError(f"Stadium 2 record {index} did not decode as a model")
        infer_stadium2_auxiliary_textures(model)
        apply_stadium2_runtime_tint(model, index)
        model_id = model.get("modelId", index)
        identity = model_identity("stadium2", model_id, reference, f"S2 model {index:03d}")
        model.update(identity)
        model["name"] = identity["name"]
        model["s2ModelIndex"] = index
        model["s2ModelRecord"] = {"offset": item["offset"], "size": item["size"]}
        if "decodedSize" in item:
            model["s2ModelRecord"]["decodedSize"] = item["decodedSize"]
        if "extractedPath" in item:
            model["s2ModelRecord"]["extractedPath"] = item["extractedPath"]
        model["animations"] = self._pose_animations(index, model, decoded)
        model["animationSlotCount"] = int(model.get("animationSlotCount", 0) or 0)
        model.setdefault("diagnostics", []).extend(self._model_diagnostics(index))
        self._model_cache[reference] = model
        return model


def summarize_animations(model: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for item in model.get("animations", []):
        result.append({"id": item.get("id", 0), "name": item.get("name", "animation"),
                       "frameCount": item.get("frameCount", 0), "supported": item.get("supported", False)})
    return result


def stadium1_model_id_supported(model: Dict[str, Any]) -> bool:
    model_id = model.get("modelId")
    return isinstance(model_id, int) and STADIUM1_MODEL_ID_MIN <= model_id <= STADIUM1_MODEL_ID_MAX


def stadium1_model_name(model_id: int, reference: str) -> str:
    return f"#{model_id:03d} · {reference}"


def health(provider: Any) -> Dict[str, Any]:
    # Absolute paths can contain usernames and repository layout. They are not
    # needed by the browser and must not be exposed in the HTTP API.
    return {"ok": True, "provider": provider.game_id,
            "modelCount": len(provider.catalog()), "diagnostics": getattr(provider, "diagnostics", [])}


def _configured_asset(config: Dict[str, Any], provider: str) -> Optional[str]:
    """Read one provider asset reference from the local-only config file."""
    providers = config.get("providers", config)
    if not isinstance(providers, dict):
        raise ValueError("viewer config 'providers' must be an object")
    value = providers.get(provider)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        asset = value.get("assets", value.get("cache", value.get("rom")))
        if isinstance(asset, str):
            return asset
    return None


def load_viewer_config(config_path: Path) -> Dict[str, str]:
    """Load and resolve local paths; the viewer never reads a ROM from config."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"viewer config not found: {config_path}; copy viewer.local.example.json to "
            "viewer.local.json or pass explicit --assets paths"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid viewer config {config_path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("format") != VIEWER_CONFIG_FORMAT:
        raise ValueError(f"{config_path} is not a {VIEWER_CONFIG_FORMAT} config")

    resolved: Dict[str, str] = {}
    for provider in ("stadium1", "stadium2"):
        value = _configured_asset(raw, provider)
        if value:
            expanded = os.path.expandvars(os.path.expanduser(value))
            path = Path(expanded)
            if not path.is_absolute():
                path = config_path.parent / path
            resolved[provider] = str(path.resolve())
    return resolved


class ViewerHandler(http.server.BaseHTTPRequestHandler):
    server_version = "Stadium1Viewer/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[stadium1-viewer] " + (fmt % args) + "\n")

    @property
    def provider(self) -> Any:
        return self.server.provider  # type: ignore[attr-defined]

    def provider_for_request(self, query: Dict[str, List[str]]) -> Any:
        providers = getattr(self.server, "providers", {self.server.provider.game_id: self.server.provider})
        requested = query.get("provider", [getattr(self.server, "default_provider", self.provider.game_id)])[0]
        if requested not in providers:
            raise ValueError(f"unknown provider {requested}")
        return providers[requested]

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/api/health":
                providers = getattr(self.server, "providers", {self.provider.game_id: self.provider})
                if len(providers) > 1:
                    self._json({
                        "ok": True, "mode": "dual",
                        "defaultProvider": getattr(self.server, "default_provider", self.provider.game_id),
                        "providers": [health(item) for item in providers.values()],
                    })
                else:
                    self._json(health(self.provider_for_request(query)))
                return
            if parsed.path == "/api/catalog":
                search_tokens = [token for token in query.get("q", [""])[0].casefold().split() if token]
                provider = self.provider_for_request(query)
                items = provider.catalog()
                if search_tokens:
                    items = [item for item in items
                             if all(token in json.dumps(item, ensure_ascii=False).casefold() for token in search_tokens)]
                self._json({"provider": provider.game_id, "models": items})
                return
            if parsed.path == "/api/model":
                provider = self.provider_for_request(query)
                reference = query.get("path", [""])[0]
                if not reference:
                    self._json({"error": "missing path"}, 400)
                    return
                self._json(provider.load_model(reference))
                return
            if parsed.path == "/" or parsed.path == "/index.html":
                self._file(Path(__file__).with_name("stadium1_viewer") / "index.html")
                return
            if parsed.path.startswith("/viewer/"):
                relative = parsed.path.removeprefix("/viewer/")
                static_path = (Path(__file__).with_name("stadium1_viewer") / relative).resolve()
                static_root = (Path(__file__).with_name("stadium1_viewer")).resolve()
                if static_path == static_root or static_root not in static_path.parents or not static_path.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                self._file(static_path)
                return
            self._json({"error": "not found"}, 404)
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 404)
        except (FormatError, ValueError, IndexError) as exc:
            self._json({"error": str(exc)}, 422)
        except Exception as exc:  # the viewer must report, not crash, on bad resources
            self._json({"error": str(exc)}, 500)


def run_server(args: argparse.Namespace) -> None:
    config: Dict[str, str] = {}
    if args.config:
        config = load_viewer_config(Path(args.config).resolve())

    def asset_path(provider: str, explicit: Optional[str]) -> Path:
        value = explicit or config.get(provider)
        if not value:
            raise ValueError(
                f"no {provider} assets configured; pass --assets or use --config viewer.local.json"
            )
        return Path(value).resolve()

    def stadium1_provider(path: Path) -> Stadium1DataProvider:
        if path.is_file() and path.suffix.casefold() in (".z64", ".n64", ".v64", ".rom"):
            return Stadium1RomDataProvider(path)
        return Stadium1DataProvider(path)

    if args.dual:
        if args.stadium1_rom and args.stadium1_assets:
            raise ValueError("use only one of --stadium1-rom and --stadium1-assets")
        if args.stadium2_rom and args.stadium2_assets:
            raise ValueError("use only one of --stadium2-rom and --stadium2-assets")
        s1_explicit = args.stadium1_rom or args.stadium1_assets
        s2_explicit = args.stadium2_rom or args.stadium2_assets
        providers = {
            "stadium1": stadium1_provider(asset_path("stadium1", s1_explicit)),
            "stadium2": Stadium2DataProvider(asset_path("stadium2", s2_explicit)),
        }
        provider = providers[args.provider]
    else:
        if args.provider == "stadium1" and args.stadium1_rom and args.assets:
            raise ValueError("use only one of --stadium1-rom and --assets")
        if args.provider == "stadium2" and args.stadium2_rom and args.assets:
            raise ValueError("use only one of --stadium2-rom and --assets")
        explicit = args.stadium1_rom if args.provider == "stadium1" else args.stadium2_rom or args.assets
        root = asset_path(args.provider, explicit)
        provider = stadium1_provider(root) if args.provider == "stadium1" else Stadium2DataProvider(root)
        providers = {provider.game_id: provider}
    server = http.server.ThreadingHTTPServer((args.host, args.port), ViewerHandler)
    server.provider = provider  # type: ignore[attr-defined]
    server.providers = providers  # type: ignore[attr-defined]
    server.default_provider = provider.game_id  # type: ignore[attr-defined]
    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{server.server_port}/"
    print(f"{'dual' if args.dual else provider.game_id} viewer: {url}")
    for item in providers.values():
        print(f"{item.game_id} provider ready")
    if args.open:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping viewer.")
    finally:
        server.server_close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the dependency-free Pokémon Stadium model/animation viewer")
    parser.add_argument("--assets", help="asset directory or a single model/archive file")
    parser.add_argument("--stadium1-rom", help="user-owned Stadium 1 .z64/.n64/.v64/.rom image")
    parser.add_argument("--config", help="local JSON file containing external provider asset paths")
    parser.add_argument("--provider", choices=("stadium1", "stadium2"), default="stadium1",
                        help="game-specific provider; Stadium 2 decodes model poses through the shared curve evaluator")
    parser.add_argument("--dual", action="store_true", help="serve Stadium 1 and Stadium 2 in separate viewer tabs")
    parser.add_argument("--stadium1-assets", help="Stadium 1 assets for --dual")
    parser.add_argument("--stadium2-assets", help="Stadium 2 extraction cache for --dual")
    parser.add_argument("--stadium2-rom", help="user-owned Stadium 2 .z64/.n64/.rom image")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the viewer in the default browser")
    args = parser.parse_args(argv)
    try:
        run_server(args)
    except (FileNotFoundError, FormatError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
