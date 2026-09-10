"""하드웨어 대신 쓰는 가짜 로봇. CLAUDE.md 연동 규격을 그대로 지킨다.

진짜 로봇이 오면 이 파일만 ROS2 노드로 교체하면 된다.
서버(app/)는 이 스크립트가 보내는 메시지 포맷만 보고 동작하므로 한 줄도 안 바뀐다.

탐지 방식(PatchCore 기반 이상탐지)이 station 단위 판정으로 바뀌면서, 이 스크립트도
"사각형 왕복 순찰 + 무작위 이벤트"가 아니라 "지정된 지점을 순서대로 방문 -> 정지 ->
촬영/판정 -> 다음 지점" 구조로 바뀌었다.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field

import websockets
from PIL import Image, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mock_robot] %(message)s")
logger = logging.getLogger("mock_robot")

TELEMETRY_HZ = 2.0
PATROL_SPEED_MPS = 0.6
RECONNECT_DELAY_SEC = 3.0
ARRIVAL_TOLERANCE_M = 0.05

IMAGE_W, IMAGE_H = 640, 480
VISION_THRESHOLD = 0.5
THERMAL_THRESHOLD_C = 50.0
DEFAULT_ANOMALY_PROB = 0.35  # 방문마다 이상이 나올 확률 (데모용 — 실제 설비는 훨씬 낮다)
DEFAULT_INSPECTION_PAUSE_SEC = 2.0  # 도착 후 촬영/판정하는 동안의 정지 시간

# 순찰 지점 3곳. make_fake_map.py가 만드는 지도 범위 안에 들어오게 잡았다
STATIONS = [
    {"id": "A", "name": "1번 설비 전면", "x": 1.0, "y": 1.0, "yaw": 0.0},
    {"id": "B", "name": "2번 설비 측면", "x": 8.0, "y": 1.0, "yaw": math.pi / 2},
    {"id": "C", "name": "적재장 통로", "x": 8.0, "y": 6.0, "yaw": math.pi},
]


def make_inspection_jpeg(is_anomaly: bool, regions: list[dict], score: float, max_temp: float) -> bytes:
    """실제로는 PatchCore가 사각형을 그려 보낸다 — 그 자리를 흉내낸 더미 이미지."""
    img = Image.new("RGB", (IMAGE_W, IMAGE_H), color=(45, 48, 54))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, IMAGE_W - 40, IMAGE_H - 40], outline=(90, 96, 105), width=2)  # 설비 윤곽 흉내
    if is_anomaly:
        for region in regions:
            x, y, w, h = region["bbox"]
            draw.rectangle([x, y, x + w, y + h], outline=(255, 92, 92), width=3)
        draw.text((20, 15), f"ANOMALY score={score:.2f} temp={max_temp:.1f}C", fill=(255, 92, 92))
    else:
        draw.text((20, 15), f"NORMAL score={score:.2f} temp={max_temp:.1f}C", fill=(110, 220, 140))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


@dataclass
class RobotSim:
    robot_id: str
    host: str
    port: int
    token: str
    scheme: str = "ws"
    anomaly_prob: float = DEFAULT_ANOMALY_PROB
    inspection_pause_sec: float = DEFAULT_INSPECTION_PAUSE_SEC
    yaw: float = 0.0
    battery: float = 100.0
    state: str = "patrolling"
    connected: bool = False
    ws: object | None = None
    pending: list[dict] = field(default_factory=list)  # 서버 ack를 못 받은 이벤트들 (오프라인 버퍼)
    station_seq: dict = field(default_factory=dict)  # station_id -> 지금까지 방문 횟수

    def __post_init__(self) -> None:
        self.x, self.y = STATIONS[0]["x"], STATIONS[0]["y"]
        self.target_x, self.target_y = self.x, self.y

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}/ws/robot?token={self.token}"

    def set_target(self, x: float, y: float) -> None:
        self.target_x = x
        self.target_y = y

    def at_target(self) -> bool:
        return math.hypot(self.target_x - self.x, self.target_y - self.y) < ARRIVAL_TOLERANCE_M

    def step_toward_target(self, dt: float) -> None:
        dx = self.target_x - self.x
        dy = self.target_y - self.y
        dist = math.hypot(dx, dy)
        step = PATROL_SPEED_MPS * dt
        if dist <= step:
            self.x, self.y = self.target_x, self.target_y
        else:
            self.yaw = math.atan2(dy, dx)
            self.x += (dx / dist) * step
            self.y += (dy / dist) * step
        if self.state == "patrolling":
            self.battery = max(0.0, self.battery - 0.02 * dt)

    def make_inspection_event(self, station: dict) -> dict:
        """지점에 도착해 정지한 뒤 촬영/판정한 결과 하나. verdict는 ANOMALY/NORMAL 둘 다 나올 수 있다."""
        attempt_anomaly = random.random() < self.anomaly_prob
        vision_anomaly = attempt_anomaly and random.random() < 0.7
        thermal_anomaly = attempt_anomaly and random.random() < 0.35
        if attempt_anomaly and not vision_anomaly and not thermal_anomaly:
            vision_anomaly = True  # 이상을 시도했으면 최소 한 채널은 터지게 보정

        if vision_anomaly:
            score = round(VISION_THRESHOLD * random.uniform(1.0, 1.6), 3)
            w, h = random.randint(60, 140), random.randint(60, 140)
            bx = random.randint(20, IMAGE_W - w - 20)
            by = random.randint(20, IMAGE_H - h - 20)
            regions = [{"bbox": [bx, by, w, h], "area_px": w * h}]
        else:
            score = round(VISION_THRESHOLD * random.uniform(0.3, 0.95), 3)
            regions = []

        max_temp = round(
            THERMAL_THRESHOLD_C + random.uniform(2, 20) if thermal_anomaly else random.uniform(28, 45), 1
        )

        verdict = "ANOMALY" if (vision_anomaly or thermal_anomaly) else "NORMAL"
        triggered_by = [name for name, hit in (("vision", vision_anomaly), ("thermal", thermal_anomaly)) if hit]

        seq = self.station_seq.get(station["id"], 0) + 1
        self.station_seq[station["id"]] = seq
        event_id = f"{time.strftime('%Y%m%d-%H%M')}-{station['id']}-{seq:03d}"

        image_bytes = make_inspection_jpeg(verdict == "ANOMALY", regions, score, max_temp)

        return {
            "id": event_id,
            "robot_id": self.robot_id,
            "station_id": station["id"],
            "station_name": station["name"],
            "seq": seq,
            "ts": time.time(),
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "yaw": round(self.yaw, 3),
            "pose_error": {
                "pos_m": round(random.uniform(0.0, 0.05), 4),
                "yaw_rad": round(random.uniform(0.0, 0.03), 4),
            },
            "verdict": verdict,
            "triggered_by": triggered_by,
            "vision": {
                "is_anomaly": vision_anomaly,
                "score": score,
                "threshold": VISION_THRESHOLD,
                "regions": regions,
            },
            "thermal": {
                "is_anomaly": thermal_anomaly,
                "max_temp_c": max_temp,
                "threshold_c": THERMAL_THRESHOLD_C,
            },
            "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        }


async def telemetry_loop(sim: RobotSim) -> None:
    """이동 + telemetry 송신. patrol_loop가 정한 target을 향해 실제로 걷는 것도 여기서 한다."""
    dt = 1 / TELEMETRY_HZ
    while True:
        await asyncio.sleep(dt)
        sim.step_toward_target(dt)
        if not (sim.connected and sim.ws is not None):
            continue
        message = {
            "type": "telemetry",
            "robot_id": sim.robot_id,
            "x": round(sim.x, 3),
            "y": round(sim.y, 3),
            "yaw": round(sim.yaw, 3),
            "battery": round(sim.battery, 1),
            "state": sim.state,
        }
        try:
            await sim.ws.send(json.dumps(message))
        except websockets.ConnectionClosed:
            pass  # 연결 관리는 connection_loop가 담당한다


async def send_event(sim: RobotSim, event: dict) -> None:
    if not (sim.connected and sim.ws is not None):
        return
    try:
        await sim.ws.send(json.dumps({"type": "event", "event": event}))
    except websockets.ConnectionClosed:
        pass  # 재연결 시 backlog로 다시 시도된다


async def patrol_loop(sim: RobotSim) -> None:
    """지점을 순서대로 방문: 이동 -> 도착 -> 정지하고 촬영/판정 -> 다음 지점."""
    station_index = 0
    while True:
        station = STATIONS[station_index]
        sim.state = "patrolling"
        sim.set_target(station["x"], station["y"])
        while not sim.at_target():
            await asyncio.sleep(0.2)

        sim.yaw = station["yaw"]  # 등록된 방향으로 정렬하고 정지
        sim.state = "idle"
        await asyncio.sleep(sim.inspection_pause_sec)

        event = sim.make_inspection_event(station)
        sim.pending.append(event)
        logger.info(
            "%s(%s) 방문#%d — verdict=%s triggered_by=%s (버퍼 %d건)",
            station["name"],
            station["id"],
            event["seq"],
            event["verdict"],
            event["triggered_by"],
            len(sim.pending),
        )
        await send_event(sim, event)

        station_index = (station_index + 1) % len(STATIONS)


async def resend_backlog(sim: RobotSim) -> None:
    """재연결 직후, ack 못 받은 이벤트를 순서대로 몰아서 다시 보낸다."""
    if not sim.pending:
        return
    logger.info("재연결 — 버퍼에 남은 %d건 재전송", len(sim.pending))
    for event in list(sim.pending):
        await send_event(sim, event)
        await asyncio.sleep(0.05)


async def receive_loop(sim: RobotSim, ws) -> None:
    async for raw in ws:
        message = json.loads(raw)
        if message.get("type") == "ack":
            event_id = message.get("id")
            before = len(sim.pending)
            sim.pending[:] = [e for e in sim.pending if e["id"] != event_id]
            if len(sim.pending) < before:
                logger.info(
                    "ack 수신 id=%s duplicate=%s (버퍼 %d건 남음)",
                    event_id,
                    message.get("duplicate"),
                    len(sim.pending),
                )
        elif message.get("type") == "pong":
            logger.debug("pong 수신")


async def connection_loop(sim: RobotSim, args: argparse.Namespace) -> None:
    offline_fired = args.offline is None  # --offline 안 주면 애초에 발동 안 함

    while True:
        try:
            async with websockets.connect(sim.url) as ws:
                sim.ws = ws
                sim.connected = True
                logger.info("서버 접속됨: %s:%s (robot_id=%s)", sim.host, sim.port, sim.robot_id)

                await ws.send(json.dumps({"type": "hello", "robot_id": sim.robot_id}))
                welcome = json.loads(await ws.recv())
                logger.info("welcome 수신 (zones=%d개)", len(welcome.get("zones", [])))

                await resend_backlog(sim)

                if not offline_fired:
                    try:
                        await asyncio.wait_for(receive_loop(sim, ws), timeout=args.offline_after)
                    except asyncio.TimeoutError:
                        pass
                    offline_fired = True
                    logger.info(
                        "=== --offline: %d초간 일부러 연결을 끊는다 (이벤트는 로컬 버퍼에만 쌓임) ===",
                        args.offline,
                    )
                    sim.connected = False
                    await ws.close()
                    await asyncio.sleep(args.offline)
                    logger.info("=== 재연결 시도 ===")
                    continue

                await receive_loop(sim, ws)  # 정상적으로 끊길 때까지 대기
        except (websockets.ConnectionClosed, OSError) as exc:
            sim.connected = False
            sim.ws = None
            logger.warning("연결 끊김(%s) — %.0f초 후 재연결", exc, RECONNECT_DELAY_SEC)
            await asyncio.sleep(RECONNECT_DELAY_SEC)


async def run(args: argparse.Namespace) -> None:
    sim = RobotSim(
        robot_id=args.robot_id,
        host=args.host,
        port=args.port,
        token=args.token,
        scheme=args.scheme,
        anomaly_prob=args.anomaly_prob,
        inspection_pause_sec=args.inspection_pause,
    )
    background = [
        asyncio.create_task(telemetry_loop(sim)),
        asyncio.create_task(patrol_loop(sim)),
    ]
    try:
        await connection_loop(sim, args)
    finally:
        for task in background:
            task.cancel()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HELM 가짜 로봇 (mock_robot)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--scheme", default="ws", choices=["ws", "wss"])
    parser.add_argument("--robot-id", default="helm-01")
    parser.add_argument(
        "--token",
        default=os.environ.get("HELM_ROBOT_TOKEN", ""),
        help="기본값은 환경변수 HELM_ROBOT_TOKEN",
    )
    parser.add_argument(
        "--anomaly-prob",
        type=float,
        default=DEFAULT_ANOMALY_PROB,
        help=f"지점 방문마다 이상이 나올 확률, 기본 {DEFAULT_ANOMALY_PROB}",
    )
    parser.add_argument(
        "--inspection-pause",
        type=float,
        default=DEFAULT_INSPECTION_PAUSE_SEC,
        help=f"도착 후 촬영/판정하는 동안 정지 시간(초), 기본 {DEFAULT_INSPECTION_PAUSE_SEC}",
    )
    parser.add_argument(
        "--offline",
        type=int,
        default=None,
        metavar="N",
        help="접속 후 --offline-after초 뒤 N초간 연결을 끊고 오프라인 버퍼링을 시연한다",
    )
    parser.add_argument(
        "--offline-after",
        type=int,
        default=15,
        help="오프라인 테스트를 시작하기까지 대기 시간(초), 기본 15",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.token:
        raise SystemExit("--token 또는 환경변수 HELM_ROBOT_TOKEN이 필요하다")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("종료")


if __name__ == "__main__":
    main()
