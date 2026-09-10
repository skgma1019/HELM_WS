from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db


def pgm_bytes(width: int = 6, height: int = 4, value: int = 255) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (width, height), value).save(buffer, format="PPM")
    return buffer.getvalue()


def yaml_bytes(
    *,
    resolution: object = 0.1,
    origin: object = None,
    image: str = "map.pgm",
) -> bytes:
    data = {
        "image": image,
        "resolution": resolution,
        "origin": [1.0, 2.0, 0.0] if origin is None else origin,
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
    }
    return yaml.safe_dump(data, sort_keys=False).encode("utf-8")


def upload(client: TestClient, pgm: bytes, map_yaml: bytes):
    return client.post(
        "/api/map/upload",
        files={
            "map_pgm": ("anything.pgm", pgm, "image/x-portable-graymap"),
            "map_yaml": ("anything.yaml", map_yaml, "application/x-yaml"),
        },
    )


def seed_active_map(map_dir: Path) -> None:
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "map.pgm").write_bytes(pgm_bytes(3, 3, 128))
    (map_dir / "map.yaml").write_bytes(yaml_bytes(resolution=0.25, origin=[-1.0, -2.0, 0.0]))


def assert_seed_map_preserved() -> None:
    assert Image.open(config.MAP_DIR / "map.pgm").size == (3, 3)
    restored = yaml.safe_load((config.MAP_DIR / "map.yaml").read_text(encoding="utf-8"))
    assert restored["resolution"] == 0.25
    assert restored["origin"] == [-1.0, -2.0, 0.0]


def test_valid_pgm_and_yaml_upload_replaces_active_map(client: TestClient) -> None:
    response = upload(
        client,
        pgm_bytes(7, 5),
        yaml_bytes(image=r"C:\Users\kang\maps\map.pgm", resolution=0.05, origin=[-3.0, 4.0, 0.0]),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["width"] == 7
    assert response.json()["height"] == 5
    assert response.json()["resolution"] == 0.05
    assert response.json()["origin"] == [-3.0, 4.0, 0.0]
    assert Image.open(config.MAP_DIR / "map.pgm").size == (7, 5)
    saved_yaml = yaml.safe_load((config.MAP_DIR / "map.yaml").read_text(encoding="utf-8"))
    assert saved_yaml["image"] == "map.pgm"
    assert saved_yaml["resolution"] == 0.05
    assert saved_yaml["origin"] == [-3.0, 4.0, 0.0]

    image_response = client.get("/api/map/image")
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(image_response.content)).size == (7, 5)


@pytest.mark.parametrize(
    "bad_yaml",
    [
        yaml.safe_dump({"origin": [0.0, 0.0, 0.0]}).encode("utf-8"),
        yaml.safe_dump({"resolution": 0.05}).encode("utf-8"),
        yaml_bytes(resolution=0),
        yaml_bytes(resolution=-0.1),
        yaml_bytes(origin=[0.0, 0.0]),
        yaml_bytes(origin=["x", 0.0, 0.0]),
        yaml_bytes(origin=[0.0, 0.0, 1.57]),
    ],
)
def test_invalid_yaml_upload_fails_and_preserves_active_map(client: TestClient, bad_yaml: bytes) -> None:
    seed_active_map(config.MAP_DIR)

    response = upload(client, pgm_bytes(7, 5), bad_yaml)

    assert response.status_code == 400
    assert_seed_map_preserved()


@pytest.mark.parametrize("bad_pgm", [b"not an image", b""])
def test_invalid_pgm_upload_fails_and_preserves_active_map(client: TestClient, bad_pgm: bytes) -> None:
    seed_active_map(config.MAP_DIR)

    response = upload(client, bad_pgm, yaml_bytes(resolution=0.05, origin=[-3.0, 4.0, 0.0]))

    assert response.status_code == 400
    assert_seed_map_preserved()


def test_keepout_mask_uses_new_uploaded_map_metadata(client: TestClient) -> None:
    upload_response = upload(client, pgm_bytes(8, 6), yaml_bytes(resolution=0.2, origin=[10.0, 20.0, 0.0]))
    assert upload_response.status_code == 200
    db.replace_zones(
        [
            {
                "id": "k1",
                "name": "상단 금지구역",
                "kind": "polygon",
                "severity": "danger",
                "points": [[10.2, 21.0], [10.6, 21.0], [10.6, 20.6], [10.2, 20.6]],
            }
        ]
    )

    pgm_response = client.get("/api/nav2/keepout_mask.pgm")
    assert pgm_response.status_code == 200
    assert pgm_response.content.startswith(b"P5\n8 6\n255\n")

    yaml_response = client.get("/api/nav2/keepout_mask.yaml")
    assert yaml_response.status_code == 200
    keepout_yaml = yaml.safe_load(yaml_response.text)
    assert keepout_yaml["resolution"] == 0.2
    assert keepout_yaml["origin"] == [10.0, 20.0, 0.0]
