"""Render Stadium 1 viewer resources without a browser.

This is a deliberately small validation renderer, not a second game renderer.
It consumes the same parsed `/api/model` data as the WebGL viewer and covers
the checks useful for regression work: textured triangles, alpha, source tile
wrap modes, static bone transforms, and sampled animation poses.
Generated PNGs and the JSON report belong in a temporary output directory.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image


DEFAULT_REGRESSION = [
    "0.bin", "1.bin", "2.bin", "14.bin", "15.bin", "16.bin", "17.bin",
    "20.bin", "23.bin", "25.bin", "84.bin", "87.bin", "88.bin", "93.bin",
    "102.bin", "125.bin", "145.bin", "150.bin",
]
DEFAULT_REPRESENTATIVE = [
    "0.bin", "1.bin", "2.bin", "14.bin", "23.bin", "25.bin", "108.bin",
    "119.bin", "131.bin", "145.bin", "150.bin",
]


def fetch_json(base_url: str, path: str) -> Dict[str, Any]:
    url = base_url.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def normalize(value: Sequence[float]) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float64)
    length = np.linalg.norm(vector)
    return vector / (length if length else 1.0)


def translate(value: Sequence[float]) -> np.ndarray:
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, 3] = value[:3]
    return matrix


def scale(value: Sequence[float]) -> np.ndarray:
    matrix = np.eye(4, dtype=np.float64)
    matrix[0, 0], matrix[1, 1], matrix[2, 2] = value[:3]
    return matrix


def rotate_xyz(degrees: Sequence[float]) -> np.ndarray:
    x, y, z = (math.radians(float(v)) for v in degrees[:3])
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    rx = np.array([[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]], dtype=np.float64)
    ry = np.array([[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]], dtype=np.float64)
    rz = np.array([[cz, -sz, 0, 0], [sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=np.float64)
    return rz @ ry @ rx


def n64_degrees(value: Sequence[float]) -> List[float]:
    return [float(v) * 180.0 / 32768.0 for v in value[:3]]


def billboard_matrix(view_matrix: np.ndarray, parent_world: np.ndarray, position: Sequence[float]) -> np.ndarray:
    parent_scale = np.linalg.norm(parent_world[:3, :3], axis=0)
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = view_matrix[:3, :3].T @ np.diag(parent_scale)
    matrix[:3, 3] = transform_point(parent_world, position)
    return matrix


def build_bone_matrices(
    bones: Sequence[Dict[str, Any]],
    poses: Optional[Sequence[Dict[str, Any]]],
    view_matrix: Optional[np.ndarray] = None,
) -> Dict[int, np.ndarray]:
    by_id = {int(bone["id"]): bone for bone in bones}
    matrices: Dict[int, np.ndarray] = {}
    states: Dict[int, Tuple[np.ndarray, Optional[np.ndarray], np.ndarray]] = {}
    visiting: set[int] = set()

    def build(bone_id: int) -> np.ndarray:
        if bone_id in matrices:
            return matrices[bone_id]
        if bone_id in visiting:
            return np.eye(4, dtype=np.float64)
        visiting.add(bone_id)
        bone = by_id.get(bone_id, {})
        pose_index = bone.get("poseIndex", bone.get("joint"))
        pose = None
        if poses is not None and isinstance(pose_index, int) and 0 <= pose_index < len(poses):
            pose = poses[pose_index]
        position = (pose or {}).get("position", bone.get("position", [0, 0, 0]))
        rotation = n64_degrees((pose or {}).get("rotation", bone.get("rotation", [0, 0, 0])))
        bone_scale = (pose or {}).get("scale", bone.get("scale", [1, 1, 1]))

        parent_id = bone.get("parent")
        parent_state = None
        if parent_id is not None and int(parent_id) in by_id:
            build(int(parent_id))
            parent_state = states.get(int(parent_id))
        parent_world = parent_state[0] if parent_state else np.eye(4, dtype=np.float64)
        parent_composed = parent_state[1] if parent_state else None
        parent_stack = parent_state[2] if parent_state else np.ones(3, dtype=np.float64)

        if int(bone.get("flags", 0)) & 2 and view_matrix is not None:
            billboard = billboard_matrix(view_matrix, parent_world, position)
            world = billboard @ rotate_xyz(rotation) @ scale(bone_scale)
            composed = world
            # func_80010228 does not push the cumulative scale stack.
            stack = parent_stack
        elif int(bone.get("flags", 0)) & 1:
            world = parent_world @ translate(position) @ rotate_xyz(rotation) @ scale(bone_scale)
            composed = None
            stack = parent_stack
        else:
            cumulative = parent_stack * np.asarray(bone_scale[:3], dtype=np.float64)
            local = translate(np.asarray(position[:3], dtype=np.float64) * parent_stack) @ rotate_xyz(rotation)
            composed = (parent_composed if parent_composed is not None else parent_world) @ local
            world = composed @ scale(cumulative)
            stack = cumulative
        states[bone_id] = (world, composed, stack)
        matrices[bone_id] = world
        visiting.remove(bone_id)
        return world

    for bone in bones:
        build(int(bone["id"]))
    return matrices


def transform_point(matrix: np.ndarray, value: Sequence[float]) -> np.ndarray:
    point = matrix @ np.array([value[0], value[1], value[2], 1.0], dtype=np.float64)
    return point[:3]


def transform_normal(matrix: np.ndarray, value: Sequence[float]) -> np.ndarray:
    return normalize(matrix[:3, :3] @ np.asarray(value[:3], dtype=np.float64))


def look_at(eye: np.ndarray, target: np.ndarray) -> np.ndarray:
    z_axis = normalize(eye - target)
    x_axis = normalize(np.cross(np.array([0.0, 1.0, 0.0]), z_axis))
    y_axis = np.cross(z_axis, x_axis)
    return np.array([
        [x_axis[0], x_axis[1], x_axis[2], -np.dot(x_axis, eye)],
        [y_axis[0], y_axis[1], y_axis[2], -np.dot(y_axis, eye)],
        [z_axis[0], z_axis[1], z_axis[2], -np.dot(z_axis, eye)],
        [0, 0, 0, 1],
    ], dtype=np.float64)


def perspective(fovy: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / math.tan(fovy / 2.0)
    nf = 1.0 / (near - far)
    return np.array([
        [f / aspect, 0, 0, 0],
        [0, f, 0, 0],
        [0, 0, (far + near) * nf, 2 * far * near * nf],
        [0, 0, -1, 0],
    ], dtype=np.float64)


def model_bounds(model: Dict[str, Any], bones: Dict[int, np.ndarray]) -> Tuple[np.ndarray, np.ndarray]:
    points: List[np.ndarray] = []
    meshes = list(model.get("meshes", []))
    # Blended display-list branches are often effects/shadows rather than the
    # model silhouette.  In S2 #196 one such billboard uses the source
    # 0xFFFFFFFF unit-scale sentinel; including it in framing makes Espeon
    # appear absent.  Keep blended geometry in the render, but fit to opaque
    # and cutout model surfaces whenever those exist.
    solid_meshes = [
        mesh for mesh in meshes
        if not (mesh.get("material") or {}).get("translucent")
        and (mesh.get("material") or {}).get("alphaMode") != "blend"
    ]
    if solid_meshes:
        meshes = solid_meshes
    for mesh in meshes:
        for vertex in mesh.get("vertices", []):
            bone_id = vertex.get("bone", mesh.get("bone"))
            point = np.asarray(vertex.get("position", [0, 0, 0]), dtype=np.float64)
            if bone_id is not None and int(bone_id) in bones:
                point = transform_point(bones[int(bone_id)], point)
            points.append(point)
    if not points:
        return np.array([-1.0, -1.0, -1.0]), np.array([1.0, 1.0, 1.0])
    values = np.asarray(points)
    return values.min(axis=0), values.max(axis=0)


def decode_texture(item: Optional[Dict[str, Any]], palette_key: Optional[str]) -> Optional[np.ndarray]:
    if not item:
        return None
    encoded = item.get("rgba")
    if palette_key is not None:
        encoded = (item.get("paletteVariants") or {}).get(palette_key, encoded)
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded)
        width, height = int(item["width"]), int(item["height"])
        return np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4))
    except (ValueError, KeyError, base64.binascii.Error):
        return None


def texture_for(model: Dict[str, Any], animation: Optional[Dict[str, Any]], frame: int, material: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    textures = model.get("textures", [])
    descriptor = material.get("textureDescriptor")
    track = (animation or {}).get("eventTrack")
    slot = material.get("textureAnimIndex")
    if isinstance(track, dict) and track.get("supported") and isinstance(slot, int) and slot >= 0:
        segments = track.get("segments") or []
        mapping = track.get("mapping") or []
        if slot < int(track.get("slotCount", 0)) and slot < len(segments):
            start, length = segments[slot]
            source_frame = max(0, min(int(frame), max(0, int(track.get("frameCount", 1)) - 1)))
            mapping_index = source_frame + length if source_frame < start else start + length - 1
            if 0 <= mapping_index < len(mapping):
                descriptor = mapping[mapping_index]
    selected = next((item for item in textures if item.get("descriptor") == descriptor), None)
    if selected is None:
        texture_index = material.get("texture")
        selected = textures[int(texture_index)] if isinstance(texture_index, int) and 0 <= texture_index < len(textures) else None
    return selected


def texture_alpha_mode(item: Optional[Dict[str, Any]], palette_key: Optional[str]) -> str:
    if not item:
        return "opaque"
    if palette_key is not None:
        modes = item.get("paletteAlphaMode") or {}
        if palette_key in modes:
            return str(modes[palette_key])
    return str(item.get("alphaMode") or ("blend" if item.get("hasAlpha") else "opaque"))


def wrap_coordinate(value: np.ndarray, mode: str) -> np.ndarray:
    if mode == "clamp":
        return np.clip(value, 0.0, 1.0)
    if mode == "mirror":
        return 1.0 - np.abs(np.mod(value, 2.0) - 1.0)
    if mode == "mirror-clamp":
        # G_TX_MIRROR | G_TX_CLAMP mirrors the first adjacent tile, then
        # clamps coordinates outside that two-tile interval. This matters for
        # model 102, whose face S coordinates reach almost two texture widths.
        mirrored = np.abs(value)
        mirrored = np.where(mirrored > 1.0, 2.0 - mirrored, mirrored)
        return np.clip(mirrored, 0.0, 1.0)
    return np.mod(value, 1.0)


def sample_texture(texture: np.ndarray, uv: np.ndarray, wrap_s: str, wrap_t: str) -> np.ndarray:
    height, width = texture.shape[:2]
    u = wrap_coordinate(uv[:, 0], wrap_s)
    v = wrap_coordinate(uv[:, 1], wrap_t)
    x = np.clip((u * max(0, width - 1)).astype(np.int32), 0, width - 1)
    y = np.clip((v * max(0, height - 1)).astype(np.int32), 0, height - 1)
    return texture[y, x].astype(np.float64) / 255.0


def project(matrix: np.ndarray, points: np.ndarray, width: int, height: int) -> Tuple[np.ndarray, np.ndarray]:
    homogeneous = np.concatenate((points, np.ones((len(points), 1), dtype=np.float64)), axis=1)
    clip = homogeneous @ matrix.T
    safe_w = np.where(np.abs(clip[:, 3]) < 1e-8, 1e-8, clip[:, 3])
    ndc = clip[:, :3] / safe_w[:, None]
    screen = np.column_stack(((ndc[:, 0] * 0.5 + 0.5) * (width - 1), (1.0 - (ndc[:, 1] * 0.5 + 0.5)) * (height - 1)))
    return screen, clip[:, 2], safe_w


def rasterize_triangle(
    image: np.ndarray,
    depth: np.ndarray,
    screen: np.ndarray,
    clip_z: np.ndarray,
    clip_w: np.ndarray,
    uv: np.ndarray,
    colors: np.ndarray,
    normals: np.ndarray,
    texture: Optional[np.ndarray],
    material: Dict[str, Any],
    lighting_enabled: bool,
    write_depth: bool = True,
    depth_test: bool = True,
) -> None:
    min_x = max(0, int(math.floor(float(screen[:, 0].min()))))
    max_x = min(image.shape[1] - 1, int(math.ceil(float(screen[:, 0].max()))))
    min_y = max(0, int(math.floor(float(screen[:, 1].min()))))
    max_y = min(image.shape[0] - 1, int(math.ceil(float(screen[:, 1].max()))))
    if min_x > max_x or min_y > max_y:
        return
    yy, xx = np.mgrid[min_y:max_y + 1, min_x:max_x + 1]
    p = np.column_stack((xx.ravel(), yy.ravel()))
    a, b, c = screen
    area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if abs(area) < 1e-8:
        return
    # The model display-list setup restores G_CULL_BACK for ordinary model
    # surfaces (see func_80015684 in the decomp).  The WebGL renderer already
    # honors material.doubleSided; keep this validation renderer in lockstep.
    # Screen Y is downward here, so source-front CCW triangles have a negative
    # signed area after projection.  Deliberately retain both windings only
    # for display lists that cleared the source cull state.
    if not material.get("doubleSided", False) and area >= 0.0:
        return
    weights = np.column_stack((
        ((b[0] - p[:, 0]) * (c[1] - p[:, 1]) - (b[1] - p[:, 1]) * (c[0] - p[:, 0])) / area,
        ((c[0] - p[:, 0]) * (a[1] - p[:, 1]) - (c[1] - p[:, 1]) * (a[0] - p[:, 0])) / area,
        ((a[0] - p[:, 0]) * (b[1] - p[:, 1]) - (a[1] - p[:, 1]) * (b[0] - p[:, 0])) / area,
    ))
    inside = np.all(weights >= -1e-6, axis=1)
    if not np.any(inside):
        return
    inv_w = 1.0 / clip_w
    weighted_w = weights @ inv_w
    # Perspective-correct depth uses clip_z / clip_w in the numerator. The
    # previous code omitted inv_w there, which made overlapping parts sort
    # incorrectly whenever a triangle spanned a meaningful depth range.
    z = (weights @ (clip_z * inv_w)) / weighted_w
    pixels = p[inside]
    z_inside = z[inside]
    current_depth = depth[pixels[:, 1], pixels[:, 0]]
    visible = np.ones(len(pixels), dtype=bool) if not depth_test else (z_inside < current_depth)
    if not np.any(visible):
        return
    pixels = pixels[visible]
    z_inside = z_inside[visible]
    visible_weights = weights[inside][visible]
    visible_inv_w = weighted_w[inside][visible]
    perspective_weights = visible_weights * inv_w[None, :] / visible_inv_w[:, None]
    uv_pixels = perspective_weights @ uv
    vertex_colors = perspective_weights @ colors
    vertex_normals = perspective_weights @ normals
    vertex_normals /= np.maximum(np.linalg.norm(vertex_normals, axis=1, keepdims=True), 1e-8)

    if texture is not None:
        sampled = sample_texture(texture, uv_pixels, material.get("wrapS", "repeat"), material.get("wrapT", "repeat"))
    else:
        sampled = np.ones((len(pixels), 4), dtype=np.float64)
    material_color = np.asarray(material.get("color", [255, 255, 255, 255]), dtype=np.float64) / 255.0
    sampled *= material_color[None, :]
    if lighting_enabled and material.get("lighting", True):
        light_direction = normalize([0.45, 0.8, 0.6])
        diffuse = np.maximum(vertex_normals @ light_direction, 0.0)
        sampled[:, :3] *= (0.28 + diffuse[:, None] * 0.72)
    elif texture is None:
        sampled *= vertex_colors
    sampled[:, 3] *= vertex_colors[:, 3]

    if material.get("alphaMode") == "cutout":
        keep = sampled[:, 3] >= 0.5
        if not np.any(keep):
            return
        pixels = pixels[keep]
        z_inside = z_inside[keep]
        sampled = sampled[keep]

    px, py = pixels[:, 0], pixels[:, 1]
    destination = image[py, px]
    alpha = np.clip(sampled[:, 3], 0.0, 1.0)[:, None]
    image[py, px, :3] = sampled[:, :3] * alpha + destination[:, :3] * (1.0 - alpha)
    image[py, px, 3] = 1.0
    if write_depth:
        depth[py, px] = z_inside


def texture_has_alpha(item: Optional[Dict[str, Any]], palette_key: Optional[str]) -> bool:
    if not item:
        return False
    if palette_key is not None:
        alpha_map = item.get("paletteAlpha") or {}
        if palette_key in alpha_map:
            return bool(alpha_map[palette_key])
    return bool(item.get("hasAlpha"))


def render_model(
    model: Dict[str, Any],
    animation: Optional[Dict[str, Any]],
    frame: int,
    width: int,
    height: int,
    yaw: float,
    pitch: float,
    distance: float,
    fit_center: Optional[Sequence[float]] = None,
    fit_radius: Optional[float] = None,
) -> Image.Image:
    image = np.zeros((height, width, 4), dtype=np.float64)
    image[:, :, :3] = np.array([0.035, 0.05, 0.075], dtype=np.float64)
    image[:, :, 3] = 1.0
    depth = np.full((height, width), np.inf, dtype=np.float64)
    bones_data = model.get("skeleton", {}).get("bones", [])
    curve = (animation or {}).get("curve") or {}
    poses = curve.get("poses") if curve.get("poses") else None
    pose_index = min(max(0, int(frame)), max(0, len(poses or []) - 1)) if poses else 0
    base_bones = build_bone_matrices(bones_data, None)
    minimum, maximum = model_bounds(model, base_bones)
    center = np.asarray(fit_center, dtype=np.float64) if fit_center is not None else (minimum + maximum) * 0.5
    radius = max(0.01, float(fit_radius) if fit_radius is not None else float(np.max(maximum - minimum)) * 0.5)
    eye = center + np.array([math.sin(yaw) * math.cos(pitch), math.sin(pitch), math.cos(yaw) * math.cos(pitch)]) * distance * radius
    view = look_at(eye, center)
    current_bones = build_bone_matrices(bones_data, poses[pose_index] if poses else None, view)
    projection = perspective(math.pi / 4.0, width / max(1, height), 0.01, max(1000.0, distance * radius * 10.0))
    matrix = projection @ view

    texture_cache: Dict[Tuple[int, Optional[str]], Optional[np.ndarray]] = {}
    prepared: List[Dict[str, Any]] = []
    for mesh in model.get("meshes", []):
        vertices = mesh.get("vertices", [])
        indices = mesh.get("indices", [])
        material = mesh.get("material") or {}
        if len(vertices) < 3 or len(indices) < 3:
            continue
        item = texture_for(model, animation, frame, material)
        palette = str(material.get("textureSecondDescriptor")) if isinstance(material.get("textureSecondDescriptor"), int) and material.get("textureSecondDescriptor") >= 0 else None
        key = (int(item.get("id", -1)) if item else -1, palette)
        if key not in texture_cache:
            texture_cache[key] = decode_texture(item, palette)
        texture = texture_cache[key]
        alpha_mode = str(material.get("alphaMode") or texture_alpha_mode(item, palette))
        if material.get("translucent"):
            alpha_mode = "blend"
        positions: List[np.ndarray] = []
        uvs: List[List[float]] = []
        colors: List[List[float]] = []
        normals: List[np.ndarray] = []
        for vertex in vertices:
            bone_id = vertex.get("bone", mesh.get("bone"))
            bone = current_bones.get(int(bone_id)) if bone_id is not None else None
            point = np.asarray(vertex.get("position", [0, 0, 0]), dtype=np.float64)
            normal = np.asarray(vertex.get("normal", [0, 1, 0]), dtype=np.float64)
            if bone is not None:
                point = transform_point(bone, point)
                normal = transform_normal(bone, normal)
            positions.append(point)
            colors.append(np.asarray(vertex.get("color", [255, 255, 255, 255]), dtype=np.float64) / 255.0)
            normals.append(normal)
            uv = vertex.get("uv", [0, 0])
            if item:
                uvs.append([float(uv[0]) / max(1, int(item.get("width", 1))), float(uv[1]) / max(1, int(item.get("height", 1)))])
            else:
                uvs.append([0.0, 0.0])
        screen, clip_z, clip_w = project(matrix, np.asarray(positions), width, height)
        prepared.append({
            "indices": [int(index) for index in indices],
            "vertex_count": len(vertices),
            "screen": screen,
            "clip_z": clip_z,
            "clip_w": clip_w,
            "uvs": np.asarray(uvs),
            "colors": np.asarray(colors),
            "normals": np.asarray(normals),
            "texture": texture,
            "material": {**material, "alphaMode": alpha_mode},
            "alpha_mode": alpha_mode,
            "translucent": alpha_mode == "blend",
        })

    # Source-like translucent ordering: the RDP draws opaque surfaces first
    # (render mode RM_AA_OPA_SURF, depth compare + update) and alpha-blended
    # RM_AA_XLU_SURF surfaces last, still testing depth but never writing it,
    # so translucent layers cannot punch holes in the opaque body behind them.
    for translucent_pass in (False, True):
        for entry in prepared:
            if entry["translucent"] != translucent_pass or entry["material"].get("renderLayer") == "expression":
                continue
            indices = entry["indices"]
            for offset in range(0, len(indices) - 2, 3):
                selected = [indices[offset], indices[offset + 1], indices[offset + 2]]
                if any(index < 0 or index >= entry["vertex_count"] for index in selected):
                    continue
                rasterize_triangle(
                    image, depth, entry["screen"][selected], entry["clip_z"][selected], entry["clip_w"][selected],
                    entry["uvs"][selected], entry["colors"][selected], entry["normals"][selected],
                    entry["texture"], entry["material"], True,
                    write_depth=not translucent_pass,
                )
    # Expression textures are cutout quads selected by the source event
    # track.  Some extracted S2 heads place those quads inside the opaque
    # head volume; drawing this small pass without a depth test preserves the
    # visible facial expression while keeping alpha cutout and culling.
    for entry in prepared:
        if entry["material"].get("renderLayer") != "expression":
            continue
        indices = entry["indices"]
        for offset in range(0, len(indices) - 2, 3):
            selected = [indices[offset], indices[offset + 1], indices[offset + 2]]
            if any(index < 0 or index >= entry["vertex_count"] for index in selected):
                continue
            rasterize_triangle(
                image, depth, entry["screen"][selected], entry["clip_z"][selected], entry["clip_w"][selected],
                entry["uvs"][selected], entry["colors"][selected], entry["normals"][selected],
                entry["texture"], entry["material"], True, write_depth=False, depth_test=False,
            )
    return Image.fromarray(np.uint8(np.clip(image * 255.0, 0, 255)), mode="RGBA")


def select_cases(catalog: Dict[str, Any], selected: Optional[Sequence[str]], set_name: str, static_only: bool) -> List[Tuple[str, str, int, int]]:
    resources = {str(item["path"]): item for item in catalog.get("models", [])}
    paths = list(selected) if selected else (list(resources) if set_name == "all" else (DEFAULT_REPRESENTATIVE if set_name == "representative" else DEFAULT_REGRESSION))
    missing = [path for path in paths if path not in resources]
    if missing:
        raise RuntimeError("Models are not present in the live catalog: " + ", ".join(missing))
    cases: List[Tuple[str, str, int, int]] = []
    for path in paths:
        stem = Path(path).stem
        cases.append((f"{stem}-static", path, -1, 0))
        if static_only:
            continue
        supported = [item for item in resources[path].get("animations", []) if item.get("supported") and int(item.get("frameCount", 0)) > 0]
        if supported:
            animation = supported[0]
            animation_id = int(animation["id"])
            midpoint = max(0, (int(animation["frameCount"]) - 1) // 2)
            cases.append((f"{stem}-anim{animation_id}-frame0", path, animation_id, 0))
            if midpoint:
                cases.append((f"{stem}-anim{animation_id}-frame{midpoint}", path, animation_id, midpoint))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8767")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--set", choices=("regression", "representative", "all"), default="regression")
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=640)
    parser.add_argument("--yaw", type=float, default=0.0)
    parser.add_argument("--pitch", type=float, default=0.05)
    parser.add_argument("--distance", type=float, default=2.4)
    args = parser.parse_args()
    output_dir = Path(args.output_dir or (Path.home() / "AppData" / "Local" / "Temp" / "stadium1-viewer-validation-native"))
    output_dir.mkdir(parents=True, exist_ok=True)
    health = fetch_json(args.base_url, "/api/health")
    if not health.get("ok"):
        raise RuntimeError("Viewer health check failed")
    catalog = fetch_json(args.base_url, "/api/catalog")
    cases = select_cases(catalog, args.models, args.set, args.static_only)
    results = []
    model_cache: Dict[str, Dict[str, Any]] = {}
    for name, path, animation_id, frame in cases:
        if path not in model_cache:
            encoded_path = urllib.parse.quote(path)
            model_cache[path] = fetch_json(args.base_url, f"/api/model?path={encoded_path}")
        model = model_cache[path]
        animation = next((item for item in model.get("animations", []) if int(item.get("id", -1)) == animation_id), None) if animation_id >= 0 else None
        # Windows turns an unsanitized ":" into an alternate data stream.
        safe_name = name.replace(":", "-")
        output = output_dir / f"{safe_name}.png"
        print(f"Rendering {safe_name} ...", flush=True)
        render_model(model, animation, frame, args.width, args.height, args.yaw, args.pitch, args.distance).save(output)
        results.append({"name": safe_name, "model": path, "animation": animation_id, "frame": frame, "path": str(output), "bytes": output.stat().st_size})
    report = {"health": health, "set": args.set, "count": len(results), "captures": results}
    report_path = output_dir / "render-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Completed {len(results)} captures.")
    print(f"Output: {output_dir}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, urllib.error.URLError, ValueError) as error:
        print(f"render_capture: {error}", file=sys.stderr)
        raise SystemExit(1)
