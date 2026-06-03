"""
Export data for the web map:
  docs/data/edge_flows.geojson   – street-level bike flow (aggregate, parallel edges merged)
  docs/data/stations.geojson     – stations sized by trip count
  docs/data/trips_timelapse.json – all summer trips; route geometries deduplicated

Environment variables:
  FLOW_TABLE    DB table to export as edge_flows.geojson (default: clean_flows).
                Set to 'edge_flows_cl' to use the centerline-routed result.
  SKIP_TIMELAPSE  Set to '1' to skip timelapse export (trips_timelapse.json untouched).
  MAX_TRIPS     Cap on timelapse trip count (0/unset = unlimited).

Idempotent: re-running overwrites the output files.
"""

import json
import math
import os
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from shapely import wkb as shapely_wkb
from shapely.geometry import LineString
from sqlalchemy import create_engine, text

load_dotenv()

DB_URL = (
    f"postgresql://{os.environ['PGUSER']}:{os.environ.get('PGPASSWORD', '')}"
    f"@{os.environ['PGHOST']}:{os.environ['PGPORT']}/{os.environ['PGDATABASE']}"
)
engine = create_engine(DB_URL)

OUT = Path("docs/data")
OUT.mkdir(parents=True, exist_ok=True)

MAX_TRIPS = int(os.environ.get("MAX_TRIPS", "0")) or None  # 0/unset = unlimited
ANIM_DURATION_S = 180  # animation clock length in seconds
FLOW_TABLE = os.environ.get("FLOW_TABLE", "clean_flows")
SKIP_TIMELAPSE = os.environ.get("SKIP_TIMELAPSE", "0") == "1"

# ── edge_flows.geojson ────────────────────────────────────────────────────────

print(f"Exporting edge_flows from {FLOW_TABLE!r}…")
gdf_flows = gpd.read_postgis(
    f"SELECT id, total_trips, geom FROM {FLOW_TABLE} WHERE total_trips > 0 ORDER BY total_trips DESC",
    engine,
    geom_col="geom",
    crs=4326,
)
print(f"  {len(gdf_flows):,} edges loaded from {FLOW_TABLE}")
gdf_flows.to_file(OUT / "edge_flows.geojson", driver="GeoJSON")
print(f"  {len(gdf_flows):,} edges  →  edge_flows.geojson")

# ── stations.geojson ─────────────────────────────────────────────────────────

print("Exporting stations…")
gdf_stations = gpd.read_postgis(
    """
    SELECT bs.sid, bs.station_name, bs.borough,
           COALESCE(tsc.trips_per_station, 0) AS trips_per_station,
           bs.geom
    FROM bike_stations bs
    LEFT JOIN trips_station_counts tsc ON bs.sid = tsc.start_station_sid
    ORDER BY trips_per_station DESC
    """,
    engine,
    geom_col="geom",
    crs=4326,
)
gdf_stations.to_file(OUT / "stations.geojson", driver="GeoJSON")
print(f"  {len(gdf_stations):,} stations  →  stations.geojson")

# ── trips_timelapse.json ──────────────────────────────────────────────────────

if SKIP_TIMELAPSE:
    print("SKIP_TIMELAPSE=1 — skipping timelapse export, trips_timelapse.json unchanged.")
    print("Export complete.")
    raise SystemExit(0)

cap_msg = f"cap {MAX_TRIPS:,}" if MAX_TRIPS else "no cap"
print(f"Exporting timelapse (full summer, dedup routes, {cap_msg})…")

# 1. Load all routed OD geometries → routes[] list, od_id → index map.
with engine.connect() as conn:
    od_rows = conn.execute(
        text("SELECT od_id, route_geom FROM od_routes ORDER BY od_id")
    ).fetchall()

routes = []
od_to_idx = {}
for od_id, wkb_hex in od_rows:
    try:
        geom = shapely_wkb.loads(wkb_hex, hex=True)
    except Exception:
        continue
    if geom.geom_type != "LineString" or len(geom.coords) < 2:
        continue
    od_to_idx[od_id] = len(routes)
    routes.append([[round(x, 5), round(y, 5)] for x, y in geom.coords])

print(f"  {len(routes):,} unique routes loaded")

# 2. Load all trips matched to routed OD pairs (ordered by start time).
with engine.connect() as conn:
    trip_rows = conn.execute(text("""
        SELECT t.starting_time, t.ending_time, op.od_id
        FROM trips t
        JOIN od_pairs op
            ON t.start_station_sid = op.start_station_sid
           AND t.end_station_sid   = op.end_station_sid
        WHERE op.od_id IN (SELECT od_id FROM od_routes)
        ORDER BY t.starting_time
    """)).fetchall()

df = pd.DataFrame(trip_rows, columns=["starting_time", "ending_time", "od_id"])
df["starting_time"] = pd.to_datetime(df["starting_time"], utc=True)
df["ending_time"]   = pd.to_datetime(df["ending_time"],   utc=True)
df = df.dropna()

if MAX_TRIPS:
    df = df.head(MAX_TRIPS)

if df.empty:
    print("  WARNING: no trips matched routed OD pairs — timelapse will be empty.")
    timelapse = {
        "period_start": "", "period_end": "", "anim_duration_s": ANIM_DURATION_S,
        "n_routes": 0, "n_trips": 0, "routes": [], "trips": [],
    }
else:
    t_min = df["starting_time"].min()
    t_max = df["ending_time"].max()
    summer_span = (t_max - t_min).total_seconds()

    def to_anim(ts: pd.Timestamp) -> float:
        return round(((ts - t_min).total_seconds() / summer_span) * ANIM_DURATION_S, 2)

    trip_records = []
    for row in df.itertuples(index=False):
        idx = od_to_idx.get(row.od_id)
        if idx is None:
            continue
        start_sec = to_anim(row.starting_time)
        dur_sec   = round(max(0.1, to_anim(row.ending_time) - start_sec), 2)
        trip_records.append([idx, start_sec, dur_sec])

    # Must be sorted by startSec for the sliding-window GPU renderer.
    trip_records.sort(key=lambda x: x[1])

    timelapse = {
        "period_start":    t_min.isoformat(),
        "period_end":      t_max.isoformat(),
        "anim_duration_s": ANIM_DURATION_S,
        "n_routes":        len(routes),
        "n_trips":         len(trip_records),
        "routes":          routes,
        "trips":           trip_records,
    }

with open(OUT / "trips_timelapse.json", "w") as f:
    json.dump(timelapse, f, separators=(",", ":"))

print(
    f"  {timelapse['n_trips']:,} trips / {timelapse['n_routes']:,} routes"
    f"  →  trips_timelapse.json"
)
print("Export complete.")
