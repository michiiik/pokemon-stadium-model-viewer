"""Capture a complete Stadium 1/Stadium 2 model animation sweep.

The sweep uses the browser-independent validation renderer. For every model it
writes a static/base image and first, middle, and last images for the first
supported animation. ``--all-animations`` expands that to every supported
animation. Animation cameras are fitted once from evenly spaced poses across
the complete curve, so flying or jumping models stay inside a fixed frame at
all requested capture points.

Generated PNGs, contact sheets, and JSON belong in an external output
directory and must not be committed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw

try:
    import render_capture as native
except ImportError:  # pragma: no cover - supports direct package execution
    from . import render_capture as native


BACKGROUND = np.array([8, 12, 19], dtype=np.int16)
CAPTURE_ROLES = ("static", "first", "middle", "last")


def console_text(value: object) -> str:
    """Keep progress output printable on legacy Windows console encodings."""
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding)


def fetch_json(base_url: str, path: str, timeout: int = 120) -> Dict[str, Any]:
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=timeout) as response:
        return json.load(response)


def provider_label(provider: str) -> str:
    return {"stadium1": "Stadium 1", "stadium2": "Stadium 2"}.get(provider, provider)


def safe_name(value: str) -> str:
    return value.replace(":", "-").replace("/", "-").replace("\\", "-")


def frame_points(frame_count: int) -> List[Tuple[str, int]]:
    if frame_count <= 0:
        return []
    values = [("first", 0), ("middle", max(0, (frame_count - 1) // 2)), ("last", frame_count - 1)]
    result: List[Tuple[str, int]] = []
    seen: set[int] = set()
    for role, frame in values:
        if frame not in seen:
            result.append((role, frame))
            seen.add(frame)
    return result


def fit_frame_indices(animation: Dict[str, Any], max_samples: int) -> List[int]:
    poses = (animation.get("curve") or {}).get("poses") or []
    if not poses:
        return [0]
    count = len(poses)
    sample_count = max(3, min(count, int(max_samples)))
    indices = {0, count // 2, count - 1}
    if sample_count > 3:
        indices.update(int(round(value)) for value in np.linspace(0, count - 1, sample_count))
    return sorted(indices)


def camera_fit(model: Dict[str, Any], animation: Optional[Dict[str, Any]], max_samples: int) -> Dict[str, Any]:
    """Fit one static camera to the base pose or sampled animation envelope."""
    bones_data = model.get("skeleton", {}).get("bones", [])
    if animation and (animation.get("curve") or {}).get("poses"):
        indices = fit_frame_indices(animation, max_samples)
        pose_list = (animation.get("curve") or {}).get("poses") or []
    else:
        indices = [0]
        pose_list = [None]
    minima: Optional[np.ndarray] = None
    maxima: Optional[np.ndarray] = None
    for index in indices:
        pose = pose_list[min(index, len(pose_list) - 1)] if pose_list else None
        bones = native.build_bone_matrices(bones_data, pose)
        minimum, maximum = native.model_bounds(model, bones)
        minima = minimum if minima is None else np.minimum(minima, minimum)
        maxima = maximum if maxima is None else np.maximum(maxima, maximum)
    assert minima is not None and maxima is not None
    center = (minima + maxima) * 0.5
    # Add enough margin for interpolation between sampled poses and raster
    # edges. The render distance is also deliberately farther than the normal
    # validation captures because this sweep is intended to find clipping.
    radius = max(0.01, float(np.max(maxima - minima)) * 0.5 * 1.18)
    return {"center": center.tolist(), "radius": radius, "fitFrames": indices}


def image_metrics(image: Image.Image) -> Dict[str, Any]:
    pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    mask = np.max(np.abs(pixels - BACKGROUND[None, None, :]), axis=2) > 10
    ys, xs = np.where(mask)
    height, width = mask.shape
    if not len(xs):
        return {"foregroundPixels": 0, "foregroundRatio": 0.0, "bbox": None, "edgeMargin": None, "blank": True, "edgeClipped": False}
    bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    edge_margin = min(bbox[0], bbox[1], width - 1 - bbox[2], height - 1 - bbox[3])
    return {
        "foregroundPixels": int(mask.sum()),
        "foregroundRatio": float(mask.mean()),
        "bbox": bbox,
        "edgeMargin": int(edge_margin),
        "blank": bool(mask.mean() < 0.002),
        "edgeClipped": bool(edge_margin <= 2),
    }


def image_difference(first: Image.Image, last: Image.Image) -> float:
    left = np.asarray(first.convert("RGB"), dtype=np.int16)
    right = np.asarray(last.convert("RGB"), dtype=np.int16)
    return float(np.abs(left - right).mean())


def fit_text(draw: ImageDraw.ImageDraw, text: str, xy: Tuple[int, int], fill: Tuple[int, int, int]) -> None:
    draw.text(xy, text, fill=fill)


def write_contact_sheets(provider_dir: Path, model_groups: List[Dict[str, Any]], sheet_dir: Path, rows_per_sheet: int, thumb_size: Tuple[int, int]) -> List[str]:
    sheet_dir.mkdir(parents=True, exist_ok=True)
    cell_width, image_height = thumb_size
    cell_height = image_height + 28
    sheet_width = cell_width * 4
    sheet_paths: List[str] = []
    for page_start in range(0, len(model_groups), rows_per_sheet):
        page = model_groups[page_start:page_start + rows_per_sheet]
        sheet = Image.new("RGB", (sheet_width, cell_height * len(page)), (8, 12, 19))
        draw = ImageDraw.Draw(sheet)
        for row, group in enumerate(page):
            y = row * cell_height
            for column, role in enumerate(CAPTURE_ROLES):
                capture = group.get(role)
                x = column * cell_width
                if capture and Path(capture["path"]).is_file():
                    with Image.open(capture["path"]) as source:
                        image = source.convert("RGB")
                    image.thumbnail((cell_width - 4, image_height - 18), Image.Resampling.LANCZOS)
                    sheet.paste(image, (x + (cell_width - image.width) // 2, y + 2))
                label = role if role == "static" else f"{role} f{capture['frame']}" if capture else role
                fit_text(draw, label, (x + 4, y + image_height + 3), (210, 220, 230))
            fit_text(draw, str(group["label"]), (4, y + 2), (100, 220, 205))
        path = sheet_dir / f"page-{page_start // rows_per_sheet + 1:03d}.png"
        sheet.save(path)
        sheet_paths.append(str(path))
    return sheet_paths


def capture_provider(base_url: str, provider: str, output_root: Path, width: int, height: int, distance: float, fit_samples: int, all_animations: bool, selected_models: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    catalog = fetch_json(base_url, f"/api/catalog?provider={urllib.parse.quote(provider)}")
    resources = sorted(catalog.get("models", []), key=lambda item: str(item.get("path", "")))
    if selected_models:
        wanted = set(selected_models)
        found = {str(item.get("path", "")) for item in resources}
        missing = sorted(wanted - found)
        if missing:
            raise RuntimeError(f"Models are not present for {provider}: {', '.join(missing)}")
        resources = [item for item in resources if str(item.get("path", "")) in wanted]
    provider_dir = output_root / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    models: List[Dict[str, Any]] = []
    sheet_groups: List[Dict[str, Any]] = []
    captures: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for number, resource in enumerate(resources, start=1):
        path = str(resource.get("path", ""))
        label = str(resource.get("name", path))
        print(console_text(f"[{provider_label(provider)} {number}/{len(resources)}] {label}"), flush=True)
        model_entry: Dict[str, Any] = {"path": path, "label": label, "animations": [], "captures": [], "issues": []}
        try:
            encoded = urllib.parse.urlencode({"provider": provider, "path": path})
            model = fetch_json(base_url, f"/api/model?{encoded}")
            model_entry["modelId"] = model.get("modelId")
            model_entry["diagnostics"] = model.get("diagnostics", [])
            supported = [item for item in model.get("animations", []) if item.get("supported") and int(item.get("frameCount", 0)) > 0]
            selected = supported if all_animations else supported[:1]
            model_entry["animations"] = [{"id": int(item["id"]), "name": item.get("name"), "frameCount": int(item.get("frameCount", 0))} for item in selected]
            static_fit = camera_fit(model, None, fit_samples)
            static_path = provider_dir / f"{safe_name(path)}-static.png"
            static_image = native.render_model(model, None, 0, width, height, 0.0, 0.05, distance, static_fit["center"], static_fit["radius"])
            static_image.save(static_path)
            static_capture = {"role": "static", "path": str(static_path), "frame": None, "animation": None, "metrics": image_metrics(static_image)}
            model_entry["captures"].append(static_capture)
            captures.append({**static_capture, "provider": provider, "model": path})
            for animation in selected:
                animation_id = int(animation["id"])
                loaded_animation = next(item for item in model.get("animations", []) if int(item.get("id", -1)) == animation_id)
                fit = camera_fit(model, loaded_animation, fit_samples)
                animation_captures: Dict[str, Image.Image] = {}
                for role, frame in frame_points(int(loaded_animation.get("frameCount", 0))):
                    filename = f"{safe_name(path)}-anim{animation_id:03d}-{role}-f{frame:04d}.png"
                    output = provider_dir / filename
                    image = native.render_model(model, loaded_animation, frame, width, height, 0.0, 0.05, distance, fit["center"], fit["radius"])
                    image.save(output)
                    animation_captures[role] = image
                    capture = {"role": role, "path": str(output), "frame": frame, "animation": animation_id, "metrics": image_metrics(image), "fit": fit}
                    model_entry["captures"].append(capture)
                    captures.append({**capture, "provider": provider, "model": path})
                sheet_group = {"label": f"{label} anim {animation_id:03d}", "static": static_capture}
                for capture in model_entry["captures"]:
                    if capture.get("animation") == animation_id and capture.get("role") in ("first", "middle", "last"):
                        sheet_group[capture["role"]] = capture
                sheet_groups.append(sheet_group)
                if animation_captures:
                    first = animation_captures.get("first") or animation_captures.get("static")
                    last = animation_captures.get("last") or first
                    difference = image_difference(first, last) if first and last else 0.0
                    model_entry.setdefault("animationMetrics", []).append({"animation": animation_id, "firstLastMeanAbsDiff": difference})
            if not selected:
                model_entry["issues"].append({"code": "no-supported-animation", "hint": "Check whether this is a static model or whether its animation list/pose bank is incomplete."})
                sheet_groups.append({"label": label, "static": static_capture})
            if any(item.get("severity") == "error" for item in model_entry["diagnostics"]):
                model_entry["issues"].append({"code": "model-diagnostics-error", "hint": "Compare this model with others sharing its texture/GeoLayout or archive family."})
            for capture in model_entry["captures"]:
                if capture["metrics"]["blank"]:
                    model_entry["issues"].append({"code": "blank-render", "hint": "Likely parser, camera, empty mesh, or unsupported display-list state."})
                if capture["metrics"]["edgeClipped"]:
                    model_entry["issues"].append({"code": "edge-clipped", "hint": "Compare with other flying/large-bounds models; inspect animation fit samples and billboard bones."})
        except Exception as error:  # Continue the sweep so one bad resource is reportable.
            model_entry["issues"].append({"code": "capture-error", "message": str(error), "hint": "Inspect the API resource report and compare the model's archive/pose family."})
            errors.append({"provider": provider, "model": path, "error": str(error)})
        models.append(model_entry)
    sheets = write_contact_sheets(provider_dir, sheet_groups, output_root / "contact-sheets" / provider, 5, (240, 150))
    issue_counts: Dict[str, int] = {}
    for model in models:
        for issue in model["issues"]:
            code = str(issue.get("code", "unknown"))
            issue_counts[code] = issue_counts.get(code, 0) + 1
    blank_count = sum(1 for capture in captures if capture["metrics"].get("blank"))
    edge_clipped_count = sum(1 for capture in captures if capture["metrics"].get("edgeClipped"))
    return {
        "provider": provider,
        "catalogCount": len(resources),
        "models": models,
        "captures": captures,
        "errors": errors,
        "contactSheets": sheets,
        "summary": {
            "captureCount": len(captures),
            "errorCount": len(errors),
            "blankCaptureCount": blank_count,
            "edgeClippedCaptureCount": edge_clipped_count,
            "modelsWithIssues": sum(1 for model in models if model["issues"]),
            "issueCounts": issue_counts,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8769")
    parser.add_argument("--providers", nargs="+", choices=("stadium1", "stadium2"), default=("stadium1", "stadium2"))
    parser.add_argument("--output-dir", default=None, help="external output directory; defaults to a timestamped temp folder")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=640)
    parser.add_argument("--model", action="append", dest="models", help="capture only this provider path; repeat for a small validation subset")
    parser.add_argument("--distance", type=float, default=3.6, help="camera distance multiplier after animation-envelope fitting")
    parser.add_argument("--fit-samples", type=int, default=24, help="poses sampled to fit each animation camera")
    parser.add_argument("--all-animations", action="store_true", help="capture first/middle/last for every supported animation, not only the first")
    args = parser.parse_args()
    if args.fit_samples < 3:
        raise SystemExit("--fit-samples must be at least 3")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root = Path(args.output_dir) if args.output_dir else Path(tempfile.gettempdir()) / f"pms-model-viewer-sweep-{timestamp}"
    output_root.mkdir(parents=True, exist_ok=True)
    health = fetch_json(args.base_url, "/api/health")
    available = {item.get("provider") for item in health.get("providers", [])} if health.get("mode") == "dual" else {health.get("provider")}
    missing = [provider for provider in args.providers if provider not in available]
    if missing:
        raise SystemExit(f"Viewer does not expose requested providers: {', '.join(missing)}; available: {', '.join(sorted(str(item) for item in available))}")
    results = [capture_provider(args.base_url, provider, output_root, args.width, args.height, args.distance, args.fit_samples, args.all_animations, args.models) for provider in args.providers]
    report = {"baseUrl": args.base_url, "health": health, "providers": args.providers, "allAnimations": args.all_animations, "width": args.width, "height": args.height, "distance": args.distance, "fitSamples": args.fit_samples, "results": results}
    report_path = output_root / "sweep-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Completed sweep: {sum(len(item['captures']) for item in results)} captures")
    for result in results:
        summary = result["summary"]
        print(
            f"{provider_label(result['provider'])}: {summary['captureCount']} captures, "
            f"{summary['errorCount']} errors, {summary['blankCaptureCount']} blank, "
            f"{summary['edgeClippedCaptureCount']} edge-clipped"
        )
    print(f"Output: {output_root}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
