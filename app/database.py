import asyncpg
from typing import Dict, List

from config import DB_CONFIG


db_pool = None


async def init_db_pool():
        """Initialize shared DB pool for API service."""
        global db_pool
        if db_pool is None:
            db_pool = await asyncpg.create_pool(**DB_CONFIG, min_size=2, max_size=20)
        return db_pool


async def close_db_pool():
        """Close shared DB pool for API service."""
        global db_pool
        if db_pool:
            await db_pool.close()
            db_pool = None


async def get_ship_positions(max_age_minutes: int = 30) -> List:
        pool = await init_db_pool()
        
        query = """
            SELECT 
                ship_id,
                latitude,
                longitude,
                course_over_ground,
                speed_over_ground,
                heading,
                navigational_status,
                rate_of_turn,
                ship_type,
                timestamp,
                updated_at
            FROM ship_positions_current
            WHERE updated_at > NOW() - INTERVAL '%s minutes'
            ORDER BY updated_at DESC
        """ % max_age_minutes
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
        return rows


async def get_ais_stations() -> List:
    """Rows from ais_stations (base stations, AtoN). Empty if table missing."""
    pool = await init_db_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT mmsi, kind, name, latitude, longitude, type_code
                FROM ais_stations
                ORDER BY kind, mmsi
                """
            )
        return rows
    except Exception:
        return []


async def get_ship_trails(trail_minutes: int = 30, points_per_ship: int = 60) -> Dict[int, List[Dict]]:
        pool = await init_db_pool()

        query = """
            WITH ranked_history AS (
                SELECT
                    ship_id,
                    latitude,
                    longitude,
                    timestamp,
                    ROW_NUMBER() OVER (
                        PARTITION BY ship_id
                        ORDER BY timestamp DESC
                    ) AS rn
                FROM ship_positions_history
                WHERE timestamp > NOW() - ($1::int * INTERVAL '1 minute')
            )
            SELECT
                ship_id,
                latitude,
                longitude,
                timestamp
            FROM ranked_history
            WHERE rn <= $2
            ORDER BY ship_id, timestamp ASC;
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, trail_minutes, points_per_ship)

        trails: Dict[int, List[Dict]] = {}
        for row in rows:
            ship_id = int(row["ship_id"])
            trails.setdefault(ship_id, []).append(
                {
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
                }
            )
        return trails


async def init_database():
        pool = await init_db_pool()
        
        check_query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'ship_positions_current'
            );
        """
        
        async with pool.acquire() as conn:
            exists = await conn.fetchval(check_query)