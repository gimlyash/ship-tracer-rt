"""FastAPI server with WebSocket for real-time ship position updates"""
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from app.database import close_db_pool, get_ship_positions, get_ship_trails, init_db_pool

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
latest_payload = {"type": "update", "ships": [], "timestamp": datetime.now(timezone.utc).isoformat()}
poller_task = None


async def refresh_positions_loop():
    """Single shared poller: one DB query, broadcast to all clients."""
    global latest_payload
    while True:
        try:
            positions = await get_ship_positions(max_age_minutes=30)
            latest_payload = {
                "type": "update",
                "ships": _serialize_positions(positions),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await manager.broadcast(latest_payload)
        except Exception as exc:
            print(f"Refresh loop error: {exc}")
        await asyncio.sleep(1)


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
        #stats { top: 10px; right: 10px; }
        #leftPanel {
            top: 10px;
            left: 10px;
            max-height: calc(100vh - 20px);
            display: flex;
            flex-direction: column;
            min-width: 280px;
            max-width: 340px;
        }
        .left-tabs {
            display: flex;
            gap: 4px;
            margin-bottom: 10px;
        }
        .left-tabs button {
            flex: 1;
            padding: 6px 8px;
            font-weight: 600;
            background: #f3f4f6;
            border: 1px solid #d1d5db;
        }
        .left-tabs button.active {
            background: #0ea5e9;
            color: #fff;
            border-color: #0284c7;
        }
        .left-tab-panel { display: none; overflow-y: auto; flex: 1; min-height: 0; }
        .left-tab-panel.active { display: block; }
        .analytics-hint {
            font-size: 11px;
            color: #6b7280;
            margin: 0 0 8px 0;
            line-height: 1.35;
        }
        .analytics-table-wrap { overflow-x: auto; margin-bottom: 10px; }
        .analytics-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 11px;
        }
        .analytics-table th, .analytics-table td {
            border: 1px solid #e5e7eb;
            padding: 4px 6px;
            text-align: right;
        }
        .analytics-table th:first-child, .analytics-table td:first-child {
            text-align: left;
            max-width: 140px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .analytics-table th { background: #f9fafb; font-weight: 600; }
        .analytics-placeholder {
            border: 1px dashed #d1d5db;
            border-radius: 6px;
            padding: 10px;
            font-size: 11px;
            color: #6b7280;
            background: #fafafa;
        }
        #legend { bottom: 10px; right: 10px; min-width: 180px; }
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
        .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
        .legend-swatch { width: 12px; height: 12px; border-radius: 3px; border: 1px solid #111827; }
    </style>
</head>
<body>
    <div id="leftPanel" class="panel">
        <div class="left-tabs" role="tablist">
            <button type="button" class="active" id="tabBtnMap" data-tab="map" role="tab" aria-selected="true">Map</button>
            <button type="button" id="tabBtnAnalytics" data-tab="analytics" role="tab" aria-selected="false">Analytics</button>
        </div>
        <div id="tabPanelMap" class="left-tab-panel active" role="tabpanel">
            <h4>Controls</h4>
            <div class="row">
                <label for="speedFilter">Speed:</label>
                <select id="speedFilter">
                    <option value="all">All</option>
                    <option value="stopped">Stopped (&lt; 1 kn)</option>
                    <option value="slow">Slow (1-4 kn)</option>
                    <option value="medium">Medium (4-12 kn)</option>
                    <option value="fast">Fast (&gt;= 12 kn)</option>
                </select>
            </div>
            <div class="row">
                <label for="trailMinutes">Trails:</label>
                <select id="trailMinutes">
                    <option value="0">Off</option>
                    <option value="15">15 min</option>
                    <option value="30" selected>30 min</option>
                    <option value="60">60 min</option>
                </select>
            </div>
            <div class="row">
                <label>Selected:</label>
                <span id="selectedShip">-</span>
            </div>
            <div class="row">
                <button id="centerSelectedBtn">Center</button>
                <button id="clearSelectedBtn">Clear</button>
            </div>
            <div class="row">
                <button id="liveBtn">Live</button>
                <button id="pauseBtn">Pause</button>
                <button id="replayBtn">Replay 2m</button>
            </div>
        </div>
        <div id="tabPanelAnalytics" class="left-tab-panel" role="tabpanel">
            <h4>Regional movement</h4>
            <p class="analytics-hint">Counts and avg speed by coarse region (same speed filter as map; full feed, not viewport sample). More metrics can plug in via API later.</p>
            <div class="analytics-table-wrap">
                <table class="analytics-table" id="analyticsTable" aria-label="Ship counts by region">
                    <thead>
                        <tr>
                            <th>Region</th>
                            <th>Ships</th>
                            <th>%</th>
                            <th>Avg kn</th>
                        </tr>
                    </thead>
                    <tbody id="analyticsTableBody"></tbody>
                </table>
            </div>
            <div class="analytics-placeholder" id="analyticsApiSlot">
                <strong>Extended analytics</strong><br>
                Placeholder for additional API-driven metrics (traffic, trends, port activity).
            </div>
        </div>
    </div>

    <div id="stats" class="panel">
        <h3>Ship Tracker RT</h3>
        <p><span class="status" id="status">Connecting...</span></p>
        <p>Shown ships: <span id="total">0</span></p>
        <p>Avg speed: <span id="avgSpeed">0.0</span> kn</p>
        <p>Fast ships (&gt;=12 kn): <span id="fastShips">0</span></p>
        <p>Updates/sec: <span id="updatesPerSec">0.0</span></p>
        <p>Last update: <span id="lastUpdate">-</span></p>
        <p>Mode: <span id="modeLabel">LIVE</span></p>
    </div>

    <div id="legend" class="panel">
        <h4>Legend</h4>
        <div class="legend-item"><span class="legend-swatch" style="background:#0ea5e9;"></span> Vessel (mini ship icon)</div>
    </div>

    <div id="map"></div>

    <script>
        const map = L.map('map').setView([20, 0], 2);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors'
        }).addTo(map);

        const markers = {};
        const markerStates = {};
        const trails = {};
        const replayBuffer = [];
        const MAX_BUFFER_FRAMES = 600;
        const updatesWindow = [];
        const TRAIL_COLOR = '#0ea5e9';
        const markerLayer = L.markerClusterGroup({
            chunkedLoading: true,
            removeOutsideVisibleBounds: true,
            disableClusteringAtZoom: 13,
            // Чуть увеличиваем радиус, чтобы чаще формировались баблы (а не singleton'ы),
            // особенно на низких зумах при очень плотных данных.
            maxClusterRadius: 55,
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

        // При клике по “кружку” кластера увеличиваем область,
        // чтобы отдельные кораблики появились и на них можно было навестиcь.
        markerLayer.on('clusterclick', function (e) {
            // MarkerCluster сам обычно делает zoomToBounds, но явно вызовем для надежности.
            renderCapOverride = 20000;
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

        const speedFilterEl = document.getElementById('speedFilter');
        const trailMinutesEl = document.getElementById('trailMinutes');
        const modeLabelEl = document.getElementById('modeLabel');
        const selectedShipEl = document.getElementById('selectedShip');
        const centerSelectedBtn = document.getElementById('centerSelectedBtn');
        const clearSelectedBtn = document.getElementById('clearSelectedBtn');
        const tabBtnMap = document.getElementById('tabBtnMap');
        const tabBtnAnalytics = document.getElementById('tabBtnAnalytics');
        const tabPanelMap = document.getElementById('tabPanelMap');
        const tabPanelAnalytics = document.getElementById('tabPanelAnalytics');
        const analyticsTableBody = document.getElementById('analyticsTableBody');

        const REGION_GRID = [
            { id: 'r0c0', name: 'N Pacific (Americas)' },
            { id: 'r0c1', name: 'N Atlantic–Europe' },
            { id: 'r0c2', name: 'N Asia–W Pacific' },
            { id: 'r1c0', name: 'Tropical E Pacific' },
            { id: 'r1c1', name: 'Tropical Atlantic–Africa' },
            { id: 'r1c2', name: 'Indian Ocean–SE Asia' },
            { id: 'r2c0', name: 'S Pacific (Americas)' },
            { id: 'r2c1', name: 'S Atlantic' },
            { id: 'r2c2', name: 'Australia–NZ' },
            { id: 'r3c0', name: 'SW Pacific' },
            { id: 'r3c1', name: 'S Atlantic–Indian' },
            { id: 'r3c2', name: 'S Indian–S Pacific' },
        ];

        function normalizeLon(lon) {
            let x = lon;
            while (x < -180) x += 360;
            while (x > 180) x -= 360;
            return x;
        }

        function getGridRegionIndex(lat, lon) {
            const L = normalizeLon(lon);
            const col = L < -60 ? 0 : (L < 60 ? 1 : 2);
            const row = lat >= 30 ? 0 : (lat >= 0 ? 1 : (lat >= -30 ? 2 : 3));
            return row * 3 + col;
        }

        function setLeftTab(which) {
            const isMap = which === 'map';
            tabBtnMap.classList.toggle('active', isMap);
            tabBtnAnalytics.classList.toggle('active', !isMap);
            tabBtnMap.setAttribute('aria-selected', isMap);
            tabBtnAnalytics.setAttribute('aria-selected', !isMap);
            tabPanelMap.classList.toggle('active', isMap);
            tabPanelAnalytics.classList.toggle('active', !isMap);
        }

        tabBtnMap.onclick = () => setLeftTab('map');
        tabBtnAnalytics.onclick = () => setLeftTab('analytics');

        function updateRegionalAnalytics(ships) {
            if (!analyticsTableBody) return;
            const totals = REGION_GRID.map(() => ({ count: 0, speedSum: 0 }));
            ships.forEach((ship) => {
                const idx = getGridRegionIndex(ship.latitude, ship.longitude);
                const slot = totals[idx];
                slot.count += 1;
                slot.speedSum += getShipSpeed(ship);
            });
            const n = ships.length || 1;
            const rows = REGION_GRID.map((r, i) => {
                const { count, speedSum } = totals[i];
                const pct = ((count / n) * 100).toFixed(1);
                const avg = count ? (speedSum / count).toFixed(1) : '–';
                return { name: r.name, count, pct, avg };
            });
            rows.sort((a, b) => b.count - a.count);
            analyticsTableBody.innerHTML = rows.map((row) => `
                <tr>
                    <td title="${row.name}">${row.name}</td>
                    <td>${row.count}</td>
                    <td>${row.pct}</td>
                    <td>${row.avg}</td>
                </tr>
            `).join('');
        }

        // Ревизия SVG-иконки: если меняем верстку createShipIcon, принудительно обновляем уже созданные маркеры.
        const ICON_REV = 5;

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

        function getShipColor(ship) {
            // Одинаковый стиль для читаемости и снижения “визуального шума”.
            return '#0ea5e9';
        }

        function createShipIcon(ship) {
            const angle = getShipAngle(ship);
            const color = getShipColor(ship);
            const svg = `
                <svg class="ship-marker" viewBox="0 0 24 24" style="transform: rotate(${angle}deg);" aria-hidden="true">
                    <circle cx="12" cy="12" r="9" fill="rgba(30, 41, 59, 0.88)" stroke="rgba(255, 255, 255, 0.35)" stroke-width="1"></circle>
                    <path d="M12 2 L19 18 L12 22 L5 18 Z" fill="ffffff" stroke="#0f172a" stroke-width="1.0" stroke-linejoin="round"></path>
                    <line x1="12" y1="6" x2="12" y2="18" stroke="#0f172a" stroke-width="1.3" stroke-linecap="round"></line>
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
            return `
                <b>Ship ID: ${ship.ship_id}</b><br>
                Lat: ${ship.latitude.toFixed(6)}<br>
                Lon: ${ship.longitude.toFixed(6)}<br>
                ${ship.course_over_ground ? `Course: ${ship.course_over_ground.toFixed(1)}°<br>` : ''}
                ${ship.speed_over_ground ? `Speed: ${ship.speed_over_ground.toFixed(1)} kn<br>` : 'Speed: 0.0 kn<br>'}
                ${ship.heading ? `Heading: ${ship.heading}°<br>` : ''}
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

        function applyShipFilter(ships) {
            const filteredNoCap = ships.filter((ship) => {
                if (!passesSpeedFilter(ship)) return false;
                return true;
            });

            // Для производительности рендерим только суда в текущем viewport.
            // KPI (Shown ships) остается “все найденные по фильтрам”, без cap/viewport.
            const bounds = map.getBounds();
            const filteredInBounds = filteredNoCap.filter((ship) => bounds.contains([ship.latitude, ship.longitude]));
            return { kpiShips: filteredNoCap, renderShips: applyZoomBasedCap(filteredInBounds) };
        }

        function getRenderCapByZoom(zoom) {
            // Подняли кап для мира: иначе при 20k судов ты видишь слишком “разреженно”.
            // Осторожно: чем выше кап, тем тяжелее для UI, но кластеризация смягчает нагрузку.
            if (zoom <= 3) return 12000;
            if (zoom <= 4) return 16000;
            if (zoom <= 6) return 20000;
            if (zoom <= 8) return 20000;
            if (zoom <= 10) return 20000;
            return 20000;
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
            // Delay зависит от того, сколько мы реально *планируем* отрисовать (кап), а не от raw-потока.
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

        function updateKpis(ships) {
            document.getElementById('total').textContent = ships.length;
            const speeds = ships.map(getShipSpeed);
            const avgSpeed = speeds.length ? (speeds.reduce((a, b) => a + b, 0) / speeds.length) : 0;
            const fastShips = speeds.filter((s) => s >= 12).length;
            document.getElementById('avgSpeed').textContent = avgSpeed.toFixed(1);
            document.getElementById('fastShips').textContent = fastShips;

            const nowMs = Date.now();
            updatesWindow.push(nowMs);
            while (updatesWindow.length && updatesWindow[0] < nowMs - 10000) {
                updatesWindow.shift();
            }
            const ups = updatesWindow.length / 10;
            document.getElementById('updatesPerSec').textContent = ups.toFixed(1);
            document.getElementById('lastUpdate').textContent = new Date(nowMs).toLocaleTimeString();

            updateRegionalAnalytics(ships);
            if (typeof window.__shipAnalyticsOnFrame === 'function') {
                try { window.__shipAnalyticsOnFrame(ships); } catch (e) { console.error(e); }
            }
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

                if (markers[shipId]) {
                    const prev = markerStates[shipId] || {};
                    const moved = Math.abs((prev.lat || 0) - lat) >= 0.00002 || Math.abs((prev.lon || 0) - lon) >= 0.00002;
                    const iconChanged = prev.iconRev !== ICON_REV || prev.angle !== angle || prev.zoom !== zoom;
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
                    markerStates[shipId] = { lat, lon, angle, zoom, iconRev: ICON_REV };
                } else {
                    markers[shipId] = L.marker([lat, lon], { icon: createShipIcon(ship), shipId: shipId })
                        .addTo(markerLayer)
                        .bindPopup(getPopup(ship));
                    markers[shipId].options.shipId = shipId;
                    markerStates[shipId] = { lat, lon, angle, zoom, iconRev: ICON_REV };

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

            updateKpis(kpiShips);

            // Обновляем иконки кластеров (особенно для count<=4, где рисуется SVG),
            // потому что iconCreateFunction для кластеров кэшируется.
            if (!clusterIconsWarm) {
                clusterIconsWarm = true;
                try {
                    markerLayer.refreshClusters();
                } catch (_) {}
            }
        }

        async function refreshTrails() {
            const minutes = Number(trailMinutesEl.value);
            Object.keys(trails).forEach((shipId) => {
                map.removeLayer(trails[shipId]);
                delete trails[shipId];
            });

            if (minutes <= 0) return;
            if (latestLiveFrame && latestLiveFrame.ships && latestLiveFrame.ships.length > 2500) return;
            try {
                const response = await fetch(`/api/trails?minutes=${minutes}`);
                if (!response.ok) return;
                const payload = await response.json();
                const trailData = payload.trails || {};
                const renderedShipIdSet = new Set(Object.keys(markers).map((s) => Number(s)));
                Object.keys(trailData).forEach((shipId) => {
                    if (!renderedShipIdSet.has(Number(shipId))) return;
                    const points = trailData[shipId].map((p) => [p.latitude, p.longitude]);
                    if (points.length < 2) return;
                    trails[shipId] = L.polyline(points, {
                        color: TRAIL_COLOR,
                        weight: 2,
                        opacity: 0.5
                    }).addTo(map);
                });
            } catch (error) {
                console.error('Failed to load trails', error);
            }
        }

        function setModeLabel() {
            if (isReplaying) modeLabelEl.textContent = 'REPLAY';
            else if (isPaused) modeLabelEl.textContent = 'PAUSED';
            else modeLabelEl.textContent = 'LIVE';
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
                statusEl.textContent = 'Connected';
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
            };

            ws.onerror = () => {
                const statusEl = document.getElementById('status');
                statusEl.textContent = 'Error';
                statusEl.className = 'status disconnected';
            };

            ws.onclose = () => {
                const statusEl = document.getElementById('status');
                statusEl.textContent = 'Disconnected';
                statusEl.className = 'status disconnected';
                if (!reconnectInterval) {
                    reconnectInterval = setInterval(connect, 3000);
                }
            };
        }

        speedFilterEl.onchange = () => {
            if (latestLiveFrame) renderFrame(latestLiveFrame);
        };

        centerSelectedBtn.onclick = () => {
            centerSelectedShip();
        };

        clearSelectedBtn.onclick = () => {
            selectedShipId = null;
            selectedShipEl.textContent = '-';
        };

        map.on('moveend', () => {
            if (latestLiveFrame && !isReplaying) {
                renderFrame(latestLiveFrame);
            }
        });

        map.on('zoomend', () => {
            if (latestLiveFrame && !isReplaying) {
                renderFrame(latestLiveFrame);
            }
            try {
                markerLayer.refreshClusters();
            } catch (_) {}
        });

        trailMinutesEl.onchange = async () => {
            await refreshTrails();
        };

        document.getElementById('liveBtn').onclick = () => {
            stopReplay();
            isPaused = false;
            setModeLabel();
            if (latestLiveFrame) renderFrame(latestLiveFrame);
        };

        document.getElementById('pauseBtn').onclick = () => {
            stopReplay();
            isPaused = true;
            setModeLabel();
        };

        document.getElementById('replayBtn').onclick = () => {
            playReplay();
        };

        setModeLabel();
        connect();
        refreshTrails();
        setInterval(refreshTrails, 20000);
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)


def _serialize_positions(positions):
    return [
        {
            "ship_id": p["ship_id"],
            "latitude": float(p["latitude"]),
            "longitude": float(p["longitude"]),
            "course_over_ground": float(p["course_over_ground"]) if p["course_over_ground"] else None,
            "speed_over_ground": float(p["speed_over_ground"]) if p["speed_over_ground"] else None,
            "heading": int(p["heading"]) if p["heading"] else None,
        }
        for p in positions
    ]


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
            # Keep socket alive and detect disconnect quickly
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
