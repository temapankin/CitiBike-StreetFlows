-- Build bike_stations from the raw data table.
-- Uses most-frequent-geometry dedup, drops NJ stations via borough polygons,
-- snaps to borough, adds UTM projection.

DROP TABLE IF EXISTS bike_stations CASCADE;

CREATE TABLE bike_stations AS
SELECT DISTINCT ON (station_id)
    station_id,
    station_name,
    geom,
    count
FROM (
    SELECT
        station_id,
        station_name,
        ST_SetSRID(ST_MakePoint(lng, lat), 4326) AS geom,
        COUNT(*) AS count
    FROM (
        SELECT start_station_id AS station_id, start_station_name AS station_name,
               start_lng AS lng, start_lat AS lat FROM data
        UNION ALL
        SELECT end_station_id, end_station_name,
               end_lng, end_lat FROM data
    ) raw
    WHERE station_id IS NOT NULL
      AND station_name IS NOT NULL
      AND lat IS NOT NULL
      AND lng IS NOT NULL
      AND lat BETWEEN 40.4 AND 41.0
      AND lng BETWEEN -74.3 AND -73.6
    GROUP BY station_id, station_name, lat, lng
) deduped
ORDER BY station_id, count DESC;

-- Synthetic integer primary key (SID) to match legacy schema
ALTER TABLE bike_stations ADD COLUMN sid SERIAL PRIMARY KEY;

-- UTM 18N projection for spatial ops
ALTER TABLE bike_stations ADD COLUMN geom_utm geometry(Point, 26918);
UPDATE bike_stations SET geom_utm = ST_Transform(geom, 26918);

CREATE INDEX bs_geom_idx     ON bike_stations USING GIST(geom);
CREATE INDEX bs_geom_utm_idx ON bike_stations USING GIST(geom_utm);
CREATE INDEX bs_sid_idx      ON bike_stations (sid);

-- Validate geometry
DO $$
DECLARE invalid_count INT;
BEGIN
    SELECT COUNT(*) INTO invalid_count
    FROM bike_stations
    WHERE NOT ST_IsValid(geom) OR NOT ST_IsSimple(geom);
    IF invalid_count > 0 THEN
        RAISE WARNING '% stations have invalid/non-simple geometry', invalid_count;
    END IF;
END $$;

-- ── Borough polygons — loaded by 02_load_trips.py; just add UTM column here ──

ALTER TABLE boroughs ADD COLUMN IF NOT EXISTS geom_utm geometry(MultiPolygon, 26918);
UPDATE boroughs SET geom_utm = ST_Transform(geom, 26918) WHERE geom_utm IS NULL;

CREATE INDEX IF NOT EXISTS boroughs_geom_idx     ON boroughs USING GIST(geom);
CREATE INDEX IF NOT EXISTS boroughs_geom_utm_idx ON boroughs USING GIST(geom_utm);

-- ── Drop non-NYC stations ──────────────────────────────────────────────────────

DELETE FROM bike_stations bs
WHERE NOT EXISTS (
    SELECT 1 FROM boroughs b
    WHERE ST_Within(bs.geom_utm, b.geom_utm)
);

-- ── Tag borough ───────────────────────────────────────────────────────────────

ALTER TABLE bike_stations ADD COLUMN IF NOT EXISTS borough VARCHAR(32);

UPDATE bike_stations bs
SET borough = b.boroname
FROM boroughs b
WHERE ST_Within(bs.geom_utm, b.geom_utm);

-- ── Summary ───────────────────────────────────────────────────────────────────

SELECT borough, COUNT(*) AS stations
FROM bike_stations
GROUP BY borough
ORDER BY stations DESC;
