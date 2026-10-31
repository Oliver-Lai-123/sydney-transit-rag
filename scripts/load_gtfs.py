#!/usr/bin/env python3
"""Load a GTFS static feed into the gtfs schema.

Sources (pick one):
  --download        Fetch the TfNSW "Timetables Complete GTFS" zip (needs TFNSW_API_KEY)
  --zip PATH        A GTFS zip you already have
  --dir PATH        A folder of GTFS .txt files (e.g. tests/fixtures/mini_gtfs)

The whole load runs in one transaction: if anything fails, the previous
timetable stays in place untouched.

Examples:
  python scripts/load_gtfs.py --dir tests/fixtures/mini_gtfs
  python scripts/load_gtfs.py --download
  python scripts/load_gtfs.py --zip data/raw/gtfs_complete_20261027.zip --with-shapes
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TFNSW_GTFS_URL = "https://api.transport.nsw.gov.au/v1/publictransport/timetables/complete/gtfs"
POST_LOAD_SQL = ROOT / "db" / "post_load_indexes.sql"

# csv module default field limit (128 KB) is too small for some route_desc fields
csv.field_size_limit(10 * 1024 * 1024)


# --------------------------------------------------------------------------- #
# Value converters. Each takes the raw CSV string and returns a Python value
# psycopg can COPY. Empty strings become NULL everywhere.
# --------------------------------------------------------------------------- #

def to_text(v: str) -> str | None:
    v = v.strip()
    return v or None


def to_int(v: str) -> int | None:
    v = v.strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return int(float(v))  # some feeds publish "1.0"


def to_float(v: str) -> float | None:
    v = v.strip()
    return float(v) if v else None


def to_date(v: str) -> dt.date | None:
    """GTFS dates are YYYYMMDD."""
    v = v.strip()
    if not v:
        return None
    return dt.date(int(v[0:4]), int(v[4:6]), int(v[6:8]))


def time_to_secs(v: str | None) -> int | None:
    """'25:10:00' -> 90600. GTFS times can exceed 24h; hours may be 1 digit."""
    if not v:
        return None
    h, m, s = v.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


# --------------------------------------------------------------------------- #
# Table specs: GTFS file -> (column, converter) in schema order.
# Columns the feed has but we do not list are ignored (and reported).
# Columns we list but the feed lacks are loaded as NULL.
# --------------------------------------------------------------------------- #

Spec = list[tuple[str, Callable[[str], object]]]

TABLES: dict[str, Spec] = {
    "agency": [
        ("agency_id", to_text), ("agency_name", to_text), ("agency_url", to_text),
        ("agency_timezone", to_text), ("agency_lang", to_text), ("agency_phone", to_text),
    ],
    "calendar": [
        ("service_id", to_text),
        ("monday", to_int), ("tuesday", to_int), ("wednesday", to_int), ("thursday", to_int),
        ("friday", to_int), ("saturday", to_int), ("sunday", to_int),
        ("start_date", to_date), ("end_date", to_date),
    ],
    "calendar_dates": [
        ("service_id", to_text), ("date", to_date), ("exception_type", to_int),
    ],
    "routes": [
        ("route_id", to_text), ("agency_id", to_text), ("route_short_name", to_text),
        ("route_long_name", to_text), ("route_desc", to_text), ("route_type", to_int),
        ("route_color", to_text), ("route_text_color", to_text),
    ],
    "stops": [
        ("stop_id", to_text), ("stop_code", to_text), ("stop_name", to_text),
        ("stop_lat", to_float), ("stop_lon", to_float), ("location_type", to_int),
        ("parent_station", to_text), ("wheelchair_boarding", to_int), ("platform_code", to_text),
    ],
    "trips": [
        ("trip_id", to_text), ("route_id", to_text), ("service_id", to_text),
        ("trip_headsign", to_text), ("trip_short_name", to_text), ("direction_id", to_int),
        ("block_id", to_text), ("shape_id", to_text), ("wheelchair_accessible", to_int),
    ],
    "stop_times": [
        ("trip_id", to_text), ("arrival_time", to_text), ("departure_time", to_text),
        ("stop_id", to_text), ("stop_sequence", to_int), ("stop_headsign", to_text),
        ("pickup_type", to_int), ("drop_off_type", to_int),
        ("shape_dist_traveled", to_float), ("timepoint", to_int),
    ],
    "shapes": [
        ("shape_id", to_text), ("shape_pt_lat", to_float), ("shape_pt_lon", to_float),
        ("shape_pt_sequence", to_int), ("shape_dist_traveled", to_float),
    ],
}

# Columns computed from other columns rather than read from the file
DERIVED: dict[str, list[tuple[str, Callable[[dict], object]]]] = {
    "stop_times": [
        ("arrival_secs", lambda r: time_to_secs(r["arrival_time"])),
        ("departure_secs", lambda r: time_to_secs(r["departure_time"])),
    ],
}

REQUIRED_FILES = ["agency", "routes", "stops", "trips", "stop_times"]
LOAD_ORDER = ["agency", "calendar", "calendar_dates", "routes", "stops", "trips", "stop_times", "shapes"]

# Post-load integrity checks: (label, SQL returning a count of bad rows)
INTEGRITY_CHECKS = [
    ("trips with unknown route_id",
     "SELECT count(*) FROM gtfs.trips t "
     "WHERE NOT EXISTS (SELECT 1 FROM gtfs.routes r WHERE r.route_id = t.route_id)"),
    ("trips with no calendar entry",
     "SELECT count(*) FROM gtfs.trips t "
     "WHERE NOT EXISTS (SELECT 1 FROM gtfs.calendar c WHERE c.service_id = t.service_id) "
     "AND NOT EXISTS (SELECT 1 FROM gtfs.calendar_dates d WHERE d.service_id = t.service_id)"),
    ("stop_times with unknown stop_id",
     "SELECT count(*) FROM gtfs.stop_times st "
     "WHERE NOT EXISTS (SELECT 1 FROM gtfs.stops s WHERE s.stop_id = st.stop_id)"),
    ("platforms whose parent_station is missing",
     "SELECT count(*) FROM gtfs.stops s WHERE s.parent_station IS NOT NULL "
     "AND NOT EXISTS (SELECT 1 FROM gtfs.stops p WHERE p.stop_id = s.parent_station)"),
]


# --------------------------------------------------------------------------- #
# Feed sources
# --------------------------------------------------------------------------- #

class FeedSource:
    """Uniform access to GTFS files in a zip or a directory."""

    def __init__(self, zip_path: Path | None = None, dir_path: Path | None = None):
        self.zip = zipfile.ZipFile(zip_path) if zip_path else None
        self.dir = dir_path
        self.label = str(zip_path or dir_path)

    def _zip_member(self, name: str) -> str | None:
        # Some feeds nest files in a folder inside the zip
        for member in self.zip.namelist():
            if member == name or member.endswith("/" + name):
                return member
        return None

    def has(self, table: str) -> bool:
        name = f"{table}.txt"
        if self.zip:
            return self._zip_member(name) is not None
        return (self.dir / name).exists()

    @contextmanager
    def open(self, table: str) -> Iterator[io.TextIOBase]:
        name = f"{table}.txt"
        if self.zip:
            raw = self.zip.open(self._zip_member(name))
            # utf-8-sig strips the byte-order mark some publishers add
            with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as fh:
                yield fh
        else:
            with open(self.dir / name, encoding="utf-8-sig", newline="") as fh:
                yield fh


def download_feed(api_key: str, dest_dir: Path) -> Path:
    import requests

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"gtfs_complete_{dt.date.today():%Y%m%d}.zip"
    print(f"Downloading TfNSW GTFS feed to {dest} ...")
    with requests.get(
        TFNSW_GTFS_URL,
        headers={"Authorization": f"apikey {api_key}"},
        stream=True,
        timeout=(10, 300),
    ) as resp:
        if resp.status_code in (401, 403):
            sys.exit(f"TfNSW API refused the key (HTTP {resp.status_code}). "
                     "Check TFNSW_API_KEY and that the key has access to the Timetables Complete GTFS API.")
        resp.raise_for_status()
        done = 0
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as out:
            for block in resp.iter_content(chunk_size=1 << 20):
                out.write(block)
                done += len(block)
                print(f"\r  {done / 1e6:,.0f} MB", end="", flush=True)
        tmp.replace(dest)
    print()
    if not zipfile.is_zipfile(dest):
        sys.exit(f"{dest} is not a zip file; the API may have returned an error page.")
    return dest


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def rows_for(table: str, fh: io.TextIOBase, warnings: list[str]) -> tuple[list[str], Iterator[tuple]]:
    """Return (column list for COPY, iterator of converted rows)."""
    reader = csv.reader(fh)
    header = [h.strip() for h in next(reader)]
    spec = TABLES[table]
    known = {c for c, _ in spec}

    extra = [h for h in header if h not in known]
    if extra:
        warnings.append(f"{table}.txt: ignored columns {extra}")
    missing = [c for c, _ in spec if c not in header]
    if missing:
        warnings.append(f"{table}.txt: columns not in feed, loaded as NULL: {missing}")

    index = {h: i for i, h in enumerate(header)}
    plan = [(c, conv, index.get(c)) for c, conv in spec]
    derived = DERIVED.get(table, [])
    columns = [c for c, _ in spec] + [c for c, _ in derived]

    def gen() -> Iterator[tuple]:
        for line_no, raw in enumerate(reader, start=2):
            if not raw or (len(raw) == 1 and not raw[0].strip()):
                continue  # blank line
            try:
                rec = {c: (conv(raw[i]) if i is not None and i < len(raw) else None) for c, conv, i in plan}
                for c, fn in derived:
                    rec[c] = fn(rec)
            except Exception as exc:  # surface the exact bad line, then stop
                raise ValueError(f"{table}.txt line {line_no}: {exc} :: {raw}") from exc
            yield tuple(rec[c] for c in columns)

    return columns, gen()


def copy_table(cur, table: str, source: FeedSource, warnings: list[str]) -> int:
    with source.open(table) as fh:
        columns, rows = rows_for(table, fh, warnings)
        cols_sql = ", ".join(columns)
        n = 0
        t0 = time.time()
        with cur.copy(f"COPY gtfs.{table} ({cols_sql}) FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)
                n += 1
                if n % 1_000_000 == 0:
                    print(f"\r  {table}: {n:,} rows ({n / (time.time() - t0):,.0f}/s)", end="", flush=True)
    print(f"\r  {table}: {n:,} rows in {time.time() - t0:.1f}s" + " " * 20)
    return n


def load(source: FeedSource, dsn: str, with_shapes: bool, skip_indexes: bool) -> None:
    import psycopg

    missing = [t for t in REQUIRED_FILES if not source.has(t)]
    if missing:
        sys.exit(f"Feed is missing required files: {', '.join(t + '.txt' for t in missing)}")
    if not source.has("calendar") and not source.has("calendar_dates"):
        sys.exit("Feed needs calendar.txt or calendar_dates.txt")

    tables = [t for t in LOAD_ORDER if source.has(t) and (t != "shapes" or with_shapes)]
    warnings: list[str] = []
    counts: dict[str, int] = {}

    print(f"Loading {source.label}")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Clear everything first so a partial feed never mixes with old data.
            # Shapes are cleared only when we are reloading them.
            cur.execute("TRUNCATE " + ", ".join(f"gtfs.{t}" for t in tables))
            for table in tables:
                counts[table] = copy_table(cur, table, source, warnings)

            if not skip_indexes:
                print("Building indexes ...")
                cur.execute(POST_LOAD_SQL.read_text())

            print("Integrity checks ...")
            for label, sql in INTEGRITY_CHECKS:
                cur.execute(sql)
                bad = cur.fetchone()[0]
                if bad:
                    warnings.append(f"{bad:,} {label}")
                print(f"  {'OK  ' if not bad else 'WARN'} {label}: {bad:,}")

            cur.execute(
                "INSERT INTO gtfs.feed_load (source, row_counts, warnings) VALUES (%s, %s, %s) RETURNING load_id",
                (source.label, json.dumps(counts), json.dumps(warnings)),
            )
            load_id = cur.fetchone()[0]
        conn.commit()

        # Refresh planner statistics so the first text-to-SQL queries get good plans
        conn.autocommit = True
        conn.execute("ANALYZE " + ", ".join(f"gtfs.{t}" for t in tables))

    print(f"\nLoad {load_id} committed.")
    for t, n in counts.items():
        print(f"  {t:<16}{n:>14,}")
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  - {w}")


def main(argv: list[str] | None = None) -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--download", action="store_true", help="download the TfNSW feed (needs TFNSW_API_KEY)")
    src.add_argument("--zip", type=Path, help="path to a GTFS zip")
    src.add_argument("--dir", type=Path, help="path to a folder of GTFS .txt files")
    p.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="admin connection string (default: DATABASE_URL)")
    p.add_argument("--with-shapes", action="store_true", help="also load shapes.txt (large, rarely needed)")
    p.add_argument("--skip-indexes", action="store_true", help="skip post-load index build")
    p.add_argument("--raw-dir", type=Path, default=ROOT / "data" / "raw", help="where --download saves the zip")
    args = p.parse_args(argv)

    if not args.dsn:
        sys.exit("No database connection: set DATABASE_URL in .env or pass --dsn")

    if args.download:
        key = os.getenv("TFNSW_API_KEY")
        if not key:
            sys.exit("TFNSW_API_KEY is not set. Add it to .env (see .env.example).")
        source = FeedSource(zip_path=download_feed(key, args.raw_dir))
    elif args.zip:
        source = FeedSource(zip_path=args.zip)
    else:
        source = FeedSource(dir_path=args.dir)

    load(source, args.dsn, with_shapes=args.with_shapes, skip_indexes=args.skip_indexes)


if __name__ == "__main__":
    main()
