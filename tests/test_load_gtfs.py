"""Tests for scripts/load_gtfs.py.

Unit tests run anywhere. The integration test runs only when
TEST_DATABASE_URL points at a database initialised with db/init/*.sql
(for example the docker compose database):

    TEST_DATABASE_URL=postgresql://transit_admin:...@localhost:5432/transit pytest -v
"""
import datetime as dt
import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import load_gtfs as lg  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "mini_gtfs"


# ---------------------------------------------------------------- converters

@pytest.mark.parametrize("raw,expected", [
    ("05:12:00", 18720),
    ("7:00:00", 25200),         # single-digit hour is valid GTFS
    ("24:05:00", 86700),        # past midnight on the same service day
    ("25:10:00", 90600),
    ("", None),
    (None, None),
])
def test_time_to_secs(raw, expected):
    assert lg.time_to_secs(raw) == expected


def test_to_date():
    assert lg.to_date("20261225") == dt.date(2026, 12, 25)
    assert lg.to_date("  ") is None


def test_to_int_handles_float_strings_and_blanks():
    assert lg.to_int("1") == 1
    assert lg.to_int("1.0") == 1
    assert lg.to_int("") is None


# ---------------------------------------------------------------- row mapping

def test_rows_map_by_header_not_position():
    # trips.txt in the fixture puts route_id first, unlike the schema order
    warnings = []
    with lg.FeedSource(dir_path=FIXTURE).open("trips") as fh:
        cols, rows = lg.rows_for("trips", fh, warnings)
        first = dict(zip(cols, next(rows), strict=True))
    assert first["trip_id"] == "T4_WD_1"
    assert first["route_id"] == "T4_EX"
    assert first["direction_id"] == 0


def test_extra_columns_are_reported_and_ignored():
    warnings = []
    with lg.FeedSource(dir_path=FIXTURE).open("routes") as fh:
        cols, rows = lg.rows_for("routes", fh, warnings)
        list(rows)
    assert "route_url" not in cols
    assert any("route_url" in w for w in warnings)


def test_bom_is_stripped():
    warnings = []
    with lg.FeedSource(dir_path=FIXTURE).open("agency") as fh:
        cols, rows = lg.rows_for("agency", fh, warnings)
        first = dict(zip(cols, next(rows), strict=True))
    assert first["agency_id"] == "SYDT"


def test_stop_times_derive_seconds():
    warnings = []
    with lg.FeedSource(dir_path=FIXTURE).open("stop_times") as fh:
        cols, rows = lg.rows_for("stop_times", fh, warnings)
        late = [dict(zip(cols, r, strict=True)) for r in rows if r[0] == "T4_WD_LATE"]
    assert late[0]["departure_time"] == "24:05:00"
    assert late[0]["departure_secs"] == 86700


def test_bad_row_reports_line_number():
    fh = io.StringIO("service_id,date,exception_type\nWKDY,2026-12-25,2\n")
    _, rows = lg.rows_for("calendar_dates", fh, [])
    with pytest.raises(ValueError, match="line 2"):
        list(rows)


# ---------------------------------------------------------------- integration

DSN = os.getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run against Postgres")
def test_load_fixture_end_to_end():
    import psycopg

    lg.load(lg.FeedSource(dir_path=FIXTURE), DSN, with_shapes=False, skip_indexes=False)
    # autocommit: an open read transaction here would block the second
    # load's TRUNCATE and hang the test
    with psycopg.connect(DSN, autocommit=True) as conn:
        counts = dict(conn.execute(
            "SELECT 'stops', count(*) FROM gtfs.stops UNION ALL "
            "SELECT 'stop_times', count(*) FROM gtfs.stop_times UNION ALL "
            "SELECT 'trips', count(*) FROM gtfs.trips"
        ).fetchall())
        assert counts == {"stops": 11, "stop_times": 20, "trips": 7}

        # Loading twice replaces, never duplicates
        lg.load(lg.FeedSource(dir_path=FIXTURE), DSN, with_shapes=False, skip_indexes=True)
        assert conn.execute("SELECT count(*) FROM gtfs.stop_times").fetchone()[0] == 20

        # Fuzzy stop resolution works with a typo
        best = conn.execute(
            "SELECT stop_name FROM gtfs.stops WHERE location_type = 1 "
            "ORDER BY similarity(stop_name, 'hurstvile stn') DESC LIMIT 1"
        ).fetchone()[0]
        assert best == "Hurstville Station"
