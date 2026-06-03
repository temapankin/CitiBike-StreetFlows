"""
Convert CitiBike CSVs → Parquet, stream Parquet → Postgres `data` table.
Also loads NYC borough GeoJSON into the `boroughs` table.

Idempotent: skips Parquet files that already exist; skips DB load if `data`
already has rows; re-loads boroughs only if table is empty.

Station-ID normalisation uses a stable explicit dict (not a re-fitted
LabelEncoder) so codes are reproducible across runs.
"""

import os
from io import StringIO
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from tqdm import tqdm

load_dotenv()

DB_URL = (
    f"postgresql://{os.environ['PGUSER']}:{os.environ.get('PGPASSWORD', '')}"
    f"@{os.environ['PGHOST']}:{os.environ['PGPORT']}/{os.environ['PGDATABASE']}"
)

RAW = Path("data/raw")
PARQUET_DIR = Path("data/parquet")
PARQUET_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_ROWS = 200_000

# Stable station-ID normalisation: maps non-numeric prefixes to fixed codes.
STATION_ID_MAP: dict[str, str] = {
    r"^SYS": "9000",
    r"^JC":  "8000",
    r"^HB":  "7000",
    r"Lab - NYC": "6000",
}


def normalise_station_id(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    for pattern, prefix in STATION_ID_MAP.items():
        mask = s.str.match(pattern)
        if mask.any():
            s = s.where(
                ~mask,
                prefix + s[mask].str.extract(r"(\d+)$", expand=False).fillna(""),
            )
    return s


SCHEMA = pa.schema([
    ("ride_id",            pa.string()),
    ("rideable_type",      pa.string()),
    ("started_at",         pa.string()),
    ("ended_at",           pa.string()),
    ("start_station_name", pa.string()),
    ("start_station_id",   pa.string()),
    ("end_station_name",   pa.string()),
    ("end_station_id",     pa.string()),
    ("start_lat",          pa.float64()),
    ("start_lng",          pa.float64()),
    ("end_lat",            pa.float64()),
    ("end_lng",            pa.float64()),
    ("member_casual",      pa.string()),
])

# ── CSV → Parquet ─────────────────────────────────────────────────────────────

CSV_FILES = sorted(
    p for p in RAW.glob("*-citibike-tripdata*.csv")
    if not p.name.startswith("._")
)
if not CSV_FILES:
    raise FileNotFoundError(f"No CSVs found in {RAW}. Run 01_download_data.py first.")

print("=== CSV → Parquet ===")
parquet_files: list[Path] = []
for csv_path in CSV_FILES:
    pq_path = PARQUET_DIR / (csv_path.stem + ".parquet")
    parquet_files.append(pq_path)
    if pq_path.exists():
        print(f"  skip (exists): {pq_path.name}")
        continue
    print(f"  converting: {csv_path.name}")
    chunks = []
    for chunk in pd.read_csv(csv_path, chunksize=CHUNK_ROWS, low_memory=False, dtype=str):
        chunk.columns = [c.strip().lower() for c in chunk.columns]
        chunk["start_station_id"] = normalise_station_id(chunk["start_station_id"])
        chunk["end_station_id"]   = normalise_station_id(chunk["end_station_id"])
        for col in ("start_lat", "start_lng", "end_lat", "end_lng"):
            chunk[col] = pd.to_numeric(chunk[col], errors="coerce")
        chunks.append(chunk)
    df = pd.concat(chunks, ignore_index=True).drop_duplicates(subset=["ride_id"])
    pq.write_table(
        pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False), pq_path
    )
    print(f"  wrote {len(df):,} rows → {pq_path.name}")

# ── Parquet → Postgres ────────────────────────────────────────────────────────

def psql_insert_copy(table, conn, keys, data_iter):
    """Fast COPY-based insert (from original UploadingDataIntoDB.ipynb)."""
    dbapi_conn = conn.connection
    with dbapi_conn.cursor() as cur:
        buf = StringIO()
        pd.DataFrame(data_iter, columns=keys).to_csv(buf, index=False, header=False)
        buf.seek(0)
        columns = ", ".join(f'"{k}"' for k in keys)
        cur.copy_expert(
            f'COPY {table.schema}.{table.name} ({columns}) FROM STDIN CSV', buf
        )


engine = create_engine(DB_URL)

# Skip loading if data table already has rows
with engine.connect() as conn:
    existing = conn.execute(text("SELECT COUNT(*) FROM data")).scalar()

if existing > 0:
    print(f"\n=== Parquet → Postgres: already {existing:,} rows in data — skipping ===")
else:
    print("\n=== Parquet → Postgres (data table) ===")
    for pq_path in tqdm(parquet_files, desc="files"):
        pf = pq.ParquetFile(pq_path)
        for batch in tqdm(pf.iter_batches(batch_size=CHUNK_ROWS), leave=False, desc=pq_path.stem):
            df = batch.to_pandas()
            df = df.dropna(subset=[
                "start_station_id", "end_station_id",
                "start_station_name", "end_station_name",
                "start_lat", "start_lng", "end_lat", "end_lng",
            ])
            df["started_at"] = pd.to_datetime(df["started_at"], errors="coerce", utc=True)
            df["ended_at"]   = pd.to_datetime(df["ended_at"],   errors="coerce", utc=True)
            df = df.dropna(subset=["started_at", "ended_at"])
            df = df[df["ended_at"] > df["started_at"]]
            df = df[(df["ended_at"] - df["started_at"]) <= pd.Timedelta("24h")]
            df.to_sql(
                "data", engine, if_exists="append", index=False,
                method=psql_insert_copy, schema="public",
            )
    print("Trips loaded.")

# ── Borough GeoJSON → Postgres ────────────────────────────────────────────────

print("\n=== Borough GeoJSON → Postgres ===")

with engine.connect() as conn:
    boro_count = conn.execute(text("SELECT COUNT(*) FROM boroughs")).scalar()

if boro_count > 0:
    print(f"  already {boro_count} boroughs — skipping")
else:
    BOROUGHS_PATH = RAW / "nyc_boroughs.geojson"
    gdf = gpd.read_file(BOROUGHS_PATH)[["BoroName", "geometry"]].rename(
        columns={"BoroName": "boroname"}
    ).to_crs(4326)

    for _, row in gdf.iterrows():
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO boroughs (boroname, geom) "
                    "VALUES (:name, ST_GeomFromText(:geom, 4326))"
                ),
                {"name": row["boroname"], "geom": row["geometry"].wkt},
            )
    print(f"  loaded {len(gdf)} boroughs.")
