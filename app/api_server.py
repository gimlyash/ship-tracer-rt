"""FastAPI server with WebSocket for real-time ship position updates"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import List

# Imports from project root (PYTHONPATH=/app in Docker)
from config import DB_CONFIG
from app.database import get_ship_positions

app = FastAPI(title="Ship Tracker RT API")

# Store active WebSocket connections
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
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
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { margin: 0; padding: 0; font-family: Arial, sans-serif; }
        #map { height: 100vh; width: 100%; }
        #stats {
            position: absolute;
            top: 10px;
            right: 10px;
            background: white;
            padding: 15px;
            border-radius: 5px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            z-index: 1000;
            min-width: 200px;
        }
        #stats h3 { margin: 0 0 10px 0; }
        #stats p { margin: 5px 0; }
        .status { font-weight: bold; }
        .status.connected { color: green; }
        .status.disconnected { color: red; }
    </style>
</head>
<body>
    <div id="stats">
        <h3>🚢 Ship Tracker RT</h3>
        <p><span class="status" id="status">Connecting...</span></p>
        <p>Total ships: <span id="total">0</span></p>
        <p>Last update: <span id="lastUpdate">-</span></p>
    </div>
    <div id="map"></div>
    
    <script>
        const map = L.map('map').setView([1.3521, 103.8198], 10);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors'
        }).addTo(map);
        
        const markers = {};
        let ws = null;
        let reconnectInterval = null;
        
        function connect() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {
                document.getElementById('status').textContent = 'Connected';
                document.getElementById('status').className = 'status connected';
                if (reconnectInterval) {
                    clearInterval(reconnectInterval);
                    reconnectInterval = null;
                }
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                updateMap(data);
            };
            
            ws.onerror = () => {
                document.getElementById('status').textContent = 'Error';
                document.getElementById('status').className = 'status disconnected';
            };
            
            ws.onclose = () => {
                document.getElementById('status').textContent = 'Disconnected';
                document.getElementById('status').className = 'status disconnected';
                // Reconnect after 3 seconds
                if (!reconnectInterval) {
                    reconnectInterval = setInterval(connect, 3000);
                }
            };
        }
        
        function updateMap(data) {
            // Update statistics
            document.getElementById('total').textContent = data.ships.length;
            const now = new Date();
            document.getElementById('lastUpdate').textContent = now.toLocaleTimeString();
            
            // Visual feedback that update happened
            const statusEl = document.getElementById('status');
            if (statusEl) {
                statusEl.textContent = 'Connected • Updated';
                setTimeout(() => {
                    statusEl.textContent = 'Connected';
                }, 500);
            }
            
            // Update markers
            const currentShipIds = new Set();
            
            data.ships.forEach(ship => {
                const shipId = ship.ship_id;
                currentShipIds.add(shipId);
                
                const lat = ship.latitude;
                const lon = ship.longitude;
                
                if (markers[shipId]) {
                    // Update existing marker position and popup
                    markers[shipId].setLatLng([lat, lon]);
                    // Update popup with latest data
                    const popup = `
                        <b>🚢 Ship ID: ${shipId}</b><br>
                        Lat: ${lat.toFixed(6)}<br>
                        Lon: ${lon.toFixed(6)}<br>
                        ${ship.course_over_ground ? `Course: ${ship.course_over_ground.toFixed(1)}°<br>` : ''}
                        ${ship.speed_over_ground ? `Speed: ${ship.speed_over_ground.toFixed(1)} kn<br>` : ''}
                        ${ship.heading ? `Heading: ${ship.heading}°<br>` : ''}
                    `;
                    markers[shipId].setPopupContent(popup);
                } else {
                    // Create new marker
                    const popup = `
                        <b>🚢 Ship ID: ${shipId}</b><br>
                        Lat: ${lat.toFixed(6)}<br>
                        Lon: ${lon.toFixed(6)}<br>
                        ${ship.course_over_ground ? `Course: ${ship.course_over_ground.toFixed(1)}°<br>` : ''}
                        ${ship.speed_over_ground ? `Speed: ${ship.speed_over_ground.toFixed(1)} kn<br>` : ''}
                        ${ship.heading ? `Heading: ${ship.heading}°<br>` : ''}
                    `;
                    
                    const icon = L.icon({
                        iconUrl: 'https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-blue.png',
                        iconSize: [25, 41],
                        iconAnchor: [12, 41],
                        popupAnchor: [1, -34]
                    });
                    
                    markers[shipId] = L.marker([lat, lon], { icon: icon })
                        .addTo(map)
                        .bindPopup(popup);
                }
            });
            
            // Remove markers for ships that are no longer in the data
            Object.keys(markers).forEach(shipId => {
                if (!currentShipIds.has(parseInt(shipId))) {
                    map.removeLayer(markers[shipId]);
                    delete markers[shipId];
                }
            });
        }
        
        // Connect on page load
        connect();
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time ship position updates"""
    await manager.connect(websocket)
    try:
        # Send initial data
        positions = await get_ship_positions(max_age_minutes=30)
        await websocket.send_json({
            "type": "update",
            "ships": [
                {
                    "ship_id": p["ship_id"],
                    "latitude": float(p["latitude"]),
                    "longitude": float(p["longitude"]),
                    "course_over_ground": float(p["course_over_ground"]) if p["course_over_ground"] else None,
                    "speed_over_ground": float(p["speed_over_ground"]) if p["speed_over_ground"] else None,
                    "heading": int(p["heading"]) if p["heading"] else None,
                }
                for p in positions
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        
        # Keep connection alive and send periodic updates
        while True:
            await asyncio.sleep(1)  # Update every 1 second for real-time updates
            
            try:
                positions = await get_ship_positions(max_age_minutes=30)
                await websocket.send_json({
                    "type": "update",
                    "ships": [
                        {
                            "ship_id": p["ship_id"],
                            "latitude": float(p["latitude"]),
                            "longitude": float(p["longitude"]),
                            "course_over_ground": float(p["course_over_ground"]) if p["course_over_ground"] else None,
                            "speed_over_ground": float(p["speed_over_ground"]) if p["speed_over_ground"] else None,
                            "heading": int(p["heading"]) if p["heading"] else None,
                        }
                        for p in positions
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            except Exception as e:
                # Log error but continue
                print(f"Error sending update: {e}")
                await asyncio.sleep(1)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
