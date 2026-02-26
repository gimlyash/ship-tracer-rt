# ShipTrackerRT — Real-Time Maritime Vessel Tracker

A completely free and open-source system for tracking maritime vessels in real-time using public AIS (Automatic Identification System) data.

## 🚀 Features

- **Real-time position updates** — updates every second via WebSocket without page reload
- **Interactive map** — Leaflet map with ship markers and popup information
- **High performance** — optimized JSON parsing (orjson), asynchronous processing
- **Region filtering** — configurable bounding boxes for selecting regions of interest
- **Data storage** — PostgreSQL for current positions and history
- **Database management web interface** — pgAdmin for convenient data management
- **Analytics dashboards** — Apache Superset for visualization and analysis
- **Full containerization** — Docker Compose for easy deployment

## 🛠 Tech Stack

- **Backend:**
  - Python 3.11 (asyncio, websockets)
  - FastAPI + WebSocket for real-time updates
  - orjson for fast JSON parsing (+20-50% performance boost)
  - asyncpg for asynchronous PostgreSQL operations
  - loguru for structured logging

- **Database:**
  - PostgreSQL 15
  - pgAdmin 4 for database management

- **Frontend:**
  - HTML5 + JavaScript
  - Leaflet for interactive maps
  - WebSocket for real-time updates

- **Analytics:**
  - Apache Superset for dashboards

- **Infrastructure:**
  - Docker & Docker Compose
  - All services are containerized

## 📋 Requirements

- Docker and Docker Compose
- AIS API key from [aisstream.io](https://aisstream.io) (free)

## 🚀 Quick Start

### 1. Clone the repository

```bash
git clone <repository-url>
cd ship-tracer-rt
```

### 2. Configure environment variables

Create a `.env` file based on `.env-example`:

```bash
cp .env-example .env
```

Edit `.env` and specify:
- `SECRET_KEY_SHIPAPI` — your API key from aisstream.io
- `POSTGRES_PASSWORD` — PostgreSQL password (optional)
- `PG_ADMIN_WEB_EMAIL` and `PG_ADMIN_WEB_PASSWORD` — pgAdmin credentials (optional)

### 3. Start the project

```bash
docker-compose up -d
```

### 4. Wait for initialization (30-60 seconds)

Superset will automatically configure the PostgreSQL connection. Check the logs:
```bash
docker logs ship-tracer-superset-init -f
```

### 5. Open the web interface

- **Real-time map:** http://localhost:8000
- **pgAdmin (DB management):** http://localhost:5050
- **Superset (analytics):** http://localhost:8088
  - Username: `admin`
  - Password: `admin`

## 📡 Ports and Services

| Service | Port | Description |
|---------|------|-------------|
| API (FastAPI) | 8000 | Web interface with real-time map |
| PostgreSQL | 5433 | Database (external access) |
| pgAdmin | 5050 | Database management web interface |
| Superset | 8088 | Analytics dashboards |

**Note:** Port 5433 is used for external access to PostgreSQL (to avoid conflicts with local PostgreSQL on 5432). Inside the Docker network, all services use port 5432.

## 🔧 Configuration

### Configuring tracking region

Edit `config.py`:

```python
AIS_BOUNDING_BOXES = [[[min_lat, min_lon], [max_lat, max_lon]]]
```

Example for Singapore and surrounding areas:
```python
AIS_BOUNDING_BOXES = [[[-11, 178], [30, 74]]]
```

### Logging configuration

In `.env` you can configure:

```bash
AIS_LOG_STATS_INTERVAL=5      # Statistics interval in seconds
AIS_LOG_DETAILED=false        # Detailed logging for each message
```

## 📊 Project Structure

```
ship-tracer-rt/
├── app/                    # Web application
│   ├── api_server.py      # FastAPI server with WebSocket
│   ├── database.py        # Database operations
│   └── map_utils.py       # Map utilities
├── collector/             # AIS data collection
│   ├── ais_client.py      # AIS stream client
│   ├── ship_repository.py # Database persistence
│   ├── db_pool.py         # Database connection pool
│   └── main.py            # Entry point
├── init_postgres/         # SQL scripts
│   ├── init_postgres.sql  # Database initialization
│   └── migrate_*.sql     # Migrations
├── test/                  # Tests and experiments
│   └── jupyter.ipynb     # Jupyter notebook
├── config.py             # Configuration
├── docker-compose.yaml   # Docker Compose configuration
├── Dockerfile            # Docker image
└── requirements.txt      # Python dependencies
```

## 🔍 Verification

### Checking collector

```bash
docker logs ship-tracer-collector -f
```

You should see statistics:
```
15:30:36 | 12 msg/s | Total: 105 | Known ships: 13
```

### Checking API

```bash
docker logs ship-tracer-api -f
```

### Checking database data

1. Open pgAdmin: http://localhost:5050
2. Log in with credentials from `.env`
3. Add server:
   - Host: `postgres`
   - Port: `5432`
   - Database: `shiptracer`
   - Username: `postgres`
   - Password: from `.env`
4. Check the `ship_positions_current` table

### Setting up Superset for dashboards

PostgreSQL connection is automatically configured on first startup via the `scripts/setup_superset_db.py` script.

**Automatic setup (recommended):**

1. **IMPORTANT:** Rebuild the Superset image with PostgreSQL driver:
   ```bash
   docker-compose build superset
   ```

2. Make sure all services are running:
   ```bash
   docker-compose up -d
   ```

3. Wait for Superset initialization (about 30-60 seconds):
   ```bash
   docker logs ship-tracer-superset-init -f
   ```

4. You should see the message:
   ```
   ✅ Database connection successfully created
   ```

**Manual setup (if automatic setup didn't work):**

**Option 1: Run script manually**

```bash
# Make sure Superset is running
docker-compose up -d superset

# Wait 30-60 seconds for initialization, then run the script
docker-compose run --rm superset-init python3.11 /app/scripts/setup_superset_db.py
```

Or locally (if Python and requests are installed):
```bash
export SUPERSET_URL=http://localhost:8088
export SUPERSET_USERNAME=admin
export SUPERSET_PASSWORD=admin
export POSTGRES_HOST=postgres
export POSTGRES_PORT=5432
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=postgres
export POSTGRES_DB=shiptracer

python3 scripts/setup_superset_db.py
```

**Option 2: Setup via web interface**

1. Open Superset: http://localhost:8088
2. Log in:
   - Username: `admin`
   - Password: `admin`
3. Go to **Settings** → **Database Connections** → **+ Database**
4. **IMPORTANT:** Use the **"Connect a database"** tab → select **"PostgreSQL"**
5. Fill in the form:
   - **Display Name:** `Ship Tracker PostgreSQL`
   - **SQLAlchemy URI:** `postgresql://postgres:postgres@postgres:5432/shiptracer`
     (replace `postgres:postgres` with `your_username:your_password` from `.env` if different)
   - **IMPORTANT:** Use `postgres:5432` (Docker service name), NOT `localhost:5433`!
   - Click **Test Connection** → if successful, click **Connect**

**If the form with separate fields (Host, Port) doesn't work:**
- Use **only SQLAlchemy URI** in the format: `postgresql://username:password@host:port/database`
- For Docker, use the service name `postgres` instead of `localhost`
- Port inside Docker network: `5432` (not `5433`!)

**Creating a dashboard:**

1. After connecting to the database, go to **SQL Lab** → **SQL Editor**
2. Select the `Ship Tracker PostgreSQL` database
3. Create a query to retrieve data:
   ```sql
   SELECT 
       ship_id,
       latitude,
       longitude,
       speed_over_ground,
       course_over_ground,
       heading,
       timestamp,
       updated_at
   FROM ship_positions_current
   ORDER BY updated_at DESC
   LIMIT 100;
   ```
4. Click **Run** to verify
5. Click **Explore** → **Save** to create a visualization
6. Create a dashboard and add visualizations:
   - **Table** — list of ships
   - **Map** — map with positions (if available)
   - **Chart** — speed, course graphs, etc.

**Example queries for dashboards:**

- Number of active ships:
  ```sql
  SELECT COUNT(*) as active_ships
  FROM ship_positions_current
  WHERE updated_at > NOW() - INTERVAL '1 hour';
  ```

- Average speed by ship:
  ```sql
  SELECT 
      ship_id,
      AVG(speed_over_ground) as avg_speed,
      MAX(speed_over_ground) as max_speed
  FROM ship_positions_current
  WHERE speed_over_ground IS NOT NULL
  GROUP BY ship_id
  ORDER BY avg_speed DESC;
  ```

- Position history for the last hour:
  ```sql
  SELECT 
      ship_id,
      latitude,
      longitude,
      timestamp
  FROM ship_positions_history
  WHERE timestamp > NOW() - INTERVAL '1 hour'
  ORDER BY timestamp DESC;
  ```

## 🐛 Troubleshooting

### Data not appearing on the map

1. Check that collector is running:
   ```bash
   docker ps | grep collector
   docker logs ship-tracer-collector
   ```

2. Check that data exists in the database via pgAdmin

3. Check API logs:
   ```bash
   docker logs ship-tracer-api
   ```

### Database connection errors

- Make sure PostgreSQL is running: `docker ps | grep postgres`
- Check environment variables in `.env`
- Inside Docker use `postgres:5432`, outside use `localhost:5433`

### WebSocket not connecting

- Open browser console (F12) and check for errors
- Make sure port 8000 is not occupied by another application

### Superset not connecting to database

**Error "Could not load database driver: PostgresEngineSpec":**

This means the PostgreSQL driver is not installed in the Superset image. Solution:

1. Rebuild the Superset image:
   ```bash
   docker-compose build superset
   docker-compose up -d superset
   ```

2. Wait for restart (30-60 seconds) and try connecting again

**Other issues:**

1. Check initialization logs:
   ```bash
   docker logs ship-tracer-superset-init
   ```

2. If automatic setup didn't work, configure manually (see "Setting up Superset for dashboards" section)

3. Make sure you're using the correct SQLAlchemy URI:
   ```
   postgresql://postgres:postgres@postgres:5432/shiptracer
   ```
   (replace password with yours from `.env`)

4. Check that PostgreSQL is accessible:
   ```bash
   docker ps | grep postgres
   docker logs ship-tracer-postgres
   ```

5. Make sure environment variables in `.env` are correct

## 📈 Performance

- **JSON parsing:** orjson provides +20-50% speed compared to standard json
- **Logging:** asynchronous logging (loguru) doesn't block the main thread
- **Updates:** real-time updates every second without page reload
- **Database:** asynchronous queries via asyncpg for maximum performance
