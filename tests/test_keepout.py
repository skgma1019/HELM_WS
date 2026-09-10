from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db, geometry, nav2


def write_test_map(map_dir: Path, width: int = 10, height: int = 10) -> geometry.MapMeta:
    map_dir.mkdir(parents=True, exist_ok=True)
    Image.new("L", (width, height), 255).save(map_dir / "map.png")
    (map_dir / "map.yaml").write_text(
        yaml.safe_dump(
            {
                "image": "map.png",
                "resolution": 0.5,
                "origin": [-2.0, -1.0, 0.0],
                "negate": 0,
                "occupied_thresh": 0.65,
                "free_thresh": 0.196,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    meta = geometry.load_map_meta(map_dir)
    assert meta is not None
    return meta


def read_pgm_pixels(data: bytes) -> tuple[int, int, list[int]]:
    lines = data.splitlines()
    assert lines[0] == b"P5"
    index = 1
    while lines[index].startswith(b"#"):
        index += 1
    width, height = map(int, lines[index].split())
    max_value = int(lines[index + 1])
    assert max_value == 255
    pixel_start = sum(len(line) + 1 for line in lines[: index + 2])
    return width, height, list(data[pixel_start:])


def pixel_value(pixels: list[int], width: int, px: int, py: int) -> int:
    return pixels[py * width + px]


@pytest.mark.parametrize("pixel", [(0, 0), (9, 9), (2.5, 7.25)])
def test_keepout_coordinate_roundtrip(pixel: tuple[float, float]) -> None:
    meta = geometry.MapMeta("map.png", 0.5, -2.0, -1.0, 10, 10)
    world = geometry.pixel_to_world(*pixel, meta)
    assert geometry.world_to_pixel(*world, meta) == pytest.approx(pixel)
    assert geometry.pixel_to_world(0, 0, meta) == pytest.approx((-2.0, 4.0))
    assert geometry.pixel_to_world(0, 10, meta) == pytest.approx((-2.0, -1.0))


def test_zones_api_saves_loads_and_deletes_polygon(client: TestClient) -> None:
    polygon = [[-1.5, 3.5], [-0.5, 3.5], [-0.5, 2.5], [-1.5, 2.5]]
    payload = [{"id": "k1", "name": "상단 금지구역", "kind": "polygon", "severity": "danger", "points": polygon}]

    saved = client.put("/api/zones", json=payload)
    assert saved.status_code == 200
    assert saved.json()["zones"][0]["points"] == polygon
    assert client.get("/api/zones").json()[0]["points"] == polygon

    deleted = client.put("/api/zones", json=[])
    assert deleted.status_code == 200
    assert client.get("/api/zones").json() == []


def test_keepout_pgm_uses_map_size_and_does_not_flip_vertical_axis() -> None:
    meta = geometry.MapMeta("map.png", 0.5, -2.0, -1.0, 10, 10)
    top_zone = {
        "kind": "polygon",
        "points": [[-1.5, 3.5], [-0.5, 3.5], [-0.5, 2.5], [-1.5, 2.5]],
    }
    width, height, pixels = read_pgm_pixels(nav2.build_keepout_mask_pgm(meta, [top_zone]))

    assert (width, height) == (10, 10)
    assert pixel_value(pixels, width, 2, 2) == 0
    assert pixel_value(pixels, width, 2, 8) == 255
    assert pixel_value(pixels, width, 8, 2) == 255


def test_keepout_download_api_returns_pgm_and_nav2_yaml(server: ModuleType, client: TestClient) -> None:
    meta = write_test_map(config.MAP_DIR)
    db.replace_zones(
        [
            {
                "id": "k1",
                "name": "상단 금지구역",
                "kind": "polygon",
                "severity": "danger",
                "points": [[-1.5, 3.5], [-0.5, 3.5], [-0.5, 2.5], [-1.5, 2.5]],
            }
        ]
    )

    pgm = client.get("/api/nav2/keepout_mask.pgm")
    assert pgm.status_code == 200
    width, height, pixels = read_pgm_pixels(pgm.content)
    assert (width, height) == (meta.width, meta.height)
    assert pixel_value(pixels, width, 2, 2) == 0
    assert pixel_value(pixels, width, 2, 8) == 255

    yaml_res = client.get("/api/nav2/keepout_mask.yaml")
    assert yaml_res.status_code == 200
    data = yaml.safe_load(yaml_res.text)
    assert data == {
        "image": "keepout_mask.pgm",
        "resolution": 0.5,
        "origin": [-2.0, -1.0, 0.0],
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
    }
