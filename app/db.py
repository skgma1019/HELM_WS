"""SQLite 접근 함수. SQL은 이 파일 밖으로 새지 않는다 (규칙 4)."""
from __future__ import annotations

import json
import sqlite3
import time

from app import config


def _connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL: 대시보드가 읽는 동안 로봇 이벤트 쓰기가 막히지 않아야 한다
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                robot_id TEXT NOT NULL,
                station_id TEXT NOT NULL,
                station_name TEXT,
                seq INTEGER,
                severity TEXT NOT NULL,
                verdict TEXT NOT NULL,
                ts REAL NOT NULL,
                received_at REAL NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                yaw REAL,
                pose_pos_error_m REAL,
                pose_yaw_error_rad REAL,
                triggered_by TEXT,
                vision_is_anomaly INTEGER,
                vision_score REAL,
                vision_threshold REAL,
                vision_regions TEXT,
                thermal_is_anomaly INTEGER,
                thermal_max_temp_c REAL,
                thermal_threshold_c REAL,
                zone_id TEXT,
                zone_name TEXT,
                image_path TEXT,
                acked INTEGER NOT NULL DEFAULT 0,
                acked_by TEXT,
                acked_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS zones (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                note TEXT,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                points TEXT,
                center TEXT,
                radius REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS robot_status (
                robot_id TEXT PRIMARY KEY,
                x REAL,
                y REAL,
                yaw REAL,
                battery REAL,
                state TEXT,
                last_seen REAL
            )
            """
        )
        conn.commit()


# ---------- events ----------
#
# 탐지 방식이 PatchCore 기반 이상탐지로 바뀌면서 "이상 종류 이름표"가 사라지고
# station/verdict/vision/thermal/pose_error 중심 스키마가 됐다 (CLAUDE.md 연동 규격 참고).
# triggered_by, vision_regions는 배열이라 JSON 텍스트로 저장한다 — zones.points와 같은 방식.

def insert_event(event: dict) -> bool:
    """event["id"]는 로봇이 만든 문자열 id. 이미 있으면 무시하고 False를 반환한다."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO events
                (id, robot_id, station_id, station_name, seq, severity, verdict,
                 ts, received_at, x, y, yaw,
                 pose_pos_error_m, pose_yaw_error_rad, triggered_by,
                 vision_is_anomaly, vision_score, vision_threshold, vision_regions,
                 thermal_is_anomaly, thermal_max_temp_c, thermal_threshold_c,
                 zone_id, zone_name, image_path, acked, acked_by, acked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                event["id"],
                event["robot_id"],
                event["station_id"],
                event.get("station_name"),
                event.get("seq"),
                event["severity"],
                event["verdict"],
                event["ts"],
                event["received_at"],
                event["x"],
                event["y"],
                event.get("yaw"),
                event.get("pose_pos_error_m"),
                event.get("pose_yaw_error_rad"),
                json.dumps(event["triggered_by"]) if event.get("triggered_by") is not None else None,
                event.get("vision_is_anomaly"),
                event.get("vision_score"),
                event.get("vision_threshold"),
                json.dumps(event["vision_regions"]) if event.get("vision_regions") is not None else None,
                event.get("thermal_is_anomaly"),
                event.get("thermal_max_temp_c"),
                event.get("thermal_threshold_c"),
                event.get("zone_id"),
                event.get("zone_name"),
                event.get("image_path"),
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def _row_to_event(row: sqlite3.Row) -> dict:
    ev = dict(row)
    ev["triggered_by"] = json.loads(ev["triggered_by"]) if ev["triggered_by"] else []
    ev["vision_regions"] = json.loads(ev["vision_regions"]) if ev["vision_regions"] else []
    if ev["vision_is_anomaly"] is not None:
        ev["vision_is_anomaly"] = bool(ev["vision_is_anomaly"])
    if ev["thermal_is_anomaly"] is not None:
        ev["thermal_is_anomaly"] = bool(ev["thermal_is_anomaly"])
    return ev


def list_events(
    limit: int = 100,
    verdict: str | None = None,
    severity: str | None = None,
    acked: bool | None = None,
) -> list[dict]:
    query = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if verdict is not None:
        query += " AND verdict = ?"
        params.append(verdict)
    if severity is not None:
        query += " AND severity = ?"
        params.append(severity)
    if acked is not None:
        query += " AND acked = ?"
        params.append(1 if acked else 0)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_event(row) for row in rows]


def ack_event(event_id: str, acked_by: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE events SET acked = 1, acked_by = ?, acked_at = ? WHERE id = ? AND acked = 0",
            (acked_by, time.time(), event_id),
        )
        conn.commit()
        return cur.rowcount > 0


# ---------- zones ----------

def list_zones() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM zones").fetchall()
        zones = []
        for row in rows:
            zone = dict(row)
            zone["points"] = json.loads(zone["points"]) if zone["points"] else None
            zone["center"] = json.loads(zone["center"]) if zone["center"] else None
            zones.append(zone)
        return zones


def replace_zones(zones: list[dict]) -> None:
    """PUT 전체 교체. 개별 PATCH로 쪼개지 않는다 (설계서 6-3 근거 참고)."""
    with _connect() as conn:
        conn.execute("DELETE FROM zones")
        conn.executemany(
            """
            INSERT INTO zones (id, name, note, kind, severity, points, center, radius)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    zone["id"],
                    zone["name"],
                    zone.get("note"),
                    zone["kind"],
                    zone["severity"],
                    json.dumps(zone["points"]) if zone.get("points") is not None else None,
                    json.dumps(zone["center"]) if zone.get("center") is not None else None,
                    zone.get("radius"),
                )
                for zone in zones
            ],
        )
        conn.commit()


# ---------- robot_status ----------

def upsert_robot_status(
    robot_id: str,
    x: float | None,
    y: float | None,
    yaw: float | None,
    battery: float | None,
    state: str | None,
    last_seen: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO robot_status (robot_id, x, y, yaw, battery, state, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(robot_id) DO UPDATE SET
                x = excluded.x,
                y = excluded.y,
                yaw = excluded.yaw,
                battery = excluded.battery,
                state = excluded.state,
                last_seen = excluded.last_seen
            """,
            (robot_id, x, y, yaw, battery, state, last_seen),
        )
        conn.commit()


def list_robot_status() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM robot_status").fetchall()
        return [dict(row) for row in rows]
