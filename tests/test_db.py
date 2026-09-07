from __future__ import annotations

from types import ModuleType

from app import db


def test_db_roundtrip_filters_and_duplicate(server: ModuleType) -> None:
    record = {
        "id": "a", "robot_id": "helm-01", "station_id": "A", "station_name": "설비",
        "seq": 1, "severity": "caution", "verdict": "ANOMALY", "ts": 123.5,
        "received_at": 124.0, "x": -1.5, "y": 2.25, "yaw": 1.0,
        "triggered_by": ["vision"], "vision_is_anomaly": True,
        "vision_regions": [{"bbox": [1, 2, 3, 4], "area_px": 12}],
        "thermal_is_anomaly": False,
    }
    assert db.insert_event(record)
    assert not db.insert_event({**record, "station_name": "중복"})
    restored = db.list_events()[0]
    for key, value in record.items():
        assert restored[key] == value
    assert db.insert_event({**record, "id": "b", "ts": 125.5, "verdict": "NORMAL", "severity": "info"})
    assert [r["id"] for r in db.list_events(limit=1)] == ["b"]
    assert [r["id"] for r in db.list_events(verdict="ANOMALY", severity="caution", acked=False)] == ["a"]
    assert db.ack_event("a", "tester")
    assert not db.ack_event("a", "tester")
    assert not db.ack_event("missing", "tester")
    assert db.list_events(acked=True)[0]["acked_by"] == "tester"


def test_zone_and_robot_roundtrip(server: ModuleType) -> None:
    zones = [
        {"id": "p", "name": "polygon", "kind": "polygon", "severity": "caution", "points": [[0, 0], [2, 0], [1, 2]]},
        {"id": "c", "name": "circle", "kind": "circle", "severity": "danger", "center": [0, 0], "radius": 2},
    ]
    db.replace_zones(zones)
    for expected, restored in zip(zones, db.list_zones()):
        for key, value in expected.items():
            assert restored[key] == value
    db.upsert_robot_status("helm-01", 1, 2, 0.5, 90, "idle", 100)
    db.upsert_robot_status("helm-01", 3, 4, 1.5, 80, "patrolling", 110)
    assert db.list_robot_status() == [{"robot_id": "helm-01", "x": 3, "y": 4, "yaw": 1.5, "battery": 80, "state": "patrolling", "last_seen": 110}]
