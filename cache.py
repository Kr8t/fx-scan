"""SQLite candle cache and freshness rules.

Freshness (``is_fresh``) - a pair's cache is reusable when ANY of these hold,
provided at least ``MIN_BARS`` bars are stored:

1. The newest cached bar is on or after the expected last *completed* trading
   day. That day is "yesterday (UTC)", rolled back over Saturday/Sunday, so on
   weekends and Monday morning a cached Friday bar is still fresh.
2. We fetched within ``RETRY_COOLDOWN`` already (covers bank holidays and
   yfinance lag, so a missing bar does not trigger a fetch on every run).

Only completed bars are ever stored (see ``data.drop_incomplete``).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pandas as pd

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    day    TEXT NOT NULL,
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    symbol     TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL
);
"""

COLUMNS = ["open", "high", "low", "close"]


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the cache database."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != config.SCHEMA_VERSION:
        # Candles cached under older day-boundary rules are not comparable; start clean.
        conn.executescript("DROP TABLE IF EXISTS candles; DROP TABLE IF EXISTS fetch_log;")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {int(config.SCHEMA_VERSION)}")
        conn.commit()
    return conn


def as_utc(moment: datetime) -> datetime:
    """Treat naive datetimes as UTC; convert aware ones to UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def session_label(moment: datetime) -> date:
    """Label of the trading session ``moment`` falls in.

    Sessions run cutoff-to-cutoff (17:00 New York) and are labelled with the day
    they close on, so Thursday 17:00 -> Friday 17:00 is "Friday" and the Sunday
    evening open belongs to Monday.
    """
    local = as_utc(moment).astimezone(ZoneInfo(config.SESSION_TZ)).replace(tzinfo=None)
    return (local + timedelta(hours=24 - config.SESSION_CUTOFF_HOUR)).date()


def expected_last_complete(now: datetime) -> date:
    """Label of the most recent fully closed weekday session."""
    day = session_label(now) - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def upsert_candles(
    conn: sqlite3.Connection,
    symbol: str,
    frame: pd.DataFrame,
    fetched_at: datetime,
) -> None:
    """Insert/update bars and stamp the fetch time."""
    values: list[list[float]] = frame[COLUMNS].to_numpy(dtype=float).tolist()
    rows = [
        (symbol, pd.Timestamp(ts).date().isoformat(), o, h, lo, c)
        for ts, (o, h, lo, c) in zip(frame.index, values, strict=True)
    ]
    with conn:
        conn.executemany(
            "INSERT INTO candles (symbol, day, open, high, low, close) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, day) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, "
            "low=excluded.low, close=excluded.close",
            rows,
        )
        conn.execute(
            "INSERT INTO fetch_log (symbol, fetched_at) VALUES (?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET fetched_at=excluded.fetched_at",
            (symbol, as_utc(fetched_at).isoformat()),
        )


def load_candles(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    """All cached bars for ``symbol`` as a date-indexed OHLC frame."""
    rows = conn.execute(
        "SELECT day, open, high, low, close FROM candles WHERE symbol = ? ORDER BY day",
        (symbol,),
    ).fetchall()
    frame = pd.DataFrame(rows, columns=["day", *COLUMNS])
    index = pd.DatetimeIndex(pd.to_datetime(frame["day"]))
    out = frame.drop(columns="day")
    out.index = index
    return cast("pd.DataFrame", out)


def count_bars(conn: sqlite3.Connection, symbol: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM candles WHERE symbol = ?", (symbol,)).fetchone()
    return int(row[0])


def latest_cached_date(conn: sqlite3.Connection, symbol: str) -> date | None:
    row = conn.execute("SELECT MAX(day) FROM candles WHERE symbol = ?", (symbol,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def last_fetch(conn: sqlite3.Connection, symbol: str) -> datetime | None:
    row = conn.execute("SELECT fetched_at FROM fetch_log WHERE symbol = ?", (symbol,)).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def is_fresh(conn: sqlite3.Connection, symbol: str, now: datetime) -> bool:
    """Apply the freshness rules described in the module docstring."""
    if count_bars(conn, symbol) < config.MIN_BARS:
        return False
    latest = latest_cached_date(conn, symbol)
    if latest is not None and latest >= expected_last_complete(now):
        return True
    fetched = last_fetch(conn, symbol)
    return fetched is not None and as_utc(now) - fetched < config.RETRY_COOLDOWN
