from __future__ import annotations

import asyncio
import base64
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import config, db


@pytest.mark.parametrize("ratio,expected", [(0.99, "caution"), (1.0, "caution"), (1.29999, "caution"), (1.3, "danger")])
def test_severity_ratio(server: ModuleType, event: dict, ratio: float, expected: str) -> None:
    event["vision"].update(score=ratio, threshold=1.0)
    assert server._compute_severity(event) == expected


@pytest.mark.parametrize("overshoot,expected", [(9.999, "caution"), (10.0, "danger")])
def test_thermal_boundary(server: ModuleType, event: dict, overshoot: float, expected: str) -> None:
    event["vision"].update(is_anomaly=False, score=0.1)
    event["thermal"].update(is_anomaly=True, max_temp_c=50 + overshoot)
    assert server._compute_severity(event) == expected


def test_both_channels_zero_threshold_and_normal(server: ModuleType, event: dict) -> None:
    event["vision"]["threshold"] = 0
    assert server._compute_severity(event) == "caution"
    event["thermal"]["is_anomaly"] = True
    assert server._compute_severity(event) == "danger"
    event["verdict"] = "NORMAL"
    assert server._compute_severity(event) == "info"


def test_configurable_thresholds(server: ModuleType, event: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEVERITY_RATIO_DANGER", 2.0)
    monkeypatch.setattr(config, "THERMAL_OVERSHOOT_DANGER_C", 20.0)
    assert server._compute_severity(event) == "caution"
    event["thermal"]["max_temp_c"] = 70
    assert server._compute_severity(event) == "danger"


def test_rest_roundtrip_duplicate_filter_and_ack(client: TestClient, headers: dict, event: dict) -> None:
    response = client.post("/api/events", headers=headers, json=event)
    assert response.status_code == 200
    assert response.json() == {"type": "ack", "id": event["id"], "duplicate": False}
    assert client.post("/api/events", headers=headers, json=event).json()["duplicate"] is True
    records = client.get("/api/events?verdict=ANOMALY").json()
    assert len(records) == 1
    record = records[0]
    for key in ("id", "station_id", "station_name", "seq", "ts", "x", "y", "yaw", "triggered_by"):
        assert record[key] == event[key]
    assert record["severity"] == "danger"
    assert record["vision_regions"] == event["vision"]["regions"]
    assert record["vision_is_anomaly"] is True
    assert record["thermal_is_anomaly"] is False
    assert record["pose_pos_error_m"] == 0.028
    assert record["pose_yaw_error_rad"] == 0.021
    assert "type" not in record and "confidence" not in record
    assert client.get(record["image_path"]).content == base64.b64decode(event["image_b64"])
    assert client.get("/api/events?verdict=NORMAL").json() == []
    assert client.post(f"/api/events/{event['id']}/ack").status_code == 200
    assert client.post(f"/api/events/{event['id']}/ack").status_code == 404
    assert client.get("/api/events?acked=false").json() == []
    acked = client.get("/api/events?acked=true").json()[0]
    assert acked["acked_by"] == "dashboard" and acked["acked_at"] > 0


def test_normal_and_batch_replay(client: TestClient, headers: dict, event: dict) -> None:
    event.update(verdict="NORMAL", triggered_by=[])
    event["vision"].update(is_anomaly=False, score=0.1, regions=[])
    response = client.post("/api/events", headers=headers, json=[event, event])
    assert [ack["duplicate"] for ack in response.json()] == [False, True]
    record = db.list_events(verdict="NORMAL")[0]
    assert record["severity"] == "info"
    assert record["triggered_by"] == [] and record["vision_regions"] == []


def test_missing_ts_rejected(server: ModuleType, event: dict) -> None:
    del event["ts"]
    with pytest.raises(ValueError, match="event.ts"):
        asyncio.run(server._process_event(event))
    assert db.list_events() == []
    assert list(config.SNAPSHOTS_DIR.iterdir()) == []


def test_zone_escalation(server: ModuleType, event: dict) -> None:
    db.replace_zones([{"id": "z", "name": "위험 구역", "kind": "circle", "severity": "danger", "center": [1.26, 0.35], "radius": 1}])
    event["vision"]["score"] = 0.62
    assert server._compute_severity(event) == "caution"
    asyncio.run(server._process_event(event))
    record = db.list_events()[0]
    assert (record["severity"], record["zone_id"], record["zone_name"]) == ("danger", "z", "위험 구역")


def test_ws_event_broadcast_duplicate_and_ack(client: TestClient, headers: dict, event: dict) -> None:
    with client.websocket_connect("/ws/dashboard") as dashboard:
        assert dashboard.receive_json()["type"] == "snapshot"
        with client.websocket_connect("/ws/robot?token=pytest-robot-token") as robot:
            robot.send_json({"type": "hello", "robot_id": "helm-01"})
            assert robot.receive_json()["type"] == "welcome"
            robot.send_json({"type": "event", "event": event})
            assert robot.receive_json()["duplicate"] is False
            pushed = dashboard.receive_json()
            assert pushed["type"] == "event" and pushed["event"]["ts"] == event["ts"]
            assert pushed["delayed"] is True
            robot.send_json({"type": "event", "event": event})
            assert robot.receive_json()["duplicate"] is True
            client.post(f"/api/events/{event['id']}/ack")
            # 중복 이벤트가 broadcast되지 않았다면 다음 메시지는 확인 처리다.
            assert dashboard.receive_json()["type"] == "event_acked"
    assert len(db.list_events()) == 1


def test_auth_and_telemetry(client: TestClient, headers: dict, event: dict) -> None:
    assert client.post("/api/events", json=event).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/robot?token=wrong"):
            pass
    pose = {"robot_id": "helm-01", "x": 3.42, "y": -1.15, "yaw": 1.57, "battery": 78.0, "state": "patrolling"}
    assert client.post("/api/robot/status", headers=headers, json=pose).status_code == 200
    record = db.list_robot_status()[0]
    for key, value in pose.items():
        assert record[key] == value
