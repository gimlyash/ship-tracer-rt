"""AIS fixed stations: base stations (msg 4), aids to navigation (msg 21)."""
from datetime import datetime, timezone

import asyncpg


async def upsert_ais_station(
    conn: asyncpg.Connection,
    mmsi: int,
    kind: str,
    latitude: float,
    longitude: float,
    name: str | None = None,
    type_code: int | None = None,
) -> None:
    """Insert or update a fixed AIS station (same MMSI overwrites)."""
    now = datetime.now(timezone.utc)
    nm = (name or "").strip() or None
    await conn.execute(
        """
        INSERT INTO ais_stations (mmsi, kind, name, latitude, longitude, type_code, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (mmsi) DO UPDATE SET
            kind = EXCLUDED.kind,
            name = COALESCE(EXCLUDED.name, ais_stations.name),
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            type_code = COALESCE(EXCLUDED.type_code, ais_stations.type_code),
            updated_at = EXCLUDED.updated_at
        """,
        mmsi,
        kind,
        nm,
        latitude,
        longitude,
        type_code,
        now,
    )


async def save_ais_station(
    pool,
    *,
    mmsi: int,
    kind: str,
    latitude: float,
    longitude: float,
    name: str | None = None,
    type_code: int | None = None,
):
    async with pool.acquire() as conn:
        await upsert_ais_station(
            conn,
            mmsi=mmsi,
            kind=kind,
            latitude=latitude,
            longitude=longitude,
            name=name,
            type_code=type_code,
        )
