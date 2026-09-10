from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from tools import mock_robot as mock


@pytest.mark.parametrize("probability,verdict", [(0.0, "NORMAL"), (1.0, "ANOMALY")])
def test_station_events(probability: float, verdict: str) -> None:
    sim = mock.RobotSim("helm-01", "localhost", 8000, "test", anomaly_prob=probability)
    for station in mock.STATIONS:
        sim.set_target(station["x"], station["y"])
        sim.step_toward_target(100)
        assert sim.at_target()
        sim.yaw = station["yaw"]
        event = sim.make_inspection_event(station)
        assert event["station_id"] == station["id"]
        assert (event["x"], event["y"]) == (station["x"], station["y"])
        assert event["verdict"] == verdict
        assert isinstance(event["ts"], float)
        assert event["triggered_by"] == [key for key in ["vision", "thermal"] if event[key]["is_anomaly"]]
        assert bool(event["triggered_by"]) == (verdict == "ANOMALY")
        assert not {"type", "confidence", "severity", "images"}.intersection(event)
        with Image.open(io.BytesIO(base64.b64decode(event["image_b64"]))) as image:
            assert image.format == "JPEG" and image.size == (640, 480)
        assert sim.make_inspection_event(station)["seq"] == 2


def test_offline_backlog_and_ack() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        async def __aiter__(self):
            yield json.dumps({"type": "ack", "id": "a", "duplicate": False})

    async def scenario() -> None:
        sim = mock.RobotSim("helm-01", "localhost", 8000, "test")
        socket = Socket()
        sim.ws = socket
        sim.pending = [{"id": "a"}, {"id": "b"}]
        await mock.send_event(sim, sim.pending[0])
        assert socket.sent == []
        sim.connected = True
        await mock.resend_backlog(sim)
        assert [message["event"]["id"] for message in socket.sent] == ["a", "b"]
        assert len(sim.pending) == 2
        await mock.receive_loop(sim, socket)
        assert sim.pending == [{"id": "b"}]

    asyncio.run(scenario())
