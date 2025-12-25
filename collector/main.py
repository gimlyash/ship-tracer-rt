"""Main entry point for AIS data collection"""
import asyncio
from ais_client import connect_ais_stream


def main():
    """Start AIS data collection"""
    asyncio.run(connect_ais_stream())


if __name__ == "__main__":
    main()

