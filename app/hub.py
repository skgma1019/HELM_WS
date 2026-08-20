"""대시보드 WebSocket 연결 관리와 broadcast."""
from __future__ import annotations

import logging

from fastapi import WebSocket

logger = logging.getLogger("helm.hub")


class DashboardHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        # 연결이 끊긴 소켓에 보내다 실패해도 나머지 broadcast는 계속되어야 한다
        dead: list[WebSocket] = []
        for connection in list(self._connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


hub = DashboardHub()
