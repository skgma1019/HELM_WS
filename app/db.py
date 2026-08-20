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
                type TEXT NOT NULL,
                severity TEXT NOT NULL,
                ts REAL NOT NULL,
                received_at REAL NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                zone_id TEXT,
                zone_name TEXT,
                confidence REAL,
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

def insert_event(event: dict) -> bool:
    """event["id"]는 로봇이 만든 UUID. 이미 있으면 무시하고 False를 반환한다."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO events
                (id, robot_id, type, severity, ts, received_at, x, y,
                 zone_id, zone_name, confidence, image_path, acked, acked_by, acked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                event["id"],
                event["robot_id"],
                event["type"],
                event["severity"],
                event["ts"],
                event["received_at"],
                event["x"],
                event["y"],
                event.get("zone_id"),
                event.get("zone_name"),
                event.get("confidence"),
                event.get("image_path"),
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def list_events(
    limit: int = 100,
    type_: str | None = None,
    severity: str | None = None,
    acked: bool | None = None,
) -> list[dict]:
    query = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if type_ is not None:
        query += " AND type = ?"
        params.append(type_)
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
        return [dict(row) for row in rows]


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
