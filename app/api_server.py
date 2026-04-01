"""FastAPI server with WebSocket for real-time ship position updates"""
import asyncio
import json

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from app.database import close_db_pool, get_ais_stations, get_ship_positions, get_ship_trails, init_db_pool
from app.world_ports import STATIC_MAJOR_PORTS
from config import OPENWEATHERMAP_API_KEY

ALLOWED_WEATHER_TILE_LAYERS = frozenset({"precipitation_new", "temp_new"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global poller_task
    await init_db_pool()
    poller_task = asyncio.create_task(refresh_positions_loop())
    try:
        yield
    finally:
        if poller_task:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
        await close_db_pool()


app = FastAPI(title="Ship Tracker RT API", lifespan=lifespan)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict):
        """Broadcast data to all connected clients"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(data)
            except Exception:
                disconnected.append(connection)
        
        # Remove disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()
latest_payload = {
    "type": "update",
    "ships": [],
    "stations": [],
    "timestamp": datetime.now(timezone.utc).isoformat(),
}
poller_task = None

_STATIC_STATIONS_PAYLOAD = [
    {
        "id": p["id"],
        "kind": p["kind"],
        "name": p["name"],
        "latitude": p["latitude"],
        "longitude": p["longitude"],
        "type_code": None,
    }
    for p in STATIC_MAJOR_PORTS
]


async def refresh_positions_loop():
    """Single shared poller: one DB query, broadcast to all clients."""
    global latest_payload
    while True:
        try:
            positions = await get_ship_positions(max_age_minutes=30)
            db_stations = _serialize_ais_stations(await get_ais_stations())
            latest_payload = {
                "type": "update",
                "ships": _serialize_positions(positions),
                "stations": db_stations + _STATIC_STATIONS_PAYLOAD,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await manager.broadcast(latest_payload)
        except Exception as exc:
            print(f"Refresh loop error: {exc}")
        await asyncio.sleep(1)


@app.get("/api/weather/current")
async def weather_current(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    lang: str = Query("en"),
):
    """Current conditions at a point: tries One Call API 3.0, then falls back to Weather 2.5."""
    if not OPENWEATHERMAP_API_KEY:
        return JSONResponse({"error": "weather_not_configured"}, status_code=503)
    lang_param = "ru" if lang.lower().startswith("ru") else "en"
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        url3 = (
            "https://api.openweathermap.org/data/3.0/onecall"
            f"?lat={lat}&lon={lon}&units=metric&exclude=minutely,hourly,daily&lang={lang_param}"
            f"&appid={OPENWEATHERMAP_API_KEY}"
        )
        try:
            r3 = await client.get(url3)
            if r3.status_code == 200:
                data = r3.json()
                cur = data.get("current") or {}
                w0 = (cur.get("weather") or [{}])[0]
                rain = cur.get("rain") or {}
                snow = cur.get("snow") or {}
                precip = rain.get("1h")
                if precip is None:
                    precip = snow.get("1h")
                return {
                    "lat": float(data.get("lat", lat)),
                    "lon": float(data.get("lon", lon)),
                    "temp_c": cur.get("temp"),
                    "feels_like_c": cur.get("feels_like"),
                    "humidity": cur.get("humidity"),
                    "wind_speed_ms": cur.get("wind_speed"),
                    "description": (w0.get("description") or "").strip(),
                    "precip_mm_h": precip,
                    "source": "onecall3",
                }
        except httpx.RequestError:
            pass

        url25 = (
            "https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat}&lon={lon}&units=metric&lang={lang_param}"
            f"&appid={OPENWEATHERMAP_API_KEY}"
        )
        try:
            r25 = await client.get(url25)
        except httpx.RequestError as e:
            return JSONResponse({"error": "upstream_unreachable", "detail": str(e)}, status_code=502)
        if r25.status_code != 200:
            return JSONResponse(
                {
                    "error": "upstream_error",
                    "status": r25.status_code,
                    "detail": (r25.text or "")[:300],
                },
                status_code=502,
            )
        data = r25.json()
        main = data.get("main") or {}
        w0 = (data.get("weather") or [{}])[0]
        coord = data.get("coord") or {}
        rain = data.get("rain") or {}
        snow = data.get("snow") or {}
        precip = rain.get("1h")
        if precip is None:
            precip = snow.get("1h")
        wind = data.get("wind") or {}
        return {
            "lat": float(coord.get("lat", lat)),
            "lon": float(coord.get("lon", lon)),
            "temp_c": main.get("temp"),
            "feels_like_c": main.get("feels_like"),
            "humidity": main.get("humidity"),
            "wind_speed_ms": wind.get("speed"),
            "description": (w0.get("description") or "").strip(),
            "precip_mm_h": precip,
            "source": "weather25",
        }


@app.get("/api/weather/tiles/{layer}/{z:int}/{x:int}/{y:int}.png")
async def weather_tile_proxy(layer: str, z: int, x: int, y: int):
    if not OPENWEATHERMAP_API_KEY:
        return Response(status_code=503)
    if layer not in ALLOWED_WEATHER_TILE_LAYERS:
        raise HTTPException(status_code=404, detail="unknown layer")
    url = f"https://tile.openweathermap.org/map/{layer}/{z}/{x}/{y}.png?appid={OPENWEATHERMAP_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url)
    except httpx.RequestError:
        return Response(status_code=502)
    return Response(content=r.content, status_code=r.status_code, media_type="image/png")


@app.get("/")
async def get_index():
    """Serve the HTML page"""
    html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Ship Tracker RT - Real-Time</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
    <style>
        body { margin: 0; padding: 0; font-family: Arial, sans-serif; }
        #map { height: 100vh; width: 100%; }
        .panel {
            position: absolute;
            background: white;
            padding: 12px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.25);
            z-index: 1000;
            min-width: 240px;
            font-size: 13px;
        }
        #controls { top: 10px; left: 10px; max-height: min(520px, calc(100vh - 100px)); overflow-y: auto; }
        .map-zoom-dock {
            position: fixed;
            left: 10px;
            top: 66%;
            transform: translateY(-50%);
            z-index: 999;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
            background: rgba(15, 23, 42, 0.92);
            border: 1px solid rgba(148, 163, 184, 0.22);
            border-radius: 8px;
            padding: 5px 6px 6px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.2);
            width: 52px;
            box-sizing: border-box;
            color: #e2e8f0;
        }
        .map-zoom-dock .controls-section-title {
            margin: 0 0 1px 0;
            font-size: 8px;
            letter-spacing: 0.04em;
            align-self: stretch;
            text-align: center;
            line-height: 1.2;
        }
        .map-zoom-dock .scale-zoom-row {
            margin-bottom: 0;
            width: 100%;
            justify-content: space-between;
            gap: 4px;
        }
        .map-zoom-dock .scale-zoom-lbl { font-size: 9px; }
        .map-zoom-dock .scale-zoom-val {
            font-size: 14px;
            font-weight: 800;
        }
        .map-zoom-dock .scale-bar-wrap {
            margin-top: 0;
            width: 100%;
        }
        .map-zoom-dock .scale-bar-track {
            width: 100%;
            max-width: 100%;
        }
        .map-zoom-dock .scale-bar-caption {
            margin-top: 2px;
            font-size: 9px;
        }
        .zoom-dock-btn {
            width: 32px;
            min-width: 32px;
            height: 28px;
            padding: 0;
            font-size: 15px;
            font-weight: 700;
            line-height: 1;
            border-radius: 5px;
            border: 1px solid rgba(148, 163, 184, 0.32);
            background: rgba(30, 41, 59, 0.95);
            color: #e2e8f0;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .zoom-dock-btn:hover { background: rgba(51, 65, 85, 0.98); }
        .bottom-lang-bar {
            position: fixed;
            left: 10px;
            bottom: 14px;
            z-index: 1001;
            margin: 0;
            padding: 8px 10px;
            min-width: auto;
            background: rgba(15, 23, 42, 0.94);
            border: 1px solid rgba(148, 163, 184, 0.28);
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.25);
        }
        .bottom-lang-bar .lang-btn {
            background: rgba(30, 41, 59, 0.9);
            color: #e2e8f0;
            border-color: rgba(148, 163, 184, 0.4);
        }
        .controls-filters-title {
            margin-top: 0;
        }
        #controls.controls-panel {
            background: rgba(15, 23, 42, 0.94);
            color: #e2e8f0;
            border: 1px solid rgba(148, 163, 184, 0.28);
            min-width: 268px;
            padding: 14px 14px 12px;
            box-sizing: border-box;
        }
        .controls-panel h4 {
            margin: 0 0 12px 0;
            font-size: 14px;
            font-weight: 700;
            color: #f8fafc;
            letter-spacing: 0.02em;
        }
        .controls-section-title {
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            color: #94a3b8;
            margin: 0 0 8px 0;
        }
        .controls-mode-section { margin-bottom: 14px; }
        .mode-btn-group {
            display: flex;
            gap: 6px;
            width: 100%;
        }
        .mode-btn {
            flex: 1;
            padding: 9px 6px;
            border-radius: 8px;
            border: 1px solid rgba(148, 163, 184, 0.35);
            background: rgba(30, 41, 59, 0.92);
            color: #e2e8f0;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
            line-height: 1.2;
        }
        .mode-btn:hover { background: rgba(51, 65, 85, 0.95); }
        .mode-btn.mode-live-on {
            border-color: #22c55e;
            box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.35);
            color: #f0fdf4;
        }
        .mode-btn.mode-pause-on {
            border-color: #eab308;
            box-shadow: 0 0 0 1px rgba(234, 179, 8, 0.35);
            color: #fefce8;
        }
        .mode-btn.mode-replay-on {
            border-color: #a78bfa;
            box-shadow: 0 0 0 1px rgba(167, 139, 250, 0.35);
            color: #f5f3ff;
        }
        .controls-scale-section { margin-bottom: 4px; }
        .scale-zoom-row {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 10px;
        }
        .scale-zoom-lbl { font-size: 12px; color: #94a3b8; }
        .scale-zoom-val { font-size: 22px; font-weight: 800; color: #f1f5f9; font-variant-numeric: tabular-nums; }
        .scale-bar-wrap { margin-top: 2px; }
        .scale-bar-track {
            width: 120px;
            max-width: 100%;
            height: 7px;
            background: rgba(148, 163, 184, 0.22);
            border-radius: 4px;
            overflow: hidden;
            border: 1px solid rgba(51, 65, 85, 0.8);
        }
        .scale-bar-fill {
            height: 100%;
            width: 48px;
            background: linear-gradient(90deg, #38bdf8, #818cf8);
            border-radius: 3px;
        }
        .scale-bar-caption {
            font-size: 11px;
            color: #94a3b8;
            margin-top: 6px;
            font-variant-numeric: tabular-nums;
        }
        .controls-divider {
            height: 1px;
            background: rgba(148, 163, 184, 0.18);
            margin: 14px 0 12px;
        }
        .controls-panel .row label { color: #cbd5e1; }
        .controls-panel select,
        .controls-panel .row button {
            background: rgba(30, 41, 59, 0.95);
            color: #e2e8f0;
            border-color: rgba(148, 163, 184, 0.35);
        }
        .controls-panel .row button:hover {
            background: rgba(51, 65, 85, 0.98);
        }
        #mapToolbar {
            top: 10px;
            right: 10px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            align-items: stretch;
            min-width: auto;
            padding: 8px;
            background: rgba(15, 23, 42, 0.92);
            color: #e2e8f0;
            border: 1px solid rgba(148, 163, 184, 0.25);
        }
        .toolbar-title {
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #94a3b8;
            margin: 0 0 4px 0;
            text-align: center;
        }
        .tool-btn {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 8px 10px;
            border-radius: 8px;
            border: 1px solid rgba(148, 163, 184, 0.35);
            background: rgba(30, 41, 59, 0.9);
            color: #e2e8f0;
            font-size: 12px;
            font-weight: 600;
        }
        .tool-btn:hover { background: rgba(51, 65, 85, 0.95); }
        .tool-btn.active {
            border-color: #0ea5e9;
            box-shadow: 0 0 0 1px rgba(14, 165, 233, 0.45);
        }
        .tool-btn:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }
        #btnToolbarHelp {
            font-size: 15px;
            font-weight: 700;
        }
        #analyticsPanel {
            position: fixed;
            top: 72px;
            right: 12px;
            width: min(480px, calc(100vw - 24px));
            max-height: min(88vh, 760px);
            overflow: auto;
            z-index: 1101;
            border-radius: 16px;
            background: linear-gradient(155deg, #0f172a 0%, #1e293b 42%, #0c1222 100%);
            border: 1px solid rgba(56, 189, 248, 0.28);
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.55), 0 0 40px rgba(14, 165, 233, 0.12);
            color: #e2e8f0;
        }
        #analyticsPanel.hidden { display: none !important; }
        .analytics-modal-inner { padding: 20px 20px 18px; }
        .analytics-drag-handle {
            cursor: grab;
            user-select: none;
            padding: 6px 4px 0 0;
            color: #64748b;
            font-size: 14px;
            line-height: 1;
            letter-spacing: -2px;
            flex-shrink: 0;
        }
        .analytics-drag-handle:active { cursor: grabbing; }
        .analytics-head {
            display: flex;
            align-items: flex-start;
            gap: 8px;
            margin-bottom: 16px;
            padding-bottom: 14px;
            border-bottom: 1px solid rgba(148, 163, 184, 0.22);
        }
        .analytics-head-main { flex: 1; min-width: 0; }
        .analytics-head-actions {
            display: flex;
            flex-direction: row;
            align-items: flex-start;
            gap: 8px;
            flex-shrink: 0;
        }
        .analytics-head h2 {
            margin: 0;
            font-size: 20px;
            font-weight: 700;
            background: linear-gradient(90deg, #38bdf8, #a78bfa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .analytics-meta { font-size: 11px; color: #94a3b8; margin-top: 6px; line-height: 1.4; }
        .btn-icon {
            width: 36px;
            height: 36px;
            border-radius: 10px;
            border: 1px solid rgba(148, 163, 184, 0.35);
            background: rgba(30, 41, 59, 0.85);
            color: #e2e8f0;
            cursor: pointer;
            font-size: 22px;
            line-height: 1;
        }
        .btn-icon:hover { background: rgba(51, 65, 85, 0.95); }
        .stat-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 14px;
        }
        .stat-card {
            background: rgba(30, 41, 59, 0.55);
            border: 1px solid rgba(148, 163, 184, 0.14);
            border-radius: 12px;
            padding: 12px;
        }
        .stat-card .lbl {
            font-size: 10px;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .stat-card .val { font-size: 21px; font-weight: 700; color: #f8fafc; margin-top: 6px; }
        .stat-card .val-sm { font-size: 15px; }
        .stat-card.wide { grid-column: 1 / -1; }
        .histogram { margin-top: 4px; }
        .histogram-row { margin-bottom: 10px; }
        .histogram-row .h-label {
            display: flex;
            justify-content: space-between;
            font-size: 11px;
            color: #94a3b8;
            margin-bottom: 4px;
        }
        .histogram-track {
            height: 9px;
            border-radius: 5px;
            background: rgba(15, 23, 42, 0.85);
            overflow: hidden;
        }
        .histogram-bar {
            height: 100%;
            border-radius: 5px;
            width: 0%;
            transition: width 0.4s ease;
        }
        .bar-stopped .histogram-bar { background: linear-gradient(90deg, #64748b, #94a3b8); }
        .bar-slow .histogram-bar { background: linear-gradient(90deg, #0ea5e9, #22d3ee); }
        .bar-medium .histogram-bar { background: linear-gradient(90deg, #7c3aed, #a78bfa); }
        .bar-fast .histogram-bar { background: linear-gradient(90deg, #ea580c, #fb923c); }
        .analytics-tabs {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 14px;
        }
        .analytics-tab {
            flex: 1;
            min-width: 0;
            padding: 8px 8px;
            font-size: 11px;
            font-weight: 600;
            border-radius: 8px;
            border: 1px solid rgba(148, 163, 184, 0.35);
            background: rgba(30, 41, 59, 0.75);
            color: #cbd5e1;
            cursor: pointer;
            line-height: 1.25;
        }
        .analytics-tab:hover {
            background: rgba(51, 65, 85, 0.92);
        }
        .analytics-tab[aria-selected="true"] {
            border-color: #38bdf8;
            color: #f8fafc;
            box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.35);
        }
        .analytics-tab-panel { margin-bottom: 4px; }
        .analytics-tab-panel.hidden { display: none !important; }
        .analytics-section-hint {
            font-size: 11px;
            color: #94a3b8;
            line-height: 1.4;
            margin: 0 0 12px 0;
        }
        .type-breakdown-row { margin-bottom: 10px; }
        .type-breakdown-top {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 8px;
            font-size: 11px;
            color: #e2e8f0;
            margin-bottom: 4px;
        }
        .type-breakdown-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .type-breakdown-count { flex-shrink: 0; color: #94a3b8; font-variant-numeric: tabular-nums; }
        .type-breakdown-bar {
            height: 7px;
            border-radius: 4px;
            background: rgba(15, 23, 42, 0.92);
            overflow: hidden;
        }
        .type-breakdown-fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.35s ease;
            min-width: 0;
        }
        .adv-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 10px;
        }
        .adv-note {
            font-size: 10px;
            color: #64748b;
            margin: 0 0 10px 0;
            line-height: 1.35;
        }
        .type-viz-toolbar {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
            margin-bottom: 12px;
        }
        .type-viz-toolbar .lbl-inline {
            font-size: 11px;
            color: #94a3b8;
            margin-right: 4px;
        }
        .analytics-viz-btn {
            padding: 6px 12px;
            font-size: 11px;
            font-weight: 600;
            border-radius: 8px;
            border: 1px solid rgba(148, 163, 184, 0.4);
            background: rgba(30, 41, 59, 0.85);
            color: #cbd5e1;
            cursor: pointer;
        }
        .analytics-viz-btn:hover {
            background: rgba(51, 65, 85, 0.95);
        }
        .analytics-viz-btn[aria-pressed="true"] {
            border-color: #38bdf8;
            color: #f8fafc;
            box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.35);
        }
        .type-chart-bars {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 3px;
            height: 150px;
            padding: 4px 2px 0;
            border-bottom: 1px solid rgba(148, 163, 184, 0.18);
        }
        .type-chart-bar-col {
            flex: 1;
            min-width: 0;
            max-width: 48px;
            display: flex;
            flex-direction: column;
            align-items: center;
            height: 100%;
            justify-content: flex-end;
        }
        .type-chart-bar-val {
            font-size: 9px;
            color: #94a3b8;
            margin-bottom: 2px;
            font-variant-numeric: tabular-nums;
        }
        .type-chart-bar-fill {
            width: 100%;
            border-radius: 4px 4px 0 0;
            min-height: 0;
            transition: height 0.35s ease;
        }
        .type-chart-bar-lbl {
            font-size: 8px;
            color: #94a3b8;
            text-align: center;
            margin-top: 6px;
            line-height: 1.15;
            max-height: 2.4em;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            word-break: break-word;
        }
        .type-chart-pie-wrap {
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            align-items: center;
            justify-content: center;
        }
        .type-chart-pie-svg {
            flex-shrink: 0;
        }
        .type-chart-pie-legend {
            font-size: 11px;
            color: #e2e8f0;
            min-width: 0;
            flex: 1 1 160px;
        }
        .type-chart-pie-leg-row {
            display: flex;
            align-items: flex-start;
            gap: 8px;
            margin-bottom: 7px;
            line-height: 1.3;
        }
        .type-chart-pie-swatch {
            width: 11px;
            height: 11px;
            border-radius: 2px;
            flex-shrink: 0;
            margin-top: 2px;
        }
        .analytics-status-row {
            margin-top: 14px;
            padding-top: 14px;
            border-top: 1px solid rgba(148, 163, 184, 0.15);
            font-size: 13px;
        }
        .lang-switch { display: flex; gap: 2px; flex-shrink: 0; }
        .lang-btn {
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 600;
            min-width: 36px;
        }
        .lang-btn.active {
            background: linear-gradient(135deg, #0ea5e9, #0284c7);
            color: #fff;
            border-color: #0284c7;
        }
        h3, h4 { margin: 0 0 8px 0; }
        .status { font-weight: bold; }
        .status.connected { color: #15803d; }
        .status.disconnected { color: #dc2626; }
        .row { margin: 6px 0; display: flex; gap: 8px; align-items: center; }
        .row label { min-width: 90px; }
        input, select, button {
            font-size: 12px;
            padding: 4px 6px;
            border: 1px solid #d1d5db;
            border-radius: 4px;
        }
        button { cursor: pointer; }
        .ship-div-icon { background: transparent; border: none; }
        .ship-marker {
            width: 18px;
            height: 18px;
            transform-origin: center center;
            transition: transform 0.2s ease;
            filter: drop-shadow(0 2px 4px rgba(0, 0, 0, 0.65));
        }
        .cluster-bubble {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: rgba(30, 41, 59, 0.92);
            border: 1px solid rgba(255, 255, 255, 0.25);
            box-shadow: 0 10px 25px rgba(0,0,0,0.25);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 2px;
            color: #e5e7eb;
        }
        .cluster-emoji {
            font-size: 14px;
            line-height: 14px;
            filter: drop-shadow(0 1px 0 rgba(0,0,0,0.35));
        }
        .cluster-count {
            font-size: 12px;
            font-weight: 700;
            color: #e5e7eb;
            display: block;
        }
        .single-bubble {
            width: 34px;
            height: 34px;
            border-radius: 50%;
            background: rgba(30, 41, 59, 0.92);
            border: 1px solid rgba(255, 255, 255, 0.25);
            box-shadow: 0 10px 25px rgba(0,0,0,0.25);
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .single-bubble .cluster-emoji {
            font-size: 14px;
            line-height: 14px;
        }
        .station-cluster-bubble {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: rgba(30, 41, 59, 0.94);
            border: 2px solid rgba(99, 102, 241, 0.75);
            box-shadow: 0 8px 22px rgba(0,0,0,0.35), 0 0 0 1px rgba(16, 185, 129, 0.35);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 1px;
            color: #e5e7eb;
        }
        .station-cluster-emoji {
            font-size: 13px;
            line-height: 13px;
        }
        .station-cluster-count {
            font-size: 11px;
            font-weight: 700;
            color: #c7d2fe;
        }
        .station-popover {
            position: fixed;
            z-index: 1102;
            width: min(280px, calc(100vw - 80px));
            max-height: min(420px, calc(100vh - 24px));
            overflow: auto;
            box-sizing: border-box;
            border-radius: 12px;
            background: linear-gradient(160deg, #0f172a 0%, #1e293b 100%);
            border: 1px solid rgba(99, 102, 241, 0.4);
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(15, 23, 42, 0.5);
            color: #e2e8f0;
        }
        .station-popover.hidden {
            display: none !important;
        }
        .station-popover-head {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 10px;
            padding: 12px 12px 10px;
            border-bottom: 1px solid rgba(148, 163, 184, 0.2);
        }
        .station-popover-head h3 {
            margin: 0;
            font-size: 14px;
            font-weight: 700;
            color: #f1f5f9;
            line-height: 1.3;
        }
        .station-popover-body {
            padding: 12px 12px 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .station-popover-hint {
            font-size: 11px;
            color: #94a3b8;
            line-height: 1.4;
            margin: 0 0 2px 0;
        }
        .station-check-row {
            display: flex;
            align-items: flex-start;
            gap: 10px;
            cursor: pointer;
            font-size: 13px;
            line-height: 1.35;
            color: #e2e8f0;
        }
        .station-check-row input {
            margin-top: 2px;
            flex-shrink: 0;
            width: 16px;
            height: 16px;
            accent-color: #6366f1;
            cursor: pointer;
        }
        .station-popover .btn-icon {
            background: transparent;
            border: none;
            color: #94a3b8;
            font-size: 22px;
            line-height: 1;
            cursor: pointer;
            padding: 0 4px;
            border-radius: 6px;
        }
        .station-popover .btn-icon:hover {
            color: #f1f5f9;
            background: rgba(148, 163, 184, 0.12);
        }
        .help-popover {
            width: min(380px, calc(100vw - 80px));
            max-height: min(86vh, 720px);
        }
        .help-subhead {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #94a3b8;
            margin: 14px 0 8px 0;
        }
        .help-subhead:first-child {
            margin-top: 0;
        }
        .help-legend-row {
            display: flex;
            gap: 12px;
            align-items: flex-start;
            margin-bottom: 12px;
        }
        .help-icon-wrap {
            flex: 0 0 52px;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 40px;
        }
        .help-desc {
            margin: 0;
            font-size: 12px;
            line-height: 1.45;
            color: #cbd5e1;
            flex: 1;
            min-width: 0;
        }
        .help-ship-type-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 8px 6px;
            margin-bottom: 4px;
        }
        .help-ship-cell {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
            text-align: center;
        }
        .help-ship-cell svg.ship-marker {
            width: 22px;
            height: 22px;
        }
        .help-ship-cell span {
            font-size: 9px;
            color: #94a3b8;
            line-height: 1.2;
        }
        .help-tb-line {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 8px;
            font-size: 12px;
            color: #cbd5e1;
            line-height: 1.4;
        }
        .help-tb-line .tool-btn {
            pointer-events: none;
            flex-shrink: 0;
            min-width: 40px;
            padding: 6px 8px;
        }
        .weather-summary-box {
            font-size: 12px;
            line-height: 1.45;
            color: #cbd5e1;
            padding: 10px 12px;
            margin-bottom: 10px;
            border-radius: 8px;
            background: rgba(30, 41, 59, 0.55);
            border: 1px solid rgba(148, 163, 184, 0.18);
        }
        .weather-summary-box p {
            margin: 0 0 6px 0;
        }
        .weather-summary-box p:last-child {
            margin-bottom: 0;
        }
        .weather-summary-muted {
            font-size: 10px;
            color: #94a3b8;
            margin-top: 8px;
        }
    </style>
</head>
<body>
    <div id="mapZoomDock" class="map-zoom-dock" role="region" aria-labelledby="lblScaleSection">
        <div class="controls-section-title" id="lblScaleSection">Map scale</div>
        <button type="button" class="zoom-dock-btn" id="btnZoomIn" title="Zoom in" aria-label="Zoom in">+</button>
        <div class="scale-zoom-row">
            <span class="scale-zoom-lbl" id="lblZoomLine"><span id="lblZoomPrefix">Zoom</span></span>
            <span class="scale-zoom-val" id="mapZoomValue">—</span>
        </div>
        <div class="scale-bar-wrap">
            <div class="scale-bar-track" aria-hidden="true">
                <div class="scale-bar-fill" id="scaleBarFill"></div>
            </div>
            <div class="scale-bar-caption" id="scaleBarCaption">—</div>
        </div>
        <button type="button" class="zoom-dock-btn" id="btnZoomOut" title="Zoom out" aria-label="Zoom out">−</button>
    </div>

    <div id="controls" class="panel controls-panel">
        <h4 id="titleControls">Controls</h4>
        <div class="controls-mode-section">
            <div class="controls-section-title" id="leftPanelStreamTitle">Feed</div>
            <div class="mode-btn-group" role="group" id="modeBtnGroup" aria-label="Stream mode">
                <button type="button" class="mode-btn mode-live-on" id="liveBtn">Live</button>
                <button type="button" class="mode-btn" id="pauseBtn">Pause</button>
                <button type="button" class="mode-btn" id="replayBtn">Replay 2m</button>
            </div>
        </div>
        <div class="controls-divider" aria-hidden="true"></div>
        <div class="controls-section-title controls-filters-title" id="lblFiltersSection">Filters</div>
        <div class="row">
            <label for="speedFilter" id="lblSpeed">Speed band</label>
            <select id="speedFilter">
                <option value="all">All</option>
                <option value="stopped">Stopped (&lt; 1 kn)</option>
                <option value="slow">Slow (1-4 kn)</option>
                <option value="medium">Medium (4-12 kn)</option>
                <option value="fast">Fast (&gt;= 12 kn)</option>
            </select>
        </div>
        <div class="row">
            <label for="typeFilter" id="lblTypeFilter">Ship type</label>
            <select id="typeFilter">
                <option value="all">All types</option>
                <option value="default">Unknown / other</option>
                <option value="tanker">Tanker</option>
                <option value="cargo">Cargo</option>
                <option value="passenger">Passenger</option>
                <option value="fishing">Fishing</option>
                <option value="towing">Towing / dredging</option>
                <option value="special">Special</option>
                <option value="other">Other</option>
            </select>
        </div>
        <div class="controls-divider" aria-hidden="true"></div>
        <div class="row">
            <label id="lblSelected">Selected:</label>
            <span id="selectedShip">-</span>
        </div>
        <div class="row">
            <button type="button" id="centerSelectedBtn">Center</button>
            <button type="button" id="clearSelectedBtn">Clear</button>
        </div>
    </div>

    <div id="mapToolbar" class="panel">
        <p class="toolbar-title" id="toolbarLayersTitle">Layers</p>
        <button type="button" class="tool-btn active" id="btnToolbarShips">&#128674;</button>
        <button type="button" class="tool-btn active" id="btnToolbarStations" aria-haspopup="dialog" aria-expanded="false">&#128205;</button>
        <button type="button" class="tool-btn" id="btnToolbarWeather" aria-haspopup="dialog" aria-expanded="false">&#127782;</button>
        <button type="button" class="tool-btn" id="btnOpenAnalytics">&#128202;</button>
        <button type="button" class="tool-btn" id="btnToolbarHelp" aria-haspopup="dialog" aria-expanded="false" title="Map legend">?</button>
    </div>

    <div id="mapHelpPopover" class="station-popover help-popover hidden" role="dialog" aria-labelledby="helpModalTitle" aria-hidden="true">
            <div class="station-popover-head">
                <h3 id="helpModalTitle">Map legend</h3>
                <button type="button" class="btn-icon" id="btnCloseHelpModal" title="Close" aria-label="Close">&times;</button>
            </div>
            <div class="station-popover-body">
                <p class="help-subhead" id="helpSubShipsEl"></p>
                <div class="help-legend-row">
                    <div class="help-icon-wrap">
                        <div class="cluster-bubble" style="transform: scale(0.82);">
                            <span class="cluster-emoji">&#128674;</span>
                            <span class="cluster-count">12</span>
                        </div>
                    </div>
                    <p class="help-desc" id="helpTxtShipCluster"></p>
                </div>
                <p class="help-desc" id="helpTxtShipTypesIntro" style="margin-bottom: 8px;"></p>
                <div class="help-ship-type-grid">
                    <div class="help-ship-cell">
                        <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(0deg);">
                            <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                            <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="#2563eb" stroke="#0f172a" stroke-width="1" stroke-linejoin="round"></path>
                            <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
                        </svg>
                        <span id="helpShipLblDefault"></span>
                    </div>
                    <div class="help-ship-cell">
                        <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(0deg);">
                            <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                            <rect x="10" y="8" width="4" height="8" rx="1" fill="rgba(255,255,255,0.28)" stroke="#0f172a" stroke-width="0.6"></rect>
                            <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="#f97316" stroke="#0f172a" stroke-width="1" stroke-linejoin="round"></path>
                            <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
                        </svg>
                        <span id="helpShipLblTanker"></span>
                    </div>
                    <div class="help-ship-cell">
                        <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(0deg);">
                            <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                            <rect x="7" y="10" width="10" height="5" rx="0.8" fill="none" stroke="#0f172a" stroke-width="0.85"></rect>
                            <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="#eab308" stroke="#0f172a" stroke-width="1" stroke-linejoin="round"></path>
                            <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
                        </svg>
                        <span id="helpShipLblCargo"></span>
                    </div>
                    <div class="help-ship-cell">
                        <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(0deg);">
                            <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                            <path d="M8 14 L16 14 M10 11 L14 11" stroke="#0f172a" stroke-width="0.9" stroke-linecap="round"></path>
                            <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="#a855f7" stroke="#0f172a" stroke-width="1" stroke-linejoin="round"></path>
                            <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
                        </svg>
                        <span id="helpShipLblPassenger"></span>
                    </div>
                    <div class="help-ship-cell">
                        <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(0deg);">
                            <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                            <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="#22c55e" stroke="#0f172a" stroke-width="1" stroke-linejoin="round"></path>
                            <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
                        </svg>
                        <span id="helpShipLblFishing"></span>
                    </div>
                    <div class="help-ship-cell">
                        <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(0deg);">
                            <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                            <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="#0f766e" stroke="#0f172a" stroke-width="1" stroke-linejoin="round"></path>
                            <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
                        </svg>
                        <span id="helpShipLblTowing"></span>
                    </div>
                    <div class="help-ship-cell">
                        <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(0deg);">
                            <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                            <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="#db2777" stroke="#0f172a" stroke-width="1" stroke-linejoin="round"></path>
                            <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
                        </svg>
                        <span id="helpShipLblSpecial"></span>
                    </div>
                    <div class="help-ship-cell">
                        <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(0deg);">
                            <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                            <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="#57534e" stroke="#0f172a" stroke-width="1" stroke-linejoin="round"></path>
                            <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
                        </svg>
                        <span id="helpShipLblOther"></span>
                    </div>
                </div>
                <p class="help-subhead" id="helpSubStationsEl"></p>
                <div class="help-legend-row">
                    <div class="help-icon-wrap">
                        <div class="station-cluster-bubble" style="transform: scale(0.9);">
                            <span class="station-cluster-emoji">&#9875;</span>
                            <span class="station-cluster-count">5</span>
                        </div>
                    </div>
                    <p class="help-desc" id="helpTxtStationCluster"></p>
                </div>
                <div class="help-legend-row">
                    <div class="help-icon-wrap">
                        <div style="width: 11px; height: 11px; background: #6366f1; border: 2px solid #0f172a; border-radius: 2px; box-shadow: 0 1px 3px rgba(0,0,0,0.45);"></div>
                    </div>
                    <p class="help-desc" id="helpTxtDotPort"></p>
                </div>
                <div class="help-legend-row">
                    <div class="help-icon-wrap">
                        <div style="width: 11px; height: 11px; background: #10b981; border: 2px solid #0f172a; border-radius: 2px; box-shadow: 0 1px 3px rgba(0,0,0,0.45);"></div>
                    </div>
                    <p class="help-desc" id="helpTxtDotBase"></p>
                </div>
                <div class="help-legend-row">
                    <div class="help-icon-wrap">
                        <div style="width: 11px; height: 11px; background: #eab308; border: 2px solid #0f172a; border-radius: 2px; box-shadow: 0 1px 3px rgba(0,0,0,0.45);"></div>
                    </div>
                    <p class="help-desc" id="helpTxtDotAton"></p>
                </div>
                <p class="help-subhead" id="helpSubToolbarEl"></p>
                <div class="help-tb-line"><span class="tool-btn">&#128674;</span><span id="helpTbShips"></span></div>
                <div class="help-tb-line"><span class="tool-btn">&#128205;</span><span id="helpTbStations"></span></div>
                <div class="help-tb-line"><span class="tool-btn">&#127782;</span><span id="helpTbWeather"></span></div>
                <div class="help-tb-line"><span class="tool-btn">&#128202;</span><span id="helpTbAnalytics"></span></div>
            </div>
    </div>

    <div id="weatherSettingsModal" class="station-popover hidden" role="dialog" aria-labelledby="weatherModalTitle" aria-hidden="true">
            <div class="station-popover-head">
                <h3 id="weatherModalTitle">Weather</h3>
                <button type="button" class="btn-icon" id="btnCloseWeatherModal" title="Close" aria-label="Close">&times;</button>
            </div>
            <div class="station-popover-body">
                <p class="station-popover-hint" id="weatherModalHint"></p>
                <div class="weather-summary-box" id="weatherSummaryBox">
                    <p id="weatherSummaryLine1">—</p>
                    <p id="weatherSummaryLine2">—</p>
                    <p id="weatherSummaryLine3" class="weather-summary-muted"></p>
                </div>
                <label class="station-check-row" for="chkWeatherTempOverlay">
                    <input type="checkbox" id="chkWeatherTempOverlay" />
                    <span id="lblWeatherTempOverlay">Temperature on map</span>
                </label>
                <label class="station-check-row" for="chkWeatherPrecipOverlay">
                    <input type="checkbox" id="chkWeatherPrecipOverlay" />
                    <span id="lblWeatherPrecipOverlay">Precipitation on map</span>
                </label>
            </div>
    </div>

    <div id="stationSettingsModal" class="station-popover hidden" role="dialog" aria-labelledby="stationModalTitle" aria-hidden="true">
            <div class="station-popover-head">
                <h3 id="stationModalTitle">Ports</h3>
                <button type="button" class="btn-icon" id="btnCloseStationModal" title="Close" aria-label="Close">&times;</button>
            </div>
            <div class="station-popover-body">
                <p class="station-popover-hint" id="stationModalHint"></p>
                <label class="station-check-row" for="chkStationPorts">
                    <input type="checkbox" id="chkStationPorts" checked />
                    <span id="lblStationPorts">Major ports (reference)</span>
                </label>
                <label class="station-check-row" for="chkStationBases">
                    <input type="checkbox" id="chkStationBases" checked />
                    <span id="lblStationBases">AIS base stations</span>
                </label>
                <label class="station-check-row" for="chkStationAton">
                    <input type="checkbox" id="chkStationAton" checked />
                    <span id="lblStationAton">Aids to navigation (AtoN)</span>
                </label>
            </div>
    </div>

    <div id="analyticsPanel" class="hidden" role="dialog" aria-labelledby="analyticsTitle" aria-hidden="true">
            <div class="analytics-modal-inner">
                <div class="analytics-head">
                    <span id="analyticsDragHandle" class="analytics-drag-handle" title="Drag">&#8942;&#8942;</span>
                    <div class="analytics-head-main">
                        <h2 id="analyticsTitle">Live analytics</h2>
                        <p class="analytics-meta" id="analyticsSubtitle">Fleet overview from AIS feed</p>
                    </div>
                    <div class="analytics-head-actions">
                        <button type="button" class="btn-icon" id="btnCloseAnalytics" title="Close">&times;</button>
                    </div>
                </div>
                <div class="analytics-tabs" role="tablist" aria-label="Analytics sections">
                    <button type="button" class="analytics-tab" role="tab" id="analyticsTabBtnOverview" aria-selected="true" aria-controls="analyticsTabPanelOverview" data-analytics-tab="overview">Overview</button>
                    <button type="button" class="analytics-tab" role="tab" id="analyticsTabBtnTypes" aria-selected="false" aria-controls="analyticsTabPanelTypes" data-analytics-tab="types">Ship types</button>
                    <button type="button" class="analytics-tab" role="tab" id="analyticsTabBtnAdvanced" aria-selected="false" aria-controls="analyticsTabPanelAdvanced" data-analytics-tab="advanced">Motion &amp; data</button>
                </div>
                <div id="analyticsTabPanelOverview" class="analytics-tab-panel" role="tabpanel" aria-labelledby="analyticsTabBtnOverview">
                <div class="stat-grid">
                    <div class="stat-card wide">
                        <div class="lbl" id="lblShownShips">Shown ships:</div>
                        <div class="val" id="total">0</div>
                    </div>
                    <div class="stat-card">
                        <div class="lbl" id="lblUps">Updates/sec:</div>
                        <div class="val val-sm" id="updatesPerSec">0.0</div>
                    </div>
                    <div class="stat-card">
                        <div class="lbl" id="lblLastUp">Last update:</div>
                        <div class="val val-sm" id="lastUpdate">-</div>
                    </div>
                    <div class="stat-card">
                        <div class="lbl" id="lblMode">Mode:</div>
                        <div class="val val-sm" id="modeLabel">LIVE</div>
                    </div>
                </div>
                <div class="lbl" id="lblSpeedMix" style="margin-bottom:8px;">Speed mix (filtered)</div>
                <div class="histogram">
                    <div class="histogram-row bar-stopped">
                        <div class="h-label"><span id="histLblStopped">Stopped</span><span id="histPctStopped">0%</span></div>
                        <div class="histogram-track"><div class="histogram-bar" id="histBarStopped"></div></div>
                    </div>
                    <div class="histogram-row bar-slow">
                        <div class="h-label"><span id="histLblSlow">Slow</span><span id="histPctSlow">0%</span></div>
                        <div class="histogram-track"><div class="histogram-bar" id="histBarSlow"></div></div>
                    </div>
                    <div class="histogram-row bar-medium">
                        <div class="h-label"><span id="histLblMedium">Medium</span><span id="histPctMedium">0%</span></div>
                        <div class="histogram-track"><div class="histogram-bar" id="histBarMedium"></div></div>
                    </div>
                    <div class="histogram-row bar-fast">
                        <div class="h-label"><span id="histLblFast">Fast</span><span id="histPctFast">0%</span></div>
                        <div class="histogram-track"><div class="histogram-bar" id="histBarFast"></div></div>
                    </div>
                </div>
                </div>
                <div id="analyticsTabPanelTypes" class="analytics-tab-panel hidden" role="tabpanel" aria-labelledby="analyticsTabBtnTypes">
                    <p class="analytics-section-hint" id="lblAnalyticsTypesHint"></p>
                    <div class="type-viz-toolbar" role="group" id="typeVizToolbar" aria-label="">
                        <span class="lbl-inline" id="lblTypeVizPrompt"></span>
                        <button type="button" class="analytics-viz-btn" id="btnTypeVizBars" aria-pressed="true">Bars</button>
                        <button type="button" class="analytics-viz-btn" id="btnTypeVizPie" aria-pressed="false">Pie</button>
                    </div>
                    <div id="shipTypeBreakdown"></div>
                </div>
                <div id="analyticsTabPanelAdvanced" class="analytics-tab-panel hidden" role="tabpanel" aria-labelledby="analyticsTabBtnAdvanced">
                    <p class="analytics-section-hint" id="lblAnalyticsAdvHint"></p>
                    <p class="adv-note" id="lblAnalyticsAdvSample"></p>
                    <div class="adv-grid">
                        <div class="stat-card">
                            <div class="lbl" id="lblAdvKnownType">Known AIS type</div>
                            <div class="val val-sm" id="advKnownTypeVal">—</div>
                        </div>
                        <div class="stat-card">
                            <div class="lbl" id="lblAdvMoving">Under way (≥1 kn)</div>
                            <div class="val val-sm" id="advMovingVal">—</div>
                        </div>
                        <div class="stat-card">
                            <div class="lbl" id="lblAdvStoppedShare">Stopped (&lt;1 kn)</div>
                            <div class="val val-sm" id="advStoppedShareVal">—</div>
                        </div>
                        <div class="stat-card">
                            <div class="lbl" id="lblAdvWithCog">Course over ground (COG)</div>
                            <div class="val val-sm" id="advWithCogVal">—</div>
                        </div>
                        <div class="stat-card">
                            <div class="lbl" id="lblAdvWithHeading">Hull heading (AIS)</div>
                            <div class="val val-sm" id="advWithHeadingVal">—</div>
                        </div>
                    </div>
                </div>
                <div class="analytics-status-row">
                    <span id="lblConn">Connection:</span>
                    <span class="status" id="status">Connecting...</span>
                </div>
            </div>
    </div>

    <div id="map"></div>

    <div id="bottomLangBar" class="panel bottom-lang-bar">
        <div class="lang-switch" id="bottomLangSwitch" role="group" aria-label="Language">
            <button type="button" class="lang-btn active" id="langEn" data-lang="en">EN</button>
            <button type="button" class="lang-btn" id="langRu" data-lang="ru">RU</button>
        </div>
    </div>

    <script>
        const WEATHER_ENABLED = __WEATHER_ENABLED__;
        const LANG_KEY = 'shipTrackerUiLang';
        let uiLang = 'en';
        try {
            uiLang = (localStorage.getItem(LANG_KEY) === 'ru') ? 'ru' : 'en';
        } catch (e) { }

        const STR = {
            en: {
                title: 'Ship Tracker RT - Real-Time',
                controls: 'Map & feed',
                leftPanelStream: 'Stream',
                scaleSection: 'Map scale',
                scaleZoom: 'Zoom',
                scaleKm: 'km',
                scaleM: 'm',
                modeGroupAria: 'Stream mode: live, pause, or replay',
                speed: 'Speed band',
                filtersSection: 'Filters',
                filterType: 'Ship type',
                filterTypeAll: 'All types',
                zoomInTitle: 'Zoom in',
                zoomOutTitle: 'Zoom out',
                mapZoomDockAria: 'Zoom and map scale',
                selected: 'Selected:',
                center: 'Center',
                clear: 'Clear',
                live: 'Live',
                pause: 'Pause',
                replay: 'Replay 2m',
                appTitle: 'Ship Tracker RT',
                analyticsTitle: 'Live analytics',
                analyticsSubtitle: 'Overview, ship-type mix, and motion metrics (same map filters: speed and type).',
                toolbarLayers: 'Layers',
                layerShips: 'Ships on map',
                layerStations: 'Ports & bases — panel next to toolbar',
                stationModalTitle: 'Ports & fixed AIS',
                stationModalHint: 'From zoom 7: only ports/bases in view; clustered like ships.',
                stationModalPorts: 'Major ports (reference list)',
                stationModalBases: 'AIS base stations (message 4)',
                stationModalAton: 'Aids to navigation — AtoN (message 21)',
                stationModalClose: 'Close',
                layerWeather: 'Weather — forecast at map center & layers',
                stationBase: 'AIS base station',
                stationAton: 'Aid to navigation (AtoN)',
                stationPort: 'Major port (reference)',
                popupStationTypeCode: 'AIS type code',
                openAnalytics: 'Open analytics',
                closeAnalytics: 'Close',
                connLabel: 'Connection:',
                speedMixTitle: 'Speed mix (filtered)',
                weatherDisabledHint: 'Set OPENWEATHERMAP_API_KEY to enable weather tiles.',
                analyticsOpenBtn: 'Analytics / pin panel',
                dragAnalyticsHint: 'Drag to move',
                popupType: 'Type',
                typeUnknown: 'Unknown / other',
                typeTanker: 'Tanker',
                typeCargo: 'Cargo',
                typePassenger: 'Passenger',
                typeFishing: 'Fishing',
                typeTowing: 'Towing / dredging',
                typeSpecial: 'Special (pilot, SAR, …)',
                typeOther: 'Other',
                statConnecting: 'Connecting...',
                statConnected: 'Connected',
                statError: 'Error',
                statDisconnected: 'Disconnected',
                shownShips: 'Shown ships:',
                ups: 'Updates/sec:',
                lastUp: 'Last update:',
                mode: 'Mode:',
                kn: 'kn',
                modeLive: 'LIVE',
                modePaused: 'PAUSED',
                modeReplay: 'REPLAY',
                speedAll: 'All',
                speedStopped: 'Stopped (< 1 kn)',
                speedSlow: 'Slow (1-4 kn)',
                speedMedium: 'Medium (4-12 kn)',
                speedFast: 'Fast (>= 12 kn)',
                popupShip: 'Ship ID:',
                popupLat: 'Lat:',
                popupLon: 'Lon:',
                popupCourse: 'Course:',
                popupSpeed: 'Speed:',
                popupHeading: 'Heading:',
                popupSpeedZero: 'Speed: 0.0 kn',
                helpBtnTitle: 'Map legend',
                helpModalTitle: 'Map legend',
                helpSubShips: 'Ships',
                helpTxtShipCluster: 'Ship cluster — several vessels in one area. Click to zoom in. Appears when zoomed out.',
                helpTxtShipTypesIntro: 'Single ship — hull color follows AIS ship type when known. Extra marks on the hull (tanker / cargo / passenger) match the map.',
                helpSubStations: 'Ports & AIS stations',
                helpTxtStationCluster: 'Station cluster — several ports or base/AtoN points. Click to zoom in.',
                helpTxtDotPort: 'Indigo square — reference major port (static list).',
                helpTxtDotBase: 'Green square — AIS base station (message 4).',
                helpTxtDotAton: 'Yellow square — aid to navigation, AtoN (message 21).',
                helpSubToolbar: 'Right toolbar',
                helpTbShips: 'Toggle ship layer on the map.',
                helpTbStations: 'Ports and stations — opens filters (types).',
                helpTbWeather: 'Weather: temperature and precipitation at map center; optional temperature and precipitation map layers.',
                helpTbAnalytics: 'Analytics panel with tabs: overview, ship types, motion & navigation fields.',
                analyticsTabsAria: 'Analytics sections',
                analyticsTabOverview: 'Overview',
                analyticsTabTypes: 'Ship types',
                analyticsTabAdvanced: 'Motion & data',
                analyticsTypesHint: 'Ships per AIS type category (same filters as the map). Counts use the full filtered fleet.',
                analyticsTypesEmpty: 'No ships match the current filter.',
                analyticsVizPrompt: 'View as',
                analyticsTypeVizAria: 'Chart type for ship categories',
                analyticsVizBars: 'Bar chart',
                analyticsVizPie: 'Pie chart',
                analyticsAdvHint: 'Navigation fields and motion share (same map filters). COG/heading percentages use the full fleet when it is small, or a sample when very large.',
                analyticsAdvSampleOn: 'COG/heading/motion shares: sample of ~{n} ships (fleet over {m}).',
                analyticsAdvSampleOff: 'COG/heading/motion shares: full filtered fleet.',
                analyticsLblAdvKnownType: 'Known AIS type',
                analyticsLblAdvMoving: 'Under way (≥ 1 kn)',
                analyticsLblAdvStoppedShare: 'Stopped (< 1 kn)',
                analyticsLblAdvWithCog: 'Course over ground (COG)',
                analyticsLblAdvWithHeading: 'Hull heading (AIS)',
                weatherModalTitle: 'Weather',
                weatherModalHint: 'Figures for the center of the visible map (viewport), not a fixed world point. OpenWeatherMap API; overlays are tiles via this server.',
                weatherLblTempOverlay: 'Temperature overlay on map',
                weatherLblPrecipOverlay: 'Precipitation overlay on map',
                weatherSummaryLoading: 'Loading…',
                weatherSummaryErr: 'Could not load weather.',
                weatherSummaryNoData: 'No data.',
                weatherTemp: 'Temperature',
                weatherFeels: 'Feels like',
                weatherPrecip: 'Precipitation',
                weatherPrecipNone: 'none (0 mm/h)',
                weatherHumidity: 'Humidity',
                weatherWind: 'Wind',
                weatherWindUnit: 'm/s',
                weatherMmH: 'mm/h',
                weatherSourceOneCall: 'API: One Call 3.0',
                weatherSource25: 'API: Current weather 2.5',
                langGroupAria: 'Interface language',
            },
            ru: {
                title: 'Ship Tracker RT - онлайн',
                controls: 'Карта и поток',
                leftPanelStream: 'Поток',
                scaleSection: 'Масштаб карты',
                scaleZoom: 'Зум',
                scaleKm: 'км',
                scaleM: 'м',
                modeGroupAria: 'Режим потока: онлайн, пауза или повтор',
                speed: 'Диапазон скорости',
                filtersSection: 'Фильтры',
                filterType: 'Тип судна',
                filterTypeAll: 'Все типы',
                zoomInTitle: 'Приблизить',
                zoomOutTitle: 'Отдалить',
                mapZoomDockAria: 'Масштаб и линейка карты',
                selected: 'Выбрано:',
                center: 'Центрировать',
                clear: 'Сброс',
                live: 'Онлайн',
                pause: 'Пауза',
                replay: 'Повтор 2 мин',
                appTitle: 'Ship Tracker RT',
                analyticsTitle: 'Аналитика',
                analyticsSubtitle: 'Обзор, типы судов и движение (те же фильтры карты: скорость и тип).',
                toolbarLayers: 'Слои',
                layerShips: 'Суда на карте',
                layerStations: 'Порты и базы — панель у правой колонки',
                stationModalTitle: 'Порты и стационарные объекты AIS',
                stationModalHint: 'С 7-го зума: только объекты в кадре; кластеры как у судов.',
                stationModalPorts: 'Крупные порты (справочный список)',
                stationModalBases: 'Базовые станции AIS (сообщение 4)',
                stationModalAton: 'Навигационные знаки — AtoN (сообщение 21)',
                stationModalClose: 'Закрыть',
                layerWeather: 'Погода — прогноз в центре карты и слои',
                stationBase: 'Базовая станция AIS',
                stationAton: 'Навигационный знак (AtoN)',
                stationPort: 'Крупный порт (справочно)',
                popupStationTypeCode: 'Код типа AIS',
                openAnalytics: 'Открыть аналитику',
                closeAnalytics: 'Закрыть',
                connLabel: 'Соединение:',
                speedMixTitle: 'Смесь скоростей (по фильтру)',
                weatherDisabledHint: 'Задайте OPENWEATHERMAP_API_KEY для слоя погоды.',
                analyticsOpenBtn: 'Аналитика (панель)',
                dragAnalyticsHint: 'Перетащить',
                popupType: 'Тип',
                typeUnknown: 'Неизв. / прочее',
                typeTanker: 'Танкер',
                typeCargo: 'Грузовое',
                typePassenger: 'Пассажирское',
                typeFishing: 'Рыболовное',
                typeTowing: 'Буксир / дноуглуб.',
                typeSpecial: 'Спец. (лоцман, поиск, …)',
                typeOther: 'Прочее',
                statConnecting: 'Подключение...',
                statConnected: 'Подключено',
                statError: 'Ошибка',
                statDisconnected: 'Нет соединения',
                shownShips: 'Показано судов:',
                ups: 'Обновл./сек:',
                lastUp: 'Посл. обновление:',
                mode: 'Режим:',
                kn: 'уз',
                modeLive: 'ОНЛАЙН',
                modePaused: 'ПАУЗА',
                modeReplay: 'ПОВТОР',
                speedAll: 'Все',
                speedStopped: 'Стоят (< 1 уз)',
                speedSlow: 'Медленные (1-4 уз)',
                speedMedium: 'Средние (4-12 уз)',
                speedFast: 'Быстрые (>= 12 уз)',
                popupShip: 'ID судна:',
                popupLat: 'Шир.:',
                popupLon: 'Долг.:',
                popupCourse: 'Курс:',
                popupSpeed: 'Скорость:',
                popupHeading: 'Направление:',
                popupSpeedZero: 'Скорость: 0.0 уз',
                helpBtnTitle: 'Обозначения карты',
                helpModalTitle: 'Обозначения карты',
                helpSubShips: 'Суда',
                helpTxtShipCluster: 'Кластер судов — несколько судов в одной области. Клик — приблизить. Показывается на малых масштабах.',
                helpTxtShipTypesIntro: 'Одно судно — цвет корпуса по типу AIS (если известен). Доп. чертежи на корпусе (танкер / груз / пассажир) как на карте.',
                helpSubStations: 'Порты и стационарные объекты AIS',
                helpTxtStationCluster: 'Кластер станций — несколько портов или баз/AtoN. Клик — приблизить.',
                helpTxtDotPort: 'Индиго квадрат — крупный порт (справочный список).',
                helpTxtDotBase: 'Зелёный квадрат — базовая станция AIS (сообщение 4).',
                helpTxtDotAton: 'Жёлтый квадрат — навигационный знак, AtoN (сообщение 21).',
                helpSubToolbar: 'Панель справа',
                helpTbShips: 'Показать или скрыть слой судов.',
                helpTbStations: 'Порты и станции — фильтры типов.',
                helpTbWeather: 'Погода: температура и осадки в центре карты; опционально слои температуры и осадков.',
                helpTbAnalytics: 'Аналитика с вкладками: обзор, типы судов, движение и поля навигации.',
                analyticsTabsAria: 'Разделы аналитики',
                analyticsTabOverview: 'Обзор',
                analyticsTabTypes: 'Типы судов',
                analyticsTabAdvanced: 'Движение и данные',
                analyticsTypesHint: 'Число судов по категории типа AIS (те же фильтры, что на карте). Подсчёт по полному отфильтрованному набору.',
                analyticsTypesEmpty: 'Нет судов по текущему фильтру.',
                analyticsVizPrompt: 'Показать как',
                analyticsTypeVizAria: 'Тип диаграммы по категориям судов',
                analyticsVizBars: 'Столбики',
                analyticsVizPie: 'Круговая',
                analyticsAdvHint: 'Навигация и доля «стоят / в движении» (те же фильтры карты). Доли COG/heading — по полному флоту или по выборке при очень большом числе судов.',
                analyticsAdvSampleOn: 'Доли COG/heading/движения: выборка ~{n} судов (флот > {m}).',
                analyticsAdvSampleOff: 'Доли COG/heading/движения: полный отфильтрованный флот.',
                analyticsLblAdvKnownType: 'Известный тип AIS',
                analyticsLblAdvMoving: 'В движении (≥ 1 уз)',
                analyticsLblAdvStoppedShare: 'Стоят (< 1 уз)',
                analyticsLblAdvWithCog: 'Курс по грунту (COG)',
                analyticsLblAdvWithHeading: 'Курс корпуса (heading AIS)',
                weatherModalTitle: 'Погода',
                weatherModalHint: 'Данные для центра видимой области (экрана), не фиксированной точки мира. OpenWeatherMap; слои — тайлы через сервер.',
                weatherLblTempOverlay: 'Слой температуры на карте',
                weatherLblPrecipOverlay: 'Слой осадков на карте',
                weatherSummaryLoading: 'Загрузка…',
                weatherSummaryErr: 'Не удалось загрузить погоду.',
                weatherSummaryNoData: 'Нет данных.',
                weatherTemp: 'Температура',
                weatherFeels: 'Ощущается как',
                weatherPrecip: 'Осадки',
                weatherPrecipNone: 'нет (0 мм/ч)',
                weatherHumidity: 'Влажность',
                weatherWind: 'Ветер',
                weatherWindUnit: 'м/с',
                weatherMmH: 'мм/ч',
                weatherSourceOneCall: 'API: One Call 3.0',
                weatherSource25: 'API: Текущая погода 2.5',
                langGroupAria: 'Язык интерфейса',
            },
        };

        function t() {
            return STR[uiLang] || STR.en;
        }

        const map = L.map('map', { zoomControl: false }).setView([20, 0], 2);
        const shipsMarkerPane = map.getPane('markerPane');
        if (shipsMarkerPane) {
            shipsMarkerPane.style.zIndex = '590';
        }
        map.createPane('weatherPaneTemp');
        const weatherPaneTempEl = map.getPane('weatherPaneTemp');
        if (weatherPaneTempEl) {
            weatherPaneTempEl.style.zIndex = '398';
            weatherPaneTempEl.style.pointerEvents = 'none';
        }
        map.createPane('weatherPane');
        const weatherPaneEl = map.getPane('weatherPane');
        if (weatherPaneEl) {
            weatherPaneEl.style.zIndex = '400';
            weatherPaneEl.style.pointerEvents = 'none';
        }
        map.createPane('stationsTopPane');
        const stationsTopPaneEl = map.getPane('stationsTopPane');
        if (stationsTopPaneEl) {
            stationsTopPaneEl.style.zIndex = '620';
        }

        const SCALE_BAR_MAX_PX = 40;

        function roundNiceMeters(d) {
            if (!isFinite(d) || d <= 0) return 1;
            const exp = Math.floor(Math.log10(d));
            const pow = Math.pow(10, exp);
            const f = d / pow;
            let n = 1;
            if (f >= 5) n = 5;
            else if (f >= 2) n = 2;
            return n * pow;
        }

        function formatScaleDistanceMeters(m, tt) {
            if (m >= 1000) {
                const km = m / 1000;
                const txt = km >= 10 ? String(Math.round(km)) : String(Math.round(km * 10) / 10).replace(/\.0$/, '');
                return txt + '\u00a0' + tt.scaleKm;
            }
            return String(Math.round(m)) + '\u00a0' + tt.scaleM;
        }

        function updateMapScaleIndicator() {
            const zEl = document.getElementById('mapZoomValue');
            const fill = document.getElementById('scaleBarFill');
            const cap = document.getElementById('scaleBarCaption');
            if (!zEl || !fill || !cap) return;
            const z = map.getZoom();
            const lat = map.getCenter().lat;
            const mpp = 156543.03392 * Math.cos(lat * Math.PI / 180) / Math.pow(2, z);
            const approx = mpp * SCALE_BAR_MAX_PX;
            const niceM = roundNiceMeters(approx);
            const barPx = Math.min(SCALE_BAR_MAX_PX, Math.max(4, niceM / mpp));
            const zStr = Number.isInteger(z) ? String(z) : String(Math.round(z * 10) / 10).replace(/\.0$/, '');
            zEl.textContent = zStr;
            fill.style.width = barPx + 'px';
            cap.textContent = '~\u00a0' + formatScaleDistanceMeters(niceM, t());
        }

        map.whenReady(updateMapScaleIndicator);

        const WEATHER_STORE_KEY = 'shipTrackerWeatherLayers';
        let weatherTempLayer = null;
        let weatherPrecipLayer = null;
        let weatherTempOn = false;
        let weatherPrecipOn = false;
        let weatherSummaryTimer = null;

        function loadWeatherOverlayPrefs() {
            try {
                const raw = localStorage.getItem(WEATHER_STORE_KEY);
                if (!raw) return { temp: false, precip: false };
                const o = JSON.parse(raw);
                return { temp: !!o.temp, precip: !!o.precip };
            } catch (e) {
                return { temp: false, precip: false };
            }
        }

        function persistWeatherOverlays() {
            try {
                localStorage.setItem(
                    WEATHER_STORE_KEY,
                    JSON.stringify({ temp: weatherTempOn, precip: weatherPrecipOn })
                );
            } catch (e) { }
        }

        function updateWeatherToolbarBtn() {
            const wb = document.getElementById('btnToolbarWeather');
            if (wb) wb.classList.toggle('active', WEATHER_ENABLED && (weatherTempOn || weatherPrecipOn));
        }

        function setWeatherTempLayer(on) {
            if (!WEATHER_ENABLED) return;
            weatherTempOn = !!on;
            if (weatherTempOn) {
                if (!weatherTempLayer) {
                    weatherTempLayer = L.tileLayer('/api/weather/tiles/temp_new/{z}/{x}/{y}.png', {
                        pane: 'weatherPaneTemp',
                        opacity: 0.55,
                        maxZoom: 19,
                        attribution: 'OpenWeatherMap'
                    });
                }
                if (!map.hasLayer(weatherTempLayer)) weatherTempLayer.addTo(map);
            } else if (weatherTempLayer && map.hasLayer(weatherTempLayer)) {
                map.removeLayer(weatherTempLayer);
            }
            const ch = document.getElementById('chkWeatherTempOverlay');
            if (ch) ch.checked = weatherTempOn;
            persistWeatherOverlays();
            updateWeatherToolbarBtn();
        }

        function setWeatherPrecipLayer(on) {
            if (!WEATHER_ENABLED) return;
            weatherPrecipOn = !!on;
            if (weatherPrecipOn) {
                if (!weatherPrecipLayer) {
                    weatherPrecipLayer = L.tileLayer('/api/weather/tiles/precipitation_new/{z}/{x}/{y}.png', {
                        pane: 'weatherPane',
                        opacity: 0.5,
                        maxZoom: 19,
                        attribution: 'OpenWeatherMap'
                    });
                }
                if (!map.hasLayer(weatherPrecipLayer)) weatherPrecipLayer.addTo(map);
            } else if (weatherPrecipLayer && map.hasLayer(weatherPrecipLayer)) {
                map.removeLayer(weatherPrecipLayer);
            }
            const ch = document.getElementById('chkWeatherPrecipOverlay');
            if (ch) ch.checked = weatherPrecipOn;
            persistWeatherOverlays();
            updateWeatherToolbarBtn();
        }

        function scheduleWeatherSummaryRefresh() {
            if (!WEATHER_ENABLED) return;
            const m = document.getElementById('weatherSettingsModal');
            if (!m || m.classList.contains('hidden')) return;
            clearTimeout(weatherSummaryTimer);
            weatherSummaryTimer = setTimeout(function () {
                fetchWeatherSummaryForCenter();
            }, 550);
        }

        async function fetchWeatherSummaryForCenter() {
            if (!WEATHER_ENABLED) return;
            const m = document.getElementById('weatherSettingsModal');
            if (!m || m.classList.contains('hidden')) return;
            const tt = t();
            const l1 = document.getElementById('weatherSummaryLine1');
            const l2 = document.getElementById('weatherSummaryLine2');
            const l3 = document.getElementById('weatherSummaryLine3');
            if (l1) l1.textContent = tt.weatherSummaryLoading;
            if (l2) l2.textContent = '';
            if (l3) l3.textContent = '';
            const c = map.getCenter();
            const url = '/api/weather/current?lat=' + encodeURIComponent(c.lat) + '&lon=' + encodeURIComponent(c.lng) + '&lang=' + encodeURIComponent(uiLang);
            try {
                const res = await fetch(url);
                if (!res.ok) {
                    if (l1) l1.textContent = tt.weatherSummaryErr;
                    return;
                }
                const d = await res.json();
                if (d.error) {
                    if (l1) l1.textContent = tt.weatherSummaryErr;
                    return;
                }
                if (d.temp_c === undefined || d.temp_c === null) {
                    if (l1) l1.textContent = tt.weatherSummaryNoData;
                    return;
                }
                const tStr = (Math.round(Number(d.temp_c) * 10) / 10) + ' °C';
                if (l1) l1.textContent = tt.weatherTemp + ': ' + tStr + (d.description ? ' — ' + d.description : '');
                let precipPart = tt.weatherPrecip + ': ';
                if (d.precip_mm_h !== undefined && d.precip_mm_h !== null && Number(d.precip_mm_h) > 0) {
                    precipPart += (Math.round(Number(d.precip_mm_h) * 10) / 10) + ' ' + tt.weatherMmH;
                } else {
                    precipPart += tt.weatherPrecipNone;
                }
                const parts2 = [precipPart];
                if (d.feels_like_c !== undefined && d.feels_like_c !== null) {
                    parts2.push(tt.weatherFeels + ': ' + (Math.round(Number(d.feels_like_c) * 10) / 10) + ' °C');
                }
                if (l2) l2.textContent = parts2.join(' · ');
                const meta = [];
                if (d.humidity != null) meta.push(tt.weatherHumidity + ' ' + d.humidity + '%');
                if (d.wind_speed_ms != null) {
                    meta.push(tt.weatherWind + ' ' + (Math.round(Number(d.wind_speed_ms) * 10) / 10) + ' ' + tt.weatherWindUnit);
                }
                if (d.source === 'onecall3') meta.push(tt.weatherSourceOneCall);
                else if (d.source === 'weather25') meta.push(tt.weatherSource25);
                if (l3) l3.textContent = meta.join(' · ');
            } catch (err) {
                if (l1) l1.textContent = tt.weatherSummaryErr;
            }
        }

        let weatherPopoverOutsideHandler = null;

        function positionWeatherPopover() {
            const toolbar = document.getElementById('mapToolbar');
            const pop = document.getElementById('weatherSettingsModal');
            if (!toolbar || !pop) return;
            const r = toolbar.getBoundingClientRect();
            const gap = 8;
            const w = Math.min(300, window.innerWidth - 16);
            let left = r.left - gap - w;
            left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
            const top = Math.max(8, Math.min(r.top, window.innerHeight - 120));
            pop.style.top = top + 'px';
            pop.style.left = left + 'px';
            pop.style.right = 'auto';
        }

        function bindWeatherPopoverOutsideClose() {
            if (weatherPopoverOutsideHandler) return;
            weatherPopoverOutsideHandler = function (e) {
                const pop = document.getElementById('weatherSettingsModal');
                const btn = document.getElementById('btnToolbarWeather');
                if (!pop || pop.classList.contains('hidden')) return;
                if (pop.contains(e.target) || (btn && btn.contains(e.target))) return;
                closeWeatherSettingsModal();
            };
            document.addEventListener('mousedown', weatherPopoverOutsideHandler, true);
        }

        function unbindWeatherPopoverOutsideClose() {
            if (!weatherPopoverOutsideHandler) return;
            document.removeEventListener('mousedown', weatherPopoverOutsideHandler, true);
            weatherPopoverOutsideHandler = null;
        }

        function openWeatherSettingsModal() {
            closeStationSettingsModal();
            closeMapHelpModal();
            positionWeatherPopover();
            const m = document.getElementById('weatherSettingsModal');
            const btn = document.getElementById('btnToolbarWeather');
            const ct = document.getElementById('chkWeatherTempOverlay');
            const cp = document.getElementById('chkWeatherPrecipOverlay');
            if (ct) ct.checked = weatherTempOn;
            if (cp) cp.checked = weatherPrecipOn;
            if (m) {
                m.classList.remove('hidden');
                m.setAttribute('aria-hidden', 'false');
            }
            if (btn) btn.setAttribute('aria-expanded', 'true');
            bindWeatherPopoverOutsideClose();
            fetchWeatherSummaryForCenter();
        }

        function closeWeatherSettingsModal() {
            const m = document.getElementById('weatherSettingsModal');
            const btn = document.getElementById('btnToolbarWeather');
            if (m) {
                m.classList.add('hidden');
                m.setAttribute('aria-hidden', 'true');
            }
            if (btn) btn.setAttribute('aria-expanded', 'false');
            unbindWeatherPopoverOutsideClose();
        }

        function toggleWeatherSettingsPopover() {
            const m = document.getElementById('weatherSettingsModal');
            if (m && !m.classList.contains('hidden')) {
                closeWeatherSettingsModal();
            } else {
                openWeatherSettingsModal();
            }
        }

        let shipsVisible = true;
        const STATION_FILTERS_KEY = 'shipTrackerStationFilters';
        let stationShowPorts = true;
        let stationShowBases = true;
        let stationShowAton = true;

        function stationFiltersAny() {
            return stationShowPorts || stationShowBases || stationShowAton;
        }

        function stationKindAllowed(kind) {
            if (kind === 'major_port') return stationShowPorts;
            if (kind === 'ais_base') return stationShowBases;
            if (kind === 'ais_aton') return stationShowAton;
            return false;
        }

        function readStationFiltersFromUI() {
            const p = document.getElementById('chkStationPorts');
            const b = document.getElementById('chkStationBases');
            const a = document.getElementById('chkStationAton');
            if (p) stationShowPorts = !!p.checked;
            if (b) stationShowBases = !!b.checked;
            if (a) stationShowAton = !!a.checked;
        }

        function persistStationFilters() {
            try {
                localStorage.setItem(
                    STATION_FILTERS_KEY,
                    JSON.stringify({
                        ports: stationShowPorts,
                        bases: stationShowBases,
                        aton: stationShowAton,
                    })
                );
            } catch (e) { }
        }

        function loadStationFiltersFromStorage() {
            try {
                const raw = localStorage.getItem(STATION_FILTERS_KEY);
                if (!raw) return;
                const o = JSON.parse(raw);
                if (typeof o.ports === 'boolean') stationShowPorts = o.ports;
                if (typeof o.bases === 'boolean') stationShowBases = o.bases;
                if (typeof o.aton === 'boolean') stationShowAton = o.aton;
            } catch (e) { }
        }

        function applyStationFilters() {
            readStationFiltersFromUI();
            persistStationFilters();
            const tb = document.getElementById('btnToolbarStations');
            if (tb) tb.classList.toggle('active', stationFiltersAny());
            if (!stationFiltersAny()) {
                lastStationSyncSig = '';
                stationLayer.clearLayers();
                Object.keys(stationMarkers).forEach((k) => delete stationMarkers[k]);
                if (map.hasLayer(stationLayer)) map.removeLayer(stationLayer);
            } else {
                if (!map.hasLayer(stationLayer)) stationLayer.addTo(map);
                if (latestLiveFrame && Array.isArray(latestLiveFrame.stations)) {
                    syncStationMarkers(latestLiveFrame.stations, { force: true });
                }
            }
        }

        let stationPopoverOutsideHandler = null;

        function positionStationPopover() {
            const toolbar = document.getElementById('mapToolbar');
            const pop = document.getElementById('stationSettingsModal');
            if (!toolbar || !pop) return;
            const r = toolbar.getBoundingClientRect();
            const gap = 8;
            const w = Math.min(280, window.innerWidth - 16);
            let left = r.left - gap - w;
            left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
            const top = Math.max(8, Math.min(r.top, window.innerHeight - 120));
            pop.style.top = top + 'px';
            pop.style.left = left + 'px';
            pop.style.right = 'auto';
        }

        function bindStationPopoverOutsideClose() {
            if (stationPopoverOutsideHandler) return;
            stationPopoverOutsideHandler = function (e) {
                const pop = document.getElementById('stationSettingsModal');
                const btn = document.getElementById('btnToolbarStations');
                if (!pop || pop.classList.contains('hidden')) return;
                if (pop.contains(e.target) || (btn && btn.contains(e.target))) return;
                closeStationSettingsModal();
            };
            document.addEventListener('mousedown', stationPopoverOutsideHandler, true);
        }

        function unbindStationPopoverOutsideClose() {
            if (!stationPopoverOutsideHandler) return;
            document.removeEventListener('mousedown', stationPopoverOutsideHandler, true);
            stationPopoverOutsideHandler = null;
        }

        function openStationSettingsModal() {
            closeMapHelpModal();
            closeWeatherSettingsModal();
            const cp = document.getElementById('chkStationPorts');
            const cb = document.getElementById('chkStationBases');
            const ca = document.getElementById('chkStationAton');
            if (cp) cp.checked = stationShowPorts;
            if (cb) cb.checked = stationShowBases;
            if (ca) ca.checked = stationShowAton;
            positionStationPopover();
            const m = document.getElementById('stationSettingsModal');
            const btn = document.getElementById('btnToolbarStations');
            if (m) {
                m.classList.remove('hidden');
                m.setAttribute('aria-hidden', 'false');
            }
            if (btn) btn.setAttribute('aria-expanded', 'true');
            bindStationPopoverOutsideClose();
        }

        function closeStationSettingsModal() {
            const m = document.getElementById('stationSettingsModal');
            const btn = document.getElementById('btnToolbarStations');
            if (m) {
                m.classList.add('hidden');
                m.setAttribute('aria-hidden', 'true');
            }
            if (btn) btn.setAttribute('aria-expanded', 'false');
            unbindStationPopoverOutsideClose();
        }

        function toggleStationSettingsPopover() {
            const m = document.getElementById('stationSettingsModal');
            if (m && !m.classList.contains('hidden')) {
                closeStationSettingsModal();
            } else {
                openStationSettingsModal();
            }
        }

        let helpPopoverOutsideHandler = null;

        function positionHelpPopover() {
            const toolbar = document.getElementById('mapToolbar');
            const pop = document.getElementById('mapHelpPopover');
            if (!toolbar || !pop) return;
            const r = toolbar.getBoundingClientRect();
            const gap = 8;
            const w = Math.min(380, window.innerWidth - 16);
            let left = r.left - gap - w;
            left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
            const top = Math.max(8, Math.min(r.top, window.innerHeight - 80));
            pop.style.top = top + 'px';
            pop.style.left = left + 'px';
            pop.style.right = 'auto';
        }

        function bindHelpPopoverOutsideClose() {
            if (helpPopoverOutsideHandler) return;
            helpPopoverOutsideHandler = function (e) {
                const pop = document.getElementById('mapHelpPopover');
                const btn = document.getElementById('btnToolbarHelp');
                if (!pop || pop.classList.contains('hidden')) return;
                if (pop.contains(e.target) || (btn && btn.contains(e.target))) return;
                closeMapHelpModal();
            };
            document.addEventListener('mousedown', helpPopoverOutsideHandler, true);
        }

        function unbindHelpPopoverOutsideClose() {
            if (!helpPopoverOutsideHandler) return;
            document.removeEventListener('mousedown', helpPopoverOutsideHandler, true);
            helpPopoverOutsideHandler = null;
        }

        function openMapHelpModal() {
            closeStationSettingsModal();
            closeWeatherSettingsModal();
            positionHelpPopover();
            const m = document.getElementById('mapHelpPopover');
            const btn = document.getElementById('btnToolbarHelp');
            if (m) {
                m.classList.remove('hidden');
                m.setAttribute('aria-hidden', 'false');
            }
            if (btn) btn.setAttribute('aria-expanded', 'true');
            bindHelpPopoverOutsideClose();
        }

        function closeMapHelpModal() {
            const m = document.getElementById('mapHelpPopover');
            const btn = document.getElementById('btnToolbarHelp');
            if (m) {
                m.classList.add('hidden');
                m.setAttribute('aria-hidden', 'true');
            }
            if (btn) btn.setAttribute('aria-expanded', 'false');
            unbindHelpPopoverOutsideClose();
        }

        function toggleMapHelpModal() {
            const m = document.getElementById('mapHelpPopover');
            if (m && !m.classList.contains('hidden')) {
                closeMapHelpModal();
            } else {
                openMapHelpModal();
            }
        }

        function isAnalyticsVisible() {
            const p = document.getElementById('analyticsPanel');
            return p && !p.classList.contains('hidden');
        }

        function showAnalyticsPanel() {
            const p = document.getElementById('analyticsPanel');
            if (!p) return;
            p.classList.remove('hidden');
            p.setAttribute('aria-hidden', 'false');
            const b = document.getElementById('btnOpenAnalytics');
            if (b) b.classList.add('active');
        }

        function hideAnalyticsPanel() {
            const p = document.getElementById('analyticsPanel');
            if (!p) return;
            p.classList.add('hidden');
            p.setAttribute('aria-hidden', 'true');
            const b = document.getElementById('btnOpenAnalytics');
            if (b) b.classList.remove('active');
        }

        function toggleAnalyticsPanel() {
            if (isAnalyticsVisible()) hideAnalyticsPanel();
            else showAnalyticsPanel();
        }

        (function setupAnalyticsDrag() {
            const panel = document.getElementById('analyticsPanel');
            const handle = document.getElementById('analyticsDragHandle');
            if (!panel || !handle) return;
            let dragging = false;
            let start = { x: 0, y: 0, l: 0, t: 0 };
            handle.addEventListener('mousedown', (e) => {
                if (e.button !== 0) return;
                e.preventDefault();
                const r = panel.getBoundingClientRect();
                if (!panel.dataset.dragInit) {
                    panel.style.left = r.left + 'px';
                    panel.style.top = r.top + 'px';
                    panel.style.right = 'auto';
                    panel.dataset.dragInit = '1';
                }
                dragging = true;
                start = {
                    x: e.clientX,
                    y: e.clientY,
                    l: parseFloat(panel.style.left) || r.left,
                    t: parseFloat(panel.style.top) || r.top,
                };
                handle.style.cursor = 'grabbing';
            });
            document.addEventListener('mousemove', (e) => {
                if (!dragging) return;
                const dx = e.clientX - start.x;
                const dy = e.clientY - start.y;
                const w = panel.offsetWidth;
                const h = panel.offsetHeight;
                const nl = Math.max(8, Math.min(window.innerWidth - w - 8, start.l + dx));
                const nt = Math.max(8, Math.min(window.innerHeight - h - 8, start.t + dy));
                panel.style.left = nl + 'px';
                panel.style.top = nt + 'px';
            });
            document.addEventListener('mouseup', () => {
                if (!dragging) return;
                dragging = false;
                handle.style.cursor = 'grab';
            });
        })();

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
            subdomains: 'abc',
            maxZoom: 19,
        }).addTo(map);

        const stationMarkers = {};
        const STATIONS_MIN_ZOOM = 7;
        const STATION_VIEWPORT_PAD = 0.15;
        let lastStationSyncSig = '';

        const markers = {};
        const markerStates = {};
        const replayBuffer = [];
        const MAX_BUFFER_FRAMES = 600;
        const updatesWindow = [];
        const MAX_SHIPS_RENDER = 30000;
        const KPI_SAMPLE_ABOVE = 35000;
        const KPI_SAMPLE_TARGET = 18000;
        const TYPE_VIZ_STORAGE_KEY = 'shipTrackerAnalyticsTypeViz';
        let analyticsTypeViz = 'bars';
        try {
            const _tv = localStorage.getItem(TYPE_VIZ_STORAGE_KEY);
            if (_tv === 'pie' || _tv === 'bars') analyticsTypeViz = _tv;
        } catch (e) { }
        let lastKpiShipsForAnalytics = [];
        const markerLayer = L.markerClusterGroup({
            chunkedLoading: true,
            chunkInterval: 280,
            chunkDelay: 75,
            removeOutsideVisibleBounds: true,
            disableClusteringAtZoom: 11,
            maxClusterRadius: 33,
            animate: false,
            animateAddingMarkers: false,
            iconCreateFunction: function (cluster) {
                const count = cluster.getChildCount();
                const html = `
                    <div class="cluster-bubble">
                        <span class="cluster-emoji">🚢</span>
                        <span class="cluster-count">${count}</span>
                    </div>
                `;
                return L.divIcon({
                    html: html,
                    className: '',
                    iconSize: [40, 40],
                    iconAnchor: [20, 20]
                });
            }
        });
        map.addLayer(markerLayer);

        const stationLayer = L.markerClusterGroup({
            chunkedLoading: true,
            chunkInterval: 280,
            chunkDelay: 75,
            removeOutsideVisibleBounds: true,
            disableClusteringAtZoom: 11,
            maxClusterRadius: 72,
            animate: false,
            animateAddingMarkers: false,
            spiderfyOnMaxZoom: true,
            showCoverageOnHover: false,
            clusterPane: 'stationsTopPane',
            zoomToBoundsOnClick: true,
            iconCreateFunction: function (cluster) {
                const count = cluster.getChildCount();
                const html = `
                    <div class="station-cluster-bubble">
                        <span class="station-cluster-emoji">⚓</span>
                        <span class="station-cluster-count">${count}</span>
                    </div>
                `;
                return L.divIcon({
                    html: html,
                    className: '',
                    iconSize: [36, 36],
                    iconAnchor: [18, 18]
                });
            }
        });
        map.addLayer(stationLayer);

        stationLayer.on('clusterclick', function (e) {
            try {
                e.layer.zoomToBounds({ padding: [24, 24] });
            } catch (_) {}
        });

        function setShipsVisible(on) {
            shipsVisible = on;
            const sb = document.getElementById('btnToolbarShips');
            if (sb) sb.classList.toggle('active', on);
            if (on) {
                if (!map.hasLayer(markerLayer)) map.addLayer(markerLayer);
            } else {
                if (map.hasLayer(markerLayer)) map.removeLayer(markerLayer);
            }
        }

        function escapeHtml(str) {
            if (str === null || str === undefined) return '';
            const d = document.createElement('div');
            d.textContent = String(str);
            return d.innerHTML;
        }

        function stationKindLabel(kind) {
            const tt = t();
            if (kind === 'ais_base') return tt.stationBase;
            if (kind === 'ais_aton') return tt.stationAton;
            if (kind === 'major_port') return tt.stationPort;
            return kind;
        }

        function stationPopup(st) {
            const tt = t();
            let lines = '<b>' + escapeHtml(st.name) + '</b><br>' + escapeHtml(stationKindLabel(st.kind)) + '<br>';
            if (st.type_code !== null && st.type_code !== undefined) {
                lines += escapeHtml(tt.popupStationTypeCode) + ': ' + st.type_code + '<br>';
            }
            lines += tt.popupLat + ' ' + st.latitude.toFixed(6) + '<br>' + tt.popupLon + ' ' + st.longitude.toFixed(6);
            return lines;
        }

        function createStationIcon(kind) {
            let color = '#22c55e';
            if (kind === 'major_port') color = '#6366f1';
            if (kind === 'ais_aton') color = '#eab308';
            if (kind === 'ais_base') color = '#10b981';
            return L.divIcon({
                className: 'station-div-icon',
                html: '<div style="width:9px;height:9px;background:' + color + ';border:2px solid #0f172a;border-radius:2px;box-shadow:0 1px 3px rgba(0,0,0,0.45);"></div>',
                iconSize: [13, 13],
                iconAnchor: [6, 6],
                popupAnchor: [0, -8]
            });
        }

        function stationsShouldDisplay() {
            return map.getZoom() >= STATIONS_MIN_ZOOM;
        }

        function getStationClipBounds() {
            try {
                return map.getBounds().pad(STATION_VIEWPORT_PAD);
            } catch (e) {
                return null;
            }
        }

        function stationInViewport(s) {
            const b = getStationClipBounds();
            if (!b) return false;
            return b.contains(L.latLng(s.latitude, s.longitude));
        }

        function stationVisibleForMap(s) {
            if (!stationKindAllowed(s.kind)) return false;
            if (!stationsShouldDisplay()) return false;
            return stationInViewport(s);
        }

        function stationViewportSignature() {
            try {
                const b = map.getBounds().pad(STATION_VIEWPORT_PAD);
                return [
                    b.getSouth().toFixed(4),
                    b.getWest().toFixed(4),
                    b.getNorth().toFixed(4),
                    b.getEast().toFixed(4),
                ].join(',');
            } catch (e) {
                return '';
            }
        }

        function computeStationSyncSignature(stations) {
            const zb = stationsShouldDisplay() ? 1 : 0;
            const zBin = Math.floor(map.getZoom());
            const vp = stationViewportSignature();
            if (!Array.isArray(stations)) return String(zb) + '|' + zBin + '|' + vp + '|0';
            const vis = stations.filter(stationVisibleForMap);
            const body = vis
                .map((s) => [s.id, Math.round(s.latitude * 1e4), Math.round(s.longitude * 1e4), s.kind].join(':'))
                .sort()
                .join(',');
            return zb + '|' + zBin + '|' + vp + '|' + vis.length + '|' + body;
        }

        function syncStationMarkers(stations, opts) {
            const force = opts && opts.force;
            if (!map.hasLayer(stationLayer)) return;
            const sig = computeStationSyncSignature(stations);
            if (!force && sig === lastStationSyncSig) return;
            if (!stationsShouldDisplay()) {
                lastStationSyncSig = sig;
                stationLayer.clearLayers();
                Object.keys(stationMarkers).forEach((k) => delete stationMarkers[k]);
                try {
                    stationLayer.refreshClusters();
                } catch (_) {}
                return;
            }
            const visible = (stations || []).filter(stationVisibleForMap);
            const ids = new Set(visible.map((s) => s.id));
            visible.forEach((st) => {
                if (stationMarkers[st.id]) {
                    stationMarkers[st.id].setLatLng([st.latitude, st.longitude]);
                    if (stationMarkers[st.id].isPopupOpen()) {
                        stationMarkers[st.id].setPopupContent(stationPopup(st));
                    }
                    if (stationMarkers[st.id].options.stKind !== st.kind) {
                        stationMarkers[st.id].setIcon(createStationIcon(st.kind));
                        stationMarkers[st.id].options.stKind = st.kind;
                    }
                } else {
                    const m = L.marker([st.latitude, st.longitude], {
                        icon: createStationIcon(st.kind),
                        stKind: st.kind,
                        pane: 'stationsTopPane'
                    })
                        .bindPopup(stationPopup(st))
                        .addTo(stationLayer);
                    stationMarkers[st.id] = m;
                }
            });
            Object.keys(stationMarkers).forEach((id) => {
                if (!ids.has(id)) {
                    stationLayer.removeLayer(stationMarkers[id]);
                    delete stationMarkers[id];
                }
            });
            lastStationSyncSig = sig;
            try {
                stationLayer.refreshClusters();
            } catch (_) {}
        }

        // При клике по “кружку” кластера увеличиваем область,
        // чтобы отдельные кораблики появились и на них можно было навестиcь.
        markerLayer.on('clusterclick', function (e) {
            // MarkerCluster сам обычно делает zoomToBounds, но явно вызовем для надежности.
            renderCapOverride = MAX_SHIPS_RENDER;
            setTimeout(() => {
                renderCapOverride = null;
            }, 3000);

            try {
                e.layer.zoomToBounds({ padding: [20, 20] });
            } catch (_) {}
        });

        let latestLiveFrame = null;
        let pendingFrame = null;
        let ws = null;
        let reconnectInterval = null;
        let renderTimer = null;
        let lastRenderAt = 0;
        let isPaused = false;
        let isReplaying = false;
        let replayTimer = null;
        let renderCapOverride = null;
        let clusterIconsWarm = false;

        function refreshModeButtons() {
            const live = document.getElementById('liveBtn');
            const pause = document.getElementById('pauseBtn');
            const replay = document.getElementById('replayBtn');
            if (!live || !pause || !replay) return;
            live.classList.remove('mode-live-on', 'mode-pause-on', 'mode-replay-on');
            pause.classList.remove('mode-live-on', 'mode-pause-on', 'mode-replay-on');
            replay.classList.remove('mode-live-on', 'mode-pause-on', 'mode-replay-on');
            if (isReplaying) replay.classList.add('mode-replay-on');
            else if (isPaused) pause.classList.add('mode-pause-on');
            else live.classList.add('mode-live-on');
        }

        const speedFilterEl = document.getElementById('speedFilter');
        const typeFilterEl = document.getElementById('typeFilter');
        const modeLabelEl = document.getElementById('modeLabel');
        const selectedShipEl = document.getElementById('selectedShip');
        const centerSelectedBtn = document.getElementById('centerSelectedBtn');
        const clearSelectedBtn = document.getElementById('clearSelectedBtn');

        function setTextById(id, text) {
            const node = document.getElementById(id);
            if (node) node.textContent = text;
        }
        function setTitleById(id, title) {
            const node = document.getElementById(id);
            if (node) node.title = title;
        }

        function rebuildSpeedSelect() {
            const sel = speedFilterEl;
            if (!sel) return;
            const v = sel.value;
            const tt = t();
            const opts = [
                { value: 'all', label: tt.speedAll },
                { value: 'stopped', label: tt.speedStopped },
                { value: 'slow', label: tt.speedSlow },
                { value: 'medium', label: tt.speedMedium },
                { value: 'fast', label: tt.speedFast },
            ];
            sel.innerHTML = opts.map((o) => `<option value="${o.value}">${o.label}</option>`).join('');
            sel.value = opts.some((o) => o.value === v) ? v : 'all';
        }

        function shipTypeCategory(st) {
            if (st === null || st === undefined) return 'default';
            const n = Number(st);
            if (Number.isNaN(n) || n === 0) return 'default';
            if (n >= 80 && n <= 89) return 'tanker';
            if (n >= 70 && n <= 79) return 'cargo';
            if (n >= 60 && n <= 69) return 'passenger';
            if (n >= 20 && n <= 29) return 'fishing';
            if (n >= 30 && n <= 39) return 'towing';
            if (n >= 50 && n <= 59) return 'special';
            return 'other';
        }

        const TYPE_COLORS = {
            default: '#2563eb',
            tanker: '#f97316',
            cargo: '#eab308',
            passenger: '#a855f7',
            fishing: '#22c55e',
            towing: '#0f766e',
            special: '#db2777',
            other: '#57534e',
        };

        const SHIP_TYPE_ORDER = ['default', 'tanker', 'cargo', 'passenger', 'fishing', 'towing', 'special', 'other'];

        function typeCategoryLabel(cat) {
            const tt = t();
            const labels = {
                default: tt.typeUnknown,
                tanker: tt.typeTanker,
                cargo: tt.typeCargo,
                passenger: tt.typePassenger,
                fishing: tt.typeFishing,
                towing: tt.typeTowing,
                special: tt.typeSpecial,
                other: tt.typeOther,
            };
            return labels[cat] || cat;
        }

        function shipTypeLabel(ship) {
            const tt = t();
            const c = shipTypeCategory(ship.ship_type);
            const code = (ship.ship_type !== null && ship.ship_type !== undefined) ? ' AIS ' + ship.ship_type : '';
            const labels = {
                default: tt.typeUnknown,
                tanker: tt.typeTanker,
                cargo: tt.typeCargo,
                passenger: tt.typePassenger,
                fishing: tt.typeFishing,
                towing: tt.typeTowing,
                special: tt.typeSpecial,
                other: tt.typeOther,
            };
            return labels[c] + code;
        }

        function rebuildTypeFilter() {
            const sel = typeFilterEl;
            if (!sel) return;
            const v = sel.value;
            const tt = t();
            const opts = [{ value: 'all', label: tt.filterTypeAll }];
            SHIP_TYPE_ORDER.forEach((cat) => {
                opts.push({ value: cat, label: typeCategoryLabel(cat) });
            });
            sel.innerHTML = opts.map((o) => `<option value="${o.value}">${escapeHtml(o.label)}</option>`).join('');
            sel.value = opts.some((o) => o.value === v) ? v : 'all';
        }

        function refreshOpenPopups() {
            if (!latestLiveFrame || !latestLiveFrame.ships) return;
            const byId = {};
            latestLiveFrame.ships.forEach((s) => { byId[s.ship_id] = s; });
            Object.keys(markers).forEach((id) => {
                const m = markers[id];
                const ship = byId[Number(id)];
                if (ship && m.isPopupOpen()) m.setPopupContent(getPopup(ship));
            });
        }

        function applyLang(nextLang) {
            uiLang = nextLang === 'ru' ? 'ru' : 'en';
            try {
                localStorage.setItem(LANG_KEY, uiLang);
            } catch (e) { }
            document.documentElement.lang = uiLang;
            const tt = t();
            document.title = tt.title;
            try {
                rebuildSpeedSelect();
            } catch (e) {
                console.error('rebuildSpeedSelect', e);
            }
            try {
                rebuildTypeFilter();
            } catch (e) {
                console.error('rebuildTypeFilter', e);
            }
            const langEnBtn = document.getElementById('langEn');
            const langRuBtn = document.getElementById('langRu');
            if (langEnBtn) langEnBtn.classList.toggle('active', uiLang === 'en');
            if (langRuBtn) langRuBtn.classList.toggle('active', uiLang === 'ru');
            const langSwitchEl = document.getElementById('bottomLangSwitch');
            if (langSwitchEl) langSwitchEl.setAttribute('aria-label', tt.langGroupAria);
            const mapZoomDockEl = document.getElementById('mapZoomDock');
            if (mapZoomDockEl) mapZoomDockEl.setAttribute('aria-label', tt.mapZoomDockAria);
            const btnZi = document.getElementById('btnZoomIn');
            if (btnZi) {
                btnZi.title = tt.zoomInTitle;
                btnZi.setAttribute('aria-label', tt.zoomInTitle);
            }
            const btnZo = document.getElementById('btnZoomOut');
            if (btnZo) {
                btnZo.title = tt.zoomOutTitle;
                btnZo.setAttribute('aria-label', tt.zoomOutTitle);
            }
            setTextById('titleControls', tt.controls);
            const leftStreamTitleEl = document.getElementById('leftPanelStreamTitle');
            if (leftStreamTitleEl) leftStreamTitleEl.textContent = tt.leftPanelStream;
            const lss = document.getElementById('lblScaleSection');
            if (lss) lss.textContent = tt.scaleSection;
            const lzp = document.getElementById('lblZoomPrefix');
            if (lzp) lzp.textContent = tt.scaleZoom;
            const mbg = document.getElementById('modeBtnGroup');
            if (mbg) mbg.setAttribute('aria-label', tt.modeGroupAria);
            const lblFiltersSec = document.getElementById('lblFiltersSection');
            if (lblFiltersSec) lblFiltersSec.textContent = tt.filtersSection;
            setTextById('lblSpeed', tt.speed);
            const lblTf = document.getElementById('lblTypeFilter');
            if (lblTf) lblTf.textContent = tt.filterType;
            setTextById('lblSelected', tt.selected);
            if (centerSelectedBtn) centerSelectedBtn.textContent = tt.center;
            if (clearSelectedBtn) clearSelectedBtn.textContent = tt.clear;
            setTextById('liveBtn', tt.live);
            setTextById('pauseBtn', tt.pause);
            setTextById('replayBtn', tt.replay);
            setTextById('analyticsTitle', tt.analyticsTitle);
            setTextById('analyticsSubtitle', tt.analyticsSubtitle);
            const tabsAria = document.querySelector('.analytics-tabs');
            if (tabsAria) tabsAria.setAttribute('aria-label', tt.analyticsTabsAria);
            const tabBo = document.getElementById('analyticsTabBtnOverview');
            if (tabBo) tabBo.textContent = tt.analyticsTabOverview;
            const tabBt = document.getElementById('analyticsTabBtnTypes');
            if (tabBt) tabBt.textContent = tt.analyticsTabTypes;
            const tabBa = document.getElementById('analyticsTabBtnAdvanced');
            if (tabBa) tabBa.textContent = tt.analyticsTabAdvanced;
            const lblTH = document.getElementById('lblAnalyticsTypesHint');
            if (lblTH) lblTH.textContent = tt.analyticsTypesHint;
            const lblAH = document.getElementById('lblAnalyticsAdvHint');
            if (lblAH) lblAH.textContent = tt.analyticsAdvHint;
            const lk = document.getElementById('lblAdvKnownType');
            if (lk) lk.textContent = tt.analyticsLblAdvKnownType;
            const lm = document.getElementById('lblAdvMoving');
            if (lm) lm.textContent = tt.analyticsLblAdvMoving;
            const lblAdvStoppedShareEl = document.getElementById('lblAdvStoppedShare');
            if (lblAdvStoppedShareEl) lblAdvStoppedShareEl.textContent = tt.analyticsLblAdvStoppedShare;
            const lc = document.getElementById('lblAdvWithCog');
            if (lc) lc.textContent = tt.analyticsLblAdvWithCog;
            const lh = document.getElementById('lblAdvWithHeading');
            if (lh) lh.textContent = tt.analyticsLblAdvWithHeading;
            const tvBar = document.getElementById('typeVizToolbar');
            if (tvBar) tvBar.setAttribute('aria-label', tt.analyticsTypeVizAria);
            const tvp = document.getElementById('lblTypeVizPrompt');
            if (tvp) tvp.textContent = tt.analyticsVizPrompt + ':';
            const bvb = document.getElementById('btnTypeVizBars');
            if (bvb) bvb.textContent = tt.analyticsVizBars;
            const bvp = document.getElementById('btnTypeVizPie');
            if (bvp) bvp.textContent = tt.analyticsVizPie;
            try {
                syncTypeVizButtons();
            } catch (e) {
                console.error('syncTypeVizButtons', e);
            }
            setTextById('toolbarLayersTitle', tt.toolbarLayers);
            setTitleById('btnToolbarShips', tt.layerShips);
            const ts = document.getElementById('btnToolbarStations');
            if (ts) ts.title = tt.layerStations;
            setTextById('stationModalTitle', tt.stationModalTitle);
            setTextById('stationModalHint', tt.stationModalHint);
            setTextById('lblStationPorts', tt.stationModalPorts);
            setTextById('lblStationBases', tt.stationModalBases);
            setTextById('lblStationAton', tt.stationModalAton);
            const bcsm = document.getElementById('btnCloseStationModal');
            if (bcsm) {
                bcsm.title = tt.stationModalClose;
                bcsm.setAttribute('aria-label', tt.stationModalClose);
            }
            const bch = document.getElementById('btnCloseHelpModal');
            if (bch) {
                bch.title = tt.stationModalClose;
                bch.setAttribute('aria-label', tt.stationModalClose);
            }
            const bHelp = document.getElementById('btnToolbarHelp');
            if (bHelp) bHelp.title = tt.helpBtnTitle;
            const hmt = document.getElementById('helpModalTitle');
            if (hmt) hmt.textContent = tt.helpModalTitle;
            const hss = document.getElementById('helpSubShipsEl');
            if (hss) hss.textContent = tt.helpSubShips;
            const htsc = document.getElementById('helpTxtShipCluster');
            if (htsc) htsc.textContent = tt.helpTxtShipCluster;
            const htsi = document.getElementById('helpTxtShipTypesIntro');
            if (htsi) htsi.textContent = tt.helpTxtShipTypesIntro;
            const hslD = document.getElementById('helpShipLblDefault');
            if (hslD) hslD.textContent = tt.typeUnknown;
            const hslTk = document.getElementById('helpShipLblTanker');
            if (hslTk) hslTk.textContent = tt.typeTanker;
            const hslCg = document.getElementById('helpShipLblCargo');
            if (hslCg) hslCg.textContent = tt.typeCargo;
            const hslPs = document.getElementById('helpShipLblPassenger');
            if (hslPs) hslPs.textContent = tt.typePassenger;
            const hslFi = document.getElementById('helpShipLblFishing');
            if (hslFi) hslFi.textContent = tt.typeFishing;
            const hslTw = document.getElementById('helpShipLblTowing');
            if (hslTw) hslTw.textContent = tt.typeTowing;
            const hslSp = document.getElementById('helpShipLblSpecial');
            if (hslSp) hslSp.textContent = tt.typeSpecial;
            const hslOt = document.getElementById('helpShipLblOther');
            if (hslOt) hslOt.textContent = tt.typeOther;
            const hst = document.getElementById('helpSubStationsEl');
            if (hst) hst.textContent = tt.helpSubStations;
            const htStCl = document.getElementById('helpTxtStationCluster');
            if (htStCl) htStCl.textContent = tt.helpTxtStationCluster;
            const hdp = document.getElementById('helpTxtDotPort');
            if (hdp) hdp.textContent = tt.helpTxtDotPort;
            const hdb = document.getElementById('helpTxtDotBase');
            if (hdb) hdb.textContent = tt.helpTxtDotBase;
            const hda = document.getElementById('helpTxtDotAton');
            if (hda) hda.textContent = tt.helpTxtDotAton;
            const hstb = document.getElementById('helpSubToolbarEl');
            if (hstb) hstb.textContent = tt.helpSubToolbar;
            const htbs = document.getElementById('helpTbShips');
            if (htbs) htbs.textContent = tt.helpTbShips;
            const htbst = document.getElementById('helpTbStations');
            if (htbst) htbst.textContent = tt.helpTbStations;
            const htbw = document.getElementById('helpTbWeather');
            if (htbw) htbw.textContent = tt.helpTbWeather;
            const htba = document.getElementById('helpTbAnalytics');
            if (htba) htba.textContent = tt.helpTbAnalytics;
            const wToolbarBtn = document.getElementById('btnToolbarWeather');
            if (wToolbarBtn) {
                wToolbarBtn.title = WEATHER_ENABLED ? tt.layerWeather : tt.weatherDisabledHint;
                wToolbarBtn.disabled = !WEATHER_ENABLED;
            }
            const wmt = document.getElementById('weatherModalTitle');
            if (wmt) wmt.textContent = tt.weatherModalTitle;
            const wmh = document.getElementById('weatherModalHint');
            if (wmh) wmh.textContent = tt.weatherModalHint;
            const wlt = document.getElementById('lblWeatherTempOverlay');
            if (wlt) wlt.textContent = tt.weatherLblTempOverlay;
            const wlp = document.getElementById('lblWeatherPrecipOverlay');
            if (wlp) wlp.textContent = tt.weatherLblPrecipOverlay;
            const bcw = document.getElementById('btnCloseWeatherModal');
            if (bcw) {
                bcw.title = tt.stationModalClose;
                bcw.setAttribute('aria-label', tt.stationModalClose);
            }
            setTitleById('btnOpenAnalytics', tt.openAnalytics);
            const dh = document.getElementById('analyticsDragHandle');
            if (dh) dh.title = tt.dragAnalyticsHint;
            const btnCloseAn = document.getElementById('btnCloseAnalytics');
            if (btnCloseAn) {
                btnCloseAn.title = tt.closeAnalytics;
                btnCloseAn.setAttribute('aria-label', tt.closeAnalytics);
            }
            setTextById('lblConn', tt.connLabel);
            setTextById('lblSpeedMix', tt.speedMixTitle);
            setTextById('histLblStopped', tt.speedStopped);
            setTextById('histLblSlow', tt.speedSlow);
            setTextById('histLblMedium', tt.speedMedium);
            setTextById('histLblFast', tt.speedFast);
            setTextById('lblShownShips', tt.shownShips);
            setTextById('lblUps', tt.ups);
            setTextById('lblLastUp', tt.lastUp);
            setTextById('lblMode', tt.mode);
            updateMapScaleIndicator();
            const wpop = document.getElementById('weatherSettingsModal');
            if (wpop && !wpop.classList.contains('hidden')) {
                fetchWeatherSummaryForCenter();
            }
            const st = document.getElementById('status');
            if (st) {
                const cur = st.textContent;
                const en = STR.en;
                const ru = STR.ru;
                if (cur === en.statConnected || cur === ru.statConnected) st.textContent = tt.statConnected;
                else if (cur === en.statDisconnected || cur === ru.statDisconnected) st.textContent = tt.statDisconnected;
                else if (cur === en.statError || cur === ru.statError) st.textContent = tt.statError;
                else st.textContent = tt.statConnecting;
            }
            setModeLabel();
            if (latestLiveFrame && latestLiveFrame.ships) {
                refreshOpenPopups();
                renderFrame(latestLiveFrame);
            } else if (latestLiveFrame && stationFiltersAny() && Array.isArray(latestLiveFrame.stations)) {
                if (!map.hasLayer(stationLayer)) stationLayer.addTo(map);
                syncStationMarkers(latestLiveFrame.stations, { force: true });
            }
        }

        // Ревизия SVG-иконки: если меняем верстку createShipIcon, принудительно обновляем уже созданные маркеры.
        const ICON_REV = 9;

        let selectedShipId = null;

        function getShipAngle(ship) {
            if (ship.heading !== null && ship.heading !== undefined) return ship.heading;
            if (ship.course_over_ground !== null && ship.course_over_ground !== undefined) return ship.course_over_ground;
            return 0;
        }

        function getShipSpeed(ship) {
            if (ship.speed_over_ground === null || ship.speed_over_ground === undefined) return 0;
            return ship.speed_over_ground;
        }

        function createShipIcon(ship) {
            const angle = getShipAngle(ship);
            const cat = shipTypeCategory(ship.ship_type);
            const fill = TYPE_COLORS[cat] || TYPE_COLORS.default;
            const stroke = '#0f172a';
            let extra = '';
            if (cat === 'tanker') {
                extra = '<rect x="10" y="8" width="4" height="8" rx="1" fill="rgba(255,255,255,0.28)" stroke="' + stroke + '" stroke-width="0.6"></rect>';
            } else if (cat === 'cargo') {
                extra = '<rect x="7" y="10" width="10" height="5" rx="0.8" fill="none" stroke="' + stroke + '" stroke-width="0.85"></rect>';
            } else if (cat === 'passenger') {
                extra = '<path d="M8 14 L16 14 M10 11 L14 11" stroke="' + stroke + '" stroke-width="0.9" stroke-linecap="round"></path>';
            }
            const svg = `
                <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(${angle}deg);" aria-hidden="true">
                    <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                    ${extra}
                    <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="${fill}" stroke="${stroke}" stroke-width="1.0" stroke-linejoin="round"></path>
                    <line x1="12" y1="6" x2="12" y2="18" stroke="${stroke}" stroke-width="1.3" stroke-linecap="round"></line>
                </svg>
            `;
            return L.divIcon({
                className: 'ship-div-icon',
                html: svg,
                iconSize: [18, 18],
                iconAnchor: [9, 9],
                popupAnchor: [0, -10]
            });
        }

        function getPopup(ship) {
            const tt = t();
            const kn = tt.kn;
            const spd = ship.speed_over_ground
                ? `${tt.popupSpeed} ${ship.speed_over_ground.toFixed(1)} ${kn}<br>`
                : `${tt.popupSpeedZero}<br>`;
            const typeLine = (ship.ship_type !== null && ship.ship_type !== undefined)
                ? `${tt.popupType}: ${shipTypeLabel(ship)}<br>`
                : '';
            return `
                <b>${tt.popupShip} ${ship.ship_id}</b><br>
                ${typeLine}
                ${tt.popupLat} ${ship.latitude.toFixed(6)}<br>
                ${tt.popupLon} ${ship.longitude.toFixed(6)}<br>
                ${ship.course_over_ground ? `${tt.popupCourse} ${ship.course_over_ground.toFixed(1)}°<br>` : ''}
                ${spd}
                ${ship.heading ? `${tt.popupHeading} ${ship.heading}°<br>` : ''}
            `;
        }

        function selectShip(shipId) {
            selectedShipId = Number(shipId);
            selectedShipEl.textContent = selectedShipId;

            const marker = markers[selectedShipId];
            if (!marker) return;
            marker.openPopup();
        }

        function centerSelectedShip() {
            if (selectedShipId === null) return;
            const marker = markers[selectedShipId];
            if (!marker) return;
            map.setView(marker.getLatLng(), Math.max(map.getZoom(), 10));
        }

        function passesSpeedFilter(ship) {
            const filter = speedFilterEl.value;
            if (filter === 'all') return true;
            const speed = getShipSpeed(ship);
            if (filter === 'stopped') return speed < 1;
            if (filter === 'slow') return speed >= 1 && speed < 4;
            if (filter === 'medium') return speed >= 4 && speed < 12;
            if (filter === 'fast') return speed >= 12;
            return true;
        }

        function passesTypeFilter(ship) {
            if (!typeFilterEl) return true;
            const f = typeFilterEl.value;
            if (f === 'all') return true;
            return shipTypeCategory(ship.ship_type) === f;
        }

        function applyShipFilter(ships) {
            const filteredNoCap = ships.filter((ship) => {
                if (!passesSpeedFilter(ship)) return false;
                if (!passesTypeFilter(ship)) return false;
                return true;
            });

            // Маркеры только в видимой области (кластер сам ещё отсекает вне экрана).
            // KPI и гистограмма — по всем судам после фильтра скорости (сэмпл при >35k).
            const bounds = map.getBounds();
            const filteredInBounds = filteredNoCap.filter((ship) => bounds.contains([ship.latitude, ship.longitude]));
            return { kpiShips: filteredNoCap, renderShips: applyZoomBasedCap(filteredInBounds) };
        }

        function getRenderCapByZoom(zoom) {
            if (zoom <= 3) return Math.min(10000, MAX_SHIPS_RENDER);
            if (zoom <= 4) return Math.min(14000, MAX_SHIPS_RENDER);
            if (zoom <= 6) return Math.min(22000, MAX_SHIPS_RENDER);
            if (zoom <= 8) return Math.min(28000, MAX_SHIPS_RENDER);
            return MAX_SHIPS_RENDER;
        }

        function stableHash(id) {
            const s = String(id);
            let h = 0;
            for (let i = 0; i < s.length; i += 1) {
                h = ((h << 5) - h) + s.charCodeAt(i);
                h |= 0;
            }
            return Math.abs(h);
        }

        function applyZoomBasedCap(ships) {
            const cap = renderCapOverride !== null ? renderCapOverride : getRenderCapByZoom(map.getZoom());
            if (ships.length <= cap) return ships;
            const ratio = cap / ships.length;
            const threshold = Math.max(1, Math.floor(ratio * 1000));
            const sampled = ships.filter((ship) => (stableHash(ship.ship_id) % 1000) < threshold);
            if (sampled.length <= cap) return sampled;
            return sampled.slice(0, cap);
        }

        function getAdaptiveRenderDelayMs(renderShipCount) {
            if (renderShipCount > 25000) return 1200;
            if (renderShipCount > 12000) return 1000;
            if (renderShipCount > 7000) return 750;
            if (renderShipCount > 3500) return 520;
            if (renderShipCount > 1500) return 320;
            return 180;
        }

        function scheduleRender(frame) {
            pendingFrame = frame;
            if (isPaused || isReplaying) return;
            if (renderTimer) return;

            const expectedRenderCap = renderCapOverride !== null ? renderCapOverride : getRenderCapByZoom(map.getZoom());
            const targetDelay = getAdaptiveRenderDelayMs(expectedRenderCap);
            const elapsed = Date.now() - lastRenderAt;
            const delay = Math.max(0, targetDelay - elapsed);

            renderTimer = setTimeout(() => {
                renderTimer = null;
                if (!pendingFrame || isPaused || isReplaying) return;
                renderFrame(pendingFrame);
                lastRenderAt = Date.now();
                pendingFrame = null;
            }, delay);
        }

        function sampleShipsEvenlyForKpi(ships, target) {
            if (ships.length <= target) return ships;
            const step = ships.length / target;
            const out = [];
            for (let i = 0; i < target; i += 1) {
                out.push(ships[Math.min(ships.length - 1, Math.floor(i * step))]);
            }
            return out;
        }

        function updateKpis(ships) {
            lastKpiShipsForAnalytics = ships;
            const elTotal = document.getElementById('total');
            if (elTotal) elTotal.textContent = ships.length;
            const forStats = ships.length > KPI_SAMPLE_ABOVE
                ? sampleShipsEvenlyForKpi(ships, KPI_SAMPLE_TARGET)
                : ships;
            const nowMs = Date.now();
            updatesWindow.push(nowMs);
            while (updatesWindow.length && updatesWindow[0] < nowMs - 10000) {
                updatesWindow.shift();
            }
            const ups = updatesWindow.length / 10;
            const elUps = document.getElementById('updatesPerSec');
            if (elUps) elUps.textContent = ups.toFixed(1);
            const elLast = document.getElementById('lastUpdate');
            if (elLast) {
                elLast.textContent = new Date(nowMs).toLocaleTimeString(
                    uiLang === 'ru' ? 'ru-RU' : 'en-GB'
                );
            }
            updateSpeedHistogram(forStats);
            updateShipTypeBreakdown(ships);
            updateAdvancedAnalytics(ships, forStats);
        }

        function syncTypeVizButtons() {
            const bb = document.getElementById('btnTypeVizBars');
            const bp = document.getElementById('btnTypeVizPie');
            if (bb) bb.setAttribute('aria-pressed', analyticsTypeViz === 'bars' ? 'true' : 'false');
            if (bp) bp.setAttribute('aria-pressed', analyticsTypeViz === 'pie' ? 'true' : 'false');
        }

        function setAnalyticsTypeViz(mode) {
            analyticsTypeViz = mode === 'pie' ? 'pie' : 'bars';
            try {
                localStorage.setItem(TYPE_VIZ_STORAGE_KEY, analyticsTypeViz);
            } catch (e) { }
            syncTypeVizButtons();
            updateShipTypeBreakdown(lastKpiShipsForAnalytics || []);
        }

        function updateSpeedHistogram(ships) {
            const c = { stopped: 0, slow: 0, medium: 0, fast: 0 };
            ships.forEach((s) => {
                const sp = getShipSpeed(s);
                if (sp < 1) c.stopped += 1;
                else if (sp < 4) c.slow += 1;
                else if (sp < 12) c.medium += 1;
                else c.fast += 1;
            });
            const total = ships.length || 1;
            [['Stopped', 'stopped'], ['Slow', 'slow'], ['Medium', 'medium'], ['Fast', 'fast']].forEach(([K, k]) => {
                const n = c[k];
                const pct = (n / total) * 100;
                const bar = document.getElementById('histBar' + K);
                const lab = document.getElementById('histPct' + K);
                if (bar) bar.style.width = pct.toFixed(2) + '%';
                if (lab) lab.textContent = pct.toFixed(1) + '%';
            });
        }

        function buildShipTypeCounts(ships) {
            const counts = {
                default: 0, tanker: 0, cargo: 0, passenger: 0,
                fishing: 0, towing: 0, special: 0, other: 0,
            };
            ships.forEach((s) => {
                const c = shipTypeCategory(s.ship_type);
                if (counts[c] !== undefined) counts[c] += 1;
            });
            return counts;
        }

        function buildShipTypeBarsHtml(counts, total) {
            const maxN = Math.max(1, ...SHIP_TYPE_ORDER.map((c) => counts[c]));
            return (
                '<div class="type-chart-bars">' +
                SHIP_TYPE_ORDER.map((cat) => {
                    const n = counts[cat];
                    const pct = total ? (n / total) * 100 : 0;
                    const h = (n / maxN) * 100;
                    const color = TYPE_COLORS[cat] || TYPE_COLORS.default;
                    const name = typeCategoryLabel(cat);
                    return (
                        '<div class="type-chart-bar-col">' +
                        '<span class="type-chart-bar-val" title="' + pct.toFixed(1) + '%">' + n + '</span>' +
                        '<div class="type-chart-bar-fill" style="height:' + h.toFixed(1) + '%;background:' + color + ';min-height:' + (n > 0 ? '3px' : '0') + ';"></div>' +
                        '<div class="type-chart-bar-lbl" title="' + escapeHtml(name) + '">' + escapeHtml(name) + '</div>' +
                        '</div>'
                    );
                }).join('') +
                '</div>'
            );
        }

        function buildShipTypePieHtml(counts, total) {
            const cx = 90;
            const cy = 90;
            const r = 78;
            let ang = -Math.PI / 2;
            const pathParts = [];
            SHIP_TYPE_ORDER.forEach((cat) => {
                const n = counts[cat];
                if (n === 0) return;
                const slice = (n / total) * 2 * Math.PI;
                const color = TYPE_COLORS[cat] || TYPE_COLORS.default;
                if (n === total) {
                    pathParts.push('<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + color + '" stroke="#0f172a" stroke-width="0.6"/>');
                    ang += slice;
                    return;
                }
                const x1 = cx + r * Math.cos(ang);
                const y1 = cy + r * Math.sin(ang);
                ang += slice;
                const x2 = cx + r * Math.cos(ang);
                const y2 = cy + r * Math.sin(ang);
                const large = slice > Math.PI ? 1 : 0;
                const d = 'M ' + cx + ' ' + cy + ' L ' + x1 + ' ' + y1 + ' A ' + r + ' ' + r + ' 0 ' + large + ' 1 ' + x2 + ' ' + y2 + ' Z';
                pathParts.push('<path fill="' + color + '" d="' + d + '" stroke="#0f172a" stroke-width="0.6"/>');
            });
            const legend = SHIP_TYPE_ORDER.map((cat) => {
                const n = counts[cat];
                const pct = total ? (n / total) * 100 : 0;
                const color = TYPE_COLORS[cat] || TYPE_COLORS.default;
                const name = typeCategoryLabel(cat);
                return (
                    '<div class="type-chart-pie-leg-row">' +
                    '<span class="type-chart-pie-swatch" style="background:' + color + ';"></span>' +
                    '<span>' + escapeHtml(name) + ' — ' + n + ' (' + pct.toFixed(1) + '%)</span>' +
                    '</div>'
                );
            }).join('');
            return (
                '<div class="type-chart-pie-wrap">' +
                '<svg class="type-chart-pie-svg" width="180" height="180" viewBox="0 0 180 180" role="img" aria-hidden="true">' +
                pathParts.join('') +
                '</svg>' +
                '<div class="type-chart-pie-legend">' + legend + '</div>' +
                '</div>'
            );
        }

        function updateShipTypeBreakdown(ships) {
            const wrap = document.getElementById('shipTypeBreakdown');
            if (!wrap) return;
            const tt = t();
            if (!ships || !ships.length) {
                wrap.innerHTML = '<p class="analytics-section-hint">' + escapeHtml(tt.analyticsTypesEmpty) + '</p>';
                return;
            }
            const counts = buildShipTypeCounts(ships);
            const total = ships.length;
            wrap.innerHTML = analyticsTypeViz === 'pie'
                ? buildShipTypePieHtml(counts, total)
                : buildShipTypeBarsHtml(counts, total);
        }

        function updateAdvancedAnalytics(ships, forStats) {
            const tt = t();
            const sampleEl = document.getElementById('lblAnalyticsAdvSample');
            if (sampleEl) {
                sampleEl.textContent = ships.length > KPI_SAMPLE_ABOVE
                    ? tt.analyticsAdvSampleOn.replace(/\{n\}/g, String(KPI_SAMPLE_TARGET)).replace(/\{m\}/g, String(KPI_SAMPLE_ABOVE))
                    : tt.analyticsAdvSampleOff;
            }
            let known = 0;
            ships.forEach((s) => {
                const st = s.ship_type;
                if (st !== null && st !== undefined) {
                    const n = Number(st);
                    if (!Number.isNaN(n) && n > 0) known += 1;
                }
            });
            const total = ships.length || 1;
            const knownPct = (known / total) * 100;
            const knownEl = document.getElementById('advKnownTypeVal');
            if (knownEl) knownEl.textContent = known + ' (' + knownPct.toFixed(1) + '%)';

            const speeds = forStats.map(getShipSpeed);
            const moving = speeds.filter((sp) => sp >= 1).length;
            const movPct = forStats.length ? (moving / forStats.length) * 100 : 0;
            const movingScaled = ships.length > KPI_SAMPLE_ABOVE
                ? Math.round((moving / forStats.length) * ships.length)
                : moving;
            const movEl = document.getElementById('advMovingVal');
            if (movEl) {
                movEl.textContent = ships.length > KPI_SAMPLE_ABOVE
                    ? '~' + movingScaled + ' (' + movPct.toFixed(1) + '%)'
                    : moving + ' (' + movPct.toFixed(1) + '%)';
            }

            const stopped = speeds.filter((sp) => sp < 1).length;
            const stoppedPct = forStats.length ? (stopped / forStats.length) * 100 : 0;
            const stoppedScaled = ships.length > KPI_SAMPLE_ABOVE
                ? Math.round((stopped / forStats.length) * ships.length)
                : stopped;
            const stEl = document.getElementById('advStoppedShareVal');
            if (stEl) {
                stEl.textContent = ships.length > KPI_SAMPLE_ABOVE
                    ? '~' + stoppedScaled + ' (' + stoppedPct.toFixed(1) + '%)'
                    : stopped + ' (' + stoppedPct.toFixed(1) + '%)';
            }

            let cogN = 0;
            let hdgN = 0;
            forStats.forEach((s) => {
                if (s.course_over_ground !== null && s.course_over_ground !== undefined && !Number.isNaN(Number(s.course_over_ground))) cogN += 1;
                if (s.heading !== null && s.heading !== undefined && !Number.isNaN(Number(s.heading))) hdgN += 1;
            });
            const cTot = forStats.length || 1;
            const cogEl = document.getElementById('advWithCogVal');
            const hdgEl = document.getElementById('advWithHeadingVal');
            if (cogEl) cogEl.textContent = cogN + ' (' + ((cogN / cTot) * 100).toFixed(1) + '%)';
            if (hdgEl) hdgEl.textContent = hdgN + ' (' + ((hdgN / cTot) * 100).toFixed(1) + '%)';
        }

        function analyticsTabPanelId(key) {
            return 'analyticsTabPanel' + key.charAt(0).toUpperCase() + key.slice(1);
        }

        function analyticsTabBtnId(key) {
            return 'analyticsTabBtn' + key.charAt(0).toUpperCase() + key.slice(1);
        }

        function switchAnalyticsTab(tab) {
            ['overview', 'types', 'advanced'].forEach((k) => {
                const panel = document.getElementById(analyticsTabPanelId(k));
                const btn = document.getElementById(analyticsTabBtnId(k));
                const on = k === tab;
                if (panel) panel.classList.toggle('hidden', !on);
                if (btn) btn.setAttribute('aria-selected', on ? 'true' : 'false');
            });
        }

        function renderFrame(frame) {
            if (!frame || !frame.ships) return;
            const { kpiShips, renderShips } = applyShipFilter(frame.ships);
            const visibleShips = renderShips;
            const currentShipIds = new Set();

            visibleShips.forEach((ship) => {
                const shipId = ship.ship_id;
                currentShipIds.add(shipId);
                const lat = ship.latitude;
                const lon = ship.longitude;
                const zoom = map.getZoom();
                const angle = Math.round(getShipAngle(ship));
                const kind = shipTypeCategory(ship.ship_type);

                if (markers[shipId]) {
                    const prev = markerStates[shipId] || {};
                    const moved = Math.abs((prev.lat || 0) - lat) >= 0.00002 || Math.abs((prev.lon || 0) - lon) >= 0.00002;
                    const iconChanged = prev.iconRev !== ICON_REV || prev.angle !== angle || prev.zoom !== zoom || prev.kind !== kind;
                    if (moved) {
                        markers[shipId].setLatLng([lat, lon]);
                    }
                    if (iconChanged) {
                        markers[shipId].setIcon(createShipIcon(ship));
                    }
                    if (markers[shipId].isPopupOpen()) {
                        markers[shipId].setPopupContent(getPopup(ship));
                    }
                    // Чтобы clusterclick мог получить shipId через options
                    markers[shipId].options.shipId = shipId;
                    markerStates[shipId] = { lat, lon, angle, zoom, iconRev: ICON_REV, kind };
                } else {
                    markers[shipId] = L.marker([lat, lon], { icon: createShipIcon(ship), shipId: shipId })
                        .addTo(markerLayer)
                        .bindPopup(getPopup(ship));
                    markers[shipId].options.shipId = shipId;
                    markerStates[shipId] = { lat, lon, angle, zoom, iconRev: ICON_REV, kind };

                    // Наведение открывает popup с параметрами (помогает в демо после клика по кластеру).
                    // Проверяем текущий zoom, чтобы не разогревать UI на дальнем зуме.
                    markers[shipId].on('mouseover', function () {
                        if (map.getZoom() >= 8) this.openPopup();
                    });
                    markers[shipId].on('mouseout', function () {
                        if (map.getZoom() >= 8) this.closePopup();
                    });
                    markers[shipId].on('click', function () {
                        selectShip(shipId);
                    });
                }
            });

            Object.keys(markers).forEach((shipIdRaw) => {
                const shipId = Number(shipIdRaw);
                if (!currentShipIds.has(shipId)) {
                    markerLayer.removeLayer(markers[shipId]);
                    delete markers[shipId];
                    delete markerStates[shipId];
                }
            });

            try {
                updateKpis(kpiShips);
            } catch (e) {
                console.error('updateKpis failed', e);
            }

            // Обновляем иконки кластеров (особенно для count<=4, где рисуется SVG),
            // потому что iconCreateFunction для кластеров кэшируется.
            if (!clusterIconsWarm) {
                clusterIconsWarm = true;
                try {
                    markerLayer.refreshClusters();
                } catch (_) {}
            }

            if (isReplaying && stationFiltersAny() && Array.isArray(frame.stations)) {
                if (!map.hasLayer(stationLayer)) stationLayer.addTo(map);
                syncStationMarkers(frame.stations);
            }
        }

        let stationsSyncRaf = null;
        function queueStationsSyncFromFrame(frame) {
            if (!frame || !Array.isArray(frame.stations) || !stationFiltersAny()) return;
            if (stationsSyncRaf !== null) cancelAnimationFrame(stationsSyncRaf);
            stationsSyncRaf = requestAnimationFrame(() => {
                stationsSyncRaf = null;
                if (!stationFiltersAny()) return;
                if (!map.hasLayer(stationLayer)) stationLayer.addTo(map);
                syncStationMarkers(frame.stations);
            });
        }

        function setModeLabel() {
            if (!modeLabelEl) return;
            const tt = t();
            if (isReplaying) modeLabelEl.textContent = tt.modeReplay;
            else if (isPaused) modeLabelEl.textContent = tt.modePaused;
            else modeLabelEl.textContent = tt.modeLive;
            refreshModeButtons();
        }

        function stopReplay() {
            if (replayTimer) {
                clearInterval(replayTimer);
                replayTimer = null;
            }
            isReplaying = false;
            setModeLabel();
        }

        function playReplay() {
            stopReplay();
            isPaused = true;
            isReplaying = true;
            setModeLabel();

            const recentFrames = replayBuffer.slice(-120);
            if (!recentFrames.length) return;

            let index = 0;
            replayTimer = setInterval(() => {
                if (index >= recentFrames.length) {
                    stopReplay();
                    return;
                }
                renderFrame(recentFrames[index]);
                index += 1;
            }, 500);
        }

        function connect() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                const statusEl = document.getElementById('status');
                statusEl.textContent = t().statConnected;
                statusEl.className = 'status connected';
                if (reconnectInterval) {
                    clearInterval(reconnectInterval);
                    reconnectInterval = null;
                }
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                latestLiveFrame = data;
                replayBuffer.push(data);
                if (replayBuffer.length > MAX_BUFFER_FRAMES) replayBuffer.shift();
                if (!isPaused && !isReplaying) {
                    scheduleRender(data);
                }
                queueStationsSyncFromFrame(data);
            };

            ws.onerror = () => {
                const statusEl = document.getElementById('status');
                statusEl.textContent = t().statError;
                statusEl.className = 'status disconnected';
            };

            ws.onclose = () => {
                const statusEl = document.getElementById('status');
                statusEl.textContent = t().statDisconnected;
                statusEl.className = 'status disconnected';
                if (!reconnectInterval) {
                    reconnectInterval = setInterval(connect, 3000);
                }
            };
        }

        loadStationFiltersFromStorage();
        const chkPortsEl = document.getElementById('chkStationPorts');
        const chkBasesEl = document.getElementById('chkStationBases');
        const chkAtonEl = document.getElementById('chkStationAton');
        if (chkPortsEl) chkPortsEl.checked = stationShowPorts;
        if (chkBasesEl) chkBasesEl.checked = stationShowBases;
        if (chkAtonEl) chkAtonEl.checked = stationShowAton;
        applyStationFilters();
        connect();
        try {
            applyLang(uiLang);
        } catch (e) {
            console.error('applyLang failed', e);
        }
        if (WEATHER_ENABLED) {
            const pr = loadWeatherOverlayPrefs();
            if (pr.temp) setWeatherTempLayer(true);
            if (pr.precip) setWeatherPrecipLayer(true);
            updateWeatherToolbarBtn();
        }

        if (speedFilterEl) {
            speedFilterEl.onchange = () => {
                if (latestLiveFrame) renderFrame(latestLiveFrame);
            };
        }
        if (typeFilterEl) {
            typeFilterEl.onchange = () => {
                if (latestLiveFrame) renderFrame(latestLiveFrame);
            };
        }
        const btnZoomInEl = document.getElementById('btnZoomIn');
        if (btnZoomInEl) btnZoomInEl.onclick = () => { map.zoomIn(); };
        const btnZoomOutEl = document.getElementById('btnZoomOut');
        if (btnZoomOutEl) btnZoomOutEl.onclick = () => { map.zoomOut(); };

        if (centerSelectedBtn) {
            centerSelectedBtn.onclick = () => {
                centerSelectedShip();
            };
        }

        if (clearSelectedBtn) {
            clearSelectedBtn.onclick = () => {
                selectedShipId = null;
                if (selectedShipEl) selectedShipEl.textContent = '-';
            };
        }

        map.on('moveend', () => {
            updateMapScaleIndicator();
            scheduleWeatherSummaryRefresh();
            if (latestLiveFrame && !isReplaying) {
                renderFrame(latestLiveFrame);
            }
            if (latestLiveFrame && stationFiltersAny()) {
                queueStationsSyncFromFrame(latestLiveFrame);
            }
        });

        map.on('zoomend', () => {
            updateMapScaleIndicator();
            if (latestLiveFrame && !isReplaying) {
                renderFrame(latestLiveFrame);
            }
            try {
                markerLayer.refreshClusters();
            } catch (_) {}
            if (latestLiveFrame && stationFiltersAny() && Array.isArray(latestLiveFrame.stations)) {
                if (!map.hasLayer(stationLayer)) stationLayer.addTo(map);
                syncStationMarkers(latestLiveFrame.stations);
            }
        });

        const liveBtnEl = document.getElementById('liveBtn');
        if (liveBtnEl) {
            liveBtnEl.onclick = () => {
                stopReplay();
                isPaused = false;
                setModeLabel();
                if (latestLiveFrame) renderFrame(latestLiveFrame);
            };
        }
        const pauseBtnEl = document.getElementById('pauseBtn');
        if (pauseBtnEl) {
            pauseBtnEl.onclick = () => {
                stopReplay();
                isPaused = true;
                setModeLabel();
            };
        }
        const replayBtnEl = document.getElementById('replayBtn');
        if (replayBtnEl) replayBtnEl.onclick = () => playReplay();

        const btnOpenAnalyticsEl = document.getElementById('btnOpenAnalytics');
        if (btnOpenAnalyticsEl) btnOpenAnalyticsEl.onclick = () => toggleAnalyticsPanel();
        const tabOverviewEl = document.getElementById('analyticsTabBtnOverview');
        if (tabOverviewEl) tabOverviewEl.onclick = () => switchAnalyticsTab('overview');
        const tabTypesEl = document.getElementById('analyticsTabBtnTypes');
        if (tabTypesEl) tabTypesEl.onclick = () => switchAnalyticsTab('types');
        const tabAdvancedEl = document.getElementById('analyticsTabBtnAdvanced');
        if (tabAdvancedEl) tabAdvancedEl.onclick = () => switchAnalyticsTab('advanced');
        const btnTypeVizBarsEl = document.getElementById('btnTypeVizBars');
        if (btnTypeVizBarsEl) btnTypeVizBarsEl.onclick = () => setAnalyticsTypeViz('bars');
        const btnTypeVizPieEl = document.getElementById('btnTypeVizPie');
        if (btnTypeVizPieEl) btnTypeVizPieEl.onclick = () => setAnalyticsTypeViz('pie');
        const btnCloseAnalyticsEl = document.getElementById('btnCloseAnalytics');
        if (btnCloseAnalyticsEl) btnCloseAnalyticsEl.onclick = () => hideAnalyticsPanel();
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape') return;
            const hm = document.getElementById('mapHelpPopover');
            if (hm && !hm.classList.contains('hidden')) {
                closeMapHelpModal();
                return;
            }
            const wx = document.getElementById('weatherSettingsModal');
            if (wx && !wx.classList.contains('hidden')) {
                closeWeatherSettingsModal();
                return;
            }
            const sm = document.getElementById('stationSettingsModal');
            if (sm && !sm.classList.contains('hidden')) {
                closeStationSettingsModal();
                return;
            }
            hideAnalyticsPanel();
        });
        const btnToolbarWeatherEl = document.getElementById('btnToolbarWeather');
        if (btnToolbarWeatherEl) {
            btnToolbarWeatherEl.onclick = () => {
                if (!WEATHER_ENABLED) return;
                toggleWeatherSettingsPopover();
            };
        }
        const btnCloseWeatherModalEl = document.getElementById('btnCloseWeatherModal');
        if (btnCloseWeatherModalEl) btnCloseWeatherModalEl.onclick = () => closeWeatherSettingsModal();
        const btnToolbarShipsEl = document.getElementById('btnToolbarShips');
        if (btnToolbarShipsEl) btnToolbarShipsEl.onclick = () => setShipsVisible(!shipsVisible);
        const btnToolbarStationsEl = document.getElementById('btnToolbarStations');
        if (btnToolbarStationsEl) btnToolbarStationsEl.onclick = () => toggleStationSettingsPopover();
        const btnCloseStationModalEl = document.getElementById('btnCloseStationModal');
        if (btnCloseStationModalEl) btnCloseStationModalEl.onclick = () => closeStationSettingsModal();
        const btnToolbarHelpEl = document.getElementById('btnToolbarHelp');
        if (btnToolbarHelpEl) btnToolbarHelpEl.onclick = () => toggleMapHelpModal();
        const btnCloseHelpModalEl = document.getElementById('btnCloseHelpModal');
        if (btnCloseHelpModalEl) btnCloseHelpModalEl.onclick = () => closeMapHelpModal();
        window.addEventListener('resize', () => {
            const m = document.getElementById('stationSettingsModal');
            if (m && !m.classList.contains('hidden')) positionStationPopover();
            const mh = document.getElementById('mapHelpPopover');
            if (mh && !mh.classList.contains('hidden')) positionHelpPopover();
            const mw = document.getElementById('weatherSettingsModal');
            if (mw && !mw.classList.contains('hidden')) positionWeatherPopover();
        });
        ['chkStationPorts', 'chkStationBases', 'chkStationAton'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', () => applyStationFilters());
        });
        const chkWt = document.getElementById('chkWeatherTempOverlay');
        if (chkWt) chkWt.addEventListener('change', () => setWeatherTempLayer(!!chkWt.checked));
        const chkWp = document.getElementById('chkWeatherPrecipOverlay');
        if (chkWp) chkWp.addEventListener('change', () => setWeatherPrecipLayer(!!chkWp.checked));

        const langEnEl = document.getElementById('langEn');
        if (langEnEl) langEnEl.onclick = () => applyLang('en');
        const langRuEl = document.getElementById('langRu');
        if (langRuEl) langRuEl.onclick = () => applyLang('ru');
    </script>
</body>
</html>
    """
    html_content = html_content.replace("__WEATHER_ENABLED__", json.dumps(bool(OPENWEATHERMAP_API_KEY)))
    return HTMLResponse(content=html_content)


def _serialize_ais_stations(rows):
    out = []
    for r in rows:
        mmsi = int(r["mmsi"])
        nm = (r.get("name") or str(mmsi)).strip()
        tc = r.get("type_code")
        out.append(
            {
                "id": f"db:{mmsi}",
                "kind": r["kind"],
                "name": nm,
                "latitude": float(r["latitude"]),
                "longitude": float(r["longitude"]),
                "type_code": int(tc) if tc is not None else None,
            }
        )
    return out


def _serialize_positions(positions):
    out = []
    for p in positions:
        row = {
            "ship_id": p["ship_id"],
            "latitude": float(p["latitude"]),
            "longitude": float(p["longitude"]),
            "course_over_ground": float(p["course_over_ground"]) if p["course_over_ground"] else None,
            "speed_over_ground": float(p["speed_over_ground"]) if p["speed_over_ground"] else None,
            "heading": int(p["heading"]) if p["heading"] else None,
        }
        try:
            st = p["ship_type"]
        except (KeyError, TypeError):
            st = None
        if st is not None:
            row["ship_type"] = int(st)
        out.append(row)
    return out


@app.get("/api/trails")
async def get_trails(minutes: int = Query(default=30, ge=0, le=60)):
    """Return ship trails from history for the selected time range."""
    if minutes == 0:
        return {"trails": {}, "minutes": 0}
    trails = await get_ship_trails(trail_minutes=minutes, points_per_ship=80)
    return {"trails": trails, "minutes": minutes}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time ship position updates"""
    await manager.connect(websocket)
    try:
        await websocket.send_json(latest_payload)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
