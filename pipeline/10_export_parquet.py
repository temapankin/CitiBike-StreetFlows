"""
Export timelapse Parquet files — denormalized, range-request-friendly layout.

Each trip row carries its route geometry inline (polyline6-encoded), so the web
client never downloads a separate routes file and never runs a client-side JOIN.
Files are ordered by t0 with small row groups, so DuckDB-WASM can satisfy a time
window with a handful of HTTP range reads instead of a full-file download.

Outputs (under web/data/):
  trips/day=YYYY-MM-DD/part-0.parquet
        columns: t0 REAL, dur REAL, geom VARCHAR (polyline6), samp UTINYINT
        ORDER BY t0, ROW_GROUP_SIZE 2048, COMPRESSION ZSTD
  timelapse_meta.json  – period, anim_duration_s, n_routes, n_trips, samp_buckets

  (routes.parquet is no longer produced — geometry is embedded per trip.)

`samp` is a stable 0..255 bucket; the client filters `samp < k` to cap the number
of concurrent trips per window without flicker between frames.

Run from repo root inside the citibike conda env:
  python pipeline/10_export_parquet.py

Requires duckdb, polyline, psycopg2/sqlalchemy in the env.
"""

import json
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import polyline
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

PGHOST     = os.environ["PGHOST"]
PGPORT     = os.environ["PGPORT"]
PGDATABASE = os.environ["PGDATABASE"]
PGUSER     = os.environ["PGUSER"]
PGPASSWORD = os.environ.get("PGPASSWORD", "")

DB_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
engine = create_engine(DB_URL)

OUT = Path("web/data")
OUT.mkdir(parents=True, exist_ok=True)
TRIPS_DIR = OUT / "trips"
TRIPS_DIR.mkdir(parents=True, exist_ok=True)

SAMP_BUCKETS = 256  # client filters `samp < k` to thin dense windows

# ── route_id → polyline6 ────────────────────────────────────────────────────────
# Read every vertex once, group by route, encode as polyline6 (precision 6).
# polyline.encode expects (lat, lon) pairs.

print("Building polyline6 geometry per route …")

with engine.connect() as conn:
    df_pts = pd.read_sql(
        text("SELECT route_id, seq, lon, lat FROM tl_route_points ORDER BY route_id, seq"),
        conn,
    )

print(f"  {len(df_pts):,} total vertices across all routes")

# Rows are already ordered by (route_id, seq). Split into per-route slices on the
# route_id change boundaries (far faster than pandas groupby over 48M rows), then
# polyline6-encode each route's (lat, lon) vertices.
route_ids = df_pts["route_id"].to_numpy()
lats = df_pts["lat"].to_numpy()
lons = df_pts["lon"].to_numpy()
bounds = np.flatnonzero(np.diff(route_ids)) + 1
starts = np.concatenate(([0], bounds))
ends   = np.concatenate((bounds, [len(route_ids)]))

geom_by_route = {}
for s, e in zip(starts, ends):
    coords = np.column_stack((lats[s:e], lons[s:e]))  # (lat, lon) pairs
    geom_by_route[int(route_ids[s])] = polyline.encode(coords.tolist(), 6)

n_routes = len(geom_by_route)
print(f"  {n_routes:,} routes encoded")
del df_pts, route_ids, lats, lons

# ── trips/ (day-partitioned, geometry embedded) ─────────────────────────────────

print("Exporting trips/ day-partitioned Parquet (geometry inline) …")

with engine.connect() as conn:
    days = pd.read_sql(text("SELECT DISTINCT day FROM tl_trips ORDER BY day"), conn)["day"].tolist()

con = duckdb.connect()
total_trips = 0
for day in days:
    day_str = str(day)
    with engine.connect() as conn:
        df_day = pd.read_sql(
            text("SELECT route_id::int AS route_id, t0::real AS t0, dur::real AS dur "
                 "FROM tl_trips WHERE day = :d ORDER BY t0"),
            conn,
            params={"d": day_str},
        )
    if df_day.empty:
        continue

    df_day["geom"] = df_day["route_id"].map(geom_by_route)
    # Drop any trip whose route produced no geometry (should be none).
    df_day = df_day[df_day["geom"].notna()]
    # Stable sample bucket, evenly distributed along the (t0-ordered) timeline.
    df_day["samp"] = (df_day.index.to_numpy() % SAMP_BUCKETS).astype("int32")

    dest = TRIPS_DIR / f"day={day_str}"
    dest.mkdir(parents=True, exist_ok=True)
    parquet_path = dest / "part-0.parquet"

    con.register("df_day", df_day[["t0", "dur", "geom", "samp"]])
    con.execute(
        f"COPY (SELECT t0, dur, geom, samp::UTINYINT AS samp FROM df_day ORDER BY t0) "
        f"TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 2048)"
    )
    con.unregister("df_day")
    total_trips += len(df_day)

print(f"  {total_trips:,} trips across {len(days)} days → trips/")

# Remove the now-obsolete normalized routes file if present.
old_routes = OUT / "routes.parquet"
if old_routes.exists():
    old_routes.unlink()
    print("  removed obsolete routes.parquet")

# ── timelapse_meta.json ──────────────────────────────────────────────────────────

print("Writing timelapse_meta.json …")
with engine.connect() as conn:
    row = conn.execute(text("SELECT * FROM tl_meta")).mappings().fetchone()

meta = {
    "period_start":    row["period_start"].isoformat() if hasattr(row["period_start"], "isoformat") else str(row["period_start"]),
    "period_end":      row["period_end"].isoformat() if hasattr(row["period_end"], "isoformat") else str(row["period_end"]),
    "anim_duration_s": int(row["anim_duration_s"]),
    "n_routes":        int(row["n_routes"]),
    "n_trips":         int(row["n_trips"]),
    "samp_buckets":    SAMP_BUCKETS,
}

with open(OUT / "timelapse_meta.json", "w") as f:
    json.dump(meta, f, indent=2)

print(f"  period: {meta['period_start']} → {meta['period_end']}")
print(f"  {meta['n_routes']:,} routes, {meta['n_trips']:,} trips")

# ── Size report ───────────────────────────────────────────────────────────────

import subprocess

def dir_size(p: Path) -> str:
    r = subprocess.run(["du", "-sh", str(p)], capture_output=True, text=True)
    return r.stdout.split()[0] if r.returncode == 0 else "?"

print("\nSize report:")
print(f"  trips/ : {dir_size(TRIPS_DIR)}")
print("Export complete.")
