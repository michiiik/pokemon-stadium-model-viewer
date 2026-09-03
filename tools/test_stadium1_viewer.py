#!/usr/bin/env python3
"""Source-backed parser checks and small in-memory Stadium 1 fixtures."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import stadium1_viewer as viewer

_render_spec = importlib.util.spec_from_file_location(
    "stadium1_viewer_render_capture", Path(__file__).parent / "stadium1_viewer" / "render_capture.py"
)
assert _render_spec and _render_spec.loader
render_capture = importlib.util.module_from_spec(_render_spec)
_render_spec.loader.exec_module(render_capture)


def put_u8(data: bytearray, offset: int, value: int) -> None:
    data[offset] = value & 0xFF


def put_s16(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">h", data, offset, value)


def put_u16(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">H", data, offset, value)


def put_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">I", data, offset, value & 0xFFFFFFFF)


def put_f32(data: bytearray, offset: int, value: float) -> None:
    struct.pack_into(">f", data, offset, value)


def pointer(offset: int) -> int:
    return 0x8FF00000 | offset


def build_fragment(animated: bool, textured: bool, translucent: bool) -> bytes:
    data = bytearray(0x800)
    put_u32(data, 0, (2 << 26) | ((0x0FF00020 >> 2) & 0x03FFFFFF))
    data[8:16] = b"FRAGMENT"
    put_u32(data, 0x10, 0x20)
    put_u32(data, 0x14, 0x700)
    put_u32(data, 0x18, len(data))
    put_u32(data, 0x1C, len(data))
    put_u32(data, 0x700, 0)

    root = 0x80
    put_u16(data, root, 7)
    put_u16(data, root + 2, 1)
    put_u8(data, root + 4, 0)
    put_u8(data, root + 5, 1 if animated else 0)
    put_u32(data, root + 8, pointer(0x100))
    put_u32(data, root + 0x0C, pointer(0x200) if animated else 0)
    put_u32(data, root + 0x10, pointer(0x600) if animated else 0)

    put_u8(data, 0x100, 0)
    put_u32(data, 0x10C, pointer(0x120))
    put_u8(data, 0x120, 0x12)
    put_u32(data, 0x12C, pointer(0x160))
    put_f32(data, 0x144, 1.0)
    put_u8(data, 0x150, 0)
    put_s16(data, 0x152, 0)
    put_u8(data, 0x160, 0x13)
    put_u32(data, 0x178, pointer(0x240))
    put_s16(data, 0x17C, 0)

    cursor = 0x240
    if textured:
        put_u32(data, cursor, 0xFD100000)
        put_u32(data, cursor + 4, pointer(0x350))
        put_u32(data, cursor + 8, 0xF2000000)
        put_u32(data, cursor + 12, 0x00004004)
        cursor += 16
    if translucent:
        put_u32(data, cursor, 0xE2000000)
        put_u32(data, cursor + 4, 0x0000C000)
        cursor += 8
    put_u32(data, cursor, 0x04003000)
    put_u32(data, cursor + 4, pointer(0x300))
    put_u32(data, cursor + 8, 0xBF000204)
    put_u32(data, cursor + 12, 0)
    put_u32(data, cursor + 16, 0xB8000000)
    put_u32(data, cursor + 20, 0)

    for index, position in enumerate(((0, 0, 0), (10, 0, 0), (0, 10, 0))):
        at = 0x300 + index * 16
        for axis, value in enumerate(position):
            put_s16(data, at + axis * 2, value)
        put_s16(data, at + 8, index * 32)
        put_s16(data, at + 10, index * 32)
        data[at + 12:at + 16] = bytes((255, 255, 255, 255))

    if textured:
        for index, value in enumerate((0xF801, 0x07C1, 0x003F, 0xFFFF)):
            put_u16(data, 0x350 + index * 2, value)

    if animated:
        put_u32(data, 0x200, pointer(0x400))
        put_u16(data, 0x404, 0)
        put_u16(data, 0x408, 3)
        put_u16(data, 0x40A, 2)
        put_u32(data, 0x40C, pointer(0x500))
        # Source func_80017090 consumes the curve payload pointers as
        # scale, rotation, position (not position, rotation, scale).
        put_u32(data, 0x410, pointer(0x5C0))
        put_u32(data, 0x414, pointer(0x5A0))
        put_u32(data, 0x418, pointer(0x580))

        for channel in range(3):
            at = 0x500 + channel * 10
            put_u8(data, at, 1)
            put_u8(data, at + 1, 1)
            put_u8(data, at + 2, 2 if channel == 0 else 1)
            put_u16(data, at + 4, 0)
            put_u16(data, at + 6, 0)
            put_u16(data, at + 8, 0)
        for channel in range(3):
            put_s16(data, 0x504 + channel * 10, 1000)
        put_s16(data, 0x580, 0)
        put_u16(data, 0x582, 0x0A00)
        for offset in (0x5A0, 0x5A2, 0x5C0, 0x5C2):
            put_s16(data, offset, 0)
        for channel in (1, 2):
            put_s16(data, 0x508 + channel * 10, 0)
            put_s16(data, 0x50C + channel * 10, 0)

        # One source event track: slot 0 selects descriptor 0 for frame 0
        # and descriptor 1 for frame 1.  The parser/frontend use this path
        # for Stadium 1 expression texture remapping.
        put_u32(data, 0x600, pointer(0x620))
        put_s16(data, 0x620, 2)
        put_s16(data, 0x624, 0)
        put_s16(data, 0x626, 0)
        put_u16(data, 0x628, 1)
        put_u16(data, 0x62A, 2)
        put_u32(data, 0x62C, pointer(0x650))
        put_u32(data, 0x630, pointer(0x660))
        put_u16(data, 0x650, 2)
        put_u16(data, 0x652, 0)
        put_u8(data, 0x660, 0)
        put_u8(data, 0x661, 1)

    return bytes(data)


def build_s2_indexed_archive(records: list[bytes]) -> bytes:
    table_end = 0x10 + len(records) * 0x10
    payload = bytearray(table_end)
    struct.pack_into(">4I", payload, 0, 0xEF, 0, sum(len(item) for item in records), len(records))
    cursor = table_end
    for index, record in enumerate(records):
        struct.pack_into(">4I", payload, 0x10 + index * 0x10, cursor, len(record), 0, 0)
        payload.extend(record)
        cursor += len(record)
    return bytes(payload)


def build_s2_pose_record() -> bytes:
    """Minimal S2 pose record: one bone, constant channels, no streams."""
    descriptor = bytearray()
    for position in (100, 200, 300):
        descriptor.extend(struct.pack(">BBBBHHH", 1, 1, 1, 0, 1000, 0, position))
    trailer_offset = 4 + len(descriptor)
    record = bytearray(trailer_offset)
    struct.pack_into(">I", record, 0, trailer_offset)
    record[4:4 + len(descriptor)] = descriptor
    record.extend(struct.pack(">HHIHHIIII", 1, 0, 0, 3, 10, 4, 4, 8, 8))
    return bytes(record)


class Stadium1ViewerChecks(unittest.TestCase):
    def test_pokemon_names_and_provider_search_identity(self) -> None:
        self.assertEqual(viewer.pokemon_name(1), "Bulbasaur")
        self.assertEqual(viewer.pokemon_name(151), "Mew")
        self.assertEqual(viewer.pokemon_name(197), "Umbreon")
        self.assertEqual(viewer.pokemon_name(249), "Lugia")
        self.assertIsNone(viewer.pokemon_name(0))
        identity = viewer.model_identity("stadium2", 249, "s2-model:249", "fallback")
        self.assertEqual(identity["name"], "S2 #249 · Lugia · s2-model:249")
        self.assertIn("s2", identity["searchAliases"])
        self.assertIn("lugia", [item.casefold() for item in identity["searchAliases"]])

    def test_yay0_and_persszp(self) -> None:
        stream = b"Yay0" + struct.pack(">III", 4, 0x18, 0x14) + struct.pack(">I", 0xFFFFFFFF) + b"TEST"
        self.assertEqual(viewer.decode_yay0(stream), b"TEST")
        pers = b"PERS-SZP" + struct.pack(">4I", 0x18, 4, 4, 0) + stream
        unpacked, info = viewer.unpack_persszp(pers)
        self.assertEqual(unpacked, b"TEST")
        self.assertEqual(info["fixups"], 0)

    def test_binarchive(self) -> None:
        blob = bytearray(0x24)
        put_u32(blob, 8, len(blob))
        put_u32(blob, 0x0C, 1)
        put_u32(blob, 0x10, 0x20)
        put_u32(blob, 0x14, 4)
        blob[0x20:0x24] = b"TEST"
        archive = viewer.parse_archive(bytes(blob))
        self.assertIsNotNone(archive)
        self.assertEqual(archive["fileCount"], 1)

    def test_stadium2_indexed_archive(self) -> None:
        archive = viewer.parse_stadium2_indexed_archive(build_s2_indexed_archive([b"MODEL", b"POSE"]))
        self.assertEqual(archive["format"], "Stadium2IndexedArchive")
        self.assertEqual(archive["fileCount"], 2)
        self.assertEqual(archive["files"][0]["offset"], archive["tableSize"])
        self.assertEqual(archive["files"][1]["size"], 4)

    def test_stadium2_pose_trailer_metadata(self) -> None:
        pose = bytearray(32)
        struct.pack_into(">HHIHH", pose, 0, 1, 0, 0, 54, 48)
        metadata = viewer.inspect_stadium2_pose_metadata(bytes(pose), "fixture pose")
        self.assertEqual(metadata["channelCount"], 54)
        self.assertEqual(metadata["frameCount"], 48)
        self.assertFalse(metadata["supported"])

    def test_stadium2_pose_record_decodes_with_shared_curve_evaluator(self) -> None:
        curve = viewer.parse_stadium2_pose(build_s2_pose_record(), "fixture pose")
        self.assertTrue(curve["supported"])
        self.assertEqual(curve["frameCount"], 10)
        self.assertEqual(curve["channelCount"], 3)
        for frame_pose in curve["poses"]:
            self.assertEqual(frame_pose[0]["position"], [100.0, 200.0, 300.0])
            self.assertEqual(frame_pose[0]["rotation"], [0.0, 0.0, 0.0])
            self.assertEqual(frame_pose[0]["scale"], [1.0, 1.0, 1.0])
        with self.assertRaises(viewer.FormatError):
            viewer.parse_stadium2_pose(b"raw-pose", "broken pose")

    def test_stadium2_duplicate_pose_slots_keep_distinct_event_tracks(self) -> None:
        pose = build_s2_pose_record()
        provider = object.__new__(viewer.Stadium2DataProvider)
        provider._pose_counts = [2]
        provider._pose_metadata = [[
            viewer.inspect_stadium2_pose_metadata(pose, "pose0"),
            viewer.inspect_stadium2_pose_metadata(pose, "pose1"),
        ]]
        provider._pose_record_blob = lambda _index, _record_index: pose

        # S2 animation slots 0 and 1 intentionally share pose record 0.
        blob = bytearray(0x30)
        put_u32(blob, 0x0C, pointer(0x20))
        put_u32(blob, 0x20, 0)
        put_u32(blob, 0x24, 0)
        track0 = {"supported": True, "frameCount": 2, "slotCount": 1, "segments": [[2, 0]], "mapping": [1, 2]}
        track1 = {"supported": True, "frameCount": 3, "slotCount": 1, "segments": [[3, 0]], "mapping": [3, 4, 5]}
        model = {
            "rootOffset": 0,
            "animationSlotCount": 2,
            "eventTracks": [track0, track1],
        }

        animations = provider._pose_animations(0, model, bytes(blob))
        self.assertEqual([(item["id"], item["poseRecord"], item.get("sourceSlot")) for item in animations], [
            (0, 0, 0), (1, 0, 1), (3, 1, None),
        ])
        self.assertEqual(animations[0]["eventTrack"]["mapping"], [1, 2])
        self.assertEqual(animations[1]["eventTrack"]["mapping"], [3, 4, 5])
        self.assertEqual(animations[0]["curve"]["poses"], animations[1]["curve"]["poses"])
        self.assertTrue(animations[2]["unreferencedPose"])

    def test_animated_textured_translucent_fragment(self) -> None:
        model = viewer.parse_fragment(build_fragment(animated=True, textured=True, translucent=True), "fixture")
        self.assertEqual(model["modelId"], 7)
        self.assertEqual(len(model["meshes"]), 1)
        self.assertEqual(len(model["meshes"][0]["indices"]), 3)
        self.assertTrue(model["meshes"][0]["material"]["translucent"])
        self.assertEqual(model["meshes"][0]["material"]["texture"], 0)
        self.assertEqual(len(model["textures"]), 1)
        animation = model["animations"][0]
        self.assertTrue(animation["supported"])
        self.assertEqual(animation["frameCount"], 2)
        self.assertEqual(animation["curve"]["poses"][1][0]["position"][0], 10.0)
        self.assertEqual(animation["eventTrack"]["mapping"], [0, 1])

    def test_static_fragment_and_json_model(self) -> None:
        model = viewer.parse_fragment(build_fragment(animated=False, textured=False, translucent=False), "static")
        self.assertEqual(model["animationSlotCount"], 0)
        self.assertEqual(len(model["meshes"]), 1)
        json_model = viewer.parse_resource(json.dumps({"kind": "model", "meshes": [], "animations": []}).encode(), "json")
        self.assertEqual(json_model["kind"], "model")

    def test_incomplete_resources_report_diagnostics(self) -> None:
        parsed = viewer.parse_resource(b"incomplete", "broken")
        self.assertTrue(parsed["diagnostics"])
        with tempfile.TemporaryDirectory() as temporary:
            provider = viewer.Stadium2DataProvider(Path(temporary) / "missing")
            self.assertEqual(provider.catalog(), [])
            self.assertTrue(provider.diagnostics)

    def test_stadium2_models_load_and_undecodable_poses_degrade_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pokemon_models.bin").write_bytes(build_s2_indexed_archive([
                build_fragment(animated=False, textured=True, translucent=False)
            ]))
            (root / "pokemon_poses.bin").write_bytes(build_s2_indexed_archive([
                build_s2_indexed_archive([b"pose0", b"pose1"])
            ]))
            provider = viewer.Stadium2DataProvider(root)
            catalog = provider.catalog()
            self.assertEqual(len(catalog), 1)
            self.assertIn("S2 #007 · Squirtle", catalog[0]["name"])
            self.assertEqual(len(catalog[0]["animations"]), 2)
            self.assertFalse(catalog[0]["animations"][0]["supported"])
            loaded = provider.load_model("s2-model:000")
            self.assertEqual(loaded["kind"], "model")
            self.assertEqual(loaded["s2ModelIndex"], 0)
            self.assertEqual(loaded["animationSlotCount"], 0)

    def test_stadium2_dark_body_bridge_isolated_to_unbound_materials(self) -> None:
        model = {
            "meshes": [
                {"material": {"textureDescriptor": -1, "texture": None, "textureMode": 1, "color": [255, 255, 255, 255]}},
                {"material": {"textureDescriptor": 2, "texture": 0, "color": [255, 255, 255, 255]}},
                {"material": {"textureDescriptor": -1, "texture": None, "textureMode": 0, "color": [255, 255, 255, 255]}},
            ]
        }
        applied = viewer.apply_stadium2_runtime_tint(model, 197)
        self.assertEqual(applied, 1)
        self.assertEqual(model["meshes"][0]["material"]["color"], list(viewer.STADIUM2_DARK_BODY_TINTS[197]))
        self.assertNotIn("runtimeTint", model["meshes"][1]["material"])
        self.assertNotIn("runtimeTint", model["meshes"][2]["material"])
        self.assertEqual(model["s2RuntimeTint"]["meshCount"], 1)
        self.assertEqual(viewer.apply_stadium2_runtime_tint(model, 1), 0)

    def test_stadium2_dark_body_bridge_allows_unown_mode_three_only(self) -> None:
        model = {
            "meshes": [
                {"material": {"textureDescriptor": -1, "texture": None,
                               "textureMode": 3, "color": [255, 255, 255, 255]}},
                {"material": {"textureDescriptor": -1, "texture": None,
                               "textureMode": 0, "color": [255, 255, 255, 255]}},
                {"material": {"textureDescriptor": 4, "texture": 0,
                               "textureMode": 3, "color": [255, 255, 255, 255]}},
            ]
        }
        applied = viewer.apply_stadium2_runtime_tint(model, 201)
        self.assertEqual(applied, 1)
        self.assertEqual(model["meshes"][0]["material"]["color"],
                         list(viewer.STADIUM2_DARK_BODY_TINTS[201]))
        self.assertNotIn("runtimeTint", model["meshes"][1]["material"])
        self.assertNotIn("runtimeTint", model["meshes"][2]["material"])
        self.assertEqual(model["s2RuntimeTint"]["textureModes"], [1, 3])

    def test_stadium2_material_bridge_has_focus_models(self) -> None:
        for model_index in (201, 212, 214, 218, 219, 228, 229, 233):
            model = {"meshes": [{"material": {
                "textureDescriptor": -1,
                "texture": None,
                "textureMode": 1,
                "color": [255, 255, 255, 255],
            }}]}
            self.assertEqual(viewer.apply_stadium2_runtime_tint(model, model_index), 1)
            self.assertEqual(model["meshes"][0]["material"]["color"],
                             list(viewer.STADIUM2_DARK_BODY_TINTS[model_index]))

    def test_stadium2_auxiliary_texture_uses_exact_small_uv_footprint(self) -> None:
        model = {
            "textures": [{"id": 0, "descriptor": 7, "width": 64, "height": 2,
                          "rgba": "AAAA", "alphaMode": "opaque"}],
            "meshes": [{"vertices": [{"uv": [0, 0]}, {"uv": [64, 2]}, {"uv": [32, 1]}],
                        "material": {"textureDescriptor": -1, "texture": None,
                                     "textureMode": 1}}],
        }
        self.assertEqual(viewer.infer_stadium2_auxiliary_textures(model), 1)
        material = model["meshes"][0]["material"]
        self.assertEqual(material["textureDescriptor"], 7)
        self.assertEqual(material["texture"], 0)
        self.assertEqual(material["textureInference"], "s2-uv-footprint")

    def test_stadium2_unit_scale_sentinel_does_not_expand_geometry(self) -> None:
        data = bytearray(0x40)
        put_u8(data, 0, 0x1D)
        put_u8(data, 1, 0)
        for offset in (0x10, 0x14, 0x18):
            put_u32(data, offset, 0xFFFFFFFF)
        context = viewer.ParseContext(bytes(data))
        _, nodes = viewer.parse_geo_layout(viewer.Reader(bytes(data), context), 0, context, [])
        self.assertEqual([node["scale"] for node in nodes if node.get("kind") == "bone"], [[1.0, 1.0, 1.0]])

    def test_framing_ignores_blended_effect_outlier_when_solid_geometry_exists(self) -> None:
        bones = {}
        model = {"meshes": [
            {"vertices": [{"position": [0, 0, 0]}, {"position": [10, 0, 0]}, {"position": [0, 10, 0]}],
             "material": {"alphaMode": "opaque"}},
            {"vertices": [{"position": [-100000, -100000, 0]}],
             "material": {"alphaMode": "blend", "translucent": True}},
        ]}
        minimum, maximum = render_capture.model_bounds(model, bones)
        self.assertEqual(minimum.tolist(), [0.0, 0.0, 0.0])
        self.assertEqual(maximum.tolist(), [10.0, 10.0, 0.0])

    def test_stadium2_extracted_cache_loads_without_source_banks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "models").mkdir()
            (root / "poses" / "000").mkdir(parents=True)
            fragment = build_fragment(animated=False, textured=True, translucent=False)
            (root / "models" / "000.fragment").write_bytes(fragment)
            (root / "poses" / "000" / "000.bin").write_bytes(b"raw-pose")
            manifest = {
                "format": viewer.STADIUM2_EXTRACT_FORMAT,
                "provider": "stadium2",
                "modelCount": 1,
                "poseGroupCount": 1,
                "poseRecordCount": 1,
                "models": [{
                    "index": 0, "path": "models/000.fragment", "size": len(fragment),
                    "sourceOffset": 123, "sourceSize": 456, "modelId": 42,
                    "poseCount": 1, "meshCount": 1, "textureCount": 1,
                    "boneCount": 0, "diagnostics": [],
                }],
                "poseGroups": [{
                    "index": 0, "records": [{"index": 0, "path": "poses/000/000.bin", "size": 8}],
                }],
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            provider = viewer.Stadium2DataProvider(root)
            self.assertIsNotNone(provider._extracted_manifest)
            self.assertIsNone(provider._model_archive)
            catalog = provider.catalog()
            self.assertEqual(catalog[0]["modelId"], 42)
            self.assertEqual(catalog[0]["size"], 456)
            self.assertFalse(catalog[0]["animations"][0]["supported"])
            loaded = provider.load_model("s2-model:000")
            self.assertEqual(loaded["kind"], "model")
            self.assertEqual(loaded["s2ModelRecord"]["offset"], 123)
            self.assertEqual(loaded["s2ModelRecord"]["decodedSize"], len(fragment))
            self.assertFalse(loaded["animations"][0]["supported"])
            self.assertIn("reason", loaded["animations"][0])

    def test_stadium2_extracted_cache_plays_decoded_pose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "models").mkdir()
            (root / "poses" / "000").mkdir(parents=True)
            fragment = build_fragment(animated=False, textured=True, translucent=False)
            (root / "models" / "000.fragment").write_bytes(fragment)
            pose = build_s2_pose_record()
            (root / "poses" / "000" / "000.bin").write_bytes(pose)
            manifest = {
                "format": viewer.STADIUM2_EXTRACT_FORMAT,
                "provider": "stadium2",
                "modelCount": 1,
                "poseGroupCount": 1,
                "poseRecordCount": 1,
                "models": [{
                    "index": 0, "path": "models/000.fragment", "size": len(fragment),
                    "sourceOffset": 123, "sourceSize": 456, "modelId": 42,
                    "poseCount": 1, "meshCount": 1, "textureCount": 1,
                    "boneCount": 0, "diagnostics": [],
                }],
                "poseGroups": [{
                    "index": 0, "records": [{
                        "index": 0, "path": "poses/000/000.bin", "size": len(pose),
                        "metadata": viewer.inspect_stadium2_pose_metadata(pose, "fixture pose"),
                    }],
                }],
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            provider = viewer.Stadium2DataProvider(root)
            catalog = provider.catalog()
            self.assertTrue(catalog[0]["animations"][0]["supported"])
            self.assertEqual(catalog[0]["animations"][0]["frameCount"], 10)
            loaded = provider.load_model("s2-model:000")
            animation = loaded["animations"][0]
            self.assertTrue(animation["supported"])
            self.assertEqual(animation["frameCount"], 10)
            self.assertEqual(animation["curve"]["poses"][0][0]["position"], [100.0, 200.0, 300.0])

    def test_stadium1_model_range_excludes_stadium2(self) -> None:
        self.assertTrue(viewer.stadium1_model_id_supported({"modelId": 1}))
        self.assertTrue(viewer.stadium1_model_id_supported({"modelId": 151}))
        self.assertFalse(viewer.stadium1_model_id_supported({"modelId": 0}))
        self.assertFalse(viewer.stadium1_model_id_supported({"modelId": 152}))
        self.assertFalse(viewer.stadium1_model_id_supported({"modelId": 251}))

    def test_provider_catalog_and_model_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "fixture.fragment"
            resource.write_bytes(build_fragment(animated=True, textured=False, translucent=False))
            provider = viewer.Stadium1DataProvider(resource)
            catalog = provider.catalog()
            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0]["animations"][0]["frameCount"], 2)
            self.assertEqual(provider.load_model(resource.name)["modelId"], 7)

    def test_geolayout_and_f3dex2_vertex_encoding(self) -> None:
        data = bytearray(0x240)
        put_u8(data, 0x00, 0x1D)
        put_u8(data, 0x01, 0)
        put_u8(data, 0x02, 1)
        put_u8(data, 0x03, 0)
        for offset in (0x10, 0x14, 0x18):
            put_u32(data, offset, 0x00010000)
        put_u8(data, 0x1C, 0x05)
        put_u8(data, 0x20, 0x1E)
        put_s16(data, 0x22, 0)
        put_u32(data, 0x24, 0x100)
        put_u8(data, 0x28, 0x06)
        put_u8(data, 0x2C, 0x04)
        put_u32(data, 0x100, 0x01003006)
        put_u32(data, 0x104, 0x200)
        put_u32(data, 0x108, 0x05000204)
        put_u32(data, 0x10C, 0)
        put_u32(data, 0x110, 0xDF000000)
        for index, position in enumerate(((0, 0, 0), (10, 0, 0), (0, 10, 0))):
            at = 0x200 + index * 16
            for axis, value in enumerate(position):
                put_s16(data, at + axis * 2, value)
            data[at + 12:at + 16] = bytes((255, 255, 255, 255))
        context = viewer.ParseContext(bytes(data))
        meshes, nodes = viewer.parse_geo_layout(viewer.Reader(bytes(data), context), 0, context, [])
        self.assertEqual(len(meshes), 1)
        self.assertEqual(len(meshes[0]["indices"]), 3)
        bones = [node for node in nodes if node.get("kind") == "bone"]
        self.assertEqual(len(bones), 1)
        # Geo cmd flags are translated by func_80018490: command bit 0 selects
        # the normal scale-stack branch, represented by node flags bit 0 clear.
        self.assertEqual(bones[0]["flags"], 0)
        self.assertFalse(context.diagnostics)

    def test_stadium2_layout_markers_do_not_shift_following_commands(self) -> None:
        # S2 model 219 contains the extension markers 0x28 and 0x29 before
        # ordinary display-list nodes. They are four-byte graph-state records;
        # treating either as an unknown command shifts the stream and drops
        # most of the model.
        data = bytearray(0x240)
        put_u8(data, 0x00, 0x28)
        put_u8(data, 0x04, 0x29)
        put_u8(data, 0x08, 0x22)
        put_u32(data, 0x0C, pointer(0x100))
        put_u8(data, 0x10, 0x04)
        put_u32(data, 0x100, 0x01003006)
        put_u32(data, 0x104, pointer(0x180))
        put_u32(data, 0x108, 0x05000204)
        put_u32(data, 0x10C, 0)
        put_u32(data, 0x110, 0xDF000000)
        for index, position in enumerate(((0, 0, 0), (10, 0, 0), (0, 10, 0))):
            at = 0x180 + index * 16
            for axis, value in enumerate(position):
                put_s16(data, at + axis * 2, value)
            data[at + 12:at + 16] = bytes((255, 255, 255, 255))

        context = viewer.ParseContext(bytes(data))
        meshes, _ = viewer.parse_geo_layout(viewer.Reader(bytes(data), context), 0, context, [])
        self.assertEqual(len(meshes), 1)
        self.assertEqual(len(meshes[0]["indices"]), 3)
        self.assertFalse(context.diagnostics)

    def test_vertex_keeps_loader_bone(self) -> None:
        # The RSP transforms vertices at G_VTX load time, so a triangle that
        # references cache slots filled by an earlier display list keeps the
        # bone that was active when each slot was loaded (hinge geometry).
        data = bytearray(0x240)
        put_u8(data, 0x00, 0x1D)  # animated part 0
        put_u8(data, 0x01, 0)
        for offset in (0x10, 0x14, 0x18):
            put_u32(data, offset, 0x00010000)
        put_u8(data, 0x1C, 0x05)  # open node
        put_u8(data, 0x20, 0x1E)  # display list bound to part 0: loads verts only
        put_s16(data, 0x22, 0)
        put_u32(data, 0x24, 0x100)
        put_u8(data, 0x28, 0x1D)  # animated part 1, child of part 0
        put_u8(data, 0x29, 1)
        for offset in (0x38, 0x3C, 0x40):
            put_u32(data, offset, 0x00010000)
        put_u8(data, 0x44, 0x05)  # open node
        put_u8(data, 0x48, 0x22)  # display list under part 1: loads 1 vert, draws
        put_u32(data, 0x4C, 0x120)
        put_u8(data, 0x50, 0x06)  # close node
        put_u8(data, 0x54, 0x06)  # close node
        put_u8(data, 0x58, 0x04)  # return
        put_u32(data, 0x100, 0x01003006)  # G_VTX: 3 verts into slots 0-2
        put_u32(data, 0x104, 0x200)
        put_u32(data, 0x108, 0xDF000000)  # G_ENDDL
        put_u32(data, 0x120, 0x01001008)  # G_VTX: 1 vert into slot 3
        put_u32(data, 0x124, 0x230)
        put_u32(data, 0x128, 0x05000604)  # G_TRI1: slots 0, 3, 2
        put_u32(data, 0x12C, 0)
        put_u32(data, 0x130, 0xDF000000)  # G_ENDDL
        for index, position in enumerate(((0, 0, 0), (10, 0, 0), (0, 10, 0), (5, 5, 0))):
            at = 0x200 + index * 16
            for axis, value in enumerate(position):
                put_s16(data, at + axis * 2, value)
            data[at + 12:at + 16] = bytes((255, 255, 255, 255))
        context = viewer.ParseContext(bytes(data))
        meshes, nodes = viewer.parse_geo_layout(viewer.Reader(bytes(data), context), 0, context, [])
        self.assertEqual(len(meshes), 1)  # the part-0 loader list draws nothing
        self.assertEqual(meshes[0]["bone"], 1)
        self.assertEqual([vertex["bone"] for vertex in meshes[0]["vertices"]], [0, 1, 0])
        self.assertFalse(context.diagnostics)

    def test_ci8_texels_are_byte_per_pixel(self) -> None:
        # CI8 uses one byte per texel; reading the stream as packed pairs
        # duplicates every byte across two pixels (the model-151 eye defect).
        palette = [(i, i, i, 255) for i in range(256)]
        ci8 = bytes((1, 2, 3, 4))
        decoded = viewer.decode_ci(ci8, 0, 4, 1, 1, palette)
        self.assertEqual(decoded, bytes((1, 1, 1, 255, 2, 2, 2, 255, 3, 3, 3, 255, 4, 4, 4, 255)))
        ci4 = bytes((0x12, 0x34))
        decoded4 = viewer.decode_ci(ci4, 0, 4, 1, 0, palette)
        self.assertEqual(decoded4, bytes((1, 1, 1, 255, 2, 2, 2, 255, 3, 3, 3, 255, 4, 4, 4, 255)))

    def test_ia16_texel_layout(self) -> None:
        # IA16 texels are two bytes: intensity high, alpha low. Model 15's
        # wings use IA16; the alpha channel cuts the wing shape out of the quad.
        decoded = viewer.decode_ia16(bytes((0x80, 0xFF, 0x40, 0x7F)), 0, 2, 1)
        self.assertEqual(decoded, bytes((0x80, 0x80, 0x80, 0xFF, 0x40, 0x40, 0x40, 0x7F)))

    def test_texture_alpha_modes(self) -> None:
        # Normal model RGBA16 facial/detail planes use one-bit alpha and must
        # stay in the depth-writing cutout pass. Intermediate alpha remains a
        # genuinely blended surface for effects such as translucent wings.
        self.assertEqual(viewer.texture_alpha_mode(bytes((1, 2, 3, 0, 4, 5, 6, 255))), "cutout")
        self.assertEqual(viewer.texture_alpha_mode(bytes((1, 2, 3, 127))), "blend")
        self.assertEqual(viewer.texture_alpha_mode(bytes((1, 2, 3, 255))), "opaque")

    def test_geometry_mode_state(self) -> None:
        # F3DEX2 G_GEOMETRYMODE clears bits via ~w0 and sets them via w1.
        # Model 15's wing lists clear G_LIGHTING and G_CULL_BACK around their
        # quads, so the mesh must render unlit and double-sided.
        def build(clear_lighting: bool, clear_cull_back: bool) -> bytearray:
            data = bytearray(0x200)
            cursor = 0x40
            if clear_lighting:
                put_u32(data, cursor, 0xD9FDFFFF)  # clear G_LIGHTING (0x20000)
                put_u32(data, cursor + 4, 0)
                cursor += 8
            if clear_cull_back:
                put_u32(data, cursor, 0xD9FFFBFF)  # clear G_CULL_BACK (0x400)
                put_u32(data, cursor + 4, 0)
                cursor += 8
            put_u32(data, cursor, 0x01003006)  # G_VTX: 3 vertices at slot 0
            put_u32(data, cursor + 4, pointer(0x100))
            put_u32(data, cursor + 8, 0x05000204)  # G_TRI1 slots 0,1,2
            put_u32(data, cursor + 12, 0)
            put_u32(data, cursor + 16, 0xDF000000)  # G_ENDDL
            for index in range(3):
                at = 0x100 + index * 16
                put_s16(data, at, index * 10)
                data[at + 12:at + 16] = bytes((255, 255, 255, 255))
            return data

        data = build(True, True)
        context = viewer.ParseContext(bytes(data))
        mesh = viewer.parse_display_list(viewer.Reader(bytes(data), context), 0x40, context, [], "wings")
        self.assertEqual(len(mesh["indices"]), 3)
        self.assertIs(mesh["material"]["lighting"], False)
        self.assertTrue(mesh["material"]["doubleSided"])

        # A plain list keeps the defaults: lit and back-face culled.
        data = build(False, False)
        context = viewer.ParseContext(bytes(data))
        mesh = viewer.parse_display_list(viewer.Reader(bytes(data), context), 0x40, context, [], "body")
        self.assertEqual(len(mesh["indices"]), 3)
        self.assertIs(mesh["material"]["lighting"], True)
        self.assertFalse(mesh["material"]["doubleSided"])

    def test_setup_sampler_mirror_clamp(self) -> None:
        # G_SETTILE CMS/CMT combine G_TX_MIRROR (1) and G_TX_CLAMP (2). Model
        # 24's hood textures use 3 (mirror+clamp); mapping 3 to plain clamp
        # hides the hood pattern (UVs fold back instead of pinning the edge).
        data = bytearray(0x40)
        put_u32(data, 0x00, 0xF5100000)  # G_SETTILE
        put_u32(data, 0x04, (1 << 18) | (3 << 8))  # tile 0, cmt=mirror, cms=mirror+clamp
        put_u32(data, 0x08, 0xDF000000)
        context = viewer.ParseContext(bytes(data))
        wrap_s, wrap_t = viewer.texture_setup_sampler(viewer.Reader(bytes(data), context), 0)
        self.assertEqual((wrap_s, wrap_t), ("mirror-clamp", "mirror"))

    def test_mirror_clamp_folds_one_adjacent_tile(self) -> None:
        values = render_capture.np.asarray([-2.4, -1.5, -0.25, 0.5, 1.5, 1.995, 2.4])
        wrapped = render_capture.wrap_coordinate(values, "mirror-clamp")
        self.assertTrue(render_capture.np.allclose(wrapped, [0.0, 0.5, 0.25, 0.5, 0.5, 0.005, 0.0]))

    def test_cumulative_scale_transform_fixture(self) -> None:
        # The renderer's bone builder lives in JavaScript; the synthetic
        # fixture in test_stadium1_viewer_bone_math.js proves the source
        # cumulative-scale sequence (non-unit parent scale, child translation,
        # rotation, and a flags&1 node) against hand-derived expectations.
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for the JavaScript transform fixture")
        script = Path(__file__).with_name("test_stadium1_viewer_bone_math.js")
        completed = subprocess.run([node, str(script)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_stadium1_texture_descriptor_binding(self) -> None:
        data = bytearray(0x240)
        # cmd17: one source texture descriptor, followed by cmd23 selecting
        # descriptor 0 and cmd22 drawing the mesh under that binding.
        put_u8(data, 0x00, 0x17)
        put_s16(data, 0x02, 1)
        put_u32(data, 0x08, pointer(0x40))
        put_u32(data, 0x10, pointer(0x1C0))
        put_u8(data, 0x14, 0x23)
        put_u8(data, 0x15, 1)
        put_s16(data, 0x16, -1)
        put_u32(data, 0x18, pointer(0x120))
        put_s16(data, 0x1C, 0)
        put_s16(data, 0x1E, -1)
        data[0x20:0x24] = bytes((255, 255, 255, 255))
        put_u8(data, 0x24, 0x22)
        put_u8(data, 0x25, 0x01)
        put_u32(data, 0x28, pointer(0x180))
        put_u8(data, 0x2C, 0x01)

        # fmt RGBA / 16-bit texels / 2x2 / 4 texels / local texture pointer.
        put_u8(data, 0x40, 0)
        put_u8(data, 0x41, 2)
        put_s16(data, 0x42, 2)
        put_s16(data, 0x44, 2)
        put_s16(data, 0x46, 4)
        put_u32(data, 0x48, pointer(0x100))
        for index, value in enumerate((0xFFFF, 0xF801, 0x07C1, 0x003E)):
            put_u16(data, 0x100 + index * 2, value)
        put_u32(data, 0x120, 0xF5101000)  # G_SETTILE: tile 0, clamp S/T
        put_u32(data, 0x124, 0x00080200)
        put_u32(data, 0x128, 0xF2000000)
        put_u32(data, 0x12C, 0x00000C04)
        put_u32(data, 0x130, 0xDF000000)
        put_u32(data, 0x134, 0)

        put_u32(data, 0x180, 0x01003006)
        put_u32(data, 0x184, pointer(0x1C0))
        put_u32(data, 0x188, 0x05000204)
        put_u32(data, 0x18C, 0)
        put_u32(data, 0x190, 0xDF000000)
        put_u32(data, 0x194, 0)
        for index, position in enumerate(((0, 0, 0), (10, 0, 0), (0, 10, 0))):
            at = 0x1C0 + index * 16
            for axis, value in enumerate(position):
                put_s16(data, at + axis * 2, value)
            data[at + 12:at + 16] = bytes((127, 127, 127, 255))

        context = viewer.ParseContext(bytes(data))
        textures = []
        meshes, _ = viewer.parse_geo_layout(viewer.Reader(bytes(data), context), 0, context, textures)
        self.assertEqual(len(textures), 1)
        self.assertIn("rgba", textures[0])
        self.assertTrue(textures[0]["hasAlpha"])
        self.assertEqual(meshes[0]["material"]["texture"], 0)
        self.assertEqual(meshes[0]["material"]["nodeMode"], 1)
        self.assertEqual(meshes[0]["material"]["wrapS"], "clamp")
        self.assertEqual(meshes[0]["material"]["wrapT"], "clamp")
        self.assertFalse(context.diagnostics)

    def test_stadium1_cmd23_binding_survives_geo_close(self) -> None:
        # Geo_NodeShadowTexture updates the renderer's active texture state;
        # closing a GeoLayout child does not restore the previous binding.
        # This is the source behavior that keeps Haunter's later spike lists
        # on descriptor 12 after the descriptor-12 child branch closes.
        data = bytearray(0x380)
        put_u8(data, 0x00, 0x17)
        put_s16(data, 0x02, 2)
        put_s16(data, 0x04, 0)
        put_u32(data, 0x08, pointer(0x100))

        # Initial binding: descriptor 0.
        put_u8(data, 0x14, 0x23)
        put_u8(data, 0x15, 1)
        put_s16(data, 0x16, -1)
        put_u32(data, 0x18, pointer(0x180))
        put_s16(data, 0x1C, 0)
        put_s16(data, 0x1E, -1)
        data[0x20:0x24] = bytes((255, 255, 255, 255))

        # Child binding: descriptor 1. The second display list is outside
        # this child, so lexical scope restoration would incorrectly return
        # it to descriptor 0.
        put_u8(data, 0x24, 0x05)
        put_u8(data, 0x28, 0x23)
        put_u8(data, 0x29, 1)
        put_s16(data, 0x2A, -1)
        put_u32(data, 0x2C, pointer(0x180))
        put_s16(data, 0x30, 1)
        put_s16(data, 0x32, -1)
        data[0x34:0x38] = bytes((255, 255, 255, 255))
        put_u8(data, 0x38, 0x22)
        put_u8(data, 0x39, 1)
        put_u32(data, 0x3C, pointer(0x280))
        put_u8(data, 0x40, 0x06)
        put_u8(data, 0x44, 0x22)
        put_u8(data, 0x45, 1)
        put_u32(data, 0x48, pointer(0x280))
        put_u8(data, 0x4C, 0x01)

        # Two small RGBA16 descriptors and their local texel data.
        for index, texture_offset in enumerate((0x200, 0x208)):
            at = 0x100 + index * 0x0C
            put_u8(data, at, 0)
            put_u8(data, at + 1, 2)
            put_s16(data, at + 2, 2)
            put_s16(data, at + 4, 2)
            put_s16(data, at + 6, 4)
            put_u32(data, at + 8, pointer(texture_offset))
            for texel in range(4):
                put_u16(data, texture_offset + texel * 2, 0xFFFF if index == 0 else 0xF801)

        # The cmd23 setup list only supplies sampler state; the display list
        # below supplies the vertices and triangle.
        put_u32(data, 0x180, 0xF5101000)
        put_u32(data, 0x184, 0x00080200)
        put_u32(data, 0x188, 0xF2000000)
        put_u32(data, 0x18C, 0x00000C04)
        put_u32(data, 0x190, 0xDF000000)
        put_u32(data, 0x194, 0)
        put_u32(data, 0x280, 0x01003006)
        put_u32(data, 0x284, pointer(0x300))
        put_u32(data, 0x288, 0x05000204)
        put_u32(data, 0x28C, 0)
        put_u32(data, 0x290, 0xDF000000)
        put_u32(data, 0x294, 0)
        for index, position in enumerate(((0, 0, 0), (10, 0, 0), (0, 10, 0))):
            at = 0x300 + index * 16
            for axis, value in enumerate(position):
                put_s16(data, at + axis * 2, value)
            data[at + 12:at + 16] = bytes((255, 255, 255, 255))

        context = viewer.ParseContext(bytes(data))
        textures = []
        meshes, _ = viewer.parse_geo_layout(viewer.Reader(bytes(data), context), 0, context, textures)
        self.assertEqual([mesh["material"]["textureDescriptor"] for mesh in meshes], [1, 1])
        self.assertFalse(context.diagnostics)

    def test_stadium1_ci_texture_palette_binding(self) -> None:
        data = bytearray(0x240)
        # cmd17 supplies the CI texture descriptors and the source palette
        # descriptor array (unk_01C). cmd23's second descriptor selects the
        # palette used for the CI texture.
        put_u8(data, 0x00, 0x17)
        put_s16(data, 0x02, 1)
        put_s16(data, 0x04, 1)
        put_u32(data, 0x08, pointer(0x40))
        put_u32(data, 0x0C, pointer(0x80))
        put_u32(data, 0x10, pointer(0x1C0))
        put_u8(data, 0x14, 0x23)
        put_u8(data, 0x15, 1)
        put_s16(data, 0x16, -1)
        put_u32(data, 0x18, pointer(0x120))
        put_s16(data, 0x1C, 0)
        put_s16(data, 0x1E, 0)
        data[0x20:0x24] = bytes((255, 255, 255, 255))
        put_u8(data, 0x24, 0x22)
        put_u32(data, 0x28, pointer(0x180))
        put_u8(data, 0x2C, 0x01)

        # fmt CI / 4-bit texels / 2x2. The two nibbles in each source byte
        # select entries from the 16-entry RGBA16 palette.
        put_u8(data, 0x40, 2)
        put_u8(data, 0x41, 0)
        put_s16(data, 0x42, 2)
        put_s16(data, 0x44, 2)
        put_s16(data, 0x46, 4)
        put_u32(data, 0x48, pointer(0x100))
        data[0x100:0x102] = bytes((0x12, 0x21))
        put_u32(data, 0x80, 16)
        put_u32(data, 0x84, pointer(0xA0))
        for index, value in enumerate((0x0001, 0xF801, 0x07C1, 0x003F) + (0xFFFF,) * 12):
            put_u16(data, 0xA0 + index * 2, value)

        put_u32(data, 0x120, 0xF5101000)
        put_u32(data, 0x124, 0x00080200)
        put_u32(data, 0x128, 0xF2000000)
        put_u32(data, 0x12C, 0x00000C04)
        put_u32(data, 0x130, 0xDF000000)
        put_u32(data, 0x134, 0)
        put_u32(data, 0x180, 0x01003006)
        put_u32(data, 0x184, pointer(0x1C0))
        put_u32(data, 0x188, 0x05000204)
        put_u32(data, 0x18C, 0)
        put_u32(data, 0x190, 0xDF000000)
        put_u32(data, 0x194, 0)
        for index, position in enumerate(((0, 0, 0), (10, 0, 0), (0, 10, 0))):
            at = 0x1C0 + index * 16
            for axis, value in enumerate(position):
                put_s16(data, at + axis * 2, value)
            data[at + 12:at + 16] = bytes((127, 127, 127, 255))

        context = viewer.ParseContext(bytes(data))
        textures = []
        meshes, _ = viewer.parse_geo_layout(viewer.Reader(bytes(data), context), 0, context, textures)
        self.assertEqual(len(textures), 1)
        self.assertIn("paletteVariants", textures[0])
        self.assertIn("0", textures[0]["paletteVariants"])
        self.assertEqual(meshes[0]["material"]["textureSecondDescriptor"], 0)
        self.assertFalse(context.diagnostics)


if __name__ == "__main__":
    unittest.main(verbosity=2)
