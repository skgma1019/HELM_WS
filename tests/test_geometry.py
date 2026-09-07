from __future__ import annotations

import pytest

from app import geometry as g


@pytest.mark.parametrize("pixel", [(0, 0), (100, 200), (42.5, 19.25)])
def test_coordinates(pixel: tuple[float, float]) -> None:
    meta = g.MapMeta("map.png", 0.05, -2, -3, 100, 200)
    assert g.world_to_pixel(*g.pixel_to_world(*pixel, meta), meta) == pytest.approx(pixel)
    assert g.pixel_to_world(0, 0, meta) == (-2, 7)
    assert g.pixel_to_world(0, 200, meta) == (-2, -3)


def test_zone_shapes_and_priority() -> None:
    points = [[0, 0], [2, 0], [2, 2], [0, 2]]
    assert g.point_in_polygon(1, 1, points)
    assert not g.point_in_polygon(3, 1, points)
    assert not g.point_in_polygon(1, 1, points[:2])
    assert g.point_in_circle(1, 0, [0, 0], 1)
    assert not g.point_in_circle(1.001, 0, [0, 0], 1)
    zones = [{"kind": "polygon", "points": points, "severity": severity} for severity in ["safe", "caution", "danger"]]
    assert [z["severity"] for z in g.zones_containing(1, 1, zones)] == ["danger", "caution", "safe"]
