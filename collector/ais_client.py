import websockets
import json
from datetime import datetime, timezone

from config import AIS_API_KEY, AIS_STREAM_URL, AIS_BOUNDING_BOXES
from ship_repository import save_ship_position
from db_pool import init_db_pool, close_db_pool


async def connect_ais_stream():
    """Connect to AIS stream and process messages"""
    # Dictionary to store ship names (ship_id -> name)
    ship_names = {}
    pool = await init_db_pool()
    message_count = 0
    
    try:
        subscribe_message = {
            "APIKey": AIS_API_KEY,
            "BoundingBoxes": AIS_BOUNDING_BOXES
        }
        
        async with websockets.connect(AIS_STREAM_URL) as websocket:
            subscribe_message_json = json.dumps(subscribe_message)
            await websocket.send(subscribe_message_json)

            async for message_json in websocket:
                try:
                    message = json.loads(message_json)
                    message_type = message.get("MessageType")
                    
                    if message_type == "ShipStaticData":
                        static_data = message.get('Message', {}).get('ShipStaticData', {})
                        ship_id = static_data.get('UserID')
                        ship_name = static_data.get('Name', '').strip()
                        if ship_id and ship_name:
                            was_unknown = ship_id not in ship_names
                            ship_names[ship_id] = ship_name
                            if was_unknown:
                                print(f"Got name for ShipID {ship_id}: {ship_name}")
                    
                    if message_type == "PositionReport":
                        ais_message = message.get('Message', {}).get('PositionReport', {})
                        
                        if ais_message:
                            message_count += 1
                            
                            ship_id = ais_message.get('UserID')
                            lat = ais_message.get('Latitude', 0)
                            lon = ais_message.get('Longitude', 0)
                            speed = ais_message.get('SpeedOverGround', None)
                            course = ais_message.get('CourseOverGround', None)
                            heading = ais_message.get('Heading', None)
                            
                            ship_name = ship_names.get(ship_id, 'Unknown')
                            
                            time_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            
                            info_parts = []
                            if speed is not None:
                                info_parts.append(f"Speed: {speed:.1f} kn")
                            if course is not None:
                                info_parts.append(f"Course: {course:.1f}°")
                            if heading is not None:
                                info_parts.append(f"Heading: {heading:.1f}°")
                            
                            info_str = " | ".join(info_parts) if info_parts else ""
                            
                            print(f"[{time_str}] #{message_count:4d} | ShipID: {ship_id:12d} | "
                                  f"Name: {ship_name:20s} | "
                                  f"Lat: {lat:8.5f}° | Lon: {lon:9.5f}°" + 
                                  (f" | {info_str}" if info_str else ""))
                            
                            await save_ship_position(pool, ais_message, save_history=False)
                            
                except json.JSONDecodeError as e:
                    print(f"JSON parsing error: {e}")
                except Exception as e:
                    print(f"Message processing error: {e}")
                    
    except Exception as e:
        print(f"AIS stream connection error: {e}")
    finally:
        await close_db_pool()

