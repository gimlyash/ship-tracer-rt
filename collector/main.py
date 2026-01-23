import asyncio
from collector.ais_client import connect_ais_stream


def main():
    asyncio.run(connect_ais_stream())

if __name__ == "__main__":
    main()
