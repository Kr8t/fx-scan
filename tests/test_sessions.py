"""Daily candles are cut at 17:00 New York, matching TradingView/OANDA."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import cache
import config
import data
import strategy
from tests.conftest import UTC


def _hourly(
    rows: list[tuple[str, float, float, float, float]], tz: str | None = "UTC"
) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    if tz:
        index = index.tz_localize(tz) if index.tz is None else index
    frame = pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
        },
        index=index,
    )
    return frame


class TestSessionsFromIntraday:
    def test_cutoff_splits_sessions_in_winter(self) -> None:
        # EST: 17:00 New York == 22:00 UTC.
        raw = _hourly(
            [
                ("2026-03-04 21:00", 1.0, 1.1, 0.9, 1.05),  # 16:00 NY Wed -> Wed
                ("2026-03-04 22:00", 2.0, 2.1, 1.9, 2.05),  # 17:00 NY Wed -> Thu
            ]
        )
        out = data.sessions_from_intraday(raw)
        assert list(out.index) == [pd.Timestamp("2026-03-04"), pd.Timestamp("2026-03-05")]

    def test_cutoff_moves_with_dst(self) -> None:
        # EDT: 17:00 New York == 21:00 UTC.
        raw = _hourly(
            [
                ("2026-07-01 20:00", 1.0, 1.1, 0.9, 1.05),
                ("2026-07-01 21:00", 2.0, 2.1, 1.9, 2.05),
            ]
        )
        out = data.sessions_from_intraday(raw)
        assert list(out.index) == [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")]

    def test_ohlc_aggregation(self) -> None:
        raw = _hourly(
            [
                ("2026-03-03 23:00", 1.00, 1.20, 0.95, 1.10),
                ("2026-03-04 10:00", 1.10, 1.30, 1.05, 1.25),
                ("2026-03-04 20:00", 1.25, 1.28, 0.90, 1.15),
            ]
        )
        row = data.sessions_from_intraday(raw).iloc[0]
        assert (row["open"], row["high"], row["low"], row["close"]) == (1.00, 1.30, 0.90, 1.15)

    def test_unsorted_input_uses_time_order_for_open_and_close(self) -> None:
        raw = _hourly(
            [
                ("2026-03-04 20:00", 1.25, 1.28, 0.90, 1.15),
                ("2026-03-03 23:00", 1.00, 1.20, 0.95, 1.10),
            ]
        )
        row = data.sessions_from_intraday(raw).iloc[0]
        assert row["open"] == 1.00
        assert row["close"] == 1.15

    def test_sunday_open_belongs_to_monday(self) -> None:
        raw = _hourly([("2026-03-01 22:00", 1.0, 1.1, 0.9, 1.05)])  # Sun 17:00 NY
        out = data.sessions_from_intraday(raw)
        assert list(out.index) == [pd.Timestamp("2026-03-02")]

    def test_post_friday_close_bars_are_dropped(self) -> None:
        raw = _hourly(
            [
                ("2026-03-06 20:00", 1.0, 1.1, 0.9, 1.05),  # Fri
                ("2026-03-06 22:00", 1.0, 1.1, 0.9, 1.05),  # Fri 17:00 NY -> Saturday label
            ]
        )
        out = data.sessions_from_intraday(raw)
        assert list(out.index) == [pd.Timestamp("2026-03-06")]

    def test_naive_index_treated_as_utc(self) -> None:
        raw = _hourly([("2026-03-04 22:00", 1.0, 1.1, 0.9, 1.05)], tz=None)
        assert list(data.sessions_from_intraday(raw).index) == [pd.Timestamp("2026-03-05")]

    def test_other_source_timezone_gives_same_sessions(self) -> None:
        london = _hourly([("2026-03-04 22:00", 1.0, 1.1, 0.9, 1.05)], tz="Europe/London")
        assert list(data.sessions_from_intraday(london).index) == [pd.Timestamp("2026-03-05")]

    def test_empty_input(self) -> None:
        assert data.sessions_from_intraday(pd.DataFrame()).empty


class TestChfjpyRegression:
    def test_wick_above_prior_high_closing_inside_is_no_signal(self) -> None:
        """Fri 30 Oct 2026: spiked above Thu's high (191.36) but closed ~191.04, inside."""
        raw = _hourly(
            [
                # Thursday session (EDT: Wed 21:00Z -> Thu 20:00Z)
                ("2026-10-28 21:00", 190.90, 191.00, 190.80, 190.90),
                ("2026-10-29 05:00", 190.90, 190.95, 190.33, 190.50),
                ("2026-10-29 18:00", 190.50, 191.36, 190.50, 191.30),
                # Friday session (Thu 21:00Z -> Fri 20:00Z)
                ("2026-10-29 22:00", 191.30, 191.90, 191.20, 191.50),
                ("2026-10-30 12:00", 191.50, 191.50, 190.53, 190.70),
                ("2026-10-30 20:00", 190.70, 191.10, 190.70, 191.04),
            ]
        )
        bars = data.to_bars(data.sessions_from_intraday(raw))
        assert [b.day for b in bars] == [date(2026, 10, 29), date(2026, 10, 30)]
        assert strategy.evaluate("CHFJPY", bars) is None


class TestSessionClock:
    def test_friday_just_before_close_is_not_complete(self) -> None:
        now = datetime(2026, 3, 6, 21, 30, tzinfo=UTC)  # 16:30 EST Fri
        assert cache.expected_last_complete(now) == date(2026, 3, 5)

    def test_friday_just_after_close_is_complete(self) -> None:
        now = datetime(2026, 3, 6, 22, 30, tzinfo=UTC)  # 17:30 EST Fri
        assert cache.expected_last_complete(now) == date(2026, 3, 6)

    def test_forming_session_is_dropped_at_cutoff(self) -> None:
        frame = pd.DataFrame(
            {"open": [1.0, 1.0], "high": [1.1, 1.1], "low": [0.9, 0.9], "close": [1.0, 1.0]},
            index=pd.DatetimeIndex(["2026-03-05", "2026-03-06"]),
        )
        before = data.drop_incomplete(frame, datetime(2026, 3, 6, 21, 30, tzinfo=UTC))
        after = data.drop_incomplete(frame, datetime(2026, 3, 6, 22, 30, tzinfo=UTC))
        assert len(before) == 1
        assert len(after) == 2


class TestSchemaVersion:
    def test_old_cache_is_wiped(self, tmp_path: Path) -> None:
        path = tmp_path / "old.sqlite"
        raw = sqlite3.connect(path)  # pre-v2 layout, user_version 0
        raw.executescript(
            "CREATE TABLE candles (symbol TEXT, day TEXT, open REAL, high REAL, low REAL,"
            " close REAL, PRIMARY KEY (symbol, day));"
            "INSERT INTO candles VALUES ('EURUSD','2026-03-03',1,1,1,1);"
        )
        raw.close()
        conn = cache.connect(path)
        assert cache.count_bars(conn, "EURUSD") == 0
        conn.close()

    def test_current_cache_is_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "new.sqlite"
        conn = cache.connect(path)
        conn.execute("INSERT INTO candles VALUES ('EURUSD','2026-03-03',1,1,1,1)")
        conn.commit()
        conn.close()
        conn = cache.connect(path)
        assert cache.count_bars(conn, "EURUSD") == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == config.SCHEMA_VERSION
        conn.close()
