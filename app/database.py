import asyncpg
from typing import List, Optional
from datetime import datetime, timezone, timedelta

from config import DB_CONFIG

# Try to import streamlit, but don't fail if not available
try:
    import streamlit as st
except ImportError:
    st = None


async def get_ship_positions(max_age_minutes: int = 30) -> List:
    try:
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
    except Exception as e:
        # Try to use streamlit if available, otherwise just print
        try:
            import streamlit as st
            st.error(f"Database connection error: {e}")
        except ImportError:
            print(f"Database connection error: {e}")
        return []


async def init_database():
    try:
        conn = await asyncpg.connect(**DB_CONFIG)
        
        check_query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'ship_positions_current'
            );
        """
        
        exists = await conn.fetchval(check_query)
        await conn.close()
        
        if not exists:
            if st:
                st.warning("Database tables not found. Make sure init_postgres.sql is executed.")
            else:
                print("Database tables not found. Make sure init_postgres.sql is executed.")
            return False
            
        return True
    except Exception as e:
        if st:
            st.error(f"Database initialization error: {e}")
        else:
            print(f"Database initialization error: {e}")
        return False

