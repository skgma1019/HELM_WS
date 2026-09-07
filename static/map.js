/**
 * 지도 렌더러. 대시보드(index.html)와 구역 편집기(zones.html)가 같이 쓴다.
 *
 * 좌표 변환식은 app/geometry.py와 여기 두 곳에만 존재한다 — 하나를 고치면 반드시 같이 고친다.
 *   world_x = origin_x + px * resolution
 *   world_y = origin_y + (height - py) * resolution
 *   px = (world_x - origin_x) / resolution
 *   py = height - (world_y - origin_y) / resolution
 */

const HELM_SEVERITY_COLORS = {
  danger: "#ff5c5c",
  caution: "#ffb020",
  info: "#4ea1ff",
};

const HELM_NORMAL_COLOR = "#3ecf7e";
const HELM_MAX_TRAIL_POINTS = 300;
const HELM_RECENT_EVENT_SEC = 30; // 이 시간 안의 미확인 이벤트는 링으로 강조

// 100, 50, 20, 10, 5, 2, 1 ... 순으로 "깔끔한" 값 하나를 고른다
function helmNiceScaleValue(targetMeters) {
  if (targetMeters <= 0) return 1;
  const exponent = Math.floor(Math.log10(targetMeters));
  const base = Math.pow(10, exponent);
  const fraction = targetMeters / base;
  let niceFraction;
  if (fraction < 1.5) niceFraction = 1;
  else if (fraction < 3.5) niceFraction = 2;
  else if (fraction < 7.5) niceFraction = 5;
  else niceFraction = 10;
  return niceFraction * base;
}

class MapView {
  constructor(canvas, { onEventClick } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onEventClick = onEventClick || null;

    this.meta = null; // {image, resolution, origin_x, origin_y, width, height}
    this.image = null; // HTMLImageElement

    this.zoom = 1; // CSS px per pixel-좌표
    this.panX = 0;
    this.panY = 0;

    this.robots = {}; // robot_id -> {x, y, yaw, trail: [{x,y}]}
    this.events = []; // 서버 이벤트 레코드 배열 (렌더 시점에 _screenX/_screenY를 덧붙인다)

    this._dpr = window.devicePixelRatio || 1;
    this._hasFitOnce = false;

    this._setupInteraction();
    this._resizeObserver = new ResizeObserver(() => this.render());
    this._resizeObserver.observe(canvas);
  }

  // ---------- 좌표 변환 (geometry.py와 동일한 식) ----------

  worldToPixel(worldX, worldY) {
    const px = (worldX - this.meta.origin_x) / this.meta.resolution;
    const py = this.meta.height - (worldY - this.meta.origin_y) / this.meta.resolution;
    return [px, py];
  }

  pixelToWorld(px, py) {
    const worldX = this.meta.origin_x + px * this.meta.resolution;
    const worldY = this.meta.origin_y + (this.meta.height - py) * this.meta.resolution;
    return [worldX, worldY];
  }

  pixelToScreen(px, py) {
    return [px * this.zoom + this.panX, py * this.zoom + this.panY];
  }

  screenToPixel(sx, sy) {
    return [(sx - this.panX) / this.zoom, (sy - this.panY) / this.zoom];
  }

  worldToScreen(worldX, worldY) {
    const [px, py] = this.worldToPixel(worldX, worldY);
    return this.pixelToScreen(px, py);
  }

  screenToWorld(sx, sy) {
    const [px, py] = this.screenToPixel(sx, sy);
    return this.pixelToWorld(px, py);
  }

  // ---------- 지도 로드 ----------

  async loadMap() {
    const res = await fetch("/api/map");
    const data = await res.json();
    if (!data.map) {
      this.meta = null;
      this.image = null;
      this.render();
      return;
    }
    this.setMapMeta(data.map);
    await this.reloadImage();
    this.fitToView();
  }

  setMapMeta(meta) {
    this.meta = meta;
  }

  async reloadImage() {
    if (!this.meta) return;
    const img = new Image();
    const url = `/api/map/image?t=${Date.now()}`; // map_updated 이후 캐시 무력화
    await new Promise((resolve, reject) => {
      img.onload = resolve;
      img.onerror = reject;
      img.src = url;
    });
    this.image = img;
    if (!this._hasFitOnce) this.fitToView();
    this.render();
  }

  // ---------- 실시간 데이터 반영 ----------

  updateRobot(robotId, pose) {
    if (!this.robots[robotId]) {
      this.robots[robotId] = { x: null, y: null, yaw: 0, trail: [] };
    }
    const r = this.robots[robotId];
    if (pose.yaw != null) r.yaw = pose.yaw;
    if (pose.x != null && pose.y != null) {
      r.x = pose.x;
      r.y = pose.y;
      r.trail.push({ x: pose.x, y: pose.y });
      if (r.trail.length > HELM_MAX_TRAIL_POINTS) r.trail.shift();
    }
    this.render();
  }

  setEvents(events) {
    this.events = events;
    this.render();
  }

  // ---------- 화면 맞춤 / 리사이즈 ----------

  fitToView() {
    if (!this.meta) return;
    const cssW = this.canvas.clientWidth || 1;
    const cssH = this.canvas.clientHeight || 1;
    const margin = 20;
    const zoomX = (cssW - margin * 2) / this.meta.width;
    const zoomY = (cssH - margin * 2) / this.meta.height;
    this.zoom = Math.max(0.02, Math.min(zoomX, zoomY));
    this.panX = (cssW - this.meta.width * this.zoom) / 2;
    this.panY = (cssH - this.meta.height * this.zoom) / 2;
    this._hasFitOnce = true;
    this.render();
  }

  _resizeCanvasToDisplaySize() {
    const dpr = window.devicePixelRatio || 1;
    const targetW = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const targetH = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== targetW || this.canvas.height !== targetH) {
      this.canvas.width = targetW;
      this.canvas.height = targetH;
    }
    this._dpr = dpr;
  }

  // ---------- 드래그 이동 / 휠 확대축소 ----------

  _setupInteraction() {
    const canvas = this.canvas;
    let dragging = false;
    let moved = false;
    let lastX = 0;
    let lastY = 0;

    canvas.addEventListener("pointerdown", (e) => {
      dragging = true;
      moved = false;
      lastX = e.clientX;
      lastY = e.clientY;
      canvas.setPointerCapture(e.pointerId);
      canvas.classList.add("dragging");
    });

    canvas.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
      this.panX += dx;
      this.panY += dy;
      lastX = e.clientX;
      lastY = e.clientY;
      this.render();
    });

    const endDrag = (e) => {
      dragging = false;
      this._didDrag = moved;
      canvas.classList.remove("dragging");
      try {
        canvas.releasePointerCapture(e.pointerId);
      } catch {
        // 이미 풀려있으면 무시
      }
    };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);

    canvas.addEventListener(
      "wheel",
      (e) => {
        if (!this.meta) return;
        e.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        // 휠 전 커서 아래 픽셀좌표를 기억했다가, 줌 후 같은 화면 위치에 오도록 pan을 보정한다
        const [pxBefore, pyBefore] = this.screenToPixel(cx, cy);
        const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
        this.zoom = Math.min(40, Math.max(0.02, this.zoom * factor));
        this.panX = cx - pxBefore * this.zoom;
        this.panY = cy - pyBefore * this.zoom;
        this.render();
      },
      { passive: false }
    );

    canvas.addEventListener("click", (e) => {
      if (this._didDrag) {
        this._didDrag = false;
        return;
      }
      const rect = canvas.getBoundingClientRect();
      const clickX = e.clientX - rect.left;
      const clickY = e.clientY - rect.top;
      let closest = null;
      let closestDist = Infinity;
      for (const ev of this.events) {
        if (ev._screenX == null) continue;
        const dist = Math.hypot(ev._screenX - clickX, ev._screenY - clickY);
        if (dist < 10 && dist < closestDist) {
          closest = ev;
          closestDist = dist;
        }
      }
      if (closest && this.onEventClick) this.onEventClick(closest.id);
    });
  }

  // ---------- 렌더 ----------

  render() {
    this._resizeCanvasToDisplaySize();
    const ctx = this.ctx;
    const dpr = this._dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const cssW = this.canvas.clientWidth;
    const cssH = this.canvas.clientHeight;
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.fillStyle = "#0f1216";
    ctx.fillRect(0, 0, cssW, cssH);

    if (!this.meta) {
      ctx.fillStyle = "#8b96a5";
      ctx.font = "13px -apple-system, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("지도가 없습니다", cssW / 2, cssH / 2);
      return;
    }

    if (this.image) {
      ctx.save();
      ctx.translate(this.panX, this.panY);
      ctx.scale(this.zoom, this.zoom);
      ctx.drawImage(this.image, 0, 0, this.meta.width, this.meta.height);
      ctx.restore();
    }

    this._drawTrails(ctx);
    this._drawEvents(ctx);
    this._drawRobots(ctx);
    this._drawScaleBar(ctx, cssW, cssH);
  }

  _drawTrails(ctx) {
    for (const robotId in this.robots) {
      const trail = this.robots[robotId].trail;
      if (trail.length < 2) continue;
      ctx.beginPath();
      ctx.strokeStyle = "rgba(78, 161, 255, 0.55)";
      ctx.lineWidth = 2;
      trail.forEach((pt, i) => {
        const [sx, sy] = this.worldToScreen(pt.x, pt.y);
        if (i === 0) ctx.moveTo(sx, sy);
        else ctx.lineTo(sx, sy);
      });
      ctx.stroke();
    }
  }

  _drawEvents(ctx) {
    const nowSec = Date.now() / 1000;
    for (const ev of this.events) {
      if (ev.x == null || ev.y == null) {
        ev._screenX = null;
        ev._screenY = null;
        continue;
      }
      const [sx, sy] = this.worldToScreen(ev.x, ev.y);
      ev._screenX = sx;
      ev._screenY = sy;
      if (ev.verdict === "NORMAL") {
        this._drawNormalMarker(ctx, sx, sy, ev.station_id);
        continue;
      }
      const color = HELM_SEVERITY_COLORS[ev.severity] || HELM_SEVERITY_COLORS.info;

      ctx.beginPath();
      ctx.arc(sx, sy, 5, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = "#0f1216";
      ctx.lineWidth = 1.5;
      ctx.stroke();

      const isRecentUnacked = !ev.acked && nowSec - ev.ts <= HELM_RECENT_EVENT_SEC;
      if (isRecentUnacked) {
        ctx.beginPath();
        ctx.arc(sx, sy, 9, 0, Math.PI * 2);
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }
  }

  _drawNormalMarker(ctx, sx, sy, stationId) {
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(sx, sy - 6);
    ctx.lineTo(sx + 6, sy);
    ctx.lineTo(sx, sy + 6);
    ctx.lineTo(sx - 6, sy);
    ctx.closePath();
    ctx.fillStyle = HELM_NORMAL_COLOR;
    ctx.fill();
    ctx.strokeStyle = "#0f1216";
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.font = "12px -apple-system, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const label = `${stationId || "-"} 정상`;
    ctx.lineWidth = 3;
    ctx.strokeText(label, sx + 10, sy);
    ctx.fillText(label, sx + 10, sy);
    ctx.restore();
  }

  _drawRobots(ctx) {
    for (const robotId in this.robots) {
      const r = this.robots[robotId];
      if (r.x == null || r.y == null) continue;
      const [sx, sy] = this.worldToScreen(r.x, r.y);
      this._drawRobotMarker(ctx, sx, sy, r.yaw || 0);
    }
  }

  _drawRobotMarker(ctx, sx, sy, worldYaw) {
    const size = 10;
    ctx.save();
    ctx.translate(sx, sy);
    // 화면(픽셀) y축은 월드와 뒤집혀 있으니 각도 부호도 뒤집는다 (설계서 5장)
    ctx.rotate(-worldYaw);
    ctx.beginPath();
    ctx.moveTo(size, 0);
    ctx.lineTo(-size * 0.6, size * 0.6);
    ctx.lineTo(-size * 0.6, -size * 0.6);
    ctx.closePath();
    ctx.fillStyle = "#4ea1ff";
    ctx.fill();
    ctx.strokeStyle = "#0f1216";
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.restore();
  }

  _drawScaleBar(ctx, cssW, cssH) {
    if (!this.meta) return;
    const pxPerMeter = this.zoom / this.meta.resolution;
    if (!isFinite(pxPerMeter) || pxPerMeter <= 0) return;

    const targetPx = 100;
    const meters = helmNiceScaleValue(targetPx / pxPerMeter);
    const barPx = meters * pxPerMeter;

    const margin = 16;
    const barY = cssH - margin;
    const barXEnd = cssW - margin;
    const barXStart = barXEnd - barPx;

    ctx.strokeStyle = "#e6e9ef";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(barXStart, barY);
    ctx.lineTo(barXEnd, barY);
    ctx.moveTo(barXStart, barY - 4);
    ctx.lineTo(barXStart, barY + 4);
    ctx.moveTo(barXEnd, barY - 4);
    ctx.lineTo(barXEnd, barY + 4);
    ctx.stroke();

    ctx.fillStyle = "#e6e9ef";
    ctx.font = "11px -apple-system, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    const label = meters >= 1 ? `${meters}m` : `${Math.round(meters * 100)}cm`;
    ctx.fillText(label, (barXStart + barXEnd) / 2, barY - 8);
  }
}
