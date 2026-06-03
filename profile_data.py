"""
§0 – Feasibility probe. Run after `make db` to decide routing parameters.

Prints:
  • total trips
  • distinct OD pairs (directed)
  • trips per day (avg + peak)
  • OD pair distribution (top-N covers what % of trips)
  • recommended N_OD_PAIRS for routing

Sets N_OD_PAIRS in .env (or prints instruction) so 06_route_od.sql uses it.
"""

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import pandas as pd

load_dotenv()

DB_URL = (
    f"postgresql://{os.environ['PGUSER']}:{os.environ['PGPASSWORD']}"
    f"@{os.environ['PGHOST']}:{os.environ['PGPORT']}/{os.environ['PGDATABASE']}"
)
engine = create_engine(DB_URL)

print("=" * 60)
print("CitiBike Summer 2023 – Data Profile")
print("=" * 60)

# ── basic counts ──────────────────────────────────────────────────────────────

row = pd.read_sql(text("""
    SELECT
        COUNT(*)                                           AS total_trips,
        COUNT(DISTINCT (start_station_sid, end_station_sid)) AS distinct_od_pairs,
        COUNT(DISTINCT start_station_sid)                  AS distinct_stations,
        MIN(starting_time)::date                           AS period_start,
        MAX(starting_time)::date                           AS period_end
    FROM trips
"""), engine).iloc[0]

print(f"\nTotal trips:          {row.total_trips:>12,}")
print(f"Distinct OD pairs:    {row.distinct_od_pairs:>12,}")
print(f"Distinct stations:    {row.distinct_stations:>12,}")
print(f"Period:               {row.period_start} → {row.period_end}")

# ── per-day volume ────────────────────────────────────────────────────────────

daily = pd.read_sql(text("""
    SELECT starting_time::date AS day, COUNT(*) AS trips
    FROM trips GROUP BY day ORDER BY day
"""), engine)

print(f"\nAvg trips/day:        {daily.trips.mean():>12,.0f}")
print(f"Peak trips/day:       {daily.trips.max():>12,}  ({daily.loc[daily.trips.idxmax(), 'day']})")
print(f"Min  trips/day:       {daily.trips.min():>12,}  ({daily.loc[daily.trips.idxmin(), 'day']})")

# ── OD pair coverage ──────────────────────────────────────────────────────────

od = pd.read_sql(text("""
    SELECT trip_count FROM od_pairs ORDER BY trip_count DESC
"""), engine)

total = od["trip_count"].sum()
cumulative = od["trip_count"].cumsum() / total

thresholds = [0.50, 0.80, 0.90, 0.95]
print("\nOD pair coverage:")
for t in thresholds:
    n = int((cumulative <= t).sum()) + 1
    print(f"  top {n:>6,} pairs cover {t*100:.0f}% of all trips")

n_all = len(od)
print(f"  all {n_all:>6,} pairs cover 100%")

# ── routing recommendation ────────────────────────────────────────────────────

# Route pairs that together cover ≥80% of trips.
n_80 = int((cumulative <= 0.80).sum()) + 1
n_90 = int((cumulative <= 0.90).sum()) + 1

print(f"\nRecommendation:")
print(f"  Route top {n_90:,} OD pairs (90% coverage).")
print(f"  For a lighter run, use top {n_80:,} (80% coverage).")
print(f"\nSet N_OD_PAIRS in .env or override on the psql command line:")
print(f"  psql -v N_OD_PAIRS={n_90} -f pipeline/06_route_od.sql")
print(f"\n  (N_OD_PAIRS=0 routes ALL {n_all:,} pairs — may take hours.)")

# ── timelapse sample guidance ─────────────────────────────────────────────────

print(f"\nTimelapse sample (N_SAMPLE):")
n_sample = min(100_000, int(row.total_trips * 0.05))
print(f"  ~5% of trips = {n_sample:,}  (default N_SAMPLE=100000)")
print(f"  Set N_SAMPLE env var in .env before running 07_export.py.")
print("=" * 60)
