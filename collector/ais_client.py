import websockets
import json
from datetime import datetime, timezone

from config import AIS_API_KEY, AIS_STREAM_URL, AIS_BOUNDING_BOXES
from ship_repository import save_ship_position
from db_pool import init_db_pool, close_db_pool


async def process_ais_message(message: dict, pool):
    """Process single AIS message"""
    message_type = message.get("MessageType")

    if message_type == "PositionReport":
        ais_message = message.get('Message', {}).get('PositionReport', {})
        
        if ais_message:
            ship_id = ais_message.get('UserID')
            lat = ais_message.get('Latitude')
            lon = ais_message.get('Longitude')
            
            print(f"[{datetime.now(timezone.utc)}] ShipId: {ship_id} "
                  f"Latitude: {lat} Longitude: {lon}")
            
            await save_ship_position(pool, ais_message, save_history=False)


async def connect_ais_stream():
    """Connect to AIS stream and process messages"""
    pool = await init_db_pool()
    
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
                    await process_ais_message(message, pool)
                except json.JSONDecodeError as e:
                    print(f"JSON parsing error: {e}")
                except Exception as e:
                    print(f"Message processing error: {e}")
                    
    except Exception as e:
        print(f"AIS stream connection error: {e}")
    finally:
        await close_db_pool()

