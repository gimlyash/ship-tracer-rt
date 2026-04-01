-- Run once on existing databases (PostgreSQL 9.1+):
ALTER TABLE ship_positions_current ADD COLUMN IF NOT EXISTS ship_type SMALLINT;
ALTER TABLE ship_positions_history ADD COLUMN IF NOT EXISTS ship_type SMALLINT;
