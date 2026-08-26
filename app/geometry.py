"""좌표 변환(월드 ↔ 픽셀)과 구역 판정.

변환식은 설계서 5장 그대로다. 이 파일과 static/map.js 두 곳에만 존재해야 하고,
하나를 고치면 반드시 다른 하나도 같이 고친다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from PIL import Image


@dataclass
class MapMeta:
    image: str  # map.yaml의 image 항목 — 파일명만 (map_dir 기준 상대경로)
    resolution: float  # m/pixel
    origin_x: float  # 지도 좌하단 픽셀의 월드 x (m)
    origin_y: float  # 지도 좌하단 픽셀의 월드 y (m)
    width: int  # 이미지 가로 픽셀 수
    height: int  # 이미지 세로 픽셀 수


def load_map_meta(map_dir: Path) -> MapMeta | None:
    """data/map/map.yaml + 실제 이미지 파일을 읽어 MapMeta를 만든다. 지도가 없으면 None."""
    yaml_path = map_dir / "map.yaml"
    if not yaml_path.is_file():
        return None

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    image_name = data.get("image", "map.png")
    image_path = map_dir / image_name
    if not image_path.is_file():
        return None

    # 이미지 크기는 yaml에 안 적혀 있으니 실제 파일에서 읽는다 — px<->world 변환의 height가 여기서 나온다
    with Image.open(image_path) as img:
        width, height = img.size

    origin = data.get("origin", [0.0, 0.0, 0.0])
    return MapMeta(
        image=image_name,
        resolution=float(data.get("resolution", 0.05)),
        origin_x=float(origin[0]),
        origin_y=float(origin[1]),
        width=width,
        height=height,
    )


def pixel_to_world(px: float, py: float, meta: MapMeta) -> tuple[float, float]:
    world_x = meta.origin_x + px * meta.resolution
    world_y = meta.origin_y + (meta.height - py) * meta.resolution  # height 빼는 게 핵심 (5장)
    return world_x, world_y


def world_to_pixel(world_x: float, world_y: float, meta: MapMeta) -> tuple[float, float]:
    px = (world_x - meta.origin_x) / meta.resolution
    py = meta.height - (world_y - meta.origin_y) / meta.resolution
    return px, py


def point_in_polygon(x: float, y: float, points: list[list[float]]) -> bool:
    """레이 캐스팅(PNPOLY). points는 월드 좌표 [[x,y], ...]."""
    n = len(points)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def point_in_circle(x: float, y: float, center: list[float], radius: float) -> bool:
    cx, cy = center
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def point_in_zone(x: float, y: float, zone: dict) -> bool:
    kind = zone.get("kind")
    if kind == "circle":
        center = zone.get("center")
        radius = zone.get("radius")
        if not center or radius is None:
            return False
        return point_in_circle(x, y, center, radius)
    # rect는 저장 시 이미 4점 폴리곤으로 변환돼 있다 (설계서 4-2) — polygon과 같은 경로를 탄다
    points = zone.get("points")
    if not points:
        return False
    return point_in_polygon(x, y, points)


_ZONE_SEVERITY_PRIORITY = {"danger": 0, "caution": 1, "safe": 2}


def zones_containing(x: float, y: float, zones: list[dict]) -> list[dict]:
    """(x,y)를 포함하는 구역들을 위험등급 높은 순(danger > caution > safe)으로 반환한다."""
    matched = [z for z in zones if point_in_zone(x, y, z)]
    matched.sort(key=lambda z: _ZONE_SEVERITY_PRIORITY.get(z.get("severity"), 99))
    return matched
