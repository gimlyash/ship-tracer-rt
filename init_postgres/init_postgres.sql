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
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_history_ship_id ON ship_positions_history(ship_id);
CREATE INDEX IF NOT EXISTS idx_history_timestamp ON ship_positions_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_current_updated_at ON ship_positions_current(updated_at);

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

