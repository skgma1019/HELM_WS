# HELM 프로젝트 — 작업 인계 문서 (Codex용)

> 이 문서는 다른 AI 코딩 도구(Codex)가 이 저장소의 맥락을 빠르게 파악하도록 작성했다.
> **먼저 `CLAUDE.md`를 읽어라.** 거기 있는 규칙은 여기서 반복하지 않고 요약만 한다.
> 이 문서는 "지금 정확히 어디까지 됐고 무엇이 남았는지"에 집중한다.

## 1. 프로젝트가 뭔가

산업현장 안전 점검 자율순찰 로봇(HELM)의 관제 서버 + 웹 대시보드.
로봇이 지정된 순찰 지점(station)에서 정지해 촬영하고, PatchCore 기반
비지도 이상탐지로 "평소와 다른지"만 판정해서 서버로 보낸다. 서버는 접속 중인
관리자 대시보드에 새로고침 없이 알림을 띄운다.

**하드웨어가 아직 없다.** `tools/mock_robot.py`가 로봇 역할을 대신한다.

### 탐지 방식 — 절대 오해하면 안 되는 것

- **이상의 "종류"를 분류하지 않는다.** YOLO 객체탐지, "안전모 미착용" 같은
  이름표 판정은 전부 폐기된 전제다. 다시 꺼내지 마라.
- 순찰 지점(station)마다 정상 상태를 학습한 Memory Bank(PatchCore/anomalib)가 있고,
  로봇은 그 지점에 정지해 촬영한 사진이 "학습된 정상과 다른지"만 판정한다.
- vision(이미지)과 thermal(열화상 MLX90640) 두 채널이 각각 독립 판정하고,
  **둘 중 하나라도 이상이면 verdict=ANOMALY** (OR 결합).
- 화면은 "무엇이 감지됐는가"가 아니라 **"어디가(어느 station이) 평소와 다른가"**를 보여준다.
- `severity`(danger/caution/info)는 **로봇이 안 보낸다. 서버가 계산한다.**
  자세한 규칙은 CLAUDE.md의 "severity 산정" 절 그대로다 — `app/main.py`의
  `_compute_severity()`가 구현이다.

### 로봇 ↔ 서버 연동 규격 (계약 — `CLAUDE.md`에 원본 있음)

```json
{"type":"event","event":{
  "id":"20260914-2147-A-001",
  "robot_id":"helm-01",
  "station_id":"A", "station_name":"1번 설비 전면", "seq":1,
  "ts":1755648012.345,
  "x":1.26, "y":0.35, "yaw":1.55,
  "pose_error":{"pos_m":0.028,"yaw_rad":0.021},
  "verdict":"ANOMALY",
  "triggered_by":["vision"],
  "vision":{"is_anomaly":true,"score":0.87,"threshold":0.62,
            "regions":[{"bbox":[412,233,96,140],"area_px":13440}]},
  "thermal":{"is_anomaly":false,"max_temp_c":43.2,"threshold_c":50.0},
  "image_b64":"<사각형이 그려진 JPEG 하나>"
}}
```

- `id`는 로봇이 만든다 (UUID든 구조화 문자열이든 무관). 서버는 `INSERT OR IGNORE`로만 중복을 거른다.
- `ts`는 **UNIX epoch 숫자**다 (ISO8601 문자열 아님 — 이건 한 번 헷갈렸다가 사용자에게 확인받은 것).
- 사진은 **annotated 한 장만** base64로 온다. 원본(raw)은 로봇 로컬에만 남고 서버로 안 온다.
  `images.raw`/`images.annotated` 같은 필드는 **없다** — `image_b64` 하나뿐.
- `type`, `confidence` 필드는 **더 이상 오지 않는다** (DB 컬럼도 삭제함).

## 2. 지금까지 한 일 (커밋 순서대로)

브랜치: `feat/anomaly-detection-schema` (main에서 분기, 아직 PR/머지 안 함)

```
1e71890 docs: PatchCore 기반 이상탐지로 연동 규격 갱신          (CLAUDE.md, 설계서 — 사용자가 직접 작성)
3fb77fb schema: events 테이블을 이상탐지 페이로드에 맞게 교체    (app/db.py)
958625e feat(server): 이상탐지 페이로드 수신 + severity 서버 산정 (app/main.py, app/config.py, .env.example)
a980e8f feat(dev-tools): mock_robot/test.html을 새 페이로드로 교체 (tools/mock_robot.py, static/test.html)
```

이 4개 커밋 이전에는(그 아래 `main` 히스토리) **완전히 다른 이상탐지 방식**(YOLO
객체탐지, "안전모 미착용" 같은 고정 이름표)을 전제로 서버·대시보드·지도까지
전부 만들어져 있었다. 그 구현(서버 뼈대, 지도 렌더링, 대시보드 껍데기)은
살아있고 여전히 유효하다 — **오직 이벤트 스키마와 그 스키마를 참조하는 코드만
새 탐지 방식에 맞게 고치는 중**이다.

### 2-1. `app/db.py` — 완료

`events` 테이블 재정의. `type`/`confidence` 컬럼 삭제. 아래 컬럼 추가:

```
station_id, station_name, seq, verdict,
yaw, pose_pos_error_m, pose_yaw_error_rad,
triggered_by (JSON 텍스트), vision_is_anomaly, vision_score, vision_threshold,
vision_regions (JSON 텍스트), thermal_is_anomaly, thermal_max_temp_c, thermal_threshold_c
```

`severity` 컬럼은 그대로 있다 (값 danger/caution/info, 서버가 계산해서 넣음).
`zones`, `robot_status` 테이블은 스키마 무변경. `list_events()` 필터가
`type` → `verdict`로 바뀜.

**기존 `data/helm.db`, `data/snapshots/*`는 사용자 승인받고 삭제했다.**
개발 단계라 마이그레이션 없이 스키마만 새로 만들면 된다 (`init_db()`가 처음부터 생성).

### 2-2. `app/main.py`, `app/config.py`, `.env.example` — 완료

- `_compute_severity(event)`: verdict/vision/thermal로 danger/caution/info 계산.
  임계값은 `config.py`의 `SEVERITY_RATIO_CAUTION`(1.0), `SEVERITY_RATIO_DANGER`(1.3),
  `THERMAL_OVERSHOOT_DANGER_C`(10.0) — 전부 `HELM_*` 환경변수로 덮어쓸 수 있음.
  **경계값 처리**: verdict가 ANOMALY인데 아무 danger 조건도 안 걸리면 바닥값으로
  `caution`을 준다 (CLAUDE.md 규칙에 명시 안 된 경계 케이스라 이렇게 해석했다 — 사용자에게
  보고했고 이견 없었음).
- `_process_event()`가 새 페이로드(중첩된 `pose_error`/`vision`/`thermal`)를 평평하게
  펴서 DB record로 만든다. 구역 기반 severity 상향(危 구역 안이면 danger로 상향)은
  `_compute_severity()` 다음 단계로 그대로 유지.
- `ts`는 `float(event["ts"])`로 그대로 받는다. 없으면 `ValueError` (조용히 안 넘어감).
- `GET /api/events` 쿼리 파라미터가 `type` → `verdict`로 바뀜.
- 이 파일에 있던 지도(`/api/map*`) 관련 라우트, `/ws/robot`, `/ws/dashboard`,
  `/test.html` 개발 콘솔, `/api/dev/samples*` 등은 **이번 작업과 무관하게 그대로 있다.**

### 2-3. `tools/mock_robot.py` — 완료

완전히 새로 짬. 순찰 지점 3곳(`STATIONS` 상수, id A/B/C)을 순서대로 방문 →
도착해서 정지(`state="idle"`) → 촬영/판정(`make_inspection_event`) → 다음 지점,
무한 반복하는 구조. `--anomaly-prob`(기본 0.35)로 이상 발생 확률, `--inspection-pause`로
정지 시간 조절 가능. `--offline N` 오프라인 버퍼링 시연은 구조 그대로 유지
(버퍼링 로직 자체는 안 건드림 — 이벤트 생성 로직만 바뀜).

`vision.threshold`는 0.5, `thermal.threshold_c`는 50.0로 코드 안에 고정 상수로 있음
(로봇이 실제로 어떤 threshold를 쓸지는 유찬님 담당 — 지금은 데모용 임의값).

### 2-4. `static/test.html` — 완료

안전모/구역침입 등 고정 이벤트 버튼 3개를 없애고: station 선택 드롭다운(A/B/C,
mock_robot.py와 동일 좌표) + vision.score 슬라이더 + thermal.max_temp_c 슬라이더 +
판정 미리보기(ratio, verdict 실시간 계산 표시) + "이벤트 전송" 버튼 하나로 교체.
사진 첨부(샘플/직접선택 토글)는 이전 그대로 재사용. "로봇 상태 갱신" 섹션
(state/battery/yaw, `POST /api/robot/status`)도 그대로 있음 — 이건 이상탐지와
무관한 별도 기능이라 안 건드림.

## 3. 지금 하다 만 것 — `static/index.html` (커밋 안 됨, 절반만 됨)

**주의: 지금 이 파일은 새 스키마 기준으로 깨져 있다.** 실제 서버를 붙이면
알림 카드 제목이 `"undefined"`로 뜬다. 아래 순서대로 마저 고쳐야 한다.

`git diff static/index.html`로 확인 가능한 것: 타일 섹션에
`<div id="tileStations">0/3</div>` 하나, 알림 목록 헤더에
`<input type="checkbox" id="showAllToggle">` 체크박스 하나 — **HTML 마크업만
추가했고, 그 뒤에 필요한 JS 로직과 CSS는 하나도 안 건드렸다.**

### 3-1. 해야 하는데 아직 안 한 JS 변경 (전부 `<script>` 안, inline)

1. **`TYPE_LABELS` 상수 삭제.** 더 이상 `event.type`이 안 온다.
2. **`renderCard()`**: 제목을 `TYPE_LABELS[event.type]` → `event.station_name`
   (station_id도 같이 작은 글씨로), meta 줄에 좌표 대신/추가로
   `vision_score`/`vision_threshold` 비율 표시. 이미지 `alt` 속성도 마찬가지로 고칠 것.
3. **`openModal()`**: 제목을 station_name+station_id로, "신뢰도"(confidence) 행 삭제,
   대신 아래 detail row 추가:
   - `pose_pos_error_m` / `pose_yaw_error_rad` (pose 오차 — cm·deg로 변환해서 보여주면 좋음)
   - `vision_score` / `vision_threshold`
   - `thermal_max_temp_c` / `thermal_threshold_c`
   - `triggered_by` (배열 join)
   - verdict 자체도 배지나 텍스트로 표시 (NORMAL/ANOMALY)
4. **알림 목록 기본 필터(verdict=ANOMALY)**: 지금은 필터링 로직이 아예 없다.
   `showAllToggle` 체크박스에 이벤트 리스너가 없다. 설계 방향(이전에 사용자와
   합의한 것): `eventsById`/`eventOrder`는 지금처럼 전체를 다 들고 있고,
   렌더링 시점에 `showAllEvents` 플래그로 걸러서 `alertList`를 다시 그리는
   `renderAlertList()` 함수를 새로 만들어 `handleSnapshot`/`handleNewEvent`/
   `applyAck`가 개별 DOM 조작 대신 이 함수를 호출하도록 바꾼다. 새 카드
   강조 애니메이션(`isNew`)은 "가장 최근에 도착한 이벤트 id"를 별도 변수로
   기억했다가 그 카드에만 `isNew: true`를 넘기는 방식으로 유지.
5. **`updateTiles()`**: "미확인 알림"/"24시간 이상상황" 타일은 **verdict==='ANOMALY'인
   것만** 세도록 조건 추가 (지금은 전체를 센다 — NORMAL 방문까지 "이상상황"에
   잡히면 안 됨). `tileStationsEl`(=`$("tileStations")`) 변수 선언도 빠져있다.
6. **새 타일 "오늘 점검 지점 X/3" 계산 로직 추가**: 오늘 날짜(캘린더 기준)에
   방문한 서로 다른 `station_id` 개수를 세서 `X/3`로 표시. 전체 지점 수 3은
   `TOTAL_STATIONS = 3` 같은 상수로 박아두면 됨 (CLAUDE.md: "순찰 지점 3곳").

### 3-2. `static/style.css`에 아직 추가 안 한 것

- `.alerts-header` (h2와 체크박스를 양끝 정렬하는 flex 컨테이너)
- `.filter-toggle` (체크박스+라벨 작은 텍스트 스타일, `--text-muted` 컬러 활용)

기존 팔레트/변수(`--surface`, `--border`, `--text-muted` 등)는 `style.css` 맨 위
`:root`에 이미 정의돼 있으니 그대로 재사용하면 됨. 새 색상 만들 필요 없음.

### 3-3. `static/map.js`에 아직 안 한 것 (CLAUDE.md/작업 지시의 "6번 — 지도" 항목)

지금 `_drawEvents()`는 모든 이벤트를 `severity` 색깔의 원으로 그린다.
**정상(verdict==='NORMAL') 지점은 다른 모양으로 구분해서 그려야 한다** (사용자
요구사항: "이상 지점과 구분되는 모양·색+라벨"). 설계 방향(이미 구상해둔 것):

- `_drawEvents()` 안에서 `ev.verdict === 'NORMAL'`이면 별도 메서드
  (`_drawNormalMarker`)로 분기 — 원이 아니라 마름모(사각형을 45도 회전)나
  체크마크 같은 걸로 그리고, 색은 `--online` 계열 초록(`#3ecf7e`) 추천,
  옆에 `station_id` 텍스트 라벨도 같이 그림.
- ANOMALY 이벤트(기존 원 + severity 색 + 최근 미확인 링)는 로직 그대로 둔다.
- 색상 상수 `HELM_SEVERITY_COLORS` 옆에 `HELM_NORMAL_COLOR = "#3ecf7e"` 추가.

이 부분은 **아직 코드에 한 글자도 안 들어갔다** (설계만 논의됨).

## 4. 아직 손도 안 댄 것

- **테스트 코드가 저장소에 하나도 없다.** 지금까지의 모든 검증은
  `C:\Users\...\AppData\Local\Temp\claude\...\scratchpad\*.py`/`*.js`에 즉석으로
  짠 스크립트로 했고, 세션이 끝나면 사라진다. **사용자가 명시적으로 "테스트"를
  마지막 커밋 카테고리로 요청**했으니, `app/db.py`/`app/main.py`의
  `_compute_severity` 경계값(ratio 정확히 1.0/1.3, 온도차 정확히 10.0)을 포함한
  진짜 pytest 스위트를 `tests/`에 만들어야 한다. 지금 이 세션에서 돌렸던
  검증 항목은 아래 5절에 재현 가능하게 정리해뒀다 — 이걸 그대로 pytest로
  옮기면 됨.
- 위 3절의 index.html/style.css/map.js 작업이 끝나면 "프론트" 커밋 하나로 묶을 것
  (사용자가 커밋을 스키마/서버로직/개발도구/프론트/테스트 5개로 나눠달라고 했고,
  앞의 4개는 이미 완료). "정상 지점 지도 마커"도 이 프론트 커밋에 포함.
- `zones.html`(구역 편집기)은 이번 작업과 무관 — 아직 존재하지 않고, 이번
  이상탐지 스키마 변경과도 상관없다. 손대지 말 것.

## 5. 검증 방법 (재현용 — 이번 세션에서 이렇게 확인했다)

이 저장소엔 `jsdom`이 npm 의존성으로 안 잡혀 있다 — 프론트 테스트할 때
임시 디렉터리에서 `npm install jsdom --no-save`로 즉석 설치해서 썼다.

기본 패턴:
```powershell
$env:HELM_DATA_DIR = "<임시 폴더>"
$env:HELM_ROBOT_TOKEN = "dev-token"
$env:HELM_DEV = "1"   # test.html/샘플 API 쓰려면 1
python -m uvicorn app.main:app --host 127.0.0.1 --port <포트>
```
그 다음 `fastapi.testclient.TestClient`로 REST/WS 직접 호출하거나,
`tools/mock_robot.py --host 127.0.0.1 --port <포트> --token dev-token
--anomaly-prob 0.6 --inspection-pause 0.3`로 실제 이벤트를 흘려보내고
`sqlite3`로 `data/helm.db`를 직접 열어 컬럼 값을 확인했다.

이번 세션에서 통과 확인한 것 (전부 임시 스크립트, 저장 안 됨):

- `geometry.py`: pixel↔world 왕복, y축 뒤집힘 방향, point_in_polygon/circle,
  zones_containing 우선순위 정렬
- `db.py` 새 스키마: insert/list 왕복(JSON 배열 필드 포함), verdict 필터,
  ack 흐름, zones/robot_status 스키마 무변경
- `_compute_severity` 경계값: ratio 0.99/1.0/1.29999/1.3, 온도차 9.999/10.0,
  vision+thermal 동시 이상, threshold=0 방어(ZeroDivisionError 없음)
- `_process_event` 전체 흐름: CLAUDE.md 예시 페이로드 그대로 POST, 중복 제거,
  image_b64 스냅샷 저장, NORMAL→severity=info, verdict 필터, ts 누락 시
  예외 발생, 구역 기반 danger 상향
- WS 경로(`/ws/robot` → event → ack, `/ws/dashboard` broadcast)도 REST와
  동일하게 확인
- `mock_robot.py`: 3개 station 순회, NORMAL/ANOMALY 둘 다 생성, `--offline`
  버퍼링(연결 끊긴 동안 생성된 이벤트가 재연결 후 재전송되고 `duplicate=false`로
  찍히는 것까지) 확인. 생성된 스냅샷이 진짜 640×480 JPEG인 것도 확인
- `test.html`: 실제 서버에 붙여서 station 변경/판정 미리보기/이벤트 전송/상태
  갱신 버튼까지 Node의 `vm` 모듈로 `<script>`를 그대로 실행해서 확인

## 6. 자잘한 주의사항

- git이 `warning: LF will be replaced by CRLF`를 계속 띄운다 — Windows 환경
  이슈이고 무시해도 된다.
- 같은 서버(같은 DB)에 `mock_robot.py`를 짧은 간격으로 두 번 이상 돌리면,
  이벤트 id 포맷이 `YYYYMMDD-HHMM-station-seq`(분 단위 해상도)라서 같은 분에
  같은 station을 처음 방문하면 id가 겹쳐 `duplicate=true`가 뜬다. 버그 아니고
  테스트 방식 문제였다 — 새 서버(새 DATA_DIR)로 재현하면 깨끗하게 재현됨.
- `.env` 파일이 실제로 존재하고 `python-dotenv`로 로드된다
  (`override=False`라 진짜 환경변수가 항상 우선). `.env.example`이 최신 상태.
- severity 임계값(1.0/1.3/10.0)은 **전부 실측 전 임시값**이라고 CLAUDE.md에
  명시돼 있다. 나중에 실측 데이터로 바뀔 걸 전제하고 있으니 하드코딩하지 말 것
  (이미 config.py에서 읽게 돼 있음, 유지만 하면 됨).
