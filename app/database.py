import asyncpg
from typing import List, Optional
from datetime import datetime, timezone, timedelta

from config import DB_CONFIG

async def get_ship_positions(max_age_minutes: int = 30) -> List:
        conn = await asyncpg.connect(**DB_CONFIG)
        
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
                timestamp,
                updated_at
            FROM ship_positions_current
            WHERE updated_at > NOW() - INTERVAL '%s minutes'
            ORDER BY updated_at DESC
        """ % max_age_minutes
        rows = await conn.fetch(query)
        await conn.close()
        return rows


async def init_database():
        conn = await asyncpg.connect(**DB_CONFIG)
        
        check_query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'ship_positions_current'
            );
        """
        
        exists = await conn.fetchval(check_query)
        await conn.close()