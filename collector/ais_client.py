import websockets
from datetime import datetime, timezone
from loguru import logger

try:
    import orjson as json
    JSON_LOADS = json.loads
    JSON_DUMPS = lambda x: json.dumps(x).decode('utf-8')
except ImportError:
    import json
    JSON_LOADS = json.loads
    JSON_DUMPS = json.dumps
    logger.warning("orjson not installed, using standard json library. Install orjson for better performance.")

from config import AIS_API_KEY, AIS_STREAM_URL, AIS_BOUNDING_BOXES, AIS_LOG_STATS_INTERVAL, AIS_LOG_DETAILED
from ship_repository import save_ship_position
from db_pool import init_db_pool, close_db_pool

logger.remove()
logger.add(
    lambda msg: print(msg, end="", flush=True),
    format="{message}",
    level="INFO",
    enqueue=True,
    colorize=False
)


async def connect_ais_stream():
    """Connect to AIS stream and process messages"""
    ship_names = {}
    pool = await init_db_pool()
    msg_count = 0
    msg_count_interval = 0
    last_stat_time = datetime.now(timezone.utc)
    
    try:
        async with websockets.connect(AIS_STREAM_URL) as websocket:
            await websocket.send(JSON_DUMPS({
                "APIKey": AIS_API_KEY,
                "BoundingBoxes": AIS_BOUNDING_BOXES
            }))

            async for message_json in websocket:
                try:
                    msg_count += 1
                    msg_count_interval += 1
                    
                    message = JSON_LOADS(message_json)
                    msg_type = message.get("MessageType")
                    
                    if msg_type == "ShipStaticData":
                        static = message.get("Message", {}).get("ShipStaticData", {})
                        mmsi = static.get("UserID")
                        name = static.get("Name", "").strip()
                        if mmsi and name and mmsi not in ship_names:
                            ship_names[mmsi] = name
                            logger.info(f"New ship {mmsi}: {name}")
                    
                    elif msg_type == "PositionReport":
                        pos = message.get("Message", {}).get("PositionReport", {})
                        if not pos:
                            continue
                        
                        mmsi = pos.get("UserID")
                        lat = pos.get("Latitude")
                        lon = pos.get("Longitude")
                        
                        if mmsi is None or lat is None or lon is None:
                            continue
                        
                        speed = pos.get("Sog", None)
                        course = pos.get("Cog", None)
                        true_heading = pos.get("TrueHeading", None)
                        heading = None if (true_heading is None or true_heading == 511) else true_heading
                        
                        name = ship_names.get(mmsi, "Unknown")
                        
                        now = datetime.now(timezone.utc)
                        if (now - last_stat_time).total_seconds() >= AIS_LOG_STATS_INTERVAL:
                            rate = msg_count_interval / AIS_LOG_STATS_INTERVAL
                            logger.info(
                                f"{now.strftime('%H:%M:%S')} | {rate:.0f} msg/s | "
                                f"Total: {msg_count} | Known ships: {len(ship_names)}"
                            )
                            last_stat_time = now
                            msg_count_interval = 0
                        
                        if AIS_LOG_DETAILED:
                            time_str = now.strftime("%H:%M:%S")
                            info_parts = []
                            if speed is not None and speed > 0:
                                info_parts.append(f"Speed: {speed:.1f} kn")
                            if course is not None:
                                info_parts.append(f"Course: {course:.1f}°")
                            if heading is not None:
                                info_parts.append(f"Heading: {heading:.1f}°")
                            info_str = " | ".join(info_parts) if info_parts else ""
                            
                            logger.info(
                                f"[{time_str}] #{msg_count:4d} | ShipID: {mmsi:12d} | "
                                f"Name: {name:20s} | "
                                f"Lat: {lat:8.5f}° | Lon: {lon:9.5f}°" + 
                                (f" | {info_str}" if info_str else "")
                            )
                        
                        await save_ship_position(pool, pos, save_history=False)
                            
                except Exception as e:
                    logger.error(f"Message processing error: {e}")
                    
    except Exception as e:
        logger.error(f"AIS stream connection error: {e}")
    finally:
        await close_db_pool()

