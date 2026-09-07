from __future__ import annotations

import base64
import importlib
import io
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    # main의 정적 파일 마운트도 실제 data/를 사용하지 않도록 import 전에 격리한다.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "helm.db")
    for name, folder in [("SNAPSHOTS_DIR", "snapshots"), ("MAP_DIR", "map"), ("SAMPLES_DIR", "samples")]:
        path = tmp_path / folder
        path.mkdir()
        monkeypatch.setattr(config, name, path)
    monkeypatch.setattr(config, "ROBOT_TOKEN", "pytest-robot-token")
    monkeypatch.setattr(config, "DEV_MODE", True)
    monkeypatch.setattr(config, "SEVERITY_RATIO_CAUTION", 1.0)
    monkeypatch.setattr(config, "SEVERITY_RATIO_DANGER", 1.3)
    monkeypatch.setattr(config, "THERMAL_OVERSHOOT_DANGER_C", 10.0)
    module = importlib.import_module("app.main")
    for route in module.app.routes:
        if route.path == "/snapshots":
            monkeypatch.setattr(route.app, "directory", str(config.SNAPSHOTS_DIR))
            monkeypatch.setattr(route.app, "all_directories", [str(config.SNAPSHOTS_DIR)])
    db.init_db()
    return module


@pytest.fixture
def client(server: ModuleType) -> Iterator[TestClient]:
    with TestClient(server.app) as client:
        yield client


@pytest.fixture
def headers() -> dict[str, str]:
    return {"X-Robot-Token": "pytest-robot-token"}


@pytest.fixture
def event() -> dict:
    image = io.BytesIO()
    Image.new("RGB", (640, 480)).save(image, format="JPEG")
    return {
        "id": "20260914-2147-A-001", "robot_id": "helm-01",
        "station_id": "A", "station_name": "1번 설비 전면", "seq": 1,
        "ts": 1755648012.345, "x": 1.26, "y": 0.35, "yaw": 1.55,
        "pose_error": {"pos_m": 0.028, "yaw_rad": 0.021},
        "verdict": "ANOMALY", "triggered_by": ["vision"],
        "vision": {"is_anomaly": True, "score": 0.87, "threshold": 0.62,
                   "regions": [{"bbox": [412, 233, 96, 140], "area_px": 13440}]},
        "thermal": {"is_anomaly": False, "max_temp_c": 43.2, "threshold_c": 50.0},
        "image_b64": base64.b64encode(image.getvalue()).decode("ascii"),
    }
