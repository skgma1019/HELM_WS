from __future__ import annotations

import math
import tempfile
from dataclasses import asdict
from pathlib import Path

import yaml
from PIL import Image, UnidentifiedImageError

from app import geometry


class MapUploadError(ValueError):
    pass


def _as_float(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MapUploadError(f"{field}는 숫자여야 한다") from exc
    if not math.isfinite(number):
        raise MapUploadError(f"{field}는 유한한 숫자여야 한다")
    return number


def validate_map_yaml(yaml_bytes: bytes) -> dict:
    try:
        data = yaml.safe_load(yaml_bytes.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise MapUploadError(f"map.yaml 파싱 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise MapUploadError("map.yaml 최상위 값은 객체여야 한다")
    if "resolution" not in data:
        raise MapUploadError("resolution이 필요하다")
    if "origin" not in data:
        raise MapUploadError("origin이 필요하다")

    resolution = _as_float(data["resolution"], "resolution")
    if resolution <= 0:
        raise MapUploadError("resolution은 0보다 커야 한다")

    origin = data["origin"]
    if not isinstance(origin, list) or len(origin) < 3:
        raise MapUploadError("origin은 [x, y, yaw] 형식이어야 한다")
    origin_x = _as_float(origin[0], "origin[0]")
    origin_y = _as_float(origin[1], "origin[1]")
    origin_yaw = _as_float(origin[2], "origin[2]")
    if origin_yaw != 0.0:
        raise MapUploadError("origin yaw가 0인 지도만 지원한다")

    return {
        **data,
        "image": "map.pgm",
        "resolution": resolution,
        "origin": [origin_x, origin_y, 0.0],
    }


def validate_pgm(pgm_path: Path) -> tuple[int, int]:
    try:
        with Image.open(pgm_path) as image:
            image.verify()
        with Image.open(pgm_path) as image:
            width, height = image.size
    except (OSError, UnidentifiedImageError) as exc:
        raise MapUploadError("map.pgm 이미지 디코딩에 실패했다") from exc
    if width <= 0 or height <= 0:
        raise MapUploadError("map.pgm 크기가 올바르지 않다")
    return width, height


def replace_active_map(map_dir: Path, pgm_bytes: bytes, yaml_bytes: bytes) -> dict:
    yaml_data = validate_map_yaml(yaml_bytes)
    map_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="map-upload-", dir=map_dir) as tmp_name:
        tmp_dir = Path(tmp_name)
        tmp_pgm = tmp_dir / "map.pgm"
        tmp_yaml = tmp_dir / "map.yaml"
        tmp_pgm.write_bytes(pgm_bytes)
        validate_pgm(tmp_pgm)
        tmp_yaml.write_text(yaml.safe_dump(yaml_data, sort_keys=False), encoding="utf-8")

        final_pgm = map_dir / "map.pgm"
        final_yaml = map_dir / "map.yaml"
        tmp_pgm.replace(final_pgm)
        tmp_yaml.replace(final_yaml)

        for old in map_dir.iterdir():
            if old.name not in {"map.pgm", "map.yaml", ".gitkeep"} and old.is_file():
                old.unlink()

    meta = geometry.load_map_meta(map_dir)
    if meta is None:
        raise MapUploadError("지도 교체 후 metadata를 읽을 수 없다")
    return asdict(meta)
