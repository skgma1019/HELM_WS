from __future__ import annotations

import base64
import json
import logging
import time

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse

from app import config, db
from app.hub import hub

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("helm.main")

# 감지(ts)~수신(received_at) 간격이 이보다 크면 오프라인 버퍼 재전송으로 간주한다
EVENT_DELAY_THRESHOLD_SEC = 3.0

app = FastAPI(title="HELM 관제 서버")


@app.on_event("startup")
async def on_startup() -> None:
    config.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()


def _save_snapshot(event_id: str, image_b64: str) -> str | None:
    try:
        image_bytes = base64.b64decode(image_b64)
    except (ValueError, TypeError):
        logger.warning("이벤트 %s: 이미지 base64 디코딩 실패", event_id)
        return None
    path = config.SNAPSHOTS_DIR / f"{event_id}.jpg"
    path.write_bytes(image_bytes)
    return f"/snapshots/{event_id}.jpg"


async def _handle_hello(websocket: WebSocket, message: dict) -> None:
    await websocket.send_json(
        {
            "type": "welcome",
            "server_time": time.time(),
            "zones": db.list_zones(),
        }
    )


async def _handle_telemetry(message: dict) -> None:
    robot_id = message.get("robot_id")
    if not robot_id:
        logger.warning("telemetry: robot_id 없음, 무시")
        return

    now = time.time()
    db.upsert_robot_status(
        robot_id=robot_id,
        x=message.get("x"),
        y=message.get("y"),
        yaw=message.get("yaw"),
        battery=message.get("battery"),
        state=message.get("state"),
        last_seen=now,
    )
    await hub.broadcast(
        {
            "type": "telemetry",
            "robot_id": robot_id,
            "x": message.get("x"),
            "y": message.get("y"),
            "yaw": message.get("yaw"),
            "battery": message.get("battery"),
            "state": message.get("state"),
        }
    )


async def _process_event(event: dict) -> dict:
    """이벤트 하나를 저장하고, 새 이벤트면 대시보드로 broadcast한다.

    WebSocket 경로(_handle_event)와 REST 폴백(POST /api/events)이 이 함수를 같이 쓴다.
    """
    event_id = event.get("id")
    if not event_id:
        raise ValueError("event.id가 없다")

    image_path = None
    image_b64 = event.get("image_b64")
    if image_b64:
        image_path = _save_snapshot(event_id, image_b64)

    received_at = time.time()
    ts = event.get("ts", received_at)
    record = {
        "id": event_id,
        "robot_id": event.get("robot_id"),
        "type": event.get("type"),
        "severity": event.get("severity"),
        "ts": ts,
        "received_at": received_at,
        "x": event.get("x"),
        "y": event.get("y"),
        # 구역 판정은 geometry.py 단계에서 채운다 — 지금은 미판정
        "zone_id": None,
        "zone_name": None,
        "confidence": event.get("confidence"),
        "image_path": image_path,
    }
    is_new = db.insert_event(record)

    if is_new:
        delayed = (received_at - ts) > EVENT_DELAY_THRESHOLD_SEC
        await hub.broadcast({"type": "event", "event": record, "delayed": delayed})

    return {"type": "ack", "id": event_id, "duplicate": not is_new}


async def _handle_event(websocket: WebSocket, message: dict) -> None:
    event = message.get("event") or {}
    if not event.get("id"):
        logger.warning("event: id 없음, 무시")
        return
    ack = await _process_event(event)
    await websocket.send_json(ack)


def _check_robot_token(x_robot_token: str | None) -> None:
    if not config.ROBOT_TOKEN or x_robot_token != config.ROBOT_TOKEN:
        raise HTTPException(status_code=401, detail="invalid robot token")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/events")
async def post_events(
    payload: dict | list[dict],
    x_robot_token: str | None = Header(default=None, alias="X-Robot-Token"),
):
    """WebSocket이 막힌 환경용 폴백 (설계서 6-1). 배열로 보내면 버퍼 일괄 재전송으로 처리한다."""
    _check_robot_token(x_robot_token)
    if isinstance(payload, list):
        return [await _process_event(event) for event in payload]
    return await _process_event(payload)


@app.post("/api/robot/status")
async def post_robot_status(
    payload: dict,
    x_robot_token: str | None = Header(default=None, alias="X-Robot-Token"),
):
    """이벤트 없이 로봇 상태만 갱신한다 (개발용 테스트 콘솔의 '정상' 버튼)."""
    _check_robot_token(x_robot_token)
    if not payload.get("robot_id"):
        raise HTTPException(status_code=400, detail="robot_id가 필요하다")
    await _handle_telemetry(payload)
    return {"ok": True}


def _require_dev_mode() -> None:
    if not config.DEV_MODE:
        raise HTTPException(status_code=404)


@app.get("/test.html")
async def dev_test_console() -> FileResponse:
    """개발용 테스트 콘솔. HELM_DEV=1일 때만 서빙 — EC2 배포본에선 막혀 있어야 한다."""
    _require_dev_mode()
    return FileResponse(config.BASE_DIR / "static" / "test.html")


SAMPLE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


@app.get("/api/dev/samples")
async def list_dev_samples() -> dict:
    """테스트 콘솔의 사진 첨부 드롭다운용 목록. data/samples/ 안의 이미지 파일명만 내려준다."""
    _require_dev_mode()
    if not config.SAMPLES_DIR.is_dir():
        return {"samples": []}
    names = sorted(
        p.name
        for p in config.SAMPLES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SAMPLE_EXTENSIONS
    )
    return {"samples": names}


@app.get("/api/dev/samples/{filename}")
async def get_dev_sample(filename: str) -> FileResponse:
    _require_dev_mode()
    path = config.SAMPLES_DIR / filename
    if path.suffix.lower() not in SAMPLE_EXTENSIONS or not path.is_file():
        raise HTTPException(status_code=404)
    if path.resolve().parent != config.SAMPLES_DIR.resolve():
        raise HTTPException(status_code=400, detail="invalid filename")
    return FileResponse(path)


@app.websocket("/ws/robot")
async def ws_robot(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    if not config.ROBOT_TOKEN or token != config.ROBOT_TOKEN:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("robot ws: JSON 파싱 실패: %r", raw)
                continue

            msg_type = message.get("type")
            if msg_type == "hello":
                await _handle_hello(websocket, message)
            elif msg_type == "telemetry":
                await _handle_telemetry(message)
            elif msg_type == "event":
                await _handle_event(websocket, message)
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong", "server_time": time.time()})
            else:
                logger.warning("robot ws: 알 수 없는 type: %s", msg_type)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket) -> None:
    await hub.connect(websocket)
    await websocket.send_json(
        {
            "type": "snapshot",
            "events": db.list_events(),
            "zones": db.list_zones(),
            "robots": db.list_robot_status(),
            "map": None,
        }
    )
    try:
        while True:
            # 대시보드는 서버로 딱히 보낼 게 없다. 연결 유지 + 끊김 감지용
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(websocket)