"""Download CitiBike summer-2023 trip data, NYC borough boundaries, and NYC OSM extract."""

import os
import subprocess
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

RAW = Path("data/raw")
RAW.mkdir(parents=True, exist_ok=True)

# ── CitiBike trip zips ────────────────────────────────────────────────────────

# 2023 data ships as a single annual zip on S3
CITIBIKE_2023_URL = "https://s3.amazonaws.com/tripdata/2023-citibike-tripdata.zip"
SUMMER_MONTHS = {"202306", "202307", "202308"}


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  skip (exists): {dest.name}")
        return
    print(f"  downloading: {dest.name}")
    r = requests.get(url, stream=True, timeout=300)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    with open(dest, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, leave=False) as bar:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
            bar.update(len(chunk))


def unzip_citibike_summer(zip_path: Path) -> None:
    """Extract only Jun/Jul/Aug CSVs from the annual zip.
    The outer zip contains nested monthly zips (202306-citibike-tripdata.zip, etc.)
    which in turn contain CSVs.
    """
    import io
    with zipfile.ZipFile(zip_path) as outer:
        for name in outer.namelist():
            stem = Path(name).name  # e.g. 202306-citibike-tripdata.zip
            month_prefix = stem[:6]
            if not stem.endswith(".zip") or month_prefix not in SUMMER_MONTHS:
                continue
            print(f"  opening nested zip: {stem}")
            monthly_bytes = io.BytesIO(outer.read(name))
            with zipfile.ZipFile(monthly_bytes) as inner:
                for csv_name in inner.namelist():
                    if not csv_name.endswith(".csv") or csv_name.startswith("__"):
                        continue
                    csv_stem = Path(csv_name).name
                    dest = RAW / csv_stem
                    if dest.exists():
                        print(f"    skip (exists): {csv_stem}")
                        continue
                    print(f"    extracting: {csv_stem}")
                    dest.write_bytes(inner.read(csv_name))


print("=== CitiBike trip data ===")
zip_path = RAW / "2023-citibike-tripdata.zip"
download(CITIBIKE_2023_URL, zip_path)
unzip_citibike_summer(zip_path)

# ── NYC Borough Boundaries (GeoJSON) ─────────────────────────────────────────

print("\n=== NYC Borough Boundaries ===")
BOROUGHS_URL = "https://raw.githubusercontent.com/dwillis/nyc-maps/master/boroughs.geojson"
BOROUGHS_DEST = RAW / "nyc_boroughs.geojson"
download(BOROUGHS_URL, BOROUGHS_DEST)

# ── OSM extract (Geofabrik New York state) ────────────────────────────────────

print("\n=== OSM extract ===")
OSM_URL = "https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf"
OSM_FULL = RAW / "new-york-latest.osm.pbf"
OSM_NYC = RAW / "nyc.osm.pbf"

download(OSM_URL, OSM_FULL)

if not OSM_NYC.exists():
    print("  clipping OSM to NYC bbox…")
    # bbox: min-lon,min-lat,max-lon,max-lat (NYC roughly)
    bbox = "-74.26,40.49,-73.69,40.92"
    subprocess.run(
        ["osmium", "extract", "--bbox", bbox, "-o", str(OSM_NYC), str(OSM_FULL)],
        check=True,
    )
    print(f"  clipped OSM: {OSM_NYC}")
else:
    print(f"  skip (exists): {OSM_NYC.name}")

# ── NYC Street Centerline (CSCL) ─────────────────────────────────────────────

print("\n=== NYC Street Centerline (CSCL) ===")
CENTERLINE_URL = (
    "https://data.cityofnewyork.us/api/geospatial/exjm-f27b"
    "?method=export&format=GeoJSON"
)
CENTERLINE_DEST = RAW / "nyc_centerline.geojson"
# Also accept a dated filename if already present (e.g. Centerline_20260530.geojson)
_centerline_exists = CENTERLINE_DEST.exists() or any(
    RAW.glob("Centerline_*.geojson")
)
if not _centerline_exists:
    download(CENTERLINE_URL, CENTERLINE_DEST)
else:
    print(f"  skip (exists): centerline already in {RAW}")

print("\nAll downloads complete.")
