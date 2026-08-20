# HELM 관제 서버 — 프로젝트 컨텍스트

> 이 파일을 저장소 루트에 `CLAUDE.md`로 두면 클로드 코드가 매 턴 읽는다.
> 짧게 유지할 것. 길어지면 매 요청마다 토큰만 먹는다.

## 프로젝트

산업현장 안전 점검 자율주행 로봇(HELM)의 **관제 서버 + 웹 대시보드**.
로봇(Jetson)이 안전모 미착용·위험구역 침입을 감지하면 사진·위치·시간을 서버로 보내고,
서버는 접속 중인 관리자 PC 화면에 새로고침 없이 알림을 띄운다.

담당: 이나흠 (웹/서버). 로봇 SLAM은 이강건, AI 탐지는 금유찬이 별도 개발.
기간: 2026-08-18 ~ 09-18. **하드웨어 미완성이라 가짜 로봇으로 개발한다.**

## 스택 (변경 금지)

- 백엔드: FastAPI + WebSocket, Python 3.11
- DB: SQLite (WAL 모드). 파일 하나 = `data/helm.db`
- 프론트: **순수 HTML + JS. 빌드 도구 없음.** 지도는 `<canvas>`로 직접 그림
- 배포: EC2 1대에서 uvicorn이 API·WebSocket·정적파일을 전부 서빙

## 절대 규칙

1. **좌표는 월드 좌표(m)로 저장한다.** 픽셀로 저장하면 지도를 다시 뜨는 순간 전부 어긋난다.
2. **이미지 y축은 뒤집혀 있다.** `world_y = origin_y + (height - py) * resolution`.
   `height -`를 빠뜨리면 로봇이 지도 위아래로 뒤집혀 움직인다.
3. **이벤트 `id`는 로봇이 만든 UUID를 그대로 쓴다.** 서버는 `INSERT OR IGNORE`.
   오프라인 버퍼 재전송 시 중복을 이걸로 거른다. 서버가 id를 새로 만들면 이 설계가 깨진다.
4. **SQL은 `app/db.py` 밖으로 새지 않는다.** ORM 도입 금지.
5. **WebSocket URL은 페이지 프로토콜을 따라간다.**
   `location.protocol === 'https:' ? 'wss' : 'ws'`. `ws://` 하드코딩 금지(HTTPS에서 차단됨).
6. **로봇 토큰은 환경변수 `HELM_ROBOT_TOKEN`.** 코드에 박지 않는다.
7. **좌표 변환식은 `app/geometry.py`와 `static/map.js` 두 곳에만 있다.** 항상 같이 고친다.

## 폴더 구조

```
app/     main.py(라우팅·WS) config.py db.py(SQL 전용) geometry.py(좌표·판정) hub.py(broadcast)
static/  index.html(대시보드) zones.html(구역편집기) map.js(지도렌더러·공용) style.css
data/    helm.db, map/(map.png+map.yaml), snapshots/   ← .gitignore
tools/   mock_robot.py (하드웨어 대신 쓰는 가짜 로봇)
```

## 로봇 ↔ 서버 연동 규격 (팀원과의 계약 — 임의 변경 금지)

로봇 → `WebSocket /ws/robot?token=<토큰>`

```json
{"type":"hello","robot_id":"helm-01"}
{"type":"telemetry","robot_id":"helm-01","x":3.42,"y":-1.15,"yaw":1.57,"battery":78.0,"state":"patrolling"}
{"type":"event","event":{"id":"<UUID>","robot_id":"helm-01","type":"no_helmet",
  "severity":"danger","ts":1755648012.3,"x":3.42,"y":-1.15,"confidence":0.91,"image_b64":"<JPEG>"}}
```

서버 응답: `hello`→`{"type":"welcome","zones":[...]}`, `event`→`{"type":"ack","id":...,"duplicate":false}`
**로봇은 ack를 받은 건만 로컬 버퍼에서 지운다.**

서버 → `WebSocket /ws/dashboard`
접속 즉시 `{"type":"snapshot", events/zones/robots/map}` 1회, 이후 변화분만 push:
`event` / `event_acked` / `telemetry` / `robot_online` / `robot_offline` / `zones_updated` / `map_updated`

`state`: `patrolling|idle|estop|charging` · `severity`: `danger|caution|info`
`type`: `no_helmet|zone_intrusion|robot_estop|low_battery`

## 코딩 컨벤션

- 주석은 한국어. **"무엇을"이 아니라 "왜"를 쓴다.** 자명한 코드에 주석 달지 않는다.
- 타입 힌트 사용. `from __future__ import annotations`
- 외부 라이브러리는 `requirements.txt`에 있는 것만. 새로 추가하려면 먼저 물어볼 것
- 색만으로 상태를 구분하지 않는다. 항상 아이콘/글자를 같이 붙인다 (관제 화면 접근성)

## 하지 말 것

- React·Vue·번들러 도입 (배포가 복잡해진다)
- SQLAlchemy 등 ORM 도입
- 시키지 않은 리팩터링. 한 번에 한 단계씩만 한다
- `data/` 안의 파일을 커밋
