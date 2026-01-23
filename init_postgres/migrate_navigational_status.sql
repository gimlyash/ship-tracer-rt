-- Для таблицы ship_positions_current
ALTER TABLE ship_positions_current 
ALTER COLUMN navigational_status TYPE INTEGER 
USING CASE 
    WHEN navigational_status ~ '^[0-9]+$' THEN navigational_status::INTEGER 
    ELSE NULL 
END;

-- Для таблицы ship_positions_history
ALTER TABLE ship_positions_history 
ALTER COLUMN navigational_status TYPE INTEGER 
USING CASE 
    WHEN navigational_status ~ '^[0-9]+$' THEN navigational_status::INTEGER 
    ELSE NULL 
END;
