import os
from pathlib import Path

env_file = Path(".env")
if env_file.exists():
    from dotenv import load_dotenv
    load_dotenv()

DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "postgres"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", "postgres"),
    "database": os.getenv("POSTGRES_DB", "shiptracer"),
}

AIS_API_KEY = os.getenv("SECRET_KEY_SHIPAPI")
AIS_STREAM_URL = "wss://stream.aisstream.io/v0/stream"
AIS_BOUNDING_BOXES = [[[-11, 178], [30, 74]]]

AIS_LOG_STATS_INTERVAL = int(os.getenv("AIS_LOG_STATS_INTERVAL", "5"))
AIS_LOG_DETAILED = os.getenv("AIS_LOG_DETAILED", "false").lower() == "true" 