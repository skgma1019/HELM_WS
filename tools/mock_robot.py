"""하드웨어 대신 쓰는 가짜 로봇. 설계서 6-1 규격을 그대로 지킨다.

진짜 로봇이 오면 이 파일만 강건님의 ROS2 노드로 교체하면 된다.
서버(app/)는 이 스크립트가 보내는 메시지 포맷만 보고 동작하므로 한 줄도 안 바뀐다.
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
import uuid
from dataclasses import dataclass, field

import websockets
from PIL import Image, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mock_robot] %(message)s")
logger = logging.getLogger("mock_robot")

TELEMETRY_HZ = 2.0
PATROL_SPEED_MPS = 0.6
EVENT_CHECK_INTERVAL_SEC = 1.0
NO_HELMET_PROB = 0.05  # 위 간격마다 굴리는 확률
RECONNECT_DELAY_SEC = 3.0

# 순찰 경로: 사각형 왕복. make_fake_map.py가 만드는 지도 범위 안에 들어오게 잡았다
PATROL_PATH = [
    (1.0, 1.0),
    (8.0, 1.0),
    (8.0, 6.0),
    (1.0, 6.0),
]


def make_dummy_jpeg() -> bytes:
    """진짜 카메라가 없으니 그 자리에서 안전모 미착용처럼 보이는 더미 이미지를 만든다."""
    img = Image.new("RGB", (640, 480), color=(40, 40, 40))
    draw = ImageDraw.Draw(img)
    draw.rectangle([180, 120, 460, 360], outline=(255, 0, 0), width=4)
    draw.ellipse([280, 140, 360, 220], outline=(255, 200, 0), width=3)
    draw.text((190, 370), f"MOCK no_helmet {time.strftime('%H:%M:%S')}", fill=(255, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


@dataclass
class RobotSim:
    robot_id: str
    host: str
    port: int
    token: str
    scheme: str = "ws"
    path_index: int = 0
    yaw: float = 0.0
    battery: float = 100.0
    connected: bool = False
    ws: object | None = None
    pending: list[dict] = field(default_factory=list)  # 서버 ack를 못 받은 이벤트들 (오프라인 버퍼)

    def __post_init__(self) -> None:
        self.x, self.y = PATROL_PATH[0]

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}/ws/robot?token={self.token}"

    def step_position(self, dt: float) -> None:
        target = PATROL_PATH[(self.path_index + 1) % len(PATROL_PATH)]
        dx = target[0] - self.x
        dy = target[1] - self.y
        dist = math.hypot(dx, dy)
        step = PATROL_SPEED_MPS * dt
        if dist <= step:
            self.x, self.y = target
            self.path_index = (self.path_index + 1) % len(PATROL_PATH)
        else:
            self.yaw = math.atan2(dy, dx)
            self.x += (dx / dist) * step
            self.y += (dy / dist) * step
        # 순찰 중엔 배터리가 천천히 줄어든다 — telemetry 필드를 그럴싸하게 채우는 용도
        self.battery = max(0.0, self.battery - 0.02 * dt)

    def make_event(self) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "robot_id": self.robot_id,
            "type": "no_helmet",
            "severity": "danger",
            "ts": time.time(),
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "confidence": round(random.uniform(0.7, 0.98), 2),
            "image_b64": base64.b64encode(make_dummy_jpeg()).decode("ascii"),
        }


async def telemetry_loop(sim: RobotSim) -> None:
    dt = 1 / TELEMETRY_HZ
    while True:
        await asyncio.sleep(dt)
        sim.step_position(dt)
        if not (sim.connected and sim.ws is not None):
            continue
        message = {
            "type": "telemetry",
            "robot_id": sim.robot_id,
            "x": round(sim.x, 3),
            "y": round(sim.y, 3),
            "yaw": round(sim.yaw, 3),
            "battery": round(sim.battery, 1),
            "state": "patrolling",
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


async def event_loop(sim: RobotSim) -> None:
    while True:
        await asyncio.sleep(EVENT_CHECK_INTERVAL_SEC)
        if random.random() >= NO_HELMET_PROB:
            continue
        event = sim.make_event()
        sim.pending.append(event)
        logger.info("no_helmet 이벤트 생성 id=%s (버퍼 %d건)", event["id"][:8], len(sim.pending))
        await send_event(sim, event)


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
                    str(event_id)[:8],
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
    )
    background = [
        asyncio.create_task(telemetry_loop(sim)),
        asyncio.create_task(event_loop(sim)),
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
