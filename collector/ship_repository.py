"""Repository for working with ship data in database"""
import asyncpg
from datetime import datetime, timezone


async def upsert_ship_position(conn: asyncpg.Connection, ship_data: dict):
    """UPSERT current ship position"""
    ship_id = ship_data.get('UserID')
    latitude = ship_data.get('Latitude')
    longitude = ship_data.get('Longitude')
    course_over_ground = ship_data.get('CourseOverGround')
    speed_over_ground = ship_data.get('SpeedOverGround')
    heading = ship_data.get('Heading')
    navigational_status = ship_data.get('NavigationalStatus')
    rate_of_turn = ship_data.get('RateOfTurn')
    timestamp = datetime.now(timezone.utc)
    
    upsert_query = """
        INSERT INTO ship_positions_current (
            ship_id, latitude, longitude, course_over_ground, 
            speed_over_ground, heading, navigational_status, 
            rate_of_turn, timestamp, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (ship_id) 
        DO UPDATE SET
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            course_over_ground = EXCLUDED.course_over_ground,
            speed_over_ground = EXCLUDED.speed_over_ground,
            heading = EXCLUDED.heading,
            navigational_status = EXCLUDED.navigational_status,
            rate_of_turn = EXCLUDED.rate_of_turn,
            timestamp = EXCLUDED.timestamp,
            updated_at = EXCLUDED.updated_at
    """
    
    await conn.execute(
        upsert_query,
        ship_id, latitude, longitude, course_over_ground,
        speed_over_ground, heading, navigational_status,
        rate_of_turn, timestamp, timestamp
    )


async def insert_history_position(conn: asyncpg.Connection, ship_data: dict):
    """Insert position into history"""
    ship_id = ship_data.get('UserID')
    latitude = ship_data.get('Latitude')
    longitude = ship_data.get('Longitude')
    course_over_ground = ship_data.get('CourseOverGround')
    speed_over_ground = ship_data.get('SpeedOverGround')
    heading = ship_data.get('Heading')
    navigational_status = ship_data.get('NavigationalStatus')
    rate_of_turn = ship_data.get('RateOfTurn')
    timestamp = datetime.now(timezone.utc)
    
    insert_query = """
        INSERT INTO ship_positions_history (
            ship_id, latitude, longitude, course_over_ground,
            speed_over_ground, heading, navigational_status,
            rate_of_turn, timestamp
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    """
    
    await conn.execute(
        insert_query,
        ship_id, latitude, longitude, course_over_ground,
        speed_over_ground, heading, navigational_status,
        rate_of_turn, timestamp
    )


async def save_ship_position(pool, ship_data: dict, save_history: bool = False):
    """Save ship position to database (UPSERT + optionally history)"""
    try:
        async with pool.acquire() as conn:
            await upsert_ship_position(conn, ship_data)
            
            if save_history:
                await insert_history_position(conn, ship_data)
    except Exception as e:
        print(f"Error saving to database: {e}")

