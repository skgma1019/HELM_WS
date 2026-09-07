from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

import yaml
from fastapi import (
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import config, db, geometry
from app.hub import hub

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("helm.main")

# 감지(ts)~수신(received_at) 간격이 이보다 크면 오프라인 버퍼 재전송으로 간주한다
EVENT_DELAY_THRESHOLD_SEC = 3.0

app = FastAPI(title="HELM 관제 서버")

# StaticFiles는 마운트 시점에 디렉터리가 있어야 한다 — on_startup보다 먼저 만들어둔다
config.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/snapshots", StaticFiles(directory=str(config.SNAPSHOTS_DIR)), name="snapshots")


def _mask_token(token: str | None) -> str:
    if not token:
        return "(없음)"
    return f"{token[:4]}***** len={len(token)}"


@app.on_event("startup")
async def on_startup() -> None:
    db.init_db()
    logger.info("설정 요약 — robot token: %s", _mask_token(config.ROBOT_TOKEN))
    logger.info("설정 요약 — dev mode: %s", "on" if config.DEV_MODE else "off")
    logger.info("설정 요약 — data dir: %s", config.DATA_DIR.resolve())


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


def _compute_severity(event: dict) -> str:
    """CLAUDE.md 연동 규격의 severity 산정 규칙. 로봇은 severity를 보내지 않는다 — 서버가 계산한다.

    구역 기반 상향(danger 구역 안이면 danger)은 이 함수 밖(_process_event)에서 따로 적용한다.
    임계값은 실측 전 임시값이라 config.py에서 읽는다 (하드코딩 금지).
    """
    if event.get("verdict") != "ANOMALY":
        return "info"

    vision = event.get("vision") or {}
    thermal = event.get("thermal") or {}

    score = vision.get("score")
    threshold = vision.get("threshold")
    ratio = (score / threshold) if (score is not None and threshold) else 0.0

    max_temp = thermal.get("max_temp_c")
    temp_threshold = thermal.get("threshold_c")
    overshoot = (max_temp - temp_threshold) if (max_temp is not None and temp_threshold is not None) else None

    both_anomaly = bool(vision.get("is_anomaly")) and bool(thermal.get("is_anomaly"))

    if ratio >= config.SEVERITY_RATIO_DANGER:
        return "danger"
    if overshoot is not None and overshoot >= config.THERMAL_OVERSHOOT_DANGER_C:
        return "danger"
    if both_anomaly:
        return "danger"
    # ANOMALY인 이상 위 조건에 안 걸려도(예: thermal만 threshold를 살짝 넘김) 최소 caution은 준다
    return "caution"


async def _process_event(event: dict) -> dict:
    """이벤트 하나를 저장하고, 새 이벤트면 대시보드로 broadcast한다.

    WebSocket 경로(_handle_event)와 REST 폴백(POST /api/events)이 이 함수를 같이 쓴다.
    """
    event_id = event.get("id")
    if not event_id:
        raise ValueError("event.id가 없다")

    ts = event.get("ts")
    if ts is None:
        raise ValueError("event.ts가 없다")
    ts = float(ts)  # 로봇은 epoch 숫자로 보낸다 (CLAUDE.md 연동 규격) — 조용히 넘어가지 않는다

    image_path = None
    image_b64 = event.get("image_b64")
    if image_b64:
        image_path = _save_snapshot(event_id, image_b64)

    received_at = time.time()
    x = event.get("x")
    y = event.get("y")

    severity = _compute_severity(event)

    zone_id = None
    zone_name = None
    if x is not None and y is not None:
        matched = geometry.zones_containing(x, y, db.list_zones())
        if matched:
            zone_id = matched[0]["id"]
            zone_name = matched[0]["name"]
            if matched[0]["severity"] == "danger":
                severity = "danger"  # danger 구역 안이면 무조건 danger로 올린다 (내리진 않음)

    vision = event.get("vision") or {}
    thermal = event.get("thermal") or {}
    pose_error = event.get("pose_error") or {}

    record = {
        "id": event_id,
        "robot_id": event.get("robot_id"),
        "station_id": event.get("station_id"),
        "station_name": event.get("station_name"),
        "seq": event.get("seq"),
        "severity": severity,
        "verdict": event.get("verdict"),
        "ts": ts,
        "received_at": received_at,
        "x": x,
        "y": y,
        "yaw": event.get("yaw"),
        "pose_pos_error_m": pose_error.get("pos_m"),
        "pose_yaw_error_rad": pose_error.get("yaw_rad"),
        "triggered_by": event.get("triggered_by") or [],
        "vision_is_anomaly": vision.get("is_anomaly"),
        "vision_score": vision.get("score"),
        "vision_threshold": vision.get("threshold"),
        "vision_regions": vision.get("regions") or [],
        "thermal_is_anomaly": thermal.get("is_anomaly"),
        "thermal_max_temp_c": thermal.get("max_temp_c"),
        "thermal_threshold_c": thermal.get("threshold_c"),
        "zone_id": zone_id,
        "zone_name": zone_name,
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


def _check_robot_token(received: str | None) -> bool:
    """토큰이 맞으면 True. 틀리면 원인 추적용으로 마스킹된 값을 로그에 남기고 False."""
    if config.ROBOT_TOKEN and received == config.ROBOT_TOKEN:
        return True
    logger.warning(
        "로봇 토큰 불일치 — 받은 토큰 %s / 기대값 %s",
        _mask_token(received),
        _mask_token(config.ROBOT_TOKEN),
    )
    return False


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def dashboard_page() -> FileResponse:
    return FileResponse(config.BASE_DIR / "static" / "index.html")


@app.get("/style.css")
async def dashboard_style() -> FileResponse:
    return FileResponse(config.BASE_DIR / "static" / "style.css")


@app.get("/map.js")
async def dashboard_map_js() -> FileResponse:
    return FileResponse(config.BASE_DIR / "static" / "map.js")


MAP_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


@app.get("/api/map")
async def get_map() -> dict:
    meta = geometry.load_map_meta(config.MAP_DIR)
    return {"map": asdict(meta) if meta else None}


@app.get("/api/map/image")
async def get_map_image() -> FileResponse:
    meta = geometry.load_map_meta(config.MAP_DIR)
    if meta is None:
        raise HTTPException(status_code=404, detail="map not found")
    return FileResponse(config.MAP_DIR / meta.image)


@app.post("/api/map")
async def post_map(
    image: UploadFile = File(...),
    yaml_file: UploadFile = File(..., alias="yaml"),
) -> dict:
    """지도 이미지 + map.yaml 업로드 (SLAM 결과 갈아끼우기용). 이미지 파일명은 서버가 정한다."""
    ext = Path(image.filename or "").suffix.lower()
    if ext not in MAP_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="지원하지 않는 이미지 형식 (png/jpg만 가능)")

    try:
        yaml_data = yaml.safe_load(await yaml_file.read()) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"map.yaml 파싱 실패: {exc}")

    config.MAP_DIR.mkdir(parents=True, exist_ok=True)
    # 갈아끼우는 것이므로 확장자가 다른 이전 지도 이미지가 남지 않게 정리한다
    for old in config.MAP_DIR.glob("map.*"):
        if old.suffix.lower() in MAP_IMAGE_EXTENSIONS:
            old.unlink(missing_ok=True)

    image_filename = f"map{ext}"
    (config.MAP_DIR / image_filename).write_bytes(await image.read())

    # 업로드된 yaml이 다른 파일명을 적어놨더라도, 실제로 저장한 이름으로 맞춘다
    yaml_data["image"] = image_filename
    with open(config.MAP_DIR / "map.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(yaml_data, f, allow_unicode=True)

    meta = geometry.load_map_meta(config.MAP_DIR)
    if meta is None:
        raise HTTPException(status_code=500, detail="지도 저장 후 읽기 실패")

    meta_dict = asdict(meta)
    await hub.broadcast({"type": "map_updated", "map": meta_dict})
    return {"map": meta_dict}


@app.get("/api/events")
async def get_events(
    limit: int = 100,
    verdict: str | None = None,
    acked: bool | None = None,
) -> list[dict]:
    return db.list_events(limit=limit, verdict=verdict, acked=acked)


@app.post("/api/events/{event_id}/ack")
async def post_ack_event(event_id: str) -> dict:
    acked_by = "dashboard"  # 관리자 로그인이 아직 없어 고정값을 쓴다
    if not db.ack_event(event_id, acked_by):
        raise HTTPException(status_code=404, detail="event not found or already acked")
    await hub.broadcast({"type": "event_acked", "id": event_id, "acked_by": acked_by})
    return {"ok": True}


@app.post("/api/events")
async def post_events(
    payload: dict | list[dict],
    x_robot_token: str | None = Header(default=None, alias="X-Robot-Token"),
):
    """WebSocket이 막힌 환경용 폴백 (설계서 6-1). 배열로 보내면 버퍼 일괄 재전송으로 처리한다."""
    if not _check_robot_token(x_robot_token):
        raise HTTPException(status_code=401, detail="invalid robot token")
    if isinstance(payload, list):
        return [await _process_event(event) for event in payload]
    return await _process_event(payload)


@app.post("/api/robot/status")
async def post_robot_status(
    payload: dict,
    x_robot_token: str | None = Header(default=None, alias="X-Robot-Token"),
):
    """이벤트 없이 로봇 상태만 갱신한다 (개발용 테스트 콘솔의 '정상' 버튼)."""
    if not _check_robot_token(x_robot_token):
        raise HTTPException(status_code=401, detail="invalid robot token")
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
    if not _check_robot_token(token):
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
    map_meta = geometry.load_map_meta(config.MAP_DIR)
    await websocket.send_json(
        {
            "type": "snapshot",
            "events": db.list_events(),
            "zones": db.list_zones(),
            "robots": db.list_robot_status(),
            "map": asdict(map_meta) if map_meta else None,
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