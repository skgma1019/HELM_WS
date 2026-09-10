from __future__ import annotations

import io
from dataclasses import asdict

import yaml
from PIL import Image, ImageDraw

from app import geometry


def _keepout_zones(zones: list[dict]) -> list[dict]:
    return [z for z in zones if z.get("kind") == "polygon" and z.get("points")]


def build_keepout_mask_pgm(meta: geometry.MapMeta, zones: list[dict]) -> bytes:
    """Nav2 Keepout Filter용 grayscale PGM. 금지구역은 0, 통행 가능 영역은 255."""
    image = Image.new("L", (meta.width, meta.height), 255)
    draw = ImageDraw.Draw(image)
    for zone in _keepout_zones(zones):
        points = [geometry.world_to_pixel(float(x), float(y), meta) for x, y in zone["points"]]
        if len(points) >= 3:
            draw.polygon(points, fill=0)

    buffer = io.BytesIO()
    image.save(buffer, format="PPM")
    return buffer.getvalue()


def build_keepout_mask_yaml(meta: geometry.MapMeta) -> bytes:
    data = {
        "image": "keepout_mask.pgm",
        "resolution": meta.resolution,
        "origin": [meta.origin_x, meta.origin_y, 0.0],
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
    }
    return yaml.safe_dump(data, sort_keys=False).encode("utf-8")


def map_meta_dict(meta: geometry.MapMeta) -> dict:
    return asdict(meta)
