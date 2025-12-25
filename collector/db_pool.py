"""Database connection pool management"""
import asyncpg
from config import DB_CONFIG

db_pool = None


async def init_db_pool():
    """Initialize database connection pool"""
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(**DB_CONFIG, min_size=2, max_size=10)
    return db_pool


async def close_db_pool():
    """Close database connection pool"""
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None


async def get_connection():
    """Get connection from pool"""
    pool = await init_db_pool()
    return await pool.acquire()


async def release_connection(conn):
    """Release connection back to pool"""
    pool = await init_db_pool()
    await pool.release(conn)

