"""강건의 SLAM 결과가 나오기 전까지 쓸 가짜 지도(map.png + map.yaml)를 만든다.

ROS map_server 규격(resolution, origin, negate, occupied_thresh, free_thresh)을 그대로 따른다.
나중에 진짜 지도로 교체할 때 이 규격만 맞으면 서버/프론트는 아무것도 안 고쳐도 된다.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = BASE_DIR / "data" / "map"

FREE = 255  # 흰색 = 이동 가능
WALL = 0  # 검은색 = 벽/장애물


def draw_floor_plan(width_px: int, height_px: int) -> Image.Image:
    img = Image.new("L", (width_px, height_px), color=FREE)
    draw = ImageDraw.Draw(img)

    # 외곽 벽
    border = 3
    draw.rectangle([0, 0, width_px - 1, height_px - 1], outline=WALL, width=border)

    # 두 구역으로 나누는 내벽 + 출입구(door_gap)
    wall_x = width_px // 2
    door_top = height_px // 2 - 15
    door_bottom = height_px // 2 + 15
    draw.line([(wall_x, border), (wall_x, door_top)], fill=WALL, width=border)
    draw.line([(wall_x, door_bottom), (wall_x, height_px - border)], fill=WALL, width=border)

    # 장애물(기계/적재대) 몇 개
    obstacles = [
        (20, 20, 60, 60),
        (width_px - 70, 20, width_px - 20, 55),
        (20, height_px - 55, 70, height_px - 20),
        (wall_x + 30, height_px - 70, wall_x + 90, height_px - 25),
        (wall_x - 90, 40, wall_x - 30, 90),
    ]
    for x0, y0, x1, y1 in obstacles:
        draw.rectangle([x0, y0, x1, y1], fill=WALL)

    return img


def write_yaml(out_dir: Path, image_name: str, resolution: float, origin: tuple[float, float, float]) -> None:
    content = (
        f"image: {image_name}\n"
        f"resolution: {resolution}\n"
        f"origin: [{origin[0]}, {origin[1]}, {origin[2]}]\n"
        f"negate: 0\n"
        f"occupied_thresh: 0.65\n"
        f"free_thresh: 0.196\n"
    )
    (out_dir / "map.yaml").write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="가짜 SLAM 지도(map.png + map.yaml) 생성")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--width-px", type=int, default=240)
    parser.add_argument("--height-px", type=int, default=200)
    parser.add_argument("--resolution", type=float, default=0.05, help="m/pixel")
    parser.add_argument("--origin-x", type=float, default=-1.0)
    parser.add_argument("--origin-y", type=float, default=-1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    img = draw_floor_plan(args.width_px, args.height_px)
    image_path = out_dir / "map.png"
    img.save(image_path, format="PNG")

    write_yaml(out_dir, "map.png", args.resolution, (args.origin_x, args.origin_y, 0.0))

    world_w = args.width_px * args.resolution
    world_h = args.height_px * args.resolution
    print(f"지도 생성 완료: {image_path}")
    print(f"map.yaml: resolution={args.resolution}, origin=({args.origin_x}, {args.origin_y}, 0.0)")
    print(
        f"월드 좌표 범위: x=[{args.origin_x:.2f}, {args.origin_x + world_w:.2f}], "
        f"y=[{args.origin_y:.2f}, {args.origin_y + world_h:.2f}] (m)"
    )


if __name__ == "__main__":
    main()
