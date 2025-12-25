"""Common configuration for the entire project"""
import os
from dotenv import load_dotenv

load_dotenv()

# Database configuration
DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "postgres"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", "postgres"),
    "database": os.getenv("POSTGRES_DB", "shiptracer"),
}

# AIS Stream configuration
AIS_API_KEY = os.getenv("SECRET_KEY_SHIPAPI", "<YOUR API KEY>")
AIS_STREAM_URL = "wss://stream.aisstream.io/v0/stream"
AIS_BOUNDING_BOXES = [[[-11, 178], [30, 74]]]

