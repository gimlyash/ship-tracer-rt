-- Adds explicit PK/FK relationships for ERD visualization.
-- Safe to run multiple times.

-- 1) Ship dimension table
CREATE TABLE IF NOT EXISTS ships (
    ship_id BIGINT PRIMARY KEY,
    first_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_ship_type SMALLINT
);

-- 2) Backfill ships from existing position tables
INSERT INTO ships (ship_id, first_seen, last_seen, last_ship_type)
SELECT
    c.ship_id,
    COALESCE(c.timestamp, NOW()) AS first_seen,
    COALESCE(c.updated_at, c.timestamp, NOW()) AS last_seen,
    c.ship_type
FROM ship_positions_current c
ON CONFLICT (ship_id) DO UPDATE SET
    last_seen = GREATEST(ships.last_seen, EXCLUDED.last_seen),
    last_ship_type = COALESCE(EXCLUDED.last_ship_type, ships.last_ship_type);

INSERT INTO ships (ship_id, first_seen, last_seen, last_ship_type)
SELECT
    h.ship_id,
    MIN(COALESCE(h.timestamp, NOW())) AS first_seen,
    MAX(COALESCE(h.timestamp, NOW())) AS last_seen,
    NULL::SMALLINT AS last_ship_type
FROM ship_positions_history h
GROUP BY h.ship_id
ON CONFLICT (ship_id) DO UPDATE SET
    first_seen = LEAST(ships.first_seen, EXCLUDED.first_seen),
    last_seen = GREATEST(ships.last_seen, EXCLUDED.last_seen);

-- 3) Trigger function to keep ships in sync
CREATE OR REPLACE FUNCTION ensure_ship_dimension_row()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO ships (ship_id, first_seen, last_seen, last_ship_type)
    VALUES (
        NEW.ship_id,
        COALESCE(NEW.timestamp, NOW()),
        COALESCE(NEW.updated_at, NEW.timestamp, NOW()),
        NEW.ship_type
    )
    ON CONFLICT (ship_id)
    DO UPDATE SET
        last_seen = GREATEST(ships.last_seen, COALESCE(NEW.updated_at, NEW.timestamp, NOW())),
        last_ship_type = COALESCE(EXCLUDED.last_ship_type, ships.last_ship_type);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_current_ensure_ship ON ship_positions_current;
CREATE TRIGGER trg_current_ensure_ship
BEFORE INSERT OR UPDATE ON ship_positions_current
FOR EACH ROW EXECUTE FUNCTION ensure_ship_dimension_row();

DROP TRIGGER IF EXISTS trg_history_ensure_ship ON ship_positions_history;
CREATE TRIGGER trg_history_ensure_ship
BEFORE INSERT ON ship_positions_history
FOR EACH ROW EXECUTE FUNCTION ensure_ship_dimension_row();

-- 4) FK: current/history -> ships
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ship_positions_current_ship'
    ) THEN
        ALTER TABLE ship_positions_current
        ADD CONSTRAINT fk_ship_positions_current_ship
        FOREIGN KEY (ship_id) REFERENCES ships(ship_id) ON DELETE CASCADE;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ship_positions_history_ship'
    ) THEN
        ALTER TABLE ship_positions_history
        ADD CONSTRAINT fk_ship_positions_history_ship
        FOREIGN KEY (ship_id) REFERENCES ships(ship_id) ON DELETE CASCADE;
    END IF;
END $$;

-- 5) Station kind dictionary + FK: ais_stations.kind -> ais_station_kinds.kind
CREATE TABLE IF NOT EXISTS ais_station_kinds (
    kind VARCHAR(32) PRIMARY KEY,
    title TEXT NOT NULL
);

INSERT INTO ais_station_kinds(kind, title) VALUES
    ('ais_base', 'AIS base station'),
    ('ais_aton', 'AIS aid to navigation'),
    ('major_port', 'Major reference port')
ON CONFLICT (kind) DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ais_stations_kind'
    ) THEN
        ALTER TABLE ais_stations
        ADD CONSTRAINT fk_ais_stations_kind
        FOREIGN KEY (kind) REFERENCES ais_station_kinds(kind);
    END IF;
END $$;

