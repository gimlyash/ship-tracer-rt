-- 1.1. Получить все текущие позиции кораблей
SELECT * FROM ship_positions_current;

-- 1.2. Получить только ID кораблей и их координаты
SELECT ship_id, latitude, longitude 
FROM ship_positions_current;

-- 1.3. Получить корабли, обновленные за последние 30 минут
SELECT ship_id, latitude, longitude, updated_at
FROM ship_positions_current
WHERE updated_at > NOW() - INTERVAL '30 minutes'
ORDER BY updated_at DESC;

-- 1.4. Подсчитать общее количество активных кораблей
SELECT COUNT(*) as total_ships
FROM ship_positions_current;

-- 1.5. Получить корабли с известной скоростью (не NULL)
SELECT ship_id, speed_over_ground, course_over_ground
FROM ship_positions_current
WHERE speed_over_ground IS NOT NULL
ORDER BY speed_over_ground DESC;

-- 2.1. Найти корабли, движущиеся быстрее 10 узлов
SELECT ship_id, speed_over_ground, course_over_ground
FROM ship_positions_current
WHERE speed_over_ground > 10
ORDER BY speed_over_ground DESC;

-- 2.2. Найти корабли в определенном регионе (пример: Сингапур)
-- Широта: 1.0 - 1.5, Долгота: 103.5 - 104.0
SELECT ship_id, latitude, longitude
FROM ship_positions_current
WHERE latitude BETWEEN 1.0 AND 1.5
  AND longitude BETWEEN 103.5 AND 104.0;

-- 2.3. Найти корабли, которые не обновлялись более 1 часа
SELECT ship_id, updated_at, 
       EXTRACT(EPOCH FROM (NOW() - updated_at))/60 as minutes_ago
FROM ship_positions_current
WHERE updated_at < NOW() - INTERVAL '1 hour'
ORDER BY updated_at ASC;

-- 2.4. Найти корабли с определенным навигационным статусом
-- (0 = Under way using engine, 1 = At anchor, и т.д.)
SELECT ship_id, navigational_status, speed_over_ground
FROM ship_positions_current
WHERE navigational_status = 0  -- В движении
  AND speed_over_ground > 0;

-- 3.1. Средняя, минимальная и максимальная скорость всех кораблей
SELECT 
    COUNT(*) as ships_with_speed,
    AVG(speed_over_ground) as avg_speed,
    MIN(speed_over_ground) as min_speed,
    MAX(speed_over_ground) as max_speed
FROM ship_positions_current
WHERE speed_over_ground IS NOT NULL;

-- 3.2. Количество кораблей по навигационному статусу
SELECT 
    navigational_status,
    COUNT(*) as ship_count
FROM ship_positions_current
WHERE navigational_status IS NOT NULL
GROUP BY navigational_status
ORDER BY ship_count DESC;

-- 3.3. Корабли, сгруппированные по диапазонам скорости
SELECT 
    CASE 
        WHEN speed_over_ground = 0 THEN 'Стоит (0 узлов)'
        WHEN speed_over_ground < 5 THEN 'Медленно (1-5 узлов)'
        WHEN speed_over_ground < 15 THEN 'Средне (5-15 узлов)'
        WHEN speed_over_ground < 25 THEN 'Быстро (15-25 узлов)'
        ELSE 'Очень быстро (>25 узлов)'
    END as speed_category,
    COUNT(*) as ship_count
FROM ship_positions_current
WHERE speed_over_ground IS NOT NULL
GROUP BY speed_category
ORDER BY ship_count DESC;

-- 3.4. Статистика по обновлениям: сколько кораблей обновлялось в каждый час
SELECT 
    DATE_TRUNC('hour', updated_at) as hour,
    COUNT(*) as ships_updated
FROM ship_positions_current
WHERE updated_at > NOW() - INTERVAL '24 hours'
GROUP BY hour
ORDER BY hour DESC;

-- 4.1. Получить историю позиций конкретного корабля
SELECT ship_id, latitude, longitude, timestamp, speed_over_ground
FROM ship_positions_history
WHERE ship_id = 312927000  -- Замените на реальный ship_id
ORDER BY timestamp DESC
LIMIT 50;

-- 4.2. Найти корабли, которые переместились на максимальное расстояние
-- (сравниваем первую и последнюю запись в истории)
WITH first_positions AS (
    SELECT DISTINCT ON (ship_id)
        ship_id, latitude, longitude, timestamp
    FROM ship_positions_history
    WHERE timestamp > NOW() - INTERVAL '1 hour'
    ORDER BY ship_id, timestamp ASC
),
last_positions AS (
    SELECT DISTINCT ON (ship_id)
        ship_id, latitude, longitude, timestamp
    FROM ship_positions_history
    WHERE timestamp > NOW() - INTERVAL '1 hour'
    ORDER BY ship_id, timestamp DESC
)
SELECT 
    f.ship_id,
    f.latitude as start_lat,
    f.longitude as start_lon,
    l.latitude as end_lat,
    l.longitude as end_lon,
    -- Примерное расстояние в км (формула гаверсинуса)
    6371 * acos(
        cos(radians(f.latitude)) * 
        cos(radians(l.latitude)) * 
        cos(radians(l.longitude) - radians(f.longitude)) + 
        sin(radians(f.latitude)) * 
        sin(radians(l.latitude))
    ) as distance_km
FROM first_positions f
JOIN last_positions l ON f.ship_id = l.ship_id
WHERE f.ship_id != l.ship_id
ORDER BY distance_km DESC
LIMIT 10;

-- 4.3. Количество записей в истории по каждому кораблю
SELECT 
    ship_id,
    COUNT(*) as history_records,
    MIN(timestamp) as first_seen,
    MAX(timestamp) as last_seen
FROM ship_positions_history
WHERE timestamp > NOW() - INTERVAL '24 hours'
GROUP BY ship_id
ORDER BY history_records DESC;

-- 5.1. Сравнить текущую позицию с последней позицией в истории
SELECT 
    c.ship_id,
    c.latitude as current_lat,
    c.longitude as current_lon,
    h.latitude as history_lat,
    h.longitude as history_lon,
    c.updated_at as current_time,
    h.timestamp as history_time
FROM ship_positions_current c
LEFT JOIN LATERAL (
    SELECT latitude, longitude, timestamp
    FROM ship_positions_history
    WHERE ship_id = c.ship_id
    ORDER BY timestamp DESC
    LIMIT 1
) h ON true
WHERE c.updated_at > NOW() - INTERVAL '1 hour';

-- 5.2. Найти корабли, которые есть в текущих позициях, но нет в истории
SELECT c.ship_id, c.latitude, c.longitude
FROM ship_positions_current c
WHERE NOT EXISTS (
    SELECT 1 
    FROM ship_positions_history h 
    WHERE h.ship_id = c.ship_id
);

-- 5.3. Корабли с максимальной скоростью в текущий момент и их средняя скорость в истории
SELECT 
    c.ship_id,
    c.speed_over_ground as current_speed,
    COALESCE(AVG(h.speed_over_ground), 0) as avg_historical_speed
FROM ship_positions_current c
LEFT JOIN ship_positions_history h ON c.ship_id = h.ship_id
    AND h.timestamp > NOW() - INTERVAL '1 hour'
WHERE c.speed_over_ground IS NOT NULL
GROUP BY c.ship_id, c.speed_over_ground
ORDER BY c.speed_over_ground DESC
LIMIT 10;

-- 6.1. Найти корабли, которые изменили курс более чем на 45 градусов
WITH course_changes AS (
    SELECT 
        ship_id,
        course_over_ground,
        LAG(course_over_ground) OVER (PARTITION BY ship_id ORDER BY timestamp) as prev_course,
        timestamp
    FROM ship_positions_history
    WHERE course_over_ground IS NOT NULL
      AND timestamp > NOW() - INTERVAL '1 hour'
)
SELECT 
    ship_id,
    prev_course,
    course_over_ground,
    ABS(course_over_ground - prev_course) as course_change,
    timestamp
FROM course_changes
WHERE prev_course IS NOT NULL
  AND ABS(course_over_ground - prev_course) > 45
ORDER BY course_change DESC;

-- 6.2. Топ-10 самых активных кораблей (по количеству обновлений)
SELECT 
    ship_id,
    COUNT(*) as update_count,
    MIN(timestamp) as first_seen,
    MAX(timestamp) as last_seen,
    MAX(timestamp) - MIN(timestamp) as active_duration
FROM ship_positions_history
WHERE timestamp > NOW() - INTERVAL '1 hour'
GROUP BY ship_id
ORDER BY update_count DESC
LIMIT 10;

-- 6.3. Корабли, которые ускорились или замедлились
WITH speed_changes AS (
    SELECT 
        ship_id,
        speed_over_ground,
        LAG(speed_over_ground) OVER (PARTITION BY ship_id ORDER BY timestamp) as prev_speed,
        timestamp
    FROM ship_positions_history
    WHERE speed_over_ground IS NOT NULL
      AND timestamp > NOW() - INTERVAL '1 hour'
)
SELECT 
    ship_id,
    prev_speed,
    speed_over_ground,
    speed_over_ground - prev_speed as speed_change,
    CASE 
        WHEN speed_over_ground > prev_speed THEN 'Ускорился'
        WHEN speed_over_ground < prev_speed THEN 'Замедлился'
        ELSE 'Без изменений'
    END as status,
    timestamp
FROM speed_changes
WHERE prev_speed IS NOT NULL
  AND ABS(speed_over_ground - prev_speed) > 2  -- Изменение более 2 узлов
ORDER BY ABS(speed_over_ground - prev_speed) DESC
LIMIT 20;

-- 7.1. Проверить использование индексов (EXPLAIN ANALYZE)
EXPLAIN ANALYZE
SELECT ship_id, latitude, longitude
FROM ship_positions_current
WHERE updated_at > NOW() - INTERVAL '30 minutes';

-- 7.2. Найти медленные запросы (если включен pg_stat_statements)
-- SELECT query, calls, total_time, mean_time
-- FROM pg_stat_statements
-- ORDER BY mean_time DESC
-- LIMIT 10;

-- 8.1. Создать представление (VIEW) для быстрого доступа к активным кораблям
CREATE OR REPLACE VIEW active_ships AS
SELECT 
    ship_id,
    latitude,
    longitude,
    speed_over_ground,
    course_over_ground,
    heading,
    updated_at,
    EXTRACT(EPOCH FROM (NOW() - updated_at))/60 as minutes_since_update
FROM ship_positions_current
WHERE updated_at > NOW() - INTERVAL '1 hour';

-- Использование представления:
-- SELECT * FROM active_ships ORDER BY minutes_since_update;

-- 8.2. Найти "призрачные" корабли (обновлялись, но сейчас неактивны)
SELECT 
    ship_id,
    MAX(timestamp) as last_seen,
    NOW() - MAX(timestamp) as time_since_last_seen
FROM ship_positions_history
WHERE timestamp > NOW() - INTERVAL '24 hours'
GROUP BY ship_id
HAVING MAX(timestamp) < NOW() - INTERVAL '2 hours'
   AND ship_id NOT IN (SELECT ship_id FROM ship_positions_current)
ORDER BY last_seen DESC;

-- 8.3. Статистика по времени суток (когда больше всего активности)
SELECT 
    EXTRACT(HOUR FROM timestamp) as hour_of_day,
    COUNT(*) as updates_count,
    COUNT(DISTINCT ship_id) as unique_ships
FROM ship_positions_history
WHERE timestamp > NOW() - INTERVAL '7 days'
GROUP BY hour_of_day
ORDER BY hour_of_day;
