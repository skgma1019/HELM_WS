from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# override=False가 기본값 — 이미 설정된 실제 환경변수가 .env 값보다 우선해야 한다 (배포 환경 안전장치)
load_dotenv()

# 저장소 루트 (app/ 의 부모)
BASE_DIR = Path(__file__).resolve().parent.parent

# HELM_DATA_DIR로 덮어쓸 수 있게 한다 — 배포 환경(EC2)에서 데이터 위치를 바꿀 수 있어야 함
DATA_DIR = Path(os.environ.get("HELM_DATA_DIR", str(BASE_DIR / "data")))

DB_PATH = DATA_DIR / "helm.db"
MAP_DIR = DATA_DIR / "map"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SAMPLES_DIR = DATA_DIR / "samples"  # 테스트 콘솔용 샘플 사진 (개발용, HELM_DEV=1일 때만 서빙)

# 로봇 인증 토큰. 코드에 박지 않는다 (규칙 6) — 미설정 시 /ws/robot은 전부 거부한다
ROBOT_TOKEN = os.environ.get("HELM_ROBOT_TOKEN")

# 개발용 테스트 콘솔(static/test.html) 서빙 여부. EC2 배포본엔 절대 세우면 안 된다
DEV_MODE = os.environ.get("HELM_DEV") == "1"
