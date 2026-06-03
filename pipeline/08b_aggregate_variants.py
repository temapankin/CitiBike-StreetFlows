"""
Produce two count-corrected centerline outputs from the existing OSM-routed edge_flows.
Replaces the naive SUM(total_trips) in 08_clean_network.py with two alternatives:

  lwavg  – length-weighted average: SUM(trips_i * edge_len_i) / centerline_len
            Sequential edges tile the length → no longitudinal double-count.
            Parallel carriageways overlap correctly (both count toward direction volume).

  max    – MAX(total_trips) per centerline segment.
            Dead simple; understates bidirectional volume on split carriageways.

Both files are written to web/data/ for side-by-side comparison on the map.
"""

import os
from pathlib import Path

import geopandas as gpd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DB_URL = (
    f"postgresql://{os.environ['PGUSER']}:{os.environ.get('PGPASSWORD', '')}"
    f"@{os.environ['PGHOST']}:{os.environ['PGPORT']}/{os.environ['PGDATABASE']}"
)
engine = create_engine(DB_URL)
OUT = Path("web/data")

MAX_SNAP_M = 30


_SQL_LWAVG = """
CREATE TABLE IF NOT EXISTS clean_flows_lwavg AS
WITH assign AS (
  SELECT ef.total_trips,
         ST_Length(ST_Transform(ef.geom, 32618)) AS edge_len_m,
         nn.cl_id
  FROM edge_flows ef
  CROSS JOIN LATERAL (
    SELECT c.cl_id,
           c.geom_m <-> ST_Transform(ST_LineInterpolatePoint(ef.geom, 0.5), 32618) AS d
    FROM nyc_centerline c
    ORDER BY c.geom_m <-> ST_Transform(ST_LineInterpolatePoint(ef.geom, 0.5), 32618)
    LIMIT 1
  ) nn
  WHERE nn.d < {snap_m}
)
SELECT c.cl_id AS id, c.geom, c.street_name,
       CASE
         WHEN ST_Length(c.geom_m) > 0
         THEN (SUM(a.total_trips * a.edge_len_m) / ST_Length(c.geom_m))::bigint
         ELSE SUM(a.total_trips)::bigint
       END AS total_trips
FROM assign a
JOIN nyc_centerline c ON c.cl_id = a.cl_id
GROUP BY c.cl_id, c.geom, c.street_name, c.geom_m
""".format(snap_m=MAX_SNAP_M)

_SQL_MAX = """
CREATE TABLE IF NOT EXISTS clean_flows_max AS
WITH assign AS (
  SELECT ef.total_trips, nn.cl_id
  FROM edge_flows ef
  CROSS JOIN LATERAL (
    SELECT c.cl_id,
           c.geom_m <-> ST_Transform(ST_LineInterpolatePoint(ef.geom, 0.5), 32618) AS d
    FROM nyc_centerline c
    ORDER BY c.geom_m <-> ST_Transform(ST_LineInterpolatePoint(ef.geom, 0.5), 32618)
    LIMIT 1
  ) nn
  WHERE nn.d < {snap_m}
)
SELECT c.cl_id AS id, c.geom, c.street_name,
       MAX(a.total_trips)::bigint AS total_trips
FROM assign a
JOIN nyc_centerline c ON c.cl_id = a.cl_id
GROUP BY c.cl_id, c.geom, c.street_name
""".format(snap_m=MAX_SNAP_M)


def export_table(table: str, path: Path) -> dict:
    gdf = gpd.read_postgis(
        f"SELECT id, total_trips, geom FROM {table} WHERE total_trips > 0 ORDER BY total_trips DESC",
        engine, geom_col="geom", crs=4326,
    )
    gdf.to_file(path, driver="GeoJSON")
    trips = list(gdf["total_trips"])
    return {
        "segments": len(trips),
        "max": max(trips),
        "sum": sum(trips),
        "top5": [f"{t:,}" for t in sorted(trips, reverse=True)[:5]],
    }


def main():
    print("Building length-weighted average variant (clean_flows_lwavg)…")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS clean_flows_lwavg"))
        conn.execute(text(_SQL_LWAVG))
        conn.execute(text("ALTER TABLE clean_flows_lwavg ADD PRIMARY KEY (id)"))
        conn.execute(text("CREATE INDEX clean_flows_lwavg_geom_idx ON clean_flows_lwavg USING GIST(geom)"))

    print("Building max variant (clean_flows_max)…")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS clean_flows_max"))
        conn.execute(text(_SQL_MAX))
        conn.execute(text("ALTER TABLE clean_flows_max ADD PRIMARY KEY (id)"))
        conn.execute(text("CREATE INDEX clean_flows_max_geom_idx ON clean_flows_max USING GIST(geom)"))

    print("Exporting GeoJSON files…")
    lwavg = export_table("clean_flows_lwavg", OUT / "edge_flows_lwavg.geojson")
    maxv  = export_table("clean_flows_max",   OUT / "edge_flows_max.geojson")

    print()
    print("=== Aggregate-variant results ===")
    print(f"  Length-weighted avg  →  edge_flows_lwavg.geojson")
    print(f"    segments: {lwavg['segments']:,}  max: {lwavg['max']:,}  sum: {lwavg['sum']:,}")
    print(f"    top 5: {lwavg['top5']}")
    print(f"  Max per centerline   →  edge_flows_max.geojson")
    print(f"    segments: {maxv['segments']:,}  max: {maxv['max']:,}  sum: {maxv['sum']:,}")
    print(f"    top 5: {maxv['top5']}")
    print()

    # Gate: both maxes must be well below the 7.5M artifact
    baseline_max = 7_542_156
    for name, info in [("lwavg", lwavg), ("max", maxv)]:
        if info["max"] >= baseline_max:
            print(f"  WARN: {name} max {info['max']:,} is NOT below baseline {baseline_max:,} — "
                  "count inflation may persist")
        else:
            print(f"  PASS: {name} max {info['max']:,} < baseline {baseline_max:,}")

    return lwavg, maxv


if __name__ == "__main__":
    main()
