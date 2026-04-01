-- Таблица для текущих позиций судов (UPSERT)
CREATE TABLE IF NOT EXISTS ship_positions_current (
    ship_id BIGINT PRIMARY KEY,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    course_over_ground DOUBLE PRECISION,
    speed_over_ground DOUBLE PRECISION,
    heading INTEGER,
    navigational_status INTEGER,
    rate_of_turn DOUBLE PRECISION,
    ship_type SMALLINT,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Таблица для истории позиций (с автоматической очисткой старых данных)
CREATE TABLE IF NOT EXISTS ship_positions_history (
    id BIGSERIAL PRIMARY KEY,
    ship_id BIGINT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    course_over_ground DOUBLE PRECISION,
    speed_over_ground DOUBLE PRECISION,
    heading INTEGER,
    navigational_status INTEGER,
    rate_of_turn DOUBLE PRECISION,
    ship_type SMALLINT,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Справочник судов для явных связей в ERD
CREATE TABLE IF NOT EXISTS ships (
    ship_id BIGINT PRIMARY KEY,
    first_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_ship_type SMALLINT
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_history_ship_id ON ship_positions_history(ship_id);
CREATE INDEX IF NOT EXISTS idx_history_timestamp ON ship_positions_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_current_updated_at ON ship_positions_current(updated_at);

-- Триггер: автоматически создает/обновляет запись в ships
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

-- Внешние ключи для явных связей в ERD
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_ship_positions_current_ship'
    ) THEN
        ALTER TABLE ship_positions_current
        ADD CONSTRAINT fk_ship_positions_current_ship
        FOREIGN KEY (ship_id) REFERENCES ships(ship_id) ON DELETE CASCADE;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_ship_positions_history_ship'
    ) THEN
        ALTER TABLE ship_positions_history
        ADD CONSTRAINT fk_ship_positions_history_ship
        FOREIGN KEY (ship_id) REFERENCES ships(ship_id) ON DELETE CASCADE;
    END IF;
END $$;

-- Функция для автоматической очистки старых данных (старше 7 дней)
CREATE OR REPLACE FUNCTION cleanup_old_positions()
RETURNS void AS $$
BEGIN
    DELETE FROM ship_positions_history 
    WHERE timestamp < NOW() - INTERVAL '7 days';
END;
$$ LANGUAGE plpgsql;

-- Комментарии к таблицам
COMMENT ON TABLE ship_positions_current IS 'Текущие позиции судов (обновляется через UPSERT)';
COMMENT ON TABLE ship_positions_history IS 'История позиций судов (автоматически очищается через 7 дней)';

-- Базовые станции AIS (сообщение 4), навигационные знаки AtoN (сообщение 21)
CREATE TABLE IF NOT EXISTS ais_stations (
    mmsi BIGINT PRIMARY KEY,
    kind VARCHAR(32) NOT NULL,
    name TEXT,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    type_code SMALLINT,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Справочник типов стационарных объектов AIS для ERD-связи
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
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_ais_stations_kind'
    ) THEN
        ALTER TABLE ais_stations
        ADD CONSTRAINT fk_ais_stations_kind
        FOREIGN KEY (kind) REFERENCES ais_station_kinds(kind);
    END IF;
END $$;

COMMENT ON TABLE ais_stations IS 'Стационарные объекты AIS: базовые станции, AtoN';
COMMENT ON TABLE ships IS 'Справочник судов (родительская сущность для текущих и исторических позиций)';
COMMENT ON TABLE ais_station_kinds IS 'Справочник категорий стационарных AIS-объектов';

