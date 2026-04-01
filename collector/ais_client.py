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
from collector.ship_repository import save_ship_position
from collector.station_repository import save_ais_station
from collector.db_pool import init_db_pool, close_db_pool

logger.remove()
logger.add(
    lambda msg: print(msg, end="", flush=True),
    format="{message}",
    level="INFO",
    enqueue=True,
    colorize=False
)

_POSITION_KEYS = {
    "PositionReport": "PositionReport",
    "ExtendedClassBPositionReport": "ExtendedClassBPositionReport",
    "StandardClassBPositionReport": "StandardClassBPositionReport",
}


def _remember_ship_type(mmsi, inner: dict, ship_types: dict) -> None:
    t = inner.get("ShipType")
    if t is None:
        t = inner.get("Type")
    if mmsi is not None and t is not None:
        try:
            ship_types[mmsi] = int(t)
        except (TypeError, ValueError):
            pass


def _merge_type_into_row(mmsi: int, inner: dict, ship_types: dict) -> dict:
    _remember_ship_type(mmsi, inner, ship_types)
    row = dict(inner)
    if mmsi in ship_types:
        row["ShipType"] = ship_types[mmsi]
    return row


async def _maybe_save_position(
    pool,
    inner: dict,
    ship_types: dict,
    last_saved_positions: dict,
    last_saved_times: dict,
) -> bool:
    """Persist position if movement/throttle rules pass. Returns True if written."""
    mmsi = inner.get("UserID")
    lat = inner.get("Latitude")
    lon = inner.get("Longitude")
    if mmsi is None or lat is None or lon is None:
        return False

    pos_row = _merge_type_into_row(mmsi, inner, ship_types)
    speed = pos_row.get("Sog")
    course = pos_row.get("Cog")
    true_heading = pos_row.get("TrueHeading", None)
    heading = None if (true_heading is None or true_heading == 511) else true_heading

    now_ts = datetime.now(timezone.utc)
    prev = last_saved_positions.get(mmsi)
    should_save = True
    save_history = False

    if prev is not None:
        moved = (
            abs(lat - prev["lat"]) >= 0.00005
            or abs(lon - prev["lon"]) >= 0.00005
        )
        speed_changed = abs((speed or 0.0) - prev["speed"]) >= 0.5
        course_changed = abs((course or 0.0) - prev["course"]) >= 5.0
        heading_changed = abs((heading or 0.0) - prev["heading"]) >= 5.0
        last_save_time = last_saved_times.get(mmsi, now_ts)
        too_old = (now_ts - last_save_time).total_seconds() >= 15

        should_save = moved or speed_changed or course_changed or heading_changed or too_old
        save_history = moved
    else:
        save_history = True

    if should_save:
        await save_ship_position(pool, pos_row, save_history=save_history)
        last_saved_positions[mmsi] = {
            "lat": lat,
            "lon": lon,
            "speed": speed or 0.0,
            "course": course or 0.0,
            "heading": heading or 0.0,
        }
        last_saved_times[mmsi] = now_ts
    return should_save


async def connect_ais_stream():
    """Connect to AIS stream and process messages."""
    ship_names = {}
    ship_types = {}
    last_saved_positions = {}
    last_saved_times = {}
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
                        stype = static.get("ShipType")
                        if stype is None:
                            stype = static.get("Type")
                        if mmsi is not None and stype is not None:
                            try:
                                ship_types[mmsi] = int(stype)
                            except (TypeError, ValueError):
                                pass
                        if mmsi and name and mmsi not in ship_names:
                            ship_names[mmsi] = name
                            logger.info(f"New ship {mmsi}: {name}")

                    elif msg_type == "StaticDataReport":
                        sdr = message.get("Message", {}).get("StaticDataReport", {})
                        mmsi = sdr.get("UserID")
                        rb = sdr.get("ReportB") or {}
                        if rb.get("Valid") and mmsi is not None:
                            st = rb.get("ShipType")
                            if st is not None:
                                try:
                                    ship_types[mmsi] = int(st)
                                except (TypeError, ValueError):
                                    pass

                    elif msg_type == "BaseStationReport":
                        bs = message.get("Message", {}).get("BaseStationReport", {})
                        mmsi = bs.get("UserID")
                        lat = bs.get("Latitude")
                        lon = bs.get("Longitude")
                        if mmsi is not None and lat is not None and lon is not None:
                            try:
                                await save_ais_station(
                                    pool,
                                    mmsi=int(mmsi),
                                    kind="ais_base",
                                    latitude=float(lat),
                                    longitude=float(lon),
                                    name=None,
                                    type_code=int(bs["FixType"]) if bs.get("FixType") is not None else None,
                                )
                            except (TypeError, ValueError) as e:
                                logger.debug(f"BaseStationReport skip: {e}")

                    elif msg_type == "AidsToNavigationReport":
                        aton = message.get("Message", {}).get("AidsToNavigationReport", {})
                        mmsi = aton.get("UserID")
                        lat = aton.get("Latitude")
                        lon = aton.get("Longitude")
                        if mmsi is not None and lat is not None and lon is not None:
                            nm = (aton.get("Name") or "").strip() or None
                            tc = aton.get("Type")
                            try:
                                await save_ais_station(
                                    pool,
                                    mmsi=int(mmsi),
                                    kind="ais_aton",
                                    latitude=float(lat),
                                    longitude=float(lon),
                                    name=nm,
                                    type_code=int(tc) if tc is not None else None,
                                )
                            except (TypeError, ValueError) as e:
                                logger.debug(f"AidsToNavigationReport skip: {e}")

                    elif msg_type == "LongRangeAisBroadcastMessage":
                        lr = message.get("Message", {}).get("LongRangeAisBroadcastMessage", {})
                        lat = lr.get("Latitude")
                        lon = lr.get("Longitude")
                        if lat is None:
                            lat = lr.get("Latitude1")
                        if lon is None:
                            lon = lr.get("Longitude1")
                        if lat is None or lon is None:
                            continue
                        inner = dict(lr)
                        inner["Latitude"] = lat
                        inner["Longitude"] = lon
                        now = datetime.now(timezone.utc)
                        if (now - last_stat_time).total_seconds() >= AIS_LOG_STATS_INTERVAL:
                            rate = msg_count_interval / AIS_LOG_STATS_INTERVAL
                            logger.info(
                                f"{now.strftime('%H:%M:%S')} | {rate:.0f} msg/s | "
                                f"Total: {msg_count} | Known ships: {len(ship_names)}"
                            )
                            last_stat_time = now
                            msg_count_interval = 0
                        await _maybe_save_position(
                            pool, inner, ship_types, last_saved_positions, last_saved_times
                        )

                    elif msg_type in _POSITION_KEYS:
                        key = _POSITION_KEYS[msg_type]
                        pos = message.get("Message", {}).get(key, {})
                        if not pos:
                            continue

                        mmsi = pos.get("UserID")
                        name = (pos.get("Name") or "").strip()
                        if mmsi and name and mmsi not in ship_names:
                            ship_names[mmsi] = name
                            logger.info(f"New ship {mmsi}: {name}")

                        lat = pos.get("Latitude")
                        lon = pos.get("Longitude")
                        speed = pos.get("Sog", None)
                        course = pos.get("Cog", None)
                        true_heading = pos.get("TrueHeading", None)
                        heading = None if (true_heading is None or true_heading == 511) else true_heading

                        now = datetime.now(timezone.utc)
                        if (now - last_stat_time).total_seconds() >= AIS_LOG_STATS_INTERVAL:
                            rate = msg_count_interval / AIS_LOG_STATS_INTERVAL
                            logger.info(
                                f"{now.strftime('%H:%M:%S')} | {rate:.0f} msg/s | "
                                f"Total: {msg_count} | Known ships: {len(ship_names)}"
                            )
                            last_stat_time = now
                            msg_count_interval = 0

                        if AIS_LOG_DETAILED and msg_type == "PositionReport":
                            time_str = now.strftime("%H:%M:%S")
                            info_parts = []
                            if speed is not None and speed > 0:
                                info_parts.append(f"Speed: {speed:.1f} kn")
                            if course is not None:
                                info_parts.append(f"Course: {course:.1f}°")
                            if heading is not None:
                                info_parts.append(f"Heading: {heading:.1f}°")
                            info_str = " | ".join(info_parts) if info_parts else ""
                            nm = ship_names.get(mmsi, "Unknown")
                            logger.info(
                                f"[{time_str}] #{msg_count:4d} | ShipID: {mmsi:12d} | "
                                f"Name: {nm:20s} | "
                                f"Lat: {lat:8.5f}° | Lon: {lon:9.5f}°" +
                                (f" | {info_str}" if info_str else "")
                            )

                        await _maybe_save_position(
                            pool, pos, ship_types, last_saved_positions, last_saved_times
                        )

                except Exception as e:
                    logger.error(f"Message processing error: {e}")

    except Exception as e:
        logger.error(f"AIS stream connection error: {e}")
    finally:
        await close_db_pool()
