CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- Raw trips from CSV/Parquet import
CREATE TABLE IF NOT EXISTS data (
    ride_id          VARCHAR(32),
    rideable_type    VARCHAR(20),
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,
    start_station_name TEXT,
    start_station_id TEXT,
    end_station_name TEXT,
    end_station_id   TEXT,
    start_lat        DOUBLE PRECISION,
    start_lng        DOUBLE PRECISION,
    end_lat          DOUBLE PRECISION,
    end_lng          DOUBLE PRECISION,
    member_casual    VARCHAR(10)
);
